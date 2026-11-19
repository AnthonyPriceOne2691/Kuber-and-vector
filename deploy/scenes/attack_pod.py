"""Сцена «заражённый под»: что может атакующий с оболочкой в поде поиска. Работает внутри пода
(его запускает deploy/scenes/infected-pod.sh через kubectl exec, код идёт по stdin), только
стандартная библиотека. Ключ пода (только на чтение) приходит в переменной KEY, адреса соседних
машин аргументами. Каждая попытка печатает строку «время<TAB>попытка<TAB>итог<TAB>отбито|прошло».
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

TIMEOUT = 3.0
HOST = "192.168.105.1"  # Мак в сети машин


def tcp(host: str, port: int) -> tuple[str, bool]:
    t = time.time()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return "соединение установлено", False
    except OSError as e:
        return f"нет соединения ({type(e).__name__}, {time.time() - t:.1f} с)", True


def dns(name: str) -> tuple[str, bool]:
    box: list = []
    th = threading.Thread(target=lambda: box.append(_resolve(name)), daemon=True)
    th.start()
    th.join(TIMEOUT + 1)
    if not box:
        return f"нет ответа за {TIMEOUT + 1:.0f} с", True
    return box[0]


def _resolve(name: str) -> tuple[str, bool]:
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)}
        return f"разрешилось: {', '.join(sorted(addrs))}", False
    except OSError as e:
        return f"не разрешилось ({type(e).__name__})", True


def http(method: str, url: str, key: str = "", data: bytes | None = None) -> tuple[int, str]:
    headers = {"api-key": key} if key else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read(200).decode("utf-8", "replace")
    except OSError as e:
        return 0, type(e).__name__


def main(argv: list[str]) -> int:
    key = os.environ.get("KEY", "").strip()
    qdrant = os.environ.get("QDRANT_URL", "").rstrip("/")
    neighbors = argv[1:]
    out = sys.stdout

    def report(what: str, result: tuple[str, bool]) -> None:
        text, blocked = result
        out.write(f"{time.time():.3f}\t{what}\t{text}\t{'отбито' if blocked else 'прошло'}\n")
        out.flush()

    out.write(f"{time.time():.3f}\tначало\t{socket.gethostname()}\t-\n")
    report("вынести данные в интернет (1.1.1.1:443)", tcp("1.1.1.1", 443))
    report("имя в DNS (example.com)", dns("example.com"))
    sa = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    report("токен сервисного аккаунта Kubernetes", ("есть", False) if os.path.exists(sa) else ("нет в поде", True))
    report("API Kubernetes изнутри (10.43.0.1:443)", tcp("10.43.0.1", 443))
    for ip in neighbors:
        report(f"соседняя машина {ip}: API k3s 6443", tcp(ip, 6443))
        report(f"соседняя машина {ip}: kubelet 10250", tcp(ip, 10250))
        report(f"соседняя машина {ip}: etcd 2379", tcp(ip, 2379))
    report("Мак: AirPlay 7000", tcp(HOST, 7000))
    code, body = http("GET", f"http://{HOST}:11434/api/tags")
    report("Мак: Ollama мимо фильтра (/api/tags)", (f"HTTP {code}", code != 200))
    if qdrant and key:
        code, body = http("DELETE", f"{qdrant}/collections/contracts_rf2", key)
        report("стереть базу ключом пода (DELETE contracts_rf2)", (f"HTTP {code}", code in (401, 403)))
        # пустая запись: с правом на запись Qdrant ответил бы 200, с ключом на чтение 403
        code, body = http("PUT", f"{qdrant}/collections/contracts_rf2/points?wait=true", key, b'{"points": []}')
        report("записать в базу ключом пода (PUT points)", (f"HTTP {code}", code in (401, 403)))
        code, body = http("GET", f"{qdrant}/collections/contracts_rf2", key)
        # честный остаток: ключ на чтение украден вместе с подом, читать можно, вынести некуда
        report("прочитать базу ключом пода", (f"HTTP {code}", code != 200))
    out.write(f"{time.time():.3f}\tконец\t-\t-\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
