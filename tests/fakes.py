"""Подставные Qdrant и Ollama для тестов: настоящие HTTP-серверы на 127.0.0.1 в отдельном потоке.

Обработчик получает (метод, путь, заголовки, тело) и возвращает (код, dict или bytes)
или None: тогда сервер принимает соединение и молчит, как выключенная машина.
Только стандартная библиотека.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-read-only-key"


class FakeServer:
    def __init__(self, handler, hang: float = 1.0):
        self.handler, self.hang = handler, hang
        self.requests: list[tuple[str, str, dict, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                res = outer.handler(self.command, self.path, self.headers, body)
                if res is None:  # «мёртвый пир»: соединение принято, ответа нет
                    time.sleep(outer.hang)
                    self.close_connection = True
                    return
                status, payload = res
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def json_bodies(self) -> list:
        return [json.loads(b) if b else None for _, _, _, b in self.requests]

    def paths(self) -> list[str]:
        return [p for _, p, _, _ in self.requests]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def closed_port_url() -> str:
    """Адрес, на котором никто не слушает: соединение сразу получает отказ."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def point(i: int, text: str | None = None, file: str = "cuad/Acme_License.txt", title: str = "Acme License") -> dict:
    """Точка Qdrant в том виде, в каком её отдаёт /points/query с нашим payload."""
    start = i * 500
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "score": round(0.9 - i * 0.01, 4), "payload": {
        "title": title, "section": "12. Governing Law." if i == 0 else "",
        "file": file, "start": start, "end": start + 500,
        "text": text if text is not None else f"Chunk {i}. This Agreement is governed by the laws of Delaware."}}


def qdrant_handler(points: list[dict], status: str = "green"):
    def handler(method, path, headers, body):
        if headers.get("api-key") != KEY:
            return 401, {"status": {"error": "Must provide an API key or an Authorization bearer token"}}
        if method == "GET" and path.startswith("/collections/"):
            return 200, {"result": {"status": status}}
        if method == "POST" and path.endswith("/points/query"):
            return 200, {"result": {"points": points}}
        return 404, {"status": {"error": "not found"}}
    return handler


def ollama_handler(dim: int = 4, chat_content: str = "{}", chat_status: int = 200):
    """Ollama: /api/embed отдаёт одинаковые векторы, /api/chat отдаёт заданный текст ответа."""
    def handler(method, path, headers, body):
        req = json.loads(body or b"{}")
        if method == "POST" and path == "/api/embed":
            inputs = req["input"] if isinstance(req["input"], list) else [req["input"]]
            return 200, {"model": req["model"], "embeddings": [[0.5] * dim for _ in inputs]}
        if method == "POST" and path == "/api/chat":
            if chat_status != 200:
                return chat_status, {"error": "busy"}
            return 200, {"model": req["model"], "created_at": "2026-11-19T10:00:00Z",
                         "message": {"role": "assistant", "content": chat_content}, "done": True,
                         "done_reason": "stop", "total_duration": 2_000_000_000, "load_duration": 1_000_000,
                         "prompt_eval_count": 900, "prompt_eval_duration": 500_000_000,
                         "eval_count": 120, "eval_duration": 1_500_000_000}
        return 404, {"error": "not found"}
    return handler
