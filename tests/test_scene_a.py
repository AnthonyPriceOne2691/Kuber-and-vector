"""Сцена А (deploy/scenes/scene-a.sh): какую машину выключать по арендам лидеров и какие аргументы годятся.

Функция pick_vm и разбор аргумента вынимаются из скрипта и гоняются в /bin/bash как есть; limactl
подменён функцией, которая сразу падает, поэтому до стенда тест не дотянется. Сама сцена с
выключением машины проверяется только на живом стенде.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"
with open(os.path.join(ROOT, "deploy", "scenes", "scene-a.sh"), encoding="utf-8") as _f:
    SCRIPT = _f.read()
PICK = re.search(r"^pick_vm\(\) \{\n.*?^\}\n", SCRIPT, re.S | re.M).group(0)
ARGS = re.search(r'^ARG="\$\{1:-\}"\n.*?^esac\n', SCRIPT, re.S | re.M).group(0)
CHOOSE = re.search(r'^pick_vm <<<"\$leases"\n.*?^mark "powering off [^\n]*\n', SCRIPT, re.S | re.M).group(0)
GUARD = 'set -euo pipefail\nlimactl() { echo "limactl в тесте" >&2; exit 97; }\n'


def pick(leases: str) -> tuple[str, str, str]:
    script = GUARD + PICK + 'pick_vm <<<"$1"\nprintf "%s\\n%s\\n%s\\n" "$PICK" "$LEADER_VMS" "$LEADERS"\n'
    r = subprocess.run([BASH, "-c", script, "pick", leases], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    vm, leader_vms, leaders = r.stdout.split("\n")[:3]
    return vm, leader_vms.split(), leaders


def lease(kcm: str, sched: str) -> str:
    return f"kube-controller-manager {kcm}\nkube-scheduler {sched}\n"


class PickVmTest(unittest.TestCase):
    def test_leaders_on_machine_2_pick_machine_3(self):
        vm, busy, text = pick(lease("lima-machine-2_6f1c7b8e-0d5e-4c1a-9f0e-1a2b3c4d5e6f", "lima-machine-2_0b9f"))
        self.assertEqual(vm, "machine-3")
        self.assertEqual(busy, ["machine-2", "machine-2"])
        self.assertEqual(text, "kube-controller-manager on machine-2, kube-scheduler on machine-2")

    def test_prefers_machine_2_then_3_then_1(self):
        self.assertEqual(pick(lease("lima-machine-1_a", "lima-machine-1_b"))[0], "machine-2")
        self.assertEqual(pick(lease("lima-machine-1_a", "lima-machine-2_b"))[0], "machine-3")
        self.assertEqual(pick(lease("lima-machine-3_a", "lima-machine-1_b"))[0], "machine-2")
        self.assertEqual(pick(lease("lima-machine-2_a", "lima-machine-3_b"))[0], "machine-1")
        self.assertEqual(pick(lease("lima-machine-3_a", "lima-machine-2_b"))[0], "machine-1")

    def test_never_picks_a_leader(self):
        machines = ("machine-1", "machine-2", "machine-3")
        for kcm in machines:
            for sched in machines:
                vm = pick(lease(f"lima-{kcm}_x", f"lima-{sched}_y"))[0]
                self.assertIn(vm, machines)
                self.assertNotIn(vm, (kcm, sched))

    def test_holder_without_lima_prefix_and_line_order(self):
        vm, _, text = pick("kube-scheduler machine-2_0b9f\nkube-controller-manager machine-1_1c2d\n")
        self.assertEqual(vm, "machine-3")
        self.assertEqual(text, "kube-controller-manager on machine-1, kube-scheduler on machine-2")

    def test_unknown_holders_do_not_block(self):
        vm, busy, text = pick("")
        self.assertEqual((vm, busy), ("machine-2", []))
        self.assertEqual(text, "kube-controller-manager on unknown, kube-scheduler on unknown")
        vm, busy, text = pick(lease("lima-machine-4_x", "something-else"))
        self.assertEqual((vm, busy), ("machine-2", []))
        vm, _, text = pick("kube-controller-manager lima-machine-2_x\n")
        self.assertEqual(vm, "machine-3")
        self.assertEqual(text, "kube-controller-manager on machine-2, kube-scheduler on unknown")


class ChooseVmTest(unittest.TestCase):
    """Кусок сцены от разбора аренд до отметки «powering off»: что печатается и откуда идёт kubectl."""

    def choose(self, leases: str, arg: str = "") -> tuple[list[str], str]:
        script = (GUARD + 'CLUSTER="machine-1 machine-2 machine-3"\nKC_VM=machine-1\nmark() { echo "$*"; }\n'
                  + PICK + 'leases="$1"; ARG="$2"\n' + CHOOSE + 'echo "kubectl via $KC_VM"\n')
        r = subprocess.run([BASH, "-c", script, "choose", leases, arg], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        return lines[:-1], lines[-1]

    def test_default_avoids_leaders(self):
        marks, via = self.choose(lease("lima-machine-2_a", "lima-machine-1_b"))
        self.assertEqual(marks, ["leaders: kube-controller-manager on machine-2, kube-scheduler on machine-1",
                                 "powering off machine-3, it holds no leader lease"])
        self.assertEqual(via, "kubectl via machine-1")

    def test_argument_wins_and_warns(self):
        marks, via = self.choose(lease("lima-machine-1_a", "lima-machine-1_b"), "machine-1")
        self.assertEqual(marks[1], "powering off machine-1, as asked, it holds a leader lease, detection may take longer")
        self.assertEqual(via, "kubectl via machine-2", "kubectl не с выключаемой машины")
        marks, _ = self.choose(lease("lima-machine-1_a", "lima-machine-1_b"), "machine-2")
        self.assertEqual(marks[1], "powering off machine-2, as asked, it holds no leader lease")


class SceneArgsTest(unittest.TestCase):
    def run_args(self, *args: str) -> subprocess.CompletedProcess:
        script = GUARD + ARGS + 'echo "ok $ARG"\n'
        return subprocess.run([BASH, "-c", script, "scene-a", *args], capture_output=True, text=True, timeout=20)

    def test_cluster_machines_and_empty_accepted(self):
        for args, want in (((), "ok "), (("machine-1",), "ok machine-1"), (("machine-3",), "ok machine-3")):
            r = self.run_args(*args)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, want.strip()))

    def test_entry_and_garbage_rejected(self):
        for bad in ("machine-4", "outside", "machine-2; rm -rf /"):
            r = self.run_args(bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("usage:", r.stdout)

    def test_off_window_default(self):
        self.assertRegex(SCRIPT, r'(?m)^OFF="\$\{OFF:-90\}"')


if __name__ == "__main__":
    unittest.main()
