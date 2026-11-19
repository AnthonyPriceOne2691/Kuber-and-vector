"""Тихий подъём для кадра (QUIET=1 deploy/stand-up.sh) и сводка в конце (deploy/stand_summary.py), без стенда.

Кусок stand-up.sh между метками «тихий режим» гоняется в /bin/bash (на Маке это bash 3.2) с поддельными
шагами: в терминал идут только английские заголовки, всё остальное в журнал runs/stand-up-*.log; при
ошибке хвост журнала без управляющих символов и ненулевой код. limactl в этих прогонах подменён
функцией, которая сразу падает, так что до стенда тест не дотянется.
"""
from __future__ import annotations

import contextlib
import glob
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"
with open(os.path.join(ROOT, "deploy", "stand-up.sh"), encoding="utf-8") as _f:
    SCRIPT = _f.read()
BLOCK = re.search(r"^# --- тихий режим[^\n]*\n(.*?)^# --- конец тихого режима ---$", SCRIPT, re.S | re.M).group(1)
GUARD = 'limactl() { echo "limactl в тесте" >&2; exit 97; }\n'
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_spec = importlib.util.spec_from_file_location("stand_summary", os.path.join(ROOT, "deploy", "stand_summary.py"))
stand_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stand_summary)


def run_block(body: str, quiet: bool = True, env: dict | None = None):
    """Кусок тихого режима и тело-подделка шагов; возвращает (процесс, текст журнала или None)."""
    with tempfile.TemporaryDirectory() as d:
        script = (
            "set -euo pipefail\n" + GUARD
            + f"ROOT={shlex.quote(d)}\nQUIET={1 if quiet else 0}\nt0=$(date +%s)\n"
            + BLOCK + "quiet_start\n" + body
        )
        r = subprocess.run([BASH, "-c", script], capture_output=True, timeout=30,
                           env={**os.environ, **(env or {})})
        logs = glob.glob(os.path.join(d, "runs", "stand-up-*.log"))
        log = None
        if logs:
            with open(logs[0], encoding="utf-8") as f:
                log = f.read()
        return r, log


class QuietBlockTest(unittest.TestCase):
    def test_block_found_and_offline(self):
        self.assertIn("quiet_start()", BLOCK)
        self.assertNotIn("limactl", BLOCK, "тихий режим не должен ходить на стенд")

    def test_terminal_gets_only_english_headers(self):
        r, log = run_block(
            'step "машины" "four VMs from one template"\n'
            'echo "подробный вывод"\n'
            'echo "ошибка в stderr" >&2\n'
            'step "данные" "data into Qdrant"\n'
            'say "stand ready in $(since) s"\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode("utf-8")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertRegex(lines[0], r"^  \+\d+ s +four VMs from one template$")
        self.assertRegex(lines[1], r"^  \+\d+ s +data into Qdrant$")
        self.assertRegex(lines[2], r"^stand ready in \d+ s$")
        self.assertIsNone(re.search(r"[а-яА-ЯёЁ]", out), "по-русски в кадр ничего")
        self.assertEqual(r.stderr, b"", "stderr шагов тоже в журнал")
        self.assertIsNotNone(log)
        self.assertRegex(log, r"(?m)^== \d+ с: машины$")
        self.assertRegex(log, r"(?m)^== \d+ с: данные$")
        for must in ("подробный вывод", "ошибка в stderr"):
            self.assertIn(must, log)
        self.assertNotIn("four VMs", log, "английские заголовки только в терминал")

    def test_failed_step_prints_clean_tail_and_exit_code(self):
        r, log = run_block(
            'step "машины" "four VMs from one template"\n'
            'echo "строка перед ошибкой"\n'
            "printf '\\033[31mкрасная ошибка\\033[0m\\r\\n'\n"
            "printf 'прогресс 10%%\\rпрогресс 100%%\\n'\n"
            "false\n"
            'echo "сюда не дойдёт"\n')
        self.assertEqual(r.returncode, 1)
        out = r.stdout.decode("utf-8")
        self.assertIn("FAILED with exit code 1 at: four VMs from one template", out)
        self.assertIn("  | строка перед ошибкой", out)
        self.assertIn("  | красная ошибка", out)
        self.assertIn("  | прогресс 10%прогресс 100%", out)
        self.assertIsNone(CONTROL.search(out), repr(out))
        self.assertIn("\x1b[31m", log, "в журнале вывод как есть")
        self.assertNotIn("сюда не дойдёт", log)

    def test_exit_code_passes_through(self):
        r, _ = run_block('step "шаг" "a step"\nexit 3\n')
        self.assertEqual(r.returncode, 3)
        self.assertIn("FAILED with exit code 3 at: a step", r.stdout.decode("utf-8"))

    def test_failure_before_first_step(self):
        r, _ = run_block("false\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("at: start", r.stdout.decode("utf-8"))

    def test_tail_length(self):
        body = 'step "шаг" "a step"\n' + "".join(f'echo "строка {i}"\n' for i in range(30)) + "false\n"
        r, _ = run_block(body, env={"QUIET_TAIL": "3"})
        out = r.stdout.decode("utf-8")
        self.assertEqual([x for x in out.splitlines() if x.startswith("  | ")],
                         ["  | строка 27", "  | строка 28", "  | строка 29"])

    def test_verbose_mode_unchanged(self):
        r, log = run_block('step "машины" "four VMs from one template"\necho "подробный вывод"\n'
                           'say "stand ready"\n', quiet=False)
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.decode("utf-8"), r"\A\n== \d+ с: машины\nподробный вывод\n\Z")
        self.assertIsNone(log, "без QUIET журнал не пишется")


class StepHeadersTest(unittest.TestCase):
    def test_every_step_has_short_english_header(self):
        calls = re.findall(r"^step (.*)$", SCRIPT, re.M)
        pairs = re.findall(r'^step "([^"]+)" "([^"]+)"$', SCRIPT, re.M)
        self.assertGreaterEqual(len(calls), 15)
        self.assertEqual(len(pairs), len(calls), "у каждого шага два заголовка: по-русски и по-английски")
        for _, en in pairs:
            self.assertRegex(en, r"^[ -~]+$", f"в кадр только ASCII: {en}")
            self.assertLessEqual(len(en) + 11, 100, f"строка шире кадра: {en}")

    def test_default_sa_step_after_all_installs(self):
        """CIS 5.1.5 шагом после панели (она создаёт stand-panel) и до политики образов, которая последняя."""
        sa = SCRIPT.index('"$HERE/k3s/default-sa.sh" machine-1')
        self.assertLess(SCRIPT.index('"$HERE/panel/install.sh"'), sa)
        self.assertLess(sa, SCRIPT.index('"$HERE/k3s/image-policy.sh"'))


def node(name: str, ready: bool = True, version: str = "v1.37.2+k3s1") -> dict:
    return {"kind": "Node", "metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "nodeInfo": {"kubeletVersion": version}}}


def pod(ns: str, name: str, on: str, labels: dict, ready: bool = True, deleting: bool = False) -> dict:
    meta = {"namespace": ns, "name": name, "labels": labels}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-19T20:00:00Z"
    return {"kind": "Pod", "metadata": meta, "spec": {"nodeName": on},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


QD = {"app.kubernetes.io/name": "qdrant"}
SEARCH = {"app": "search"}
MACHINES = ("machine-1", "machine-2", "machine-3")


def healthy_kube() -> dict:
    items = [node(m) for m in MACHINES]
    items += [pod("qdrant", f"qdrant-{i}", m, QD) for i, m in enumerate(MACHINES)]
    items += [pod("search", f"search-{i}", m, SEARCH) for i, m in enumerate(MACHINES)]
    items += [pod("qdrant", "qdrant-gate-x", "machine-1", {"app": "qdrant-gate"}),
              pod("kube-system", "coredns-1", "machine-1", {"k8s-app": "kube-dns"})]
    return {"kind": "List", "items": items}


GATE = {"result": {"status": "enabled", "peers": {"1": {}, "2": {}, "3": {}}, "raft_info": {"leader": 2}}}
LOG = "...\nитого: 61 ✓, 0 ✗\n...\nитого: 62 ✓, 0 ✗\n"


class SummaryTest(unittest.TestCase):
    def test_healthy_stand(self):
        lines = stand_summary.summary(healthy_kube(), GATE, "192.168.105.2", LOG)
        self.assertEqual(lines, [
            "  nodes    3 of 3 Ready (machine-1, machine-2, machine-3), k3s v1.37.2+k3s1",
            "  Qdrant   3 peers in consensus, one per machine",
            "  search   3 pods Ready (machine-1, machine-2, machine-3)",
            "  entry    https://192.168.105.2, HAProxy on machine-4 with TLS and an API key",
            "  checks   62 of 62 passed",
        ])
        for text in lines:
            self.assertRegex(text, r"^[ -~]+$")
            self.assertLessEqual(len(text), 100)

    def test_degraded_stand(self):
        kube = healthy_kube()
        kube["items"][1] = node("machine-2", ready=False)
        kube["items"] = [it for it in kube["items"] if it["metadata"].get("name") != "search-1"]
        kube["items"].append(pod("search", "search-old", "machine-2", SEARCH, deleting=True))
        kube["items"].append(pod("search", "search-new", "machine-3", SEARCH, ready=False))
        lines = stand_summary.summary(kube, {"result": {"peers": {"1": {}, "2": {}}, "raft_info": {"leader": None}}},
                                      "", "итого: 60 ✓, 2 ✗\n")
        self.assertEqual(lines[0], "  nodes    2 of 3 Ready (machine-1, machine-3), k3s v1.37.2+k3s1")
        self.assertEqual(lines[1], "  Qdrant   2 peers in consensus, no raft leader yet, 3 pods Ready")
        self.assertEqual(lines[2], "  search   2 pods Ready (machine-1, machine-3)")
        self.assertEqual(lines[3], "  entry    unknown, no address on machine-4")
        self.assertEqual(lines[4], "  checks   60 of 62 passed, 2 failed")

    def test_nothing_answered(self):
        lines = stand_summary.summary(None, None, "", None)
        self.assertEqual([x.split()[0] for x in lines], ["nodes", "Qdrant", "search", "entry", "checks"])
        self.assertTrue(all("unknown" in x for x in lines), lines)

    def test_gate_silent_pods_ready(self):
        line = stand_summary.qdrant_line(healthy_kube(), None)
        self.assertEqual(line, "  Qdrant   3 peers Ready, one per machine, consensus unknown (gate did not answer)")

    def test_main_reads_files_and_survives_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for name, text in (("kube", json.dumps(healthy_kube())), ("gate", "not json"), ("log", LOG)):
                paths[name] = os.path.join(d, name)
                with open(paths[name], "w", encoding="utf-8") as f:
                    f.write(text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = stand_summary.main(["--kube", paths["kube"], "--gate", paths["gate"], "--entry", " 192.168.105.2\n",
                                           "--check-log", paths["log"]])
                missing = stand_summary.main(["--kube", os.path.join(d, "missing"), "--check-log", os.path.join(d, "no.log")])
        self.assertEqual((code, missing), (0, 0))
        out = buf.getvalue().splitlines()
        self.assertEqual(len(out), 10)
        self.assertIn("consensus unknown", out[1])
        self.assertTrue(out[3].startswith("  entry    https://192.168.105.2,"))
        self.assertTrue(all("unknown" in x for x in out[5:]), out[5:])


if __name__ == "__main__":
    unittest.main()
