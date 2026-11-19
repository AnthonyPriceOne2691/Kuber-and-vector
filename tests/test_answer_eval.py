"""Оценка ответа (eval/answer_eval.py) на крошечном индексе с подставной Ollama: прогон, докачка, итоги."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from array import array
from unittest import mock

from eval import answer_eval as E
from tests.fakes import FakeServer

TEXTS = [
    "This Agreement shall be governed by the laws of the State of Delaware.",
    "Either party may terminate this Agreement upon thirty days written notice.",
    "Supplier shall deliver the Products within ten business days of each order.",
]
POINTS = [
    {"id": "a", "dataset": "cuad", "file": "cuad/Acme_License.txt", "title": "Acme License", "section": "",
     "start": 0, "end": 70, "text": TEXTS[0]},
    {"id": "b", "dataset": "cuad", "file": "cuad/Acme_License.txt", "title": "Acme License", "section": "",
     "start": 70, "end": 145, "text": TEXTS[1]},
    {"id": "c", "dataset": "cuad", "file": "cuad/Beta_Supply.txt", "title": "Beta Supply", "section": "7. Delivery.",
     "start": 0, "end": 76, "text": TEXTS[2]},
]
VECS = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
QUERIES = [
    ({"dataset": "cuad", "query": "Which law governs the Acme license?", "sparse": {},
      "snippets": [["cuad/Acme_License.txt", 10, 50]]}, [1, 0.1, 0, 0]),
    ({"dataset": "cuad", "query": "When must Beta deliver the products?", "sparse": {},
      "snippets": [["cuad/Beta_Supply.txt", 0, 30]]}, [0, 0, 1, 0.1]),
]
BENCH = {"tests": [
    {"query": "Which law governs the Acme license?", "snippets": [{"file_path": "cuad/Acme_License.txt", "span": [10, 50]}]},
    {"query": "Consider the Gamma lease; who pays rent?", "snippets": [{"file_path": "cuad/Gamma_Lease.txt", "span": [0, 40]}]},
    {"query": "Consider the Gamma lease; can it be renewed?", "snippets": [{"file_path": "cuad/Gamma_Lease.txt", "span": [90, 140]}]},
]}


def chat_handler(method, path, headers, body):
    req = json.loads(body)
    if path == "/api/embed":
        return 200, {"embeddings": [[0.5] * 4 for _ in req["input"]]}
    user = req["messages"][1]["content"]
    if "Gamma" in user.splitlines()[0]:  # договора Gamma в индексе нет: модель честно отказывает
        content = {"found": False, "answer": "", "citations": []}
    else:
        first = user.split("[1] Document: ")[1].splitlines()[1]
        content = {"found": True, "answer": "See [1].", "citations": [{"n": 1, "quote": first[:40]}]}
    return 200, {"message": {"role": "assistant", "content": json.dumps(content)},
                 "prompt_eval_count": 300, "eval_count": 40, "eval_duration": 1_000_000_000}


def write_f32(path: str, rows: list[list[float]]) -> None:
    with open(path, "wb") as f:
        for r in rows:
            array("f", r).tofile(f)


class AnswerEvalRunTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = self.tmp = tmp.name
        for d in ("index", "stage", "data/benchmarks", "emb"):
            os.makedirs(os.path.join(t, d))
        with open(os.path.join(t, "index/points.jsonl"), "w") as f:
            f.writelines(json.dumps(p) + "\n" for p in POINTS)
        write_f32(os.path.join(t, "index/vectors.f32"), VECS)
        with open(os.path.join(t, "stage/meta.json"), "w") as f:
            json.dump({"dim": 4}, f)
        with open(os.path.join(t, "stage/queries.jsonl"), "w") as f:
            f.writelines(json.dumps(q) + "\n" for q, _ in QUERIES)
        write_f32(os.path.join(t, "stage/query_vectors.f32"), [v for _, v in QUERIES])
        with open(os.path.join(t, "data/benchmarks/cuad.json"), "w") as f:
            json.dump(BENCH, f)
        self.ollama = FakeServer(chat_handler)
        self.addCleanup(self.ollama.close)

    def run_eval(self, *extra: str) -> None:
        t = self.tmp
        argv = ["answer_eval.py", "--data", f"{t}/data", "--stage", f"{t}/stage", "--index", f"{t}/index",
                "--ollama", self.ollama.url, "--embed-cache", f"{t}/emb", "--k", "2", "--per-dataset", "5",
                "--out-of-base", "5", "--out", f"{t}/answers.jsonl", "--report", f"{t}/answer.md", *extra]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            E.main()

    def records(self) -> list[dict]:
        with open(f"{self.tmp}/answers.jsonl") as f:
            return [json.loads(line) for line in f]

    def test_run_resume_and_report(self):
        self.run_eval()
        recs = {r["query"]: r for r in self.records()}
        self.assertEqual(len(recs), 4)  # 2 в базе и 2 вне: вопрос про Acme в индексе, из «вне базы» выпал
        acme = recs["Which law governs the Acme license?"]
        self.assertEqual((acme["kind"], acme["status"], acme["gold_retrieved"]), ("in", "answered", True))
        self.assertEqual(acme["retrieved"][0], ["cuad/Acme_License.txt", 0, 70])
        c = acme["citations"][0]
        self.assertEqual((c["valid"], c["verified"], c["gold"]), (True, True, True))
        self.assertEqual(acme["tokens_per_s"], 40.0)
        beta = recs["When must Beta deliver the products?"]
        self.assertEqual((beta["status"], beta["citations"][0]["file"]), ("answered", "cuad/Beta_Supply.txt"))
        gamma = [r for r in recs.values() if r["kind"] == "out"]
        self.assertEqual([r["status"] for r in gamma], ["refused", "refused"])
        chats = self.ollama.paths().count("/api/chat")
        self.assertEqual(chats, 4)

        self.run_eval()  # докачка: всё готово, к модели больше не ходим
        self.assertEqual(self.ollama.paths().count("/api/chat"), chats)
        self.assertEqual(len(self.records()), 4)
        with open(f"{self.tmp}/answer.md") as f:
            text = f.read()
        self.assertIn("| отказ (правильно) | 100.0 [100.0; 100.0] |", text)
        self.assertIn("| ответ с подтверждённой цитатой | 100.0 [100.0; 100.0] |", text)

        os.remove(f"{self.tmp}/answer.md")
        self.run_eval("--report-only")
        self.assertTrue(os.path.exists(f"{self.tmp}/answer.md"))


class SummarizeTest(unittest.TestCase):
    def rec(self, kind, status, gold_retrieved=True, cits=()):
        return {"kind": kind, "status": status, "gold_retrieved": gold_retrieved, "citations": list(cits),
                "seconds": 2.0, "tokens_per_s": 30.0, "prompt_tokens": 800}

    def test_shares(self):
        ok = {"valid": True, "verified": True, "gold": True}
        wrong = {"valid": True, "verified": False, "gold": False}
        ghost = {"valid": False, "verified": False, "gold": False}
        s = E.summarize([self.rec("in", "answered", cits=[ok, wrong]), self.rec("in", "refused"),
                         self.rec("in", "unsupported", cits=[ghost]), self.rec("in", "answered", False, cits=[ok]),
                         self.rec("out", "refused"), self.rec("out", "answered", cits=[ok])])
        self.assertEqual((s["n_in"], s["n_out"], s["n_citations"]), (4, 2, 4))
        self.assertEqual(s["in"]["answered"][0], 50.0)
        self.assertEqual(s["cit_valid"][0], 75.0)        # 3 из 4 ссылок на существующий кусок
        self.assertAlmostEqual(s["cit_verified"][0], 200 / 3)  # 2 из 3 существующих подтверждены
        self.assertAlmostEqual(s["in_answered_when_found"][0], 100 / 3)  # эталон найден у трёх, ответ у одного
        self.assertEqual((s["out_declined"][0], s["out_answered"][0]), (50.0, 50.0))
        self.assertEqual(s["seconds_p50"], 2.0)

    def test_overlaps(self):
        gold = [["a.txt", 10, 20]]
        self.assertTrue(E.overlaps("a.txt", [15, 30], gold))
        self.assertFalse(E.overlaps("a.txt", [20, 30], gold))  # касание без общего знака
        self.assertFalse(E.overlaps("b.txt", [10, 20], gold))


if __name__ == "__main__":
    unittest.main()
