"""Загрузка кусков и векторов в Qdrant. Запускается внутри кластера (на машине стенда),
только стандартная библиотека. Ключ админский, читается из файла, а не из аргументов.

Векторы именованные: «dense» (bge-m3, косинус) и, если в точках есть BM25, разреженный «bm25»
с modifier idf: IDF считает Qdrant, частоту и длину документа считали при подготовке.

    python3 -I qdrant_load.py --url http://IP:6333 --key-file KEY --dir naive500 --collection C
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from array import array


def call(method: str, url: str, key: str, body: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--replicas", type=int, default=1)
    args = ap.parse_args()
    key = open(args.key_file).read().strip()
    root = args.url.rstrip("/")
    base = f"{root}/collections/{args.collection}"

    points = [json.loads(line) for line in open(f"{args.dir}/points.jsonl", encoding="utf-8")]
    sparse = "sparse" in points[0]
    vectors = array("f")
    with open(f"{args.dir}/vectors.f32", "rb") as f:
        vectors.fromfile(f, len(points) * args.dim)

    existing = {c["name"] for c in call("GET", f"{root}/collections", key)["result"]["collections"]}
    if args.collection not in existing:
        cfg: dict = {"vectors": {"dense": {"size": args.dim, "distance": "Cosine"}},
                     "shard_number": args.shards, "replication_factor": args.replicas}
        if sparse:
            cfg["sparse_vectors"] = {"bm25": {"modifier": "idf"}}
        call("PUT", base, key, cfg)

    t0 = time.perf_counter()
    for i in range(0, len(points), args.batch):
        batch = []
        for j, p in enumerate(points[i:i + args.batch], start=i):
            vec: dict = {"dense": vectors[j * args.dim:(j + 1) * args.dim].tolist()}
            if sparse:
                vec["bm25"] = p["sparse"]
            batch.append({"id": p["id"], "vector": vec,
                          "payload": {k: p[k] for k in ("dataset", "file", "title", "section", "start", "end", "text")
                                      if k in p}})
        call("PUT", f"{base}/points?wait=true", key, {"points": batch})
    took = time.perf_counter() - t0
    # Индекс HNSW достраивается в фоне; оценивать до статуса green нельзя: цифры поплывут
    t1 = time.perf_counter()
    while True:
        info = call("GET", base, key)["result"]
        if info.get("status") == "green" or time.perf_counter() - t1 > 900:
            break
        time.sleep(2)
    print(f"{args.collection}: загружено {len(points)} точек за {took:.0f} с; индекс готов через "
          f"{time.perf_counter() - t1:.0f} с; в коллекции {info.get('points_count')}, статус {info.get('status')}")


if __name__ == "__main__":
    main()
