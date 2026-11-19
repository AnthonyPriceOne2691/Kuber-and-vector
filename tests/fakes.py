"""Подставные Qdrant и Ollama для тестов: настоящие HTTP-серверы на 127.0.0.1 в отдельном потоке.

Обработчик получает (метод, путь, заголовки, тело) и возвращает (код, dict или bytes)
или None: тогда сервер принимает соединение и молчит, как выключенная машина.
Только стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-read-only-key"
ADMIN_KEY = "test-admin-key"


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

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

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


# --- смена модели: векторы «моделей» и Qdrant в памяти ------------------------------------------
def model_vector(model: str, text: str, dim: int = 16) -> list[float]:
    """Вектор «модели»: детерминирован по (модель, текст); у разных моделей и текстов почти ортогонален."""
    rng = random.Random(hashlib.sha256(f"{model}\0{text}".encode()).digest())
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def model_ollama_handler(dim: int = 16, allowed: tuple[str, ...] | None = None):
    """Ollama, у которой вектор зависит от модели и текста; allowed: какие модели пускает (как фильтр)."""
    def handler(method, path, headers, body):
        req = json.loads(body or b"{}")
        if method == "POST" and path == "/api/embed":
            if allowed is not None and req.get("model") not in allowed:
                return 403, {"error": "model not allowed"}
            inputs = req["input"] if isinstance(req["input"], list) else [req["input"]]
            return 200, {"model": req["model"], "embeddings": [model_vector(req["model"], t, dim) for t in inputs]}
        return 404, {"error": "not found"}
    return handler


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _pick(d: dict | None, sel) -> dict | None:
    """with_payload / with_vector: true, false или список имён."""
    if not sel or d is None:
        return None
    return dict(d) if sel is True else {k: v for k, v in d.items() if k in sel}


class MemoryQdrant:
    """Qdrant в памяти: коллекции, точки, алиасы, поиск косинусом по «dense». Два ключа, как на стенде:
    админский пишет, ключ на чтение только читает (запись 403). Имя коллекции в пути может быть
    алиасом, как у Qdrant. Удаление несуществующего алиаса и создание уже существующего дают ошибку:
    так тесты ловят переключение не одним телом «delete + create»."""

    WRITES = ("/points/delete", "/collections/aliases")

    def __init__(self, admin_key: str = ADMIN_KEY, read_key: str = KEY) -> None:
        self.admin_key, self.read_key = admin_key, read_key
        self.cols: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.alias_requests: list[list[dict]] = []

    def name(self, raw: str) -> str:
        return self.aliases.get(raw, raw)

    def __call__(self, method, path, headers, body):
        key = headers.get("api-key")
        if key not in (self.admin_key, self.read_key):
            return 401, {"status": {"error": "Must provide an API key"}}
        path = urllib.parse.urlsplit(path).path
        write = method in ("PUT", "DELETE", "PATCH") or path.endswith(self.WRITES)
        if write and key != self.admin_key:
            return 403, {"status": {"error": "read-only key"}}
        req = json.loads(body) if body else {}
        if path == "/aliases" and method == "GET":
            return 200, {"result": {"aliases": [{"alias_name": a, "collection_name": c}
                                                for a, c in self.aliases.items()]}}
        if path == "/collections/aliases" and method == "POST":
            return self._aliases(req["actions"])
        if path == "/collections" and method == "GET":
            return 200, {"result": {"collections": [{"name": n} for n in self.cols]}}
        parts = path.split("/")[2:]  # ["имя", "points", ...]
        if not parts or not parts[0]:
            return 404, {"status": {"error": "not found"}}
        raw, rest = parts[0], "/".join(parts[1:])
        name = self.name(raw)
        if rest == "" and method == "PUT":
            if raw in self.cols:
                return 400, {"status": {"error": f"Collection `{raw}` already exists!"}}
            self.cols[raw] = {"config": req, "points": {}}
            return 200, {"result": True}
        col = self.cols.get(name)
        if col is None:
            return 404, {"status": {"error": f"Collection `{raw}` doesn't exist!"}}
        if rest == "" and method == "GET":
            cfg = col["config"]
            return 200, {"result": {"status": "green", "points_count": len(col["points"]),
                                    "config": {"params": {"vectors": cfg.get("vectors"),
                                                          "shard_number": cfg.get("shard_number"),
                                                          "replication_factor": cfg.get("replication_factor")}}}}
        if rest == "" and method == "DELETE":
            del self.cols[name]
            self.aliases = {a: c for a, c in self.aliases.items() if c != name}
            return 200, {"result": True}
        if rest == "points" and method == "PUT":
            for p in req["points"]:
                col["points"][p["id"]] = {"vector": p.get("vector") or {}, "payload": p.get("payload") or {}}
            return 200, {"result": {"status": "completed"}}
        if rest == "points" and method == "POST":  # точки по id
            out = [{"id": i, "payload": _pick(col["points"][i]["payload"], req.get("with_payload", True)),
                    "vector": _pick(col["points"][i]["vector"], req.get("with_vector", False))}
                   for i in req["ids"] if i in col["points"]]
            return 200, {"result": out}
        if rest == "points/scroll" and method == "POST":
            out = [{"id": i, "payload": _pick(p["payload"], req.get("with_payload", True))}
                   for i, p in col["points"].items()][:req.get("limit", 10)]
            return 200, {"result": {"points": out, "next_page_offset": None}}
        if rest == "points/delete" and method == "POST":
            for i in req["points"]:
                col["points"].pop(i, None)
            return 200, {"result": {"status": "completed"}}
        if rest == "points/query" and method == "POST":
            using, vec = req.get("using", "dense"), req["query"]
            scored = sorted(((_cos(vec, p["vector"][using]), i, p) for i, p in col["points"].items()
                             if using in p["vector"]), key=lambda x: -x[0])[:req.get("limit", 10)]
            return 200, {"result": {"points": [{"id": i, "score": s, "payload": _pick(p["payload"], req.get("with_payload"))}
                                               for s, i, p in scored]}}
        return 404, {"status": {"error": "not found"}}

    def _aliases(self, actions: list[dict]):
        self.alias_requests.append(actions)
        new = dict(self.aliases)
        for act in actions:
            if "delete_alias" in act:
                a = act["delete_alias"]["alias_name"]
                if a not in new:
                    return 404, {"status": {"error": f"Alias {a} does not exist"}}
                del new[a]
            elif "create_alias" in act:
                a, c = act["create_alias"]["alias_name"], act["create_alias"]["collection_name"]
                if c not in self.cols:
                    return 404, {"status": {"error": f"Collection `{c}` doesn't exist!"}}
                if a in new:
                    return 400, {"status": {"error": f"Alias {a} already exists"}}
                new[a] = c
            else:
                return 400, {"status": {"error": "unknown action"}}
        self.aliases = new  # всё тело целиком или ничего
        return 200, {"result": True}
