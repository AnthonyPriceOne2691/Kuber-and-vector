"""Проба DNS кластера (deploy/k3s/dns_probe.py) против подставного DNS на UDP."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import socket
import struct
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("dns_probe", os.path.join(ROOT, "deploy", "k3s", "dns_probe.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class FakeDNS:
    """Отвечает на каждый запрос заданным rcode и числом ответов; mute молчит; noise шлёт сперва чужой пакет."""

    def __init__(self, rcode: int = 0, answers: int = 1, mute: bool = False, noise: bool = False):
        self.rcode, self.answers, self.mute, self.noise = rcode, answers, mute, noise
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.names: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(512)
            except socket.timeout:
                continue
            txid = struct.unpack(">H", data[:2])[0]
            labels, i = [], 12
            while data[i]:
                labels.append(data[i + 1:i + 1 + data[i]].decode())
                i += 1 + data[i]
            self.names.append(".".join(labels))
            if self.mute:
                continue
            if self.noise:
                self.sock.sendto(struct.pack(">HHHHHH", txid ^ 1, 0x8180, 1, 1, 0, 0), addr)
            flags = 0x8180 | self.rcode
            self.sock.sendto(struct.pack(">HHHHHH", txid, flags, 1, self.answers, 0, 0) + data[12:], addr)

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
        self.sock.close()


class ProbeTest(unittest.TestCase):
    def dns(self, **kw) -> FakeDNS:
        d = FakeDNS(**kw)
        self.addCleanup(d.close)
        return d

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probe.main(list(args))
        return code, out.getvalue()

    def test_cluster_name_answers(self):
        d = self.dns()
        code, out = self.run_main("kubernetes.default.svc.cluster.local", "--server", "127.0.0.1", "--port", str(d.port))
        self.assertEqual(code, 0, out)
        self.assertEqual(d.names, ["kubernetes.default.svc.cluster.local"])
        self.assertIn("NOERROR, ответов 1", out)

    def test_refused_fast(self):
        d = self.dns(rcode=2, answers=0)
        self.assertEqual(self.run_main("example.com", "--refused", "--server", "127.0.0.1", "--port", str(d.port))[0], 0)
        # обычная проба на отказе падает
        self.assertEqual(self.run_main("example.com", "--server", "127.0.0.1", "--port", str(d.port))[0], 1)

    def test_silence_is_not_refusal(self):
        d = self.dns(mute=True)
        code, out = self.run_main("example.com", "--refused", "--server", "127.0.0.1", "--port", str(d.port),
                                  "--timeout", "0.2")
        self.assertEqual(code, 1, "таймаут значит, что запрос куда-то ушёл и висит, а не отказ")
        self.assertIn("нет ответа", out)

    def test_foreign_packet_ignored(self):
        d = self.dns(noise=True)
        rcode, answers, _ = probe.ask("a.cluster.local", "127.0.0.1", d.port, 1.0)
        self.assertEqual((rcode, answers), (0, 1))

    def test_bad_names(self):
        for name in ("", "a..b", "x" * 64 + ".local"):
            with self.assertRaises(ValueError):
                probe.query(name, 1)
        with self.assertRaises(ValueError):
            probe.parse(b"\0" * 5, 1)


if __name__ == "__main__":
    unittest.main()
