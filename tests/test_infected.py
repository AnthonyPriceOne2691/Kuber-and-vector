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
                mock.patch.object(attack, "tcp", return_value=("нет соединения (TimeoutError, 3.0 с)", True)), \
                mock.patch.object(attack, "dns", return_value=("нет ответа за 4 с", True)), \
                mock.patch.object(attack, "http", side_effect=http), \
                contextlib.redirect_stdout(out):
            self.assertEqual(attack.main(["-", "192.168.105.5"]), 0)
        rows = [line.split("\t") for line in out.getvalue().splitlines()]
        verdict = {r[1]: (r[2], r[3]) for r in rows}
        self.assertEqual(verdict["стереть базу ключом пода (DELETE contracts_rf2)"], ("HTTP 403", "отбито"))
        self.assertEqual(verdict["записать в базу ключом пода (PUT points)"], ("HTTP 403", "отбито"))
        self.assertEqual(verdict["прочитать базу ключом пода"], ("HTTP 200", "прошло"))
        self.assertEqual(verdict["Мак: Ollama мимо фильтра (/api/tags)"], ("HTTP 403", "отбито"))
        self.assertEqual(verdict["соседняя машина 192.168.105.5: etcd 2379"][1], "отбито")
        self.assertEqual((rows[0][1], rows[-1][1]), ("начало", "конец"))
        self.assertEqual([r[0] for r in q.requests], ["DELETE", "PUT", "GET"])

    def test_tcp_refused_is_blocked(self):
        host, port = closed_port_url().rsplit("//", 1)[1].split(":")
        text, blocked = attack.tcp(host, int(port))
        self.assertTrue(blocked)
        self.assertIn("ConnectionRefusedError", text)


def attack_tsv(start: float, all_blocked: bool = True) -> str:
    rows = [f"{start:.3f}\tначало\tsearch-abc\t-",
            f"{start + 3:.3f}\tвынести данные в интернет (1.1.1.1:443)\tнет соединения\t{'отбито' if all_blocked else 'прошло'}",
            f"{start + 3.1:.3f}\tстереть базу ключом пода (DELETE contracts_rf2)\tHTTP 403\tотбито",
            f"{start + 3.2:.3f}\tпрочитать базу ключом пода\tHTTP 200\tпрошло",
            f"{start + 3.3:.3f}\tконец\t-\t-"]
    return "\n".join(rows) + "\n"


def falco_line(ts: str, rule: str, pod: str = "search-abc") -> str:
    return json.dumps({"time": ts, "rule": rule, "priority": "Warning", "output_fields": {"k8s.pod.name": pod}})


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
        self.assertIn("Отбито 2 из 2.", text)
        self.assertIn("Тревог Falco 4, первая через 50 мс после начала атаки.", text)
        self.assertIn("| Stand forbidden connection from search pod | 2 |", text)
        self.assertIn("Прошло чтение базы", text)

    def test_leak_or_silence_fails(self):
        start = infected.falco_time("2026-11-19T10:00:00Z")
        alert = falco_line("2026-11-19T10:00:00.5Z", "Stand shell in search pod")
        self.assertFalse(infected.report(self.run_dir(attack_tsv(start, all_blocked=False), alert))[1])
        text, ok = infected.report(self.run_dir(attack_tsv(start), None))
        self.assertFalse(ok)
        self.assertIn("Тревог Falco нет.", text)


if __name__ == "__main__":
    unittest.main()
