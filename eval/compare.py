"""Парное сравнение двух замеров LegalBench-RAG на тех же вопросах (бутстреп из eval/report.py).

    python3 -I -B eval/compare.py НОВЫЙ.json БАЗА.json [--md файл.md]

Печатает recall@k по наборам и в среднем, разницу «новый − база» с 95 % интервалом для каждого k
и среднее по k (как в статье SAC: recall и precision, усреднённые по k = 1…64). Разница, интервал
которой накрывает ноль, не считается ни улучшением, ни ухудшением.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # с -I папка скрипта сама в путь не попадает

from eval.report import DS, K, NAMES, boot, fmt  # noqa: E402


def same_questions(a: dict, b: dict) -> None:
    """Сравнивать можно только одинаковые вопросы в одинаковом порядке."""
    for d in DS:
        na, nb = len(a["per_query"][d]["1"]), len(b["per_query"][d]["1"])
        if na != nb:
            raise SystemExit(f"{d}: вопросов {na} и {nb}, сравнение не парное")


def over_k(res: dict, what: str) -> float:
    return statistics.mean(res["all"][k][what] for k in K)


def report(new: dict, base: dict) -> list[str]:
    same_questions(new, base)
    name = lambda r: f"{r['collection']} [{r['mode']}]"  # noqa: E731
    out = [f"Новый {name(new)} против базы {name(base)}, вопросов {new['queries']}", "",
           "| k | база | новый | разница [95 %] |", "|---|---|---|---|"]
    for k in K:
        out.append(f"| {k} | {base['all'][k]['recall']:.1f} | {new['all'][k]['recall']:.1f} | "
                   f"{fmt(boot(new, k, base), sign=True)} |")
    out += ["", "| набор | база @8 | новый @8 | база @64 | новый @64 |", "|---|---|---|---|---|"]
    for d in DS:
        b, n = base["datasets"][d], new["datasets"][d]
        out.append(f"| {NAMES[d]} | {b['8']['recall']:.1f} | {n['8']['recall']:.1f} | "
                   f"{b['64']['recall']:.1f} | {n['64']['recall']:.1f} |")
    out += ["", f"Среднее по k: recall {over_k(base, 'recall'):.2f} → {over_k(new, 'recall'):.2f}, "
                f"precision {over_k(base, 'precision'):.2f} → {over_k(new, 'precision'):.2f}"]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("new")
    ap.add_argument("base")
    ap.add_argument("--md", help="дописать таблицы в этот файл")
    args = ap.parse_args(argv)
    with open(args.new) as fn, open(args.base) as fb:
        lines = report(json.load(fn), json.load(fb))
    print("\n".join(lines))
    if args.md:
        with open(args.md, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
