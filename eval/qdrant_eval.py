"""Оценка поиска на LegalBench-RAG-mini через Qdrant. Запускается внутри кластера,
только стандартная библиотека, ключ ТОЛЬКО НА ЧТЕНИЕ (из файла).

Векторы вопросов посчитаны заранее на Маке. Для каждого вопроса один поиск top-64,
precision и recall при k = 1…64 считаются по первым k результатам, как у авторов.

    python3 -I qdrant_eval.py --url http://IP:6333 --key-file KEY --dir . --collection C --out res.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # наш legalbench.py рядом; с -I сам не находится
from legalbench import DATASETS, K_VALUES, precision_recall  # noqa: E402


def search(url: str, key: str, collection: str, vector: list[float], limit: int, exact: bool = False) -> list[dict]:
    body = json.dumps({"query": vector, "limit": limit, "with_payload": ["file", "start", "end"],
                       "params": {"exact": exact}}).encode()
    req = urllib.request.Request(f"{url}/collections/{collection}/points/query", data=body, method="POST",
                                 headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["result"]["points"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    key = open(args.key_file).read().strip()
    url = args.url.rstrip("/")

    queries = [json.loads(line) for line in open(f"{args.dir}/queries.jsonl", encoding="utf-8")]
    qv = array("f")
    with open(f"{args.dir}/query_vectors.f32", "rb") as f:
        qv.fromfile(f, len(queries) * args.dim)

    # Качество модели меряем на точном поиске (воспроизводимо), а HNSW сравниваем с ним отдельно:
    # доля точного top-k, которую нашёл приближённый поиск (это метрика самой векторной базы).
    kmax = max(K_VALUES)
    per_ds: dict[str, dict[int, list[tuple[float, float]]]] = {d: {k: [] for k in K_VALUES} for d in DATASETS}
    ann_overlap: dict[int, list[float]] = {k: [] for k in K_VALUES}
    lat_exact, lat_hnsw = [], []
    for i, q in enumerate(queries):
        vec = qv[i * args.dim:(i + 1) * args.dim].tolist()
        t0 = time.perf_counter()
        exact = search(url, key, args.collection, vec, kmax, exact=True)
        lat_exact.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        hnsw = search(url, key, args.collection, vec, kmax)
        lat_hnsw.append((time.perf_counter() - t0) * 1000)
        found = [(h["payload"]["file"], h["payload"]["start"], h["payload"]["end"]) for h in exact]
        gt = [tuple(s) for s in q["snippets"]]
        for k in K_VALUES:
            per_ds[q["dataset"]][k].append(precision_recall(found[:k], gt))
            ex_ids = {h["id"] for h in exact[:k]}
            ann_overlap[k].append(len(ex_ids & {h["id"] for h in hnsw[:k]}) / len(ex_ids) if ex_ids else 1.0)

    result = {"collection": args.collection, "queries": len(queries), "search": "exact", "datasets": {}, "all": {},
              "hnsw_vs_exact": {str(k): 100 * statistics.mean(v) for k, v in ann_overlap.items()}}
    for d in DATASETS:
        result["datasets"][d] = {str(k): {"precision": 100 * statistics.mean(p for p, _ in v),
                                          "recall": 100 * statistics.mean(r for _, r in v)}
                                 for k, v in per_ds[d].items()}
    for k in K_VALUES:
        result["all"][str(k)] = {m: statistics.mean(result["datasets"][d][str(k)][m] for d in DATASETS)
                                 for m in ("precision", "recall")}
    def pct(xs: list[float]) -> dict:
        s = sorted(xs)
        return {"p50": s[len(s) // 2], "p95": s[int(len(s) * 0.95)], "p99": s[int(len(s) * 0.99)]}

    result["latency_ms"] = {"hnsw": pct(lat_hnsw), "exact": pct(lat_exact),
                            "note": "один клиент, последовательно, top-64, с узла через ClusterIP"}
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    r, h = result["all"], result["hnsw_vs_exact"]
    print(f"{args.collection}: recall@1/8/64 = {r['1']['recall']:.1f} / {r['8']['recall']:.1f} / {r['64']['recall']:.1f} "
          f"(точный поиск); HNSW совпал с точным на {h['8']:.1f}% (k=8) и {h['64']:.1f}% (k=64); "
          f"HNSW p50 {result['latency_ms']['hnsw']['p50']:.1f} мс, p95 {result['latency_ms']['hnsw']['p95']:.1f} мс")


if __name__ == "__main__":
    main()
