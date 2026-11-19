"""API Kubernetes без kubectl (KubeAPI): настоящий HTTPS, сертификат по CA кластера, токен из файла.

УЦ и сертификат сервера на 127.0.0.1 выпускает openssl во временной папке на время теста: ключей
в репозитории нет. Нет openssl — тесты HTTPS пропускаются.
"""
import base64
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aggregator
from sources import KubeAPI, KubeSource

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PATHS = {"/api/v1/nodes", "/api/v1/pods", "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases"}


def fake_token(sub: str) -> str:
    """Строка вида JWT без подписи: собирается здесь, в репозитории похожих на токен строк нет."""
    part = lambda doc: base64.urlsafe_b64encode(json.dumps(doc).encode()).decode().rstrip("=")  # noqa: E731
    return f"{part({'alg': 'none'})}.{part({'sub': sub})}.unsigned"


TOKEN = fake_token("test-panel")

CNF = """
[req]
distinguished_name = dn
prompt = no
[dn]
CN = unused
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[v3_srv]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def api_list(items: list[dict], kind: str) -> dict:
    """Список как отдаёт API: у элементов нет kind и apiVersion (их дописывает только kubectl)."""
    return {"kind": kind + "List", "apiVersion": "v1", "metadata": {"resourceVersion": "42"},
            "items": [{k: v for k, v in it.items() if k not in ("kind", "apiVersion")} for it in items]}


def make_pki(d: str) -> None:
    with open(os.path.join(d, "openssl.cnf"), "w", encoding="utf-8") as f:
        f.write(CNF)

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True, stdin=subprocess.DEVNULL)
    for ca in ("ca", "other-ca"):
        run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{ca}.key")
        run("req", "-x509", "-new", "-key", f"{ca}.key", "-sha256", "-days", "2", "-subj", f"/CN=test {ca}",
            "-config", "openssl.cnf", "-extensions", "v3_ca", "-out", f"{ca}.pem")
    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "srv.key")
    run("req", "-new", "-key", "srv.key", "-subj", "/CN=kube-apiserver", "-config", "openssl.cnf", "-out", "srv.csr")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
        "-sha256", "-extfile", "openssl.cnf", "-extensions", "v3_srv", "-out", "srv.pem")


class _Kube(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, doc: dict, extra: dict | None = None) -> None:
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _any(self) -> None:
        s = self.server
        s.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.command != "GET":
            return self._send(405, {"kind": "Status", "code": 405})
        if self.headers.get("Authorization") != f"Bearer {s.token}":
            return self._send(401, {"kind": "Status", "code": 401, "message": "Unauthorized"})
        if self.path in s.redirect:
            return self._send(302, {}, {"Location": s.redirect[self.path]})
        if self.path in s.forbid:
            return self._send(403, {"kind": "Status", "code": 403, "reason": "Forbidden"})
        if self.path not in s.answers:
            return self._send(404, {"kind": "Status", "code": 404})
        self._send(200, s.answers[self.path])

    do_GET = do_POST = do_PUT = do_DELETE = _any

    def log_message(self, *a) -> None:
        pass


@unittest.skipUnless(shutil.which("openssl"), "нет openssl: сертификаты для теста HTTPS не выпустить")
class KubeAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = cls.tmp.name
        make_pki(cls.d)
        kubectl_doc = fixture("kubectl_nodes_pods.json")
        nodes = [i for i in kubectl_doc["items"] if i["kind"] == "Node"]
        pods = [i for i in kubectl_doc["items"] if i["kind"] == "Pod"]
        cls.answers = {"/api/v1/nodes": api_list(nodes, "Node"), "/api/v1/pods": api_list(pods, "Pod"),
                       "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases":
                           api_list(fixture("kube_leases.json")["items"], "Lease")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Kube)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(self.d, "srv.pem"), os.path.join(self.d, "srv.key"))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.daemon_threads = True
        srv.seen, srv.token, srv.answers, srv.forbid, srv.redirect = [], TOKEN, self.answers, set(), {}
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.srv = srv
        self.port = srv.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"
        self.token_file = os.path.join(self.d, "kube-token")
        self.write_token(TOKEN + "\n")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def write_token(self, value: str) -> None:
        tmp = self.token_file + ".new"  # как install.sh: новый файл и переименование
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, self.token_file)

    def source(self, url=None, ca="ca.pem", leases=True) -> KubeSource:
        api = KubeAPI(url or self.url, os.path.join(self.d, ca), self.token_file, timeout=3)
        return KubeSource(api=api, leases=leases)

    def test_reads_three_lists_with_cluster_ca_and_token(self):
        k = self.source()
        k.poll(now=100.0)
        st = k.status(100.0)
        self.assertTrue(st["ok"], st)
        data = k.data()
        self.assertEqual(sorted(n["name"] for n in data["nodes"]), ["machine-1", "machine-2", "machine-3"])
        self.assertEqual(data["nodes"][1]["ready"], "Unknown")
        pods = {p["name"]: p for p in data["pods"]}
        self.assertEqual(pods["qdrant-2"]["node"], "machine-3")
        self.assertTrue(pods["coredns-6799fbcd5-x7k2p"]["system"])
        self.assertIn("machine-2", data["leases"])
        self.assertEqual(data["server"], self.url)
        # только чтение: три GET, токен только в заголовке, в данных и статусе его нет
        self.assertEqual({m for m, _, _ in self.srv.seen}, {"GET"})
        self.assertEqual({p for _, p, _ in self.srv.seen}, PATHS)
        self.assertEqual({a for _, _, a in self.srv.seen}, {f"Bearer {TOKEN}"})
        self.assertNotIn(TOKEN, json.dumps(data) + json.dumps(st))

    def test_other_ca_is_refused_before_the_token_leaves(self):
        k = self.source(ca="other-ca.pem")
        k.poll(now=1.0)
        st = k.status(1.0)
        self.assertFalse(st["ok"])
        self.assertIn("Kubernetes API", st["error"])
        self.assertIn("CERTIFICATE_VERIFY_FAILED", st["error"])
        self.assertEqual(self.srv.seen, [])  # запрос с токеном так и не ушёл
        self.assertIsNone(k.data())

    def test_name_must_match_certificate(self):
        k = self.source(url=f"https://localhost:{self.port}")  # в сертификате только IP 127.0.0.1
        k.poll(now=1.0)
        self.assertFalse(k.status(1.0)["ok"])
        self.assertEqual(self.srv.seen, [])

    def test_new_token_file_is_picked_up_and_rejection_is_reported_without_token(self):
        k = self.source(leases=False)
        k.poll(now=1.0)
        self.assertTrue(k.status(1.0)["ok"])
        self.srv.token = fake_token("test-panel-new")  # установка выпустила новый, старый отозван
        k.poll(now=3.0)
        st = k.status(3.0)
        self.assertFalse(st["ok"])
        self.assertIn("401", st["error"])
        self.assertIn("token rejected", st["error"])
        self.assertNotIn(TOKEN, st["error"])
        self.assertEqual(len(k.data()["nodes"]), 3)  # последние удачные данные остались
        self.write_token(self.srv.token)
        k.poll(now=5.0)
        self.assertTrue(k.status(5.0)["ok"])
        self.assertEqual(self.srv.seen[-1][2], f"Bearer {self.srv.token}")

    def test_leases_forbidden_is_not_fatal(self):
        self.srv.forbid.add("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases")
        k = self.source()
        k.poll(now=1.0)
        self.assertTrue(k.status(1.0)["ok"])
        self.assertIsNone(k.data()["leases"])

    def test_redirect_is_not_followed(self):
        self.srv.redirect["/api/v1/nodes"] = f"{self.url}/elsewhere"
        k = self.source(leases=False)
        k.poll(now=1.0)
        st = k.status(1.0)
        self.assertFalse(st["ok"])
        self.assertIn("HTTP 302", st["error"])
        self.assertNotIn("/elsewhere", [p for _, p, _ in self.srv.seen])

    def test_failover_to_next_server(self):
        dead = ThreadingHTTPServer(("127.0.0.1", 0), _Kube)  # порт был занят и освобождён: соединение отклоняется
        dead_url = f"https://127.0.0.1:{dead.server_address[1]}"
        dead.server_close()
        api = KubeAPI([dead_url, self.url], os.path.join(self.d, "ca.pem"), self.token_file, timeout=3)
        k = KubeSource(api=api, leases=False)
        k.poll(now=1.0)
        self.assertTrue(k.status(1.0)["ok"])
        self.assertEqual(k.data()["server"], self.url)

    def test_missing_token_file(self):
        os.unlink(self.token_file)
        k = self.source()
        k.poll(now=1.0)
        self.assertEqual(k.status(1.0)["error"], "Kubernetes API: cannot read token file")
        self.assertEqual(self.srv.seen, [])


class KubeAPIArgsTest(unittest.TestCase):
    def test_only_https_and_ca_and_token_are_required(self):
        with self.assertRaises(ValueError):
            KubeAPI("http://127.0.0.1:6443", "ca.pem", "token")
        with self.assertRaises(ValueError):
            KubeAPI("https://127.0.0.1:6443", "", "token")
        with self.assertRaises(ValueError):
            KubeAPI("https://127.0.0.1:6443", "ca.pem", "")
        with self.assertRaises(OSError):  # нет файла CA — ошибка при запуске, а не тихий откат
            KubeAPI("https://127.0.0.1:6443", "/nonexistent/ca.pem", "token")

    def test_aggregator_flags(self):
        def build(*argv):
            return aggregator.build(aggregator.parse_args(list(argv)))
        with self.assertRaises(ValueError):  # без CA и токена
            build("--kube-api", "https://127.0.0.1:6443")
        with self.assertRaises(ValueError):  # API напрямую и kubectl одновременно
            build("--kube-api", "https://127.0.0.1:6443", "--kube-ca", "ca.pem", "--kube-token-file", "t",
                  "--kubeconfig", "k.yaml")
        with self.assertRaises(ValueError):  # CA без --kube-api — опечатка, а не kubectl
            build("--kube-ca", "ca.pem")


if __name__ == "__main__":
    unittest.main()
