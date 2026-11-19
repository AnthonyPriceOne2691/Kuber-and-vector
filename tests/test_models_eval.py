"""Модели векторов, реранкер и HyDE (eval/models_eval.py): префиксы, этап модели, разбор балла, пересортировка,
кэш ответов LLM и прогон stage → models → rerank → hyde → report на крошечном корпусе с подставной Ollama."""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from eval import legalbench as lb
from eval import models_eval as M
from eval import routing_eval as R
from eval.prepare import index_text
from tests.fakes import FakeServer, model_ollama_handler, model_vector
from tests.test_routing_eval import DIM, build_stage, corpus_docs, corpus_queries, write_f32


def chat_reply(req: dict) -> str:
    """Реранкер (есть схема ответа): балл от длины промпта; HyDE: короткий «пункт договора»."""
    user = req["messages"][-1]["content"]
    if "format" in req:
        return json.dumps({"score": len(user) % 11})
    return "<think>\n\n</think>\n\nThis Agreement shall be governed by the laws of Delaware."


def fake_ollama(dim: int = DIM):
    embed = model_ollama_handler(dim=dim)

    def handler(method, path, headers, body):
        if method == "POST" and path == "/api/chat":
            req = json.loads(body)
            return 200, {"model": req["model"], "message": {"role": "assistant", "content": chat_reply(req)},
                         "done": True, "prompt_eval_count": 50, "prompt_eval_duration": 1_000_000,
                         "eval_count": 7, "eval_duration": 2_000_000, "load_duration": 1000}
        if method == "POST" and path == "/api/generate":
            return 200, {"done": True}
        return embed(method, path, headers, body)
    return handler


def cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


class PrefixTest(unittest.TestCase):
    def test_prefixes_by_model(self):
        self.assertEqual(M.prefixes("bge-m3"), ("", ""))
        self.assertEqual(M.prefixes("snowflake-arctic-embed2:latest"), ("query: ", ""))
        for name in ("nomic-embed-text", "nomic-embed-text:latest"):
            self.assertEqual(M.prefixes(name), ("search_query: ", "search_document: "))

    def test_measure_only_models_stay_out_of_service_list(self):
        """nomic только для замера: в списке сервиса его нет, иначе его должен пускать фильтр стенда."""
        from app import embed_models
        self.assertFalse(set(M.MEASURE_PREFIX) & set(embed_models.QUERY_PREFIX))


class ScoreAndOrderTest(unittest.TestCase):
    def test_parse_score(self):
        self.assertEqual(M.parse_score('{"score": 7}'), 7)
        self.assertEqual(M.parse_score('<think>\nhm\n</think>\n{"score":3}'), 3)
        self.assertEqual(M.parse_score('{"score": 12}'), 10)
        self.assertEqual(M.parse_score('{"score": -2}'), 0)
        self.assertEqual(M.parse_score('{"score": 6.6}'), 7)
        for bad in ("", "seven", '{"score": "7"}', '{"score": true}', '{"grade": 7}', "[7]", '{"score": 7'):
            self.assertIsNone(M.parse_score(bad), bad)

    def test_rerank_keeps_tail_ties_and_unscored(self):
        ranked = [5, 6, 7, 8, 9]
        # 6 и 8 с равным баллом идут в порядке маршрута; 7 без балла остаётся на третьем месте; 9 за N
        self.assertEqual(M.rerank(ranked, {5: 2, 6: 9, 7: None, 8: 9, 9: 10}, 4), [6, 8, 7, 5, 9])
        self.assertEqual(M.rerank(ranked, {}, 3), ranked)
        self.assertEqual(M.rerank(ranked, {c: 5 for c in ranked}, 5), ranked)  # все равны: порядок маршрута

    def test_oracle_and_sample_order(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        build_stage(Path(tmp.name), {"cuad/A.txt": 4}, [], 1)
        corpus = R.Corpus(Path(tmp.name) / "naive500_title", DIM)
        gt = [("cuad/A.txt", 1100, 1600)]  # 400 знаков в куске 2 и 100 в куске 3
        self.assertEqual(M.oracle(corpus, [0, 3, 1, 2], gt, 3), [3, 0, 1, 2])  # 2 за N: не поднимается
        self.assertEqual(M.oracle(corpus, [0, 3, 1, 2], gt, 4), [2, 3, 0, 1])
        acc = {"gold": [], "other": [], "llm": [0.0, 0], "route": [0.0, 0]}
        M.discriminate(acc, corpus, [0, 2, 1, 3], {0: 5, 2: 9, 1: 5, 3: 5}, gt)
        s = M.discrimination_summary(acc)
        # пары (эталонный, чужой): (2,0) (2,1) (3,0) (3,1); LLM выше в двух, равны в двух: 75 %; маршрут выше только 2 над 1
        self.assertEqual((s["llm_pairs"], s["route_pairs"], s["pairs"]), (75.0, 25.0, 4))
        self.assertEqual((s["gold_mean"], s["other_mean"], s["gold_ge7"], s["other_ge7"]), (7.0, 5.0, 50.0, 0.0))
        qs = [{"i": i, "dataset": lb.DATASETS[i % 4]} for i in range(40)]
        half = R.halves(qs)
        order = M.sample_order(qs, half, "B")
        self.assertEqual(order, M.sample_order(qs, half, "B"))
        self.assertEqual(sorted(q["i"] for q in order), sorted(i for i, h in half.items() if h == "B"))
        self.assertEqual({q["dataset"] for q in order[:4]}, set(lb.DATASETS))  # наборы по очереди


class LLMTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer(fake_ollama())
        self.addCleanup(self.srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "llm.jsonl"

    def test_body_cache_and_unload(self):
        llm = M.LLM(self.srv.url, "qwen3:8b", self.cache)
        body = M.rerank_body("qwen3:8b", "Consider the NDA; Is there a cap?", "Document: NDA\n\nThe cap is $1.")
        r = llm.ask(body)
        self.assertEqual(M.parse_score(r["content"]), len(body["messages"][-1]["content"]) % 11)
        sent = self.srv.json_bodies()[0]
        self.assertIs(sent["think"], False)
        self.assertEqual(sent["options"]["temperature"], 0)
        self.assertEqual(sent["options"]["num_ctx"], M.NUM_CTX)
        self.assertEqual(sent["format"], M.RERANK_SCHEMA)
        self.assertTrue(sent["messages"][-1]["content"].startswith("Question: Consider the NDA"))  # вопрос до куска
        self.assertEqual(llm.ask(body)["content"], r["content"])
        self.assertEqual(self.srv.paths().count("/api/chat"), 1)  # второй раз из кэша
        with open(self.cache, "a", encoding="utf-8") as f:
            f.write('{"key": "оборванная строка')
        again = M.LLM(self.srv.url, "qwen3:8b", self.cache)
        self.assertEqual(again.ask(body)["content"], r["content"])
        self.assertEqual(self.srv.paths().count("/api/chat"), 1)  # кэш с диска, оборванная строка пропущена
        again.unload()
        self.assertEqual(self.srv.json_bodies()[-1], {"model": "qwen3:8b", "keep_alive": 0})

    def test_hyde_body_has_no_schema(self):
        b = M.hyde_body("qwen3:8b", "cuad", "Is there a non-compete?")
        self.assertNotIn("format", b)
        self.assertIn("a commercial contract", b["messages"][-1]["content"])
        self.assertEqual(M.strip_think(chat_reply(b)), "This Agreement shall be governed by the laws of Delaware.")


class MixScoresTest(unittest.TestCase):
    def test_mix(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        build_stage(Path(tmp.name), {"cuad/A.txt": 2, "cuad/B.txt": 2}, [], 2)
        corpus = R.Corpus(Path(tmp.name) / "naive500_title", DIM)
        s = array("f", [0.1, 0.2, 0.3, 0.4])
        f = array("f", [0.9, 0.9, 0.9, 0.9])
        base = R.Scores(corpus, None, arrays={("s", "ask"): s, ("f", 0): f})
        mix = M.MixScores(base, {"ask": array("f", [1.0, 0.0, 0.5, 0.5])}, 0.5)
        for got, want in zip(mix.doc(("s", "ask"), 1), [0.4, 0.45]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertEqual(list(mix.doc(("f", 0), 0)), list(base.doc(("f", 0), 0)))  # весь вопрос без примеси
        self.assertEqual(list(M.MixScores(base, {}, 0.0).doc(("s", "ask"), 0)), list(base.doc(("s", "ask"), 0)))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name)
        docs = corpus_docs(2)
        build_stage(self.t / "raw/stage1", docs, corpus_queries(docs, 3), 5)  # куски и вопросы, векторы случайные
        self.srv = FakeServer(fake_ollama())
        self.addCleanup(self.srv.close)

    def run_step(self, step: str, *extra: str) -> None:
        argv = [step, "--embed-cache", str(self.t / "emb"), "--ollama", self.srv.url, "--work", str(self.t / "work"),
                "--md", str(self.t / "models.md"), "--embed-pause", "0", *extra]
        with mock.patch("builtins.print"):
            M.main(argv)

    def stage(self, model: str, out: str, *extra: str) -> Path:
        self.run_step("stage", "--model", model, "--cache", str(self.t / "raw"), "--out", str(self.t / out), *extra)
        return self.t / out

    def write_ready_chunks(self, model: str) -> Path:
        """Готовые векторы кусков (как cache/stage1_arctic): посчитаны «моделью» по тексту без префикса."""
        src = self.t / f"ready_{model}"
        (src / "naive500_title").mkdir(parents=True)
        lines = (self.t / "raw/stage1/naive500_title/points.jsonl").read_text().splitlines()
        (src / "naive500_title/points.jsonl").write_text("\n".join(lines) + "\n")
        write_f32(src / "naive500_title/vectors.f32",
                  [model_vector(model, index_text(json.loads(x), "title"), DIM) for x in lines])
        (src / "meta.json").write_text(json.dumps({"strategies": {"naive500_title": {
            "chunks_per_second": 23.2, "embed_seconds": 10.0, "new_vectors": len(lines)}}}))
        return src

    def test_stage_vectors_and_prefixes(self):
        out = self.stage("nomic-embed-text", "nomic")
        meta = json.loads((out / "meta.json").read_text())
        self.assertEqual((meta["query_prefix"], meta["doc_prefix"], meta["dim"]), ("search_query: ", "search_document: ", DIM))
        p0 = json.loads((out / "naive500_title/points.jsonl").read_text().splitlines()[0])
        q0 = json.loads((out / "queries.jsonl").read_text().splitlines()[0])
        row = R.read_vectors(out / "naive500_title/vectors.f32", DIM, 0, 1)[0]
        self.assertAlmostEqual(cos(row, model_vector("nomic-embed-text", "search_document: " + index_text(p0, "title"), DIM)), 1, 5)
        qrow = R.read_vectors(out / "query_vectors.f32", DIM, 0, 1)[0]
        self.assertAlmostEqual(cos(qrow, model_vector("nomic-embed-text", "search_query: " + q0["query"], DIM)), 1, 5)

    def test_ready_chunks_are_probed(self):
        out = self.stage("snowflake-arctic-embed2", "arc", "--chunks", str(self.write_ready_chunks("snowflake-arctic-embed2")))
        meta = json.loads((out / "meta.json").read_text())
        self.assertEqual(meta["chunk_probe_cos"], 1.0)
        self.assertTrue(os.path.islink(out / "naive500_title/vectors.f32"))
        self.assertEqual(M.chunk_speed(meta)["source"], "prepare")
        with self.assertRaises(SystemExit):  # куски другой модели: проба не сходится
            self.stage("snowflake-arctic-embed2", "arc2", "--chunks", str(self.write_ready_chunks("bge-m3")))

    def test_models_rerank_hyde_report(self):
        bge = self.stage("bge-m3", "cache/stage1")
        nomic = self.stage("nomic-embed-text", "nomic")
        arc = self.stage("snowflake-arctic-embed2", "arc", "--chunks", str(self.write_ready_chunks("snowflake-arctic-embed2")))
        self.run_step("models", "--stage", f"bge-m3={bge}", "--stage", f"nomic={nomic}", "--stage", f"arctic={arc}")
        mo = json.loads((self.t / "work/models.json").read_text())
        self.assertEqual(mo["order"], ["bge-m3", "nomic", "arctic"])
        n = len(mo["queries"])
        for st in mo["stages"].values():
            self.assertEqual(st["embed_short"]["probe_cos"], 1.0)  # префиксы частей вопроса те же, что у вопроса
            for v in M.VARIANTS:
                self.assertEqual(len(st["variants"][v]["R"]), n)
        self.assertEqual(mo["stages"]["nomic"]["query_prefix"], "search_query: ")

        self.run_step("rerank", "--cache", str(self.t / "cache"), "--n", "2,4")
        rr = json.loads((self.t / "work/rerank_qwen3_8b.json").read_text())
        b_total = sum(1 for q in mo["queries"] if q["half"] == "B")
        self.assertEqual((rr["sample"], rr["b_total"]), (b_total, b_total))
        self.assertEqual(set(rr["variants"]), {"маршрут", "реранкер N=2", "реранкер N=4", "потолок N=2", "потолок N=4"})
        self.assertNotIn("нет", rr["scores"])
        calls = self.srv.paths().count("/api/chat")
        self.assertTrue(0 < calls <= 4 * b_total)  # у кусков одного документа тут одинаковый текст: часть из кэша
        self.assertEqual(len((self.t / "work/llm_cache.jsonl").read_text().splitlines()), calls)
        self.run_step("rerank", "--cache", str(self.t / "cache"), "--n", "2,4", "--questions", "4")
        self.assertEqual(self.srv.paths().count("/api/chat"), calls)  # всё из кэша
        self.assertEqual(self.srv.paths()[-1], "/api/generate")  # модель выгружена

        self.run_step("hyde", "--cache", str(self.t / "cache"))
        hy = json.loads((self.t / "work/hyde_qwen3_8b.json").read_text())
        self.assertEqual([b for b, _ in hy["grid"]], list(M.BETAS))
        self.assertIn(hy["beta"], M.BETAS)
        self.assertEqual(set(hy["variants"]), {"маршрут", "HyDE, подбор на A", "HyDE вместо вопроса"})
        self.assertNotIn("<think>", hy["examples"][0]["draft"])

        self.run_step("report")
        md = (self.t / "models.md").read_text()
        for part in ("## Коротко", "## Модели векторов", "### Половина B", "## LLM как реранкер", "## HyDE"):
            self.assertIn(part, md)


if __name__ == "__main__":
    unittest.main()
