"""Маршрутизация и деление вопроса (eval/routing_eval.py): метрика, ранжирование, бутстреп и прогон
mini → full → report на крошечном корпусе с подставной Ollama. Без данных и без настоящей Ollama."""
from __future__ import annotations

import json
import math
import os
import random
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from eval import legalbench as lb
from eval import routing_eval as R
from tests.fakes import FakeServer, model_ollama_handler

DIM = 8


def unit(rng: random.Random) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def write_f32(path: Path, rows: list[list[float]]) -> None:
    with open(path, "wb") as f:
        for r in rows:
            array("f", r).tofile(f)


def build_stage(stage: Path, docs: dict[str, int], queries: list[tuple], seed: int) -> None:
    """Куски по 500 знаков, случайные единичные векторы; вопрос (набор, текст, [[файл, начало, конец]])."""
    rng = random.Random(seed)
    idx = stage / "naive500_title"
    idx.mkdir(parents=True)
    points = [{"id": f"{f}:{j}", "dataset": f.split("/")[0], "file": f, "title": Path(f).stem, "section": "",
               "start": 500 * j, "end": 500 * (j + 1), "text": "x" * 500} for f, n in docs.items() for j in range(n)]
    with open(idx / "points.jsonl", "w") as f:
        f.writelines(json.dumps(p) + "\n" for p in points)
    write_f32(idx / "vectors.f32", [unit(rng) for _ in points])
    with open(stage / "queries.jsonl", "w") as f:
        f.writelines(json.dumps({"dataset": d, "query": q, "sparse": {}, "snippets": s}) + "\n" for d, q, s in queries)
    write_f32(stage / "query_vectors.f32", [unit(rng) for _ in queries])
    (stage / "meta.json").write_text(json.dumps({"dim": DIM, "model": "bge-m3"}))


def corpus_docs(n_per_ds: int) -> dict[str, int]:
    return {f"{d}/{d.upper()}_{j}.txt": 3 + (j % 3) for d in lb.DATASETS for j in range(n_per_ds)}


def corpus_queries(docs: dict[str, int], per_doc: int) -> list[tuple]:
    out = []
    for f, n in docs.items():
        d = f.split("/")[0]
        for j in range(per_doc):
            c = j % n
            out.append((d, f"Consider the agreement {Path(f).stem}; question {j} about clause {c}?",
                        [[f, 500 * c + 100, 500 * c + 700]]))
    return out


class SplitAndMetricTest(unittest.TestCase):
    def test_split_query(self):
        self.assertEqual(R.split_query("Consider the NDA between A and B; Is there a cap?"),
                         ("Consider the NDA between A and B", "Is there a cap?"))
        self.assertEqual(R.split_query("A; b; c"), ("A", "b; c"))  # только по первой «;»
        self.assertEqual(R.split_query("Who can see my data?"), ("Who can see my data?",) * 2)
        self.assertEqual(R.split_query("; only a question"), ("; only a question",) * 2)

    def test_pr_at_k_matches_authors_metric(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        docs = {"cuad/A.txt": 6, "cuad/B.txt": 4}
        build_stage(Path(tmp.name), docs, [], 1)
        corpus = R.Corpus(Path(tmp.name) / "naive500_title", DIM)
        rng = random.Random(3)
        for _ in range(50):
            gt = [("cuad/A.txt", 120, 980), ("cuad/A.txt", 2400, 2450)] if rng.random() < 0.5 else [("cuad/B.txt", 0, 1700)]
            ranked = rng.sample(range(corpus.n), rng.randint(0, corpus.n))
            rec, prec = R.pr_at_k(corpus, ranked, gt)
            found = [(corpus.files[corpus.doc_of[c]], corpus.start[c], corpus.end[c]) for c in ranked]
            for j, k in enumerate(lb.K_VALUES):
                p, r = lb.precision_recall(found[:k], gt)
                self.assertAlmostEqual(rec[j], r)
                self.assertAlmostEqual(prec[j], p)

    def test_halves_are_fixed_and_balanced(self):
        qs = [{"i": i, "dataset": lb.DATASETS[i % 4]} for i in range(41)]
        h1, h2 = R.halves(qs), R.halves(qs)
        self.assertEqual(h1, h2)
        for d in lb.DATASETS:
            ids = [q["i"] for q in qs if q["dataset"] == d]
            self.assertEqual(sum(1 for i in ids if h1[i] == "A"), len(ids) // 2)

    def test_boot_point_and_paired_zero(self):
        a = {"cuad": [1.0, 2.0, 3.0], "maud": [10.0, 10.0]}
        zero = {"cuad": [0.0] * 3, "maud": [0.0] * 2}
        bt = R.boot({"a": a, "d": zero}, b=200)
        self.assertAlmostEqual(bt["a"][0], (2.0 + 10.0) / 2)  # наборы с равными весами
        self.assertLessEqual(bt["a"][1], bt["a"][0])
        self.assertGreaterEqual(bt["a"][2], bt["a"][0])
        self.assertEqual(bt["d"], (0.0, 0.0, 0.0))


class RankTest(unittest.TestCase):
    """Три документа по два-три куска; баллы задаются руками через полные массивы."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        build_stage(Path(tmp.name), {"cuad/A.txt": 3, "cuad/B.txt": 2, "cuad/C.txt": 2}, [], 2)
        self.corpus = R.Corpus(Path(tmp.name) / "naive500_title", DIM)
        # куски: A 0,1,2; B 3,4; C 5,6
        f = array("f", [0.50, 0.40, 0.30, 0.90, 0.10, 0.20, 0.15])  # весь вопрос
        s = array("f", [0.10, 0.70, 0.20, 0.30, 0.80, 0.60, 0.05])  # сам вопрос
        p = array("f", [0.90, 0.85, 0.80, 0.30, 0.20, 0.10, 0.10])  # ссылка
        self.sc = R.Scores(self.corpus, None, arrays={("f", 0): f, ("s", "ask"): s, ("p", "ref"): p})

        class FixedRouter:
            def scores(self, q, name):
                return [0.9, 0.5, 0.4]  # A, потом B, потом C
        self.ctx = R.QueryCtx(self.corpus, self.sc, FixedRouter(), "x").set({"i": 0, "ask": "ask", "ref": "ref"})

    def test_hard_top1_fills_from_next_documents(self):
        got = R.rank(R.route_cfg(1, 0.0, 1.0), self.ctx, k=5)
        self.assertEqual(got, [0, 1, 2, 3, 4])  # A по баллу, затем B по баллу

    def test_hard_top2_merges_by_chunk_score(self):
        self.assertEqual(R.rank(R.route_cfg(2, 0.0, 1.0), self.ctx, k=3), [3, 0, 1])

    def test_doc_major_and_soft(self):
        self.assertEqual(R.rank(R.route_cfg(3, R.INF, 1.0), self.ctx, k=4), [0, 1, 2, 3])
        # λ = 0.25: лучший кусок B (0.9 + 0.125) обгоняет документ A (0.5 + 0.225), слабый кусок B нет
        soft = R.rank(R.route_cfg(3, 0.25, 1.0), self.ctx, k=7)
        self.assertEqual(soft, [3, 0, 1, 2, 5, 6, 4])

    def test_question_part_ranks_inside_document(self):
        self.assertEqual(R.rank(R.route_cfg(1, 0.0, 0.0), self.ctx, k=3), [1, 2, 0])  # A по «самому вопросу»
        mix = R.rank(R.route_cfg(1, 0.0, 0.5), self.ctx, k=1)
        self.assertEqual(mix, [1])  # 0.5·0.4 + 0.5·0.7 у куска 1 выше, чем 0.5·0.5 + 0.5·0.1 у куска 0

    def test_neighbors_smoothing(self):
        self.assertEqual(R.smooth([0.1, 0.5, 0.2], 0.5), [0.1 + 0.25, 0.5 + 0.1, 0.2 + 0.25])
        self.assertEqual(R.smooth([0.5], 0.5), [0.75])  # без соседей: сам себе сосед

    def test_fusion(self):
        top = lambda cfg: R.rank(cfg, self.ctx, k=3)  # noqa: E731
        self.assertEqual(top({"family": "fuse", "how": "wsum", "w": 1.0}), [0, 1, 2])  # только ссылка
        self.assertEqual(top({"family": "fuse", "how": "wsum", "w": 0.0}), [4, 1, 5])  # только сам вопрос
        self.assertEqual(top({"family": "fuse", "how": "max"}), [0, 1, 2])
        rrf = top({"family": "fuse", "how": "rrf"})
        self.assertEqual(rrf[0], 1)  # второй у ссылки и второй у вопроса: лучшая сумма мест

    def test_aggregation(self):
        # кандидаты по «всему вопросу»: max у B 0.9, у A 0.5; сумма top-3 у A 1.2, у B 1.0
        self.assertEqual(R.rank({"family": "agg", "how": "max", "lam": R.INF}, self.ctx, k=3), [3, 4, 0])
        self.assertEqual(R.rank({"family": "agg", "how": "sum3", "lam": R.INF}, self.ctx, k=3), [0, 1, 2])
        self.assertEqual(R.rank({"family": "base"}, self.ctx, k=3), [3, 0, 1])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name)
        mini_docs = corpus_docs(2)
        full_docs = dict(mini_docs, **{f"{d}/{d.upper()}_x{j}.txt": 4 for d in lb.DATASETS for j in range(2)})
        mini_q = corpus_queries(mini_docs, 3)
        full_q = mini_q + corpus_queries({f: n for f, n in full_docs.items() if f not in mini_docs}, 3)
        build_stage(self.t / "cache/stage1", mini_docs, mini_q, 5)
        build_stage(self.t / "cache/full", full_docs, full_q, 6)
        self.write_qdrant_base()
        self.ollama = FakeServer(model_ollama_handler(dim=DIM))
        self.addCleanup(self.ollama.close)

    def write_qdrant_base(self) -> None:
        """«Прогон стенда» для полного набора: тот же точный поиск, что у нас (сверка должна сойтись)."""
        stage = self.t / "cache/full"
        corpus = R.Corpus(stage / "naive500_title", DIM)
        queries, qv = R.load_queries(stage, DIM)
        per = {d: {str(k): [] for k in lb.K_VALUES} for d in lb.DATASETS}
        for q in queries:
            ranked = R.pipeline_base(corpus, qv[q["i"] * DIM:(q["i"] + 1) * DIM].tolist())
            rec, prec = R.pr_at_k(corpus, ranked, q["gt"])
            for j, k in enumerate(lb.K_VALUES):
                per[q["dataset"]][str(k)].append([prec[j], rec[j]])
        (stage / "results").mkdir()
        (stage / "results/result_full.json").write_text(json.dumps({"per_query": per}))

    def run_step(self, step: str) -> None:
        t = self.t
        argv = [step, "--cache", str(t / "cache"), "--embed-cache", str(t / "emb"), "--ollama", self.ollama.url,
                "--work", str(t / "work"), "--md", str(t / "routing.md"), "--procs", "1", "--sample", "4",
                "--time-n", "3", "--time-base-n", "2"]
        with mock.patch("builtins.print"):
            R.main(argv)

    def test_mini_full_report(self):
        self.run_step("mini")
        mini = json.loads((self.t / "work/mini.json").read_text())
        self.assertIn(mini["router"], R.ROUTERS)
        self.assertIn(mini["best"], mini["show"])
        self.assertEqual(mini["meta"]["no_semicolon"], {d: 0 for d in lb.DATASETS})
        self.assertEqual(mini["timing"]["route_same_as_cached"], mini["timing"]["n_route"])
        n = len(mini["queries"])
        for v in mini["variants"].values():
            self.assertEqual((len(v["R"]), len(v["R"][0])), (n, len(lb.K_VALUES)))
        embeds = self.ollama.paths().count("/api/embed")
        self.assertGreater(embeds, 0)

        self.run_step("full")
        full = json.loads((self.t / "work/full.json").read_text())
        self.assertEqual(full["meta"]["tuned_found"], sum(1 for q in mini["queries"] if q["half"] == "A"))
        self.assertEqual(len(full["elig"]), full["meta"]["queries"] - full["meta"]["tuned_found"])
        self.assertEqual(full["base_check"]["same_recall_all_k"], full["base_check"]["queries"])
        self.assertEqual(full["timing"]["route_same_as_cached"], full["timing"]["n_route"])
        self.assertIn(full["tune"]["router"], R.ROUTERS)  # справочный подбор ширины на половине A полного
        fb = full["tune"]["fullB"]
        self.assertTrue(fb and set(fb) <= set(full["elig"]))
        self.assertEqual(len(full["variants"]["подобран на полном A"]["R"]), len(fb))

        self.run_step("report")
        md = (self.t / "routing.md").read_text()
        for part in ("## Мини-набор, половина B", "## Полный набор", "## Точность маршрутизации", "## Время на вопрос"):
            self.assertIn(part, md)

        before = self.ollama.paths().count("/api/embed")
        self.run_step("mini")  # векторы частей уже в кэше: к Ollama больше не ходим
        self.assertEqual(self.ollama.paths().count("/api/embed"), before)
        self.assertTrue(os.path.exists(self.t / "work/mini.json"))


if __name__ == "__main__":
    unittest.main()
