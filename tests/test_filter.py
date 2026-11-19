"""Фильтр перед Ollama на Маке (deploy/host/ollama_filter.py): что пропускает и что режет.

Фильтр поднимается на 127.0.0.1 со случайным портом, «Ollama» за ним подставная.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.fakes import FakeServer, closed_port_url, ollama_handler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ollama_filter", os.path.join(ROOT, "deploy", "host", "ollama_filter.py"))
flt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flt)


class FilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeServer(ollama_handler())
        self.addCleanup(self.upstream.close)
        self.patch(UPSTREAM=self.upstream.url)
        self.srv = flt.Server(("127.0.0.1", 0), flt.Handler)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.log = io.StringIO()
        redirect = contextlib.redirect_stderr(self.log)  # журнал фильтра пишется в stderr
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def patch(self, **values) -> None:
        p = mock.patch.multiple(flt, **values)
        p.start()
        self.addCleanup(p.stop)

    def send(self, path: str, body=None, raw: bytes | None = None, method: str = "POST") -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.getvalue().splitlines() if line.startswith("{")]


class EmbedTest(FilterTest):
    def test_embed_passes_only_allowed_fields(self):
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": "hello",
                                              "keep_alive": -1, "options": {"num_ctx": 131072}})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["embeddings"]), 1)
        # наверх ушли только модель, текст и truncate: keep_alive и options фильтр выбросил
        self.assertEqual(self.upstream.json_bodies(), [{"model": "bge-m3", "input": ["hello"], "truncate": True}])

    def test_embed_batch(self):
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": ["a", "b", "c"]})
        self.assertEqual((code, len(body["embeddings"])), (200, 3))

    def test_other_model_denied(self):
        for model in ("nomic-embed-text", "qwen3:8b", "snowflake-arctic-embed2:568m", "bge-m3:other", None, 5):
            code, _ = self.send("/api/embed", {"model": model, "input": "x"})
            self.assertEqual(code, 403, model)
        self.assertEqual(self.upstream.requests, [])

    def test_second_embed_model_passes_under_its_own_name(self):
        """Сцена смены модели: snowflake-arctic-embed2 пускаем, и наверх уходит именно она."""
        code, body = self.send("/api/embed", {"model": "snowflake-arctic-embed2", "input": ["query: hello", "doc"]})
        self.assertEqual((code, len(body["embeddings"])), (200, 2))
        code, _ = self.send("/api/embed", {"model": "snowflake-arctic-embed2:latest", "input": "x", "keep_alive": -1})
        self.assertEqual(code, 200)
        self.assertEqual(self.upstream.json_bodies(), [
            {"model": "snowflake-arctic-embed2", "input": ["query: hello", "doc"], "truncate": True},
            {"model": "snowflake-arctic-embed2", "input": ["x"], "truncate": True}])

    def test_every_model_with_a_query_prefix_is_allowed(self):
        """Модели из app/embed_models.py сервис может получить из реестра: фильтр должен их пускать."""
        from app import embed_models
        self.assertEqual(sorted(embed_models.QUERY_PREFIX), sorted(flt.EMBED_MODELS))

    def test_input_limits(self):
        cases = [[], [""], ["x" * (flt.MAX_CHARS + 1)], ["x"] * (flt.MAX_INPUTS + 1), [1], "", None]
        for inp in cases:
            code, _ = self.send("/api/embed", {"model": "bge-m3", "input": inp})
            self.assertEqual(code, 400, repr(inp)[:40])
        self.assertEqual(self.upstream.requests, [])

    def test_body_limits(self):
        self.assertEqual(self.send("/api/embed", raw=b"{not json")[0], 400)
        self.assertEqual(self.send("/api/embed", raw=b"")[0], 413)
        self.assertEqual(self.send("/api/embed", ["bge-m3"])[0], 400)  # не объект JSON
        self.patch(MAX_BODY=100)
        self.assertEqual(self.send("/api/embed", {"model": "bge-m3", "input": "x" * 200})[0], 413)
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_down(self):
        self.patch(UPSTREAM=closed_port_url())
        code, body = self.send("/api/embed", {"model": "bge-m3", "input": "hello"})
        self.assertEqual(code, 502)
        self.assertEqual(body["error"], "векторизатор недоступен")


class DenyTest(FilterTest):
    def test_dangerous_calls_denied_and_logged(self):
        for path in ("/api/pull", "/api/push", "/api/create", "/api/generate", "/api/copy", "/api/delete"):
            self.assertEqual(self.send(path, {"model": "bge-m3"})[0], 403, path)
        self.assertEqual(self.send("/api/tags", method="GET")[0], 403)
        self.assertEqual(self.send("/api/ps", method="GET")[0], 403)
        self.assertEqual(self.send("/api/delete", {"model": "bge-m3"}, method="DELETE")[0], 403)
        self.assertEqual(self.upstream.requests, [])  # до Ollama не дошло ничего
        denied = [e for e in self.events() if e["event"] == "denied"]
        self.assertEqual(len(denied), 9)
        self.assertIn("/api/pull", {e["path"] for e in denied})

    def test_refuses_all_interfaces(self):
        for addr in ("0.0.0.0", "::"):
            with mock.patch.object(sys, "argv", ["ollama_filter.py", "--listen", addr]):
                with self.assertRaises(SystemExit):
                    flt.main()



def chat_request(**over) -> dict:
    body = {"model": "qwen3:8b", "stream": False, "think": False, "format": {"type": "object"},
            "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}],
            "options": {"temperature": 0, "seed": 7, "num_ctx": 4096, "num_predict": 400}}
    body.update(over)
    return body


class ChatTest(FilterTest):
    def setUp(self) -> None:
        super().setUp()
        self.upstream.handler = ollama_handler(chat_content='{"found": false, "answer": "", "citations": []}')

    def test_chat_forwarded_clean(self):
        code, body = self.send("/api/chat", chat_request())
        self.assertEqual(code, 200)
        self.assertEqual(body["message"]["role"], "assistant")
        self.assertEqual(self.upstream.json_bodies(), [{
            "model": "qwen3:8b", "stream": False, "think": False, "format": {"type": "object"},
            "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}],
            "options": {"temperature": 0, "seed": 7, "num_ctx": 4096, "num_predict": 400}}])
        self.assertEqual([e["event"] for e in self.events()], ["chat"])

    def test_context_fixed_and_answer_capped(self):
        self.send("/api/chat", chat_request(options={"num_ctx": 131072}))
        sent = self.upstream.json_bodies()[-1]["options"]
        self.assertEqual(sent, {"num_ctx": flt.CHAT_NUM_CTX, "num_predict": flt.CHAT_MAX_PREDICT})
        self.assertEqual(self.send("/api/chat", chat_request(options={"num_predict": 100000}))[0], 400)

    def test_other_models_denied(self):
        for model in ("qwen3:14b", "bge-m3", "snowflake-arctic-embed2", "llama3"):
            self.assertEqual(self.send("/api/chat", chat_request(model=model))[0], 403, model)
        self.assertEqual(self.upstream.requests, [])

    def test_dangerous_fields_denied(self):
        cases = [chat_request(keep_alive=-1), chat_request(tools=[{"type": "function"}]),
                 chat_request(images=["aGk="]), chat_request(raw=True), chat_request(stream=True),
                 chat_request(think="high"), chat_request(format="yaml"),
                 chat_request(options={"num_gpu": 99}), chat_request(options={"temperature": True}),
                 chat_request(options={"top_k": 1.5}), chat_request(options=[1])]
        for body in cases:
            self.assertEqual(self.send("/api/chat", body)[0], 400, json.dumps(body)[:80])
        self.assertEqual(self.upstream.requests, [])

    def test_messages_limits(self):
        bad = [[], [{"role": "tool", "content": "x"}], [{"role": "user", "content": ""}],
               [{"role": "user", "content": "x", "images": ["aGk="]}], [{"role": "user", "content": 5}],
               [{"role": "user", "content": "x"}] * (flt.CHAT_MAX_MESSAGES + 1),
               [{"role": "user", "content": "x" * (flt.CHAT_MAX_CHARS + 1)}], "hello"]
        for msgs in bad:
            self.assertEqual(self.send("/api/chat", chat_request(messages=msgs))[0], 400, repr(msgs)[:60])
        self.assertEqual(self.upstream.requests, [])

    def test_one_at_a_time(self):
        self.assertTrue(flt.CHAT_SLOT.acquire(blocking=False))  # модель «занята» другим запросом
        try:
            code, body = self.send("/api/chat", chat_request())
        finally:
            flt.CHAT_SLOT.release()
        self.assertEqual((code, body["error"]), (429, "модель занята"))
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.send("/api/chat", chat_request())[0], 200)  # слот освободился

    def test_upstream_down_releases_slot(self):
        self.patch(UPSTREAM=closed_port_url())
        self.assertEqual(self.send("/api/chat", chat_request())[0], 502)
        self.assertTrue(flt.CHAT_SLOT.acquire(blocking=False))
        flt.CHAT_SLOT.release()

    def test_generate_still_denied(self):
        self.assertEqual(self.send("/api/generate", {"model": "qwen3:8b", "prompt": "hi"})[0], 403)
        self.assertEqual(self.upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
