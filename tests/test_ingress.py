"""Фильтр входящих на сети машин (deploy/vm/ingress.py): кто куда стучится, проверка адресов.
Сами правила nftables проверяет nft -c на машине (deploy/vm/ingress.sh) и deploy/check.sh на стенде."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ingress", os.path.join(ROOT, "deploy", "vm", "ingress.py"))
ingress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingress)

CLUSTER = ["192.168.105.5", "192.168.105.3", "192.168.105.4"]
ENTRY = "192.168.105.2"


def rules(text: str) -> list[str]:
    """Строки правил цепочки без комментариев."""
    chain = text.split("chain prerouting {", 1)[1]
    return [s.strip() for s in chain.splitlines() if s.strip() and not s.strip().startswith(("#", "type ", "}"))]


def accepted_tcp(text: str, source: str = "") -> set[int]:
    """Порты TCP, открытые источнику source («» значит любому)."""
    prefix = f"ip saddr {source} " if source else ""
    ports: set[int] = set()
    for line in rules(text):
        m = re.fullmatch(re.escape(prefix) + r"tcp dport \{ ([0-9, ]+) \} accept", line)
        if m:
            ports |= {int(p) for p in m.group(1).split(",")}
    return ports


class RenderTest(unittest.TestCase):
    def test_cluster_ports_by_source(self):
        text = ingress.render("cluster", CLUSTER, ENTRY)
        self.assertEqual(accepted_tcp(text, "@cluster"), {2379, 2380, 6443, 10250})
        self.assertEqual(accepted_tcp(text, "@entry"), {6443, 9100, 30080, 30633, 30765})
        self.assertIn("ip saddr @cluster udp dport { 51820 } accept", rules(text))
        self.assertEqual(accepted_tcp(text, ""), set(), "машине кластера никто посторонний не пишет")
        self.assertIn("set cluster { type ipv4_addr; elements = { 192.168.105.5, 192.168.105.3, 192.168.105.4 }; }", text)
        self.assertIn("set entry { type ipv4_addr; elements = { 192.168.105.2 }; }", text)

    def test_observer_only_443_for_everyone(self):
        text = ingress.render("observer", CLUSTER, ENTRY)
        self.assertEqual(accepted_tcp(text, ""), {443})
        self.assertEqual(accepted_tcp(text, "@cluster"), {8090})
        self.assertEqual(accepted_tcp(text, "@entry"), set())
        self.assertNotIn("udp dport { 51820 }", text)

    def test_order_and_shape(self):
        for role in ingress.ROLES:
            r = rules(ingress.render(role, CLUSTER, ENTRY))
            self.assertEqual(r[0], 'iifname != "lima0" accept', "фильтруем только сеть машин, eth0 Lima не трогаем")
            self.assertEqual(r[1], "ct state established,related accept")
            self.assertEqual(r[-1], 'counter name "dropped" drop', "последним всё остальное в счётчик и в отбой")
            self.assertEqual(sum("drop" in s for s in r), 1)
            self.assertNotIn("eth0", ingress.render(role, CLUSTER, ENTRY))

    def test_atomic_replace(self):
        text = ingress.render("cluster", CLUSTER, ENTRY)
        head = [s for s in text.splitlines() if s and not s.startswith("#")][:3]
        self.assertEqual(head, ["table inet stand_ingress", "delete table inet stand_ingress", "table inet stand_ingress {"])
        self.assertIn("type filter hook prerouting priority -150; policy accept;", text)

    def test_nodeports_match_manifests(self):
        """NodePort в манифестах и порты входа в фильтре должны совпадать, иначе мониторинг или вход отвалятся."""
        found = set()
        for path in ("deploy/search/k8s.yaml", "deploy/qdrant/gate.yaml", "deploy/monitoring/k8s.yaml"):
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                found |= {int(p) for p in re.findall(r"nodePort: (\d+)", f.read())}
        self.assertEqual(found, set(ingress.FROM_ENTRY_TCP) - {6443, 9100})


class AddressTest(unittest.TestCase):
    def test_rejects_outside_lan_host_and_garbage(self):
        bad = ["10.0.0.5", "192.168.105.1", "192.168.105.0", "192.168.105.255", "192.168.105.5; flush ruleset",
               "192.168.105.5 }", "", "::1", "machine-1"]
        for a in bad:
            with self.assertRaises(ValueError, msg=a):
                ingress.render("cluster", [a, "192.168.105.3"], ENTRY)
            with self.assertRaises(ValueError, msg=a):
                ingress.render("observer", CLUSTER, a)

    def test_rejects_duplicates_and_entry_in_cluster(self):
        with self.assertRaises(ValueError):
            ingress.render("cluster", ["192.168.105.5", "192.168.105.5"], ENTRY)
        with self.assertRaises(ValueError):
            ingress.render("cluster", CLUSTER, CLUSTER[0])
        with self.assertRaises(ValueError):
            ingress.render("cluster", [], ENTRY)
        with self.assertRaises(ValueError):
            ingress.render("builder", CLUSTER, ENTRY)

    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ingress.main(["--role", "cluster", "--cluster", ",".join(CLUSTER), "--entry", ENTRY]), 0)
            self.assertEqual(ingress.main(["--role", "cluster", "--cluster", "192.168.105.1", "--entry", ENTRY]), 2)
        self.assertIn("table inet stand_ingress {", out.getvalue())
        self.assertIn("это Мак", err.getvalue())


if __name__ == "__main__":
    unittest.main()
