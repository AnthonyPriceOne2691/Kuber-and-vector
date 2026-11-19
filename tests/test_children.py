"""Поиск «от малого к большому»: дети родителя (eval/children.py), гибрид с запасом кандидатов
(eval/qdrant_eval.py --prefetch) и парное сравнение замеров (eval/compare.py)."""
from __future__ import annotations

import unittest

from eval import compare
from eval import qdrant_eval as Q
from eval.children import children_of, points_for

PARENT = {"id": "p1", "dataset": "cuad", "file": "cuad/Acme.txt", "title": "Acme", "section": "7. Term.",
          "start": 1000, "end": 1500, "text": "a" * 200 + "b" * 200 + "c" * 100}


class ChildrenTest(unittest.TestCase):
    def test_children_cover_parent_and_carry_its_address(self):
        kids = children_of(PARENT, 200, 200)
        self.assertEqual(["a" * 200, "b" * 200, "c" * 100], [k["text"] for k in kids])
        self.assertEqual([1000, 1200, 1400], [k["child_start"] for k in kids])
        for k in kids:  # в выдаче родитель: адрес и раздел родительские
            self.assertEqual((1000, 1500, "p1", "7. Term.", "Acme"),
                             (k["start"], k["end"], k["parent"], k["section"], k["title"]))

    def test_ids_are_unique_and_stable(self):
        a, b = children_of(PARENT, 200, 200), children_of(PARENT, 200, 200)
        self.assertEqual([k["id"] for k in a], [k["id"] for k in b])
        self.assertEqual(3, len({k["id"] for k in a}))
        other = children_of(PARENT, 100, 100)  # другой размер даёт другие точки, не перезаписывает эти
        self.assertFalse({k["id"] for k in a} & {k["id"] for k in other})

    def test_overlap_and_short_parent(self):
        kids = children_of(PARENT, 200, 100)  # с перекрытием, последний ребёнок доходит до конца
        self.assertEqual(500, kids[-1]["child_start"] - 1000 + len(kids[-1]["text"]))
        short = dict(PARENT, text="short clause")
        self.assertEqual(["short clause"], [k["text"] for k in children_of(short, 200, 200)])

    def test_with_parents_adds_parent_points(self):
        pts = points_for([dict(PARENT, sparse={"indices": [1], "values": [1.0]})], 200, 200, with_parents=True)
        self.assertEqual(4, len(pts))
        own = pts[0]
        self.assertEqual(("p1", "p1", 1000, PARENT["text"]), (own["id"], own["parent"], own["child_start"], own["text"]))
        self.assertNotIn("sparse", own)  # BM25 пересчитывается по всем точкам вместе
        self.assertEqual(3, len(points_for([PARENT], 200, 200)))

    def test_blank_pieces_are_skipped(self):
        p = dict(PARENT, text="x" * 200 + " " * 200 + "y" * 50)
        self.assertEqual(["x" * 200, "y" * 50], [k["text"] for k in children_of(p, 200, 200)])


class PrefetchTest(unittest.TestCase):
    def test_hybrid_prefetch_is_configurable(self):
        body = Q.body_for("hybrid", [0.1], {"indices": [1], "values": [1.0]}, 64, exact=True, prefetch=300)
        self.assertEqual([300, 300], [p["limit"] for p in body["prefetch"]])
        self.assertEqual(64, body["limit"])
        base = Q.body_for("hybrid", [0.1], {"indices": [1], "values": [1.0]}, 64, exact=True)
        self.assertEqual([Q.PREFETCH] * 2, [p["limit"] for p in base["prefetch"]])


def fake_result(name: str, recall: float, n: int = 3) -> dict:
    ks = compare.K
    per_query = {d: {k: [[0.1, recall]] * n for k in ks} for d in compare.DS}
    agg = {k: {"precision": 10.0, "recall": 100 * recall} for k in ks}
    return {"collection": name, "mode": "dense", "queries": n * len(compare.DS), "per_query": per_query,
            "datasets": {d: agg for d in compare.DS}, "all": agg}


class CompareTest(unittest.TestCase):
    def test_paired_difference(self):
        text = "\n".join(compare.report(fake_result("new", 0.75), fake_result("base", 0.5)))
        self.assertIn("| 64 | 50.0 | 75.0 | +25.0 [+25.0; +25.0] |", text)
        self.assertIn("recall 50.00 → 75.00", text)

    def test_different_questions_are_refused(self):
        with self.assertRaises(SystemExit):
            compare.report(fake_result("new", 0.75, n=3), fake_result("base", 0.5, n=4))


if __name__ == "__main__":
    unittest.main()
