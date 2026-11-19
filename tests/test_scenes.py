"""Проверочная часть сцен (deploy/scenes): копии шардов, проба записи, итог нагрузки.
Пиры Qdrant подставные; оркестровка на shell проверяется только на живом стенде."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests.fakes import KEY, FakeServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "deploy", "scenes", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


replicas = load("replicas")
outage = load("outage_writes")
load_summary = load("load_summary")


def peer(local_shards: list[tuple[int, str, int]], rf: int = 2):
    def handler(method, path, headers, body):
        if headers.get("api-key") != KEY:
            return 401, {}
        if path.endswith("/cluster"):
            return 200, {"result": {"local_shards": [{"shard_id": s, "state": st, "points_count": n}
                                                     for s, st, n in local_shards]}}
        return 200, {"result": {"config": {"params": {"replication_factor": rf}}}}
    return handler


class ReplicasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.servers = []

    def tearDown(self) -> None:
        for s in self.servers:
            s.close()

    def pods(self, layout: dict[str, list]) -> list[list[str]]:
        out = []
        for node, shards in layout.items():
            s = FakeServer(peer(shards))
            self.servers.append(s)
            out.append([f"qdrant-{node[-1]}", node, s.url.rsplit(":", 1)[1]])
        return out

    def test_healthy(self):
        pods = self.pods({"machine-1": [(0, "Active", 10), (1, "Active", 7)],
                          "machine-2": [(0, "Active", 10), (2, "Active", 5)],
                          "machine-3": [(1, "Active", 7), (2, "Active", 5)]})
        self.assertEqual(replicas.problems(*replicas.snapshot(pods, KEY, "c")), [])

    def test_catching_up_and_diverged(self):
        pods = self.pods({"machine-1": [(0, "Active", 10), (1, "Active", 7)],
                          "machine-2": [(0, "Partial", 4), (2, "Active", 5)],
                          "machine-3": [(1, "Active", 6), (2, "Active", 5)]})
        issues = replicas.problems(*replicas.snapshot(pods, KEY, "c"))
        self.assertIn("шард 0: machine-2 Partial", issues)
        self.assertTrue(any(i.startswith("шард 1: у копий разное число точек") for i in issues))
        self.assertTrue(any(i.startswith("шард 0: у копий разное число точек") for i in issues))

    def test_dead_peer(self):
        pods = self.pods({"machine-1": [(0, "Active", 10)], "machine-3": [(1, "Active", 7)]})
        pods.append(["qdrant-1", "machine-2", "1"])  # туннель в никуда
        issues = replicas.problems(*replicas.snapshot(pods, KEY, "c"))
        self.assertIn("пир на machine-2 не отвечает", issues)
        self.assertIn("шард 0: копий 1 из 2", issues)

    def test_cli_exit_code(self):
        pods = self.pods({"machine-1": [(0, "Active", 1)], "machine-2": [(0, "Active", 1)]})
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "key"), "w") as f:
                f.write(KEY)
            with open(os.path.join(tmp, "pods"), "w") as f:
                f.writelines(" ".join(p) + "\n" for p in pods)
            argv = ["replicas.py", os.path.join(tmp, "key"), os.path.join(tmp, "pods"), "c"]
            with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print") as out:
                with self.assertRaises(SystemExit) as ctx:
                    replicas.main()
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("все копии Active и совпадают", out.call_args_list[0].args[0])


class OutageWritesTest(unittest.TestCase):
    def test_write_then_verify(self):
        stored: dict[int, dict] = {}
        calls = {"n": 0}

        def handler(method, path, headers, body):
            req = json.loads(body) if body else {}
            if method in ("DELETE",):
                return 404, {}
            if method == "PUT" and path == "/collections/outage_rf2":
                self.assertEqual((req["shard_number"], req["replication_factor"]), (6, 2))
                return 200, {"result": True}
            if method == "PUT" and path.startswith("/collections/outage_rf2/points"):
                calls["n"] += 1
                if 3 <= calls["n"] <= 4:  # «машина выключилась»: две записи не подтверждены
                    return 503, {"status": {"error": "replica unavailable"}}
                for p in req["points"]:
                    stored[p["id"]] = p
                return 200, {"result": {"status": "completed"}}
            if method == "POST" and path == "/collections/outage_rf2/points":
                return 200, {"result": [{"id": i} for i in req["ids"] if i in stored]}
            if method == "POST" and path.endswith("/points/count"):
                return 200, {"result": {"count": len(stored)}}
            return 404, {}
        q = FakeServer(handler)
        self.addCleanup(q.close)
        with tempfile.TemporaryDirectory() as tmp:
            key, log = os.path.join(tmp, "key"), os.path.join(tmp, "w.json")
            with open(key, "w") as f:
                f.write(KEY)
            base = ["outage_writes.py", "--url", q.url, "--key-file", key]
            with mock.patch.object(sys, "argv", base + ["write", "--seconds", "0.3", "--rate", "20", "--out", log]), \
                    mock.patch("builtins.print"):
                outage.main()
            with open(log) as f:
                writes = json.load(f)["writes"]
            self.assertEqual(len(writes), 6)
            self.assertEqual([w["ok"] for w in writes], [True, True, False, False, True, True])
            self.assertEqual(writes[2]["error"], "HTTP 503")
            stored.pop(5)  # подтверждённая запись «потерялась» при догонке копии
            with mock.patch.object(sys, "argv", base + ["verify", "--log", log]), mock.patch("builtins.print") as out:
                outage.main()
            s = json.loads(out.call_args_list[1].args[0])
            self.assertEqual((s["sent"], s["acked"], s["failed"]), (6, 4, 2))
            self.assertEqual(s["acked_missing"], [5])
            self.assertEqual(s["points_in_collection"], 3)
            self.assertEqual(s["errors"], ["HTTP 503"])

    def test_vector_deterministic(self):
        self.assertEqual(outage.vector(3), outage.vector(3))
        self.assertEqual(len(outage.vector(0)), outage.DIM)


class LoadSummaryTest(unittest.TestCase):
    def test_summary(self):
        snap = {"totals": {"count": 2400, "skipped": 0, "retries": 31,
                           "without_retry": {"errors": 29, "rate": 29 / 2400},
                           "with_retry": {"errors": 0, "rate": 0.0}},
                "series": [{"p95": 40, "p99": 60, "errors_with_retry": 0, "retries": 0},
                           {"p95": 540, "p99": 900, "errors_with_retry": 0, "retries": 12},
                           {"p95": None, "p99": None, "errors_with_retry": 0, "retries": 0}]}
        text = load_summary.summary(snap)
        self.assertIn("ошибок у пользователя 0 (0.00 %), без повтора было бы 29 (1.21 %), повторов 31", text)
        self.assertIn("худший p95 540 мс, худший p99 900 мс, секунд с ошибками 0, секунд с повторами 1", text)


if __name__ == "__main__":
    unittest.main()
