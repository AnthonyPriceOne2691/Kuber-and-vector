"""Шлюз к Qdrant (deploy/qdrant/gate.py): пропускает только чтение состояния, ключ добавляет сам."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.fakes import KEY, FakeServer, closed_port_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("qdrant_gate", os.path.join(ROOT, "deploy", "qdrant", "gate.py"))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def qdrant(method, path, headers, body):
    if headers.get("api-key") != KEY:
        return 401, {"status": {"error": "Must provide an API key"}}
    return 200, {"result": {"path": path}}


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.q = FakeServer(qdrant)
        self.addCleanup(self.q.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key = os.path.join(tmp.name, "key")
        with open(key, "w") as f:
            f.write(KEY + "\n")
        p = mock.patch.multiple(gate, QDRANT_URL=self.q.url, KEY_FILE=key)
        p.start()
        self.addCleanup(p.stop)
        self.srv = gate.Server(("127.0.0.1", 0), gate.Handler)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        log = io.StringIO()
        r = contextlib.redirect_stderr(log)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def get(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        req = urllib.request.Request(self.url + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_state_paths_pass_with_key(self):
        for path in ("/metrics", "/cluster", "/aliases", "/collections/contracts_rf2",
                     "/collections/contracts_rf2/cluster"):
            code, body = self.get(path)
            self.assertEqual((code, json.loads(body)["result"]["path"]), (200, path))
        self.assertTrue(all(h.get("api-key") == KEY for _, _, h, _ in self.q.requests))

    def test_query_string_dropped(self):
        self.get("/collections/contracts_rf2?with_payload=true&limit=100000")
        self.assertEqual(self.q.paths(), ["/collections/contracts_rf2"])

    def test_data_and_admin_paths_denied(self):
        for path in ("/collections", "/collections/contracts_rf2/points", "/collections/contracts_rf2/points/scroll",
                     "/collections/contracts_rf2/points/query", "/collections/contracts_rf2/snapshots",
                     "/snapshots", "/telemetry", "/locks", "/cluster/peer/1", "/collections/../cluster",
                     "/collections/a%2Fb/cluster", "/metrics/", "/"):
            self.assertEqual(self.get(path)[0], 404, path)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.get("/collections/contracts_rf2", method=method)[0], 405, method)
        self.assertEqual(self.q.requests, [])  # до Qdrant не дошло ничего

    def test_qdrant_down_and_health(self):
        self.assertEqual(self.get("/healthz"), (200, b'{"ok":true}'))
        with mock.patch.object(gate, "QDRANT_URL", closed_port_url()):
            self.assertEqual(self.get("/cluster")[0], 502)

    def test_key_never_echoed(self):
        for path in ("/cluster", "/points", "/healthz"):
            self.assertNotIn(KEY.encode(), self.get(path)[1])


if __name__ == "__main__":
    unittest.main()
