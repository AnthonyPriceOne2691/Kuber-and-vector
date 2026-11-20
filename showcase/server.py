#!/usr/bin/env python3
"""Сцены ролика на Маке: поиск по договорам с адресом источника и доска чисел.

Сервер на стандартной библиотеке, слушает только 127.0.0.1. Отдаёт две страницы (static/) и API:
    GET /                      сцена поиска (static/index.html)
    GET /board                 доска чисел (static/board.html)
    GET /api/config            пресеты, адрес входа, есть ли эталон; ключа здесь нет
    GET /api/search?q=&k=      прокси в /search входа стенда + эталон и подсветка кусков
    GET /api/answer?q=&k=      прокси в /answer (ответ модели со ссылками) + эталон у источников
    GET /api/truth?q=          эталон LegalBench-RAG для вопроса (разметка юристов)
    GET /api/numbers           showcase/numbers.json и готовые к показу строки
    GET /healthz

Вход стенда по умолчанию https://192.168.105.2 (HAProxy на machine-4). TLS проверяется только по
УЦ стенда (runs/stand-ca.pem) со сверкой адреса в сертификате, ключ API берётся из
runs/entry-api-key.header (строка «X-API-Key: …») на каждый запрос: стенд пересобрали — новый
ключ подхватится без перезапуска. Ключ не печатается, не пишется в журнал и не уходит на страницу.
Вход ходит напрямую, без прокси из окружения и без перенаправлений.

    python3 -I -B showcase/server.py               живой стенд
    python3 -I -B showcase/server.py --stub        без стенда: заглушка входа по кэшу замера
    open http://127.0.0.1:8095/                    поиск;  /board — доска чисел
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import socketserver
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # Наш код (eval, app, showcase). В конец списка: при запуске с -I стандартная библиотека
    # остаётся главнее, подменить json.py файлом из репозитория не выйдет.
    sys.path.append(str(ROOT))

from showcase import numbers as board  # noqa: E402
from showcase.truth import Benchmark  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
PAGES = {"/": "index.html", "/index.html": "index.html", "/board": "board.html", "/board.html": "board.html"}
DEFAULT_ENTRY = "https://192.168.105.2"
# Пределы те же, что у сервиса поиска (app/api.py): длина вопроса, k у /search и у /answer
LIMITS = {"q_min": 3, "q_max": 500, "k_default": 5, "search_k_max": 20, "answer_k_max": 8}
MAX_UPSTREAM = 8 * 1024 * 1024
_INLINE = re.compile(rb"<(script|style)>(.*?)</\1>", re.S)
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
_HEADER_VALUE = re.compile(r"[\x21-\x7e]{1,512}")


class ConfigError(Exception):
    """Вход не настроен: нет УЦ, нет или испорчен файл ключа, не тот адрес. Текст — в журнал,
    на страницу только код: url, key или ca."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


CONFIG_PROBLEMS = {"url": "Stand entry address is invalid", "key": "Entry API key file is missing or invalid",
                   "ca": "Stand CA file is missing or invalid"}


class UpstreamError(Exception):
    """Вход не ответил как надо. message идёт на страницу, поэтому по-английски."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def parse_key_header(text: str) -> tuple[str, str]:
    """«X-API-Key: ключ» (так пишет deploy/observer/install.sh) или голый ключ -> (заголовок, ключ).
    Ключа нет ни в одном тексте ошибки."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ConfigError("файл ключа: нужна одна строка «X-API-Key: ключ»", "key")
    name, sep, value = lines[0].partition(":")
    if not sep:
        name, value = "X-API-Key", lines[0]
    name, value = name.strip(), value.strip()
    if not _HEADER_NAME.fullmatch(name) or not _HEADER_VALUE.fullmatch(value):
        raise ConfigError("файл ключа: нужна строка «X-API-Key: ключ» (ключ без пробелов)", "key")
    return name, value


@dataclass
class Entry:
    """Вход стенда: адрес, УЦ для https, ключ API (из файла или, у заглушки, в памяти)."""

    url: str = DEFAULT_ENTRY
    ca: str | None = None
    key_file: str | None = None
    key: tuple[str, str] | None = None  # только заглушка: ключ в памяти, на диск не пишется
    timeout_search: float = 10.0
    timeout_answer: float = 170.0  # модель до 120 с, сервис 130 с, HAProxy 150 с

    def target(self) -> tuple[str, str, int]:
        try:
            u = urllib.parse.urlsplit(self.url)
            port = u.port
        except ValueError:
            raise ConfigError("адрес входа не разобран", "url") from None
        if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query \
                or u.username or u.password:
            raise ConfigError("адрес входа: нужно https://хост[:порт] без пути", "url")
        return u.scheme, u.hostname, port or (443 if u.scheme == "https" else 80)

    def header(self) -> tuple[str, str]:
        if self.key is not None:
            return self.key
        if not self.key_file:
            raise ConfigError("не задан файл ключа API (--key-header-file)", "key")
        try:
            with open(self.key_file, encoding="utf-8") as f:
                text = f.read(4096)
        except (OSError, UnicodeDecodeError):
            raise ConfigError("файл ключа API не прочитан (--key-header-file)", "key") from None
        return parse_key_header(text)

    def tls(self) -> ssl.SSLContext:
        """Только УЦ стенда, системные корни не грузятся; сверка адреса включена (у сертификата
        входа его IP в subjectAltName, deploy/observer/install.sh)."""
        if not self.ca:
            raise ConfigError("для https нужен УЦ стенда (--ca)", "ca")
        try:
            ctx = ssl.create_default_context(cafile=self.ca)
        except (OSError, ssl.SSLError):
            raise ConfigError("УЦ стенда не прочитан (--ca)", "ca") from None
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def public(self) -> dict:
        """Для /api/config: что настроено, без ключа и без путей на диске; problem — код url, key или ca."""
        out = {"url": self.url, "host": None, "tls": False, "ready": False, "problem": None}
        try:
            scheme, host, port = self.target()
            out.update(host=host if port in (80, 443) else f"{host}:{port}", tls=scheme == "https")
            self.header()
            if scheme == "https":
                self.tls()
            out["ready"] = True
        except ConfigError as e:
            out["problem"] = e.code
        return out


def fetch(entry: Entry, path: str, params: dict, timeout: float) -> tuple[int, bytes, float]:
    """GET на вход стенда: (код, тело, мс от соединения до последнего байта). Напрямую через
    http.client: без прокси из окружения и без перенаправлений."""
    scheme, host, port = entry.target()
    name, value = entry.header()
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=entry.tls())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    url = path + "?" + urllib.parse.urlencode(params)
    t0 = time.perf_counter()
    try:
        conn.request("GET", url, headers={name: value, "Accept": "application/json",
                                          "User-Agent": "showcase/1", "Connection": "close"})
        r = conn.getresponse()
        body = r.read(MAX_UPSTREAM + 1)
        status = r.status
    except ssl.SSLCertVerificationError:
        raise UpstreamError(502, "tls", "TLS certificate not trusted by the stand CA") from None
    except ssl.SSLError:
        raise UpstreamError(502, "tls", "TLS handshake with the entry failed") from None
    except TimeoutError:
        raise UpstreamError(504, "timeout", f"No reply from the entry in {timeout:g} s") from None
    except ConnectionRefusedError:
        raise UpstreamError(502, "unreachable", "Entry refused the connection") from None
    except (OSError, http.client.HTTPException):
        raise UpstreamError(502, "unreachable", "Entry not reachable") from None
    finally:
        conn.close()
    elapsed = (time.perf_counter() - t0) * 1000
    if len(body) > MAX_UPSTREAM:
        raise UpstreamError(502, "upstream", "Entry reply too large")
    return status, body, elapsed


def upstream_error(endpoint: str, status: int) -> tuple[str, str]:
    """Код входа или сервиса -> (вид, строка для экрана). Сервис пишет ошибки по-русски, а на экран
    идёт английский, поэтому текст берётся по коду, а не из ответа."""
    answer = endpoint == "answer"
    table = {
        401: ("key", "Entry rejected the API key"),
        403: ("key", "Entry refused the request"),
        404: ("path", "Entry does not serve this path"),
        405: ("path", "Entry does not allow this method"),
        422: ("input", "Service rejected the question"),
        429: ("busy", "Model busy, try again in a moment" if answer else "Too many requests, wait a moment"),
        502: ("format", "Model reply was not in the expected format") if answer
        else ("upstream", "Entry got a bad reply from the service"),
        503: ("unavailable", "Answer service unavailable" if answer else "Search service unavailable"),
        504: ("timeout", "Service did not answer in time"),
    }
    return table.get(status, ("upstream", f"Entry answered HTTP {status}"))


class Showcase:
    """Логика API без HTTP: проверка ввода, прокси во вход, эталон, числа доски."""

    def __init__(self, entry: Entry, bench: Benchmark, numbers_path: str, presets_path: str,
                 stub: bool = False, log=None) -> None:
        self.entry, self.bench = entry, bench
        self.numbers_path, self.presets_path = numbers_path, presets_path
        self.stub = stub
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    # --- настройки страницы -------------------------------------------------------------------
    def presets(self) -> list[dict]:
        """showcase/presets.json, читается на каждый запрос: правка видна без перезапуска.
        "in_benchmark": false в пресете — вопрос нарочно не из набора (договор не из базы): эталона нет
        и не ждём (expect_truth false), при запуске такой пресет не считается пропавшим из набора."""
        try:
            with open(self.presets_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return []
        out = []
        for p in (raw.get("presets") if isinstance(raw, dict) else None) or []:
            if not isinstance(p, dict) or not isinstance(p.get("query"), str) or not p["query"].strip():
                continue
            k = p.get("k")
            k = k if isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= LIMITS["search_k_max"] \
                else LIMITS["k_default"]
            outside = p.get("in_benchmark") is False
            if outside:
                in_benchmark = False
            elif self.bench.available():
                in_benchmark = self.bench.lookup(p["query"]) is not None
            else:
                in_benchmark = None
            out.append({"id": str(p.get("id") or ""), "label": str(p.get("label") or ""),
                        "dataset": str(p.get("dataset") or ""), "k": k, "query": p["query"],
                        "in_benchmark": in_benchmark, "expect_truth": not outside})
        return out

    def config(self) -> dict:
        return {"entry": self.entry.public(), "stub": self.stub, "limits": dict(LIMITS),
                "truth": {"available": self.bench.available()}, "presets": self.presets()}

    # --- поиск и ответ ------------------------------------------------------------------------
    def _err(self, code: int, kind: str, message: str, **extra) -> tuple[int, dict]:
        return code, {"ok": False, "kind": kind, "error": message, "stub": self.stub, **extra}

    def ask(self, endpoint: str, q: str | None, k_raw: str | None) -> tuple[int, dict]:
        """Вопрос во вход стенда и эталон к найденным кускам. endpoint: search или answer."""
        if endpoint not in ("search", "answer"):
            return self._err(404, "path", "Unknown endpoint")
        kmax = LIMITS["search_k_max"] if endpoint == "search" else LIMITS["answer_k_max"]
        q = (q or "").strip()
        if not LIMITS["q_min"] <= len(q) <= LIMITS["q_max"]:
            return self._err(400, "input", f"Question must be {LIMITS['q_min']} to {LIMITS['q_max']} characters")
        try:
            k = LIMITS["k_default"] if k_raw in (None, "") else int(k_raw)
        except ValueError:
            return self._err(400, "input", "k must be a whole number")
        if not 1 <= k <= kmax:
            return self._err(400, "input", f"k must be from 1 to {kmax}")
        timeout = self.entry.timeout_search if endpoint == "search" else self.entry.timeout_answer
        try:
            status, body, ms = fetch(self.entry, "/" + endpoint, {"q": q, "k": k}, timeout)
        except ConfigError as e:
            self.log(f"[showcase] вход не настроен: {e}")
            return self._err(500, "config", CONFIG_PROBLEMS.get(e.code, "Stand entry is not configured"))
        except UpstreamError as e:
            self.log(f"[showcase] /{endpoint}: {e.kind}")
            return self._err(e.status, e.kind, e.message)
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if not 200 <= status < 300:
            kind, message = upstream_error(endpoint, status)
            said = doc.get("error") if isinstance(doc, dict) else None
            self.log(f"[showcase] /{endpoint}: вход ответил {status}" + (f" ({said})" if isinstance(said, str) else ""))
            return self._err(status if 400 <= status < 600 else 502, kind, message, status=status,
                             elapsed_ms=round(ms, 1))
        if not isinstance(doc, dict):
            return self._err(502, "upstream", "Entry reply is not JSON", status=status)
        hits = doc.get("results" if endpoint == "search" else "sources")
        hits = hits if isinstance(hits, list) else []
        truth = self.bench.lookup(q)
        ann = self.bench.annotate(hits, truth)
        if truth is not None:
            for sn, ranks in zip(truth["snippets"], ann["found_in"]):
                sn["found_in"] = ranks
        return 200, {"ok": True, "endpoint": endpoint, "status": status, "k": k, "elapsed_ms": round(ms, 1),
                     "stub": self.stub, "result": doc, "truth_available": self.bench.available(), "truth": truth,
                     "annotations": ann["hits"], "coverage": ann["coverage"]}

    def truth(self, q: str | None) -> tuple[int, dict]:
        q = (q or "").strip()
        if not q:
            return 400, {"ok": False, "kind": "input", "error": "Question is empty"}
        if not self.bench.available():
            return 200, {"ok": True, "available": False, "truth": None}
        return 200, {"ok": True, "available": True, "truth": self.bench.lookup(q)}

    def numbers(self) -> tuple[int, dict]:
        try:
            raw = board.load(self.numbers_path)
        except (OSError, ValueError) as e:
            self.log(f"[showcase] numbers.json не прочитан: {type(e).__name__}")
            return 500, {"ok": False, "error": "numbers.json is not readable"}
        return 200, {"ok": True, "numbers": raw, "view": board.view(raw)}


# --- HTTP ------------------------------------------------------------------------------------
class ShowcaseHTTPServer(ThreadingHTTPServer):
    """Без обратного DNS при запуске, как у панели: getfqdn не нужен, адрес всегда 127.0.0.1."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def csp_for(html: bytes) -> str:
    """CSP с хэшами встроенных <script> и <style>: чужой код в странице не исполнится."""
    hashes = {b"script": [], b"style": []}
    for tag, body in _INLINE.findall(html):
        hashes[tag].append("'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'")
    script = " ".join(hashes[b"script"]) or "'none'"
    style = " ".join(hashes[b"style"]) or "'none'"
    return (f"default-src 'none'; script-src {script}; style-src {style}; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_handler(app: Showcase, static_dir: str | os.PathLike = STATIC, verbose: bool = False):
    pages = {path: os.path.join(static_dir, name) for path, name in PAGES.items()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "showcase/1"
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:  # без версии Python в заголовке Server
            return self.server_version

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if self.close_connection:
                self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, doc: dict) -> None:
            self._send(code, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """Только 127.0.0.1 и localhost в Host: чужой домен, перенаправленный на 127.0.0.1
            (DNS rebinding), не прочитает ответы этого сервера."""
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            return name in ("127.0.0.1", "localhost")

        def _cross_site(self) -> bool:
            site = self.headers.get("Sec-Fetch-Site")
            return site is not None and site not in ("same-origin", "none")

        def do_GET(self) -> None:  # noqa: N802
            u = urllib.parse.urlsplit(self.path)
            if not self._host_ok():
                self._json(421, {"ok": False, "error": "unknown host"})
                return
            if u.path.startswith("/api/") and self._cross_site():
                self._json(403, {"ok": False, "error": "cross-site request"})
                return
            qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)

            def one(name: str) -> str | None:
                return (qs.get(name) or [None])[0]

            if u.path in pages:
                self._page(pages[u.path])
            elif u.path == "/api/config":
                self._json(200, app.config())
            elif u.path in ("/api/search", "/api/answer"):
                self._json(*app.ask(u.path.rsplit("/", 1)[1], one("q"), one("k")))
            elif u.path == "/api/truth":
                self._json(*app.truth(one("q")))
            elif u.path == "/api/numbers":
                self._json(*app.numbers())
            elif u.path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def _page(self, path: str) -> None:
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json(404, {"ok": False, "error": "not found"})
                return
            self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp_for(body)})

        def do_POST(self) -> None:  # noqa: N802
            """Только чтение. Небольшое тело дочитываем (иначе закрытие с непрочитанными данными
            даёт клиенту сброс вместо ответа), большое или без длины — нет; соединение закрывается."""
            self.close_connection = True
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not self.headers.get("Transfer-Encoding") and 0 < n <= 64 * 1024:
                self.rfile.read(n)
            self._json(405, {"ok": False, "error": "method not allowed"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt: str, *args) -> None:
            if verbose:  # заголовков (и ключа) в журнале нет никогда, только строка запроса
                sys.stderr.write(f"[showcase] {self.log_date_time_string()} {fmt % args}\n")

    return Handler


# --- запуск ----------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Сцены ролика: поиск с адресом источника и доска чисел (только 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8095, help="порт на 127.0.0.1 (8095)")
    ap.add_argument("--entry", default=DEFAULT_ENTRY,
                    help="вход стенда, https://хост[:порт] без пути (https://192.168.105.2)")
    ap.add_argument("--ca", default=str(ROOT / "runs" / "stand-ca.pem"), help="УЦ стенда для проверки TLS")
    ap.add_argument("--key-header-file", default=str(ROOT / "runs" / "entry-api-key.header"),
                    help="файл со строкой «X-API-Key: ключ»")
    ap.add_argument("--data", default=str(ROOT / "data" / "legalbenchrag"),
                    help="LegalBench-RAG: benchmarks/ и corpus/ (эталон и проверка адресов)")
    ap.add_argument("--numbers", default=str(HERE / "numbers.json"), help="числа доски и полосы качества")
    ap.add_argument("--presets", default=str(HERE / "presets.json"), help="пресеты сцены поиска")
    ap.add_argument("--timeout", type=float, default=10.0, help="таймаут /search, с (10)")
    ap.add_argument("--answer-timeout", type=float, default=170.0, help="таймаут /answer, с (170)")
    ap.add_argument("--stub", action="store_true", help="без стенда: заглушка входа по кэшу замера")
    ap.add_argument("--stub-cache", default=str(ROOT / "cache" / "stage1"),
                    help="кэш замера для заглушки (queries.jsonl, query_vectors.f32, naive500_title/, "
                         "naive500_title_docs/ для выбора договора)")
    ap.add_argument("--stub-ollama", default="http://127.0.0.1:11434",
                    help="Ollama на Маке для векторов ссылки и самого вопроса у заглушки")
    ap.add_argument("--stub-embed-cache", default=None,
                    help="кэш векторов Ollama у заглушки (по умолчанию embeddings/ рядом с --stub-cache)")
    ap.add_argument("--stub-answer", default="answered",
                    choices=("answered", "refused", "unsupported", "busy", "invalid"),
                    help="что отвечает /answer заглушки (answered)")
    ap.add_argument("--verbose", action="store_true", help="журнал HTTP-запросов (без заголовков)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bench = Benchmark(args.data)
    stub_entry = None
    if args.stub:
        from showcase import stub
        try:
            t0 = time.perf_counter()
            index = stub.StubIndex(args.stub_cache)
        except (OSError, ValueError) as e:
            print(f"[showcase] заглушка: кэш замера не прочитан ({type(e).__name__}: {e})", file=sys.stderr)
            return 2
        embed_cache = args.stub_embed_cache or str(Path(args.stub_cache).parent / "embeddings")
        try:
            router = stub.open_router(index, args.stub_cache, args.stub_ollama, embed_cache)
            gate = router.threshold()
            route_note = (f"выбор договора по {len(router.docs.docs)} договорам, гейт "
                          + (f"{gate:g}" if gate is not None else "выключен")
                          + f", векторы частей вопроса через Ollama {args.stub_ollama} с кэшем {embed_cache}"
                          + ("" if stub.ollama_reachable(args.stub_ollama)
                             else " · Ollama не отвечает: выбор договора только по векторам из кэша"))
        except (OSError, ValueError) as e:
            router, route_note = None, f"выбор договора выключен ({type(e).__name__}: {e})"
        key = secrets.token_hex(16)
        stub_entry = stub.StubEntry(index, key, answer_mode=args.stub_answer, router=router)
        stub_entry.start()
        entry = Entry(url=stub_entry.url, key=("X-API-Key", key), timeout_search=args.timeout,
                      timeout_answer=args.answer_timeout)
        print(f"[showcase] заглушка входа {stub_entry.url}: {len(index.points)} кусков, "
              f"{len(index.queries)} вопросов, {time.perf_counter() - t0:.1f} с", file=sys.stderr)
        print(f"[showcase] заглушка: {route_note}", file=sys.stderr)
    else:
        entry = Entry(url=args.entry, ca=args.ca, key_file=args.key_header_file,
                      timeout_search=args.timeout, timeout_answer=args.answer_timeout)
    app = Showcase(entry, bench, args.numbers, args.presets, stub=args.stub)
    try:
        server = ShowcaseHTTPServer(("127.0.0.1", args.port), make_handler(app, verbose=args.verbose))
    except OSError as e:
        print(f"[showcase] не открыть 127.0.0.1:{args.port}: {e.strerror or e}", file=sys.stderr)
        if stub_entry:
            stub_entry.close()
        return 2

    def on_term(signum, frame) -> None:  # SIGTERM — как Ctrl+C: закрыть сокеты
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    problem = None
    try:
        entry.target(), entry.header()
        if entry.target()[0] == "https":
            entry.tls()
    except ConfigError as e:
        problem = str(e)
    print(f"[showcase] http://127.0.0.1:{args.port}/ поиск, /board доска · вход {entry.url}"
          + (f" · НЕ ГОТОВ: {problem}" if problem else ""), file=sys.stderr)
    print("[showcase] эталон LegalBench-RAG: " + ("есть" if bench.available() else f"нет ({bench.error}), подсветки не будет"),
          file=sys.stderr)
    code, nums = app.numbers()
    for p in (nums.get("view") or {}).get("problems") or []:
        print(f"[showcase] numbers.json: {p}", file=sys.stderr)
    missing = [p["id"] for p in app.presets() if p["in_benchmark"] is False and p["expect_truth"]]
    if missing:
        print(f"[showcase] пресетов нет в наборе: {', '.join(missing)}", file=sys.stderr)
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if stub_entry:
            stub_entry.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
