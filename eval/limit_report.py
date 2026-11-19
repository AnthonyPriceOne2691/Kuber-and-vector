"""Отчёт замера предела: eval/results/limit.md из сырых данных runs/limit/<прогон>/.

    python3 -I -B eval/limit_report.py runs/limit/20261120-101500-synth_rf2 [ещё прогон…]
    python3 -I -B eval/limit_report.py --template          шаблон с пустыми местами (до прогона)

Читает baseline.json (снимок до синтетики), stage-N.json (загрузка и сторож, limit_synth.py),
qps-N.json (лестница нагрузки, limit_qps.py), stages.txt (какие ступени задумывались).
Числа в таблицах только из этих файлов; раздел «Оценка до порогов» это линейная прикидка,
он так и подписан.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "results", "limit.md")
EMPTY = "…"
TITLE = "# Предел стенда на синтетике"
DEFAULT_THRESHOLDS = {"soft_qdrant_mem": 0.75, "hard_qdrant_mem": 0.90, "soft_node_avail_mib": 300,
                      "hard_node_avail_mib": 150, "soft_disk_free": 0.25, "hard_disk_free": 0.18}
DEFAULT_CRITERIA = {"max_errors": 0.01, "min_rate_share": 0.95, "p99_growth": 2.0, "p99_floor_ms": 50.0,
                    "p99_max_ms": 1000.0}
DEFAULT_STAGES = [100000, 250000, 500000, 1000000]
DEFAULT_RATES = [5, 10, 20, 30, 40, 50]


# --- формат чисел ---------------------------------------------------------------------------
def num(n) -> str:
    return EMPTY if n is None else f"{int(n):,}".replace(",", " ")


def dec(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def gb(b) -> str:
    return EMPTY if b is None else dec(b / 1e9, 2) + " ГБ"


def pct(x) -> str:
    return EMPTY if x is None else f"{100 * x:.0f} %"


def ms(v) -> str:
    return EMPTY if v is None else f"{v:.0f}"


def rate(r) -> str:
    return EMPTY if r is None else f"{r:g}"


def dims(n) -> str:
    """«1024 измерения», «8 измерений»: число с правильным словом."""
    if not isinstance(n, int):
        return f"{EMPTY} измерений"
    word = ("измерение" if n % 10 == 1 and n % 100 != 11 else
            "измерения" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "измерений")
    return f"{n} {word}"


def dur(s) -> str:
    if s is None:
        return EMPTY
    s = int(round(s))
    if s < 120:
        return f"{s} с"
    m = s // 60
    return f"{m} мин" if m < 120 else f"{m // 60} ч {m % 60:02d} мин"


# --- данные прогона ---------------------------------------------------------------------------
def _read(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_run(d: str) -> dict:
    run = {"dir": d, "name": os.path.basename(os.path.normpath(d)), "baseline": _read(os.path.join(d, "baseline.json")),
           "planned": None, "stages": []}
    planned = os.path.join(d, "stages.txt")
    if os.path.exists(planned):
        with open(planned, encoding="utf-8") as f:
            run["planned"] = [int(x) for x in f.read().split()]
    for f in os.listdir(d):
        m = re.fullmatch(r"stage-(\d+)\.json", f)
        if m:
            n = int(m.group(1))
            run["stages"].append({"n": n, "stage": _read(os.path.join(d, f)),
                                  "qps": _read(os.path.join(d, f"qps-{n}.json"))})
    run["stages"].sort(key=lambda s: s["n"])
    return run


def _limit(v: dict):
    return v.get("qdrant_limit") or v.get("mem_capacity")


def _mem_share(v: dict, key: str = "qdrant_rss"):
    lim = _limit(v)
    return v[key] / lim if v.get(key) is not None and lim else None


def _ws_share(v: dict):
    return _mem_share(v, "qdrant_working_set")


def _disk_free(v: dict):
    return v["fs_available"] / v["fs_capacity"] if v.get("fs_available") is not None and v.get("fs_capacity") else None


def worst(view: dict | None, fn, pick=max) -> tuple:
    vals = [(fn(v), n) for n, v in sorted((view or {}).items()) if fn(v) is not None]
    return pick(vals) if vals else (None, None)


def first_stage(run: dict) -> dict:
    return next((s["stage"] for s in run["stages"] if s["stage"]), {}) if run else {}


def thresholds(run: dict | None) -> dict:
    return {**DEFAULT_THRESHOLDS, **(first_stage(run).get("thresholds") or {})}


def short(reasons: list[str], keep: int = 2) -> str:
    """Причины для ячейки таблицы: первые две и сколько ещё."""
    head = "; ".join(reasons[:keep])
    return head + (f" и ещё {len(reasons) - keep}" if len(reasons) > keep else "")


def stage_result(st: dict) -> str:
    stop = st.get("stopped") or {}
    reasons = short(stop.get("reasons") or [])
    outcome = st.get("outcome")
    if outcome == "hard":
        return f"жёсткий порог: {reasons}" + ("; коллекция удалена" if st.get("deleted") else "; удалить не вышло")
    if outcome == "index_timeout":
        return f"индекс не достроен за {dur(st.get('index_s'))}, статус {st.get('status')}"
    if outcome == "soft":
        return f"мягкий порог: {reasons}"
    if outcome == "ok":
        return "индекс готов"
    return "не закончена"


def stage_points(s: dict):
    """Сколько точек было на ступени на самом деле: мягкий порог мог остановить её раньше цели."""
    st = s["stage"] or {}
    return st.get("points") or (st.get("stopped") or {}).get("at_points") or s["n"]


def stage_label(s: dict) -> str:
    pts = stage_points(s)
    return num(pts) + ("" if pts == s["n"] else f" (цель {num(s['n'])})")


def held_step(q: dict | None) -> dict | None:
    if not q or q.get("held_rate") is None:
        return None
    return next((s for s in q["steps"] if s["rate"] == q["held_rate"]), None)


def stop_step(q: dict | None) -> dict | None:
    if not q or not q.get("stopped"):
        return None
    return next((s for s in q["steps"] if s["rate"] == q["stopped"]["rate"]), None)


# --- разделы ----------------------------------------------------------------------------------
def describe(run: dict) -> str:
    st = first_stage(run)
    cfg = st.get("config") or {}
    dense = (cfg.get("vectors") or {}).get("dense") or {}
    quant = ((cfg.get("quantization_config") or {}).get("scalar") or {}).get("type")
    where = "исходные на диске" if dense.get("on_disk") else "исходные в памяти"
    return (f"коллекция `{st.get('collection', EMPTY)}`, {cfg.get('shard_number', EMPTY)} шардов по "
            f"{cfg.get('replication_factor', EMPTY)} копии, векторы {dims(dense.get('size'))}, косинус, "
            f"{where}" + (f", в памяти {quant}" if quant else ", без квантования"))


def section_intro(runs: list[dict]) -> list[str]:
    out = [TITLE, "",
           "Сколько векторов влезает в стенд и сколько запросов в секунду он держит, пока не растёт p99. "
           "Стенд это четыре ВМ на одном ноутбуке (Apple M5, 32 ГБ), а не сервер: машины кластера "
           "machine-1…3 по 2 vCPU и 4 ГБ, Qdrant 1.20.2 из трёх пиров с пределом памяти пода 2 ГиБ, "
           "вход HAProxy и генератор нагрузки на machine-4.", ""]
    if not runs:
        out += ["**Замера ещё не было.** Это шаблон: пустые места (…) заполнит `eval/limit_report.py` после "
                "прогона `deploy/limit/limit.sh run`; как запускать, в `deploy/limit/README.md`.", ""]
        return out
    for run in runs:
        when = (run["baseline"] or {}).get("ts") or first_stage(run).get("started_at")
        date = _date(when)
        out.append(f"Замер {date}: {describe(run)}. Сырые данные в `runs/limit/{run['name']}/` (в git не идут).")
    out.append("")
    return out


def _date(ts) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else EMPTY


def summary_lines(run: dict | None) -> list[str]:
    if run is None or not run["stages"]:
        return [f"- **Векторов влезло:** {EMPTY}",
                f"- **Запросов в секунду без роста p99:** {EMPTY}",
                f"- **Где растёт задержка:** {EMPTY}"]
    stages = [s for s in run["stages"] if s["stage"]]
    last = stages[-1]["stage"]
    pts = last.get("points") or (last.get("stopped") or {}).get("at_points")
    outcome = last.get("outcome")
    planned = run["planned"] or [s["n"] for s in stages]
    reasons = short((last.get("stopped") or {}).get("reasons") or [])
    if outcome == "ok":
        done = ", ".join(num(s["n"]) for s in stages)
        vec = (f"{num(pts)}, все ступени ({done}) прошли без стоп-условий" if last["target"] >= max(planned)
               else f"{num(pts)} без стоп-условий; ступени выше не мерились")
    elif outcome == "soft":
        vec = f"{num(pts)}, дальше не грузили, мягкий порог ({reasons})"
    elif outcome == "hard":
        vec = (f"меньше {num((last.get('stopped') or {}).get('at_points'))}, на них жёсткий порог ({reasons}); "
               + ("синтетическая коллекция удалена" if last.get("deleted") else "удалить коллекцию не вышло"))
    elif outcome == "index_timeout":
        vec = f"{num(pts)}, но индекс не достроился за {dur(last.get('index_s'))}"
    else:
        vec = f"{num(pts)}, ступень не закончена"
    lines = [f"- **Векторов влезло:** {vec}."]
    measured = [s for s in stages if s["qps"]]
    if not measured:
        lines += ["- **Запросов в секунду без роста p99:** нагрузку не мерили.", f"- **Где растёт задержка:** {EMPTY}"]
        return lines
    big = measured[-1]
    q = big["qps"]
    h = held_step(q)
    if h is None:
        qline = (f"на {num(stage_points(big))} векторах не держит даже "
                 f"{rate(q['steps'][0]['rate'] if q['steps'] else None)} в секунду")
    else:
        qline = (f"{rate(q['held_rate'])} на {num(stage_points(big))} векторах, p99 {ms(h['p99'])} мс "
                 f"(на первой ступени {ms(q.get('base_p99'))} мс)")
        if q.get("stopped") is None:
            qline += "; лестница кончилась раньше, чем вырос p99, предел выше"
    lines.append(f"- **Запросов в секунду без роста p99:** {qline}.")
    s = stop_step(q)
    first = q["steps"][0] if q["steps"] else None
    if s and first and s is not first:
        lines.append(f"- **Где растёт задержка:** на {rate(s['rate'])} в секунду p99 у пользователя {ms(s['p99'])} мс "
                     f"(на {rate(first['rate'])} было {ms(first['p99'])}); из них Qdrant p99 {ms(s['search_p99'])} мс "
                     f"(было {ms(first['search_p99'])}), векторизация на Маке p99 {ms(s['embed_p99'])} мс "
                     f"(было {ms(first['embed_p99'])}). Почему ступень не держит: {'; '.join(s['why'])}.")
    elif s:
        lines.append(f"- **Где растёт задержка:** уже первая ступень не держит: {'; '.join(s['why'])}.")
    else:
        lines.append("- **Где растёт задержка:** в пределах лестницы p99 не вырос выше порога.")
    return lines


def section_summary(runs: list[dict]) -> list[str]:
    out = ["## Итог", ""]
    if not runs:
        return out + summary_lines(None) + [""]
    for run in runs:
        if len(runs) > 1:
            out += [f"### `{first_stage(run).get('collection', run['name'])}`", ""]
        out += summary_lines(run) + [""]
    return out


MEM_HEAD = ["| Векторов | Загрузка | Индекс | Память Qdrant после индекса | Пик памяти Qdrant | Рабочий набор Qdrant | "
            "Свободно памяти машины | Свободно диска | Ступень |", "|---|---|---|---|---|---|---|---|---|"]


def mem_row(label: str, view: dict | None, peaks: dict | None, st: dict | None) -> str:
    share, node = worst(view, _mem_share)
    rss = (view or {}).get(node, {}).get("qdrant_rss") if node else None
    ws_share, wnode = worst(view, _ws_share)
    ws = (view or {}).get(wnode, {}).get("qdrant_working_set") if wnode else None
    peak, pnode = worst(peaks, lambda v: v.get("qdrant_rss"))
    avail, anode = worst(view, lambda v: v.get("mem_available"), min)
    free, fnode = worst(view, _disk_free, min)
    fs_av = (view or {}).get(fnode, {}).get("fs_available") if fnode else None
    return "| " + " | ".join([
        label,
        dur(st.get("upload_s")) if st else "",
        dur(st.get("index_s")) if st else "",
        f"{gb(rss)}, {pct(share)} предела пода ({node})" if node else EMPTY,
        f"{gb(peak)} ({pnode})" if pnode else ("" if st is None else EMPTY),
        f"{gb(ws)}, {pct(ws_share)} ({wnode})" if wnode else EMPTY,
        f"{gb(avail)} ({anode})" if anode else EMPTY,
        f"{pct(free)}, {gb(fs_av)} ({fnode})" if fnode else EMPTY,
        stage_result(st) if st else "",
    ]) + " |"


def section_memory(runs: list[dict]) -> list[str]:
    out = ["## Память и диск по ступеням", "",
           "Худшая машина по каждому столбцу. Память Qdrant это анонимная память пира (её не вытеснить на диск, "
           "она и ведёт к OOM), в процентах от предела пода; пик за ступень по снимкам сторожа. Рабочий набор это "
           "то, что предел пода считает вместе с горячим кэшем файлов (исходные векторы на диске читаются через "
           "него): когда он упирается в предел, кэш вытесняется и чтения идут с диска. "
           "Свободная память и диск глазами kubelet: по ним он вытесняет поды и чистит образы.", ""]
    if not runs:
        base = ["до синтетики", "", "", EMPTY, "", EMPTY, EMPTY, EMPTY, ""]
        return out + MEM_HEAD + ["| " + " | ".join(base) + " |"] + \
            [f"| {num(n)} | " + " | ".join([EMPTY] * 8) + " |" for n in DEFAULT_STAGES] + [""]
    for run in runs:
        if len(runs) > 1:
            out += [f"### `{first_stage(run).get('collection', run['name'])}`", ""]
        out += MEM_HEAD
        if run["baseline"]:
            out.append(mem_row("до синтетики", run["baseline"].get("nodes"), None, None))
        for s in run["stages"]:
            st = s["stage"] or {}
            out.append(mem_row(stage_label(s), st.get("after"), st.get("peaks"), st))
        out.append("")
    return out


QPS_HEAD = ["| Векторов | Держит, в секунду | p50 / p95 / p99 на этом темпе, мс | p99 первой ступени, мс | "
            "Qdrant p99, мс | Векторизация p99, мс | Ошибок | Дальше |", "|---|---|---|---|---|---|---|---|"]


def section_qps(runs: list[dict]) -> list[str]:
    out = ["## Запросы в секунду по ступеням", "",
           "Темп, который стенд держит без роста p99, и что было на следующем темпе. Qdrant и векторизация это "
           "шаги сервиса поиска из его ответа (timing_ms), задержка у пользователя включает ещё вход и сеть.", ""]
    if not runs:
        return out + QPS_HEAD + [f"| {num(n)} | " + " | ".join([EMPTY] * 7) + " |" for n in DEFAULT_STAGES] + [""]
    for run in runs:
        if len(runs) > 1:
            out += [f"### `{first_stage(run).get('collection', run['name'])}`", ""]
        out += QPS_HEAD
        for s in run["stages"]:
            q = s["qps"]
            if not q:
                out.append(f"| {stage_label(s)} | не мерили | | | | | | |")
                continue
            h = held_step(q)
            stop = q.get("stopped")
            nxt = f"{rate(stop['rate'])}: {'; '.join(stop['why'])}" if stop else "все темпы держит, предел выше"
            if h is None:
                out.append(f"| {stage_label(s)} | не держит | | | | | | {nxt} |")
                continue
            out.append("| " + " | ".join([
                stage_label(s), rate(q["held_rate"]), f"{ms(h['p50'])} / {ms(h['p95'])} / {ms(h['p99'])}",
                ms(q.get("base_p99")), ms(h["search_p99"]), ms(h["embed_p99"]),
                f"{h['errors']} из {num(h['count'])}", nxt]) + " |")
        out.append("")
    return out


LADDER_HEAD = ["| Темп | Ответов в секунду | p50 / p95 / p99, мс | Порог p99, мс | Qdrant p50 / p99, мс | "
               "Векторизация p50 / p99, мс | Ошибок | Пропущено | Держит |", "|---|---|---|---|---|---|---|---|---|"]


def section_ladders(runs: list[dict]) -> list[str]:
    out = ["## Лестницы нагрузки", ""]
    if not runs:
        return out + ["По таблице на каждую ступень векторов: каждый темп, его задержки, ошибки и решение.", "",
                      *LADDER_HEAD] + [f"| {rate(r)} | " + " | ".join([EMPTY] * 8) + " |" for r in DEFAULT_RATES] + [""]
    for run in runs:
        coll = first_stage(run).get("collection", run["name"])
        for s in run["stages"]:
            q = s["qps"]
            if not q:
                continue
            out += [f"### {'`' + coll + '`, ' if len(runs) > 1 else ''}{stage_label(s)} векторов", "", *LADDER_HEAD]
            for step in q["steps"]:
                kinds = ", ".join(f"{k} {v}" for k, v in sorted(step["errors_by_kind"].items()))
                out.append("| " + " | ".join([
                    rate(step["rate"]), f"{step['qps']:g}".replace(".", ","),
                    f"{ms(step['p50'])} / {ms(step['p95'])} / {ms(step['p99'])}", ms(step["p99_limit"]),
                    f"{ms(step['search_p50'])} / {ms(step['search_p99'])}",
                    f"{ms(step['embed_p50'])} / {ms(step['embed_p99'])}",
                    f"{step['errors']} из {num(step['count'])}" + (f" ({kinds})" if kinds else ""),
                    str(step["skipped"]), "да" if step["holds"] else "нет: " + "; ".join(step["why"])]) + " |")
            out.append("")
    return out


def estimate_lines(run: dict) -> list[str]:
    t = thresholds(run)
    pts = []
    if run["baseline"] and run["baseline"].get("nodes"):
        pts.append((0, run["baseline"]["nodes"]))
    for s in run["stages"]:
        st = s["stage"] or {}
        if st.get("outcome") in ("ok", "soft") and st.get("after") and st.get("points"):
            pts.append((st["points"], st["after"]))
    if len(pts) < 2 or pts[-1][0] <= pts[-2][0]:
        return ["Нужны два снимка с разным числом векторов (до синтетики и ступень): пока прикидывать не по чему."]
    (na, va), (nb, vb) = pts[-2], pts[-1]
    mem, disk, ws = [], [], []
    for node, b in sorted(vb.items()):
        a = va.get(node) or {}
        lim = _limit(b)
        for key, share, acc in (("qdrant_rss", t["soft_qdrant_mem"], mem), ("qdrant_working_set", 1.0, ws)):
            if a.get(key) is not None and b.get(key) is not None and lim:
                slope = (b[key] - a[key]) / (nb - na)
                if slope > 0:
                    acc.append((max(0.0, (share * lim - b[key]) / slope), node, slope))
        if a.get("fs_available") is not None and b.get("fs_available") is not None and b.get("fs_capacity"):
            slope = (a["fs_available"] - b["fs_available"]) / (nb - na)
            if slope > 0:
                disk.append((max(0.0, (b["fs_available"] - t["soft_disk_free"] * b["fs_capacity"]) / slope), node, slope))
    out = [f"**Оценка, не замер.** Линейная прикидка между {num(na)} и {num(nb)} векторами на худшей машине. "
           "Слияния сегментов и построение индекса дают скачки, поэтому настоящий предел может быть ближе.", ""]
    def about(rem: float) -> str:
        return num(round(rem, -3) if rem >= 1000 else round(rem))

    if mem:
        rem, node, slope = min(mem)
        soft = pct(t["soft_qdrant_mem"])
        out.append(f"- Память Qdrant растёт примерно на {num(slope)} байт на вектор на машину ({node}); "
                   + (f"мягкий порог ({soft} предела пода) уже достигнут" if rem < 1
                      else f"до мягкого порога ({soft} предела пода) оценка: ещё около {about(rem)} векторов") + ".")
    else:
        out.append("- Память Qdrant между этими снимками не росла: прикидки по памяти нет.")
    if ws:
        rem, node, slope = min(ws)
        out.append(f"- Рабочий набор Qdrant растёт примерно на {num(slope)} байт на вектор на машину ({node}); "
                   + ("он уже упёрся в предел пода, кэш исходных векторов вытесняется" if rem < 1
                      else f"до предела пода, после которого кэш исходных векторов вытесняется, оценка: ещё около "
                           f"{about(rem)} векторов") + ".")
    if disk:
        rem, node, slope = min(disk)
        out.append(f"- Диск уходит примерно по {num(slope)} байт на вектор на машину ({node}); "
                   + (f"мягкий порог ({pct(t['soft_disk_free'])} свободного диска) уже достигнут" if rem < 1
                      else f"до мягкого порога ({pct(t['soft_disk_free'])} свободного диска) оценка: ещё около "
                           f"{about(rem)} векторов") + ".")
    else:
        out.append("- Свободный диск между этими снимками не убывал: прикидки по диску нет.")
    return out


def section_estimate(runs: list[dict]) -> list[str]:
    out = ["## Оценка до порогов", ""]
    if not runs:
        return out + ["**Оценка, не замер.** Сколько ещё векторов влезло бы до мягких порогов, если рост линейный.", "",
                      f"- По памяти Qdrant: {EMPTY}", f"- По рабочему набору Qdrant: {EMPTY}", f"- По диску: {EMPTY}", ""]
    for run in runs:
        if len(runs) > 1:
            out += [f"### `{first_stage(run).get('collection', run['name'])}`", ""]
        out += estimate_lines(run) + [""]
    return out


def section_method(runs: list[dict]) -> list[str]:
    run = runs[0] if runs else None
    st = first_stage(run)
    t = thresholds(run)
    q = next((s["qps"] for s in run["stages"] if s["qps"]), None) if run else None
    crit = {**DEFAULT_CRITERIA, **((q or {}).get("criteria") or {})}
    qcfg = (q or {}).get("config") or {}
    stages = (run or {}).get("planned") or ([s["n"] for s in run["stages"]] if run else DEFAULT_STAGES)
    coll = st.get("collection", "synth_rf2")
    rates = qcfg.get("rates") or DEFAULT_RATES
    return [
        "## Как получено", "",
        f"- **Векторы.** Случайные и воспроизводимые: компоненты вектора точки это байты SHAKE-256 от зерна "
        f"{st.get('seed', 1)} и номера точки, целые от −128 до 127; Qdrant с косинусом сам нормирует вектор. "
        "Считаются на лету внутри machine-1 и идут в Qdrant через туннель kubectl port-forward к пиру на этой машине "
        "админским ключом из Secret, тем же путём, что загрузка корпуса. Файлов с векторами нет, поэтому диск машины "
        "занимает только сам Qdrant. Payload маленький, с полями, которые читает сервис поиска.",
        "- **Почему случайные, а не шум вокруг настоящих.** Память и диск зависят от числа и размера векторов, схемы "
        "хранения и квантования, а не от смысла чисел. Случайным векторам не нужен кэш корпуса, их не надо копировать "
        "в машину, загрузчик держит в памяти одну порцию. Шум вокруг 17 403 кусков мини-набора дал бы около 57 почти "
        "копий каждого куска на миллион, это тоже не настоящий корпус.",
        f"- **Коллекция** `{coll}`: схема как у contracts_* (вектор dense, {dims(st.get('dim', 1024))}, косинус), "
        "но исходные векторы на диске (`on_disk`), в памяти скалярное квантование int8 (`always_ram`), payload на "
        "диске; индекс HNSW с настройками Qdrant по умолчанию.",
        f"- **Ступени:** {', '.join(num(n) for n in stages)} векторов. Коллекция растёт от ступени к ступени; "
        "нагрузку меряем только после статуса green (индекс достроен).",
        f"- **Сторож** между порциями (каждые {num(st.get('check_every', 20000))} точек) и раз в "
        f"{rate(st.get('guard_interval', 15))} с, пока строится индекс; индекс ждём green у каждого пира "
        f"(общий статус Qdrant собирает по одной копии шарда), не дольше {dur(st.get('wait_green', 7200))}. Мягкий порог: "
        f"анонимная память пира Qdrant {pct(t['soft_qdrant_mem'])} предела пода, свободной памяти машины меньше "
        f"{t['soft_node_avail_mib']} МиБ, свободного диска меньше {pct(t['soft_disk_free'])}; дальше не грузим, "
        f"ступень последняя. Жёсткий порог: {pct(t['hard_qdrant_mem'])} предела пода, меньше {t['hard_node_avail_mib']} "
        f"МиБ, меньше {pct(t['hard_disk_free'])} диска, условие MemoryPressure или DiskPressure, узел не Ready, пир "
        "Qdrant перезапускался или не готов, коллекция red; синтетическая коллекция удаляется сразу.",
        f"- **Нагрузка:** генератор `panel/loadgen.py` на machine-4 через вход HAProxy (TLS по УЦ стенда, ключ API). "
        f"Сервис поиска на время замера переключён на `{coll}` (`kubectl set env COLLECTION`), после возвращён. "
        f"Темпы {', '.join(rate(r) for r in rates)} в секунду, на каждом разогрев {rate(qcfg.get('warm_s', 10))} с "
        f"и замер {rate(qcfg.get('measure_s', 60))} с, повтора нет. Ступень держит, если ошибок не больше "
        f"{pct(crit['max_errors'])}, пропусков нет, ответов не меньше {pct(crit['min_rate_share'])} темпа, все ответы "
        f"из `{coll}`, а p99 не выше порога: в {rate(crit['p99_growth'])} раза выше p99 первой ступени, но не меньше "
        f"чем на {rate(crit['p99_floor_ms'])} мс выше неё и не выше {rate(crit['p99_max_ms'])} мс.",
        "",
    ]


def section_caveats(runs: list[dict]) -> list[str]:
    run = runs[0] if runs else None
    q = next((s["qps"] for s in run["stages"] if s["qps"]), None) if run else None
    nq = ((q or {}).get("config") or {}).get("questions")
    top = max(((q or {}).get("config") or {}).get("rates") or DEFAULT_RATES)
    return [
        "## Оговорки", "",
        "- **Ноутбук, а не сервер.** Четыре ВМ делят один Apple M5 и его диск, у машины кластера 2 vCPU. Цифры "
        "показывают порядок и место, где стенд упирается, а не мощность сервера.",
        "- **Синтетика.** Качество поиска на ней не меряется: у случайных векторов нет правильных ответов (качество "
        "на настоящем корпусе в `eval/results/stage1.md`). На случайных векторах большой размерности граф HNSW "
        "строится и обходится тяжелее, чем на настоящих, у которых есть скопления; зато настоящие вопросы, скорее "
        "всего, попадают в похожие места графа (у векторов bge-m3 есть общая составляющая), и кэш теплее. Это "
        "рассуждение, а не замер: в какую сторону синтетика сдвигает задержку, этот замер не показывает.",
        "- **Векторизация на Маке.** Каждый запрос сначала векторизует Ollama (bge-m3) на Маке, эта часть от числа "
        "векторов не зависит, поэтому Qdrant и векторизация в таблицах отдельно.",
        "- **Потолок входа.** HAProxy пускает с одного адреса не больше 600 запросов за 10 с, то есть 60 в секунду "
        f"(`deploy/observer/haproxy.cfg.in`), а генератор один; лестница здесь до {rate(top)} в секунду. Если стенд "
        "держит и верхний темп, предел выше, и этим замером он не виден.",
        f"- **Вопросов** {nq if nq else EMPTY} (по умолчанию `panel/questions.txt`), идут по кругу.",
        "- **Время загрузки** включает генерацию векторов на machine-1 и туннель kubectl, это не скорость записи Qdrant.",
        "",
    ]


def render(runs: list[dict]) -> str:
    parts = []
    for section in (section_intro, section_summary, section_memory, section_qps, section_ladders,
                    section_estimate, section_method, section_caveats):
        parts += section(runs)
    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts) + "\n"


def headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт замера предела стенда")
    ap.add_argument("runs", nargs="*", help="папки прогонов runs/limit/…")
    ap.add_argument("--template", action="store_true", help="шаблон без данных")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if not args.runs and not args.template:
        ap.error("нужна папка прогона или --template")
    runs = [] if args.template else [load_run(d) for d in args.runs]
    text = render(runs)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
