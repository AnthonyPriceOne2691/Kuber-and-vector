"""Заглушка входа стенда: репетиция сцены поиска без стенда (server.py --stub).

GET /search — точный поиск по замороженным векторам мини-набора: те же куски и векторы
(cache/stage1/naive500_title), что загружены на стенде в contracts_rf2. Вектор вопроса берётся
из cache/stage1/query_vectors.f32, Ollama не зовётся, поэтому заглушка знает только 776 вопросов
мини-набора, на остальные отвечает 503. GET /answer — модель не зовётся: ответ собирается из
первого куска и проходит ту же проверку цитат, что в сервисе (app/answer.py). Каждый ответ
помечен "stub": true, и страница показывает плашку SIMULATED DATA. Слушает только 127.0.0.1,
ключ API проверяет, как вход стенда (заголовок X-API-Key).
"""
from __future__ import annotations

import heapq
import hmac
import json
import math
import operator
import os
import socketserver
import threading
import time
import urllib.parse
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import answer as answering
from showcase.truth import norm_query

ANSWER_MODES = ("answered", "refused", "unsupported", "busy", "invalid")
SERVED_BY = {"node": "stub", "pod": "local"}

if hasattr(math, "sumprod"):  # Python 3.12+: скалярное произведение в C
    _dot = math.sumprod
else:  # pragma: no cover
    def _dot(a, b) -> float:
        return sum(map(operator.mul, a, b))


def _read_f32(path: str) -> array:
    vec = array("f")
    with open(path, "rb") as f:
        vec.frombytes(f.read())
    return vec


class StubIndex:
    """Куски и векторы из кэша замера, точный поиск по косинусу."""

    def __init__(self, cache_dir: str, strategy: str = "naive500_title") -> None:
        d = os.path.join(cache_dir, strategy)
        self.points: list[dict] = []
        with open(os.path.join(d, "points.jsonl"), encoding="utf-8") as f:
            for line in f:
                p = json.loads(line)
                self.points.append({k: p.get(k) for k in ("title", "section", "file", "start", "end", "text")})
        self.vectors = _read_f32(os.path.join(d, "vectors.f32"))
        n = len(self.points)
        if not n or len(self.vectors) % n:
            raise ValueError(f"{d}: число векторов не делится на число кусков")
        self.dim = len(self.vectors) // n
        mv = memoryview(self.vectors)
        self.norms = [math.sqrt(_dot(mv[i * self.dim:(i + 1) * self.dim], mv[i * self.dim:(i + 1) * self.dim])) or 1.0
                      for i in range(n)]
        with open(os.path.join(cache_dir, "queries.jsonl"), encoding="utf-8") as f:
            queries = [json.loads(line)["query"] for line in f if line.strip()]
        self.query_vectors = _read_f32(os.path.join(cache_dir, "query_vectors.f32"))
        if len(self.query_vectors) != len(queries) * self.dim:
            raise ValueError(f"{cache_dir}: векторы вопросов не той длины")
        self.queries: dict[str, int] = {}
        for i, q in enumerate(queries):
            self.queries.setdefault(norm_query(q), i)

    def search(self, q: str, k: int) -> list[dict] | None:
        """Top-k в форме ответа /search сервиса; None — вектора вопроса в кэше нет."""
        i = self.queries.get(norm_query(q))
        if i is None:
            return None
        dim = self.dim
        qv = memoryview(self.query_vectors)[i * dim:(i + 1) * dim]
        qn = math.sqrt(_dot(qv, qv)) or 1.0
        mv = memoryview(self.vectors)
        scores = [_dot(mv[j * dim:(j + 1) * dim], qv) / (self.norms[j] * qn) for j in range(len(self.points))]
        out = []
        for j in heapq.nlargest(k, range(len(scores)), key=scores.__getitem__):
            p = self.points[j]
            out.append({"score": round(scores[j], 4), "document": p["title"], "section": p["section"] or None,
                        "file": p["file"], "span": [p["start"], p["end"]], "text": p["text"]})
        return out


class _Loopback(ThreadingHTTPServer):
    """Без обратного DNS при запуске (как у панели): getfqdn не нужен, адрес всегда 127.0.0.1."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


class StubEntry:
    """HTTP-заглушка входа на 127.0.0.1:случайный порт: /search, /answer, /healthz."""

    def __init__(self, index, key: str, answer_mode: str = "answered") -> None:
        if answer_mode not in ANSWER_MODES:
            raise ValueError(f"answer_mode: одно из {', '.join(ANSWER_MODES)}")
        self.index, self.key, self.answer_mode = index, key, answer_mode
        self.srv = _Loopback(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="showcase-stub", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self.srv.shutdown()
        self.srv.server_close()

    # --- ответы ----------------------------------------------------------------------------
    def _base(self, q: str) -> dict:
        return {"query": q, "collection": "contracts_rf2 · local stub", "qdrant_attempts": 1,
                "qdrant_route": "local", "served_by": dict(SERVED_BY), "stub": True}

    def search(self, q: str, k: int) -> tuple[int, dict]:
        t0 = time.perf_counter()
        hits = self.index.search(q, k)
        if hits is None:
            return 503, {"error": "stub has no cached vector for this question", "served_by": dict(SERVED_BY)}
        took = round((time.perf_counter() - t0) * 1000, 1)
        return 200, {**self._base(q), "model": "bge-m3", "mode": "dense",
                     "timing_ms": {"embed": 0.0, "search": took}, "results": hits}

    def answer(self, q: str, k: int) -> tuple[int, dict]:
        mode = self.answer_mode
        if mode == "busy":
            return 429, {"error": "stub: model busy", "served_by": dict(SERVED_BY)}
        if mode == "invalid":
            return 502, {"error": "stub: model reply not in the expected format", "served_by": dict(SERVED_BY)}
        t0 = time.perf_counter()
        hits = self.index.search(q, k)
        if hits is None:
            return 503, {"error": "stub has no cached vector for this question", "served_by": dict(SERVED_BY)}
        took = round((time.perf_counter() - t0) * 1000, 1)
        if mode == "refused" or not hits:
            reply = {"found": False, "answer": "", "citations": []}
        elif mode == "unsupported":
            reply = {"found": True, "answer": "Simulated answer that no source supports [1].",
                     "citations": [{"n": 1, "quote": "this sentence does not appear in any retrieved source"}]}
        else:
            quote = " ".join(hits[0]["text"].split()[:14])
            reply = {"found": True,
                     "answer": "Simulated answer from the local stub, no model was called. The quote comes from source [1].",
                     "citations": [{"n": 1, "quote": quote}]}
        checked = answering.check(reply, hits, q)  # та же проверка цитат, что в сервисе
        checked.pop("model_answer", None)
        return 200, {**self._base(q), "model": "stub, no model", **checked, "sources": hits,
                     "timing_ms": {"embed": 0.0, "search": took, "generate": 0.0},
                     "tokens": {"prompt": None, "output": None}}

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "showcase-stub/1"
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _json(self, code: int, doc: dict) -> None:
                body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                if u.path == "/healthz":
                    self._json(200, {"ok": True})
                    return
                if u.path not in ("/search", "/answer"):
                    self._json(404, {"error": "not found"})
                    return
                got = self.headers.get("X-API-Key") or ""
                if not hmac.compare_digest(got.encode(), stub.key.encode()):
                    self._json(401, {"error": "API key required"})
                    return
                qs = urllib.parse.parse_qs(u.query)
                q = (qs.get("q") or [""])[0]
                try:
                    k = int((qs.get("k") or ["5"])[0])
                except ValueError:
                    k = 0
                kmax = 20 if u.path == "/search" else 8
                if not 3 <= len(q) <= 500 or not 1 <= k <= kmax:
                    self._json(422, {"detail": "invalid q or k"})
                    return
                self._json(*(stub.search(q, k) if u.path == "/search" else stub.answer(q, k)))

        return Handler
