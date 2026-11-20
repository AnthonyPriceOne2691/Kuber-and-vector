"""Замер вживую через вход (eval/live_eval.py): метрика по адресам из ответа сервиса и счёт маршрута."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from eval import live_eval as L

QUERIES = [
    {"dataset": "cuad", "query": "Consider the Acme License; Which law governs?",
     "snippets": [["cuad/acme.txt", 1000, 1100]]},
    {"dataset": "maud", "query": "Consider the Beta Merger; What is the outside date?",
     "snippets": [["maud/beta.txt", 0, 100]]},
]


def fake(q: str) -> dict:
    if "Acme" in q:  # верный договор, нужный пункт вторым
        return {"collection": "contracts_rf2", "served_by": {"node": "machine-2"},
                "route": {"document": {"file": "cuad/acme.txt"}},
                "results": [{"file": "cuad/acme.txt", "span": [0, 500]}, {"file": "cuad/acme.txt", "span": [1000, 1500]}]}
    return {"collection": "contracts_rf2", "served_by": {"node": "machine-3"}, "route": {"gate": {"passed": False}},
            "not_in_base": True, "results": []}


class LiveEvalTest(unittest.TestCase):
    def test_metrics_and_route_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in QUERIES) + "\n")
            args = argparse.Namespace(stage=tmp, limit=0, k=64, sleep=0, entry="https://x")
            res = L.run(args, fetch=fake)
        self.assertEqual(2, res["queries"])
        self.assertEqual({"routed": 1, "right_document": 1, "not_in_base": 1}, res["route"])
        cuad = res["per_query"]["cuad"]
        self.assertEqual([0.0, 0.0], cuad["1"][0])        # первым шла преамбула
        self.assertEqual(1.0, cuad["2"][0][1])            # пункт найден на втором месте
        self.assertEqual(0.0, res["per_query"]["maud"]["64"][0][1])
        self.assertEqual({"machine-2": 1, "machine-3": 1}, res["served_by"])
        self.assertEqual("test", res["entry"])
        self.assertEqual({"cuad", "maud"}, set(res["datasets"]))

    def test_key_header(self):
        self.assertEqual(("X-API-Key", "abc"), L.key_header("X-API-Key: abc\n"))
        self.assertEqual(("X-API-Key", "abc"), L.key_header("abc"))


if __name__ == "__main__":
    unittest.main()
