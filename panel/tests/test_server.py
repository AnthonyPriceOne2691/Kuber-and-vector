"""HTTP агрегатора: статика с CSP, /api/state по контракту из README, POST /api/event, защита путей,
отдельный адрес только для событий."""
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import aggregator

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Контракт /api/state (panel/README.md): ключи объектов. Меняется контракт — меняются README и этот тест.
TOP = {"schema", "ts", "mode", "ui", "sources", "load", "machines", "kube", "qdrant", "incident", "events"}
MACHINE = {"name", "color", "role", "status", "status_since", "heartbeat_at", "heartbeat_age_s", "ip", "kube",
           "pods", "system_pods", "qdrant", "services"}
POD = {"namespace", "name", "short", "app", "status", "state", "ready", "restarts", "owner_kind", "age_s"}
LOAD = {"schema", "ts", "started_at", "windows", "totals", "series", "serving", "server_timing_ms", "source",
        "running", "inflight", "config", "age_s", "stale"}
WINDOW = {"seconds", "count", "qps", "p50_ms", "p95_ms", "p99_ms", "max_ms", "without_retry", "with_retry",
          "retries", "skipped"}
POINT = {"t", "qps", "p50", "p95", "p99", "errors_without_retry", "errors_with_retry", "retries"}
QDRANT = {"ok", "collection", "alias", "status", "points", "shard_count", "replication_factor", "peers", "shards",
          "transfers", "summary", "raft"}
PEER = {"id", "machine", "pod", "uri", "reachable", "leader"}
REPLICA = {"peer", "machine", "state", "display", "progress", "from_machine"}
INCIDENT = {"active", "machine", "color", "phase", "started", "elapsed_s", "downtime_s", "recovery_s", "total_s",
            "steps", "apps", "load"}
STEP = {"key", "label", "ts", "done", "offset_s"}
EVENT = {"id", "ts", "kind", "level", "title", "detail", "machine", "pod", "source", "count"}
KUBE = {"ok", "age_s", "error", "server", "heartbeats", "nodes", "unscheduled"}


class _Server:
    def __init__(self, panel, token=None, srv=None, **kw):
        self.srv = srv or ThreadingHTTPServer(("127.0.0.1", 0), aggregator.make_handler(panel, STATIC, token, **kw))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def req(self, path, data=None, headers=None, method=None):
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = aggregator.build(aggregator.parse_args(["--mock", "--mock-start", "84"]))
        cls.panel.tick()
        cls.s = _Server(cls.panel)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_index_with_hash_based_csp(self):
        code, h, body = self.s.req("/")
        self.assertEqual(code, 200)
        self.assertTrue(h["Content-Type"].startswith("text/html"))
        self.assertIn(b"<canvas", body)
        csp = h["Content-Security-Policy"]
        script = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Server"], "stand-panel/1")  # без версии Python
        self.assertNotIn(b"https://", body.split(b"<script>")[0])  # без внешних шрифтов и CDN

    def test_head_and_health(self):
        code, h, body = self.s.req("/", method="HEAD")
        self.assertEqual((code, body), (200, b""))
        self.assertEqual(json.loads(self.s.req("/healthz")[2]), {"ok": True})

    def test_paths_outside_static_are_not_served(self):
        for path in ("/../aggregator.py", "/%2e%2e/aggregator.py", "/static/../../panel/aggregator.py",
                     "/..%2f..%2fREADME.md", "/nope.html", "/questions.txt"):
            self.assertEqual(self.s.req(path)[0], 404, path)

    def test_state_contract(self):
        code, h, body = self.s.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(h["Cache-Control"], "no-store")
        st = json.loads(body)
        self.assertEqual(set(st), TOP)
        self.assertEqual((st["schema"], st["mode"]), ("panel/1", "mock"))
        self.assertEqual(set(st["ui"]), {"title", "subtitle", "caption"})
        self.assertEqual(set(st["sources"]), {"load", "kube", "qdrant", "observer"})
        for m in st["machines"]:
            self.assertEqual(set(m), MACHINE)
            for p in m["pods"]:
                self.assertEqual(set(p), POD)
        self.assertEqual([m["name"] for m in st["machines"]], ["machine-1", "machine-2", "machine-3", "machine-4"])
        self.assertEqual(st["machines"][3]["role"], "observer")
        self.assertTrue(st["machines"][3]["services"])
        self.assertEqual(set(st["load"]), LOAD)
        for w in st["load"]["windows"].values():
            self.assertEqual(set(w), WINDOW)
            self.assertEqual(set(w["without_retry"]), {"errors", "rate", "by_kind"})
        self.assertEqual(set(st["load"]["series"][-1]), POINT)
        self.assertEqual(set(st["kube"]), KUBE)
        self.assertEqual(set(st["qdrant"]), QDRANT)
        for p in st["qdrant"]["peers"]:
            self.assertEqual(set(p), PEER)
            self.assertIsInstance(p["id"], str)
        for sh in st["qdrant"]["shards"]:
            for r in sh["replicas"]:
                self.assertEqual(set(r), REPLICA)
        self.assertEqual(set(st["incident"]), INCIDENT)
        for s in st["incident"]["steps"]:
            self.assertEqual(set(s), STEP)
        self.assertEqual(set(st["incident"]["load"]), {"requests", "without_retry", "with_retry", "p99_max"})
        for e in st["events"]:
            self.assertTrue(EVENT <= set(e) <= EVENT | {"last_ts"})

    def test_post_event_from_localhost(self):
        body = json.dumps({"title": "Pod quarantined", "level": "ok", "machine": "machine-1"}).encode()
        code, _, resp = self.s.req("/api/event", data=body, headers={"Content-Type": "application/json"})
        self.assertEqual(code, 201)
        ev_id = json.loads(resp)["ids"][0]
        st = self.panel.tick()
        self.assertTrue(any(e["id"] == ev_id and e["kind"] == "security" for e in st["events"]))

    def test_post_errors(self):
        self.assertEqual(self.s.req("/api/event", data=b"{not json")[0], 400)
        self.assertEqual(self.s.req("/api/event", data=b'{"detail": "no title"}')[0], 400)
        self.assertEqual(self.s.req("/api/event", data=b"x" * (65 * 1024))[0], 413)
        self.assertEqual(self.s.req("/api/other", data=b"{}")[0], 404)

    def test_falco_batch(self):
        alerts = [{"rule": "Terminal shell in container", "priority": "Notice", "output": "shell", "hostname": "machine-1"},
                  {"rule": "Contact K8S API Server From Container", "priority": "Warning", "output": "curl"}]
        code, _, resp = self.s.req("/api/event", data=json.dumps(alerts).encode())
        self.assertEqual(code, 201)
        self.assertEqual(len(json.loads(resp)["ids"]), 2)


class TokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = aggregator.build(aggregator.parse_args(["--mock"]))
        cls.panel.tick()
        cls.s = _Server(cls.panel, token="t0k3n-for-tests")

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_token_required_when_configured(self):
        body = b'{"title": "x"}'
        self.assertEqual(self.s.req("/api/event", data=body)[0], 403)
        self.assertEqual(self.s.req("/api/event", data=body, headers={"X-Panel-Token": "wrong"})[0], 403)
        self.assertEqual(self.s.req("/api/event", data=body, headers={"X-Panel-Token": "t0k3n-for-tests"})[0], 201)
        self.assertEqual(self.s.req("/api/event", data=body,
                                    headers={"Authorization": "Bearer t0k3n-for-tests"})[0], 201)
        self.assertEqual(self.s.req("/api/event?token=t0k3n-for-tests", data=body)[0], 201)  # для Falco http_output

    def test_csp_helper(self):
        csp = aggregator.csp_for(b"<style>a{}</style><script>1</script>")
        self.assertIn("style-src 'sha256-", csp)
        self.assertIn("script-src 'sha256-", csp)
        self.assertIn("script-src 'none'", aggregator.csp_for(b"<p>no scripts</p>"))

    def test_token_is_masked_in_request_log(self):
        s = _Server(self.panel, token="t0k3n-for-tests", verbose=True)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code = s.req("/api/event?token=t0k3n-for-tests&x=1", data=b'{"title": "x"}')[0]
        finally:
            s.close()
        self.assertEqual(code, 201)
        self.assertIn("POST /api/event?token=…&x=1", err.getvalue())
        self.assertNotIn("t0k3n-for-tests", err.getvalue())


class EventsAddressTest(unittest.TestCase):
    """Два адреса (--bind и --event-bind): страница только на первом, на втором только события с токеном."""

    @classmethod
    def setUpClass(cls):
        args = aggregator.parse_args(["--mock", "--port", "0", "--event-bind", "127.0.0.1", "--event-port", "0"])
        cls.panel = aggregator.build(args)
        cls.panel.tick()
        ui, events = aggregator.listeners(args, cls.panel, "t0k3n-for-tests")
        cls.ui, cls.ev = _Server(cls.panel, srv=ui), _Server(cls.panel, srv=events)

    @classmethod
    def tearDownClass(cls):
        cls.ui.close()
        cls.ev.close()

    def test_page_and_state_only_on_main_address(self):
        self.assertEqual(self.ui.req("/api/state")[0], 200)
        self.assertEqual(self.ui.req("/")[0], 200)
        for path in ("/", "/api/state", "/healthz", "/index.html"):
            self.assertEqual(self.ev.req(path)[0], 404, path)
            self.assertEqual(self.ev.req(path, method="HEAD")[0], 404, path)

    def test_events_address_needs_token_even_for_localhost(self):
        body = b'{"title": "Pod quarantined", "kind": "security"}'
        self.assertEqual(self.ev.req("/api/event", data=body)[0], 403)
        self.assertEqual(self.ev.req("/api/event?token=wrong", data=body)[0], 403)
        code, _, resp = self.ev.req("/api/event?token=t0k3n-for-tests", data=body)
        self.assertEqual(code, 201)
        ev_id = json.loads(resp)["ids"][0]
        self.assertTrue(any(e["id"] == ev_id for e in self.panel.tick()["events"]))  # в той же ленте
        self.assertEqual(self.ev.req("/api/other?token=t0k3n-for-tests", data=body)[0], 404)

    def test_refusal_keeps_keepalive_connection_in_sync(self):
        # тело отказанного запроса дочитывается: то же соединение дальше годится, остаток тела
        # не разбирается как следующий запрос (было бы 400), клиент не видит сброса вместо ответа
        c = http.client.HTTPConnection("127.0.0.1", self.ev.srv.server_address[1], timeout=5)
        try:
            socks = []
            for path, want in (("/api/event", 403), ("/api/other", 404), ("/api/event?token=t0k3n-for-tests", 201)):
                c.request("POST", path, body=b'{"title": "keep-alive"}', headers={"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, want, path)
                socks.append(c.sock)
            self.assertIsNotNone(socks[0])
            self.assertEqual(len(set(map(id, socks))), 1)  # одно соединение на все три запроса
        finally:
            c.close()

    def test_refusal_with_big_body_closes_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.ev.srv.server_address[1], timeout=5)
        try:
            c.request("POST", "/api/event", body=b"x" * (aggregator.MAX_EVENT_BODY + 1))
            r = c.getresponse()
            r.read()
            self.assertEqual((r.status, r.getheader("Connection")), (403, "close"))
        except ConnectionError:
            pass  # большое тело не читаем: сброс соединения тоже честный отказ
        finally:
            c.close()

    def test_event_address_requires_token_file(self):
        args = aggregator.parse_args(["--port", "0", "--event-bind", "127.0.0.1", "--event-port", "0"])
        with self.assertRaises(ValueError):
            aggregator.listeners(args, self.panel, None)

    def test_busy_port_closes_what_was_opened(self):
        busy = self.ui.srv.server_address[1]
        args = aggregator.parse_args(["--port", "0", "--event-bind", "127.0.0.1", "--event-port", str(busy)])
        with self.assertRaises(OSError) as cm:
            aggregator.listeners(args, self.panel, "t0k3n-for-tests")
        self.assertIn(f"127.0.0.1:{busy}", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
