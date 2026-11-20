"""Заглушка входа стенда: репетиция сцены поиска без стенда (server.py --stub).

GET /search — точный поиск по замороженным векторам мини-набора: те же куски и векторы
(cache/stage1/naive500_title), что загружены на стенде в contracts_rf2. Вектор вопроса мини-набора
берётся из cache/stage1/query_vectors.f32.

Выбор договора и гейт той же логикой, что в сервисе (app/api.py, функции app/routing.py, те же
параметры): вопрос с «;» делится на ссылку на договор и сам вопрос, ссылка выбирает договор по
векторам договоров (cache/stage1/naive500_title_docs, формат в deploy/qdrant/docs.py), куски ищутся
точно внутри него с соседями. Ссылка не похожа ни на один договор базы — «this contract is not in the
database», кусков нет, модель не зовётся. Векторы ссылки, самого вопроса и вопроса не из мини-набора
считает Ollama на Маке (app/embedding.py, bge-m3, кэш на диске). Ollama недоступна, а в кэше вектора
нет — заглушка работает по-старому: поиск по всем кускам без поля route, вопрос не из мини-набора
получает 503.

GET /answer — модель не зовётся: ответ собирается из первого куска и проходит ту же проверку цитат,
что в сервисе (app/answer.py); отказ гейта отвечает, как сервис. Каждый ответ помечен "stub": true,
и страница показывает плашку SIMULATED DATA. Слушает только 127.0.0.1, ключ API проверяет, как вход
стенда (заголовок X-API-Key).
"""
from __future__ import annotations

import heapq
import hmac
import http.client
import json
import math
import operator
import os
import socketserver
import threading
import time
import urllib.parse
import urllib.request
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import answer as answering
from app import embed_models
from app import routing
from showcase.truth import norm_query

ANSWER_MODES = ("answered", "refused", "unsupported", "busy", "invalid")
SERVED_BY = {"node": "stub", "pod": "local"}
MODEL = "bge-m3"  # модель векторов кэша замера (cache/stage1/meta.json)
# выбор договора: значения сервиса по умолчанию (app/api.py), подобраны на половине A мини
ROUTE_DOCS = 5        # сколько договоров маршрута держать в запасе
ROUTE_MIX = 0.0       # доля всего вопроса в балле куска, как в сервисе (app/api.py, ROUTE_MIX)
ROUTE_NEIGHBOR = 0.5  # μ: вес лучшего соседа куска
NOT_IN_BASE = "this contract is not in the database"  # как у сервиса
EMBED_TIMEOUT = 8     # с на векторы у Ollama: витрина ждёт ответ входа 10 с (server.py --timeout)

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


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


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
        self.by_file: dict[str, list[int]] = {}  # куски договора: поиск внутри него идёт только по ним
        for i, p in enumerate(self.points):
            self.by_file.setdefault(p["file"], []).append(i)
        with open(os.path.join(cache_dir, "queries.jsonl"), encoding="utf-8") as f:
            queries = [json.loads(line)["query"] for line in f if line.strip()]
        self.query_vectors = _read_f32(os.path.join(cache_dir, "query_vectors.f32"))
        if len(self.query_vectors) != len(queries) * self.dim:
            raise ValueError(f"{cache_dir}: векторы вопросов не той длины")
        self.queries: dict[str, int] = {}
        for i, q in enumerate(queries):
            self.queries.setdefault(norm_query(q), i)

    def query_vector(self, q: str):
        """Вектор вопроса мини-набора из кэша замера; None, если такого вопроса там нет."""
        i = self.queries.get(norm_query(q))
        return None if i is None else memoryview(self.query_vectors)[i * self.dim:(i + 1) * self.dim]

    def scores(self, qv, rows) -> list[float]:
        """Косинус вектора qv с кусками rows (как Qdrant с расстоянием Cosine)."""
        qn = math.sqrt(_dot(qv, qv)) or 1.0
        mv, dim = memoryview(self.vectors), self.dim
        return [_dot(mv[j * dim:(j + 1) * dim], qv) / (self.norms[j] * qn) for j in rows]

    def hit(self, score: float, j: int) -> dict:
        """Кусок в форме ответа /search сервиса."""
        p = self.points[j]
        return {"score": round(score, 4), "document": p["title"], "section": p["section"] or None,
                "file": p["file"], "span": [p["start"], p["end"]], "text": p["text"]}

    def top(self, qv, k: int) -> list[dict]:
        """Top-k по всем кускам."""
        scores = self.scores(qv, range(len(self.points)))
        return [self.hit(scores[j], j) for j in heapq.nlargest(k, range(len(scores)), key=scores.__getitem__)]

    def search(self, q: str, k: int) -> list[dict] | None:
        """Top-k в форме ответа /search сервиса; None — вектора вопроса в кэше нет."""
        qv = self.query_vector(q)
        return None if qv is None else self.top(qv, k)


class StubDocs:
    """Договоры для выбора договора и гейта: <набор>_docs из deploy/qdrant/docs.py build (points.jsonl,
    route.f32, gate.f32, meta.json). route — средний вектор кусков договора (у сервиса Cosine),
    gate — (вектор «Document: <название>» + вектор первого куска) / 2 (у сервиса Dot)."""

    def __init__(self, docs_dir: str, dim: int) -> None:
        with open(os.path.join(docs_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        model = embed_models.base_name(str(meta.get("model") or ""))
        if model != MODEL or meta.get("dim") != dim:
            raise ValueError(f"{docs_dir}: векторы {model or '?'} размерности {meta.get('dim')}, "
                             f"а у кусков {MODEL} размерности {dim}")
        with open(os.path.join(docs_dir, "points.jsonl"), encoding="utf-8") as f:
            self.docs = [json.loads(line) for line in f if line.strip()]
        self.route = _read_f32(os.path.join(docs_dir, "route.f32"))
        self.gate = _read_f32(os.path.join(docs_dir, "gate.f32"))
        n = len(self.docs)
        if not n or len(self.route) != n * dim or len(self.gate) != n * dim:
            raise ValueError(f"{docs_dir}: векторов не столько, сколько договоров")
        self.dim = dim
        self.route_norms = [math.sqrt(_dot(self._row(self.route, i), self._row(self.route, i))) or 1.0
                            for i in range(n)]

    def _row(self, vec: array, i: int):
        return memoryview(vec)[i * self.dim:(i + 1) * self.dim]

    def by_route(self, ref: list[float], n: int) -> list[tuple[float, int]]:
        """n договоров, ближайших к нормированной ссылке по косинусу, по убыванию балла."""
        scores = [_dot(self._row(self.route, i), ref) / self.route_norms[i] for i in range(len(self.docs))]
        return [(scores[i], i) for i in heapq.nlargest(n, range(len(scores)), key=scores.__getitem__)]

    def by_gate(self, ref: list[float]) -> tuple[float, int]:
        """Лучший балл гейта: скалярное произведение нормированной ссылки с вектором gate, то есть
        среднее двух косинусов (с названием и с первым куском договора)."""
        scores = [_dot(self._row(self.gate, i), ref) for i in range(len(self.docs))]
        best = max(range(len(scores)), key=scores.__getitem__)
        return scores[best], best


class StubRouter:
    """Выбор договора и гейт, как app/api._routed: те же функции app/routing.py и те же параметры.
    Отличие одно: поиск по договорам и внутри договора точный, в памяти, без Qdrant."""

    def __init__(self, index: StubIndex, docs: StubDocs, embedder) -> None:
        self.index, self.docs, self.embedder = index, docs, embedder
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Векторы текстов (тот же текст, что уходит в модель у сервиса) через Ollama с кэшем на диске;
        None, если вектора нет в кэше, а Ollama недоступна."""
        texts = [embed_models.query_text(MODEL, t) for t in texts]
        with self._lock:  # кэш векторов пишет один поток, читает через один открытый файл
            try:
                self.embedder.ensure(texts, progress=False)
                return [list(self.embedder.vector(t)) for t in texts]
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                return None

    @staticmethod
    def threshold() -> float | None:
        """Порог гейта, как у сервиса: замеренный для bge-m3, GATE_MIN в окружении перекрывает."""
        return routing.gate_min(MODEL)

    def route(self, full, ref, ask, parts: tuple[str, str], k: int) -> tuple[list[dict], dict] | None:
        """(куски, маршрут) или None, если договоров нет: тогда прежний поиск по всем кускам."""
        full, ref, ask = routing.unit(list(full)), routing.unit(list(ref)), routing.unit(list(ask))
        threshold = self.threshold()
        found = self.docs.by_route(ref, ROUTE_DOCS)
        route: dict = {"used": True, "reference": parts[0], "question": parts[1], "document": None, "gate": None}
        if threshold is not None:
            score, best = self.docs.by_gate(ref)
            route["gate"] = {"score": round(score, 4), "threshold": threshold, "passed": score >= threshold,
                             "closest": self.docs.docs[best].get("title")}
            if score < threshold:  # этого договора в базе нет: куски не ищем
                return [], route
        if not found:
            return None
        top_score, top = found[0]
        d = self.docs.docs[top]
        route["document"] = {"file": d["file"], "title": d.get("title"), "score": round(top_score, 4)}
        query = routing.mix(full, ask, ROUTE_MIX)
        chosen: list[tuple[float, int]] = []
        for _, i in found:  # договор за договором, пока кусков меньше k
            rows = self.index.by_file.get(self.docs.docs[i]["file"], [])
            points = [{"id": j, "score": s, "payload": {"start": self.index.points[j]["start"]}}
                      for j, s in zip(rows, self.index.scores(query, rows))]
            chosen += routing.rank_in_doc(points, ROUTE_NEIGHBOR)[:k - len(chosen)]
            if len(chosen) >= k:
                break
        return [self.index.hit(s, j) for s, j in chosen], route

    def close(self) -> None:
        with self._lock:
            self.embedder.close()


def open_router(index: StubIndex, cache_dir: str, ollama_url: str, embed_cache: str,
                strategy: str = "naive500_title") -> StubRouter:
    """Договоры из <cache_dir>/<strategy>_docs, векторы текстов через Ollama с кэшем в embed_cache.
    Нет папки договоров или она не того вида — OSError или ValueError, заглушка тогда без выбора договора."""
    from app.embedding import OllamaEmbedder
    docs = StubDocs(os.path.join(cache_dir, strategy + "_docs"), index.dim)
    return StubRouter(index, docs, OllamaEmbedder(model=MODEL, url=ollama_url, cache_dir=embed_cache,
                                                  timeout=EMBED_TIMEOUT))


def ollama_reachable(url: str, timeout: float = 1.0) -> bool:
    """Отвечает ли Ollama (GET /api/version); только для строки журнала при запуске."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _refused(route: dict | None) -> bool:
    return bool(route and route.get("gate") and not route["gate"]["passed"])


class _Loopback(ThreadingHTTPServer):
    """Без обратного DNS при запуске (как у панели): getfqdn не нужен, адрес всегда 127.0.0.1."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


class StubEntry:
    """HTTP-заглушка входа на 127.0.0.1:случайный порт: /search, /answer, /healthz.
    router — выбор договора и гейт (open_router); без него поиск по всем кускам, как раньше."""

    def __init__(self, index, key: str, answer_mode: str = "answered", router: StubRouter | None = None) -> None:
        if answer_mode not in ANSWER_MODES:
            raise ValueError(f"answer_mode: одно из {', '.join(ANSWER_MODES)}")
        self.index, self.key, self.answer_mode, self.router = index, key, answer_mode, router
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
        if self.router is not None:
            self.router.close()

    # --- поиск -----------------------------------------------------------------------------
    def _retrieve(self, q: str, k: int) -> tuple[list[dict], dict | None, dict] | None:
        """(куски, маршрут, время по шагам в мс), как _retrieve сервиса. Маршрут None: вопрос без «;»,
        выбора договора нет или векторов частей вопроса не достать. None: вектора вопроса нет ни в кэше
        замера, ни у Ollama."""
        t0 = time.perf_counter()
        full = self.index.query_vector(q)
        parts = routing.split_question(q) if self.router is not None else None
        ref = ask = None
        texts = ([] if full is not None else [q]) + (list(parts) if parts else [])
        if texts and self.router is not None:
            got = self.router.embed(texts)
            if got is not None:
                if full is None:
                    full = got.pop(0)
                if parts:
                    ref, ask = got
        if full is None:
            return None
        t1 = time.perf_counter()
        routed = self.router.route(full, ref, ask, parts, k) if ref is not None else None
        hits, route = routed if routed is not None else (self.index.top(full, k), None)
        return hits, route, {"embed": _ms(t1 - t0), "search": _ms(time.perf_counter() - t1)}

    # --- ответы ----------------------------------------------------------------------------
    def _base(self, q: str) -> dict:
        return {"query": q, "collection": "contracts_rf2 · local stub", "qdrant_attempts": 1,
                "qdrant_route": "local", "served_by": dict(SERVED_BY), "stub": True}

    def _no_vector(self) -> tuple[int, dict]:
        return 503, {"error": "stub has no vector for this question (not cached, Ollama not reachable)",
                     "served_by": dict(SERVED_BY)}

    def search(self, q: str, k: int) -> tuple[int, dict]:
        found = self._retrieve(q, k)
        if found is None:
            return self._no_vector()
        hits, route, timing = found
        out = {**self._base(q), "model": MODEL, "mode": "dense", "timing_ms": timing, "results": hits}
        if route is not None:
            out["route"] = route
            if _refused(route):
                out.update(not_in_base=True, message=NOT_IN_BASE)
        return 200, out

    def answer(self, q: str, k: int) -> tuple[int, dict]:
        found = self._retrieve(q, k)
        if found is None:
            return self._no_vector()
        hits, route, timing = found
        gated = _refused(route)
        mode = self.answer_mode
        if gated:  # договора нет в базе: модель не зовётся, как в сервисе
            checked = {"status": "refused", "citations": [], "answer": answering.not_in_base(q)}
        elif mode == "busy":
            return 429, {"error": "stub: model busy", "served_by": dict(SERVED_BY)}
        elif mode == "invalid":
            return 502, {"error": "stub: model reply not in the expected format", "served_by": dict(SERVED_BY)}
        else:
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
            timing["generate"] = 0.0
        out = {**self._base(q), "model": "stub, no model", **checked, "sources": hits,
               "timing_ms": timing, "tokens": {"prompt": None, "output": None}}
        if route is not None:
            out["route"] = route
            if gated:
                out["not_in_base"] = True
        return 200, out

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
