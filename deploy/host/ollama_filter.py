"""Фильтр перед Ollama на Маке: из сети стенда видны ровно два вызова, векторизация двумя моделями
векторов (bge-m3 и snowflake-arctic-embed2, вторая для сцены смены модели) и чат модели ответа qwen3:8b.

Зачем: у API Ollama нет аутентификации (в 2026 у него были CVE-2026-7482 и CVE-2026-5757),
а «заражённый под» поиска — как раз тот, кому к Ollama можно. Поэтому:
- слушаем только адрес моста socket_vmnet (192.168.105.1), а не все интерфейсы:
  из домашней сети фильтр не виден;
- пропускаем только POST /api/embed с моделью из EMBED_MODELS и POST /api/chat с моделью qwen3:8b,
  с лимитами на размер тела, число и длину текстов; наверх уходят только разрешённые поля;
- чат держит память и видеоядро Мака, поэтому его фильтр ограничивает строже: контекст модели
  фиксирован (num_ctx), длина ответа ограничена, keep_alive, инструменты и картинки запрещены,
  к модели один запрос за раз (остальные сразу получают 429);
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
import threading
import time
import urllib.request

EMBED_MODELS = ("bge-m3", "snowflake-arctic-embed2")  # «имя:latest» в Ollama та же модель
MAX_BODY = 256 * 1024
MAX_INPUTS = 64
MAX_CHARS = 8000
UPSTREAM = "http://127.0.0.1:11434"

CHAT_MODEL = "qwen3:8b"
CHAT_MAX_BODY = 64 * 1024
CHAT_MAX_MESSAGES = 4
CHAT_MAX_CHARS = 16000      # все сообщения вместе; промпт сервиса около 5 тыс. знаков при k = 8
CHAT_MAX_PREDICT = 512      # предел длины ответа в токенах
CHAT_NUM_CTX = 4096         # контекст фиксирован: больший раздувает память модели на Маке
CHAT_TIMEOUT = 120          # меньше, чем ждёт сервис поиска (130 с): слот не держим зря
CHAT_FIELDS = {"model", "messages", "options", "format", "think", "stream"}
CHAT_OPTIONS = {  # имя: (тип, минимум, максимум); num_ctx фильтр всё равно ставит свой
    "temperature": (float, 0, 2), "top_p": (float, 0, 1), "top_k": (int, 1, 100),
    "seed": (int, 0, 2**31 - 1), "num_predict": (int, 1, CHAT_MAX_PREDICT), "num_ctx": (int, 1, 1 << 20),
}
CHAT_SLOT = threading.BoundedSemaphore(1)


def embed_model(name: object) -> str | None:
    """Разрешённая модель векторов без тега «:latest» или None."""
    if not isinstance(name, str):
        return None
    name = name[:-len(":latest")] if name.endswith(":latest") else name
    return name if name in EMBED_MODELS else None


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

    def _json_body(self, limit: int) -> dict | None:
        """Тело запроса как объект JSON; при ошибке отвечает сам и возвращает None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            self._deny(413, "пустое или слишком большое тело")
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._deny(400, "тело не JSON")
            return None
        if not isinstance(body, dict):
            self._deny(400, "тело не объект JSON")
            return None
        return body

    def _upstream(self, path: str, forward: dict, timeout: int) -> bytes | None:
        req = urllib.request.Request(UPSTREAM + path, data=json.dumps(forward).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # Ollama недоступна: сервис поиска уйдёт в свой ответ 503
            log("upstream_error", client=self.client_address[0], path=path, error=type(e).__name__)
            return None

    def do_POST(self) -> None:
        if self.path == "/api/embed":
            return self._embed()
        if self.path == "/api/chat":
            return self._chat()
        return self._deny(403, "разрешены только векторизация (/api/embed) и чат модели ответа (/api/chat)")

    def _embed(self) -> None:
        body = self._json_body(MAX_BODY)
        if body is None:
            return
        model = embed_model(body.get("model"))
        if model is None:
            return self._deny(403, f"разрешены только модели {', '.join(EMBED_MODELS)}")
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if (not isinstance(inputs, list) or not inputs or len(inputs) > MAX_INPUTS
                or not all(isinstance(t, str) and 0 < len(t) <= MAX_CHARS for t in inputs)):
            return self._deny(400, "input: от 1 до 64 строк длиной до 8000 знаков")
        data = self._upstream("/api/embed", {"model": model, "input": inputs, "truncate": True}, 30)
        if data is None:
            return self._reply(502, json.dumps({"error": "embedder unavailable"}).encode())
        self._reply(200, data)

    def _chat(self) -> None:
        body = self._json_body(CHAT_MAX_BODY)
        if body is None:
            return
        if body.get("model") != CHAT_MODEL:
            return self._deny(403, f"чат разрешён только с моделью {CHAT_MODEL}")
        extra = sorted(set(body) - CHAT_FIELDS)
        if extra:  # keep_alive, tools, images, template, raw и прочее
            return self._deny(400, f"поля не разрешены: {', '.join(extra)}")
        msgs = body.get("messages")
        if (not isinstance(msgs, list) or not 0 < len(msgs) <= CHAT_MAX_MESSAGES
                or not all(isinstance(m, dict) and set(m) <= {"role", "content"}
                           and m.get("role") in ("system", "user", "assistant")
                           and isinstance(m.get("content"), str) and m["content"] for m in msgs)
                or sum(len(m["content"]) for m in msgs) > CHAT_MAX_CHARS):
            return self._deny(400, "messages: от 1 до 4 сообщений system, user или assistant, всего до 16000 знаков")
        options = body.get("options", {})
        if not isinstance(options, dict) or set(options) - set(CHAT_OPTIONS):
            return self._deny(400, "options: разрешены temperature, top_p, top_k, seed, num_predict, num_ctx")
        clean = {}
        for name, value in options.items():
            kind, lo, hi = CHAT_OPTIONS[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or (kind is int and not isinstance(value, int)) or not lo <= value <= hi):
                return self._deny(400, f"options.{name} вне допустимого")
            clean[name] = value
        clean["num_ctx"] = CHAT_NUM_CTX
        clean.setdefault("num_predict", CHAT_MAX_PREDICT)
        fmt = body.get("format")
        if fmt is not None and fmt != "json" and not isinstance(fmt, dict):
            return self._deny(400, "format: \"json\" или схема JSON")
        think = body.get("think", False)
        if not isinstance(think, bool):
            return self._deny(400, "think: true или false")
        if body.get("stream", False) is not False:
            return self._deny(400, "потоковый ответ не поддерживается")
        forward = {"model": CHAT_MODEL, "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
                   "stream": False, "think": think, "options": clean}
        if fmt is not None:
            forward["format"] = fmt
        if not CHAT_SLOT.acquire(blocking=False):
            log("busy", client=self.client_address[0], path=self.path)
            return self._reply(429, json.dumps({"error": "модель занята"}).encode())
        t0 = time.monotonic()
        try:
            data = self._upstream("/api/chat", forward, CHAT_TIMEOUT)
        finally:
            CHAT_SLOT.release()
        if data is None:
            return self._reply(502, json.dumps({"error": "модель недоступна"}).encode())
        log("chat", client=self.client_address[0], chars=sum(len(m["content"]) for m in msgs),
            seconds=round(time.monotonic() - t0, 1))
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
    log("listening", address=f"{args.listen}:{args.port}", upstream=UPSTREAM, embed_models=list(EMBED_MODELS),
        chat_model=CHAT_MODEL, host=socket.gethostname())
    srv.serve_forever()


if __name__ == "__main__":
    main()
