"""Гейт «этого договора в базе нет» (eval/gate_eval.py): AUC, пороги, половины по документам, сводка."""
from __future__ import annotations

import unittest

from eval import gate_eval as G


class MetricsTest(unittest.TestCase):
    def test_auc(self):
        self.assertEqual(1.0, G.auc([0.9, 0.8], [0.1, 0.2]))
        self.assertEqual(0.0, G.auc([0.1], [0.5, 0.6]))
        self.assertEqual(0.5, G.auc([0.5], [0.5]))  # ничья пополам

    def test_threshold_keeps_false_refusals_under_limit(self):
        inside = [i / 100 for i in range(100)]  # 0.00 … 0.99
        t = G.threshold_for(inside, 0.05)
        self.assertLessEqual(sum(x < t for x in inside), 5)
        self.assertEqual(0.0, G.threshold_for(inside, 0.0))  # ни одного ложного отказа

    def test_youden_separates_clean_split(self):
        t = G.youden([0.8, 0.9, 0.85], [0.1, 0.2, 0.3])
        self.assertTrue(0.3 < t <= 0.8)
        self.assertEqual([1.0, 1.0, 1.0], G.refused([0.1, 0.2, 0.3], t))
        self.assertEqual([0.0, 0.0, 0.0], G.refused([0.8, 0.9, 0.85], t))

    def test_doc_half_is_stable_and_splits(self):
        files = [f"cuad/doc_{i}.txt" for i in range(200)]
        halves = [G.doc_half(f) for f in files]
        self.assertEqual(halves, [G.doc_half(f) for f in files])
        self.assertTrue(60 < halves.count("A") < 140)


class FakeRouter:
    """Балл документа задан прямо: вопрос из базы похож на свой договор, вне базы ни на что."""

    def scores(self, q, name):
        return [0.9, 0.2] if q["inside"] else [0.3, 0.25]


class EvaluateTest(unittest.TestCase):
    def test_gate_refuses_outside_and_keeps_inside(self):
        inside = [{"i": i, "inside": True, "dataset": "cuad", "ref": "Consider A"} for i in range(8)]
        outside = [{"i": i, "inside": False, "dataset": "cuad", "ref": f"Consider X{i}", "half": "AB"[i % 2]}
                   for i in range(8)]
        res = G.evaluate(FakeRouter(), inside, outside, {i: "AB"[i % 2] for i in range(8)})
        r = res["centroid"]
        self.assertEqual(1.0, r["auc_B"])
        balanced = r["thresholds"][-1]
        self.assertEqual(0.0, balanced["false_B"][0])
        self.assertEqual(100.0, balanced["true_B"][0])
        lines = G.report(res, 2)
        self.assertTrue(any(line.startswith("## Вывод") for line in lines))


if __name__ == "__main__":
    unittest.main()
