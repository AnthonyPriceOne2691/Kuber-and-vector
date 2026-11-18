"""Нарезка текста на куски с точными смещениями (индексы символов Python str).

naive   куски фиксированной длины без перекрытия, как стратегия «naive» у авторов
        LegalBench-RAG (chunk_size=500)
rcts    рекурсивная нарезка по разделителям (абзац, строка, пробел, символ) с жадной
        склейкой соседних кусков до лимита, без перекрытия

Обе функции возвращают список (start, end): куски идут подряд и покрывают весь текст.
Куски только из пробелов отбрасывает вызывающий код.
"""
from __future__ import annotations

SEPARATORS: tuple[str, ...] = ("\n\n", "\n", " ", "")


def naive(text: str, size: int = 500) -> list[tuple[int, int]]:
    return [(i, min(i + size, len(text))) for i in range(0, len(text), size)]


def rcts(text: str, size: int = 500, separators: tuple[str, ...] = SEPARATORS) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for s, e in _split(text, 0, len(text), size, separators):
        if cur is None:
            cur = (s, e)
        elif e - cur[0] <= size:
            cur = (cur[0], e)
        else:
            spans.append(cur)
            cur = (s, e)
    if cur is not None:
        spans.append(cur)
    return spans


def _split(text: str, start: int, end: int, size: int, seps: tuple[str, ...]) -> list[tuple[int, int]]:
    if end - start <= size:
        return [(start, end)]
    sep, rest = seps[0], seps[1:]
    if sep == "":
        return [(i, min(i + size, end)) for i in range(start, end, size)]
    out: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        j = text.find(sep, pos, end)
        seg_end = end if j == -1 else j + len(sep)  # разделитель остаётся у левого куска
        if seg_end - pos > size:
            out.extend(_split(text, pos, seg_end, size, rest))
        else:
            out.append((pos, seg_end))
        pos = seg_end
    return out


STRATEGIES = {
    "naive500": lambda text: naive(text, 500),
    "rcts500": lambda text: rcts(text, 500),
}
