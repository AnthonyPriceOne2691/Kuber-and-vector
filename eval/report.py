"""Отчёт по веткам поиска на LegalBench-RAG-mini, с доверительными интервалами.

    python3 -I eval/report.py cache/stage1 eval/results/stage1.md

- Интервалы 95 %: бутстреп по вопросам (2000 повторов, зерно 0), выборка внутри каждого набора.
- Разница с базой: парный бутстреп на тех же вопросах. Разница, интервал которой
  накрывает ноль, не считается ни улучшением, ни ухудшением (правило §8б из RAG LESSONS).
- Числа статьи: arXiv 2408.10343, табл. 4 (naive, без реранкера, OpenAI text-embedding-3-large),
  тот же мини-набор. Строка ALL в статье не равна среднему по наборам, поэтому сравниваем
  по наборам, а среднее считаем сами.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

K = ("1", "2", "4", "8", "16", "32", "64")
DS = ("privacy_qa", "contractnli", "maud", "cuad")
NAMES = {"privacy_qa": "PrivacyQA", "contractnli": "ContractNLI", "maud": "MAUD", "cuad": "CUAD"}
MODES = {"dense": "вектор", "bm25": "BM25", "hybrid": "гибрид (RRF)"}
BASE = ("naive500", "dense")
PAPER_NAIVE_RECALL = {
    "privacy_qa": [7.45, 12.53, 20.88, 32.38, 42.45, 54.27, 66.07],
    "contractnli": [11.32, 19.10, 29.79, 45.59, 56.75, 69.88, 86.57],
    "maud": [2.54, 3.12, 4.53, 8.75, 13.16, 18.36, 25.62],
    "cuad": [12.60, 19.47, 27.92, 40.70, 51.02, 64.38, 75.71],
}
B = 2000


def recalls(res: dict, d: str, k: str) -> list[float]:
    return [100 * r for _, r in res["per_query"][d][k]]


def boot(res: dict, k: str, other: dict | None = None, ds=DS) -> tuple[float, float, float]:
    """Среднее по наборам и 95 % интервал; с other это парная разница res − other."""
    rng = random.Random(0)
    data = {d: (recalls(res, d, k), recalls(other, d, k) if other else None) for d in ds}
    point = statistics.mean(statistics.mean(a) - (statistics.mean(b) if b else 0) for a, b in data.values())
    sims = []
    for _ in range(B):
        vals = []
        for a, b in data.values():
            idx = [rng.randrange(len(a)) for _ in a]
            vals.append(sum(a[i] for i in idx) / len(idx) - (sum(b[i] for i in idx) / len(idx) if b else 0))
        sims.append(statistics.mean(vals))
    sims.sort()
    return point, sims[int(0.025 * B)], sims[int(0.975 * B)]


def fmt(t: tuple[float, float, float], sign: bool = False) -> str:
    p, lo, hi = t
    s = "+" if sign else ""
    return f"{p:{s}.1f} [{lo:{s}.1f}; {hi:{s}.1f}]"


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    meta = json.load(open(src / "meta.json"))
    res = {}
    for f in sorted((src / "results").glob("result_*.json")):
        r = json.load(open(f))
        strategy = f.stem[len("result_"):].rsplit("_", 1)[0]
        res[(strategy, r["mode"])] = r
    base = res[BASE]

    L = ["# Замер поиска на LegalBench-RAG-mini", "",
         f"Модель векторов {meta['model']} локально (Ollama на GPU Мака). Вопросов {meta['queries']}, документов "
         f"{meta['docs']}, знаков {meta['chars']:,}: мини-набор повторён по коду авторов и совпал со статьёй до знака. "
         "Метрика посимвольная, как у авторов. Вектор считается точным поиском (воспроизводимо). "
         "В скобках 95 % доверительный интервал (бутстреп по вопросам); разница с базой посчитана парно на тех же вопросах.", "",
         "## Ветки", "",
         "| Нарезка | Режим | Recall@8 | Recall@64 | Δ к базе @64 | p50 / p95 вживую, мс |",
         "|---|---|---|---|---|---|"]
    for (strategy, mode), r in sorted(res.items(), key=lambda x: (x[0][0], list(MODES).index(x[0][1]))):
        lat = r["latency_ms"]["live"]
        delta = "база" if (strategy, mode) == BASE else fmt(boot(r, "64", base), sign=True)
        L.append(f"| {strategy} | {MODES[mode]} | {fmt(boot(r, '8'))} | {fmt(boot(r, '64'))} | {delta} | "
                 f"{lat['p50']:.1f} / {lat['p95']:.1f} |")
    L += ["", "Нарезка `_title`: к тексту для поиска приписаны название документа и раздел (contextual embeddings "
          "без LLM); адрес и смещения куска те же.", ""]

    L += ["## Recall@64 по наборам, %", "",
          "| Набор | Статья (OpenAI, naive) | " + " | ".join(f"{s} {MODES[m]}" for s, m in sorted(res)) + " |",
          "|---|---|" + "---|" * len(res)]
    for d in DS:
        cells = [fmt(boot(res[key], "64", ds=(d,))) for key in sorted(res)]
        L.append(f"| {NAMES[d]} | {PAPER_NAIVE_RECALL[d][-1]:.1f} | " + " | ".join(cells) + " |")
    paper_mean = statistics.mean(PAPER_NAIVE_RECALL[d][-1] for d in DS)
    L.append(f"| Среднее | {paper_mean:.1f} | " + " | ".join(fmt(boot(res[key], "64")) for key in sorted(res)) + " |")
    L += ["", "## База (naive500, вектор) против статьи по всем k, recall %", "",
          "| Набор | " + " | ".join(f"k={k}" for k in K) + " |", "|---|" + "---|" * len(K)]
    for d in DS:
        L.append(f"| {NAMES[d]} | " + " | ".join(f"{base['datasets'][d][k]['recall']:.1f} / {p:.1f}"
                                              for k, p in zip(K, PAPER_NAIVE_RECALL[d])) + " |")
    hv = base["live_vs_exact"]
    L += ["", f"Приближённый HNSW совпал с точным поиском на {hv['8']:.1f} % при k=8 и {hv['64']:.1f} % при k=64.", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
