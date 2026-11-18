"""Карта «шард × машина» для коллекций кластерного Qdrant. Запускается внутри машины кластера.

    python3 -I placement.py KEY_FILE PODS_FILE collection [collection ...]

PODS_FILE: строки «под машина порт», где порт это локальный конец kubectl port-forward
к этому поду. Каждого пира спрашиваем про его локальные шарды, поэтому карта собрана
из первых рук, а не из чужого представления.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def get(port: str, path: str, key: str) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"api-key": key})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)["result"]


def main() -> None:
    key = open(sys.argv[1]).read().strip()
    pods = [line.split() for line in open(sys.argv[2]) if line.strip()]
    for coll in sys.argv[3:]:
        held: dict[int, dict[str, str]] = {}
        machines = []
        for pod, node, port in sorted(pods, key=lambda p: p[1]):
            machines.append(node)
            info = get(port, f"/collections/{coll}/cluster", key)
            for s in info["local_shards"]:
                held.setdefault(s["shard_id"], {})[node] = f'{s["state"]} {s["points_count"]}'
        cfg = get(pods[0][2], f"/collections/{coll}", key)["config"]["params"]
        print(f"\n{coll}: шардов {cfg['shard_number']}, копий {cfg['replication_factor']}")
        print("шард  " + "  ".join(f"{m:>16}" for m in machines))
        for sid in sorted(held):
            print(f"{sid:>4}  " + "  ".join(f"{held[sid].get(m, '·'):>16}" for m in machines))
        per = {m: sum(1 for s in held.values() if m in s) for m in machines}
        print("всего " + "  ".join(f"{per[m]:>16}" for m in machines))


if __name__ == "__main__":
    main()
