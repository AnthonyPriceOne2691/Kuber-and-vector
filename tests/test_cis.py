"""Правки по kube-bench k3s-cis-1.9 (прогон 19.11) без стенда.

- 5.1.5: удалённая часть deploy/k3s/default-sa.sh гоняется в /bin/bash с поддельным kubectl;
- 5.1.5 и 5.1.6: функции проверок из deploy/check.sh гоняются с поддельным kc, в том числе на
  длинном выводе, где под pipefail код kubectl мог бы перебить результат поиска;
- 5.1.6: Qdrant и Falco без своих учёток, токен у default выключается до установки чарта;
- 1.1.20: команда ExecStartPost из правила systemd в deploy/k3s/cis.sh разбирается, как её разберёт
  systemd, и выполняется на временной папке: *.crt становятся 600, остальное не трогается.
limactl в прогонах подменён функцией, которая сразу падает: до стенда тест не дотянется.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash"
GUARD = 'limactl() { echo "limactl в тесте" >&2; exit 97; }\n'


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def function(text: str, name: str) -> str:
    return re.search(rf"^{name}\(\) \{{.*?^\}}\n", text, re.S | re.M).group(0)


DEFAULT_SA_BODY = re.search(r"sudo bash -s <<'EOS'\n(.*?\n)EOS\n", read("deploy", "k3s", "default-sa.sh"), re.S).group(1)
CHECK = read("deploy", "check.sh")

FAKE_KUBECTL = r'''
kubectl() {
  local ns="" body
  if [ "$1" = -n ]; then ns="$2"; shift 2; fi
  echo "${ns:-_} $*" >> "$STATE/calls"
  case "$1 $2" in
    "get namespaces")
      case "$*" in
        *'status.phase=="Active"'*) awk '$2 == "Active" {printf "%s ", $1}' "$STATE/namespaces" ;;
        *) awk '{printf "%s ", $1}' "$STATE/namespaces" ;;
      esac ;;
    "get serviceaccount")
      [ -f "$STATE/sa-$ns" ] || { echo "Error from server (NotFound)" >&2; return 1; }
      cat "$STATE/sa-$ns" ;;
    "apply --server-side")
      body="$(cat)"
      case "$body" in *"name: default"*"automountServiceAccountToken: false"*) ;; *) echo "bad manifest" >&2; return 1 ;; esac
      case "$*" in *--field-manager=stand-default-sa*) ;; *) echo "no field manager" >&2; return 1 ;; esac
      printf false > "$STATE/sa-$ns"; echo "$ns" >> "$STATE/applied" ;;
    *) echo "unexpected: kubectl $*" >&2; return 1 ;;
  esac
}
'''


class DefaultSaScriptTest(unittest.TestCase):
    """5.1.5 для всех пространств, кроме kube-system: что выключено, что уже было, что пропущено."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        namespaces = ["default Active", "kube-node-lease Active", "kube-public Active", "kube-system Active",
                      "qdrant Active", "falco Active", "search Active", "stand-panel Active", "old Terminating"]
        self.write("namespaces", "\n".join(namespaces) + "\n")
        # пусто: поле не задано; stand-panel: контроллер учётку ещё не создал
        for ns, value in (("default", ""), ("kube-node-lease", "true"), ("kube-public", ""), ("kube-system", ""),
                          ("qdrant", "false"), ("falco", "false"), ("search", ""), ("old", "")):
            self.write(f"sa-{ns}", value)

    def write(self, name: str, text: str) -> None:
        with open(os.path.join(self.state, name), "w", encoding="utf-8") as f:
            f.write(text)

    def read_state(self, name: str) -> str:
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def run_body(self) -> subprocess.CompletedProcess:
        script = GUARD + FAKE_KUBECTL + DEFAULT_SA_BODY
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "STATE": self.state})

    def test_turns_off_everywhere_but_kube_system(self):
        r = self.run_body()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_state("applied").split(),
                         ["default", "kube-node-lease", "kube-public", "search", "stand-panel"])
        for ns in ("default", "kube-node-lease", "kube-public", "search", "stand-panel", "qdrant", "falco"):
            self.assertEqual(self.read_state(f"sa-{ns}"), "false", ns)
        self.assertEqual(self.read_state("sa-kube-system"), "", "kube-system не трогаем")
        self.assertEqual(self.read_state("sa-old"), "", "пространство на удалении не трогаем")
        self.assertIn("выключен сейчас: default kube-node-lease kube-public search stand-panel;", r.stdout)
        self.assertIn("уже был выключен: qdrant falco", r.stdout)

    def test_second_run_changes_nothing(self):
        self.assertEqual(self.run_body().returncode, 0)
        os.remove(os.path.join(self.state, "applied"))
        r = self.run_body()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_state("applied"), "")
        self.assertIn("выключен сейчас: нигде;", r.stdout)

    def test_kubectl_failure_stops_the_script(self):
        script = GUARD + FAKE_KUBECTL.replace('printf false > "$STATE/sa-$ns"', 'return 1') + DEFAULT_SA_BODY
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "STATE": self.state})
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("выключен сейчас", r.stdout)


class CheckFunctionsTest(unittest.TestCase):
    """Проверки 5.1.5 и 5.1.6 из check.sh: решают по тексту ответа, код kubectl проверяется отдельно."""

    def run_check(self, name: str, out: str, fail: bool = False) -> int:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out")
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
            fake = 'kc() { cat "$KC_OUT"; [ -z "${KC_FAIL:-}" ]; }\n'
            script = "set -uo pipefail\n" + GUARD + fake + function(CHECK, name) + f"{name}\n"
            env = {**os.environ, "KC_OUT": path}
            if fail:
                env["KC_FAIL"] = "1"
            return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30, env=env).returncode

    def test_default_sa_without_token(self):
        good = "default false\nfalco false\nkube-node-lease false\nkube-public false\nkube-system \n" \
               "qdrant false\nsearch false\nstand-panel false\n"
        self.assertEqual(self.run_check("default_sa_without_token", good), 0)
        self.assertEqual(self.run_check("default_sa_without_token", good.replace("search false", "search ")), 1)
        self.assertEqual(self.run_check("default_sa_without_token", good.replace("falco false", "falco true")), 1)
        self.assertEqual(self.run_check("default_sa_without_token", ""), 1, "пустой ответ не успех")
        self.assertEqual(self.run_check("default_sa_without_token", "kube-system \n"), 1)
        self.assertEqual(self.run_check("default_sa_without_token", good, fail=True), 1, "kubectl упал")

    def test_pods_without_api_token(self):
        good = ("kube-system coredns-1 config-volume kube-api-access-x7k2p\n"
                "qdrant qdrant-0 qdrant-storage qdrant-config qdrant-init qdrant-secret\n"
                "qdrant qdrant-gate-abc12 code key\n"
                "falco falco-xyz12 root-falco-fs panel-output\n"
                "search search-7fbff8b77b-4n2qm qdrant-key tmp\n"
                "default no-volumes\n")
        self.assertEqual(self.run_check("pods_without_api_token", good), 0)
        bad = good.replace("qdrant-0 qdrant-storage", "qdrant-0 kube-api-access-9zq4r qdrant-storage")
        self.assertEqual(self.run_check("pods_without_api_token", bad), 1)
        self.assertEqual(self.run_check("pods_without_api_token", ""), 1, "пустой ответ не успех")
        self.assertEqual(self.run_check("pods_without_api_token", good, fail=True), 1, "kubectl упал")
        # длинный ответ с находкой в первой строке: результат не зависит от того, кто первым закрыл трубу
        long_bad = "falco falco-1 kube-api-access-abcde\n" + good * 5000
        self.assertEqual(self.run_check("pods_without_api_token", long_bad), 1)
        self.assertEqual(self.run_check("pods_without_api_token", good * 5000), 0)

    def test_checks_are_called(self):
        for name in ("k3s_certs_private", "default_sa_without_token", "pods_without_api_token"):
            self.assertRegex(CHECK, rf'(?m)^check "[^"]+" {name}$')


class ChartsWithoutTokenTest(unittest.TestCase):
    def test_qdrant_and_falco_use_default_account(self):
        for chart in ("qdrant", "falco"):
            self.assertRegex(read("deploy", chart, "values.yaml"), r"(?m)^serviceAccount:\n  create: false$", chart)
        self.assertRegex(read("deploy", "falco", "values.yaml"), r"(?m)^rbac:\n  create: false$")
        self.assertRegex(read("deploy", "falco", "values.yaml"), r"(?m)^  kubernetes:\n    enabled: false$",
                         "k8smeta выключен: Falco без API Kubernetes")

    def test_token_off_before_chart_install(self):
        for chart in ("qdrant", "falco"):
            text = read("deploy", chart, "install.sh")
            sa = text.index("automountServiceAccountToken: false")
            self.assertLess(text.index(f"kubectl create namespace {chart}"), sa, chart)
            self.assertLess(sa, text.index(f"helm upgrade --install {chart}"), chart)


class CertPermissionsTest(unittest.TestCase):
    """1.1.20: правило systemd из cis.sh и что делает его команда."""

    DROPIN = re.search(r"cat > /etc/systemd/system/k3s\.service\.d/40-cis-tls\.conf <<'EOF'\n(.*?)\nEOF\n",
                       read("deploy", "k3s", "cis.sh"), re.S).group(1)

    def exec_start_post(self) -> list[str]:
        lines = self.DROPIN.splitlines()
        self.assertIn("[Service]", lines)
        post = [x for x in lines if x.startswith("ExecStartPost=")]
        self.assertEqual(len(post), 1)
        value = post[0].split("=", 1)[1]
        self.assertTrue(value.startswith("-/"), "«-»: сбой команды не должен останавливать k3s")
        self.assertNotIn("%", value, "systemd подставил бы спецификаторы")
        self.assertNotIn("$", value, "systemd подставил бы переменные")
        return shlex.split(value[1:])  # кавычки systemd здесь разбираются так же, как в shell

    def test_command_makes_crt_private_and_leaves_keys(self):
        argv = self.exec_start_post()
        self.assertEqual(argv[:2], ["/usr/bin/find", "/var/lib/rancher/k3s/server/tls"])
        with tempfile.TemporaryDirectory() as d:
            files = {"server-ca.crt": 0o644, "client-ca.nochain.crt": 0o644, "server-ca.key": 0o600,
                     "dynamic-cert.json": 0o644, os.path.join("etcd", "peer-ca.crt"): 0o644}
            os.mkdir(os.path.join(d, "etcd"))
            for name, mode in files.items():
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("x")
                os.chmod(path, mode)
            r = subprocess.run(["find", d, *argv[2:]], capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = {name: stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode) for name in files}
        self.assertEqual(got, {"server-ca.crt": 0o600, "client-ca.nochain.crt": 0o600, "server-ca.key": 0o600,
                               "dynamic-cert.json": 0o644, os.path.join("etcd", "peer-ca.crt"): 0o600})

    def test_reload_and_immediate_fix_on_running_server(self):
        text = read("deploy", "k3s", "cis.sh")
        self.assertIn("systemctl daemon-reload", text)
        self.assertRegex(text, r"""(?m)^if \[ -d "\$S/tls" \]; then find "\$S/tls" -type f -name '\*\.crt' -exec chmod 0600 \{\} \+; fi$""")


if __name__ == "__main__":
    unittest.main()
