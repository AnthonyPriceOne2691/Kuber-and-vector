"""Ступени нагрузки для замера предела: тот же генератор panel/loadgen.py и тот же вход, что у сцен.

Запускается на machine-4 (deploy/limit/limit.sh): запросы идут через HAProxy по TLS с ключом API,
как у пользователя. На каждой ступени темп ровный (open loop, как в генераторе): сначала разогрев
--warm, потом замер --measure. В замер входят запросы, ответ на которые пришёл внутри окна замера.
Повтора нет: ошибка первой попытки и есть ошибка. Кроме задержки у пользователя (вход, сервис
поиска, векторизация вопроса на Маке, Qdrant) считаются шаги сервиса из его ответа (timing_ms:
embed и search), чтобы было видно, что именно растёт.

Ступень держит, если:
  - ошибок не больше --max-errors (доля ответов);
  - пропусков нет: генератор не упёрся в потолок одновременных запросов (--concurrency);
  - ответов в секунду не меньше --min-rate-share от заданного темпа;
  - все ответы из той коллекции, которую меряем (--collection);
  - p99 не выше порога: в --p99-growth раз выше p99 первой ступени, но не меньше чем на
    --p99-floor-ms выше неё (шум ВМ) и не выше --p99-max-ms.
Первая ступень, которая не держит, останавливает лестницу: выше темп не поднимаем.

    python3 -I -B limit_qps.py --loadgen loadgen.py --url https://ENTRY/search --cafile ca.pem \\
        --api-key-file key --questions questions.txt --collection synth_rf2 --label 100000 --out qps.json
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass

SCHEMA = "limit-qps/1"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOADGEN = next((p for p in (os.path.join(HERE, "loadgen.py"),
                                    os.path.join(HERE, "..", "panel", "loadgen.py")) if os.path.exists(p)),
                       os.path.join(HERE, "loadgen.py"))


def load_loadgen(path: str):
    """Генератор как модуль по явному пути: python3 -I не ищет модули рядом со скриптом."""
    spec = importlib.util.spec_from_file_location("loadgen", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не загрузить генератор из {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Criteria:
    max_errors: float = 0.01
    min_rate_share: float = 0.95
    p99_growth: float = 2.0
    p99_floor_ms: float = 50.0
    p99_max_ms: float = 1000.0


def p99_limit(base: float | None, c: Criteria) -> float:
    if base is None:
        return c.p99_max_ms
    return min(c.p99_max_ms, max(base * c.p99_growth, base + c.p99_floor_ms))


def recorder(lg):
    """Stats генератора и сырой журнал ответов: окно замера считаем точно, а не по целым секундам."""

    class Recorder(lg.Stats):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.events: list[tuple] = []  # (время ответа, задержка мс, ошибка, коллекция, embed мс, search мс)
            self.skips: list[float] = []

        def record(self, ts, latency_ms, without_retry=None, with_retry=None, retried=False,
                   serving=None, timing=None) -> None:
            super().record(ts, latency_ms, without_retry=without_retry, with_retry=with_retry,
                           retried=retried, serving=serving, timing=timing)
            self.events.append((ts, latency_ms, with_retry, serving[1] if serving else None,
                                timing[0] if timing else None, timing[1] if timing else None))

        def skip(self, ts) -> None:
            super().skip(ts)
            self.skips.append(ts)

    return Recorder


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def summarize(events: list[tuple], skips: list[float], t_from: float, t_to: float, percentile) -> dict:
    ev = [e for e in events if t_from <= e[0] < t_to]
    lat = sorted(e[1] for e in ev)
    kinds: dict[str, int] = {}
    colls: dict[str, int] = {}
    for e in ev:
        if e[2]:
            kinds[e[2]] = kinds.get(e[2], 0) + 1
        else:
            colls[str(e[3])] = colls.get(str(e[3]), 0) + 1
    embed = sorted(e[4] for e in ev if e[4] is not None)
    search = sorted(e[5] for e in ev if e[5] is not None)
    n, errors, seconds = len(ev), sum(kinds.values()), t_to - t_from
    return {
        "count": n, "qps": round(n / seconds, 2) if seconds > 0 else 0.0,
        "p50": _r(percentile(lat, 50)), "p95": _r(percentile(lat, 95)), "p99": _r(percentile(lat, 99)),
        "max": _r(lat[-1]) if lat else None,
        "errors": errors, "error_rate": round(errors / n, 4) if n else 0.0, "errors_by_kind": kinds,
        "skipped": sum(1 for s in skips if t_from <= s < t_to), "collections": colls,
        "embed_p50": _r(percentile(embed, 50)), "embed_p99": _r(percentile(embed, 99)),
        "search_p50": _r(percentile(search, 50)), "search_p99": _r(percentile(search, 99)),
    }


def judge(s: dict, rate: float, base_p99: float | None, c: Criteria, collection: str | None,
          concurrency: int) -> tuple[bool, list[str], float]:
    """Держит ли ступень и почему нет."""
    limit = round(p99_limit(base_p99, c), 1)
    if s["count"] == 0:
        return False, ["ни одного ответа за окно замера"], limit
    why = []
    if s["error_rate"] > c.max_errors:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(s["errors_by_kind"].items()))
        why.append(f"ошибок {s['errors']} из {s['count']} ({kinds})")
    if s["skipped"]:
        why.append(f"пропущено {s['skipped']}: заняты все {concurrency} мест")
    if s["qps"] < c.min_rate_share * rate:
        why.append(f"ответов {s['qps']:g} в секунду из {rate:g}")
    others = sorted(set(s["collections"]) - {collection}) if collection else []
    if others:
        why.append(f"отвечала не та коллекция: {', '.join(others)}")
    if s["p99"] is not None and s["p99"] > limit:
        why.append(f"p99 {s['p99']:g} мс выше порога {limit:g} мс")
    return not why, why, limit


def step_line(s: dict) -> str:
    def ms(v):
        return "?" if v is None else f"{v:.0f}"
    verdict = "держит" if s["holds"] else "не держит: " + "; ".join(s["why"])
    return (f"  {s['rate']:>5g} в секунду: ответов {s['qps']:g}/с, p50/p95/p99 {ms(s['p50'])}/{ms(s['p95'])}/"
            f"{ms(s['p99'])} мс, ошибок {s['errors']}, Qdrant p99 {ms(s['search_p99'])} мс, "
            f"векторизация p99 {ms(s['embed_p99'])} мс; {verdict}")


async def _step(gen, warm: float, measure: float) -> tuple[float, float]:
    t0 = time.time()
    await gen.run(asyncio.Event(), warm + measure)
    return t0 + warm, t0 + warm + measure


def run_ladder(lg, args: argparse.Namespace, questions: list[str], ssl_ctx=None, api_key: str | None = None,
               log=print, fetch=None) -> dict:
    crit = Criteria(**{f: getattr(args, f) for f in asdict(Criteria())})
    rec_cls = recorder(lg)
    doc = {"schema": SCHEMA, "label": args.label, "collection": args.collection,
           "config": {"rates": args.rates, "warm_s": args.warm, "measure_s": args.measure,
                      "concurrency": args.concurrency, "timeout_s": args.timeout, "k": args.k, "retry": "none",
                      "questions": len(questions)},
           "criteria": asdict(crit), "started_at": round(time.time(), 1),
           "steps": [], "held_rate": None, "base_p99": None, "stopped": None}
    base = None
    for n, rate in enumerate(args.rates):
        if n:
            time.sleep(args.pause)  # очереди после прошлой ступени рассасываются
        rec = rec_cls(keep_s=int(args.warm + args.measure) + 60)
        gen = lg.LoadGen([args.url], questions, rec, rate=rate, concurrency=args.concurrency,
                         timeout=args.timeout, retry="none", k=args.k, ssl_ctx=ssl_ctx, api_key=api_key,
                         fetch=fetch, seed=0)
        t_from, t_to = asyncio.run(_step(gen, args.warm, args.measure))
        s = summarize(rec.events, rec.skips, t_from, t_to, lg.percentile)
        holds, why, limit = judge(s, rate, base, crit, args.collection, args.concurrency)
        step = {"rate": rate, **s, "p99_limit": limit, "holds": holds, "why": why}
        doc["steps"].append(step)
        if holds and doc["stopped"] is None:
            doc["held_rate"] = rate
            if base is None:
                base = doc["base_p99"] = s["p99"]
        if not holds and doc["stopped"] is None:
            doc["stopped"] = {"rate": rate, "why": why}
        lg.write_atomic(args.out, doc)
        log(step_line(step))
        if not holds and not args.no_stop:
            break
    doc["finished_at"] = round(time.time(), 1)
    lg.write_atomic(args.out, doc)
    return doc


def parse_rates(text: str) -> list[float]:
    rates = [float(x) for x in re.split(r"[\s,]+", text.strip()) if x]
    if not rates or any(r <= 0 for r in rates):
        raise ValueError(f"темпы должны быть больше нуля: {text!r}")
    return rates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Ступени нагрузки для замера предела стенда")
    ap.add_argument("--loadgen", default=DEFAULT_LOADGEN, help="путь к panel/loadgen.py")
    ap.add_argument("--url", required=True, help="https://вход/search")
    ap.add_argument("--cafile", help="корневой сертификат стенда")
    ap.add_argument("--api-key-file", help="ключ API входа (заголовок X-API-Key)")
    ap.add_argument("--questions", required=True, help="вопросы по одному на строку")
    ap.add_argument("--collection", help="какая коллекция должна отвечать")
    ap.add_argument("--label", default="", help="метка ступени, обычно число векторов")
    ap.add_argument("--rates", default="5 10 20 30 40 50", help="темпы через пробел или запятую")
    ap.add_argument("--warm", type=float, default=10)
    ap.add_argument("--measure", type=float, default=60)
    ap.add_argument("--pause", type=float, default=5)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--k", type=int, default=5)
    for f, v in asdict(Criteria()).items():
        ap.add_argument("--" + f.replace("_", "-"), type=float, default=v)
    ap.add_argument("--no-stop", action="store_true", help="пройти все темпы, даже если ступень не держит")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        args.rates = parse_rates(args.rates)
    except ValueError as e:
        ap.error(str(e))
    if args.warm < 0 or args.measure <= 0:
        ap.error("--warm от 0, --measure больше 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lg = load_loadgen(args.loadgen)
    questions = lg.load_questions(args.questions)
    ctx = ssl.create_default_context(cafile=args.cafile) if args.url.startswith("https:") else None
    key = None
    if args.api_key_file:  # ключ из файла: в списке процессов его не видно
        with open(args.api_key_file, encoding="utf-8") as f:
            key = f.read().strip()
    print(f"{args.label or 'ступень'}: темпы {', '.join(f'{r:g}' for r in args.rates)} в секунду, "
          f"разогрев {args.warm:g} с, замер {args.measure:g} с, вопросов {len(questions)}", flush=True)
    doc = run_ladder(lg, args, questions, ctx, key, log=lambda s: print(s, flush=True))
    held = doc["held_rate"]
    print(f"держит {held:g} в секунду" if held is not None else "не держит даже первую ступень", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
