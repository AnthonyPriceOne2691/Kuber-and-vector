"""LegalBench-RAG: загрузка, мини-выборка и метрики ровно как у авторов.

Сверено с кодом авторов (github.com/zeroentropy-cc/legalbenchrag, ветка master, 18.11.2026):
- benchmark.py: MAX_TESTS_PER_BENCHMARK = 194, SORT_BY_DOCUMENT = True; вопросы сортируются
  по ключу (random.seed(путь первого фрагмента), random.random())[1], берутся первые 194;
  корпус сужается до документов, на которые ссылаются выбранные вопросы;
- run_benchmark.py, QAResult: precision и recall посимвольные, пересечения считаются попарно
  (найденный кусок × эталонный фрагмент в том же файле), без склейки; при нулевой длине 0;
- итог по набору это среднее по вопросам, ALL это среднее по наборам с равными весами.
"""
from __future__ import annotations

import json
import os
import random

DATASETS = ("privacy_qa", "contractnli", "maud", "cuad")
MAX_TESTS = 194
K_VALUES = (1, 2, 4, 8, 16, 32, 64)

# Опубликованные в статье размеры мини-корпуса (arXiv 2408.10343, табл. 3) для самопроверки
PAPER_MINI_CORPUS = {
    "contractnli": (18, 184_267), "maud": (18, 7_109_200),
    "cuad": (29, 1_211_773), "privacy_qa": (7, 176_864),
}


def load_tests(root: str, name: str) -> list[dict]:
    with open(os.path.join(root, "benchmarks", f"{name}.json"), encoding="utf-8") as f:
        data = json.load(f)
    return data["tests"] if isinstance(data, dict) else data


def select_mini(tests: list[dict]) -> list[dict]:
    if len(tests) <= MAX_TESTS:
        return list(tests)
    ordered = sorted(tests, key=lambda t: (random.seed(t["snippets"][0]["file_path"]), random.random())[1])
    return ordered[:MAX_TESTS]


def corpus_files(tests: list[dict]) -> set[str]:
    return {s["file_path"] for t in tests for s in t["snippets"]}


def read_doc(root: str, rel_path: str) -> str:
    with open(os.path.join(root, "corpus", rel_path), encoding="utf-8") as f:
        return f.read()


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def precision_recall(retrieved: list[tuple[str, int, int]], gt: list[tuple[str, int, int]]) -> tuple[float, float]:
    total_retrieved = sum(e - s for _, s, e in retrieved)
    total_relevant = sum(e - s for _, s, e in gt)
    rel_in_retrieved = sum(_overlap((s, e), (gs, ge)) for f, s, e in retrieved for gf, gs, ge in gt if gf == f)
    precision = rel_in_retrieved / total_retrieved if total_retrieved else 0.0
    recall = rel_in_retrieved / total_relevant if total_relevant else 0.0
    return precision, recall
