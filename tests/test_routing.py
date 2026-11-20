"""Выбор договора, деление вопроса и гейт «этого договора в базе нет» (app/routing.py, app/api.py)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from app import routing

try:
    from app import api
    from fastapi.testclient import TestClient
except ImportError:  # без FastAPI или httpx тесты ручек пропускаются
    api = TestClient = None

from tests.fakes import KEY, FakeServer, MemoryQdrant


class RoutingLogicTest(unittest.TestCase):
    def test_split_question(self):
        self.assertEqual(("Consider the NDA between A and B", "Does it survive termination?"),
                         routing.split_question("Consider the NDA between A and B; Does it survive termination?"))
        self.assertEqual(("Consider X", "a; b"), routing.split_question("Consider X; a; b"))  # по первой «;»
        for q in ("no semicolon here", "; only question", "only reference;", "  ;  "):
            self.assertIsNone(routing.split_question(q), q)

    def test_smooth_and_rank(self):
        self.assertEqual([2.0, 2.75, 2.5], routing.smooth([1.0, 2.0, 1.5], 0.5))
        self.assertEqual([3.0], routing.smooth([2.0], 0.5))  # один кусок: сосед это он сам
        pts = [{"id": "c", "score": 0.1, "payload": {"start": 1000}},
               {"id": "a", "score": 0.5, "payload": {"start": 0}},
               {"id": "b", "score": 0.6, "payload": {"start": 500}}]
        ranked = routing.rank_in_doc(pts, 0.5)  # порядок соседей по смещению, а не по выдаче
        self.assertEqual(["b", "a", "c"], [i for _, i in ranked])
        self.assertAlmostEqual(0.6 + 0.25, ranked[0][0])

    def test_mix_and_gate_threshold(self):
        self.assertEqual([0.25, 0.75], routing.mix([1.0, 0.0], [0.0, 1.0], 0.25))
        with mock.patch.dict(os.environ, {"GATE_MIN": ""}):
            self.assertEqual(0.586, routing.gate_min("bge-m3:latest"))
            self.assertIsNone(routing.gate_min("snowflake-arctic-embed2"))  # порог не мерили: гейта нет
        with mock.patch.dict(os.environ, {"GATE_MIN": "0.7"}):
            self.assertEqual(0.7, routing.gate_min("snowflake-arctic-embed2"))
        with mock.patch.dict(os.environ, {"GATE_MIN": "off"}):
            self.assertIsNone(routing.gate_min("bge-m3"))
        self.assertEqual("contracts_rf2_docs", routing.docs_collection("contracts_rf2"))


def u(*xs: float) -> list[float]:
    return routing.unit(list(xs))


# Вопросы и их части: вектор каждого текста задан (подставная Ollama отдаёт его по тексту)
REF_A, ASK_A = "Consider the Acme License between Acme and Beta", "Which law governs?"
REF_OUT = "Consider the Zeta Lease between Zeta and Omega"
VECTORS = {
    REF_A: u(1, 0, 0, 0),
    ASK_A: u(0, 0, 0, 1),
    f"{REF_A}; {ASK_A}": u(1, 0, 0, 1),
    REF_OUT: u(0, 0, 1, 0),
    f"{REF_OUT}; {ASK_A}": u(0, 0, 1, 1),
    "governing law": u(0, 0, 0, 1),
}


def ollama_vectors(method, path, headers, body):
    req = json.loads(body or b"{}")
    if method == "POST" and path == "/api/embed":
        return 200, {"model": req["model"], "embeddings": [VECTORS[t] for t in req["input"]]}
    if method == "POST" and path == "/api/chat":
        return 500, {"error": "the model must not be called"}
    return 404, {"error": "not found"}


def chunk(file: str, title: str, start: int, vec: list[float]) -> tuple[str, dict]:
    cid = f"{file}#{start}"
    return cid, {"vector": {"dense": vec}, "payload": {"file": file, "title": title, "section": "1. Law.",
                                                     "start": start, "end": start + 500,
                                                     "text": f"{title} text at {start}"}}


def memory_stand(with_docs: bool = True) -> MemoryQdrant:
    mq = MemoryQdrant()
    a, b = "cuad/acme.txt", "cuad/beta.txt"
    chunks = dict([chunk(a, "Acme License", 0, u(1, 0, 0, 0.1)),     # преамбула со сторонами
                   chunk(a, "Acme License", 500, u(0, 0, 0.2, 0.9)),
                   chunk(a, "Acme License", 1000, u(0, 0, 0.1, 1)),  # нужный пункт
                   chunk(a, "Acme License", 1500, u(0, 1, 0, 0.2)),
                   chunk(b, "Beta Supply", 0, u(0, 1, 0, 0)),
                   chunk(b, "Beta Supply", 500, u(0, 1, 0, 0.5))])
    mq.cols["contracts_rf2"] = {"config": {"vectors": {"dense": {"size": 4, "distance": "Cosine"}}}, "points": chunks}
    if with_docs:
        docs = {
            "d-a": {"vector": {"route": u(1, 0, 0, 0.3), "gate": [0.9, 0, 0, 0]},
                    "payload": {"file": a, "title": "Acme License", "chunks": 4}},
            "d-b": {"vector": {"route": u(0, 1, 0, 0), "gate": [0, 0.9, 0, 0]},
                    "payload": {"file": b, "title": "Beta Supply", "chunks": 2}},
        }
        mq.cols["contracts_rf2_docs"] = {"config": {"vectors": {"route": {"size": 4, "distance": "Cosine"},
                                                               "gate": {"size": 4, "distance": "Dot"}}},
                                         "points": docs}
    return mq


@unittest.skipIf(TestClient is None, "нужны fastapi и httpx")
class RoutedSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.start(memory_stand())

    def start(self, mq: MemoryQdrant) -> None:
        self.mq = mq
        self.qdrant = FakeServer(mq)
        self.ollama = FakeServer(ollama_vectors)
        self.addCleanup(self.qdrant.close)
        self.addCleanup(self.ollama.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key_file = os.path.join(tmp.name, "key")
        with open(key_file, "w") as f:
            f.write(KEY)
        p = mock.patch.multiple(api, QDRANT_URL=self.qdrant.url, OLLAMA_URL=self.ollama.url, KEY_FILE=key_file,
                                COLLECTION="contracts_rf2", SEARCH_MODE="dense", SEARCH_ALIAS="", ROUTE=True,
                                QDRANT_LOCAL_URL="", QDRANT_CONNECT_TIMEOUT=0.3, QDRANT_READ_TIMEOUT=0.3,
                                QDRANT_ATTEMPTS=2, _DOCS_MISSING={})
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"GATE_MIN": ""})
        e.start()
        self.addCleanup(e.stop)
        self.client = TestClient(api.app)

    def search(self, q: str, k: int = 3) -> dict:
        r = self.client.get("/search", params={"q": q, "k": k})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_contract_chosen_and_clause_found(self):
        body = self.search(f"{REF_A}; {ASK_A}")
        route = body["route"]
        self.assertEqual("cuad/acme.txt", route["document"]["file"])
        self.assertEqual((REF_A, ASK_A), (route["reference"], route["question"]))
        self.assertTrue(route["gate"]["passed"])
        self.assertAlmostEqual(0.9, route["gate"]["score"], places=3)
        self.assertNotIn("not_in_base", body)
        self.assertEqual({"cuad/acme.txt"}, {h["file"] for h in body["results"]})
        self.assertEqual([1000, 500], [h["span"][0] for h in body["results"][:2]])  # пункт, а не преамбула
        self.assertEqual("Acme License text at 1000", body["results"][0]["text"])
        self.assertEqual(["Consider the Acme License between Acme and Beta; Which law governs?", REF_A, ASK_A],
                         self.ollama.json_bodies()[-1]["input"])  # одна поездка в Ollama за три вектора
        chunk_queries = [b for b in self.qdrant.json_bodies() if isinstance(b, dict) and "filter" in b]
        self.assertEqual([{"key": "file", "match": {"value": "cuad/acme.txt"}}], chunk_queries[0]["filter"]["must"])
        self.assertTrue(chunk_queries[0]["params"]["exact"])

    def test_next_contract_fills_when_first_is_short(self):
        body = self.search(f"{REF_A}; {ASK_A}", k=5)
        self.assertEqual(["cuad/acme.txt"] * 4 + ["cuad/beta.txt"], [h["file"] for h in body["results"]])

    def test_contract_not_in_base(self):
        body = self.search(f"{REF_OUT}; {ASK_A}")
        self.assertTrue(body["not_in_base"])
        self.assertEqual("this contract is not in the database", body["message"])
        self.assertEqual([], body["results"])
        self.assertFalse(body["route"]["gate"]["passed"])
        self.assertIsNone(body["route"]["document"])
        paths = self.qdrant.paths()
        self.assertFalse([p for p in paths if p.startswith("/collections/contracts_rf2/")])  # куски не искали

    def test_answer_refused_without_model(self):
        r = self.client.get("/answer", params={"q": f"{REF_OUT}; {ASK_A}", "k": 3})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertEqual(("refused", "This contract is not in the database."), (body["status"], body["answer"]))
        self.assertTrue(body["not_in_base"])
        self.assertEqual([], body["sources"])
        self.assertNotIn("/api/chat", self.ollama.paths())

    def test_question_without_semicolon_searches_everything(self):
        body = self.search("governing law")
        self.assertNotIn("route", body)
        self.assertNotIn("/collections/contracts_rf2_docs/points/query/batch", self.qdrant.paths())

    def test_without_gate_threshold(self):
        with mock.patch.dict(os.environ, {"GATE_MIN": "off"}):
            body = self.search(f"{REF_OUT}; {ASK_A}")
        self.assertIsNone(body["route"]["gate"])
        self.assertTrue(body["results"])  # без гейта отказа нет, договор выбирается как обычно


@unittest.skipIf(TestClient is None, "нужны fastapi и httpx")
class NoDocsCollectionTest(RoutedSearchTest):
    def setUp(self) -> None:
        self.start(memory_stand(with_docs=False))

    def test_falls_back_to_plain_search(self):
        body = self.search(f"{REF_A}; {ASK_A}")
        self.assertNotIn("route", body)
        self.assertEqual(3, len(body["results"]))
        n = self.qdrant.paths().count("/collections/contracts_rf2_docs/points/query/batch")
        self.search(f"{REF_A}; {ASK_A}")  # второй раз коллекцию документов не спрашиваем (DOCS_RECHECK)
        self.assertEqual(n, self.qdrant.paths().count("/collections/contracts_rf2_docs/points/query/batch"))

    # тесты маршрута из родителя здесь неприменимы
    test_contract_chosen_and_clause_found = test_next_contract_fills_when_first_is_short = None
    test_contract_not_in_base = test_answer_refused_without_model = test_without_gate_threshold = None


if __name__ == "__main__":
    unittest.main()
