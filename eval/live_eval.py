"""Качество поиска вживую: вопросы LegalBench-RAG идут во вход стенда (/search), как у клиента.

Тот же набор вопросов, что у замера без стенда (cache/stage1/queries.jsonl, мини 776), та же
посимвольная метрика (eval/legalbench.py) по адресам, которые вернул сервис (file, span). Результат в
формате eval/qdrant_eval.py, поэтому его можно сравнить парно с любым прежним замером (eval/compare.py).
Сверху: доля вопросов, где сервис выбрал договор, верный ли он, отказы гейта, время ответа, кто отвечал.

    python3 -I -B eval/live_eval.py --out runs/live/result.json
Вход https://192.168.105.2 (HAProxy на machine-4), TLS по УЦ стенда (runs/stand-ca.pem), ключ API из
runs/entry-api-key.header. Запросы по одному: вход режет больше 600 запросов за 10 с с адреса.
"""
from __future__ import annotations

import argparse
import json
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # наш код; с -I папка скрипта сама в путь не попадает

from eval.legalbench import DATASETS, K_VALUES, precision_recall  # noqa: E402


def key_header(text: str) -> tuple[str, str]:
    """«X-API-Key: ключ» (так пишет deploy/observer/install.sh) или голый ключ."""
    line = text.strip().splitlines()[0].strip()
    name, sep, value = line.partition(":")
    return (name.strip(), value.strip()) if sep else ("X-API-Key", line)


def pct(xs: list[float]) -> dict:
    s = sorted(xs)
    return {"p50": s[len(s) // 2], "p95": s[int(len(s) * 0.95)], "p99": s[int(len(s) * 0.99)]}


def summarize(per_query: dict, extra: dict) -> dict:
    result = {"queries": sum(len(v["1"]) for v in per_query.values()), "datasets": {}, "all": {},
              "per_query": per_query, **extra}
    for d, ks in per_query.items():
        if ks["1"]:
            result["datasets"][d] = {k: {"precision": 100 * statistics.mean(p for p, _ in v),
                                         "recall": 100 * statistics.mean(r for _, r in v)} for k, v in ks.items()}
    for k in map(str, K_VALUES):
        result["all"][k] = {m: statistics.mean(result["datasets"][d][k][m] for d in result["datasets"])
                            for m in ("precision", "recall")}
    return result


def run(args, fetch=None) -> dict:
    with open(Path(args.stage) / "queries.jsonl", encoding="utf-8") as f:
        queries = [json.loads(line) for line in f]
    if args.limit:
        queries = queries[:args.limit]
    live = fetch is None
    if live:
        ctx = ssl.create_default_context(cafile=args.ca)
        header = key_header(Path(args.key_header_file).read_text())

        def fetch(q: str) -> dict:
            url = f"{args.entry.rstrip('/')}/search?" + urllib.parse.urlencode({"q": q, "k": args.k})
            req = urllib.request.Request(url, headers={header[0]: header[1]})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return json.load(r)
    kmax = max(K_VALUES)
    per_query = {d: {str(k): [] for k in K_VALUES} for d in DATASETS}
    lat, served, errors = [], Counter(), Counter()
    route = Counter()
    t_start = time.perf_counter()
    for n, q in enumerate(queries, 1):
        t0 = time.perf_counter()
        try:
            body = fetch(q["query"])
        except urllib.error.HTTPError as e:
            errors[str(e.code)] += 1
            e.close()
            body = {"results": []}
        except OSError as e:
            errors[type(e).__name__] += 1
            body = {"results": []}
        lat.append((time.perf_counter() - t0) * 1000)
        found = [(h["file"], h["span"][0], h["span"][1]) for h in body.get("results", [])][:kmax]
        gt = [tuple(s) for s in q["snippets"]]
        for k in K_VALUES:
            per_query[q["dataset"]][str(k)].append(list(precision_recall(found[:k], gt)))
        r = body.get("route")
        if r is None:
            route["plain"] += 1
        elif body.get("not_in_base"):
            route["not_in_base"] += 1
        else:
            route["routed"] += 1
            route["right_document"] += (r.get("document") or {}).get("file") in {s[0] for s in gt}
        served[(body.get("served_by") or {}).get("node") or "?"] += 1
        if args.sleep:
            time.sleep(args.sleep)
        if n % 100 == 0:
            print(f"  {n}/{len(queries)}, {time.perf_counter() - t_start:.0f} с", flush=True)
    extra = {"collection": body.get("collection") if queries else None, "mode": "live",
             "entry": args.entry if live else "test", "k": args.k,
             "route": dict(route), "errors": dict(errors), "served_by": dict(served),
             "latency_ms": {"client": pct(lat), "note": "один клиент, последовательно, через вход (TLS, ключ)"},
             "seconds": round(time.perf_counter() - t_start, 1)}
    return summarize(per_query, extra)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--entry", default="https://192.168.105.2")
    ap.add_argument("--ca", default=str(ROOT / "runs/stand-ca.pem"))
    ap.add_argument("--key-header-file", default=str(ROOT / "runs/entry-api-key.header"))
    ap.add_argument("--stage", default=str(ROOT / "cache/stage1"))
    ap.add_argument("--k", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0, help="только первые N вопросов")
    ap.add_argument("--sleep", type=float, default=0.0, help="пауза между запросами, с")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    res = run(args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res))
    a, rt = res["all"], res["route"]
    print(f"{res['collection']} вживую: вопросов {res['queries']}, recall@1/8/64 = {a['1']['recall']:.1f} / "
          f"{a['8']['recall']:.1f} / {a['64']['recall']:.1f}; среднее по k recall "
          f"{statistics.mean(v['recall'] for v in a.values()):.1f}, precision "
          f"{statistics.mean(v['precision'] for v in a.values()):.2f}")
    print(f"  выбор договора {rt}; ошибки {res['errors'] or 0}; p50 {res['latency_ms']['client']['p50']:.0f} мс, "
          f"p95 {res['latency_ms']['client']['p95']:.0f} мс; машины {res['served_by']}; {res['seconds']} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
