"""Сводки проверок безопасности стенда в Markdown: скан портов снаружи (nmap), CIS (kube-bench),
уязвимости образов (trivy). Читает папку прогона runs/security/<время>/, пишет сводку в stdout.
Отчёты сканеров это чужие данные: XML без объявлений ENTITY и не больше 64 МБ, JSON через json.
  python3 -I -B deploy/security/summary.py ports  runs/security/<время>
  python3 -I -B deploy/security/summary.py bench  runs/security/<время>
  python3 -I -B deploy/security/summary.py images runs/security/<время>
Код выхода 0, если итог как ждали (ports: открыт только 443 на входе; bench и images: отчёты разобраны).
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

MAX_BYTES = 64 * 1024 * 1024
EXPECTED_OPEN = {"machine-4": {443}}  # снаружи открыт только вход, и только 443
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def _read(path: str) -> bytes:
    if os.path.getsize(path) > MAX_BYTES:
        raise ValueError(f"{path}: larger than {MAX_BYTES} bytes")
    with open(path, "rb") as f:
        return f.read()


def parse_nmap(data: bytes) -> dict[str, dict]:
    """адрес -> {"open": {порт}, "closed": {порт}, "closed_more": число, "silent": число}.
    closed значит, что машина ответила отказом; silent это filtered и open|filtered (ответа не было)."""
    if b"<!ENTITY" in data:
        raise ValueError("nmap report declares ENTITY, refusing to parse it")
    root = ET.fromstring(data)
    hosts: dict[str, dict] = {}
    for host in root.iter("host"):
        addr = next((a.get("addr") for a in host.iter("address") if a.get("addrtype") == "ipv4"), None)
        if not addr:
            continue
        h = hosts.setdefault(addr, {"open": set(), "closed": set(), "closed_more": 0, "silent": 0})
        for port in host.iter("port"):
            state = port.find("state")
            st = state.get("state", "") if state is not None else ""
            num = int(port.get("portid", "0"))
            if st == "open":
                h["open"].add(num)
            elif st == "closed":
                h["closed"].add(num)
            else:  # filtered, open|filtered: ответа не было
                h["silent"] += 1
        for extra in host.iter("extraports"):
            if extra.get("state") == "closed":
                h["closed_more"] += int(extra.get("count", "0"))  # закрытые без списка портов
            else:
                h["silent"] += int(extra.get("count", "0"))
    return hosts


def _machines(run: str) -> list[tuple[str, str]]:
    out = []
    with open(os.path.join(run, "machines.txt"), encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                name, ip = line.strip().split("=", 1)
                out.append((name, ip))
    return out


def _fmt_ports(ports: set[int], more: int = 0) -> str:
    parts = [", ".join(str(p) for p in sorted(ports))] if ports else []
    if more:
        parts.append(f"+{more} more")
    return "; ".join(parts) if parts else "none"


def _knock(run: str) -> list[tuple[str, str]]:
    path = os.path.join(run, "knock.txt")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\t" in line:
                what, code = line.rstrip("\n").split("\t", 1)
                rows.append((what, code.strip() or "000"))
    return rows


def ports_report(run: str) -> tuple[str, bool]:
    tcp = parse_nmap(_read(os.path.join(run, "nmap-tcp.xml")))
    udp_path = os.path.join(run, "nmap-udp.xml")
    udp = parse_nmap(_read(udp_path)) if os.path.exists(udp_path) else {}
    ok = True
    lines = ["## Port scan from outside", "",
             "From the outside machine on the machine network: every TCP port and selected UDP ports (nmap -sS -p-, -sU).",
             "Silence (packet dropped) shows in nmap as filtered or open|filtered; closed means the machine answered with a refusal.",
             "", "| Machine | Address | Open TCP | Refused TCP (closed) | Silent TCP | Open UDP |", "|---|---|---|---|---|---|"]
    for name, ip in _machines(run):
        h = tcp.get(ip)
        if h is None:  # машины нет в отчёте: скан не дошёл, это не «всё закрыто»
            ok = False
            lines.append(f"| {name} | {ip} | missing from the report | | | |")
            continue
        u = udp.get(ip, {"open": set()})
        if h["open"] != EXPECTED_OPEN.get(name, set()) or h["closed"] or h["closed_more"] or u["open"]:
            ok = False
        lines.append(f"| {name} | {ip} | {_fmt_ports(h['open'])} | {_fmt_ports(h['closed'], h['closed_more'])} "
                     f"| {h['silent']} | {_fmt_ports(u['open'])} |")
    knock = _knock(run)
    if knock:
        lines += ["", "| Manual knock | Answer |", "|---|---|"]
        brute = [code for what, code in knock if what.startswith("key guessing")]
        for what, code in knock:
            if not what.startswith("key guessing"):
                lines.append(f"| {what} | {'no connection' if code == '000' else code} |")
        if brute:
            first429 = next((i for i, c in enumerate(brute, 1) if c == "429"), None)
            tail = f", 429 from attempt {first429}" if first429 else ", no 429"
            lines.append(f"| key guessing, {len(brute)} attempts | 401 {brute.count('401')} times{tail} |")
            ok = ok and first429 is not None
        direct = [code for what, code in knock if what.endswith(" direct")]
        ok = ok and all(code == "000" for code in direct)
        ok = ok and all(code == "401" for what, code in knock if "without a key" in what)
    lines += ["", f"Result: {'as expected, from outside only 443 on the entry is open' if ok else 'NOT as expected, see the tables'}."]
    return "\n".join(lines) + "\n", ok


def _bench_results(node) -> list[dict]:
    """Все записи с test_number и status, где бы они ни лежали в отчёте kube-bench."""
    found = []
    if isinstance(node, dict):
        if "test_number" in node and "status" in node:
            found.append(node)
        for v in node.values():
            found += _bench_results(v)
    elif isinstance(node, list):
        for v in node:
            found += _bench_results(v)
    return found


def bench_report(run: str) -> tuple[str, bool]:
    report = json.loads(_read(os.path.join(run, "kube-bench.json")))
    results = _bench_results(report)
    counts = {s: sum(r.get("status") == s for r in results) for s in ("PASS", "FAIL", "WARN", "INFO")}
    lines = ["## Kubernetes against CIS (kube-bench)", "",
             f"Checks {len(results)}: PASS {counts['PASS']}, FAIL {counts['FAIL']}, WARN {counts['WARN']}, INFO {counts['INFO']}.",
             "The profile targets Kubernetes 1.27 to 1.29; FAIL and WARN are reviewed by hand against the k3s self-assessment before they count."]
    for status in ("FAIL", "WARN"):
        rows = [r for r in results if r.get("status") == status]
        if rows:
            lines += ["", f"| {status} | Check |", "|---|---|"]
            lines += [f"| {r.get('test_number')} | {str(r.get('test_desc', '')).replace('|', '/')} |" for r in rows]
    return "\n".join(lines) + "\n", bool(results)


def images_report(run: str) -> tuple[str, bool]:
    names = {}
    with open(os.path.join(run, "images.txt"), encoding="utf-8") as f:
        for line in f:
            if "\t" in line:
                file, img = line.rstrip("\n").split("\t", 1)
                names[file] = img
    db = ""
    db_path = os.path.join(run, "trivy-db.json")
    if os.path.exists(db_path):
        db = json.loads(_read(db_path)).get("UpdatedAt", "")[:10]
    lines = ["## Image vulnerabilities (trivy)", "",
             f"Vulnerability database from the kit, updated {db or 'on an unknown date'}; the scanner runs offline.", "",
             "| Image | " + " | ".join(SEVERITIES) + " | Critical and high with a fix |",
             "|---|" + "---|" * (len(SEVERITIES) + 1)]
    ok = bool(names)
    for file, img in names.items():
        path = os.path.join(run, "trivy", f"{file}.json")
        if not os.path.exists(path):
            lines.append(f"| {img} | " + " | ".join("?" for _ in SEVERITIES) + " | no report |")
            ok = False
            continue
        report = json.loads(_read(path))
        vulns = [v for r in report.get("Results") or [] for v in r.get("Vulnerabilities") or []]
        seen, uniq = set(), []
        for v in vulns:  # одна и та же CVE в одном пакете считается один раз
            key = (v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion"))
            if key not in seen:
                seen.add(key)
                uniq.append(v)
        by = {s: sum(v.get("Severity", "UNKNOWN") == s for v in uniq) for s in SEVERITIES}
        fixable = sum(v.get("Severity") in ("CRITICAL", "HIGH") and bool(v.get("FixedVersion")) for v in uniq)
        lines.append(f"| {img} | " + " | ".join(str(by[s]) for s in SEVERITIES) + f" | {fixable} |")
    return "\n".join(lines) + "\n", ok


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    modes = {"ports": ports_report, "bench": bench_report, "images": images_report}
    if len(argv) != 2 or argv[0] not in modes:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    text, ok = modes[argv[0]](argv[1])
    sys.stdout.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
