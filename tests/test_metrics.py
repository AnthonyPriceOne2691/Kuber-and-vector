"""Метрики сервиса поиска (app/metrics.py и /metrics в app/api.py): формат Prometheus и что считается."""
from __future__ import annotations

import unittest
from unittest import mock

from app import api, metrics
from tests.fakes import closed_port_url, ollama_handler
from tests.test_api import StandTest, TestClient, model_reply


class RegistryTest(unittest.TestCase):
    def test_counter_and_histogram_format(self):
        r = metrics.Registry({"pod": "search-1", "empty": ""})
        r.inc("search_requests_total", {"endpoint": "search", "code": "200"})
        r.inc("search_requests_total", {"endpoint": "search", "code": "200"})
        r.observe("search_request_seconds", {"endpoint": "search"}, 0.03)
        r.observe("search_request_seconds", {"endpoint": "search"}, 7)
        text = r.render()
        self.assertIn("# TYPE search_requests_total counter", text)
        self.assertIn('search_requests_total{code="200",endpoint="search",pod="search-1"} 2', text)
        self.assertNotIn("empty", text)  # пустая постоянная метка не выводится
        self.assertIn('search_request_seconds_bucket{endpoint="search",pod="search-1",le="0.025"} 0', text)
        self.assertIn('search_request_seconds_bucket{endpoint="search",pod="search-1",le="0.05"} 1', text)
        self.assertIn('search_request_seconds_bucket{endpoint="search",pod="search-1",le="10"} 2', text)
        self.assertIn('search_request_seconds_bucket{endpoint="search",pod="search-1",le="+Inf"} 2', text)
        self.assertIn('search_request_seconds_count{endpoint="search",pod="search-1"} 2', text)
        self.assertIn('search_request_seconds_sum{endpoint="search",pod="search-1"} 7.03', text)
        self.assertTrue(text.endswith("\n"))

    def test_escaping_and_unknown(self):
        r = metrics.Registry()
        r.inc("search_answer_total", {"result": 'a"b\\c\nd'})
        self.assertIn('search_answer_total{result="a\\"b\\\\c\\nd"} 1', r.render())
        with self.assertRaises(KeyError):
            r.inc("made_up_total")


@unittest.skipIf(TestClient is None, "нет fastapi.testclient (httpx)")
class EndpointMetricsTest(StandTest):
    def setUp(self) -> None:
        super().setUp()
        self.patch(METRICS=metrics.Registry({"pod": "search-abc"}), ANSWER_MODEL="qwen3:8b")
        self.client = TestClient(api.app)

    def text(self) -> str:
        r = self.client.get("/metrics")
        self.assertEqual(r.headers["content-type"], metrics.CONTENT_TYPE)
        return r.text

    def test_search_counted_not_service_paths(self):
        self.client.get("/search", params={"q": "governing law"})
        self.client.get("/search", params={"q": "ab"})  # 422
        self.client.get("/healthz")
        self.client.get("/readyz")
        self.text()
        t = self.text()
        self.assertIn('search_requests_total{code="200",endpoint="search",pod="search-abc"} 1', t)
        self.assertIn('search_requests_total{code="422",endpoint="search",pod="search-abc"} 1', t)
        self.assertNotIn('endpoint="metrics"', t)
        self.assertEqual(t.count("search_requests_total{"), 2)
        self.assertIn('search_stage_seconds_count{pod="search-abc",stage="embed"} 1', t)
        self.assertIn('search_stage_seconds_count{pod="search-abc",stage="qdrant"} 1', t)

    def test_retries_counted(self):
        self.patch(QDRANT_LOCAL_URL=closed_port_url())  # свой пир молчит: одна попытка сверху
        self.client.get("/search", params={"q": "governing law"})
        self.assertIn('search_qdrant_retries_total{pod="search-abc"} 1', self.text())

    def test_answer_results(self):
        self.ollama.handler = ollama_handler(chat_content=model_reply())
        self.client.get("/answer", params={"q": "Which law governs?"})
        self.ollama.handler = ollama_handler(chat_content=model_reply(found=False))
        self.client.get("/answer", params={"q": "Which law governs?"})
        self.ollama.handler = ollama_handler(chat_content="garbage")
        self.client.get("/answer", params={"q": "Which law governs?"})
        t = self.text()
        for result in ("answered", "refused", "error"):
            self.assertIn(f'search_answer_total{{pod="search-abc",result="{result}"}} 1', t)
        self.assertIn('search_stage_seconds_count{pod="search-abc",stage="generate"} 3', t)
        self.assertIn('search_requests_total{code="502",endpoint="answer",pod="search-abc"} 1', t)


if __name__ == "__main__":
    unittest.main()
