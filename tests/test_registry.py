"""Реестр «коллекция → модель» (deploy/qdrant/registry.py), отпечаток набора (eval/fingerprint.py),
загрузка части набора (eval/qdrant_load.py --limit) и префикс вопроса при подготовке (eval/prepare.py).
Qdrant в памяти (tests/fakes.py MemoryQdrant), Ollama подставная, сети нет."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from array import array
from unittest import mock

from app import binding
from eval import fingerprint, prepare, qdrant_load
from tests.fakes import ADMIN_KEY, KEY, FakeServer, MemoryQdrant, model_vector
from tests.test_data_pipeline import write_dataset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("registry", os.path.join(ROOT, "deploy", "qdrant", "registry.py"))
registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(registry)

DIM = 16


def make_set(root: str, name: str, model: str, n: int = 6, strategy: str = "naive500_title") -> str:
    """Набор как у eval/prepare.py: points.jsonl, vectors.f32 (векторы «модели» по тексту куска) и meta.json."""
    d = os.path.join(root, name, strategy)
    os.makedirs(d)
    variant = strategy.partition("_")[2]
    with open(os.path.join(d, "points.jsonl"), "w", encoding="utf-8") as fp, open(os.path.join(d, "vectors.f32"), "wb") as fv:
        for i in range(n):
            p = {"id": f"00000000-0000-0000-0000-{i:012d}", "dataset": "cuad", "file": f"cuad/Doc_{i % 2}.txt",
                 "title": f"Doc {i % 2}", "section": "12. Governing Law." if i % 3 == 0 else "",
                 "start": i * 500, "end": i * 500 + 500,
                 "text": f"Chunk {i}. This Agreement is governed by the laws of state number {i}."}
            fp.write(json.dumps(p) + "\n")
            array("f", model_vector(model, prepare.index_text(p, variant), DIM)).tofile(fv)
    with open(os.path.join(root, name, "meta.json"), "w") as f:
        json.dump({"model": model, "dim": DIM, "strategies": {strategy: {"chunks": n}}}, f)
    return d


def load(url: str, key_file: str, src: str, collection: str, *extra: str) -> None:
    argv = ["qdrant_load.py", "--url", url, "--key-file", key_file, "--dir", src, "--collection", collection,
            "--dim", str(DIM), "--batch", "4", *extra]
    with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
        qdrant_load.main()


class FingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_entry_from_set(self):
        src = make_set(self.root, "stage1_arctic", "snowflake-arctic-embed2")
        e = fingerprint.entry(src)
        with open(os.path.join(src, "points.jsonl")) as f:
            first = json.loads(f.readline())
        self.assertEqual((e["model"], e["dim"], e["probe_id"]), ("snowflake-arctic-embed2", DIM, first["id"]))
        self.assertEqual(e["probe_text"], "Document: Doc 0\nSection: 12. Governing Law.\n\n" + first["text"])
        self.assertEqual(e["source"], "stage1_arctic/naive500_title")  # без абсолютных путей
        want = model_vector("snowflake-arctic-embed2", e["probe_text"], DIM)
        self.assertGreater(binding.cosine(e["vector"], want), 0.9999)  # вектор пробы из набора, по её тексту
        self.assertNotIn("planted", e)

    def test_planted_label(self):
        e = fingerprint.entry(make_set(self.root, "stage1_arctic", "snowflake-arctic-embed2"), "bge-m3")
        self.assertEqual((e["model"], e["planted"], e["meta_model"]), ("bge-m3", True, "snowflake-arctic-embed2"))
        same = fingerprint.entry(os.path.join(self.root, "stage1_arctic", "naive500_title"), "snowflake-arctic-embed2")
        self.assertNotIn("planted", same)  # та же модель, что в meta.json: это не ловушка

    def test_plain_strategy_has_no_title_prefix(self):
        e = fingerprint.entry(make_set(self.root, "stage1", "bge-m3", strategy="naive500"))
        self.assertTrue(e["probe_text"].startswith("Chunk 0."))

    def test_broken_sets(self):
        src = make_set(self.root, "s", "bge-m3")
        with open(os.path.join(src, "vectors.f32"), "ab") as f:
            array("f", [1.0]).tofile(f)
        with self.assertRaises(ValueError):
            fingerprint.entry(src)
        os.remove(os.path.join(self.root, "s", "meta.json"))
        with self.assertRaises(ValueError):
            fingerprint.entry(src)
        other = make_set(self.root, "t", "bge-m3")
        with open(os.path.join(self.root, "t", "meta.json"), "w") as f:
            json.dump({"model": "bge-m3", "dim": DIM, "strategies": {"naive500": {}}}, f)
        with self.assertRaises(ValueError):  # meta.json от другого прогона
            fingerprint.entry(other)

    def test_cli(self):
        src = make_set(self.root, "s", "bge-m3")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["fingerprint.py", src, "--model", "snowflake-arctic-embed2"]), \
                contextlib.redirect_stdout(out):
            fingerprint.main()
        self.assertEqual(json.loads(out.getvalue())["meta_model"], "bge-m3")


class RegistryToolTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.mem = MemoryQdrant()
        self.srv = FakeServer(self.mem)
        self.addCleanup(self.srv.close)
        self.key = os.path.join(self.root, "rw")
        with open(self.key, "w") as f:
            f.write(ADMIN_KEY)
        self.q = registry.Qdrant(self.srv.url, ADMIN_KEY)
        self.bge = make_set(self.root, "stage1", "bge-m3")
        self.arctic = make_set(self.root, "stage1_arctic", "snowflake-arctic-embed2")
        load(self.srv.url, self.key, self.bge, "contracts_rf2")
        load(self.srv.url, self.key, self.arctic, "contracts_rf2_arctic")

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.main(["--url", self.srv.url, "--key-file", self.key, *args])
        return code, out.getvalue(), err.getvalue()

    def entry_file(self, src: str, model: str | None = None) -> str:
        path = os.path.join(self.root, f"entry-{len(os.listdir(self.root))}.json")
        with open(path, "w") as f:
            json.dump(fingerprint.entry(src, model), f)
        return path

    def test_ids_match_the_service(self):
        for c in ("contracts_rf2", "contracts_rf2_arctic", "x"):
            self.assertEqual(registry.registry_id(c), binding.registry_id(c))

    def test_register_creates_registry_and_entry(self):
        code, out, _ = self.run_cli("register", "--entry", self.entry_file(self.bge), "--collection", "contracts_rf2")
        self.assertEqual(code, 0)
        self.assertIn("registered contracts_rf2: bge-m3", out)
        cfg = self.mem.cols["embedding_registry"]["config"]
        self.assertEqual((cfg["vectors"], cfg["shard_number"], cfg["replication_factor"]),
                         ({"fingerprint": {"size": DIM, "distance": "Cosine"}}, 1, 3))
        point = self.mem.cols["embedding_registry"]["points"][binding.registry_id("contracts_rf2")]
        self.assertEqual(point["payload"]["model"], "bge-m3")
        self.assertEqual(point["payload"]["probe_id"], "00000000-0000-0000-0000-000000000000")
        self.assertEqual(len(point["vector"]["fingerprint"]), DIM)
        self.assertEqual(self.run_cli("register", "--entry", self.entry_file(self.arctic),
                                      "--collection", "contracts_rf2_arctic")[0], 0)
        self.assertEqual(len(self.mem.cols["embedding_registry"]["points"]), 2)  # реестр не пересоздан

    def test_register_refuses_collection_from_another_set(self):
        code, _, err = self.run_cli("register", "--entry", self.entry_file(self.arctic), "--collection", "contracts_rf2")
        self.assertEqual(code, 1)
        self.assertIn("holds other vectors than stage1_arctic/naive500_title", err)
        other = make_set(self.root, "other", "bge-m3", n=2)
        with open(os.path.join(other, "points.jsonl")) as f:
            lines = [json.loads(x) for x in f]
        lines[0]["id"] = "11111111-0000-0000-0000-000000000000"
        with open(os.path.join(other, "points.jsonl"), "w") as f:
            f.writelines(json.dumps(x) + "\n" for x in lines)
        code, _, err = self.run_cli("register", "--entry", self.entry_file(other), "--collection", "contracts_rf2")
        self.assertIn("has no point 11111111", err)
        code, _, err = self.run_cli("register", "--entry", self.entry_file(self.bge), "--collection", "nope")
        self.assertIn("does not exist", err)
        self.assertNotIn("embedding_registry", self.mem.cols)  # ни одной записи не появилось

    def test_alias_switch_is_one_request(self):
        self.run_cli("register", "--entry", self.entry_file(self.bge), "--collection", "contracts_rf2")
        self.run_cli("register", "--entry", self.entry_file(self.arctic), "--collection", "contracts_rf2_arctic")
        code, out, _ = self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2")
        self.assertEqual((code, self.mem.aliases), (0, {"contracts": "contracts_rf2"}))
        self.assertEqual(self.mem.alias_requests[-1], [{"create_alias": {"collection_name": "contracts_rf2",
                                                                         "alias_name": "contracts"}}])
        code, out, _ = self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2_arctic")
        self.assertEqual(code, 0)
        self.assertEqual(self.mem.alias_requests[-1], [
            {"delete_alias": {"alias_name": "contracts"}},
            {"create_alias": {"collection_name": "contracts_rf2_arctic", "alias_name": "contracts"}}])
        self.assertEqual(len(self.mem.alias_requests), 2)
        self.assertIn("alias contracts: contracts_rf2 (bge-m3) -> contracts_rf2_arctic (snowflake-arctic-embed2)", out)
        code, out, _ = self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2_arctic")
        self.assertIn("already points", out)
        self.assertEqual(len(self.mem.alias_requests), 2)  # без лишнего запроса

    def test_alias_checks(self):
        self.assertEqual(self.run_cli("alias", "--name", "contracts", "--to", "nope")[0], 1)
        code, out, _ = self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2")
        self.assertIn("warning: contracts_rf2 is not in the registry", out)  # предупреждает, но переключает
        self.assertEqual(self.mem.aliases, {"contracts": "contracts_rf2"})

    def test_drop_only_planted_and_unused(self):
        load(self.srv.url, self.key, self.arctic, "contracts_mislabeled", "--limit", "2")
        self.assertEqual(len(self.mem.cols["contracts_mislabeled"]["points"]), 2)
        self.run_cli("register", "--entry", self.entry_file(self.bge), "--collection", "contracts_rf2")
        code, out, _ = self.run_cli("register", "--entry", self.entry_file(self.arctic, "bge-m3"),
                                    "--collection", "contracts_mislabeled")
        self.assertIn("planted: the vectors were made by snowflake-arctic-embed2", out)
        self.run_cli("alias", "--name", "contracts", "--to", "contracts_mislabeled")
        code, _, err = self.run_cli("drop", "--collection", "contracts_mislabeled")
        self.assertEqual(code, 1)
        self.assertIn("used by alias contracts", err)
        self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2")
        code, _, err = self.run_cli("drop", "--collection", "contracts_rf2")
        self.assertIn("used by alias contracts", err)
        for name in ("contracts_rf2_arctic", "embedding_registry"):  # не подложенные: не удаляются
            code, _, err = self.run_cli("drop", "--collection", name)
            self.assertIn("not a planted collection", err)
            self.assertIn(name, self.mem.cols)
        code, out, _ = self.run_cli("drop", "--collection", "contracts_mislabeled")
        self.assertEqual(code, 0)
        self.assertNotIn("contracts_mislabeled", self.mem.cols)
        self.assertNotIn(binding.registry_id("contracts_mislabeled"), self.mem.cols["embedding_registry"]["points"])

    def test_show(self):
        code, out, _ = self.run_cli("show")
        self.assertIn("no aliases", out)
        self.assertIn("registry embedding_registry: not created yet", out)
        self.run_cli("register", "--entry", self.entry_file(self.bge), "--collection", "contracts_rf2")
        self.run_cli("alias", "--name", "contracts", "--to", "contracts_rf2")
        code, out, _ = self.run_cli("show")
        self.assertIn("alias contracts -> contracts_rf2 (bge-m3)", out)
        self.assertRegex(out, r"contracts_rf2_arctic\s+not registered")
        code, out, _ = self.run_cli("show", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["aliases"], doc["registry"]["contracts_rf2"]["model"]), ({"contracts": "contracts_rf2"}, "bge-m3"))

    def test_read_only_key_cannot_write(self):
        ro = registry.Qdrant(self.srv.url, KEY)
        with self.assertRaises(registry.Failed):
            ro.call("POST", "/collections/aliases", {"actions": []})


class LoaderLimitTest(unittest.TestCase):
    def test_limit_takes_first_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_set(tmp, "s", "bge-m3", n=7)
            batches = list(qdrant_load.batches(src, DIM, 2, limit=3))
            self.assertEqual([len(b) for b in batches], [2, 1])
            self.assertEqual(batches[0][0][0]["id"], "00000000-0000-0000-0000-000000000000")
            self.assertEqual(sum(len(b) for b in qdrant_load.batches(src, DIM, 4)), 7)


def embed_handler():
    """Ollama /api/embed: вектор из длины текста и первой буквы, как в test_data_pipeline."""
    def handler(method, path, headers, body):
        texts = json.loads(body)["input"]
        return 200, {"embeddings": [[float(len(t)), float(ord(t[0])), 1.0, 0.0] for t in texts]}
    return handler


class PrepareQueryPrefixTest(unittest.TestCase):
    """Вопросы с префиксом модели, куски без: тот же текст, что увидит модель в сервисе поиска."""

    def run_prepare(self, tmp: str, model: str) -> tuple[FakeServer, str]:
        ollama = FakeServer(embed_handler())
        self.addCleanup(ollama.close)
        out = os.path.join(tmp, f"out-{model}")
        argv = ["prepare.py", "--full", "--data", os.path.join(tmp, "data"), "--out", out, "--model", model,
                "--ollama", ollama.url, "--embed-cache", os.path.join(tmp, "emb")]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            prepare.main()
        return ollama, out

    def test_arctic_queries_get_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(os.path.join(tmp, "data"))
            ollama, out = self.run_prepare(tmp, "snowflake-arctic-embed2")
            sent = [t for b in ollama.json_bodies() for t in b["input"]]
            questions = [t for t in sent if "Question" in t]
            self.assertEqual(len(questions), 8)
            self.assertTrue(all(t.startswith("query: Question ") for t in questions))
            self.assertTrue(all(t.startswith("Document: ") for t in sent if "Question" not in t))  # куски без префикса
            vec = array("f")
            with open(os.path.join(out, "query_vectors.f32"), "rb") as f:
                vec.fromfile(f, 4)
            self.assertEqual(vec[1], float(ord("q")))  # вектор вопроса посчитан по тексту с префиксом
            with open(os.path.join(out, "queries.jsonl")) as f:
                self.assertTrue(json.loads(f.readline())["query"].startswith("Question "))  # сам вопрос без префикса
            with open(os.path.join(out, "meta.json")) as f:
                self.assertEqual(json.load(f)["query_prefix"], "query: ")

    def test_bge_queries_unchanged_and_cache_by_actual_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_dataset(os.path.join(tmp, "data"))
            ollama, out = self.run_prepare(tmp, "bge-m3")
            questions = [t for b in ollama.json_bodies() for t in b["input"] if "Question" in t]
            self.assertTrue(all(t.startswith("Question ") for t in questions))
            with open(os.path.join(out, "meta.json")) as f:
                self.assertEqual(json.load(f)["query_prefix"], "")
            ollama2, _ = self.run_prepare(tmp, "bge-m3")
            self.assertEqual(ollama2.requests, [])  # всё из кэша: ключ по фактическому тексту


if __name__ == "__main__":
    unittest.main()
