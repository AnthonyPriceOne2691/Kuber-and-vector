"""Замер предела на синтетике: векторы, сторож, загрузка ступени, лестница нагрузки, отчёт.
Qdrant, kubelet и вход подставные; оркестровка на shell (deploy/limit/limit.sh) проверяется только
на живом стенде."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from eval import limit_qps, limit_report
from eval import limit_synth as ls
from tests.fakes import KEY, FakeServer, ollama_handler, qdrant_handler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GI, MI = 2 ** 30, 2 ** 20
NODES = ("machine-1", "machine-2", "machine-3")


class FakeQdrant:
    """Qdrant для загрузчика: коллекции, точки по id. Статус индекса задаёт тест; в c["since"]
    число опросов коллекции после последней записи."""

    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.status = lambda c: "green"
        self.upserts: list[list[int]] = []
        self.calls: list[tuple[str, str]] = []
        self.server = FakeServer(self.handle)

    def add(self, name: str, cfg: dict, n: int = 0) -> None:
        self.collections[name] = {"config": cfg, "points": {i: None for i in range(n)}, "patched": 0, "since": 0}

    def handle(self, method, path, headers, body):
        if headers.get("api-key") != KEY:
            return 401, {"status": {"error": "Must provide an API key"}}
        base = path.split("?")[0]
        self.calls.append((method, base))
        req = json.loads(body) if body else None
        parts = base.strip("/").split("/")
        if base == "/collections" and method == "GET":
            return 200, {"result": {"collections": [{"name": n} for n in self.collections]}}
        name = parts[1]
        c = self.collections.get(name)
        if len(parts) == 2 and method == "PUT":
            self.add(name, req)
            return 200, {"result": True}
        if c is None:
            return 404, {"status": {"error": f"Collection {name} not found"}}
        if len(parts) == 2:
            if method == "DELETE":
                del self.collections[name]
                return 200, {"result": True}
            if method == "PATCH":
                c["patched"] += 1
                return 200, {"result": True}
            cfg = c["config"]
            c["since"] += 1
            return 200, {"result": {
                "status": self.status(c), "optimizer_status": "ok", "points_count": len(c["points"]),
                "indexed_vectors_count": len(c["points"]), "segments_count": 4,
                "config": {"params": {"vectors": cfg["vectors"], "shard_number": cfg["shard_number"],
                                      "replication_factor": cfg["replication_factor"]},
                           "quantization_config": cfg.get("quantization_config")}}}
        if parts[2:] == ["points"] and method == "PUT":
            self.upserts.append([p["id"] for p in req["points"]])
            c["since"] = 0
            for p in req["points"]:
                c["points"][p["id"]] = p
            return 200, {"result": {"status": "completed"}}
        if parts[2:] == ["points"] and method == "POST":
            out = []
            for i in req["ids"]:
                p = c["points"].get(i)
                if p:
                    v = p["vector"]["dense"]
                    n = sum(x * x for x in v) ** 0.5
                    out.append({"id": i, "payload": p["payload"], "vector": {"dense": [x / n for x in v]}})
            return 200, {"result": out}
        if parts[2:] == ["points", "count"]:
            return 200, {"result": {"count": len(c["points"])}}
        return 404, {}

    def points(self, name: str) -> int:
        return len(self.collections[name]["points"]) if name in self.collections else 0


class FakeKube:
    """kubectl для сторожа: узлы, поды Qdrant (и шлюз рядом), stats/summary от kubelet."""

    def __init__(self, rss=lambda: 0.4 * GI) -> None:
        self.rss = rss
        self.limit = "2Gi"
        self.avail = 1.5 * GI
        self.fs_cap, self.fs_avail = 20 * 10 ** 9, 12 * 10 ** 9
        self.pressure = lambda: {}
        self.ready = True
        self.restarts = 0
        self.fail = 0
        self.calls = 0

    def __call__(self, args):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise ls.GuardError("kubectl get: API не отвечает")
        if args[:2] == ["get", "nodes"]:
            pressure = self.pressure()
            return {"items": [{"metadata": {"name": n}, "status": {
                "capacity": {"memory": "4013236Ki"},
                "conditions": [{"type": "Ready", "status": "True" if self.ready else "False"}]
                + [{"type": t, "status": "True" if pressure.get(n) == t else "False"} for t in ls.PRESSURE]}}
                for n in NODES]}
        if "pods" in args:
            assert ls.QDRANT_SELECTOR in args  # шлюз qdrant-gate за пира не считаем
            return {"items": [{"metadata": {"name": f"qdrant-{i}"}, "spec": {"nodeName": n, "containers": [
                {"name": "qdrant", "resources": {"limits": {"memory": self.limit}}}]},
                "status": {"containerStatuses": [{"name": "qdrant", "ready": True, "restartCount": self.restarts,
                                                  "lastState": {"terminated": {"reason": "OOMKilled"}}
                                                  if self.restarts else {}}]}} for i, n in enumerate(NODES)]}
        if args[:2] == ["get", "--raw"]:
            node = args[2].split("/")[4]
            rss = int(self.rss())
            return {"node": {"nodeName": node, "memory": {"availableBytes": int(self.avail), "workingSetBytes": GI},
                             "fs": {"capacityBytes": self.fs_cap, "availableBytes": self.fs_avail}},
                    "pods": [{"podRef": {"name": "qdrant-gate-abcde", "namespace": "qdrant"},
                              "containers": [{"name": "gate", "memory": {"rssBytes": 1}}]},
                             {"podRef": {"name": f"qdrant-{NODES.index(node)}", "namespace": "qdrant"},
                              "containers": [{"name": "qdrant", "memory": {"rssBytes": rss,
                                                                           "workingSetBytes": rss + GI // 4}}]}]}
        raise AssertionError(args)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class SyntheticPointsTest(unittest.TestCase):
    def test_vectors_are_reproducible_small_integers(self):
        v = ls.vector(5, 1, 1024)
        self.assertEqual(v, ls.vector(5, 1, 1024))
        self.assertNotEqual(v, ls.vector(6, 1, 1024))
        self.assertNotEqual(v, ls.vector(5, 2, 1024))
        self.assertEqual(len(v), 1024)
        self.assertTrue(all(isinstance(x, int) and -128 <= x <= 127 for x in v))
        body = json.loads(ls.batch_body(10, 13, 1, 8))
        self.assertEqual([p["id"] for p in body["points"]], [10, 11, 12])
        self.assertEqual(body["points"][0]["vector"]["dense"], ls.vector(10, 1, 8))
        self.assertLess(len(ls.batch_body(0, 100, 1, 1024)) / 100, 4000)  # целые: около 3,9 КБ на точку

    def test_collection_config(self):
        cfg = ls.collection_config(1024, 6, 2, "int8", True)
        self.assertEqual(cfg["vectors"], {"dense": {"size": 1024, "distance": "Cosine", "on_disk": True}})
        self.assertEqual((cfg["shard_number"], cfg["replication_factor"], cfg["on_disk_payload"]), (6, 2, True))
        self.assertEqual(cfg["quantization_config"], {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}})
        self.assertNotIn("hnsw_config", cfg)
        plain = ls.collection_config(1024, 3, 3, None, False, hnsw_m=8, ef_construct=64)
        self.assertNotIn("quantization_config", plain)
        self.assertNotIn("on_disk", plain["vectors"]["dense"])
        self.assertEqual(plain["hnsw_config"], {"m": 8, "ef_construct": 64})

    def test_names_only_synth(self):
        self.assertEqual(ls.check_name("synth_rf2"), "synth_rf2")
        for bad in ("contracts_rf2", "synth_", "synth_../x", "SYNTH_rf2", "outage_rf2", "synth_rf2 "):
            with self.assertRaises(ValueError):
                ls.check_name(bad)

    def test_search_service_reads_synthetic_payload(self):
        try:
            from app import api
        except ImportError:  # без FastAPI сервис не импортируется
            self.skipTest("нет FastAPI")
        hit = {"id": 7, "score": 0.12, "payload": ls.payload(7)}
        qdrant, ollama = FakeServer(qdrant_handler([hit])), FakeServer(ollama_handler())
        self.addCleanup(qdrant.close)
        self.addCleanup(ollama.close)
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "ro")
            with open(key, "w") as f:
                f.write(KEY)
            with mock.patch.multiple(api, QDRANT_URL=qdrant.url, QDRANT_LOCAL_URL="", OLLAMA_URL=ollama.url,
                                     KEY_FILE=key, COLLECTION="synth_rf2", SEARCH_MODE="dense"):
                hits, timing, _ = api._retrieve("What is the governing law?", 5)
        self.assertEqual(hits[0]["file"], "synth/0000007")
        self.assertEqual((hits[0]["span"], hits[0]["text"], hits[0]["document"]), ([0, 0], "", "synthetic"))
        self.assertEqual(set(timing), {"embed", "search"})


class GuardTest(unittest.TestCase):
    def view(self, kube: FakeKube) -> dict:
        return ls.Guard(kube).snapshot()

    def test_quantity(self):
        self.assertEqual(ls.parse_quantity("2Gi"), 2 * GI)
        self.assertEqual(ls.parse_quantity("4013236Ki"), 4013236 * 1024)
        self.assertEqual(ls.parse_quantity("1500M"), 1_500_000_000)
        self.assertEqual(ls.parse_quantity("1.5Gi"), int(1.5 * GI))
        self.assertEqual(ls.parse_quantity("123"), 123)
        self.assertEqual(ls.parse_quantity("1e3"), 1000)
        with self.assertRaises(ValueError):
            ls.parse_quantity("lots")

    def test_view_picks_qdrant_not_gate(self):
        view = self.view(FakeKube(rss=lambda: GI))
        self.assertEqual(sorted(view), list(NODES))
        v = view["machine-2"]
        self.assertEqual((v["qdrant_pod"], v["qdrant_rss"], v["qdrant_limit"]), ("qdrant-1", GI, 2 * GI))
        self.assertEqual((v["fs_capacity"], v["mem_capacity"]), (20 * 10 ** 9, 4013236 * 1024))
        self.assertTrue(v["ready"] and v["qdrant_ready"])

    def test_thresholds(self):
        t = ls.Thresholds()
        cases = [
            (dict(), None, None),
            (dict(rss=lambda: 0.8 * 2 * GI), ls.SOFT, "80 % предела пода"),
            (dict(rss=lambda: 0.95 * 2 * GI), ls.HARD, "95 % предела пода"),
            (dict(avail=200 * MI), ls.SOFT, "свободно памяти 200 МиБ"),
            (dict(avail=100 * MI), ls.HARD, "свободно памяти 100 МиБ"),
            (dict(fs_avail=4 * 10 ** 9), ls.SOFT, "свободно диска 20 %"),
            (dict(fs_avail=2 * 10 ** 9), ls.HARD, "свободно диска 10 %"),
            (dict(ready=False), ls.HARD, "узел не Ready"),
            (dict(pressure=lambda: {"machine-3": "DiskPressure"}), ls.HARD, "machine-3: DiskPressure"),
        ]
        for attrs, level, text in cases:
            kube = FakeKube()
            for k, v in attrs.items():
                setattr(kube, k, v)
            got, reasons = ls.verdict(self.view(kube), None, t, {n: 0 for n in NODES})
            self.assertEqual(got, level, attrs)
            if text:
                self.assertTrue(any(text in r for r in reasons), (attrs, reasons))
        kube = FakeKube()
        kube.restarts = 1
        level, reasons = ls.verdict(self.view(kube), None, t, {n: 0 for n in NODES})
        self.assertEqual(level, ls.HARD)
        self.assertIn("machine-1: пир перезапускался (OOMKilled)", reasons)
        level, reasons = ls.verdict(self.view(FakeKube()), {"status": "red", "optimizer_status": {"error": "disk"}}, t)
        self.assertEqual(level, ls.HARD)
        self.assertEqual(reasons, ["коллекция в статусе red", "ошибка оптимизатора: disk"])

    def test_blind_guard_is_soft(self):
        view = self.view(FakeKube())
        view["machine-1"]["qdrant_rss"] = None
        self.assertEqual(ls.verdict(view, None, ls.Thresholds())[0], ls.SOFT)
        kube = FakeKube()
        kube.fail = 5
        g = ls.Guard(kube)
        self.assertEqual(g.check()[0], None)  # один сбой ещё не повод
        self.assertEqual(g.check()[0], None)
        level, reasons, _ = g.check()
        self.assertEqual(level, ls.SOFT)
        self.assertIn("сторож не видит машины 3 раза подряд", reasons[0])

    def test_peaks(self):
        kube = FakeKube()
        g = ls.Guard(kube)
        for rss, avail in ((0.5, 1.2), (0.9, 0.8), (0.7, 1.0)):
            kube.rss, kube.avail = (lambda r=rss: r * GI), avail * GI
            g.check()
        self.assertEqual(g.peaks["machine-2"]["qdrant_rss"], int(0.9 * GI))  # пик, а не последнее значение
        self.assertEqual(g.peaks["machine-2"]["mem_available"], int(0.8 * GI))
        self.assertEqual(g.last["machine-2"]["qdrant_rss"], int(0.7 * GI))


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.fq = FakeQdrant()
        self.addCleanup(self.fq.server.close)
        self.clock = Clock()
        self.kube = FakeKube()
        self.out = os.path.join(self.dir, "stage.json")

    def args(self, *extra: str):
        return ls.parse_args(["load", "--url", self.fq.server.url, "--key-file", "unused", "--collection", "synth_t",
                              "--target", "1000", "--dim", "8", "--batch", "100", "--check-every", "300",
                              "--shards", "2", "--replicas", "1", "--on-disk", "--out", self.out, *extra])

    def run_load(self, *extra: str, peers=None) -> tuple[int, dict]:
        q = ls.Qdrant(self.fq.server.url, KEY, timeout=5, attempts=2, sleep=self.clock.sleep)
        guard = ls.Guard(self.kube, ls.Thresholds(), {n: 0 for n in NODES})
        with mock.patch("builtins.print"):
            code = ls.run_load(self.args(*extra), q, guard, clock=self.clock, sleep=self.clock.sleep, peers=peers)
        with open(self.out) as f:
            return code, json.load(f)

    def test_fresh_collection_to_target(self):
        self.fq.status = lambda c: "yellow" if c["since"] <= 3 else "green"  # индекс достраивается два опроса
        code, rec = self.run_load()
        self.assertEqual(code, 0)
        self.assertEqual((rec["outcome"], rec["points"], rec["points_before"], rec["uploaded"]), ("ok", 1000, 0, 1000))
        self.assertEqual(self.fq.upserts[0], list(range(100)))
        self.assertEqual(sum(len(u) for u in self.fq.upserts), 1000)
        cfg = self.fq.collections["synth_t"]["config"]
        self.assertEqual(cfg, ls.collection_config(8, 2, 1, "int8", True))
        point = self.fq.collections["synth_t"]["points"][999]
        self.assertEqual(point["vector"]["dense"], ls.vector(999, 1, 8))
        self.assertEqual(set(point["payload"]), {"title", "section", "file", "start", "end", "text"})
        self.assertIn(("POST", "/collections/synth_t/points"), self.fq.calls)  # точка сверена после первой порции
        self.assertGreaterEqual(rec["guard_samples"], 5)  # до загрузки, на 300, 600, 900, 1000, после индекса
        self.assertEqual(rec["status"], "green")
        self.assertEqual(rec["index_s"], 10.0)  # два опроса yellow по 5 с на подставных часах
        self.assertEqual(sorted(rec["after"]), list(NODES))

    def test_resume_resends_last_batch(self):
        self.fq.add("synth_t", ls.collection_config(8, 2, 1, "int8", True), n=950)
        code, rec = self.run_load()
        self.assertEqual(code, 0)
        self.assertEqual(self.fq.upserts[0], list(range(850, 950)))  # последняя порция могла лечь не во все шарды
        self.assertEqual(self.fq.upserts[-1], list(range(950, 1000)))
        self.assertEqual(rec["points"], 1000)

    def test_existing_collection_with_other_schema(self):
        self.fq.add("synth_t", ls.collection_config(8, 3, 1, "int8", True), n=10)
        with self.assertRaises(ls.SetupError) as ctx:
            self.run_load()
        self.assertIn("шардов 3, нужно 2", str(ctx.exception))
        self.assertEqual(self.fq.upserts, [])

    def test_soft_limit_stops_growth(self):
        self.kube.rss = lambda: 0.4 * GI + self.fq.points("synth_t") * MI
        code, rec = self.run_load("--target", "3000")
        self.assertEqual(code, ls.EXIT_SOFT)
        self.assertEqual(rec["outcome"], "soft")
        self.assertEqual(rec["stopped"]["at_points"], 1200)  # на 900 ещё 64 %, на 1200 уже 79 % предела пода
        self.assertTrue(any("предела пода" in r for r in rec["stopped"]["reasons"]))
        self.assertEqual(self.fq.points("synth_t"), 1200)
        self.assertEqual(rec["status"], "green")  # индекс всё равно дождались: на этой ступени меряем нагрузку

    def test_hard_limit_deletes_synthetic(self):
        self.kube.pressure = lambda: {"machine-2": "MemoryPressure"} if self.fq.points("synth_t") >= 600 else {}
        code, rec = self.run_load()
        self.assertEqual(code, ls.EXIT_HARD)
        self.assertEqual((rec["outcome"], rec["deleted"], rec["stopped"]["at_points"]), ("hard", True, 600))
        self.assertIn("machine-2: MemoryPressure", rec["stopped"]["reasons"])
        self.assertNotIn("synth_t", self.fq.collections)

    def test_waits_for_green_on_every_peer(self):
        """Общий green собран по одной копии шарда: ждём, пока индекс достроят все пиры."""
        views = iter([{"qdrant-0": "green", "qdrant-1": "yellow", "qdrant-2": "green"},
                      {"qdrant-0": "green", "qdrant-1": "green", "qdrant-2": "green"}])
        code, rec = self.run_load(peers=lambda: next(views))
        self.assertEqual(code, 0)
        self.assertEqual(rec["index_s"], 5.0)  # один лишний опрос, пока qdrant-1 достраивал
        self.assertEqual(set(rec["peers"].values()), {"green"})

    def test_silent_peer_tunnel_does_not_block(self):
        code, rec = self.run_load(peers=lambda: {"qdrant-0": "green", "qdrant-1": "unreachable"})
        self.assertEqual(code, 0)
        self.assertEqual(rec["index_s"], 20.0)  # пять опросов подряд без ответа, потом только общий статус
        self.assertIn("qdrant-1", rec["peers_note"])

    def test_grey_status_gets_a_push(self):
        self.fq.status = lambda c: "green" if c["patched"] else "grey"
        code, rec = self.run_load()
        self.assertEqual(code, 0)
        self.assertEqual(self.fq.collections["synth_t"]["patched"], 1)
        self.assertGreaterEqual(rec["index_s"], 30)

    def test_index_timeout(self):
        self.fq.status = lambda c: "yellow"
        code, rec = self.run_load("--wait-green", "20")
        self.assertEqual(code, ls.EXIT_INDEX)
        self.assertEqual((rec["outcome"], rec["status"]), ("index_timeout", "yellow"))

    def test_blind_guard_does_not_load(self):
        self.kube.fail = 3
        code, rec = self.run_load()
        self.assertEqual(code, ls.EXIT_SOFT)
        self.assertEqual(self.fq.upserts, [])
        self.assertIn("сторож не видит машины", rec["stopped"]["reasons"][0])


class KubectlPlumbingTest(unittest.TestCase):
    """Настоящие вызовы subprocess, но kubectl это подставной скрипт в PATH."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fq = FakeQdrant()
        self.addCleanup(self.fq.server.close)
        self.fq.add("synth_t", ls.collection_config(8, 2, 1, "int8", True))
        port = self.fq.server.url.rsplit(":", 1)[1]
        script = os.path.join(tmp.name, "kubectl")
        with open(script, "w") as f:
            f.write("#!/bin/sh\n"
                    'case "$*" in\n'
                    "  *port-forward*pod/qdrant-dead*) exit 1 ;;\n"
                    f'  *port-forward*) echo "Forwarding from 127.0.0.1:{port} -> 6333"; exec sleep 30 ;;\n'
                    "  *nodes*) echo '{\"items\": []}' ;;\n"
                    '  *) echo "error: boom" >&2; exit 1 ;;\n'
                    "esac\n")
        os.chmod(script, 0o755)
        p = mock.patch.dict(os.environ, {"PATH": tmp.name + os.pathsep + os.environ.get("PATH", "")})
        p.start()
        self.addCleanup(p.stop)

    def test_peer_statuses_through_tunnel(self):
        st = ls.peer_statuses(["qdrant-0", "qdrant-dead"], KEY, "synth_t")
        self.assertEqual(st, {"qdrant-0": "green", "qdrant-dead": "unreachable"})

    def test_kubectl_json_and_errors(self):
        self.assertEqual(ls.kubectl(["get", "nodes", "-o", "json"]), {"items": []})
        with self.assertRaises(ls.GuardError) as ctx:
            ls.kubectl(["get", "pods"])
        self.assertIn("boom", str(ctx.exception))

    def test_main_load_end_to_end(self):
        """Командная строка целиком: сторож на подставном kubelet, green у пиров через туннели."""
        with tempfile.TemporaryDirectory() as d:
            key, base, out = (os.path.join(d, n) for n in ("key", "baseline.json", "stage.json"))
            with open(key, "w") as f:
                f.write(KEY)
            ls.write_json(base, {"nodes": {}})  # пустой снимок не база: перезапуски считаем от первого снимка
            with mock.patch.object(ls, "kubectl", FakeKube()), mock.patch("builtins.print"):
                code = ls.main(["load", "--url", self.fq.server.url, "--key-file", key, "--collection", "synth_e2e",
                                "--target", "300", "--dim", "8", "--batch", "100", "--baseline", base, "--out", out])
            with open(out) as f:
                rec = json.load(f)
        self.assertEqual(code, 0)
        self.assertEqual((rec["outcome"], rec["points"]), ("ok", 300))
        self.assertEqual(rec["peers"], {"qdrant-0": "green", "qdrant-1": "green", "qdrant-2": "green"})


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fq = FakeQdrant()
        self.addCleanup(self.fq.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.key = os.path.join(self.dir, "key")
        with open(self.key, "w") as f:
            f.write(KEY + "\n")
        for name in ("contracts_rf2", "contracts_rf3", "outage_rf2", "synth_rf2", "synth_rf3"):
            self.fq.add(name, ls.collection_config(8, 3, 3, None, False), n=3)

    def main(self, *argv: str, kube=None) -> int:
        with mock.patch.object(ls, "kubectl", kube or FakeKube()), mock.patch("builtins.print"):
            return ls.main([argv[0], "--url", self.fq.server.url, "--key-file", self.key, *argv[1:]])

    def test_clean_touches_only_synth(self):
        self.assertEqual(self.main("clean", "--collection", "synth_rf3"), 0)
        self.assertEqual(sorted(self.fq.collections), ["contracts_rf2", "contracts_rf3", "outage_rf2", "synth_rf2"])
        self.assertEqual(self.main("clean"), 0)
        self.assertEqual(sorted(self.fq.collections), ["contracts_rf2", "contracts_rf3", "outage_rf2"])
        with self.assertRaises(SystemExit):  # чужое имя отсекает ещё разбор аргументов
            with mock.patch("sys.stderr"):
                self.main("clean", "--collection", "contracts_rf2")
        self.assertIn("contracts_rf2", self.fq.collections)

    def test_snapshot_strict(self):
        out = os.path.join(self.dir, "baseline.json")
        self.assertEqual(self.main("snapshot", "--strict", "--out", out), 0)
        with open(out) as f:
            snap = json.load(f)
        self.assertEqual(len(snap["collections"]), 5)
        self.assertEqual(sorted(snap["nodes"]), list(NODES))
        full = FakeKube(rss=lambda: 1.7 * GI)
        self.assertEqual(self.main("snapshot", "--strict", kube=full), ls.EXIT_SOFT)
        self.assertEqual(self.main("snapshot", kube=full), 0)  # без --strict только показывает

    def test_blind_snapshot_is_soft(self):
        kube = FakeKube()
        kube.fail = 3
        q = ls.Qdrant(self.fq.server.url, KEY, timeout=5, attempts=1)
        snap = ls.snapshot(q, ls.Guard(kube), sleep=lambda s: None)
        self.assertEqual((snap["level"], snap["nodes"]), (ls.SOFT, {}))  # limit.sh по такому снимку не грузит


class LadderLogicTest(unittest.TestCase):
    def test_p99_limit(self):
        c = limit_qps.Criteria()
        self.assertEqual(limit_qps.p99_limit(None, c), 1000)
        self.assertEqual(limit_qps.p99_limit(40, c), 90)    # вдвое было бы 80, но не меньше чем +50 мс
        self.assertEqual(limit_qps.p99_limit(120, c), 240)
        self.assertEqual(limit_qps.p99_limit(600, c), 1000)

    def step(self, **kw) -> dict:
        s = {"count": 600, "qps": 10.0, "p99": 60.0, "errors": 0, "error_rate": 0.0, "errors_by_kind": {},
             "skipped": 0, "collections": {"synth_rf2": 600}}
        s.update(kw)
        return s

    def test_judge(self):
        c = limit_qps.Criteria()
        ok, why, limit = limit_qps.judge(self.step(), 10, 40, c, "synth_rf2", 64)
        self.assertEqual((ok, why, limit), (True, [], 90))
        cases = [
            (dict(errors=12, error_rate=0.02, errors_by_kind={"timeout": 12}), "ошибок 12 из 600 (timeout 12)"),
            (dict(skipped=3), "пропущено 3: заняты все 64 мест"),
            (dict(qps=9.0), "ответов 9 в секунду из 10"),
            (dict(collections={"contracts_rf2": 600}), "отвечала не та коллекция: contracts_rf2"),
            (dict(p99=95.0), "p99 95 мс выше порога 90 мс"),
            (dict(count=0), "ни одного ответа за окно замера"),
        ]
        for kw, text in cases:
            ok, why, _ = limit_qps.judge(self.step(**kw), 10, 40, c, "synth_rf2", 64)
            self.assertFalse(ok, kw)
            self.assertIn(text, why)

    def test_summarize_window(self):
        events = [(9.9, 500.0, None, "synth_rf2", 10.0, 1.0),       # ответ до окна: разогрев
                  (10.0, 20.0, None, "synth_rf2", 12.0, 2.0),
                  (10.5, 30.0, "timeout", None, None, None),
                  (11.9, 40.0, None, "synth_rf2", 14.0, 3.0),
                  (12.0, 900.0, None, "synth_rf2", 15.0, 4.0)]       # ответ после окна
        lg = limit_qps.load_loadgen(os.path.join(ROOT, "panel", "loadgen.py"))
        s = limit_qps.summarize(events, [10.2, 13.0], 10.0, 12.0, lg.percentile)
        self.assertEqual((s["count"], s["qps"], s["errors"], s["skipped"]), (3, 1.5, 1, 1))
        self.assertEqual((s["p50"], s["p99"], s["max"]), (30.0, 40.0, 40.0))
        self.assertEqual(s["collections"], {"synth_rf2": 2})
        self.assertEqual((s["embed_p99"], s["search_p50"]), (14.0, 2.0))
        self.assertEqual(s["errors_by_kind"], {"timeout": 1})


class _Entry(BaseHTTPRequestHandler):
    """Вход стенда: отвечает как сервис поиска, задержку задаёт тест."""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        delay = self.server.delay
        if delay:
            time.sleep(delay)
        body = json.dumps({"query": q["q"][0], "model": "bge-m3", "collection": "synth_t",
                           "timing_ms": {"embed": 10.0, "search": 1.0 + delay * 1000}, "results": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class LadderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Entry)
        self.srv.daemon_threads = True
        self.srv.delay = 0.0
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.questions = os.path.join(self.dir, "q.txt")
        with open(self.questions, "w") as f:
            f.write("# вопросы\nWhat is the governing law?\nCan the agreement be terminated?\n")
        self.lg = limit_qps.load_loadgen(os.path.join(ROOT, "panel", "loadgen.py"))

    def test_ladder_stops_where_p99_grows(self):
        out = os.path.join(self.dir, "qps.json")
        args = limit_qps.parse_args([
            "--url", f"http://127.0.0.1:{self.srv.server_address[1]}/search", "--questions", self.questions,
            "--collection", "synth_t", "--label", "1000", "--rates", "10,20,40", "--warm", "0.2", "--measure", "1",
            "--pause", "0", "--min-rate-share", "0.5", "--out", out])
        lines = []

        def log(line):  # после первой ступени «вход» тормозит: так выглядит упор
            lines.append(line)
            self.srv.delay = 0.3
        doc = limit_qps.run_ladder(self.lg, args, self.lg.load_questions(self.questions), log=log)
        self.assertEqual([s["holds"] for s in doc["steps"]], [True, False])  # третий темп не пробуем
        self.assertEqual(doc["held_rate"], 10)
        self.assertEqual(doc["stopped"]["rate"], 20)
        self.assertTrue(any(w.startswith("p99 ") for w in doc["stopped"]["why"]), doc["stopped"])
        first, second = doc["steps"]
        self.assertEqual(first["collections"], {"synth_t": first["count"]})
        self.assertGreater(second["search_p99"], 250)  # шаг Qdrant из ответа сервиса виден отдельно
        self.assertEqual(first["embed_p99"], 10.0)
        with open(out) as f:
            self.assertEqual(json.load(f)["steps"], doc["steps"])
        self.assertIn("держит", lines[0])


def qps_doc(rates_holds: list[tuple[float, bool, float]], label: int) -> dict:
    """Лестница в формате limit_qps.py: (темп, держит ли, p99)."""
    steps, held, stopped = [], None, None
    for r, holds, p99 in rates_holds:
        steps.append({"rate": r, "count": int(r * 60), "qps": r, "p50": p99 / 2, "p95": p99 * 0.8, "p99": p99,
                      "max": p99 * 1.5, "errors": 0 if holds else 9, "error_rate": 0.0, "errors_by_kind": {} if holds
                      else {"timeout": 9}, "skipped": 0, "collections": {"synth_rf2": int(r * 60)},
                      "embed_p50": 20.0, "embed_p99": 30.0, "search_p50": 3.0, "search_p99": p99 / 4,
                      "p99_limit": 90.0, "holds": holds, "why": [] if holds else [f"p99 {p99:g} мс выше порога 90 мс"]})
        if holds and stopped is None:
            held = r
        elif not holds and stopped is None:
            stopped = {"rate": r, "why": steps[-1]["why"]}
    return {"schema": limit_qps.SCHEMA, "label": label, "collection": "synth_rf2",
            "config": {"rates": [s[0] for s in rates_holds], "warm_s": 10, "measure_s": 60, "questions": 40},
            "criteria": {}, "steps": steps, "held_rate": held, "base_p99": steps[0]["p99"], "stopped": stopped}


class ReportTest(unittest.TestCase):
    def test_template_matches_committed_file(self):
        text = limit_report.render([])
        self.assertIn("Замера ещё не было", text)
        self.assertIn("одном ноутбуке", text)  # подпись про ноутбук на месте
        with open(os.path.join(ROOT, "eval", "results", "limit.md"), encoding="utf-8") as f:
            committed = f.read()
        self.assertTrue(committed.startswith(limit_report.TITLE))
        self.assertEqual(limit_report.headings(committed), limit_report.headings(text))

    def test_report_from_loader_output(self):
        """Итог ступени пишет настоящий загрузчик на подставных Qdrant и kubelet, лестницы в формате limit_qps."""
        with tempfile.TemporaryDirectory() as run:
            fq = FakeQdrant()
            self.addCleanup(fq.server.close)
            kube = FakeKube(rss=lambda: 0.3 * GI + fq.points("synth_rf2") * 1.25 * MI)
            clock = Clock()
            q = ls.Qdrant(fq.server.url, KEY, timeout=5, attempts=2, sleep=clock.sleep)
            ls.write_json(os.path.join(run, "baseline.json"), ls.snapshot(q, ls.Guard(kube)))
            for target in (400, 1000):
                args = ls.parse_args(["load", "--url", fq.server.url, "--key-file", "unused", "--collection",
                                      "synth_rf2", "--target", str(target), "--dim", "8", "--batch", "100",
                                      "--check-every", "200", "--on-disk", "--out", os.path.join(run, f"stage-{target}.json")])
                with mock.patch("builtins.print"):
                    ls.run_load(args, q, ls.Guard(kube, restarts0={n: 0 for n in NODES}), clock=clock, sleep=clock.sleep)
            ls.write_json(os.path.join(run, "qps-400.json"), qps_doc([(5, True, 40.0), (10, True, 45.0), (20, False, 120.0)], 400))
            ls.write_json(os.path.join(run, "qps-1000.json"), qps_doc([(5, True, 42.0), (10, False, 150.0)], 1000))
            with open(os.path.join(run, "stages.txt"), "w") as f:
                f.write("400 1000 4000\n")
            text = limit_report.render([limit_report.load_run(run)])
        self.assertIn("1 000, дальше не грузили, мягкий порог", text)   # 0,3 ГиБ + 1000 × 1,25 МиБ = 76 % предела
        self.assertIn("| 400 | ", text)
        self.assertIn("**Запросов в секунду без роста p99:** 5 на 1 000 векторах, p99 42 мс", text)
        self.assertIn("на 10 в секунду p99 у пользователя 150 мс (на 5 было 42)", text)
        self.assertIn("### 1 000 векторов", text)
        self.assertIn("| 20 | 20 | 60 / 96 / 120 | 90 |", text)
        self.assertIn("**Оценка, не замер.**", text)
        self.assertIn("1 310 720 байт на вектор на машину (machine-1); мягкий порог (75 % предела пода) "
                      "уже достигнут", text)   # 1,25 МиБ на точку в подставном kubelet
        # рабочий набор на 0,25 ГиБ больше: (2 ГиБ − 1 901 278 003 байт) / 1 310 720 байт ≈ 188 векторов
        self.assertIn("до предела пода, после которого кэш исходных векторов вытесняется, оценка: ещё около 188 ", text)
        self.assertIn("и ещё 1", text)  # три одинаковые причины в ячейке сокращены до двух
        self.assertIn("коллекция `synth_rf2`, 6 шардов по 2 копии", text)
        self.assertEqual(limit_report.headings(text), limit_report.headings(limit_report.render([])))
        self.assertNotIn(limit_report.EMPTY + " |", text.split("## Как получено")[0].split("## Оценка")[0])


if __name__ == "__main__":
    unittest.main()
