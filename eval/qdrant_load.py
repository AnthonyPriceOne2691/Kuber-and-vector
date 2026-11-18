"""Загрузка кусков и векторов в Qdrant. Запускается внутри кластера (на машине стенда),
только стандартная библиотека. Ключ админский, читается из файла, а не из аргументов.

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
    base = f"{args.url.rstrip('/')}/collections/{args.collection}"

    existing = call("GET", f"{args.url.rstrip('/')}/collections", key)["result"]["collections"]
    if args.collection not in {c["name"] for c in existing}:
        call("PUT", base, key, {"vectors": {"size": args.dim, "distance": "Cosine"},
                                "shard_number": args.shards, "replication_factor": args.replicas})

    points = [json.loads(line) for line in open(f"{args.dir}/points.jsonl", encoding="utf-8")]
    vectors = array("f")
    with open(f"{args.dir}/vectors.f32", "rb") as f:
        vectors.fromfile(f, len(points) * args.dim)

    t0 = time.perf_counter()
    for i in range(0, len(points), args.batch):
        batch = []
        for j, p in enumerate(points[i:i + args.batch], start=i):
            batch.append({"id": p["id"], "vector": vectors[j * args.dim:(j + 1) * args.dim].tolist(),
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
