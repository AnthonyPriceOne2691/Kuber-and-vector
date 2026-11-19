"""Итог сцены «заражённый под»: что отбито, какие тревоги поднял Falco и через сколько пришла первая.
  python3 -I -B deploy/scenes/infected_summary.py runs/infected-<время>
Файлы прогона: attack.tsv (печатает attack_pod.py) и falco.log (журнал Falco той же машины,
kubectl logs --timestamps; у прогонов до 19.11, вечер, falco.jsonl без меток).
Начало атаки: запуск оболочки атакующего. Её первая команда date пишет T0; Falco видит и запуск
оболочки, и date, по паре «date у Falco, T0 у пода» узнаём сдвиг часов Falco (shell_started). Время тревоги:
метка журнала контейнера, когда Falco её написал; её ставит containerd той же машины по тем же часам,
что и под, поэтому задержку считаем без поправок. Поле time самого Falco для этого не годится: это
время системного вызова, и Falco переводит его в часы один раз при старте, а после подводки часов
машины (жёсткий перезапуск, Lima подводит часы) оно уезжает; 19.11 на +0,48 с. Его берём только
для старых прогонов без меток.
Код выхода 0, если отбито всё, кроме честного остатка (чтение базы украденным ключом), и тревога была.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

LOG_STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")

SHELL_RULE = "Stand shell in search pod"
EXPECTED_PASS = ("read the database with the pod key",)  # ключ на чтение живёт в поде: читать можно, вынести некуда


def parse_attack(text: str) -> tuple[float, str, list[tuple[float, str, str, bool]]]:
    """(время начала, имя пода, [(время, попытка, итог, отбито)])."""
    start, pod, rows = 0.0, "", []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        ts, what, result, verdict = parts
        if what == "start":
            start, pod = float(ts), result
        elif what != "end":
            rows.append((float(ts), what, result, verdict == "blocked"))
    return start, pod, rows


def falco_time(value: str) -> float:
    """Время Falco «2026-11-19T10:00:00.123456789Z» в секунды эпохи; наносекунды обрезаем до микро."""
    base, _, frac = value.rstrip("Z").partition(".")
    t = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return t + (float("0." + frac[:6]) if frac else 0.0)


def log_time(value: str) -> float:
    """Метка журнала контейнера «2026-11-19T20:28:43.451785625+03:00» (или с Z) в секунды эпохи."""
    m = LOG_STAMP.fullmatch(value)
    if not m:
        raise ValueError(f"не метка времени: {value[:40]}")
    base, frac, tz = m.groups()
    t = datetime.fromisoformat(base + ("+00:00" if tz == "Z" else tz)).timestamp()
    return t + (float("0." + frac[:6]) if frac else 0.0)


def parse_falco(text: str, pod: str) -> list[tuple[float, str, str, float, str]]:
    """Тревоги про этот под: [(время в журнале, правило, важность, время Falco, процесс)]. Строки без
    JSON и служебные события самого Falco («Falco internal», например потеря событий) пропускаем."""
    alerts = []
    for line in text.splitlines():
        line = line.strip()
        stamp = None
        if not line.startswith("{"):
            head, _, rest = line.partition(" ")
            if not rest.startswith("{"):
                continue
            try:
                stamp = log_time(head)
            except ValueError:
                continue
            line = rest
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or "rule" not in ev or "time" not in ev:
            continue
        if str(ev["rule"]).startswith("Falco internal"):
            continue
        fields = ev.get("output_fields") or {}
        if pod and fields.get("k8s.pod.name") not in (None, pod):
            continue
        event_t = falco_time(ev["time"])
        t = stamp if stamp is not None else event_t
        alerts.append((t, str(ev["rule"]), str(ev.get("priority", "")), event_t, str(fields.get("proc.name") or "")))
    return sorted(alerts)


def shell_started(alerts: list[tuple[float, str, str, float, str]], t0: float) -> float:
    """Когда запустилась оболочка атакующего, по часам пода. T0 пишет команда date уже внутри оболочки,
    то есть чуть позже её запуска. Falco видит запуск и оболочки, и date, но его часы сдвинуты: сдвиг
    берём по date (время Falco минус T0) и переносим на событие оболочки. Без тревоги на date
    (прогоны до 19.11, вечер) началом остаётся T0."""
    date = next((a for a in alerts if a[4] == "date"), None)
    shell = next((a for a in alerts if a[1] == SHELL_RULE), None)
    if date is None or shell is None:
        return t0
    return min(t0, shell[3] - (date[3] - t0))


def report(run: str) -> tuple[str, bool]:
    with open(os.path.join(run, "attack.tsv"), encoding="utf-8") as f:
        start, pod, rows = parse_attack(f.read())
    alerts = []
    for name in ("falco.log", "falco.jsonl"):
        falco_path = os.path.join(run, name)
        if os.path.exists(falco_path):
            with open(falco_path, encoding="utf-8") as f:
                alerts = parse_falco(f.read(), pod)
            break
    alerts = [a for a in alerts if a[0] >= start - 1]  # старые тревоги до атаки не считаем
    begin = shell_started(alerts, start)
    must = [r for r in rows if r[1] not in EXPECTED_PASS]
    blocked = sum(r[3] for r in must)
    lines = [f"## Infected pod {pod or '?'}: attempts from inside", "", "| Attempt | Result | |", "|---|---|---|"]
    lines += [f"| {what} | {result} | {'blocked' if b else 'passed'} |" for _, what, result, b in rows]
    lines += ["", f"Blocked {blocked} of {len(must)}."]
    passed = [r[1] for r in rows if r[1] in EXPECTED_PASS and not r[3]]
    if passed:
        lines.append("Reading the database went through: the read-only key lives in the pod and leaves with the shell. There is nowhere to send what was read, every exit above is closed.")
    if alerts:
        first = (alerts[0][0] - begin) * 1000
        by_rule: dict[str, list[float]] = {}
        for t, rule, *_ in alerts:
            by_rule.setdefault(rule, []).append(t)
        lines += ["", f"Falco alerts {len(alerts)}, the first {first:.0f} ms after the attacker got a shell.", "",
                  "| Rule | Alerts | First alert |", "|---|---|---|"]
        lines += [f"| {rule} | {len(ts)} | {(ts[0] - begin) * 1000:.0f} ms |"
                  for rule, ts in sorted(by_rule.items(), key=lambda x: x[1][0])]
    else:
        lines += ["", "No Falco alerts."]
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
