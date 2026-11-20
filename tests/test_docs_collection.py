"""Коллекция договоров (deploy/qdrant/docs.py): векторы документа как в замере и загрузка в Qdrant."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import tempfile
import unittest
from array import array
from pathlib import Path

from tests.fakes import ADMIN_KEY, FakeServer, MemoryQdrant

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docs_tool", ROOT / "deploy/qdrant/docs.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

POINTS = [  # два документа, куски не по порядку смещений
    {"file": "cuad/a.txt", "title": "Acme", "dataset": "cuad", "start": 500},
    {"file": "cuad/a.txt", "title": "Acme", "dataset": "cuad", "start": 0},
    {"file": "cuad/b.txt", "title": "Beta", "dataset": "cuad", "start": 0},
]
VECS = [[0.0, 1.0], [1.0, 0.0], [0.6, 0.8]]


class BuildTest(unittest.TestCase):
    def test_group_finds_first_chunk_by_offset(self):
        docs = D.group(POINTS)
        self.assertEqual(["cuad/a.txt", "cuad/b.txt"], [d["file"] for d in docs])
        self.assertEqual(([0, 1], 1), (docs[0]["rows"], docs[0]["first"]))  # первый кусок это start 0

    def test_route_is_centroid_and_gate_is_mean_of_title_and_first(self):
        doc = D.group(POINTS)[0]
        route, gate = D.doc_vectors(doc, lambda i: VECS[i], [0.0, 2.0])
        s = 1 / math.sqrt(2)
        self.assertAlmostEqual(s, route[0])
        self.assertAlmostEqual(s, route[1])
        self.assertEqual([0.5, 0.5], gate)  # (название (0,1) + первый кусок (1,0)) / 2
        ref = [s, s]  # балл гейта по Dot = среднее двух косинусов
        self.assertAlmostEqual((s + s) / 2, sum(x * y for x, y in zip(ref, gate)))

    def test_doc_id_is_stable(self):
        self.assertEqual(D.doc_id("cuad/a.txt"), D.doc_id("cuad/a.txt"))
        self.assertNotEqual(D.doc_id("cuad/a.txt"), D.doc_id("cuad/b.txt"))


class LoadTest(unittest.TestCase):
    def test_load_creates_docs_collection_and_file_index(self):
        mq = MemoryQdrant()
        mq.cols["contracts_rf2"] = {"config": {}, "points": {}}
        srv = FakeServer(mq)
        self.addCleanup(srv.close)
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "key").write_text(ADMIN_KEY)
            (t / "meta.json").write_text(json.dumps({"model": "bge-m3", "dim": 2}))
            (t / "points.jsonl").write_text("\n".join(json.dumps(
                {"id": D.doc_id(f), "file": f, "title": f, "dataset": "cuad", "chunks": 2}) for f in ("a", "b")) + "\n")
            for name, values in (("route.f32", [1, 0, 0, 1]), ("gate.f32", [0.5, 0.5, 0.2, 0.1])):
                with open(t / name, "wb") as f:
                    array("f", values).tofile(f)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = D.main(["load", "--url", srv.url, "--key-file", str(t / "key"), "--dir", str(t),
                             "--collection", "contracts_rf2"])
        self.assertIn("contracts_rf2_docs: документов 2, статус green", out.getvalue())
        self.assertEqual(0, rc)
        col = mq.cols["contracts_rf2_docs"]
        self.assertEqual({"route": {"size": 2, "distance": "Cosine"}, "gate": {"size": 2, "distance": "Dot"}},
                         col["config"]["vectors"])
        self.assertEqual((1, 3), (col["config"]["shard_number"], col["config"]["replication_factor"]))
        p = col["points"][D.doc_id("b")]
        self.assertEqual(([0.0, 1.0], [0.2, 0.1]), (p["vector"]["route"], [round(x, 6) for x in p["vector"]["gate"]]))
        self.assertEqual(2, p["payload"]["chunks"])
        self.assertEqual([{"field_name": "file", "field_schema": "keyword"}], mq.cols["contracts_rf2"]["indexes"])


if __name__ == "__main__":
    unittest.main()
