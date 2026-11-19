"""Итог сцены «заражённый под»: что отбито, какие тревоги поднял Falco и через сколько пришла первая.
  python3 -I -B deploy/scenes/infected_summary.py runs/infected-<время>
Файлы прогона: attack.tsv (печатает attack_pod.py) и falco.jsonl (журнал Falco той же машины).
Время атаки и время тревог идут по часам одной машины, поэтому задержку считаем без поправок.
Код выхода 0, если отбито всё, кроме честного остатка (чтение базы украденным ключом), и тревога была.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

EXPECTED_PASS = ("прочитать базу ключом пода",)  # ключ на чтение живёт в поде: читать можно, вынести некуда


def parse_attack(text: str) -> tuple[float, str, list[tuple[float, str, str, bool]]]:
    """(время начала, имя пода, [(время, попытка, итог, отбито)])."""
    start, pod, rows = 0.0, "", []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        ts, what, result, verdict = parts
        if what == "начало":
            start, pod = float(ts), result
        elif what != "конец":
            rows.append((float(ts), what, result, verdict == "отбито"))
    return start, pod, rows


def falco_time(value: str) -> float:
    """Время Falco «2026-11-19T10:00:00.123456789Z» в секунды эпохи; наносекунды обрезаем до микро."""
    base, _, frac = value.rstrip("Z").partition(".")
    t = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return t + (float("0." + frac[:6]) if frac else 0.0)


def parse_falco(text: str, pod: str) -> list[tuple[float, str, str]]:
    """Тревоги про этот под: [(время, правило, важность)]. Строки не JSON пропускаем."""
    alerts = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or "rule" not in ev or "time" not in ev:
            continue
        fields = ev.get("output_fields") or {}
        if pod and fields.get("k8s.pod.name") not in (None, pod):
            continue
        alerts.append((falco_time(ev["time"]), str(ev["rule"]), str(ev.get("priority", ""))))
    return sorted(alerts)


def report(run: str) -> tuple[str, bool]:
    with open(os.path.join(run, "attack.tsv"), encoding="utf-8") as f:
        start, pod, rows = parse_attack(f.read())
    falco_path = os.path.join(run, "falco.jsonl")
    alerts = []
    if os.path.exists(falco_path):
        with open(falco_path, encoding="utf-8") as f:
            alerts = parse_falco(f.read(), pod)
    alerts = [a for a in alerts if a[0] >= start - 1]  # старые тревоги до атаки не считаем
    must = [r for r in rows if r[1] not in EXPECTED_PASS]
    blocked = sum(r[3] for r in must)
    lines = [f"## Заражённый под {pod or '?'}: попытки изнутри", "", "| Попытка | Итог | |", "|---|---|---|"]
    lines += [f"| {what} | {result} | {'отбито' if b else 'прошло'} |" for _, what, result, b in rows]
    lines += ["", f"Отбито {blocked} из {len(must)}."]
    passed = [r[1] for r in rows if r[1] in EXPECTED_PASS and not r[3]]
    if passed:
        lines.append("Прошло чтение базы: ключ на чтение лежит в поде, его уносят вместе с оболочкой. Вынести прочитанное некуда, все выходы выше закрыты.")
    if alerts:
        first = (alerts[0][0] - start) * 1000
        by_rule: dict[str, int] = {}
        for _, rule, _ in alerts:
            by_rule[rule] = by_rule.get(rule, 0) + 1
        lines += ["", f"Тревог Falco {len(alerts)}, первая через {first:.0f} мс после начала атаки.", "",
                  "| Правило | Тревог |", "|---|---|"]
        lines += [f"| {rule} | {n} |" for rule, n in sorted(by_rule.items(), key=lambda x: -x[1])]
    else:
        lines += ["", "Тревог Falco нет."]
    ok = bool(must) and blocked == len(must) and bool(alerts)
    return "\n".join(lines) + "\n", ok


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    text, ok = report(argv[0])
    sys.stdout.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
