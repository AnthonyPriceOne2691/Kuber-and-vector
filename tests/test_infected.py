"""Сцена «заражённый под»: попытки изнутри (deploy/scenes/attack_pod.py) против подставного Qdrant
и итог сцены (infected_summary.py). Сеть пода здесь не настоящая: проверяем код, а не правила сети."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.fakes import KEY, FakeServer, closed_port_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "deploy", "scenes", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attack = load("attack_pod")
infected = load("infected_summary")


def read_only_qdrant(method, path, headers, body):
    if headers.get("api-key") != KEY:
        return 401, {"status": {"error": "Must provide an API key"}}
    if method != "GET":
        return 403, {"status": {"error": "Write access denied"}}
    return 200, {"result": {"status": "green"}}


class AttackTest(unittest.TestCase):
    def test_read_only_key_cannot_write(self):
        q = FakeServer(read_only_qdrant)
        self.addCleanup(q.close)
        real_http = attack.http

        def http(method, url, key="", data=None):
            if ":11434/" in url:  # фильтр Ollama на Маке: в тесте его нет
                return 403, "forbidden"
            return real_http(method, url, key, data)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"KEY": KEY, "QDRANT_URL": q.url}), \
                mock.patch.object(attack, "tcp", return_value=("no connection (TimeoutError, 3.0 s)", True)), \
                mock.patch.object(attack, "dns", return_value=("no answer within 4 s", True)), \
                mock.patch.object(attack, "http", side_effect=http), \
                contextlib.redirect_stdout(out):
            self.assertEqual(attack.main(["-", "192.168.105.5"]), 0)
        rows = [line.split("\t") for line in out.getvalue().splitlines()]
        verdict = {r[1]: (r[2], r[3]) for r in rows}
        self.assertEqual(verdict["wipe the database with the pod key (DELETE contracts_rf2)"], ("HTTP 403", "blocked"))
        self.assertEqual(verdict["write to the database with the pod key (PUT points)"], ("HTTP 403", "blocked"))
        self.assertEqual(verdict["read the database with the pod key"], ("HTTP 200", "passed"))
        self.assertEqual(verdict["host Mac: Ollama past the filter (/api/tags)"], ("HTTP 403", "blocked"))
        self.assertEqual(verdict["neighbor 192.168.105.5: etcd 2379"][1], "blocked")
        self.assertEqual((rows[0][1], rows[-1][1]), ("start", "end"))
        self.assertEqual([r[0] for r in q.requests], ["DELETE", "PUT", "GET"])

    def test_start_is_when_the_shell_began(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"T0": "1795109323.012345678", "KEY": "", "QDRANT_URL": ""}), \
                mock.patch.object(attack, "tcp", return_value=("no connection", True)), \
                mock.patch.object(attack, "dns", return_value=("not resolved", True)), \
                mock.patch.object(attack, "http", return_value=(403, "forbidden")), \
                contextlib.redirect_stdout(out):
            attack.main(["-"])
        self.assertEqual(out.getvalue().split("\t", 1)[0], "1795109323.012")
        with mock.patch.dict(os.environ, {"T0": "не число"}):
            self.assertGreater(attack.started(), 1.7e9)

    def test_tcp_refused_is_blocked(self):
        host, port = closed_port_url().rsplit("//", 1)[1].split(":")
        text, blocked = attack.tcp(host, int(port))
        self.assertTrue(blocked)
        self.assertIn("ConnectionRefusedError", text)


def attack_tsv(start: float, all_blocked: bool = True) -> str:
    rows = [f"{start:.3f}\tstart\tsearch-abc\t-",
            f"{start + 3:.3f}\texfiltrate to the internet (1.1.1.1:443)\tno connection\t{'blocked' if all_blocked else 'passed'}",
            f"{start + 3.1:.3f}\twipe the database with the pod key (DELETE contracts_rf2)\tHTTP 403\tblocked",
            f"{start + 3.2:.3f}\tread the database with the pod key\tHTTP 200\tpassed",
            f"{start + 3.3:.3f}\tend\t-\t-"]
    return "\n".join(rows) + "\n"


def falco_line(ts: str, rule: str, pod: str | None = "search-abc", proc: str = "") -> str:
    fields = {"k8s.pod.name": pod} if pod else {}
    if proc:
        fields["proc.name"] = proc
    return json.dumps({"time": ts, "rule": rule, "priority": "Warning", "output_fields": fields})


class SummaryTest(unittest.TestCase):
    def run_dir(self, attack_text: str, falco_text: str | None) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "attack.tsv"), "w", encoding="utf-8") as f:
            f.write(attack_text)
        if falco_text is not None:
            with open(os.path.join(tmp.name, "falco.jsonl"), "w", encoding="utf-8") as f:
                f.write(falco_text)
        return tmp.name

    def test_first_alert_delay_and_counts(self):
        start = infected.falco_time("2026-11-19T10:00:00.000000000Z")
        falco = "\n".join([
            "Thu Nov 19 10:00:00 2026: Falco version: 0.45.2",          # служебная строка, не JSON
            falco_line("2026-11-19T09:59:00.000000000Z", "Stand shell in search pod"),  # до атаки
            falco_line("2026-11-19T10:00:00.050123456Z", "Stand shell in search pod"),
            falco_line("2026-11-19T10:00:00.051000000Z", "Stand Qdrant key read from shell"),
            falco_line("2026-11-19T10:00:01.000000000Z", "Stand forbidden connection from search pod"),
            falco_line("2026-11-19T10:00:01.100000000Z", "Stand forbidden connection from search pod"),
            falco_line("2026-11-19T10:00:01.200000000Z", "Stand shell in search pod", pod="other"),
        ])
        text, ok = infected.report(self.run_dir(attack_tsv(start), falco))
        self.assertTrue(ok, text)
        self.assertIn("Blocked 2 of 2.", text)
        self.assertIn("Falco alerts 4, the first 50 ms after the attacker got a shell.", text)
        self.assertIn("| Stand forbidden connection from search pod | 2 | 1000 ms |", text)
        self.assertIn("Reading the database went through", text)

    def test_container_log_stamp_beats_falco_clock(self):
        # 19.11: после жёстких перезапусков машины поле time у Falco уехало на +0,48 с;
        # метка containerd идёт по часам пода, по ней и считаем. Без тревоги на date началом служит T0.
        start = infected.log_time("2026-11-19T20:28:43.000000000+03:00")
        falco = "\n".join([
            "2026-11-19T20:28:43.010000000+03:00 " + falco_line("2026-11-19T17:28:43.490000000Z",
                                                                "Falco internal: syscall event drop", pod=None),
            "2026-11-19T20:28:43.020000000+03:00 " + falco_line("2026-11-19T17:28:43.500000000Z",
                                                                "Stand shell in search pod"),
            "2026-11-19T17:28:43.083000000Z " + falco_line("2026-11-19T17:28:43.560000000Z",
                                                           "Stand forbidden connection from search pod"),
            "2026-11-19T20:28:43.090000000+03:00 not json",
        ])
        run = self.run_dir(attack_tsv(start), None)
        with open(os.path.join(run, "falco.log"), "w", encoding="utf-8") as f:
            f.write(falco)
        text, ok = infected.report(run)
        self.assertTrue(ok, text)
        self.assertIn("Falco alerts 2, the first 20 ms after the attacker got a shell.", text)
        self.assertIn("| Stand forbidden connection from search pod | 1 | 83 ms |", text)
        self.assertNotIn("Falco internal", text)

    def test_shell_start_from_date_anchor(self):
        # Оболочка запустилась за 2 мс до того, как date записал T0; часы Falco впереди на 480 мс.
        # Сдвиг узнаём по date (время Falco 479,7 мс после T0), запуск оболочки по часам пода T0 - 1,7 мс,
        # тревога на неё вышла в T0 - 1 мс, то есть через 0,7 мс после запуска.
        t0 = infected.log_time("2026-11-19T20:28:43.000000000+03:00")
        falco = "\n".join([
            "2026-11-19T20:28:42.999000000+03:00 " + falco_line("2026-11-19T17:28:43.478000000Z",
                                                                "Stand shell in search pod", proc="sh"),
            "2026-11-19T20:28:43.000800000+03:00 " + falco_line("2026-11-19T17:28:43.479700000Z",
                                                                "Stand unexpected process in search pod", proc="date"),
            "2026-11-19T20:28:43.083000000+03:00 " + falco_line("2026-11-19T17:28:43.505000000Z",
                                                                "Stand forbidden connection from search pod",
                                                                proc="python3"),
        ])
        run = self.run_dir(attack_tsv(t0), None)
        with open(os.path.join(run, "falco.log"), "w", encoding="utf-8") as f:
            f.write(falco)
        alerts = infected.parse_falco(falco, "search-abc")
        self.assertAlmostEqual(infected.shell_started(alerts, t0) - t0, -0.0017, places=4)
        text, ok = infected.report(run)
        self.assertTrue(ok, text)
        self.assertIn("Falco alerts 3, the first 1 ms after the attacker got a shell.", text)
        self.assertIn("| Stand shell in search pod | 1 | 1 ms |", text)
        self.assertIn("| Stand forbidden connection from search pod | 1 | 85 ms |", text)

    def test_log_time_formats(self):
        self.assertAlmostEqual(infected.log_time("2026-11-19T17:28:43.451785625Z"),
                               infected.log_time("2026-11-19T20:28:43.451785+03:00"), places=6)
        with self.assertRaises(ValueError):
            infected.log_time("Thu Nov 19 10:00:00 2026:")

    def test_leak_or_silence_fails(self):
        start = infected.falco_time("2026-11-19T10:00:00Z")
        alert = falco_line("2026-11-19T10:00:00.5Z", "Stand shell in search pod")
        self.assertFalse(infected.report(self.run_dir(attack_tsv(start, all_blocked=False), alert))[1])
        text, ok = infected.report(self.run_dir(attack_tsv(start), None))
        self.assertFalse(ok)
        self.assertIn("No Falco alerts.", text)


if __name__ == "__main__":
    unittest.main()
