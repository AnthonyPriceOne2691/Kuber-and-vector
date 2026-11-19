"""Сводки проверок безопасности (deploy/security/summary.py) на маленьких отчётах nmap, kube-bench и trivy."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("security_summary", os.path.join(ROOT, "deploy", "security", "summary.py"))
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

MACHINES = {"machine-1": "192.168.105.5", "machine-2": "192.168.105.3", "machine-3": "192.168.105.4",
            "machine-4": "192.168.105.2"}


def host(ip: str, ports: dict[int, str], extra: tuple[str, int] = ("filtered", 65535)) -> str:
    rows = "".join(f'<port protocol="tcp" portid="{p}"><state state="{s}"/></port>' for p, s in ports.items())
    return (f'<host><status state="up"/><address addr="{ip}" addrtype="ipv4"/>'
            f'<address addr="52:55:55:00:00:01" addrtype="mac"/><ports>'
            f'<extraports state="{extra[0]}" count="{extra[1]}"/>{rows}</ports></host>')


def nmap(*hosts: str) -> str:
    return '<?xml version="1.0"?>\n<!DOCTYPE nmaprun>\n<nmaprun scanner="nmap">' + "".join(hosts) + "</nmaprun>"


class Run:
    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name: str, text: str) -> None:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def good_ports(run: Run, entry_ports: dict[int, str] | None = None, knock_extra: str = "") -> None:
    run.write("machines.txt", "\n".join(f"{n}={ip}" for n, ip in MACHINES.items()) + "\n")
    hosts = [host(ip, {}) for n, ip in MACHINES.items() if n != "machine-4"]
    hosts.append(host(MACHINES["machine-4"], entry_ports if entry_ports is not None else {443: "open"}, ("filtered", 65534)))
    run.write("nmap-tcp.xml", nmap(*hosts))
    run.write("nmap-udp.xml", nmap(*(host(ip, {51820: "open|filtered"}, ("filtered", 7)) for ip in MACHINES.values())))
    brute = "".join(f"key guessing, attempt {i}\t{'401' if i <= 10 else '429'}\n" for i in range(1, 16))
    run.write("knock.txt", "Qdrant machine-1:6333 direct\t000\nk3s API machine-1:6443 direct\t000\n"
                           "search through the entry without a key\t401\n" + brute + knock_extra)


class PortsTest(unittest.TestCase):
    def test_only_443_on_entry_is_ok(self):
        run = Run(self)
        good_ports(run)
        text, ok = summary.ports_report(run.dir)
        self.assertTrue(ok, text)
        self.assertIn("| machine-4 | 192.168.105.2 | 443 | none | 65534 | none |", text)
        self.assertIn("| machine-1 | 192.168.105.5 | none | none | 65535 | none |", text)
        self.assertIn("| key guessing, 15 attempts | 401 10 times, 429 from attempt 11 |", text)
        self.assertIn("| Qdrant machine-1:6333 direct | no connection |", text)

    def test_extra_open_port_or_rst_fails(self):
        run = Run(self)
        good_ports(run, {443: "open", 22: "open"})
        self.assertFalse(summary.ports_report(run.dir)[1])
        run2 = Run(self)
        good_ports(run2)
        # машина ответила отказом: пакет дошёл до неё, фильтр не стоит
        run2.write("nmap-tcp.xml", nmap(host(MACHINES["machine-1"], {}, ("closed", 65535)),
                                        *(host(ip, {443: "open"} if n == "machine-4" else {})
                                          for n, ip in MACHINES.items() if n != "machine-1")))
        text, ok = summary.ports_report(run2.dir)
        self.assertFalse(ok)
        self.assertIn("+65535 more", text)

    def test_missing_host_and_answered_knock_fail(self):
        run = Run(self)
        good_ports(run)
        run.write("nmap-tcp.xml", nmap(host(MACHINES["machine-4"], {443: "open"})))
        text, ok = summary.ports_report(run.dir)
        self.assertFalse(ok)
        self.assertIn("missing from the report", text)
        run2 = Run(self)
        good_ports(run2, knock_extra="kubelet machine-1:10250 direct\t401\n")
        self.assertFalse(summary.ports_report(run2.dir)[1], "kubelet ответил снаружи")

    def test_entities_rejected(self):
        with self.assertRaises(ValueError):
            summary.parse_nmap(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><nmaprun>&a;</nmaprun>')


class BenchImagesTest(unittest.TestCase):
    def test_bench_counts_and_lists(self):
        run = Run(self)
        res = [{"test_number": "1.1.1", "test_desc": "Ensure that the API server pod spec file permissions", "status": "PASS"},
               {"test_number": "1.2.6", "test_desc": "Ensure that the --kubelet-certificate-authority | argument", "status": "FAIL"},
               {"test_number": "4.2.1", "test_desc": "Ensure anonymous auth is disabled", "status": "WARN"}]
        run.write("kube-bench.json", json.dumps({"Controls": [{"id": "1", "tests": [{"section": "1.1", "results": res}]}],
                                                 "Totals": {"total_pass": 1, "total_fail": 1}}))
        text, ok = summary.bench_report(run.dir)
        self.assertTrue(ok)
        self.assertIn("Checks 3: PASS 1, FAIL 1, WARN 1, INFO 0.", text)
        self.assertIn("| 1.2.6 | Ensure that the --kubelet-certificate-authority / argument |", text)

    def test_images_dedup_and_fixable(self):
        run = Run(self)
        run.write("images.txt", "localhost_search_stage2.tar\tlocalhost/search:stage2\nq.tar\tdocker.io/qdrant/qdrant:v1.20.2\n")
        v = {"VulnerabilityID": "CVE-1", "PkgName": "openssl", "InstalledVersion": "3.0", "FixedVersion": "3.1", "Severity": "HIGH"}
        run.write("trivy/localhost_search_stage2.tar.json", json.dumps({"Results": [
            {"Target": "os", "Vulnerabilities": [v, dict(v), {**v, "VulnerabilityID": "CVE-2", "FixedVersion": "", "Severity": "CRITICAL"},
                                                 {**v, "VulnerabilityID": "CVE-3", "Severity": "LOW"}]},
            {"Target": "python-pkg"}]}))
        run.write("trivy-db.json", json.dumps({"UpdatedAt": "2026-11-18T13:03:23Z"}))
        text, ok = summary.images_report(run.dir)
        self.assertFalse(ok, "у второго образа нет отчёта")
        self.assertIn("| localhost/search:stage2 | 1 | 1 | 0 | 1 | 0 | 1 |", text)
        self.assertIn("| docker.io/qdrant/qdrant:v1.20.2 | ? | ? | ? | ? | ? | no report |", text)
        self.assertIn("updated 2026-11-18", text)

    def test_cli_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(summary.main(["nope", "x"]), 2)
        self.assertIn("summary.py ports", err.getvalue())


if __name__ == "__main__":
    unittest.main()
