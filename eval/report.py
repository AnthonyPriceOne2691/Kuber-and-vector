"""Отчёт этапа 1: precision и recall по наборам против чисел статьи LegalBench-RAG.

    python3 -I eval/report.py cache/stage1 eval/results/stage1.md

Числа статьи: arXiv 2408.10343, табл. 4 (naive) и 5 (RCTS), без реранкера,
модель OpenAI text-embedding-3-large, куски по 500 знаков, тот же мини-набор (776 вопросов).
В самих таблицах статьи есть несостыковки (строка ALL не равна среднему по наборам),
поэтому сравниваем по наборам, а ALL считаем сами как среднее.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

K = ("1", "2", "4", "8", "16", "32", "64")
DS = ("privacy_qa", "contractnli", "maud", "cuad")
NAMES = {"privacy_qa": "PrivacyQA", "contractnli": "ContractNLI", "maud": "MAUD", "cuad": "CUAD"}
PAPER = {
    "naive500": {
        "recall": {"privacy_qa": [7.45, 12.53, 20.88, 32.38, 42.45, 54.27, 66.07],
                   "contractnli": [11.32, 19.10, 29.79, 45.59, 56.75, 69.88, 86.57],
                   "maud": [2.54, 3.12, 4.53, 8.75, 13.16, 18.36, 25.62],
                   "cuad": [12.60, 19.47, 27.92, 40.70, 51.02, 64.38, 75.71]},
        "precision": {"privacy_qa": [7.86, 7.31, 6.41, 5.06, 3.58, 2.41, 1.54],
                      "contractnli": [16.45, 14.80, 12.53, 9.73, 6.70, 4.65, 3.04],
                      "maud": [3.36, 2.65, 2.18, 1.89, 1.48, 1.06, 0.75],
                      "cuad": [9.27, 8.05, 5.98, 4.33, 2.77, 1.77, 1.09]}},
    "rcts500": {
        "recall": {"privacy_qa": [8.85, 15.21, 27.92, 42.37, 55.12, 71.19, 84.19],
                   "contractnli": [7.63, 11.33, 17.34, 24.99, 35.80, 46.57, 61.72],
                   "maud": [1.65, 2.09, 4.59, 6.18, 12.93, 21.04, 28.28],
                   "cuad": [1.62, 8.11, 17.72, 31.68, 44.38, 60.04, 74.70]},
        "precision": {"privacy_qa": [14.38, 13.55, 12.34, 9.03, 6.06, 4.17, 2.81],
                      "contractnli": [6.63, 5.29, 3.89, 2.81, 1.98, 1.29, 0.90],
                      "maud": [2.65, 1.77, 1.96, 1.40, 1.39, 1.15, 0.82],
                      "cuad": [1.97, 4.03, 4.83, 4.20, 2.94, 1.99, 1.25]}},
}


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    meta = json.load(open(src / "meta.json"))
    lines = ["# Этап 1: качество поиска на LegalBench-RAG-mini", "",
             f"Модель векторов: {meta['model']} (локально, Ollama на GPU Мака). Вопросов {meta['queries']}, "
             f"документов {meta['docs']}, знаков {meta['chars']:,}: мини-набор повторён по коду авторов и совпал "
             "со статьёй до знака. Метрика посимвольная, как у авторов. Статья: та же нарезка, модель OpenAI "
             "text-embedding-3-large, без реранкера (arXiv 2408.10343, табл. 4 и 5).", ""]
    for strategy, st in meta["strategies"].items():
        r = json.load(open(src / "results" / f"result_{strategy}.json"))
        lines += [f"## {strategy}: кусков {st['chunks']}", ""]
        if st.get("chunks_per_second"):
            lines += [f"Векторизация: {st['new_vectors']} кусков за {st['embed_seconds']} с, "
                      f"{st['chunks_per_second']} кусков/с.", ""]
        lat, hv = r["latency_ms"], r["hnsw_vs_exact"]
        lines += [f"Качество ниже посчитано на точном поиске, он воспроизводим. Приближённый HNSW совпал с точным "
                  f"на {hv['8']:.1f} % при k=8 и на {hv['64']:.1f} % при k=64.", "",
                  f"Задержка поиска top-64, один клиент с узла через ClusterIP: HNSW p50 {lat['hnsw']['p50']:.1f} мс, "
                  f"p95 {lat['hnsw']['p95']:.1f} мс, p99 {lat['hnsw']['p99']:.1f} мс; точный поиск "
                  f"p50 {lat['exact']['p50']:.1f} мс, p95 {lat['exact']['p95']:.1f} мс.", ""]
        for metric, title in (("recall", "Recall@k, %"), ("precision", "Precision@k, %")):
            lines += [f"**{title}** (наше / статья)", "",
                      "| Набор | " + " | ".join(f"k={k}" for k in K) + " |",
                      "|---|" + "---|" * len(K)]
            for d in DS:
                ours = [r["datasets"][d][k][metric] for k in K]
                paper = PAPER.get(strategy, {}).get(metric, {}).get(d)
                cells = [f"{o:.1f} / {p:.1f}" if paper else f"{o:.1f}" for o, p in zip(ours, paper or ours)]
                lines.append(f"| {NAMES[d]} | " + " | ".join(cells) + " |")
            mean_ours = [r["all"][k][metric] for k in K]
            pv = PAPER.get(strategy, {}).get(metric)
            mean_paper = [sum(pv[d][i] for d in DS) / len(DS) for i in range(len(K))] if pv else None
            cells = [f"{o:.1f} / {p:.1f}" if mean_paper else f"{o:.1f}" for o, p in zip(mean_ours, mean_paper or mean_ours)]
            lines += ["| Среднее по наборам | " + " | ".join(cells) + " |", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
