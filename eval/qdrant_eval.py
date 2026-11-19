"""Оценка поиска на LegalBench-RAG-mini через Qdrant. Запускается внутри кластера,
только стандартная библиотека, ключ ТОЛЬКО НА ЧТЕНИЕ (из файла).

Режимы:
  dense   вектор bge-m3; качество на точном поиске (воспроизводимо), отдельно доля
          совпадения приближённого HNSW с точным и задержки обоих
  bm25    разреженный BM25 (IDF считает Qdrant)
  hybrid  dense и bm25 по 100 кандидатов, слияние RRF внутри Qdrant (Query API)
Векторы вопросов посчитаны заранее на Маке. На вопрос один поиск top-64, precision и recall
при k = 1…64 по первым k результатам, как у авторов. Результат по каждому вопросу сохраняется
для доверительных интервалов.

    python3 -I -B qdrant_eval.py --url http://IP:6333 --key-file KEY --dir . --collection C --mode dense --out res.json
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

PAYLOAD = ["file", "start", "end"]
PREFETCH = 100


def query(url: str, key: str, collection: str, body: dict, group_by: str | None = None) -> list[dict]:
    """Точки по порядку. С group_by — группы Qdrant (/points/query/groups): из каждой группы лучшая
    точка, групп limit штук. Так ищут «от малого к большому»: дети ранжируют родителя (eval/children.py),
    в выдаче родитель один раз, его адрес лежит в каждой точке-ребёнке."""
    path = "points/query/groups" if group_by else "points/query"
    if group_by:
        body = dict(body, group_by=group_by, group_size=1)
    req = urllib.request.Request(f"{url}/collections/{collection}/{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)["result"]
    if group_by:
        return [g["hits"][0] for g in res["groups"] if g.get("hits")]
    return res["points"]


def body_for(mode: str, vec: list[float], sparse: dict, limit: int, exact: bool) -> dict:
    if mode == "dense":
        return {"query": vec, "using": "dense", "limit": limit, "with_payload": PAYLOAD, "params": {"exact": exact}}
    if mode == "bm25":
        return {"query": sparse, "using": "bm25", "limit": limit, "with_payload": PAYLOAD}
    return {"prefetch": [{"query": vec, "using": "dense", "limit": PREFETCH, "params": {"exact": exact}},
                         {"query": sparse, "using": "bm25", "limit": PREFETCH}],
            "query": {"fusion": "rrf"}, "limit": limit, "with_payload": PAYLOAD}


def pct(xs: list[float]) -> dict:
    s = sorted(xs)
    return {"p50": s[len(s) // 2], "p95": s[int(len(s) * 0.95)], "p99": s[int(len(s) * 0.99)]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--mode", choices=("dense", "bm25", "hybrid"), default="dense")
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group-by", help="поле группы, например parent: дети ранжируют родителя")
    args = ap.parse_args()
    key = open(args.key_file).read().strip()
    url = args.url.rstrip("/")

    queries = [json.loads(line) for line in open(f"{args.dir}/queries.jsonl", encoding="utf-8")]
    qv = array("f")
    with open(f"{args.dir}/query_vectors.f32", "rb") as f:
        qv.fromfile(f, len(queries) * args.dim)

    kmax = max(K_VALUES)
    per_query: dict[str, dict[str, list[list[float]]]] = {d: {str(k): [] for k in K_VALUES} for d in DATASETS}
    overlap: dict[int, list[float]] = {k: [] for k in K_VALUES}
    lat_eval, lat_live = [], []
    for i, q in enumerate(queries):
        vec = qv[i * args.dim:(i + 1) * args.dim].tolist()
        t0 = time.perf_counter()
        hits = query(url, key, args.collection, body_for(args.mode, vec, q["sparse"], kmax, exact=True), args.group_by)
        lat_eval.append((time.perf_counter() - t0) * 1000)
        if args.mode != "bm25":  # так поиск работал бы вживую: dense через HNSW
            t0 = time.perf_counter()
            live = query(url, key, args.collection, body_for(args.mode, vec, q["sparse"], kmax, exact=False), args.group_by)
            lat_live.append((time.perf_counter() - t0) * 1000)
        else:
            live = hits
        found = [(h["payload"]["file"], h["payload"]["start"], h["payload"]["end"]) for h in hits]
        gt = [tuple(s) for s in q["snippets"]]
        for k in K_VALUES:
            per_query[q["dataset"]][str(k)].append(list(precision_recall(found[:k], gt)))
            ids = {h["id"] for h in hits[:k]}
            overlap[k].append(len(ids & {h["id"] for h in live[:k]}) / len(ids) if ids else 1.0)

    result = {"collection": args.collection, "mode": args.mode, "queries": len(queries),
              "datasets": {}, "all": {}, "per_query": per_query,
              "live_vs_exact": {str(k): 100 * statistics.mean(v) for k, v in overlap.items()},
              "latency_ms": {"eval": pct(lat_eval), "live": pct(lat_live or lat_eval),
                             "note": "один клиент, последовательно, top-64, с узла через ClusterIP"}}
    for d in DATASETS:
        result["datasets"][d] = {k: {"precision": 100 * statistics.mean(p for p, _ in v),
                                     "recall": 100 * statistics.mean(r for _, r in v)}
                                 for k, v in per_query[d].items()}
    for k in K_VALUES:
        result["all"][str(k)] = {m: statistics.mean(result["datasets"][d][str(k)][m] for d in DATASETS)
                                 for m in ("precision", "recall")}
    with open(args.out, "w") as f:
        json.dump(result, f)
    r, h, lat = result["all"], result["live_vs_exact"], result["latency_ms"]["live"]
    print(f"{args.collection} [{args.mode}]: recall@1/8/64 = {r['1']['recall']:.1f} / {r['8']['recall']:.1f} / "
          f"{r['64']['recall']:.1f}; вживую совпало с точным на {h['8']:.1f}% (k=8); p50 {lat['p50']:.1f} мс, p95 {lat['p95']:.1f} мс")


if __name__ == "__main__":
    main()
