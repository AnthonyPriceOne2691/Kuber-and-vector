"""Короткая сводка стенда по-английски в конце тихого подъёма (QUIET=1 deploy/stand-up.sh): она идёт в кадр.

    python3 -I -B deploy/stand_summary.py --kube K.json --gate G.json --entry IP --check-log LOG

  --kube       kubectl get nodes,pods -A -o json
  --gate       ответ шлюза Qdrant на /cluster (deploy/qdrant/gate.py): консенсус глазами пира
  --entry      адрес входа на machine-4
  --check-log  журнал подъёма: строка «итого: N ✓, M ✗» от deploy/check.sh

Пустой или битый вход не ошибка: в строке будет unknown. Строки целые, без цвета и управляющих
символов: их снимает capture.py витрины в псевдотерминале. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

LABEL = 9
CHECKS = re.compile(r"итого: (\d+) ✓, (\d+) ✗")


def load_json(path: str | None):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_ready(obj: dict) -> bool:
    conds = (obj.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)


def live_pods(items: list, namespace: str, label: str, value: str) -> list[dict]:
    """Поды пространства с меткой, готовые и не на удалении."""
    out = []
    for it in items:
        meta = it.get("metadata") or {}
        if it.get("kind") != "Pod" or meta.get("namespace") != namespace:
            continue
        if (meta.get("labels") or {}).get(label) != value or meta.get("deletionTimestamp"):
            continue
        if (it.get("status") or {}).get("phase") == "Running" and is_ready(it):
            out.append(it)
    return out


def machines(pods: list[dict]) -> list[str]:
    return sorted((p.get("spec") or {}).get("nodeName") or "?" for p in pods)


def line(label: str, text: str) -> str:
    return f"  {label:<{LABEL}}{text}"


def nodes_line(kube) -> str:
    if not isinstance(kube, dict):
        return line("nodes", "unknown, kubectl did not answer")
    nodes = [it for it in kube.get("items") or [] if it.get("kind") == "Node"]
    if not nodes:
        return line("nodes", "unknown, no nodes in the answer")
    ready = sorted((n.get("metadata") or {}).get("name", "?") for n in nodes if is_ready(n))
    versions = sorted({((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "") for n in nodes} - {""})
    text = f"{len(ready)} of {len(nodes)} Ready"
    if ready:
        text += f" ({', '.join(ready)})"
    if versions:
        text += f", k3s {', '.join(versions)}"
    return line("nodes", text)


def qdrant_line(kube, gate) -> str:
    pods = live_pods(kube.get("items") or [], "qdrant", "app.kubernetes.io/name", "qdrant") if isinstance(kube, dict) else None
    where = ""
    if pods:
        on = machines(pods)
        where = ", one per machine" if len(set(on)) == len(on) else f", on {', '.join(on)}"
    result = gate.get("result") if isinstance(gate, dict) else None
    peers = result.get("peers") if isinstance(result, dict) else None
    if isinstance(peers, dict) and peers:
        leader = (result.get("raft_info") or {}).get("leader")
        text = f"{len(peers)} peers in consensus" + (where if pods is not None and len(pods) == len(peers) else "")
        if leader is None:
            text += ", no raft leader yet"
        if pods is not None and len(pods) != len(peers):
            text += f", {len(pods)} pods Ready"
        return line("Qdrant", text)
    if pods is None:
        return line("Qdrant", "unknown, kubectl did not answer")
    return line("Qdrant", f"{len(pods)} peers Ready{where}, consensus unknown (gate did not answer)")


def search_line(kube) -> str:
    if not isinstance(kube, dict):
        return line("search", "unknown, kubectl did not answer")
    pods = live_pods(kube.get("items") or [], "search", "app", "search")
    if not pods:
        return line("search", "no pods Ready")
    return line("search", f"{len(pods)} pods Ready ({', '.join(machines(pods))})")


def entry_line(entry: str | None) -> str:
    if not entry:
        return line("entry", "unknown, no address on machine-4")
    return line("entry", f"https://{entry}, HAProxy on machine-4 with TLS and an API key")


def checks_line(log_text: str | None) -> str:
    found = CHECKS.findall(log_text or "")
    if not found:
        return line("checks", "unknown, no check.sh total in the log")
    ok, bad = (int(x) for x in found[-1])
    text = f"{ok} of {ok + bad} passed"
    return line("checks", text if not bad else f"{text}, {bad} failed")


def summary(kube, gate, entry: str | None, log_text: str | None) -> list[str]:
    return [nodes_line(kube), qdrant_line(kube, gate), search_line(kube), entry_line(entry), checks_line(log_text)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kube")
    ap.add_argument("--gate")
    ap.add_argument("--entry", default="")
    ap.add_argument("--check-log")
    args = ap.parse_args(argv)
    log_text = None
    if args.check_log:
        try:
            with open(args.check_log, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        except OSError:
            log_text = None
    for text in summary(load_json(args.kube), load_json(args.gate), args.entry.strip(), log_text):
        print(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
