"""Проба записи во время отказа машины. Запускается внутри machine-1 через kubectl port-forward
к пиру Qdrant на этой же машине (его не выключаем), админским ключом из файла.

    python3 -I outage_writes.py write  --url http://127.0.0.1:P --key-file RW --collection C --seconds 150 --out w.json
    python3 -I outage_writes.py verify --url http://127.0.0.1:P --key-file RW --collection C --log w.json

write: создаёт пустую коллекцию (6 шардов по 2 копии, маленькие векторы) и пишет по точке
с ровным темпом, каждая запись с wait=true. Журнал: номер, время, подтверждена ли, ошибка.
verify: после возвращения машины проверяет, что каждая подтверждённая запись на месте.
Сверку копий шардов делает replicas.py. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request

DIM = 8


def call(method: str, url: str, key: str, body: dict | None = None, timeout: float = 3) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def vector(i: int) -> list[float]:
    return [round(math.sin(i * (j + 1)), 6) for j in range(DIM)]


def create(base: str, key: str, shards: int, replicas: int) -> None:
    try:
        call("DELETE", base, key, timeout=30)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code != 404:
            raise
    call("PUT", base, key, {"vectors": {"size": DIM, "distance": "Cosine"}, "shard_number": shards,
                            "replication_factor": replicas, "write_consistency_factor": 1}, timeout=30)


def write(args: argparse.Namespace, key: str) -> dict:
    base = f"{args.url.rstrip('/')}/collections/{args.collection}"
    create(base, key, args.shards, args.replicas)
    log = []
    t_start = time.time()
    i = 0
    while i / args.rate < args.seconds:
        time.sleep(max(0.0, t_start + i / args.rate - time.time()))
        t0 = time.time()
        rec = {"id": i, "t": round(t0 - t_start, 3)}
        try:
            call("PUT", f"{base}/points?wait=true", key,
                 {"points": [{"id": i, "vector": vector(i), "payload": {"seq": i}}]}, timeout=args.timeout)
            rec["ok"] = True
        except Exception as e:  # отказ, таймаут или 5xx: запись не подтверждена
            rec["ok"] = False
            if isinstance(e, urllib.error.HTTPError):
                e.close()
                rec["error"] = f"HTTP {e.code}"
            else:
                rec["error"] = type(e).__name__
        rec["ms"] = round((time.time() - t0) * 1000, 1)
        log.append(rec)
        i += 1
    return {"collection": args.collection, "rate": args.rate, "seconds": args.seconds, "writes": log}


def verify(args: argparse.Namespace, key: str) -> dict:
    with open(args.log) as f:
        result = json.load(f)
    base = f"{args.url.rstrip('/')}/collections/{args.collection}"
    acked = [r["id"] for r in result["writes"] if r["ok"]]
    found: set[int] = set()
    for i in range(0, len(acked), 256):
        got = call("POST", f"{base}/points", key, {"ids": acked[i:i + 256], "with_payload": False,
                                                   "with_vector": False}, timeout=30)["result"]
        found.update(p["id"] for p in got)
    count = call("POST", f"{base}/points/count", key, {"exact": True}, timeout=30)["result"]["count"]
    return summarize(result["writes"], found, count)


def summarize(writes: list[dict], found: set[int], count: int) -> dict:
    acked = [r["id"] for r in writes if r["ok"]]
    failed = [r for r in writes if not r["ok"]]
    return {
        "sent": len(writes), "acked": len(acked), "failed": len(failed),
        "failed_window_s": [failed[0]["t"], failed[-1]["t"]] if failed else None,
        "errors": sorted({r["error"] for r in failed}),
        "max_ms": max((r["ms"] for r in writes), default=None),
        "acked_missing": sorted(set(acked) - found),
        "points_in_collection": count,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("write", "verify"))
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--collection", default="outage_rf2")
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--replicas", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=150)
    ap.add_argument("--rate", type=float, default=10)
    ap.add_argument("--timeout", type=float, default=3)
    ap.add_argument("--out")
    ap.add_argument("--log")
    args = ap.parse_args()
    with open(args.key_file) as f:
        key = f.read().strip()
    if args.mode == "write":
        result = write(args, key)
        with open(args.out, "w") as f:
            json.dump(result, f)
        s = summarize(result["writes"], {r["id"] for r in result["writes"] if r["ok"]}, -1)
        print(f"записей {s['sent']}, подтверждено {s['acked']}, ошибок {s['failed']}"
              + (f" с {s['failed_window_s'][0]} по {s['failed_window_s'][1]} с ({', '.join(s['errors'])})" if s["failed"] else "")
              + f", самая долгая запись {s['max_ms']} мс")
    else:
        s = verify(args, key)
        print(f"подтверждённых записей {s['acked']}, из них не нашлось {len(s['acked_missing'])}; "
              f"точек в коллекции {s['points_in_collection']}")
        print(json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    main()
