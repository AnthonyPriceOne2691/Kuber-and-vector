"""Копии шардов Qdrant по машинам: все ли Active и совпадает ли у копий число точек.
Запускается внутри машины кластера, как deploy/qdrant/placement.py: каждого пира спрашиваем
про его локальные шарды через свой туннель kubectl port-forward.

    python3 -I replicas.py KEY_FILE PODS_FILE collection [collection ...] [--wait 300]

PODS_FILE: строки «под машина порт». Код выхода 0, если у каждой коллекции у каждого шарда
столько копий, сколько задано, все Active и с одинаковым числом точек. С --wait ждёт этого
до N секунд и печатает, сколько ждал. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def get(port: str, path: str, key: str, timeout: float = 3) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def snapshot(pods: list[list[str]], key: str, collection: str) -> tuple[dict, dict, int | None]:
    """({шард: {машина: (состояние, точек)}}, {машина: отвечает ли пир}, число копий из настроек)."""
    held: dict[int, dict[str, tuple[str, int | None]]] = {}
    reachable: dict[str, bool] = {}
    rf = None
    for _pod, node, port in pods:
        try:
            info = get(port, f"/collections/{collection}/cluster", key)
            if rf is None:
                rf = get(port, f"/collections/{collection}", key)["config"]["params"].get("replication_factor", 1)
        except Exception:
            reachable[node] = False
            continue
        reachable[node] = True
        for s in info["local_shards"]:
            held.setdefault(s["shard_id"], {})[node] = (s["state"], s.get("points_count"))
    return held, reachable, rf


def problems(held: dict, reachable: dict, rf: int | None) -> list[str]:
    out = [f"пир на {n} не отвечает" for n, ok in sorted(reachable.items()) if not ok]
    if rf is None:
        return out + ["ни один пир не ответил"]
    for sid in sorted(held):
        copies = held[sid]
        bad = {n: st for n, (st, _) in copies.items() if st != "Active"}
        if bad:
            out.append(f"шард {sid}: " + ", ".join(f"{n} {st}" for n, st in sorted(bad.items())))
        if len(copies) < rf:
            out.append(f"шард {sid}: копий {len(copies)} из {rf}")
        counts = {c for _, c in copies.values()}
        if len(counts) > 1:
            out.append(f"шард {sid}: у копий разное число точек " +
                       ", ".join(f"{n} {c}" for n, (_, c) in sorted(copies.items())))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file")
    ap.add_argument("pods_file")
    ap.add_argument("collections", nargs="+")
    ap.add_argument("--wait", type=float, default=0)
    args = ap.parse_args()
    with open(args.key_file) as f:
        key = f.read().strip()
    with open(args.pods_file) as f:
        pods = [line.split() for line in f if line.strip()]
    t0 = time.monotonic()
    while True:
        report = {c: problems(*snapshot(pods, key, c)) for c in args.collections}
        if not any(report.values()) or time.monotonic() - t0 >= args.wait:
            break
        time.sleep(2)
    waited = time.monotonic() - t0
    for c, issues in report.items():
        print(f"{c}: " + ("все копии Active и совпадают" if not issues else "; ".join(issues)))
    if args.wait:
        print(f"ждали {waited:.0f} с")
    sys.exit(1 if any(report.values()) else 0)


if __name__ == "__main__":
    main()
