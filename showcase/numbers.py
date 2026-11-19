"""Доска чисел ролика и полоса качества: showcase/numbers.json -> готовые строки для кадра.

Правило: в кадр попадают только числа из файла. Страницы ничего не считают и не форматируют
сами, они кладут на экран строки отсюда. null в файле значит «ещё не измерено»: на экране
pending, а с "hide_pending": true такая плитка или строка пропадает. Ошибка формата не роняет
доску: значение показывается как pending, описание ошибки уходит в problems (журнал и тест).

Формат файла (schema showcase-numbers/1), все строки идут в кадр по-английски:
    title, subtitle, caption   шапка доски и подпись про ноутбук
    hide_pending               true: неизмеренное не показывать вовсе
    quality                    качество поиска: metric, unit, rows [{id, label, value}], caveat
    tiles                      плитки по порядку: {id, title, value, unit, prefix?, of?, wide?, tone?,
                               hidden?, note?, facts: [{label, value, unit?, prefix?}]};
                               {"id": "quality", "kind": "quality"} ставит на доску плитку качества
"""
from __future__ import annotations

import json
import math
import re

SCHEMA = "showcase-numbers/1"
TONES = ("neutral", "good", "warn")  # цвет большого числа: good — зелёный «всё отбито, ошибок нет»
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


def fmt(v) -> str | None:
    """Число из файла строкой для кадра: 2656 -> «2,656», 82.6 -> «82.6»; не число -> None."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return format(v, ",")


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("numbers.json: ждали объект JSON")
    return raw


def _number(v, where: str, problems: list[str]) -> str | None:
    if v is None:
        return None
    s = fmt(v)
    if s is None:
        problems.append(f"{where}: не число ({type(v).__name__}), показано как pending")
    return s


def _text(v, where: str, problems: list[str]) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        problems.append(f"{where}: не строка")
        return ""
    return v


def _state(values: list[str | None]) -> str:
    if not values or all(v is None for v in values):
        return "pending"
    return "measured" if all(v is not None for v in values) else "partial"


def quality_view(q, problems: list[str]) -> dict | None:
    """Полоса качества: строки «наш способ / статья» с шириной полосы. Ширина — CSS-строка из
    того же числа: проценты как есть (0…100), другие единицы — доля от наибольшего значения."""
    if q is None:
        return None
    if not isinstance(q, dict):
        problems.append("quality: ждали объект")
        return None
    unit = _text(q.get("unit"), "quality.unit", problems)
    rows, raws = [], []
    for i, r in enumerate(q.get("rows") or []):
        if not isinstance(r, dict):
            problems.append(f"quality.rows[{i}]: ждали объект")
            continue
        value = _number(r.get("value"), f"quality.rows[{i}].value", problems)
        raws.append(r.get("value") if value is not None else None)
        rows.append({"id": _text(r.get("id"), f"quality.rows[{i}].id", problems),
                     "label": _text(r.get("label"), f"quality.rows[{i}].label", problems),
                     "value": value, "unit": unit, "bar": None})
    top = max((x for x in raws if x is not None), default=None)
    for row, x in zip(rows, raws):
        if x is None:
            continue
        share = x if unit == "%" else (x / top * 100 if top and top > 0 else 0)
        row["bar"] = f"{min(max(share, 0), 100):g}%"
    return {"title": _text(q.get("title"), "quality.title", problems),
            "benchmark": _text(q.get("benchmark"), "quality.benchmark", problems),
            "metric": _text(q.get("metric"), "quality.metric", problems),
            "search": _text(q.get("search"), "quality.search", problems),
            "caveat": _text(q.get("caveat"), "quality.caveat", problems),
            "unit": unit, "rows": rows, "state": _state([r["value"] for r in rows])}


def tile_view(t, i: int, problems: list[str]) -> dict | None:
    where = f"tiles[{i}]"
    if not isinstance(t, dict):
        problems.append(f"{where}: ждали объект")
        return None
    tid = t.get("id")
    if not isinstance(tid, str) or not _ID.fullmatch(tid):
        problems.append(f"{where}: id из латиницы, цифр и дефиса обязателен, плитка пропущена")
        return None
    kind = t.get("kind", "stat")
    if kind == "quality":
        return {"id": tid, "kind": "quality", "wide": t.get("wide") is True}
    if kind != "stat":
        problems.append(f"{where}: неизвестный kind {kind!r}, плитка пропущена")
        return None
    facts = []
    for j, f in enumerate(t.get("facts") or []):
        if not isinstance(f, dict):
            problems.append(f"{where}.facts[{j}]: ждали объект")
            continue
        value = _number(f.get("value"), f"{where}.facts[{j}].value", problems)
        facts.append({"label": _text(f.get("label"), f"{where}.facts[{j}].label", problems),
                      "prefix": _text(f.get("prefix"), f"{where}.facts[{j}].prefix", problems),
                      "value": value, "unit": _text(f.get("unit"), f"{where}.facts[{j}].unit", problems),
                      "state": "pending" if value is None else "measured"})
    value = _number(t.get("value"), f"{where}.value", problems)
    tone = t.get("tone", "neutral")
    if tone not in TONES:
        problems.append(f"{where}.tone: одно из {', '.join(TONES)}")
        tone = "neutral"
    return {"id": tid, "kind": "stat", "wide": t.get("wide") is True, "tone": tone,
            "title": _text(t.get("title"), f"{where}.title", problems),
            "state": "pending" if value is None else "measured",
            "prefix": _text(t.get("prefix"), f"{where}.prefix", problems), "value": value,
            "of": _number(t.get("of"), f"{where}.of", problems) if value is not None else None,
            "unit": _text(t.get("unit"), f"{where}.unit", problems),
            "note": _text(t.get("note"), f"{where}.note", problems), "facts": facts}


def view(raw: dict) -> dict:
    """Всё, что страницы кладут в кадр: строки, состояния (measured, partial, pending), порядок."""
    problems: list[str] = []
    if raw.get("schema") != SCHEMA:
        problems.append(f"schema: ждали {SCHEMA}")
    hide = raw.get("hide_pending") is True
    quality = quality_view(raw.get("quality"), problems)
    if quality is not None and hide:
        quality["rows"] = [r for r in quality["rows"] if r["value"] is not None]
    tiles, seen = [], set()
    for i, t in enumerate(raw.get("tiles") or []):
        v = tile_view(t, i, problems)
        if v is None:
            continue
        if v["id"] in seen:
            problems.append(f"tiles[{i}]: повтор id {v['id']!r}, плитка пропущена")
            continue
        seen.add(v["id"])
        if t.get("hidden") is True:
            continue
        if v["kind"] == "quality":
            if quality is None:
                problems.append(f"tiles[{i}]: плитка качества без блока quality, пропущена")
                continue
            v["state"] = quality["state"]
        if hide:
            if v["state"] == "pending":
                continue
            if v["kind"] == "stat":
                v["facts"] = [f for f in v["facts"] if f["state"] != "pending"]
        tiles.append(v)
    return {"schema": SCHEMA, "hide_pending": hide,
            "title": _text(raw.get("title"), "title", problems),
            "subtitle": _text(raw.get("subtitle"), "subtitle", problems),
            "caption": _text(raw.get("caption"), "caption", problems),
            "quality": quality, "tiles": tiles, "problems": problems}
