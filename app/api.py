"""Сервис поиска: вопрос -> куски документов с адресом источника, по желанию ответ модели.

Вопрос векторизует Ollama (bge-m3) на Маке, поиск идёт в Qdrant ключом ТОЛЬКО НА ЧТЕНИЕ:
админского ключа у сервиса нет. Ответ своими словами пишет qwen3:8b через тот же фильтр на Маке,
со ссылками на найденные куски (app/answer.py). Наружу только /search, /answer, /healthz и /readyz,
документации API нет. Внешних зависимостей, кроме FastAPI и uvicorn, нет: к Qdrant и Ollama ходим
стандартной библиотекой.

Отказ машины: адрес Qdrant это ClusterIP, каждое новое соединение уходит на случайный пир.
Мёртвый пир молчит, поэтому соединение ждём недолго (QDRANT_CONNECT_TIMEOUT) и повторяем:
следующая попытка почти всегда попадает на живой пир. Поиск без побочных эффектов, повтор безопасен.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app import answer as answering
from app import bm25

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").rstrip("/")
COLLECTION = os.environ.get("COLLECTION", "lbr_v2_naive500_title")  # лучшая ветка замера 18.11
SEARCH_MODE = os.environ.get("SEARCH_MODE", "dense")  # dense | hybrid (dense + BM25, RRF в Qdrant)
PREFETCH = int(os.environ.get("PREFETCH", "100"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.105.1:11434").rstrip("/")  # фильтр на Маке
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "qwen3:8b")
# генерация идёт секунды; фильтр на Маке ждёт модель 120 с, сервис чуть дольше, вход HAProxy ещё дольше
ANSWER_TIMEOUT = float(os.environ.get("ANSWER_TIMEOUT", "130"))
ANSWER_MAX_K = 8  # больше кусков модели не даём: промпт должен влезть в её контекст (answer.NUM_CTX)
KEY_FILE = os.environ.get("QDRANT_KEY_FILE", "/run/secrets/qdrant/read-only-api-key")
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "5"))
QDRANT_CONNECT_TIMEOUT = float(os.environ.get("QDRANT_CONNECT_TIMEOUT", "0.25"))
QDRANT_READ_TIMEOUT = float(os.environ.get("QDRANT_READ_TIMEOUT", "2"))
QDRANT_ATTEMPTS = int(os.environ.get("QDRANT_ATTEMPTS", "4"))
NODE = os.environ.get("NODE_NAME", "")
POD = os.environ.get("POD_NAME", "")
PAYLOAD = ["title", "section", "file", "start", "end", "text"]

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def _key() -> str:
    with open(KEY_FILE) as f:
        return f.read().strip()


def _call(url: str, body: dict | None = None, headers: dict | None = None, timeout: float | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        return json.load(r)


class QdrantError(Exception):
    pass


class Unavailable(Exception):
    """Нужная служба не ответила; клиент получает 503 с этим текстом."""


def _qdrant(path: str, body: dict | None = None) -> tuple[dict, int]:
    """Запрос к Qdrant с повтором на другой пир. Возвращает ответ и номер удачной попытки."""
    u = urllib.parse.urlsplit(QDRANT_URL)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"api-key": _key(), "Content-Type": "application/json"}
    for attempt in range(1, QDRANT_ATTEMPTS + 1):
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=QDRANT_CONNECT_TIMEOUT)
        try:
            conn.connect()  # новое соединение: kube-proxy выбирает пир заново
            conn.sock.settimeout(QDRANT_READ_TIMEOUT)
            conn.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            r = conn.getresponse()
            payload = r.read()
            if r.status < 500:
                if r.status >= 400:  # 4xx: ошибка запроса или ключа, повтор не поможет
                    raise QdrantError(f"qdrant {r.status}")
                return json.loads(payload), attempt
        except QdrantError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            pass  # пир молчит или оборвал соединение: пробуем снова
        finally:
            conn.close()
    raise QdrantError("все попытки к Qdrant неудачны")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> JSONResponse:
    try:
        status = _qdrant(f"/collections/{COLLECTION}")[0]["result"]["status"]
        ready = status in ("green", "yellow")
    except Exception:
        ready = False
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _served_by() -> dict:
    return {"node": NODE, "pod": POD}


def _retrieve(q: str, k: int) -> tuple[list[dict], dict, int]:
    """Вопрос -> (куски с адресами, время по шагам в мс, номер удачной попытки к Qdrant)."""
    t0 = time.perf_counter()
    try:
        vector = _call(f"{OLLAMA_URL}/api/embed", {"model": MODEL, "input": [q]})["embeddings"][0]
    except Exception:
        raise Unavailable("векторизатор недоступен")
    t1 = time.perf_counter()
    if SEARCH_MODE == "hybrid":
        body = {"prefetch": [{"query": vector, "using": "dense", "limit": PREFETCH},
                             {"query": bm25.query_sparse(q), "using": "bm25", "limit": PREFETCH}],
                "query": {"fusion": "rrf"}, "limit": k, "with_payload": PAYLOAD}
    else:
        body = {"query": vector, "using": "dense", "limit": k, "with_payload": PAYLOAD}
    try:
        result, attempts = _qdrant(f"/collections/{COLLECTION}/points/query", body)
        points = result["result"]["points"]
    except Exception:
        raise Unavailable("база недоступна")
    t2 = time.perf_counter()
    hits = [{
        "score": round(h["score"], 4),
        "document": h["payload"].get("title"),
        "section": h["payload"].get("section") or None,
        "file": h["payload"]["file"],
        "span": [h["payload"]["start"], h["payload"]["end"]],
        "text": h["payload"]["text"],
    } for h in points]
    return hits, {"embed": _ms(t1 - t0), "search": _ms(t2 - t1)}, attempts


def _error(message: str, status: int = 503) -> JSONResponse:
    return JSONResponse({"error": message, "served_by": _served_by()}, status_code=status)


@app.get("/search")
def search(q: str = Query(min_length=3, max_length=500), k: int = Query(5, ge=1, le=20)):
    try:
        hits, timing, attempts = _retrieve(q, k)
    except Unavailable as e:
        return _error(str(e))
    return {
        "query": q, "model": MODEL, "collection": COLLECTION, "mode": SEARCH_MODE,
        "timing_ms": timing,
        "qdrant_attempts": attempts,
        "served_by": _served_by(),
        "results": hits,
    }


@app.get("/answer")
def answer(q: str = Query(min_length=3, max_length=500), k: int = Query(5, ge=1, le=ANSWER_MAX_K)):
    """Ответ своими словами по найденным кускам, со ссылками и проверенными цитатами."""
    try:
        hits, timing, attempts = _retrieve(q, k)
    except Unavailable as e:
        return _error(str(e))
    tokens = {"prompt": None, "output": None}
    if hits:
        t0 = time.perf_counter()
        try:
            reply = _call(f"{OLLAMA_URL}/api/chat", answering.chat_body(ANSWER_MODEL, q, hits), timeout=ANSWER_TIMEOUT)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 429:  # фильтр пускает к модели один запрос за раз
                return _error("модель занята, повторите позже", 429)
            return _error("модель недоступна")
        except Exception:
            return _error("модель недоступна")
        timing["generate"] = _ms(time.perf_counter() - t0)
        tokens = {"prompt": reply.get("prompt_eval_count"), "output": reply.get("eval_count")}
        checked = answering.check(answering.parse((reply.get("message") or {}).get("content", "")), hits, q)
    else:  # кусков нет: модели нечего читать
        checked = {"status": "refused", "answer": answering.refusal(q), "citations": []}
    if checked["status"] == "invalid":
        return _error("модель ответила не по формату", 502)
    checked.pop("model_answer", None)  # неподтверждённый ответ модели наружу не отдаём
    return {
        "query": q, "model": ANSWER_MODEL, "collection": COLLECTION,
        **checked,
        "sources": hits,
        "timing_ms": timing,
        "tokens": tokens,
        "qdrant_attempts": attempts,
        "served_by": _served_by(),
    }
