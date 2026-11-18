"""Разбор `kubectl get nodes,pods -A -o json` и аренд kubelet; источник kube: таймауты, кэш, запасные API."""
import json
import os
import subprocess
import unittest
from datetime import datetime, timezone

from sources import KubeSource, parse_kube, parse_leases, parse_time

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


class ParseTimeTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_time("2026-11-18T10:05:44Z"), ts("2026-11-18T10:05:44"))
        self.assertAlmostEqual(parse_time("2026-11-18T10:05:01.987654321Z"), ts("2026-11-18T10:05:01.987654"), places=5)
        self.assertEqual(parse_time("2026-11-18T13:05:44+03:00"), ts("2026-11-18T10:05:44"))
        self.assertEqual(parse_time(1700000000), 1700000000.0)
        self.assertIsNone(parse_time("yesterday"))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))


class ParseKubeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k = parse_kube(fixture("kubectl_nodes_pods.json"))
        cls.nodes = {n["name"]: n for n in cls.k["nodes"]}
        cls.pods = {p["name"]: p for p in cls.k["pods"]}

    def test_nodes(self):
        self.assertEqual(sorted(self.nodes), ["machine-1", "machine-2", "machine-3"])
        m1, m2, m3 = self.nodes["machine-1"], self.nodes["machine-2"], self.nodes["machine-3"]
        self.assertEqual(m1["ready"], "True")
        self.assertEqual(m1["ip"], "192.168.104.3")
        self.assertEqual(m1["roles"], ["control-plane", "etcd", "master"])
        self.assertEqual(m1["kubelet"], "v1.37.2+k3s1")
        self.assertEqual(m2["ready"], "Unknown")
        self.assertEqual(m2["reason"], "NodeStatusUnknown")
        self.assertEqual(m2["ready_since"], ts("2026-11-18T10:05:44"))
        self.assertIn("node.kubernetes.io/unreachable:NoExecute", m2["taints"])
        self.assertTrue(m3["unschedulable"])
        self.assertFalse(m1["unschedulable"])

    def test_pod_statuses_like_kubectl(self):
        p = self.pods
        self.assertEqual(p["search-7f9c6d8b5-dfzb8"]["status"], "Running")
        self.assertTrue(p["search-7f9c6d8b5-dfzb8"]["ready"])
        self.assertEqual(p["search-7f9c6d8b5-8szvd"]["status"], "Terminating")
        self.assertTrue(p["search-7f9c6d8b5-8szvd"]["deleting"])
        self.assertEqual(p["search-7f9c6d8b5-jd2w8"]["status"], "ContainerCreating")
        self.assertEqual(p["search-7f9c6d8b5-q4w7r"]["status"], "Pending")
        self.assertIsNone(p["search-7f9c6d8b5-q4w7r"]["node"])
        self.assertEqual(p["search-7f9c6d8b5-ev1ct"]["status"], "Evicted")
        self.assertEqual(p["warmup-6c9d4b7f8-k2m4n"]["status"], "Init")
        self.assertEqual(p["metrics-server-54fd9b65b-t8w2v"]["status"], "CrashLoopBackOff")
        self.assertEqual(p["helm-install-traefik-9xk2p"]["phase"], "Succeeded")
        self.assertEqual(p["helm-install-traefik-9xk2p"]["status"], "Succeeded")

    def test_pod_fields(self):
        q2 = self.pods["qdrant-2"]
        self.assertEqual(q2["restarts"], 3)  # сумма по контейнерам
        self.assertEqual((q2["app"], q2["owner_kind"], q2["ip"], q2["node"]), ("qdrant", "StatefulSet", "10.42.2.12", "machine-3"))
        self.assertEqual(self.pods["search-7f9c6d8b5-dfzb8"]["app"], "search")
        self.assertEqual(self.pods["coredns-6799fbcd5-x7k2p"]["app"], "kube-dns")
        self.assertEqual(self.pods["search-7f9c6d8b5-dfzb8"]["created"], ts("2026-11-18T09:30:00"))

    def test_system_namespaces(self):
        self.assertFalse(self.pods["qdrant-0"]["system"])
        self.assertFalse(self.pods["search-7f9c6d8b5-dfzb8"]["system"])
        self.assertTrue(self.pods["coredns-6799fbcd5-x7k2p"]["system"])
        only_search = {p["name"]: p for p in parse_kube(fixture("kubectl_nodes_pods.json"), ("search",))["pods"]}
        self.assertTrue(only_search["qdrant-0"]["system"])

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_kube({"kind": "Status", "message": "Unauthorized"})

    def test_leases(self):
        le = parse_leases(fixture("kube_leases.json"))
        self.assertEqual(sorted(le), ["machine-1", "machine-2", "machine-3"])  # аренда планировщика не в счёт
        self.assertAlmostEqual(le["machine-2"]["renew"], ts("2026-11-18T10:05:01.987654"), places=5)
        self.assertEqual(le["machine-1"]["duration"], 40)


class KubeSourceTest(unittest.TestCase):
    def runner_for(self, script):
        """script: список ответов по очереди — строка JSON или исключение."""
        calls = []

        def run(cmd, timeout):
            calls.append(cmd)
            step = script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return run, calls

    def test_command_line(self):
        k = KubeSource(kubectl="/usr/local/bin/kubectl", kubeconfig="/etc/stand/kube.yaml", context="stand",
                       timeout=4.0, runner=lambda c, t: "")
        cmd = k.command("https://lima-machine-1.internal:6443", ["get", "nodes,pods", "-A", "-o", "json"])
        self.assertEqual(cmd[:2], ["/usr/local/bin/kubectl", "--request-timeout=3s"])
        self.assertIn("--kubeconfig", cmd)
        self.assertEqual(cmd[cmd.index("--server") + 1], "https://lima-machine-1.internal:6443")
        self.assertEqual(cmd[-5:], ["get", "nodes,pods", "-A", "-o", "json"])

    def test_cache_survives_hanging_api(self):
        good = json.dumps(fixture("kubectl_nodes_pods.json"))
        leases = json.dumps(fixture("kube_leases.json"))
        run, calls = self.runner_for([good, leases, subprocess.TimeoutExpired(["kubectl"], 4)])
        k = KubeSource(runner=run)
        k.poll(now=1000.0)
        self.assertTrue(k.status(1000.5)["ok"])
        self.assertEqual(len(k.data()["nodes"]), 3)
        self.assertIn("machine-2", k.data()["leases"])
        k.poll(now=1010.0)  # API повис: kubectl убит по таймауту
        st = k.status(1012.0)
        self.assertFalse(st["ok"])
        self.assertIn("not responding", st["error"])
        self.assertEqual(st["age_s"], 12.0)
        self.assertEqual(len(k.data()["nodes"]), 3)  # последние удачные данные остались

    def test_failover_to_next_api_server_and_stick(self):
        good = json.dumps(fixture("kubectl_nodes_pods.json"))
        run, calls = self.runner_for([RuntimeError("dial tcp: connect: no route to host"), good, good, good])
        k = KubeSource(servers=["https://a:6443", "https://b:6443"], leases=False, runner=run)
        k.poll(now=1.0)
        self.assertTrue(k.status(1.0)["ok"])
        self.assertEqual(k.data()["server"], "https://b:6443")
        k.poll(now=3.0)
        servers = [c[c.index("--server") + 1] for c in calls]
        self.assertEqual(servers, ["https://a:6443", "https://b:6443", "https://b:6443"])

    def test_leases_forbidden_is_not_fatal(self):
        good = json.dumps(fixture("kubectl_nodes_pods.json"))
        run, _ = self.runner_for([good, RuntimeError("leases is forbidden")])
        k = KubeSource(runner=run)
        k.poll(now=5.0)
        self.assertTrue(k.status(5.0)["ok"])
        self.assertIsNone(k.data()["leases"])

    def test_missing_kubectl(self):
        run, _ = self.runner_for([FileNotFoundError("kubectl")])
        k = KubeSource(runner=run)
        k.poll(now=1.0)
        self.assertEqual(k.status(1.0)["error"], "kubectl not found")
        self.assertIsNone(k.data())


if __name__ == "__main__":
    unittest.main()
