"""Шлюз к пиру Qdrant своей машины: только чтение состояния, ключ Qdrant не выходит из кластера.

DaemonSet в пространстве qdrant, по поду на машину (deploy/qdrant/gate.yaml). Под ходит в пир
своей машины (сервис qdrant-local, internalTrafficPolicy: Local) с ключом только на чтение
и отдаёт наружу ровно пять путей, только GET:
    /metrics  /cluster  /aliases  /collections/<имя>  /collections/<имя>/cluster
Точки, поиск, scroll, снапшоты и любые записи через шлюз недоступны. Строка запроса не
передаётся. Снаружи шлюз виден только machine-4 (NodePort 30633 и правило сети): Prometheus и
панели там ключ Qdrant не нужен. Только стандартная библиотека.
    python3 -I gate.py            настройки из окружения: QDRANT_URL, KEY_FILE, PORT
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import sys
import time
import urllib.parse

QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
KEY_FILE = os.environ.get("KEY_FILE", "/run/secrets/qdrant/read-only-api-key")
PORT = int(os.environ.get("PORT", "9633"))
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "2"))
MAX_BYTES = 4 * 1024 * 1024
NAME = r"[A-Za-z0-9_-]{1,64}"
ALLOWED = tuple(re.compile(p) for p in (
    r"/metrics", r"/cluster", r"/aliases", rf"/collections/{NAME}", rf"/collections/{NAME}/cluster"))


def allowed(path: str) -> bool:
    return any(p.fullmatch(path) for p in ALLOWED)


def log(event: str, **kw) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}, ensure_ascii=False),
          file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "qdrant-gate"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._reply(200, b'{"ok":true}')
        if not allowed(path):
            log("denied", client=self.client_address[0], path=self.path[:200])
            return self._reply(404, b'{"error":"path not allowed"}')
        try:
            with open(KEY_FILE) as f:
                key = f.read().strip()
        except OSError:
            return self._reply(500, b'{"error":"no qdrant key"}')
        u = urllib.parse.urlsplit(QDRANT_URL)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=TIMEOUT)
        try:
            conn.request("GET", path, headers={"api-key": key, "Accept": self.headers.get("Accept") or "*/*"})
            r = conn.getresponse()
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return self._reply(502, b'{"error":"qdrant answer too large"}')
            self._reply(r.status, body, r.getheader("Content-Type") or "application/octet-stream")
        except (OSError, http.client.HTTPException):
            self._reply(502, b'{"error":"qdrant unavailable"}')
        finally:
            conn.close()

    def _deny_method(self) -> None:
        log("denied", client=self.client_address[0], method=self.command, path=self.path[:200])
        self._reply(405, b'{"error":"only GET"}')

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _deny_method


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    srv = Server(("0.0.0.0", PORT), Handler)
    log("listening", port=PORT, upstream=QDRANT_URL)
    srv.serve_forever()


if __name__ == "__main__":
    main()
