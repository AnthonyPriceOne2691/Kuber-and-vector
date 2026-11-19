"""Генератор нагрузки: политика повтора, виды ошибок, HTTP-клиент на asyncio, вопросы, запись снимка."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loadgen import (Endpoint, HTTPFormatError, LoadGen, Stats, classify, load_questions, read_response,
                     write_atomic)

OK = json.dumps({"query": "q", "model": "bge-m3", "collection": "lbr", "timing_ms": {"embed": 17.0, "search": 1.2},
                 "results": [{"score": 0.5, "document": "d", "section": None, "file": "f", "span": [0, 5],
                              "text": "t"}]}).encode()


def fake_fetch(plan):
    """plan[i] — что делает цель i: "timeout", "refused", (status, body) или список по очереди."""
    calls = []

    async def fetch(i, query):
        calls.append((i, query))
        step = plan[i]
        if isinstance(step, list):
            step = step.pop(0)
        if step == "timeout":
            await asyncio.sleep(5)
        if step == "refused":
            raise ConnectionRefusedError("refused")
        return step
    return fetch, calls


def gen(plan, targets=("a", "b"), retry="other", timeout=0.05):
    fetch, calls = fake_fetch(plan)
    stats = Stats()
    g = LoadGen(list(targets), ["What is a contract?"], stats, rate=10, concurrency=4, timeout=timeout,
                retry=retry, fetch=fetch)
    return g, stats, calls


class RetryPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_then_retry_to_other_target_succeeds(self):
        g, stats, calls = gen({0: "timeout", 1: (200, OK)})
        self.assertEqual(await g.one(), ("timeout", None))
        self.assertEqual([c[0] for c in calls], [0, 1])
        t = stats.totals()
        self.assertEqual(t["without_retry"]["by_kind"]["timeout"], 1)
        self.assertEqual(t["with_retry"]["errors"], 0)
        self.assertEqual(t["retries"], 1)
        lat = stats._b[next(iter(stats._b))].lat[0]
        self.assertGreaterEqual(lat, 50)  # задержка пользователя включает таймаут первой попытки

    async def test_no_retry_counts_error_for_user(self):
        g, stats, calls = gen({0: "timeout", 1: (200, OK)}, retry="none")
        self.assertEqual(await g.one(), ("timeout", "timeout"))
        self.assertEqual(len(calls), 1)
        t = stats.totals()
        self.assertEqual(t["without_retry"]["errors"], 1)
        self.assertEqual(t["with_retry"]["errors"], 1)
        self.assertEqual(t["retries"], 0)

    async def test_5xx_is_retried_4xx_is_not(self):
        g, _, calls = gen({0: (503, b"{}"), 1: (200, OK)})
        self.assertEqual(await g.one(), ("5xx", None))
        self.assertEqual(len(calls), 2)
        g, _, calls = gen({0: (429, b"{}"), 1: (200, OK)})
        self.assertEqual(await g.one(), ("4xx", "4xx"))
        self.assertEqual(len(calls), 1)

    async def test_connection_refused_and_bad_body(self):
        g, _, _ = gen({0: "refused", 1: (200, b"not json")})
        self.assertEqual(await g.one(), ("conn", "bad"))

    async def test_both_targets_down_is_a_user_error(self):
        g, stats, _ = gen({0: "timeout", 1: "refused"})
        self.assertEqual(await g.one(), ("timeout", "conn"))
        self.assertEqual(stats.totals()["with_retry"]["by_kind"]["conn"], 1)

    async def test_single_target_retries_itself(self):
        g, _, calls = gen({0: ["timeout", (200, OK)]}, targets=("only",))
        self.assertEqual(await g.one(), ("timeout", None))
        self.assertEqual([c[0] for c in calls], [0, 0])

    async def test_round_robin_and_question_rotation(self):
        fetch, calls = fake_fetch({0: (200, OK), 1: (200, OK)})
        g = LoadGen(["a", "b"], ["first question", "second question"], Stats(), fetch=fetch)
        for _ in range(4):
            await g.one()
        self.assertEqual([c[0] for c in calls], [0, 1, 0, 1])
        qs = [urllib.parse.parse_qs(c[1])["q"][0] for c in calls]
        self.assertEqual(qs, ["first question", "second question", "first question", "second question"])
        self.assertEqual(urllib.parse.parse_qs(calls[0][1])["k"], ["5"])

    async def test_serving_and_timing_are_recorded(self):
        g, stats, _ = gen({0: (200, OK), 1: (200, OK)})
        await g.one()
        now = max(stats._b) + 1.0
        self.assertEqual(stats.serving(10, now)[0]["model"], "bge-m3")
        self.assertEqual(stats.server_timing(10, now), {"embed": 17.0, "search": 1.2})

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            LoadGen(["a"], ["q?"], Stats(), retry="twice", fetch=lambda i, q: None)
        with self.assertRaises(ValueError):
            LoadGen([], ["q?"], Stats(), fetch=lambda i, q: None)


class ClassifyTest(unittest.TestCase):
    def test_classify(self):
        kind, meta = classify(200, OK)
        self.assertIsNone(kind)
        self.assertEqual((meta["model"], meta["collection"], meta["embed"]), ("bge-m3", "lbr", 17.0))
        self.assertEqual(classify(200, b"{oops")[0], "bad")
        self.assertEqual(classify(200, b'{"results": 5}')[0], "bad")
        self.assertEqual(classify(500, b"")[0], "5xx")
        self.assertEqual(classify(503, b"{}")[0], "5xx")
        self.assertEqual(classify(404, b"{}")[0], "4xx")
        self.assertEqual(classify(302, b"")[0], "4xx")
        # поле "mode" вместо "model" тоже понимаем
        self.assertEqual(classify(200, b'{"mode": "e5", "results": []}')[1]["model"], "e5")


async def _parse(raw: bytes):
    r = asyncio.StreamReader()
    r.feed_data(raw)
    r.feed_eof()
    return await read_response(r)


class HTTPParsingTest(unittest.TestCase):
    def test_content_length(self):
        st, h, body = asyncio.run(_parse(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhiEXTRA"))
        self.assertEqual((st, body), (200, b"hi"))

    def test_chunked_with_trailer(self):
        raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"4\r\nWiki\r\n6;ext=1\r\npedia!\r\n0\r\nX-Trailer: 1\r\n\r\n")
        self.assertEqual(asyncio.run(_parse(raw))[2], b"Wikipedia!")

    def test_body_until_close(self):
        st, h, body = asyncio.run(_parse(b"HTTP/1.0 200 OK\r\nServer: x\r\n\r\nall of it"))
        self.assertEqual(body, b"all of it")
        self.assertEqual(h["connection"], "close")

    def test_no_body_statuses_and_errors(self):
        self.assertEqual(asyncio.run(_parse(b"HTTP/1.1 204 No Content\r\n\r\n"))[2], b"")
        with self.assertRaises(HTTPFormatError):
            asyncio.run(_parse(b"SSH-2.0-OpenSSH\r\n\r\n"))
        with self.assertRaises(ConnectionError):
            asyncio.run(_parse(b""))

    def test_endpoint_url(self):
        e = Endpoint("https://user:secret@search.stand:8443/search?x=1")
        self.assertEqual((e.host, e.port, e.path, e.base_query), ("search.stand", 8443, "/search", "x=1"))
        self.assertNotIn("secret", e.url)  # логин и пароль не уходят на панель
        self.assertEqual(Endpoint("http://[::1]/s").host_header, "[::1]")
        self.assertEqual(Endpoint("http://h/s").port, 80)
        with self.assertRaises(ValueError):
            Endpoint("ftp://h/s")

    def test_endpoint_api_key(self):
        req = Endpoint("https://entry/search", api_key="k123")._request("q=a").decode("latin-1")
        self.assertIn("\r\nX-API-Key: k123\r\n", req)
        self.assertTrue(req.endswith("\r\n\r\n"))
        self.assertNotIn("X-API-Key", Endpoint("https://entry/search")._request("q=a").decode("latin-1"))
        for bad in ("", "k\r\nX-Evil: 1", "k\n"):  # заголовок не подделать переводом строки
            with self.assertRaises(ValueError):
                Endpoint("https://entry/search", api_key=bad)


class _Search(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        with self.server.lock:
            self.server.queries.append(urllib.parse.parse_qs(u.query))
        code, body = (200, OK) if u.path == "/search" else (500, b'{"error": "boom"}')
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Search)
        self.srv.daemon_threads = True
        self.srv.lock = threading.Lock()
        self.srv.connections = 0
        self.srv.queries = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_real_http_with_failing_target_and_retry(self):
        stats = Stats()
        g = LoadGen([f"{self.base}/search", f"{self.base}/fail"], ["what is a contract?"], stats,
                    rate=60, concurrency=8, timeout=1.0, retry="other")
        asyncio.run(g.run(asyncio.Event(), duration=0.6))
        t = stats.totals()
        self.assertGreaterEqual(t["count"], 20)
        # половина первых попыток идёт на /fail (500) и повторяется на /search
        self.assertAlmostEqual(t["without_retry"]["by_kind"]["5xx"], t["count"] / 2, delta=1)
        self.assertEqual(t["with_retry"]["errors"], 0)
        self.assertEqual(self.srv.queries[0]["q"], ["what is a contract?"])
        self.assertLess(self.srv.connections, t["count"])  # keep-alive: соединения переиспользуются


class StaleKeepAliveTest(unittest.TestCase):
    def test_idle_connection_closed_by_server_is_resent_once(self):
        stats = {"requests": 0, "connections": 0}

        async def main():
            async def handle(reader, writer):
                stats["connections"] += 1
                served = 0
                try:
                    while True:
                        await reader.readuntil(b"\r\n\r\n")
                        stats["requests"] += 1
                        served += 1
                        if served == 2:  # второй запрос в том же соединении: сервер «закрыл по простою»
                            break
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        await writer.drain()
                except (asyncio.IncompleteReadError, ConnectionError):
                    pass  # клиент закрыл соединение
                writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            ep = Endpoint(f"http://127.0.0.1:{port}/search")
            try:
                first = await ep.get("q=a")
                second = await ep.get("q=b")
            finally:
                ep.close()
                server.close()
            return first, second

        first, second = asyncio.run(main())
        self.assertEqual(first, (200, b"ok"))
        self.assertEqual(second, (200, b"ok"))
        self.assertEqual(stats["connections"], 2)
        self.assertEqual(stats["requests"], 3)


class FilesTest(unittest.TestCase):
    def test_load_questions(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "q.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# comment\n\nDoes the app share data?\nab\n  Who owns the IP?  \n" + "x" * 600 + "\n")
            q = load_questions(p)
            self.assertEqual(q[:2], ["Does the app share data?", "Who owns the IP?"])
            self.assertEqual(len(q), 3)
            self.assertEqual(len(q[2]), 500)
            with open(p, "w", encoding="utf-8") as f:
                f.write("# only comments\n")
            with self.assertRaises(ValueError):
                load_questions(p)

    def test_repo_questions_file_is_valid(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertGreaterEqual(len(load_questions(os.path.join(here, "questions.txt"))), 20)

    def test_write_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "load.json")
            write_atomic(p, {"a": 1})
            write_atomic(p, {"a": 2})
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 2})
            self.assertEqual(os.listdir(d), ["load.json"])  # временных файлов не осталось


if __name__ == "__main__":
    unittest.main()
