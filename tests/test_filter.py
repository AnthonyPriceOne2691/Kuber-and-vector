"""Фильтр перед Ollama на Маке (deploy/host/ollama_filter.py): что пропускает и что режет.

Фильтр поднимается на 127.0.0.1 со случайным портом, «Ollama» за ним подставная.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.fakes import FakeServer, closed_port_url, ollama_handler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ollama_filter", os.path.join(ROOT, "deploy", "host", "ollama_filter.py"))
flt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flt)


class FilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeServer(ollama_handler())
        self.addCleanup(self.upstream.close)
        self.patch(UPSTREAM=self.upstream.url)
        self.srv = flt.Server(("127.0.0.1", 0), flt.Handler)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.log = io.StringIO()
        redirect = contextlib.redirect_stderr(self.log)  # журнал фильтра пишется в stderr
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def patch(self, **values) -> None:
        p = mock.patch.multiple(flt, **values)
        p.start()
        self.addCleanup(p.stop)

    def send(self, path: str, body=None, raw: bytes | None = None, method: str = "POST") -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.getvalue().splitlines() if line.startswith("{")]


class EmbedTest(FilterTest):
    def test_embed_passes_only_allowed_fields(self):
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": "hello",
                                              "keep_alive": -1, "options": {"num_ctx": 131072}})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["embeddings"]), 1)
        # наверх ушли только модель, текст и truncate: keep_alive и options фильтр выбросил
        self.assertEqual(self.upstream.json_bodies(), [{"model": "bge-m3", "input": ["hello"], "truncate": True}])

    def test_embed_batch(self):
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": ["a", "b", "c"]})
        self.assertEqual((code, len(body["embeddings"])), (200, 3))

    def test_other_model_denied(self):
        code, _ = self.send("/api/embed", {"model": "nomic-embed-text", "input": "x"})
        self.assertEqual(code, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_input_limits(self):
        cases = [[], [""], ["x" * (flt.MAX_CHARS + 1)], ["x"] * (flt.MAX_INPUTS + 1), [1], "", None]
        for inp in cases:
            code, _ = self.send("/api/embed", {"model": "bge-m3", "input": inp})
            self.assertEqual(code, 400, repr(inp)[:40])
        self.assertEqual(self.upstream.requests, [])

    def test_body_limits(self):
        self.assertEqual(self.send("/api/embed", raw=b"{not json")[0], 400)
        self.assertEqual(self.send("/api/embed", raw=b"")[0], 413)
        self.assertEqual(self.send("/api/embed", ["bge-m3"])[0], 403)  # не объект
        self.patch(MAX_BODY=100)
        self.assertEqual(self.send("/api/embed", {"model": "bge-m3", "input": "x" * 200})[0], 413)
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_down(self):
        self.patch(UPSTREAM=closed_port_url())
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": "hello"})
        self.assertEqual(code, 502)
        self.assertEqual(body["error"], "векторизатор недоступен")


class DenyTest(FilterTest):
    def test_dangerous_calls_denied_and_logged(self):
        for path in ("/api/pull", "/api/push", "/api/create", "/api/generate", "/api/copy", "/api/delete"):
            self.assertEqual(self.send(path, {"model": "bge-m3"})[0], 403, path)
        self.assertEqual(self.send("/api/tags", method="GET")[0], 403)
        self.assertEqual(self.send("/api/ps", method="GET")[0], 403)
        self.assertEqual(self.send("/api/delete", {"model": "bge-m3"}, method="DELETE")[0], 403)
        self.assertEqual(self.upstream.requests, [])  # до Ollama не дошло ничего
        denied = [e for e in self.events() if e["event"] == "denied"]
        self.assertEqual(len(denied), 9)
        self.assertIn("/api/pull", {e["path"] for e in denied})

    def test_refuses_all_interfaces(self):
        for addr in ("0.0.0.0", "::"):
            with mock.patch.object(sys, "argv", ["ollama_filter.py", "--listen", addr]):
                with self.assertRaises(SystemExit):
                    flt.main()


if __name__ == "__main__":
    unittest.main()
