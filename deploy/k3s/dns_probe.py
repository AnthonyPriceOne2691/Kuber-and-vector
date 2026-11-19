"""Проба DNS кластера с машины (deploy/check.sh): имя кластера отвечает, чужое имя сразу получает отказ.
Один запрос A по UDP, только стандартная библиотека.
  python3 -I -B dns_probe.py kubernetes.default.svc.cluster.local    код 0, если пришёл ответ с адресом
  python3 -I -B dns_probe.py --refused example.com                    код 0, если отказ пришёл быстрее --fast-ms
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import time

CLUSTER_DNS = "10.43.0.10"  # сервис kube-dns в k3s по умолчанию
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


def query(name: str, txid: int) -> bytes:
    labels = [p for p in name.rstrip(".").split(".")]
    if not labels or any(not p or len(p) > 63 for p in labels):
        raise ValueError(f"плохое имя: {name!r}")
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in labels) + b"\0"
    return struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)


def parse(data: bytes, txid: int) -> tuple[int, int]:
    """(rcode, число ответов); ValueError, если это не ответ на наш запрос."""
    if len(data) < 12:
        raise ValueError("короткий ответ")
    rid, flags, _, ancount = struct.unpack(">HHHH", data[:8])
    if rid != txid or not flags & 0x8000:
        raise ValueError("чужой пакет")
    return flags & 0x000F, ancount


def ask(name: str, server: str = CLUSTER_DNS, port: int = 53, timeout: float = 3.0) -> tuple[int, int, float]:
    """(rcode, ответов, мс); rcode -1 значит ответа не было до таймаута."""
    txid = int.from_bytes(os.urandom(2), "big")
    t0 = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query(name, txid), (server, port))
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                return -1, 0, (time.monotonic() - t0) * 1000
            try:
                rcode, ancount = parse(data, txid)
            except ValueError:
                continue
            return rcode, ancount, (time.monotonic() - t0) * 1000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Проба DNS кластера")
    ap.add_argument("name")
    ap.add_argument("--server", default=CLUSTER_DNS)
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--refused", action="store_true", help="ждём отказ, и быстрый")
    ap.add_argument("--fast-ms", type=float, default=1000.0)
    args = ap.parse_args(argv)
    rcode, answers, ms = ask(args.name, args.server, args.port, args.timeout)
    print(f"{args.name}: {RCODES.get(rcode, 'нет ответа' if rcode < 0 else rcode)}, ответов {answers}, {ms:.0f} мс")
    if args.refused:
        return 0 if rcode > 0 and ms < args.fast_ms else 1
    return 0 if rcode == 0 and answers > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
