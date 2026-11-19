"""Сервис поиска (app/api.py) против подставных Qdrant и Ollama: повторы, ответы, отказы, заголовки.

    python3 -I -B -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from app import api
from tests.fakes import KEY, FakeServer, closed_port_url, ollama_handler, point, qdrant_handler

try:
    from fastapi.testclient import TestClient
except ImportError:  # без httpx клиента нет; тесты ручек пропускаются
    TestClient = None


class StandTest(unittest.TestCase):
    """Подставной стенд: Qdrant и Ollama на 127.0.0.1, ключ в файле, короткие таймауты."""

    points = [point(0), point(1), point(2)]

    def setUp(self) -> None:
        self.qdrant = FakeServer(qdrant_handler(self.points))
        self.ollama = FakeServer(ollama_handler())
        self.addCleanup(self.qdrant.close)
        self.addCleanup(self.ollama.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.key_file = os.path.join(tmp.name, "read-only-api-key")
        with open(self.key_file, "w") as f:
            f.write(KEY + "\n")
        self.patch(QDRANT_URL=self.qdrant.url, OLLAMA_URL=self.ollama.url, KEY_FILE=self.key_file,
                   COLLECTION="contracts_rf2", SEARCH_MODE="dense", NODE="machine-2", POD="search-abc",
                   QDRANT_CONNECT_TIMEOUT=0.3, QDRANT_READ_TIMEOUT=0.3, QDRANT_ATTEMPTS=4)

    def patch(self, **values) -> None:
        p = mock.patch.multiple(api, **values)
        p.start()
        self.addCleanup(p.stop)


class QdrantRetryTest(StandTest):
    def test_first_attempt(self):
        result, attempt = api._qdrant("/collections/contracts_rf2")
        self.assertEqual(result["result"]["status"], "green")
        self.assertEqual(attempt, 1)
        self.assertEqual(self.qdrant.requests[0][2].get("api-key"), KEY)  # ключ из файла, не из кода

    def test_server_error_then_ok(self):
        calls = []
        ok = qdrant_handler(self.points)

        def flaky(*args):
            calls.append(1)
            return (503, {"status": {"error": "starting"}}) if len(calls) == 1 else ok(*args)
        self.qdrant.handler = flaky
        _, attempt = api._qdrant("/collections/contracts_rf2")
        self.assertEqual(attempt, 2)

    def test_silent_peer_then_ok(self):
        """Первый пир молчит (машина выключена): ждём не дольше таймаута чтения и идём дальше."""
        calls = []
        ok = qdrant_handler(self.points)

        def first_silent(*args):
            calls.append(1)
            return None if len(calls) == 1 else ok(*args)
        self.qdrant.handler = first_silent
        t0 = time.perf_counter()
        _, attempt = api._qdrant("/collections/contracts_rf2")
        elapsed = time.perf_counter() - t0
        self.assertEqual(attempt, 2)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 0.9)

    def test_client_error_not_retried(self):
        with open(self.key_file, "w") as f:
            f.write("wrong-key")
        with self.assertRaises(api.QdrantError) as ctx:
            api._qdrant("/collections/contracts_rf2")
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(self.qdrant.requests), 1)  # 4xx повтором не лечится

    def test_garbage_retried_then_fails(self):
        self.qdrant.handler = lambda *a: (200, b"not json")
        with self.assertRaises(api.QdrantError):
            api._qdrant("/collections/contracts_rf2")
        self.assertEqual(len(self.qdrant.requests), 4)

    def test_all_refused_fails_fast(self):
        self.patch(QDRANT_URL=closed_port_url())
        t0 = time.perf_counter()
        with self.assertRaises(api.QdrantError):
            api._qdrant("/collections/contracts_rf2")
        self.assertLess(time.perf_counter() - t0, 1.0)


@unittest.skipIf(TestClient is None, "нет fastapi.testclient (httpx)")
class EndpointsTest(StandTest):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(api.app)

    def test_search_dense(self):
        r = self.client.get("/search", params={"q": "governing law", "k": 3})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "dense")
        self.assertEqual(body["collection"], "contracts_rf2")
        self.assertEqual(body["qdrant_attempts"], 1)
        self.assertEqual(body["served_by"], {"node": "machine-2", "pod": "search-abc"})
        self.assertEqual(set(body["timing_ms"]), {"embed", "search"})
        first = body["results"][0]
        self.assertEqual(first["file"], "cuad/Acme_License.txt")
        self.assertEqual(first["span"], [0, 500])
        self.assertEqual(first["section"], "12. Governing Law.")
        self.assertIsNone(body["results"][1]["section"])  # пустой раздел отдаём как null
        sent = self.qdrant.json_bodies()[-1]
        self.assertEqual((sent["using"], sent["limit"]), ("dense", 3))
        self.assertEqual(sent["query"], [0.5] * 4)
        self.assertEqual(self.ollama.json_bodies()[-1], {"model": "bge-m3", "input": ["governing law"]})

    def test_search_hybrid_body(self):
        self.patch(SEARCH_MODE="hybrid")
        r = self.client.get("/search", params={"q": "governing law", "k": 2})
        self.assertEqual(r.status_code, 200)
        sent = self.qdrant.json_bodies()[-1]
        self.assertEqual([p["using"] for p in sent["prefetch"]], ["dense", "bm25"])
        self.assertEqual(sent["query"], {"fusion": "rrf"})
        self.assertEqual(sent["limit"], 2)
        self.assertTrue(sent["prefetch"][1]["query"]["indices"])

    def test_validation(self):
        for params in ({"q": "ab"}, {"q": "x" * 501}, {"q": "governing law", "k": 0},
                       {"q": "governing law", "k": 21}, {}):
            self.assertEqual(self.client.get("/search", params=params).status_code, 422, params)

    def test_embedder_down(self):
        self.patch(OLLAMA_URL=closed_port_url())
        r = self.client.get("/search", params={"q": "governing law"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "векторизатор недоступен")
        self.assertEqual(self.qdrant.requests, [])

    def test_database_down(self):
        self.patch(QDRANT_URL=closed_port_url())
        r = self.client.get("/search", params={"q": "governing law"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "база недоступна", "served_by": {"node": "machine-2", "pod": "search-abc"}})

    def test_readyz(self):
        self.assertEqual(self.client.get("/readyz").json(), {"ready": True})
        self.qdrant.handler = qdrant_handler(self.points, status="red")
        self.assertEqual(self.client.get("/readyz").status_code, 503)
        self.patch(QDRANT_URL=closed_port_url())
        self.assertEqual(self.client.get("/readyz").status_code, 503)

    def test_headers_and_no_docs(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["cache-control"], "no-store")
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
