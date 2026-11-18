"""Сервис поиска: вопрос -> куски документов с адресом источника.

Вопрос векторизует Ollama (bge-m3) на Маке, поиск идёт в Qdrant ключом ТОЛЬКО НА ЧТЕНИЕ:
админского ключа у сервиса нет. Наружу только /search, /healthz и /readyz, документации API нет.
Внешних зависимостей, кроме FastAPI и uvicorn, нет: к Qdrant и Ollama ходим стандартной библиотекой.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app import bm25

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").rstrip("/")
COLLECTION = os.environ.get("COLLECTION", "lbr_v2_naive500_title")  # лучшая ветка замера 18.11
SEARCH_MODE = os.environ.get("SEARCH_MODE", "dense")  # dense | hybrid (dense + BM25, RRF в Qdrant)
PREFETCH = int(os.environ.get("PREFETCH", "100"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.104.2:11434").rstrip("/")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
KEY_FILE = os.environ.get("QDRANT_KEY_FILE", "/run/secrets/qdrant/read-only-api-key")
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "5"))
PAYLOAD = ["title", "section", "file", "start", "end", "text"]

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def _key() -> str:
    with open(KEY_FILE) as f:
        return f.read().strip()


def _call(url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


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
        status = _call(f"{QDRANT_URL}/collections/{COLLECTION}", headers={"api-key": _key()})["result"]["status"]
        ready = status in ("green", "yellow")
    except Exception:
        ready = False
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


@app.get("/search")
def search(q: str = Query(min_length=3, max_length=500), k: int = Query(5, ge=1, le=20)):
    t0 = time.perf_counter()
    try:
        vector = _call(f"{OLLAMA_URL}/api/embed", {"model": MODEL, "input": [q]})["embeddings"][0]
    except Exception:
        return JSONResponse({"error": "векторизатор недоступен"}, status_code=503)
    t1 = time.perf_counter()
    if SEARCH_MODE == "hybrid":
        body = {"prefetch": [{"query": vector, "using": "dense", "limit": PREFETCH},
                             {"query": bm25.query_sparse(q), "using": "bm25", "limit": PREFETCH}],
                "query": {"fusion": "rrf"}, "limit": k, "with_payload": PAYLOAD}
    else:
        body = {"query": vector, "using": "dense", "limit": k, "with_payload": PAYLOAD}
    try:
        hits = _call(f"{QDRANT_URL}/collections/{COLLECTION}/points/query", body,
                     {"api-key": _key()})["result"]["points"]
    except Exception:
        return JSONResponse({"error": "база недоступна"}, status_code=503)
    t2 = time.perf_counter()
    return {
        "query": q, "model": MODEL, "collection": COLLECTION, "mode": SEARCH_MODE,
        "timing_ms": {"embed": round((t1 - t0) * 1000, 1), "search": round((t2 - t1) * 1000, 1)},
        "results": [{
            "score": round(h["score"], 4),
            "document": h["payload"].get("title"),
            "section": h["payload"].get("section") or None,
            "file": h["payload"]["file"],
            "span": [h["payload"]["start"], h["payload"]["end"]],
            "text": h["payload"]["text"],
        } for h in hits],
    }
