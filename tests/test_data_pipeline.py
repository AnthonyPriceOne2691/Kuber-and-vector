"""Конвейер данных без Мака и без кластера: кэш векторов с диска, подготовка полного корпуса,
порционная загрузка в Qdrant. Ollama и Qdrant подставные."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from array import array
from unittest import mock

from app.embedding import OllamaEmbedder
from eval import legalbench as lb
from eval import prepare, qdrant_load
from tests.fakes import KEY, FakeServer


def embed_handler(dim: int = 4):
    """Ollama /api/embed: вектор зависит от текста (длина, первая буква), чтобы различать строки."""
    def handler(method, path, headers, body):
        texts = json.loads(body)["input"]
        return 200, {"embeddings": [[float(len(t)), float(ord(t[0])), 1.0, 0.0][:dim] for t in texts]}
    return handler


class EmbedderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.ollama = FakeServer(embed_handler())
        self.addCleanup(self.ollama.close)

    def emb(self) -> OllamaEmbedder:
        e = OllamaEmbedder(url=self.ollama.url, cache_dir=self.dir, batch=2)
        self.addCleanup(e.close)
        return e

    def test_cache_on_disk_and_reused(self):
        e = self.emb()
        e.ensure(["alpha", "beta", "alpha", "gamma"], progress=False)
        self.assertEqual(e.computed, 3)  # повтор внутри списка не считается дважды
        self.assertEqual(list(e.vector("beta")), [4.0, 98.0, 1.0, 0.0])
        self.assertFalse(hasattr(e, "vectors"))  # векторы не держим в памяти
        e2 = self.emb()  # новый процесс: всё уже в кэше
        e2.ensure(["gamma", "alpha", "delta"], progress=False)
        self.assertEqual(e2.computed, 1)
        self.assertEqual(list(e2.vector("alpha")), [5.0, 97.0, 1.0, 0.0])
        self.assertEqual(list(e2.vector("delta")), [5.0, 100.0, 1.0, 0.0])
        calls = sum(len(b["input"]) for b in self.ollama.json_bodies())
        self.assertEqual(calls, 4)

    def test_broken_tail_repaired(self):
        e = self.emb()
        e.ensure(["alpha", "beta"], progress=False)
        e.close()
        with open(e.vec_path, "ab") as f:  # обрыв: половина вектора без ключа
            array("f", [1.0, 2.0]).tofile(f)
        with open(e.keys_path, "a") as f:
            f.write("f" * 64 + "\n")  # и ключ без вектора
        e2 = self.emb()
        self.assertEqual(len(e2.index), 2)
        self.assertEqual(os.path.getsize(e2.vec_path), 2 * 4 * 4)
        e2.ensure(["gamma"], progress=False)  # дописывается ровно после целых строк
        self.assertEqual(list(e2.vector("gamma")), [5.0, 103.0, 1.0, 0.0])
        self.assertEqual(list(e2.vector("beta")), [4.0, 98.0, 1.0, 0.0])


def write_dataset(root: str) -> None:
    """Крошечный LegalBench-RAG: у каждого набора свой документ и два вопроса."""
    os.makedirs(os.path.join(root, "benchmarks"))
    for i, ds in enumerate(lb.DATASETS):
        os.makedirs(os.path.join(root, "corpus", ds))
        text = (f"{i + 1}. Term. This agreement {ds} starts today. " * 30).strip()
        with open(os.path.join(root, "corpus", ds, "Doc_One.txt"), "w") as f:
            f.write(text)
        tests = [{"query": f"Question {j} about {ds}?", "snippets": [{"file_path": f"{ds}/Doc_One.txt", "span": [0, 40 + j]}]}
                 for j in range(2)]
        with open(os.path.join(root, "benchmarks", f"{ds}.json"), "w") as f:
            json.dump({"tests": tests}, f)


class PrepareFullTest(unittest.TestCase):
    def test_full_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(os.path.join(tmp, "data"))
            ollama = FakeServer(embed_handler(dim=4))
            self.addCleanup(ollama.close)
            argv = ["prepare.py", "--full", "--data", os.path.join(tmp, "data"), "--out", os.path.join(tmp, "out"),
                    "--ollama", ollama.url, "--embed-cache", os.path.join(tmp, "emb")]
            with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
                prepare.main()
            out = os.path.join(tmp, "out")
            self.assertEqual(sorted(os.listdir(out)), ["meta.json", "naive500_title", "queries.jsonl", "query_vectors.f32"])
            with open(os.path.join(out, "naive500_title", "points.jsonl")) as f:
                points = [json.loads(line) for line in f]
            self.assertEqual({p["dataset"] for p in points}, set(lb.DATASETS))
            self.assertTrue(all("sparse" not in p for p in points))  # у полного корпуса без BM25
            self.assertTrue(all(p["text"] and p["end"] - p["start"] <= 500 for p in points))
            vec = array("f")
            with open(os.path.join(out, "naive500_title", "vectors.f32"), "rb") as f:
                vec.fromfile(f, len(points) * 4)
                self.assertEqual(f.read(), b"")
            self.assertEqual({vec[i * 4 + 1] for i in range(len(points))}, {float(ord("D"))})  # «Document: …» в тексте вектора
            with open(os.path.join(out, "queries.jsonl")) as f:
                queries = [json.loads(line) for line in f]
            self.assertEqual(len(queries), 2 * len(lb.DATASETS))  # полный тест: все вопросы
            self.assertTrue(all(q["sparse"]["indices"] and q["snippets"] for q in queries))
            self.assertEqual(os.path.getsize(os.path.join(out, "query_vectors.f32")), len(queries) * 4 * 4)
            with open(os.path.join(out, "meta.json")) as f:
                meta = json.load(f)
            self.assertEqual((meta["selection"], meta["sparse"], meta["docs"]), ("full", False, 4))


class LoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.collections: list[str] = []

        def handler(method, path, headers, body):
            if headers.get("api-key") != KEY:
                return 401, {}
            if method == "GET" and path == "/collections":
                return 200, {"result": {"collections": [{"name": n} for n in self.collections]}}
            if method == "PUT" and path == "/collections/test":
                self.collections.append("test")
                return 200, {"result": True}
            if method == "PUT" and path.startswith("/collections/test/points"):
                return 200, {"result": {"status": "completed"}}
            if method == "GET" and path == "/collections/test":
                return 200, {"result": {"status": "green", "points_count": 5}}
            return 404, {}
        self.qdrant = FakeServer(handler)
        self.addCleanup(self.qdrant.close)
        with open(os.path.join(self.dir, "key"), "w") as f:
            f.write(KEY)

    def write_points(self, n: int, sparse: bool, extra_vectors: int = 0) -> None:
        with open(os.path.join(self.dir, "points.jsonl"), "w") as fp, open(os.path.join(self.dir, "vectors.f32"), "wb") as fv:
            for i in range(n):
                p = {"id": f"id-{i}", "dataset": "cuad", "file": "cuad/a.txt", "title": "A", "section": "",
                     "start": i * 10, "end": i * 10 + 10, "text": f"text {i}", "junk": "not sent"}
                if sparse:
                    p["sparse"] = {"indices": [1], "values": [0.5]}
                fp.write(json.dumps(p) + "\n")
            for i in range(n + extra_vectors):
                array("f", [float(i), 0.0, 0.0, 1.0]).tofile(fv)

    def run_loader(self, *extra: str) -> None:
        argv = ["qdrant_load.py", "--url", self.qdrant.url, "--key-file", os.path.join(self.dir, "key"),
                "--dir", self.dir, "--collection", "test", "--dim", "4", "--batch", "2", *extra]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            qdrant_load.main()

    def test_batches_and_config(self):
        self.write_points(5, sparse=True)
        self.run_loader("--shards", "6", "--replicas", "2")
        bodies = self.qdrant.json_bodies()
        paths = self.qdrant.paths()
        cfg = bodies[paths.index("/collections/test")]
        self.assertEqual((cfg["shard_number"], cfg["replication_factor"]), (6, 2))
        self.assertEqual(cfg["sparse_vectors"], {"bm25": {"modifier": "idf"}})
        self.assertNotIn("quantization_config", cfg)
        puts = [b for p, b in zip(paths, bodies) if p.startswith("/collections/test/points")]
        self.assertEqual([len(b["points"]) for b in puts], [2, 2, 1])  # порции по --batch
        first = puts[0]["points"][0]
        self.assertEqual(first["vector"]["dense"], [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(first["vector"]["bm25"], {"indices": [1], "values": [0.5]})
        self.assertNotIn("junk", first["payload"])
        self.assertEqual(puts[2]["points"][0]["vector"]["dense"], [4.0, 0.0, 0.0, 1.0])

    def test_quantization_and_existing_collection(self):
        self.write_points(3, sparse=False)
        self.run_loader("--quantization", "int8", "--on-disk")
        cfg = self.qdrant.json_bodies()[self.qdrant.paths().index("/collections/test")]
        self.assertEqual(cfg["quantization_config"]["scalar"]["type"], "int8")
        self.assertTrue(cfg["vectors"]["dense"]["on_disk"])
        self.assertNotIn("sparse_vectors", cfg)
        self.run_loader()  # коллекция уже есть: второй раз не создаётся, точки дописываются (upsert)
        self.assertEqual(self.qdrant.paths().count("/collections/test"), 1 + 2)  # PUT один раз, GET дважды

    def test_mismatched_files(self):
        self.write_points(3, sparse=False, extra_vectors=1)
        with self.assertRaises(ValueError):
            self.run_loader()


if __name__ == "__main__":
    unittest.main()
