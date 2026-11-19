#!/usr/bin/env python3
"""Генератор нагрузки для сервиса поиска стенда «Kubernetes + Qdrant».

Только стандартная библиотека (asyncio). Темп ровный и не зависит от ответов (open loop):
запросы уходят по расписанию, поэтому зависшая машина не снижает темп, а честно видна
в задержке и ошибках. Конкуррентность — потолок одновременных запросов: если все места
заняты, запрос по расписанию не отправляется и считается пропущенным (skipped).

Повтор (--retry):
  none   без повтора;
  other  один повтор на другую цель: следующий --url по кругу, если цель одна — на неё же.
Повторяются только «временные» сбои: таймаут, обрыв соединения, 5xx, битый ответ; 4xx — нет.

Каждый запрос учитывается дважды:
  without_retry  исход первой попытки — что увидел бы пользователь без повтора;
  with_retry     итог после повтора — что пользователь получил на самом деле.
Задержка — от начала первой попытки до итогового ответа, вместе с повтором.

Окна 1 с, 10 с и 60 с — по завершённым секундам (текущая, неполная, не входит). Перцентили —
по ближайшему рангу. Ряд за 3 минуты: по точке на секунду, p50/p95/p99 точки — по последним
5 секундам (сглаживание без запаздывания пика). Каждые 0,5 с снимок статистики атомарно
пишется в JSON (--stats-file), его читает агрегатор панели (panel/aggregator.py --load-stats).

    python3 -I -B panel/loadgen.py --url https://127.0.0.1/search --insecure \\
        --rate 25 --concurrency 64 --timeout 2 --retry other \\
        --questions panel/questions.txt --stats-file /tmp/panel/load.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import signal
import ssl
import sys
import tempfile
import time
import urllib.parse

SCHEMA = "loadgen/1"
# Виды ошибок: таймаут, обрыв или отказ соединения, ответ 5xx, прочий не-2xx ответ, битый ответ
KINDS = ("timeout", "conn", "5xx", "4xx", "bad")
RETRIABLE = frozenset({"timeout", "conn", "5xx", "bad"})
WINDOWS = (1, 10, 60)
SERIES_SECONDS = 180
MAX_BODY = 4 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Перцентиль по ближайшему рангу: наблюдённое значение, ниже или равно которому p % выборки."""
    n = len(sorted_values)
    if n == 0:
        return None
    if p <= 0:
        return sorted_values[0]
    rank = math.ceil(p / 100.0 * n)
    return sorted_values[min(n, max(1, rank)) - 1]


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def _errors_block(counts: dict[str, int], n: int) -> dict:
    total = sum(counts.values())
    return {
        "errors": total,
        "rate": round(total / n, 4) if n else 0.0,
        "by_kind": {k: counts.get(k, 0) for k in KINDS},
    }


class _Bucket:
    """Всё, что завершилось за одну секунду (по часам time.time)."""

    __slots__ = ("sec", "n", "lat", "wo", "wr", "retries", "skipped", "serving",
                 "t_embed", "t_search", "t_n", "ver")

    def __init__(self, sec: int) -> None:
        self.sec = sec
        self.n = 0
        self.lat: list[float] = []
        self.wo: dict[str, int] = {}
        self.wr: dict[str, int] = {}
        self.retries = 0
        self.skipped = 0
        self.serving: dict[tuple, int] = {}
        self.t_embed = 0.0
        self.t_search = 0.0
        self.t_n = 0
        self.ver = 0


class Stats:
    """Счётчики по секундам и скользящие окна поверх них.

    Блокировок нет: генератор пишет из одного цикла asyncio, mock панели — из одного потока.
    Выборка задержек в секунде ограничена max_samples (резервуар): до этого темпа перцентили
    точные, выше — приближённые.
    """

    def __init__(self, keep_s: int = 240, max_samples: int = 2000, smooth_s: int = 5,
                 seed: int | None = None) -> None:
        self.keep_s = keep_s
        self.max_samples = max_samples
        self.smooth_s = smooth_s
        self.started_at: float | None = None
        self._rng = random.Random(seed)
        self._b: dict[int, _Bucket] = {}
        self._cache: dict[int, tuple] = {}
        self._last_sec: int | None = None
        self._tot_n = 0
        self._tot_wo: dict[str, int] = {}
        self._tot_wr: dict[str, int] = {}
        self._tot_retries = 0
        self._tot_skipped = 0

    # --- запись ---------------------------------------------------------------------------
    def _bucket(self, ts: float) -> _Bucket:
        if self.started_at is None:
            self.started_at = ts
        sec = int(ts)
        b = self._b.get(sec)
        if b is None:
            b = self._b[sec] = _Bucket(sec)
            if self._last_sec is None or sec > self._last_sec:
                self._last_sec = sec
                old = sec - self.keep_s
                for s in [s for s in self._b if s < old]:
                    del self._b[s]
        b.ver += 1
        return b

    def record(self, ts: float, latency_ms: float, without_retry: str | None = None,
               with_retry: str | None = None, retried: bool = False,
               serving: tuple | None = None, timing: tuple | None = None) -> None:
        """Один завершённый запрос. without_retry/with_retry — вид ошибки или None (успех)."""
        b = self._bucket(ts)
        b.n += 1
        self._tot_n += 1
        if len(b.lat) < self.max_samples:
            b.lat.append(latency_ms)
        else:
            # резервуар (алгоритм R): каждая задержка секунды попадает в выборку с равной вероятностью
            j = self._rng.randrange(b.n)
            if j < self.max_samples:
                b.lat[j] = latency_ms
        if without_retry:
            b.wo[without_retry] = b.wo.get(without_retry, 0) + 1
            self._tot_wo[without_retry] = self._tot_wo.get(without_retry, 0) + 1
        if with_retry:
            b.wr[with_retry] = b.wr.get(with_retry, 0) + 1
            self._tot_wr[with_retry] = self._tot_wr.get(with_retry, 0) + 1
        if retried:
            b.retries += 1
            self._tot_retries += 1
        if serving:
            b.serving[serving] = b.serving.get(serving, 0) + 1
        if timing:
            b.t_embed += timing[0]
            b.t_search += timing[1]
            b.t_n += 1

    def skip(self, ts: float) -> None:
        """Запрос по расписанию не отправлен: все места конкуррентности заняты."""
        b = self._bucket(ts)
        b.skipped += 1
        self._tot_skipped += 1

    # --- чтение ---------------------------------------------------------------------------
    def _span(self, start: int, end: int) -> list[_Bucket]:
        return [b for s in range(start, end) if (b := self._b.get(s)) is not None]

    def window(self, seconds: int, now: float) -> dict:
        """Окно из `seconds` последних завершённых секунд (текущая неполная не входит)."""
        end = int(now)
        lat: list[float] = []
        n = retries = skipped = 0
        wo: dict[str, int] = {}
        wr: dict[str, int] = {}
        for b in self._span(end - seconds, end):
            n += b.n
            lat.extend(b.lat)
            retries += b.retries
            skipped += b.skipped
            for k, v in b.wo.items():
                wo[k] = wo.get(k, 0) + v
            for k, v in b.wr.items():
                wr[k] = wr.get(k, 0) + v
        lat.sort()
        return {
            "seconds": seconds,
            "count": n,
            "qps": round(n / seconds, 2),
            "p50_ms": _r(percentile(lat, 50)),
            "p95_ms": _r(percentile(lat, 95)),
            "p99_ms": _r(percentile(lat, 99)),
            "max_ms": _r(lat[-1] if lat else None),
            "without_retry": _errors_block(wo, n),
            "with_retry": _errors_block(wr, n),
            "retries": retries,
            "skipped": skipped,
        }

    def series(self, now: float, seconds: int = SERIES_SECONDS) -> list[dict]:
        """Точка на каждую завершённую секунду: темп и ошибки — этой секунды, перцентили —
        по последним smooth_s секундам. Готовые точки кэшируются по версиям корзин."""
        end = int(now)
        out = []
        for s in range(end - seconds, end):
            key = tuple(self._b[x].ver if x in self._b else 0
                        for x in range(s - self.smooth_s + 1, s + 1))
            hit = self._cache.get(s)
            if hit is not None and hit[0] == key:
                out.append(hit[1])
                continue
            b = self._b.get(s)
            lat = sorted(v for x in self._span(s - self.smooth_s + 1, s + 1) for v in x.lat)
            pt = {
                "t": s,
                "qps": b.n if b else 0,
                "p50": _r(percentile(lat, 50)),
                "p95": _r(percentile(lat, 95)),
                "p99": _r(percentile(lat, 99)),
                "errors_without_retry": sum(b.wo.values()) if b else 0,
                "errors_with_retry": sum(b.wr.values()) if b else 0,
                "retries": b.retries if b else 0,
            }
            self._cache[s] = (key, pt)
            out.append(pt)
        for s in [s for s in self._cache if s < end - seconds]:
            del self._cache[s]
        return out

    def totals(self) -> dict:
        return {
            "count": self._tot_n,
            "without_retry": _errors_block(self._tot_wo, self._tot_n),
            "with_retry": _errors_block(self._tot_wr, self._tot_n),
            "retries": self._tot_retries,
            "skipped": self._tot_skipped,
        }

    def serving(self, seconds: int, now: float) -> list[dict]:
        """Какие модель и коллекция отвечали за окно — для сцены со сменой алиаса."""
        end = int(now)
        acc: dict[tuple, int] = {}
        for b in self._span(end - seconds, end):
            for key, v in b.serving.items():
                acc[key] = acc.get(key, 0) + v
        total = sum(acc.values())
        rows = [{"model": k[0], "collection": k[1], "count": v,
                 "share": round(v / total, 4) if total else 0.0} for k, v in acc.items()]
        rows.sort(key=lambda r: -r["count"])
        return rows

    def server_timing(self, seconds: int, now: float) -> dict | None:
        end = int(now)
        e = s = 0.0
        n = 0
        for b in self._span(end - seconds, end):
            e += b.t_embed
            s += b.t_search
            n += b.t_n
        if not n:
            return None
        return {"embed": round(e / n, 1), "search": round(s / n, 2)}

    def snapshot(self, now: float) -> dict:
        return {
            "schema": SCHEMA,
            "ts": round(now, 3),
            "started_at": self.started_at,
            "windows": {f"{s}s": self.window(s, now) for s in WINDOWS},
            "totals": self.totals(),
            "series": self.series(now),
            "serving": self.serving(10, now),
            "server_timing_ms": self.server_timing(10, now),
        }


# --- HTTP-клиент на asyncio -------------------------------------------------------------------
class _NoResponse(ConnectionError):
    """Соединение закрылось, не прислав ни байта ответа."""


class HTTPFormatError(ValueError):
    """Ответ не похож на HTTP/1.x."""


async def read_response(reader: asyncio.StreamReader, limit: int = MAX_BODY,
                        first_line: bytes | None = None) -> tuple[int, dict, bytes]:
    line = first_line if first_line is not None else await reader.readline()
    if not line:
        raise _NoResponse("соединение закрыто без ответа")
    parts = line.decode("latin-1").split(None, 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
        raise HTTPFormatError("не HTTP/1.x")
    status = int(parts[1])
    headers: dict[str, str] = {}
    for _ in range(200):
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        name, _, value = h.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    else:
        raise HTTPFormatError("слишком много заголовков")
    if status in (204, 304) or 100 <= status < 200:
        return status, headers, b""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = bytearray()
        while True:
            size_line = await reader.readline()
            try:
                size = int(size_line.split(b";")[0].strip(), 16)
            except ValueError:
                raise HTTPFormatError("битый chunk") from None
            if size == 0:
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                break
            if len(body) + size > limit:
                raise HTTPFormatError("ответ больше предела")
            body += await reader.readexactly(size)
            await reader.readexactly(2)
        return status, headers, bytes(body)
    if "content-length" in headers:
        n = int(headers["content-length"])
        if n > limit:
            raise HTTPFormatError("ответ больше предела")
        return status, headers, await reader.readexactly(n)
    headers["connection"] = "close"  # длина не указана: тело до закрытия соединения
    return status, headers, await reader.read(limit)


class Endpoint:
    """Одна цель (URL) с пулом соединений keep-alive."""

    def __init__(self, url: str, ssl_ctx: ssl.SSLContext | None = None, keepalive: bool = True,
                 max_idle: float = 4.0, api_key: str | None = None) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"нужен URL http(s)://хост/путь, получено: {url!r}")
        if api_key is not None and (not api_key or any(c in api_key for c in "\r\n\0")):
            raise ValueError("ключ API пустой или с переводом строки")
        self.api_key = api_key  # вход стенда пускает к поиску только с заголовком X-API-Key
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.tls = (ssl_ctx or ssl.create_default_context()) if u.scheme == "https" else None
        self.path = u.path or "/"
        self.base_query = u.query
        host = f"[{self.host}]" if ":" in self.host else self.host
        self.host_header = host if u.port is None else f"{host}:{u.port}"
        # адрес для показа на панели — без логина и пароля, если их вписали в URL
        self.url = urllib.parse.urlunsplit((u.scheme, self.host_header, u.path, u.query, ""))
        self.keepalive = keepalive
        self.max_idle = max_idle
        self._idle: list[tuple[asyncio.StreamReader, asyncio.StreamWriter, float]] = []

    def _request(self, query: str) -> bytes:
        qs = f"{self.base_query}&{query}" if self.base_query else query
        conn = "keep-alive" if self.keepalive else "close"
        key = f"X-API-Key: {self.api_key}\r\n" if self.api_key else ""
        return (f"GET {self.path}?{qs} HTTP/1.1\r\nHost: {self.host_header}\r\n"
                f"User-Agent: stand-loadgen/1\r\nAccept: application/json\r\n{key}"
                f"Connection: {conn}\r\n\r\n").encode("latin-1")

    async def _acquire(self):
        now = time.monotonic()
        while self._idle:
            r, w, t = self._idle.pop()
            if now - t < self.max_idle and not w.is_closing() and not r.at_eof():
                return r, w, True
            w.close()
        r, w = await asyncio.open_connection(self.host, self.port, ssl=self.tls,
                                             server_hostname=self.host if self.tls else None)
        return r, w, False

    async def get(self, query: str) -> tuple[int, bytes]:
        for attempt in (0, 1):
            r, w, reused = await self._acquire()
            # Соединение из пула мог закрыть сервер, пока оно простаивало. Если такое соединение
            # не дало ни байта ответа, GET один раз переотправляется на новом соединении (так делают
            # все HTTP-клиенты). Это не политика повтора: та же цель, мёртвую цель это не скроет.
            stale_ok = reused and attempt == 0
            try:
                try:
                    w.write(self._request(query))
                    await w.drain()
                    first = await r.readline()
                except ConnectionError:
                    if not stale_ok:
                        raise
                    first = b""
                if not first:
                    if stale_ok:
                        w.close()
                        continue
                    raise _NoResponse("соединение закрыто без ответа")
                status, headers, body = await read_response(r, first_line=first)
            except BaseException:
                w.close()
                raise
            if self.keepalive and headers.get("connection", "").lower() != "close" and len(self._idle) < 256:
                self._idle.append((r, w, time.monotonic()))
            else:
                w.close()
            return status, body
        raise _NoResponse("соединение закрыто без ответа")

    def close(self) -> None:
        for _, w, _ in self._idle:
            w.close()
        self._idle.clear()


def classify(status: int, body: bytes) -> tuple[str | None, dict | None]:
    """Вид ошибки по ответу (None — успех) и полезные поля ответа сервиса поиска."""
    if status >= 500:
        return "5xx", None
    if not 200 <= status < 300:
        return "4xx", None
    try:
        doc = json.loads(body)
    except ValueError:
        return "bad", None
    if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
        return "bad", None
    timing = doc.get("timing_ms") if isinstance(doc.get("timing_ms"), dict) else {}
    return None, {
        "model": doc.get("model") or doc.get("mode"),
        "collection": doc.get("collection"),
        "embed": timing.get("embed"),
        "search": timing.get("search"),
    }


def load_questions(path: str) -> list[str]:
    """Вопросы из файла: по одному на строку; пустые строки и строки с # пропускаются."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            q = line.strip()
            if not q or q.startswith("#") or len(q) < 3:
                continue
            out.append(q[:500])  # сервис принимает от 3 до 500 знаков
    if not out:
        raise ValueError(f"в файле вопросов нет ни одной строки: {path}")
    return out


class LoadGen:
    """Расписание запросов, политика повтора и учёт исходов в Stats.

    fetch(i, query) -> (status, body) подменяется в тестах; по умолчанию — Endpoint.get.
    """

    def __init__(self, targets: list[str], questions: list[str], stats: Stats, rate: float = 10.0,
                 concurrency: int = 32, timeout: float = 2.0, retry: str = "other", k: int = 5,
                 arrival: str = "uniform", fetch=None, ssl_ctx: ssl.SSLContext | None = None,
                 keepalive: bool = True, seed: int | None = None, api_key: str | None = None) -> None:
        if retry not in ("none", "other"):
            raise ValueError("retry: none или other")
        if not targets:
            raise ValueError("нужна хотя бы одна цель")
        if rate <= 0 or concurrency < 1 or timeout <= 0:
            raise ValueError("темп, конкуррентность и таймаут должны быть больше нуля")
        self.targets = list(targets)
        self.questions = list(questions)
        self.stats = stats
        self.rate = rate
        self.concurrency = concurrency
        self.timeout = timeout
        self.retry = retry
        self.k = k
        self.arrival = arrival
        self._endpoints = None
        if fetch is None:
            self._endpoints = [Endpoint(u, ssl_ctx, keepalive, api_key=api_key) for u in self.targets]
            self.targets = [e.url for e in self._endpoints]
            fetch = self._fetch_http
        self.fetch = fetch
        self.inflight = 0
        self._tasks: set[asyncio.Task] = set()
        self._qi = 0
        self._rr = 0
        self._rng = random.Random(seed)

    async def _fetch_http(self, i: int, query: str) -> tuple[int, bytes]:
        return await self._endpoints[i].get(query)

    def config(self) -> dict:
        return {"targets": self.targets, "rate": self.rate, "concurrency": self.concurrency,
                "timeout_s": self.timeout, "retry": self.retry, "k": self.k, "arrival": self.arrival}

    async def attempt(self, i: int, question: str) -> tuple[str | None, dict | None]:
        query = urllib.parse.urlencode({"q": question, "k": self.k})
        try:
            status, body = await asyncio.wait_for(self.fetch(i, query), self.timeout)
        except TimeoutError:  # в 3.11+ asyncio.TimeoutError — это он же
            return "timeout", None
        except (OSError, EOFError, ValueError, asyncio.LimitOverrunError):
            return "conn", None
        return classify(status, body)

    async def one(self) -> tuple[str | None, str | None]:
        """Один запрос пользователя: первая попытка и, если надо, повтор на другую цель."""
        q = self.questions[self._qi % len(self.questions)]
        self._qi += 1
        i = self._rr
        self._rr = (self._rr + 1) % len(self.targets)
        t0 = time.perf_counter()
        first, meta = await self.attempt(i, q)
        final, retried = first, False
        if first is not None and self.retry == "other" and first in RETRIABLE:
            retried = True
            final, meta = await self.attempt((i + 1) % len(self.targets), q)
        latency = (time.perf_counter() - t0) * 1000.0
        serving = timing = None
        if meta:
            serving = (meta["model"], meta["collection"])
            if isinstance(meta["embed"], (int, float)) and isinstance(meta["search"], (int, float)):
                timing = (float(meta["embed"]), float(meta["search"]))
        self.stats.record(time.time(), latency, without_retry=first, with_retry=final,
                          retried=retried, serving=serving, timing=timing)
        return first, final

    def _gap(self) -> float:
        if self.arrival == "poisson":
            return self._rng.expovariate(self.rate)
        return 1.0 / self.rate

    async def _run_one(self) -> None:
        try:
            await self.one()
        finally:
            self.inflight -= 1

    async def run(self, stop: asyncio.Event, duration: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        t_start = loop.time()
        next_t = t_start
        while not stop.is_set():
            now = loop.time()
            if duration and now - t_start >= duration:
                break
            if now < next_t:
                await asyncio.sleep(min(next_t - now, 0.25))
                continue
            if now - next_t > 1.0:
                next_t = now  # процесс отстал (например, спал): не догоняем пачкой
            next_t += self._gap()
            if self.inflight >= self.concurrency:
                self.stats.skip(time.time())
                continue
            self.inflight += 1
            task = asyncio.create_task(self._run_one())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=self.timeout * 2 + 1)
        for e in self._endpoints or []:
            e.close()


def write_atomic(path: str, doc: dict) -> None:
    """Пишет JSON во временный файл рядом и подменяет им цель: читатель не увидит половину."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".loadgen-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def status_line(snap: dict, inflight: int) -> str:
    w = snap["windows"]["10s"]
    t = snap["totals"]

    def ms(v):
        return "—" if v is None else f"{v:.0f}"
    return (f"[loadgen] {w['qps']:.1f} req/s · p50 {ms(w['p50_ms'])} · p95 {ms(w['p95_ms'])} · "
            f"p99 {ms(w['p99_ms'])} ms · ошибки без повтора {t['without_retry']['errors']} / "
            f"с повтором {t['with_retry']['errors']} · в полёте {inflight}")


async def main_async(args: argparse.Namespace) -> int:
    questions = load_questions(args.questions)
    ctx = None
    if any(u.startswith("https:") for u in args.url):
        ctx = ssl.create_default_context(cafile=args.cafile)
        if args.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    stats = Stats(smooth_s=args.smooth)
    api_key = None
    if args.api_key_file:  # ключ из файла, а не из аргумента: в списке процессов его не видно
        with open(args.api_key_file, encoding="utf-8") as f:
            api_key = f.read().strip()
    gen = LoadGen(args.url, questions, stats, rate=args.rate, concurrency=args.concurrency,
                  timeout=args.timeout, retry=args.retry, k=args.k, arrival=args.arrival,
                  ssl_ctx=ctx, keepalive=not args.no_keepalive, api_key=api_key)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    def snapshot(running: bool) -> dict:
        snap = stats.snapshot(time.time())
        snap.update({"source": "loadgen", "running": running, "inflight": gen.inflight,
                     "config": gen.config()})
        return snap

    async def reporter() -> None:
        last_print = 0.0
        while True:
            await asyncio.sleep(args.interval)
            snap = snapshot(True)
            if args.stats_file:
                try:
                    write_atomic(args.stats_file, snap)
                except OSError as e:
                    print(f"[loadgen] не записать {args.stats_file}: {e}", file=sys.stderr)
            if not args.quiet and time.monotonic() - last_print >= 5:
                last_print = time.monotonic()
                print(status_line(snap, gen.inflight), file=sys.stderr, flush=True)

    print(f"[loadgen] цели: {', '.join(gen.targets)} · {args.rate} req/s · конкуррентность "
          f"{args.concurrency} · таймаут {args.timeout} с · повтор {args.retry} · вопросов "
          f"{len(questions)}", file=sys.stderr, flush=True)
    rep = asyncio.create_task(reporter())
    try:
        await gen.run(stop, args.duration or None)
    finally:
        rep.cancel()
        final = snapshot(False)
        if args.stats_file:
            write_atomic(args.stats_file, final)
        print(status_line(final, 0), file=sys.stderr, flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Генератор нагрузки для сервиса поиска стенда")
    ap.add_argument("--url", action="append", required=True,
                    help="цель: https://хост/search; можно несколько раз — запросы идут по кругу")
    ap.add_argument("--rate", type=float, default=20.0, help="запросов в секунду (по умолчанию 20)")
    ap.add_argument("--concurrency", type=int, default=64, help="потолок одновременных запросов (64)")
    ap.add_argument("--timeout", type=float, default=2.0, help="таймаут одной попытки, с (2)")
    ap.add_argument("--retry", choices=("none", "other"), default="other",
                    help="none — без повтора; other — один повтор на другую цель (по умолчанию)")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.txt"),
                    help="файл вопросов, по одному на строку (panel/questions.txt)")
    ap.add_argument("--k", type=int, default=5, help="сколько результатов просить (1..20)")
    ap.add_argument("--stats-file", help="куда писать снимок статистики для панели (JSON)")
    ap.add_argument("--interval", type=float, default=0.5, help="как часто писать снимок, с (0.5)")
    ap.add_argument("--smooth", type=int, default=5, help="окно сглаживания перцентилей в ряду, с (5)")
    ap.add_argument("--arrival", choices=("uniform", "poisson"), default="uniform",
                    help="ровный поток или пуассоновский")
    ap.add_argument("--duration", type=float, default=0, help="остановиться через N секунд (0 — работать)")
    ap.add_argument("--cafile", help="свой корневой сертификат для https")
    ap.add_argument("--insecure", action="store_true", help="не проверять сертификат (только для стенда)")
    ap.add_argument("--api-key-file", help="файл с ключом API входа (заголовок X-API-Key)")
    ap.add_argument("--no-keepalive", action="store_true", help="новое соединение на каждый запрос")
    ap.add_argument("--quiet", action="store_true", help="не печатать сводку каждые 5 с")
    args = ap.parse_args(argv)
    if not 1 <= args.k <= 20:
        ap.error("--k: от 1 до 20")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except (ValueError, OSError) as e:
        print(f"[loadgen] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
