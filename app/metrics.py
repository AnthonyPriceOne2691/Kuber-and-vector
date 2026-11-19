"""Метрики сервиса поиска в текстовом формате Prometheus, без сторонних библиотек.

Счётчики и гистограммы живут в памяти процесса (на под один процесс uvicorn). У каждой серии
метка pod: Prometheus на machine-4 снимает метрики через NodePort машины, а после переезда пода
на одной машине их бывает два; без метки счётчики разных подов смешались бы в одну серию.
Контракт имён и меток описан в deploy/monitoring/README.md, на нём построен дашборд.
"""
from __future__ import annotations

import math
import threading

BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

DEFINITIONS = {
    "search_requests_total": ("counter", "Запросы к /search и /answer по коду ответа"),
    "search_request_seconds": ("histogram", "Время ответа /search и /answer"),
    "search_stage_seconds": ("histogram", "Время шага: embed, qdrant, generate"),
    "search_qdrant_retries_total": ("counter", "Повторы к Qdrant сверх первой попытки"),
    "search_answer_total": ("counter", "Исходы /answer: answered, refused, unsupported, error"),
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v: float) -> str:
    if v == math.inf:
        return "+Inf"
    return repr(float(v)) if not float(v).is_integer() else str(int(v))


class Registry:
    def __init__(self, const_labels: dict[str, str] | None = None) -> None:
        self.const = {k: v for k, v in (const_labels or {}).items() if v}
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._hists: dict[tuple[str, tuple], list[float]] = {}  # корзины…, сумма, число

    def _key(self, name: str, labels: dict[str, str]) -> tuple[str, tuple]:
        if name not in DEFINITIONS:
            raise KeyError(f"нет такой метрики: {name}")
        return name, tuple(sorted({**labels, **self.const}.items()))

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = self._key(name, labels or {})
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, labels: dict[str, str], seconds: float) -> None:
        key = self._key(name, labels)
        with self._lock:
            h = self._hists.setdefault(key, [0.0] * (len(BUCKETS) + 2))
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1

    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            hists = {k: list(v) for k, v in self._hists.items()}
        out = []
        for name, (kind, text) in DEFINITIONS.items():
            out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
            if kind == "counter":
                for (n, labels), v in sorted(counters.items()):
                    if n == name:
                        out.append(f"{name}{_labels(labels)} {_fmt(v)}")
            else:
                for (n, labels), h in sorted(hists.items()):
                    if n != name:
                        continue
                    for b, c in zip((*BUCKETS, math.inf), (*h[:len(BUCKETS)], h[-1])):
                        out.append(f"{name}_bucket{_labels(labels + (('le', _fmt(b)),))} {_fmt(c)}")
                    out.append(f"{name}_sum{_labels(labels)} {_fmt(h[-2])}")
                    out.append(f"{name}_count{_labels(labels)} {_fmt(h[-1])}")
        return "\n".join(out) + "\n"


def _labels(pairs: tuple) -> str:
    return "{" + ",".join(f'{k}="{_escape(str(v))}"' for k, v in pairs) + "}" if pairs else ""
