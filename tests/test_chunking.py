"""Нарезка (app/chunking.py) и BM25 (app/bm25.py): куски подряд и без потерь, веса предсказуемы."""
from __future__ import annotations

import random
import unittest

from app import bm25
from app.chunking import STRATEGIES, naive, rcts


def sample_text(seed: int = 0, paragraphs: int = 40) -> str:
    rng = random.Random(seed)
    words = "agreement party shall terminate notice license governing law delaware confidential".split()
    paras = []
    for _ in range(paragraphs):
        lines = [" ".join(rng.choice(words) for _ in range(rng.randint(3, 60))) for _ in range(rng.randint(1, 4))]
        paras.append("\n".join(lines))
    return "\n\n".join(paras) + " " + "x" * 1300  # длинное слово без разделителей режется по символам


class ChunkingTest(unittest.TestCase):
    def assert_tiles(self, text: str, spans: list[tuple[int, int]], size: int) -> None:
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for (s1, e1), (s2, _) in zip(spans, spans[1:]):
            self.assertEqual(e1, s2)  # подряд, без дыр и перекрытий
        self.assertTrue(all(0 < e - s <= size for s, e in spans))
        self.assertEqual("".join(text[s:e] for s, e in spans), text)

    def test_naive(self):
        text = sample_text()
        spans = naive(text, 500)
        self.assert_tiles(text, spans, 500)
        self.assertTrue(all(e - s == 500 for s, e in spans[:-1]))

    def test_rcts(self):
        for seed in range(5):
            text = sample_text(seed)
            self.assert_tiles(text, rcts(text, 500), 500)

    def test_rcts_keeps_separator_on_left(self):
        text = "a" * 300 + "\n\n" + "b" * 300
        spans = rcts(text, 500)
        self.assertEqual(text[spans[0][0]:spans[0][1]], "a" * 300 + "\n\n")

    def test_short_and_empty(self):
        self.assertEqual(naive("", 500), [])
        self.assertEqual(rcts("short", 500), [(0, 5)])
        self.assertEqual(STRATEGIES["naive500"]("x" * 1001), [(0, 500), (500, 1000), (1000, 1001)])


class Bm25Test(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(bm25.tokens("The Licensee SHALL notify; a 30-day notice, in writing."),
                         ["licensee", "notify", "30", "day", "notice", "writing"])

    def test_query_sparse(self):
        q = bm25.query_sparse("notice notice termination")
        self.assertEqual(q["indices"], sorted(set(q["indices"])))
        self.assertEqual(len(q["indices"]), 2)
        self.assertEqual(q["values"], [1.0, 1.0])
        self.assertEqual(q, bm25.query_sparse("termination notice"))  # порядок и повторы не важны

    def test_term_id_stable(self):
        self.assertEqual(bm25.term_id("notice"), bm25.term_id("notice"))
        self.assertLess(bm25.term_id("notice"), 2 ** 31)

    def test_doc_sparse_saturates(self):
        one = bm25.doc_sparse("notice", avgdl=1)
        many = bm25.doc_sparse(" ".join(["notice"] * 50), avgdl=50)
        w1, w50 = one["values"][0], many["values"][0]
        self.assertLess(w1, w50)
        self.assertLess(w50, 1.2 + 1)  # tf насыщается к k1 + 1


if __name__ == "__main__":
    unittest.main()
