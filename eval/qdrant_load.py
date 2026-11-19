"""Загрузка кусков и векторов в Qdrant. Запускается внутри кластера (на машине стенда),
только стандартная библиотека. Ключ админский, читается из файла, а не из аргументов.

Векторы именованные: «dense» (модель набора, косинус) и, если в точках есть BM25, разреженный «bm25»
с modifier idf: IDF считает Qdrant, частоту и длину документа считали при подготовке.
Файлы читаются порциями: память загрузчика не растёт с размером корпуса (полный корпус это
около 159 тыс. точек и 0,65 ГБ векторов, а у машины стенда всего 4 ГБ).

Для замера предела (сколько влезает в машину):
    --quantization int8   скалярное квантование, сжатые векторы в памяти, в 4 раза меньше float32
    --on-disk             исходные векторы на диске, в памяти только то, что нужно поиску
Для подложенной ошибки в сцене смены модели (deploy/scenes/model-switch.sh):
    --limit N             только первые N кусков набора

    python3 -I qdrant_load.py --url http://IP:6333 --key-file KEY --dir naive500 --collection C
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from array import array
from typing import Iterator

PAYLOAD = ("dataset", "file", "title", "section", "start", "end", "text", "parent")  # parent: у детей (eval/children.py)


def call(method: str, url: str, key: str, body: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def batches(directory: str, dim: int, size: int, limit: int | None = None) -> Iterator[list[tuple[dict, array]]]:
    """Точки и их векторы порциями; points.jsonl и vectors.f32 идут в одном порядке."""
    with open(f"{directory}/points.jsonl", encoding="utf-8") as fp, open(f"{directory}/vectors.f32", "rb") as fv:
        batch: list[tuple[dict, array]] = []
        n = 0
        for line in fp:
            if limit is not None and n >= limit:
                break
            vec = array("f")
            vec.fromfile(fv, dim)  # EOFError, если векторов меньше, чем точек
            batch.append((json.loads(line), vec))
            n += 1
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch
        if limit is None and fv.read(1):
            raise ValueError("векторов больше, чем точек: файлы от разных прогонов")


def collection_config(dim: int, shards: int, replicas: int, sparse: bool,
                      quantization: str | None = None, on_disk: bool = False) -> dict:
    dense: dict = {"size": dim, "distance": "Cosine"}
    if on_disk:
        dense["on_disk"] = True
    cfg: dict = {"vectors": {"dense": dense}, "shard_number": shards, "replication_factor": replicas}
    if sparse:
        cfg["sparse_vectors"] = {"bm25": {"modifier": "idf"}}
    if quantization == "int8":
        cfg["quantization_config"] = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}
    return cfg


def to_point(p: dict, vec: array) -> dict:
    vector: dict = {"dense": vec.tolist()}
    if "sparse" in p:
        vector["bm25"] = p["sparse"]
    return {"id": p["id"], "vector": vector, "payload": {k: p[k] for k in PAYLOAD if k in p}}


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
    ap.add_argument("--quantization", choices=("int8",), default=None)
    ap.add_argument("--on-disk", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="только первые N кусков")
    ap.add_argument("--wait-green", type=int, default=900, help="сколько секунд ждать готовности индекса")
    args = ap.parse_args()
    with open(args.key_file) as f:
        key = f.read().strip()
    root = args.url.rstrip("/")
    base = f"{root}/collections/{args.collection}"

    with open(f"{args.dir}/points.jsonl", encoding="utf-8") as f:
        sparse = "sparse" in json.loads(f.readline())
    existing = {c["name"] for c in call("GET", f"{root}/collections", key)["result"]["collections"]}
    if args.collection not in existing:
        call("PUT", base, key, collection_config(args.dim, args.shards, args.replicas, sparse,
                                                 args.quantization, args.on_disk))

    t0 = time.perf_counter()
    loaded = 0
    for batch in batches(args.dir, args.dim, args.batch, args.limit):
        call("PUT", f"{base}/points?wait=true", key, {"points": [to_point(p, v) for p, v in batch]})
        loaded += len(batch)
    took = time.perf_counter() - t0
    # Индекс HNSW достраивается в фоне; оценивать до статуса green нельзя: цифры поплывут
    t1 = time.perf_counter()
    while True:
        info = call("GET", base, key)["result"]
        if info.get("status") == "green" or time.perf_counter() - t1 > args.wait_green:
            break
        time.sleep(2)
    print(f"{args.collection}: загружено {loaded} точек за {took:.0f} с; индекс готов через "
          f"{time.perf_counter() - t1:.0f} с; в коллекции {info.get('points_count')}, статус {info.get('status')}")


if __name__ == "__main__":
    main()
