"""Фильтр перед Ollama на Маке: из сети стенда виден ровно один вызов, векторизация bge-m3.

Зачем: у API Ollama нет аутентификации (в 2026 у него были CVE-2026-7482 и CVE-2026-5757),
а «заражённый под» поиска — как раз тот, кому к Ollama можно. Поэтому:
- слушаем только адрес моста socket_vmnet (192.168.105.1), а не все интерфейсы:
  из домашней сети фильтр не виден;
- пропускаем только POST /api/embed с моделью bge-m3, с лимитами на размер тела,
  число текстов и длину текста; наверх уходят только разрешённые поля;
- всё остальное (pull, create, push, generate, tags, delete…) получает 403,
  и каждая такая попытка пишется в журнал (пригодится для сцены «заражённый под»).
Только стандартная библиотека. Запуск на Маке, от своего пользователя:
    python3 -I -B deploy/host/ollama_filter.py --listen 192.168.105.1
"""
from __future__ import annotations

import argparse
import http.server
import json
import socket
import sys
import time
import urllib.request

MODEL = "bge-m3"
MAX_BODY = 256 * 1024
MAX_INPUTS = 64
MAX_CHARS = 8000
UPSTREAM = "http://127.0.0.1:11434"


def log(event: str, **kw) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    print(json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "embed-filter"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # свой журнал вместо стандартного
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, reason: str) -> None:
        log("denied", client=self.client_address[0], method=self.command, path=self.path, code=code, reason=reason)
        self._reply(code, json.dumps({"error": reason}).encode())

    def do_POST(self) -> None:
        if self.path != "/api/embed":
            return self._deny(403, "разрешена только векторизация (/api/embed)")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._deny(413, "пустое или слишком большое тело")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._deny(400, "тело не JSON")
        if not isinstance(body, dict) or body.get("model") != MODEL:
            return self._deny(403, f"разрешена только модель {MODEL}")
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if (not isinstance(inputs, list) or not inputs or len(inputs) > MAX_INPUTS
                or not all(isinstance(t, str) and 0 < len(t) <= MAX_CHARS for t in inputs)):
            return self._deny(400, "input: от 1 до 64 строк длиной до 8000 знаков")
        forward = json.dumps({"model": MODEL, "input": inputs, "truncate": True}).encode()
        req = urllib.request.Request(UPSTREAM + "/api/embed", data=forward,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except Exception as e:  # Ollama недоступна: сервис поиска уйдёт в свой ответ 503
            log("upstream_error", client=self.client_address[0], error=type(e).__name__)
            return self._reply(502, json.dumps({"error": "векторизатор недоступен"}).encode())
        self._reply(200, data)

    def _deny_any(self) -> None:
        self._deny(403, "метод или путь запрещён")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _deny_any


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="192.168.105.1")
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--wait", type=int, default=120, help="сколько секунд ждать появления адреса моста")
    args = ap.parse_args()
    if args.listen in ("0.0.0.0", "", "::"):
        sys.exit("нельзя слушать все интерфейсы: фильтр должен быть виден только из сети стенда")
    deadline = time.time() + args.wait
    while True:  # мост 192.168.105.1 появляется, когда стартует первая машина сети shared
        try:
            srv = Server((args.listen, args.port), Handler)
            break
        except OSError as e:
            if time.time() > deadline:
                sys.exit(f"не удалось занять {args.listen}:{args.port}: {e}")
            time.sleep(2)
    log("listening", address=f"{args.listen}:{args.port}", upstream=UPSTREAM, model=MODEL,
        host=socket.gethostname())
    srv.serve_forever()


if __name__ == "__main__":
    main()
