"""Итог генератора нагрузки для сцены: что увидел пользователь и что увидел бы без повтора.

    python3 -I load_summary.py runs/scene-a-load.json

Читает снимок loadgen/1 (panel/loadgen.py --stats-file). Итоги за весь прогон, задержки
по ряду за последние до 180 с (столько держит генератор).
"""
from __future__ import annotations

import json
import sys


def summary(snap: dict) -> str:
    t = snap["totals"]
    wo, wr = t["without_retry"], t["with_retry"]
    series = snap.get("series") or []
    p95 = max((p.get("p95") or 0 for p in series), default=0)
    p99 = max((p.get("p99") or 0 for p in series), default=0)
    bad_seconds = sum(1 for p in series if p.get("errors_with_retry"))
    retry_seconds = sum(1 for p in series if p.get("retries"))
    return "\n".join([
        f"запросов {t['count']}, пропущено по потолку {t.get('skipped', 0)}",
        f"ошибок у пользователя {wr['errors']} ({100 * wr['rate']:.2f} %), без повтора было бы {wo['errors']} "
        f"({100 * wo['rate']:.2f} %), повторов {t['retries']}",
        f"за последние {len(series)} с: худший p95 {p95} мс, худший p99 {p99} мс, секунд с ошибками {bad_seconds}, "
        f"секунд с повторами {retry_seconds}",
    ])


def main() -> None:
    with open(sys.argv[1]) as f:
        print(summary(json.load(f)))


if __name__ == "__main__":
    main()
