"""Сервис поиска: вопрос -> куски документов с адресом источника, по желанию ответ модели.

Вопрос векторизует Ollama на Маке (через фильтр), поиск идёт в Qdrant ключом ТОЛЬКО НА ЧТЕНИЕ:
админского ключа у сервиса нет. Ответ своими словами пишет qwen3:8b через тот же фильтр на Маке,
со ссылками на найденные куски (app/answer.py). Наружу только /search, /answer, /healthz и /readyz,
документации API нет. Внешних зависимостей, кроме FastAPI и uvicorn, нет: к Qdrant и Ollama ходим
стандартной библиотекой.

Отказ машины. Сначала идём в пир Qdrant на своей машине (QDRANT_LOCAL_URL, сервис qdrant-local
с internalTrafficPolicy: Local): он жив, пока жива машина самого сервиса, а при схеме RF3 у него
полная копия базы. Если свой пир не ответил, идём в общий ClusterIP, где каждое новое соединение
уходит на случайный пир. Мёртвый пир молчит, поэтому соединение ждём недолго
(QDRANT_CONNECT_TIMEOUT) и повторяем: следующая попытка почти всегда попадает на живой пир.
Поиск без побочных эффектов, повтор безопасен.

Пара (коллекция, модель вопроса). Без SEARCH_ALIAS это COLLECTION и EMBED_MODEL, как раньше.
С SEARCH_ALIAS (например contracts) фоновый поток раз в ALIAS_TTL секунд смотрит, куда указывает
алиас, и берёт новую пару только после проверки по реестру «коллекция → модель» (REGISTRY_COLLECTION):
модель берётся из реестра, отпечаток должен сойтись. Отпечаток это настоящий кусок самой коллекции:
реестр хранит его id и текст, сервис читает вектор куска из коллекции, считает вектор того же текста
моделью из реестра и сравнивает (косинус не ниже FINGERPRINT_MIN). Так проверяются сами векторы
коллекции, а не подпись. Не сошлось — отказ: отвечаем прежней парой, отказ виден в /readyz и в поле
model_check ответа и пишется в журнал (app/binding.py). Запрос берёт пару один раз и ищет по имени
настоящей коллекции, а не алиаса: модель вопроса и коллекция всегда из одной пары.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from app import answer as answering
from app import binding
from app import bm25
from app import embed_models
from app import metrics

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").rstrip("/")
QDRANT_LOCAL_URL = os.environ.get("QDRANT_LOCAL_URL", "").rstrip("/")  # свой пир; пусто значит не пробовать
COLLECTION = os.environ.get("COLLECTION", "lbr_v2_naive500_title")  # лучшая ветка замера 18.11
SEARCH_MODE = os.environ.get("SEARCH_MODE", "dense")  # dense | hybrid (dense + BM25, RRF в Qdrant)
PREFETCH = int(os.environ.get("PREFETCH", "100"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.105.1:11434").rstrip("/")  # фильтр на Маке
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")  # модель вопроса для COLLECTION
SEARCH_ALIAS = os.environ.get("SEARCH_ALIAS", "")  # пусто: пара COLLECTION + EMBED_MODEL без смены
REGISTRY = os.environ.get("REGISTRY_COLLECTION", "embedding_registry")
ALIAS_TTL = float(os.environ.get("ALIAS_TTL", "1"))           # как часто смотреть, куда указывает алиас
MODEL_RECHECK = float(os.environ.get("MODEL_RECHECK", "5"))   # отказанную цель проверяем снова не чаще
FINGERPRINT_MIN = float(os.environ.get("FINGERPRINT_MIN", "0.99"))
VERIFY_TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT", "30"))  # проверка в фоне: модель может загружаться
BACKGROUND = True  # фоновый поток алиаса; тесты выключают и зовут refresh сами
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
METRICS = metrics.Registry({"pod": POD})
COUNTED = {"/search": "search", "/answer": "answer"}  # /metrics, /readyz и /healthz не считаем


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
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Unavailable(Exception):
    """Нужная служба не ответила; клиент получает 503 с этим текстом."""


def _qdrant(path: str, body: dict | None = None) -> tuple[dict, int]:
    """Запрос к Qdrant: свой пир одной попыткой, потом общий адрес с повтором на другой пир.
    Возвращает ответ и номер удачной попытки (при своём пире попытка 1 значит «ответил свой»)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"api-key": _key(), "Content-Type": "application/json"}
    targets = ([QDRANT_LOCAL_URL] if QDRANT_LOCAL_URL else []) + [QDRANT_URL] * QDRANT_ATTEMPTS
    for attempt, url in enumerate(targets, 1):
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=QDRANT_CONNECT_TIMEOUT)
        try:
            conn.connect()  # новое соединение: kube-proxy выбирает пир заново
            conn.sock.settimeout(QDRANT_READ_TIMEOUT)
            conn.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            r = conn.getresponse()
            payload = r.read()
            if r.status < 500:
                if r.status >= 400:  # 4xx: ошибка запроса или ключа, повтор не поможет
                    raise QdrantError(f"qdrant {r.status}", r.status)
                return json.loads(payload), attempt
        except QdrantError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            pass  # пир молчит или оборвал соединение: пробуем снова
        finally:
            conn.close()
    raise QdrantError("qdrant: all attempts failed")  # текст может уйти клиенту в model_check.reason


def log(event: str, **fields) -> None:
    """Журнал сервиса: строка JSON в stderr (kubectl logs). Пишет только смену пары и отказы."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "pod": POD, **fields}
    print(json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)


def _embed(model: str, texts: list[str], timeout: float | None = None) -> list[list[float]]:
    return _call(f"{OLLAMA_URL}/api/embed", {"model": model, "input": texts}, timeout=timeout)["embeddings"]


# --- пара (коллекция, модель) и алиас ----------------------------------------------------------
def _resolve_alias() -> str | None:
    """Куда указывает SEARCH_ALIAS (GET /aliases, ключ на чтение); None, если такого алиаса нет."""
    for a in _qdrant("/aliases")[0]["result"]["aliases"]:
        if a.get("alias_name") == SEARCH_ALIAS:
            return a.get("collection_name")
    return None


def _verify(collection: str) -> binding.Pair:
    """Проверка цели: модель из реестра, вектор куска-пробы из самой коллекции против вектора
    того же текста, посчитанного этой моделью сейчас. Отказ: binding.Refused с причиной."""
    try:
        found = _qdrant(f"/collections/{REGISTRY}/points", {
            "ids": [binding.registry_id(collection)], "with_payload": True, "with_vector": False})[0]["result"]
    except QdrantError as e:
        if e.status == 404:
            raise binding.Refused(f"registry {REGISTRY} not found") from None
        raise
    entry = (found[0].get("payload") or {}) if found else {}
    if not found or entry.get("collection") != collection:
        raise binding.Refused(f"{collection} is not in the registry")
    model, probe_id, probe_text = entry.get("model"), entry.get("probe_id"), entry.get("probe_text")
    if not (isinstance(model, str) and model and isinstance(probe_id, (str, int))
            and isinstance(probe_text, str) and probe_text):
        raise binding.Refused(f"registry entry for {collection} is incomplete")
    try:
        points = _qdrant(f"/collections/{collection}/points", {
            "ids": [probe_id], "with_payload": False, "with_vector": ["dense"]})[0]["result"]
    except QdrantError as e:
        if e.status == 404:
            raise binding.Refused(f"collection {collection} not found", model) from None
        raise
    stored = ((points[0].get("vector") or {}).get("dense")) if points else None
    if not stored:
        raise binding.Refused("fingerprint point is missing from the collection", model)
    try:
        fresh = _embed(model, [probe_text], timeout=VERIFY_TIMEOUT)[0]
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 403:  # фильтр Ollama не знает такой модели: вопросы ею тоже не посчитать
            raise binding.Refused(f"embedder does not allow model {model}", model) from None
        raise
    if len(fresh) != len(stored):
        raise binding.Refused(f"dimension mismatch: {model} gives {len(fresh)}, collection stores {len(stored)}", model)
    cos = binding.cosine(fresh, stored)
    if cos < FINGERPRINT_MIN:
        raise binding.Refused(f"fingerprint mismatch: cosine {cos:.3f} < {FINGERPRINT_MIN}", model)
    return binding.Pair(collection, model, verified=True, cosine=round(cos, 4))


BINDING: binding.Binding | None = None
_BINDING_LOCK = threading.Lock()


def _binding() -> binding.Binding:
    """Состояние пары по алиасу; создаётся при первом обращении, тогда же стартует фоновый поток."""
    global BINDING
    if BINDING is None:
        with _BINDING_LOCK:
            if BINDING is None:
                BINDING = binding.Binding(SEARCH_ALIAS, COLLECTION, MODEL, resolve=_resolve_alias, verify=_verify,
                                          ttl=ALIAS_TTL, recheck=MODEL_RECHECK, log=log)
    if BACKGROUND:
        BINDING.ensure_running()
    return BINDING


def _serving() -> tuple[binding.Pair, dict | None]:
    """Пара для этого запроса (снимок) и состояние проверки для поля model_check (без алиаса None)."""
    if not SEARCH_ALIAS:
        return binding.Pair(COLLECTION, MODEL), None
    b = _binding()
    st = b.state
    return st.pair, b.view(st)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    endpoint = COUNTED.get(request.url.path)
    if endpoint:
        METRICS.observe("search_request_seconds", {"endpoint": endpoint}, time.perf_counter() - t0)
        METRICS.inc("search_requests_total", {"endpoint": endpoint, "code": str(response.status_code)})
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/metrics")
def metrics_text() -> Response:
    """Метрики для Prometheus на machine-4; через вход 443 не видны (HAProxy пускает только API)."""
    return Response(METRICS.render(), media_type=metrics.CONTENT_TYPE)


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Готов, если коллекция текущей пары жива. С алиасом ещё и после первого чтения алиаса: до него
    под не знает, какой парой отвечать. Отказ цели алиаса готовность не снимает: под отвечает прежней
    проверенной парой, а отказ виден в теле (model_check)."""
    if SEARCH_ALIAS:
        b = _binding()
        st = b.state
        pair, check, ready = st.pair, b.view(st), st.resolved
    else:
        pair, check, ready = binding.Pair(COLLECTION, MODEL), None, True
    if ready:
        try:
            status = _qdrant(f"/collections/{pair.collection}")[0]["result"]["status"]
            ready = status in ("green", "yellow")
        except Exception:
            ready = False
    body: dict = {"ready": ready}
    if check is not None:
        body.update(collection=pair.collection, model=pair.model, model_check=check)
    return JSONResponse(body, status_code=200 if ready else 503)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _served_by() -> dict:
    return {"node": NODE, "pod": POD}


def _route(attempts: int) -> str:
    """Кто из Qdrant ответил: свой пир на этой машине или общий сервис."""
    return "local" if QDRANT_LOCAL_URL and attempts == 1 else "cluster"


def _retrieve(q: str, k: int, pair: binding.Pair | None = None) -> tuple[list[dict], dict, int]:
    """Вопрос -> (куски с адресами, время по шагам в мс, номер удачной попытки к Qdrant).
    Модель вопроса и коллекция из одной пары; без пары берётся текущая."""
    pair = pair or _serving()[0]
    t0 = time.perf_counter()
    try:
        vector = _embed(pair.model, [embed_models.query_text(pair.model, q)])[0]
    except Exception:
        raise Unavailable("векторизатор недоступен")
    t1 = time.perf_counter()
    METRICS.observe("search_stage_seconds", {"stage": "embed"}, t1 - t0)
    if SEARCH_MODE == "hybrid":
        body = {"prefetch": [{"query": vector, "using": "dense", "limit": PREFETCH},
                             {"query": bm25.query_sparse(q), "using": "bm25", "limit": PREFETCH}],
                "query": {"fusion": "rrf"}, "limit": k, "with_payload": PAYLOAD}
    else:
        body = {"query": vector, "using": "dense", "limit": k, "with_payload": PAYLOAD}
    try:
        result, attempts = _qdrant(f"/collections/{pair.collection}/points/query", body)
        points = result["result"]["points"]
    except Exception:
        raise Unavailable("база недоступна")
    t2 = time.perf_counter()
    METRICS.observe("search_stage_seconds", {"stage": "qdrant"}, t2 - t1)
    if attempts > 1:
        METRICS.inc("search_qdrant_retries_total", value=attempts - 1)
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
    pair, check = _serving()
    try:
        hits, timing, attempts = _retrieve(q, k, pair)
    except Unavailable as e:
        return _error(str(e))
    out = {
        "query": q, "model": pair.model, "collection": pair.collection, "mode": SEARCH_MODE,
        "timing_ms": timing,
        "qdrant_attempts": attempts,
        "qdrant_route": _route(attempts),
        "served_by": _served_by(),
        "results": hits,
    }
    if check is not None:
        out["model_check"] = check
    return out


@app.get("/answer")
def answer(q: str = Query(min_length=3, max_length=500), k: int = Query(5, ge=1, le=ANSWER_MAX_K)):
    """Ответ своими словами по найденным кускам, со ссылками и проверенными цитатами."""
    pair, check = _serving()
    try:
        hits, timing, attempts = _retrieve(q, k, pair)
    except Unavailable as e:
        METRICS.inc("search_answer_total", {"result": "error"})
        return _error(str(e))
    tokens = {"prompt": None, "output": None}
    if hits:
        t0 = time.perf_counter()
        try:
            reply = _call(f"{OLLAMA_URL}/api/chat", answering.chat_body(ANSWER_MODEL, q, hits), timeout=ANSWER_TIMEOUT)
        except urllib.error.HTTPError as e:
            e.close()
            METRICS.inc("search_answer_total", {"result": "error"})
            if e.code == 429:  # фильтр пускает к модели один запрос за раз
                return _error("model is busy, try again later", 429)
            return _error("model unavailable")
        except Exception:
            METRICS.inc("search_answer_total", {"result": "error"})
            return _error("model unavailable")
        METRICS.observe("search_stage_seconds", {"stage": "generate"}, time.perf_counter() - t0)
        timing["generate"] = _ms(time.perf_counter() - t0)
        tokens = {"prompt": reply.get("prompt_eval_count"), "output": reply.get("eval_count")}
        checked = answering.check(answering.parse((reply.get("message") or {}).get("content", "")), hits, q)
    else:  # кусков нет: модели нечего читать
        checked = {"status": "refused", "answer": answering.refusal(q), "citations": []}
    if checked["status"] == "invalid":
        METRICS.inc("search_answer_total", {"result": "error"})
        return _error("model reply is not in the expected format", 502)
    METRICS.inc("search_answer_total", {"result": checked["status"]})
    checked.pop("model_answer", None)  # неподтверждённый ответ модели наружу не отдаём
    out = {
        "query": q, "model": ANSWER_MODEL, "embed_model": pair.model, "collection": pair.collection,
        **checked,
        "sources": hits,
        "timing_ms": timing,
        "tokens": tokens,
        "qdrant_attempts": attempts,
        "qdrant_route": _route(attempts),
        "served_by": _served_by(),
    }
    if check is not None:
        out["model_check"] = check
    return out
