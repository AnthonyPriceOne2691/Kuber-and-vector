"""Витрина для ролика (showcase/): прокси во вход стенда против подставного входа, эталон
LegalBench-RAG на маленьких примерах, numbers.json (пропуски -> pending), защита от вставки HTML,
заглушка входа с выбором договора и гейтом (сверка с сервисом на одном стенде в памяти). Стенда и
сети нет: вход — FakeServer на 127.0.0.1, сертификаты TLS — одноразовые из openssl по рецепту
deploy/observer/install.sh (нет openssl — эти тесты пропускаются).

    python3 -I -B -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from array import array
from html.parser import HTMLParser
from unittest import mock

from app import routing
from eval import legalbench as lb
from showcase import numbers as board
from showcase import server
from showcase import stub
from showcase.truth import Benchmark, merge, norm_query, segments
from tests import test_routing as tr  # стенд в памяти и подставная Ollama тестов выбора договора в сервисе
from tests.fakes import FakeServer, closed_port_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOWCASE = os.path.join(ROOT, "showcase")
STATIC = os.path.join(SHOWCASE, "static")
KEY = "entry-test-key-0123456789abcdef"

# --- маленький LegalBench-RAG: два документа, два вопроса ------------------------------------
PHRASE = "The term of this Agreement ends on 31 December 2030."
DOC_A = "Preamble. " * 48 + PHRASE + " " + "Other terms apply here. " * 60   # фраза на [480, 532)
FILE_A = "cuad/Acme_License.txt"
Q_A = "Consider the License Agreement of Acme; When does the term end?"
DOC_B = ("Intro line.\n" * 20) + "We never sell your data.\n" + ("Filler text. " * 20) + "You may delete your account.\n"
FILE_B = "privacy_qa/Beta Notes.txt"
Q_B = "Consider \"Beta\"'s privacy policy; is my data sold or deleted?"


def make_dataset(root: str) -> None:
    os.makedirs(os.path.join(root, "benchmarks"))
    for f, text in ((FILE_A, DOC_A), (FILE_B, DOC_B)):
        os.makedirs(os.path.join(root, "corpus", os.path.dirname(f)), exist_ok=True)
        with open(os.path.join(root, "corpus", f), "w", encoding="utf-8") as fp:
            fp.write(text)
    a0 = DOC_A.index(PHRASE)
    b1, b2 = DOC_B.index("We never sell"), DOC_B.index("You may delete")
    tests = {
        "cuad": [{"query": Q_A, "snippets": [{"file_path": FILE_A, "span": [a0, a0 + len(PHRASE)], "answer": PHRASE}]}],
        "privacy_qa": [{"query": Q_B, "snippets": [
            {"file_path": FILE_B, "span": [b1, b1 + 24], "answer": DOC_B[b1:b1 + 24]},
            {"file_path": FILE_B, "span": [b2, b2 + 28], "answer": DOC_B[b2:b2 + 28]}]}],
        "contractnli": [], "maud": [],
    }
    for name, items in tests.items():
        with open(os.path.join(root, "benchmarks", f"{name}.json"), "w", encoding="utf-8") as fp:
            json.dump({"tests": items}, fp)


def hit(s: int, e: int, file: str = FILE_A, doc: str = DOC_A, score: float = 0.8, text: str | None = None) -> dict:
    """Кусок в том виде, в каком его отдаёт /search сервиса (app/api.py)."""
    return {"score": score, "document": os.path.splitext(os.path.basename(file))[0].replace("_", " "),
            "section": "1. Term.", "file": file, "span": [s, e], "text": doc[s:e] if text is None else text}


class DataMixin:
    def make_data(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        make_dataset(tmp.name)
        return tmp.name


# --- эталон и сопоставление ---------------------------------------------------------------------
class TruthTest(DataMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Benchmark(self.make_data())

    def test_norm_query(self):
        self.assertEqual(norm_query("  a \n b\t c "), "a b c")
        self.assertEqual(norm_query(None), "")

    def test_merge_and_segments(self):
        self.assertEqual(merge([(5, 9), (0, 3), (3, 4), (8, 12), (20, 20)]), [(0, 4), (5, 12)])
        text = "0123456789abcdef"
        segs = segments(text, [(2, 5), (10, 16)])
        self.assertEqual("".join(s["text"] for s in segs), text)  # склейка даёт исходный текст
        self.assertEqual([(s["text"], s["truth"]) for s in segs],
                         [("01", False), ("234", True), ("56789", False), ("abcdef", True)])
        self.assertEqual(segments("abc", []), [{"text": "abc", "truth": False}])
        self.assertEqual(segments("", []), [{"text": "", "truth": False}])

    def test_lookup_finds_question_with_other_whitespace(self):
        t = self.bench.lookup("Consider the License Agreement of Acme;\n  When does the term end? ")
        self.assertIsNotNone(t)
        self.assertEqual((t["dataset"], t["dataset_name"], t["in_mini"]), ("cuad", "CUAD", True))
        sn = t["snippets"][0]
        self.assertEqual(sn["file"], FILE_A)
        self.assertEqual(sn["text"], PHRASE)
        self.assertEqual(DOC_A[sn["span"][0]:sn["span"][1]], sn["text"])  # координаты эталона в знаках документа
        self.assertEqual(t["total_chars"], len(PHRASE))
        self.assertIsNone(self.bench.lookup("When does the term end?"))
        self.assertIsNot(self.bench.lookup(Q_A), self.bench.lookup(Q_A))  # каждый раз новый словарь

    def test_annotate_overlaps_on_small_example(self):
        a0 = DOC_A.index(PHRASE)  # 480: фраза лежит на границе кусков по 500 знаков
        hits = [hit(500, 1000), hit(0, 500), hit(a0 + len(PHRASE), 1000), hit(0, 500, FILE_B, DOC_B)]
        truth = self.bench.lookup(Q_A)
        ann = self.bench.annotate(hits, truth)
        h = ann["hits"]
        self.assertEqual([x["match"] for x in h], [True, True, False, False])
        self.assertEqual(h[0]["overlap"], a0 + len(PHRASE) - 500)
        self.assertEqual(h[0]["segments"][0], {"text": DOC_A[500:a0 + len(PHRASE)], "truth": True})
        self.assertEqual(h[1]["segments"][-1], {"text": DOC_A[a0:500], "truth": True})
        self.assertEqual("".join(s["text"] for s in h[1]["segments"]), DOC_A[0:500])
        self.assertEqual(h[2]["overlap"], 0)  # кусок начинается ровно там, где кончается разметка
        self.assertEqual(ann["found_in"], [[1, 2]])
        p, r = lb.precision_recall([(x["file"], *x["span"]) for x in hits], [(FILE_A, a0, a0 + len(PHRASE))])
        self.assertEqual(ann["coverage"], {"k": 4, "recall": round(r, 4), "precision": round(p, 4)})
        self.assertEqual(ann["coverage"]["recall"], 1.0)

    def test_two_snippets_one_found(self):
        b2 = DOC_B.index("You may delete")
        truth = self.bench.lookup(Q_B)
        ann = self.bench.annotate([hit(b2 - 10, len(DOC_B), FILE_B, DOC_B)], truth)
        self.assertEqual(ann["found_in"], [[], [1]])
        self.assertTrue(ann["hits"][0]["match"])
        self.assertAlmostEqual(ann["coverage"]["recall"], 28 / (24 + 28), places=4)

    def test_without_truth_match_is_unknown(self):
        ann = self.bench.annotate([hit(0, 500)], None)
        self.assertIsNone(ann["hits"][0]["match"])
        self.assertIsNone(ann["coverage"])
        self.assertTrue(ann["hits"][0]["span_ok"])  # адрес всё равно сверяется с файлом

    def test_span_check_against_source_file(self):
        self.assertTrue(self.bench.check_span(FILE_A, 0, 500, DOC_A[:500]))
        self.assertFalse(self.bench.check_span(FILE_A, 1, 501, DOC_A[:500]))
        self.assertFalse(self.bench.check_span(FILE_A, 0, 10 ** 6, DOC_A))
        self.assertIsNone(self.bench.check_span("cuad/missing.txt", 0, 5, "abcde"))
        # путь со стенда не выводит из корпуса
        self.assertIsNone(self.bench.doc("../benchmarks/cuad.json"))
        self.assertIsNone(self.bench.doc("/etc/passwd"))
        self.assertIsNone(self.bench.doc("cuad/../../benchmarks/cuad.json"))

    def test_text_and_address_disagree(self):
        bad = hit(500, 1000, text=DOC_A[500:900])  # текст короче адреса: подсветку не режем
        ann = self.bench.annotate([bad], self.bench.lookup(Q_A))["hits"][0]
        self.assertTrue(ann["match"])
        self.assertEqual(ann["segments"], [{"text": DOC_A[500:900], "truth": False}])
        self.assertFalse(ann["span_ok"])

    def test_malformed_hits(self):
        weird = [{"text": "no address"}, "not a dict", {"file": FILE_A, "span": [5, 2], "text": "x"},
                 {"file": FILE_A, "span": [True, 3], "text": "x"}]
        ann = self.bench.annotate(weird, self.bench.lookup(Q_A))
        self.assertEqual([x["match"] for x in ann["hits"]], [None] * 4)
        self.assertEqual(ann["hits"][0]["segments"], [{"text": "no address", "truth": False}])
        self.assertEqual(ann["coverage"]["recall"], 0.0)

    def test_no_data(self):
        for root in (None, os.path.join(ROOT, "no-such-dir")):
            b = Benchmark(root)
            self.assertFalse(b.available())
            self.assertIsNone(b.lookup(Q_A))
            self.assertIsNone(b.annotate([hit(0, 500)], None)["hits"][0]["span_ok"])


# --- numbers.json -------------------------------------------------------------------------------
def _display_strings(v: dict) -> list[str]:
    out = [v["title"], v["subtitle"], v["caption"]]
    q = v["quality"] or {}
    out += [q.get(k) or "" for k in ("title", "benchmark", "metric", "search", "caveat", "unit")]
    for r in q.get("rows", []):
        out += [r["label"], r["value"] or "", r["unit"]]
    for t in v["tiles"]:
        if t["kind"] == "stat":
            out += [t["title"], t["prefix"], t["value"] or "", t["of"] or "", t["unit"], t["note"]]
            for f in t["facts"]:
                out += [f["label"], f["prefix"], f["value"] or "", f["unit"]]
    return out


def _raw_strings(x) -> list[str]:
    if isinstance(x, dict):
        return [s for v in x.values() for s in _raw_strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in _raw_strings(v)]
    if isinstance(x, str):
        return [x]
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return [board.fmt(x) or ""]
    return []


class NumbersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = board.load(os.path.join(SHOWCASE, "numbers.json"))
        self.view = board.view(self.raw)
        self.tiles = {t["id"]: t for t in self.view["tiles"]}

    def test_shipped_file_is_clean(self):
        self.assertEqual(self.view["problems"], [])
        self.assertEqual(self.view["caption"], "3 VMs + observer on one laptop · numbers for this hardware")
        self.assertFalse(self.view["hide_pending"])

    def test_measured_values_from_the_stand(self):
        def facts(tid):
            return [(f["prefix"], f["value"], f["unit"]) for f in self.tiles[tid]["facts"]]
        t = self.tiles["one-machine-off"]
        self.assertEqual((t["state"], t["value"]), ("measured", "0"))
        self.assertEqual(facts("one-machine-off"), [("", "3,439", ""), ("", "1", ""), ("", "19", "s"), ("", "49", "s")])
        self.assertEqual((self.tiles["two-machines-off"]["value"], facts("two-machines-off")), ("0", [("", "1,498", ""), ("", "2", "")]))
        self.assertEqual((self.tiles["writes-during-failure"]["value"], facts("writes-during-failure")),
                         ("0", [("", "1,489", "")]))
        pod = self.tiles["infected-pod"]
        self.assertEqual((pod["value"], pod["of"]), ("14", "14"))
        self.assertEqual(facts("infected-pod"), [("< ", "1", "ms"), ("", "1", "ms"), ("", "11", "ms"), ("", "15", "")])
        self.assertEqual((self.tiles["port-scan"]["value"], facts("port-scan")), ("1", [("", "262,140", ""), ("", "443", "")]))
        self.assertEqual((self.tiles["image-scan"]["value"], facts("image-scan")), ("0", [("", "7", "")]))
        q = self.view["quality"]
        self.assertEqual([(r["id"], r["value"], r["bar"]) for r in q["rows"]],
                         [("plain", "51.8", "51.8%"), ("title", "82.6", "82.6%"), ("ours", "89.4", "89.4%"),
                          ("paper", "63.5", "63.5%")])
        self.assertEqual((q["metric"], q["state"]), ("recall@64", "measured"))
        self.assertIn("31 points", q["caveat"])
        self.assertEqual(self.tiles["quality"]["state"], "measured")

    def test_measured_on_the_stand(self):
        # 19.11: полный LegalBench-RAG (eval/run_full.sh) и предел на синтетике (eval/results/limit.md)
        want = {"full-corpus": "159,767", "vector-limit": "1,000,000", "latency-p95": "32"}
        for tid, value in want.items():
            self.assertEqual((self.tiles[tid]["state"], self.tiles[tid]["value"]), ("measured", value), tid)

    def test_every_number_on_screen_comes_from_the_file(self):
        allowed = " ".join(_raw_strings(self.raw))
        for s in _display_strings(self.view):
            for token in re.findall(r"\d[\d,.]*\d|\d", s):
                self.assertIn(token, allowed, f"{token!r} из {s!r} не из numbers.json")

    def test_pending_hidden_on_request(self):
        raw = {"schema": board.SCHEMA, "hide_pending": True, "caption": "c",
               "quality": {"unit": "%", "rows": [{"id": "a", "label": "A", "value": 50}, {"id": "b", "label": "B", "value": None}]},
               "tiles": [{"id": "quality", "kind": "quality"},
                         {"id": "x", "title": "X", "value": 3, "facts": [{"label": "n", "value": None}, {"label": "m", "value": 2}]},
                         {"id": "y", "title": "Y", "value": None},
                         {"id": "z", "title": "Z", "value": 1, "hidden": True}]}
        v = board.view(raw)
        self.assertEqual([t["id"] for t in v["tiles"]], ["quality", "x"])
        self.assertEqual([f["label"] for f in v["tiles"][1]["facts"]], ["m"])
        self.assertEqual([r["id"] for r in v["quality"]["rows"]], ["a"])
        raw["hide_pending"] = False
        v = board.view(raw)
        self.assertEqual([(t["id"], t["state"]) for t in v["tiles"]], [("quality", "partial"), ("x", "measured"), ("y", "pending")])
        self.assertEqual([f["state"] for f in v["tiles"][1]["facts"]], ["pending", "measured"])

    def test_bad_values_become_pending_not_numbers(self):
        raw = {"schema": board.SCHEMA, "tiles": [
            {"id": "a", "title": "A", "value": "2656"}, {"id": "b", "title": "B", "value": True},
            {"id": "c", "title": "C", "value": float("nan")}, {"id": "a", "title": "dup", "value": 1},
            {"id": "Bad Id", "value": 1}, {"title": "no id", "value": 1}, {"id": "q", "kind": "quality"},
            {"id": "t", "value": 1, "tone": "loud"}]}
        v = board.view(raw)
        self.assertEqual([(t["id"], t["value"]) for t in v["tiles"]], [("a", None), ("b", None), ("c", None), ("t", "1")])
        self.assertEqual(v["tiles"][-1]["tone"], "neutral")
        # три плохих значения, повтор, два плохих id, плитка качества без блока, неизвестный tone
        self.assertEqual(len(v["problems"]), 8, v["problems"])
        self.assertIsNone(board.fmt(float("inf")))
        self.assertEqual(board.fmt(1234567), "1,234,567")
        self.assertEqual(board.view({})["problems"], ["schema: ждали showcase-numbers/1"])

    def test_quality_bar_for_other_units(self):
        q = board.quality_view({"unit": "ms", "rows": [{"value": 20}, {"value": 10}]}, [])
        self.assertEqual([r["bar"] for r in q["rows"]], ["100%", "50%"])
        q = board.quality_view({"unit": "%", "rows": [{"value": 140}, {"value": -5}]}, [])
        self.assertEqual([r["bar"] for r in q["rows"]], ["100%", "0%"])


# --- подставной вход стенда -----------------------------------------------------------------------
def entry_handler(search_hits: list, answer_doc: dict | None = None):
    def handler(method, path, headers, body):
        if headers.get("X-API-Key") != KEY:
            return 401, {"error": "нужен ключ API"}
        u = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/search":
            k = int(qs["k"][0])
            return 200, {"query": qs["q"][0], "model": "bge-m3", "collection": "contracts_rf2", "mode": "dense",
                         "timing_ms": {"embed": 11.2, "search": 2.4}, "qdrant_attempts": 1, "qdrant_route": "local",
                         "served_by": {"node": "machine-2", "pod": "search-abc"}, "results": search_hits[:k]}
        if u.path == "/answer":
            return 200, answer_doc
        return 404, {"error": "not found"}
    return handler


class _Http:
    """Наш сервер витрины на 127.0.0.1:случайный порт в отдельном потоке."""

    def __init__(self, app, verbose: bool = False) -> None:
        self.srv = server.ShowcaseHTTPServer(("127.0.0.1", 0), server.make_handler(app, verbose=verbose))
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def get(self, path: str, headers: dict | None = None, method: str = "GET", data: bytes | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method, data=data)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class ShowcaseCase(DataMixin, unittest.TestCase):
    """Витрина против подставного входа: ключ во временном файле, свои данные набора."""

    hits = [hit(500, 1000), hit(0, 500), hit(1000, 1500), hit(0, 500, FILE_B, DOC_B)]
    answer_doc = None

    def setUp(self) -> None:
        self.data = self.make_data()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.key_file = os.path.join(self.tmp, "entry-api-key.header")
        with open(self.key_file, "w") as f:
            f.write(f"X-API-Key: {KEY}\n")
        self.fake = FakeServer(entry_handler(self.hits, self.answer_doc), hang=0.6)
        self.addCleanup(self.fake.close)
        self.logs: list[str] = []
        self.app = self.make_app(server.Entry(url=self.fake.url, key_file=self.key_file, timeout_search=2.0,
                                              timeout_answer=2.0))

    def make_app(self, entry: server.Entry, numbers: str | None = None) -> server.Showcase:
        self.assertIn("://127.0.0.1:", entry.url)  # в тестах вход только на 127.0.0.1, к стенду не ходим
        return server.Showcase(entry, Benchmark(self.data), numbers or os.path.join(SHOWCASE, "numbers.json"),
                               os.path.join(SHOWCASE, "presets.json"), log=self.logs.append)

    def http(self, verbose: bool = False) -> _Http:
        h = _Http(self.app, verbose=verbose)
        self.addCleanup(h.close)
        return h


class ProxyTest(ShowcaseCase):
    def test_search_forwards_key_and_question(self):
        code, d = self.app.ask("search", "  " + Q_A + " ", "3")
        self.assertEqual(code, 200, d)
        method, path, headers, _ = self.fake.requests[0]
        u = urllib.parse.urlsplit(path)
        self.assertEqual((method, u.path), ("GET", "/search"))
        self.assertEqual(urllib.parse.parse_qs(u.query), {"q": [Q_A], "k": ["3"]})
        self.assertEqual(headers.get("X-API-Key"), KEY)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["result"]["results"]), 3)
        self.assertEqual([a["match"] for a in d["annotations"]], [True, True, False])
        self.assertEqual(d["truth"]["snippets"][0]["found_in"], [1, 2])
        self.assertEqual(d["coverage"]["k"], 3)
        self.assertIsInstance(d["elapsed_ms"], float)
        self.assertEqual([a["span_ok"] for a in d["annotations"]], [True, True, True])

    def test_question_outside_the_benchmark(self):
        code, d = self.app.ask("search", "Who pays for shipping?", None)
        self.assertEqual(code, 200)
        self.assertIsNone(d["truth"])
        self.assertTrue(d["truth_available"])
        self.assertEqual({a["match"] for a in d["annotations"]}, {None})
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(self.fake.paths()[0]).query)["k"], ["5"])

    def test_input_checked_before_the_entry(self):
        for endpoint, q, k in (("search", "ab", "5"), ("search", "x" * 501, "5"), ("search", Q_A, "0"),
                               ("search", Q_A, "21"), ("search", Q_A, "five"), ("answer", Q_A, "9"),
                               ("delete", Q_A, "5")):
            code, d = self.app.ask(endpoint, q, k)
            self.assertIn(code, (400, 404), (endpoint, q[:10], k))
            self.assertFalse(d["ok"])
        self.assertEqual(self.fake.requests, [])

    def test_upstream_errors_are_english_and_typed(self):
        cases = [("search", 401, "key"), ("search", 429, "busy"), ("search", 503, "unavailable"),
                 ("answer", 502, "format"), ("answer", 429, "busy"), ("search", 500, "upstream")]
        for endpoint, status, kind in cases:
            self.fake.handler = lambda *a, s=status: (s, {"error": "база недоступна", "detail": "по-русски"})
            code, d = self.app.ask(endpoint, Q_A, "5")
            self.assertEqual((code, d["kind"]), (status, kind), (endpoint, status))
            self.assertFalse(re.search("[а-яё]", json.dumps(d, ensure_ascii=False)), d)  # на экран только английский
        self.assertTrue(any("база недоступна" in m for m in self.logs))  # русский текст сервиса — в журнал

    def test_wrong_key_is_401(self):
        with open(self.key_file, "w") as f:
            f.write("X-API-Key: not-the-key\n")
        code, d = self.app.ask("search", Q_A, "5")
        self.assertEqual((code, d["kind"]), (401, "key"))

    def test_key_file_problems(self):
        os.remove(self.key_file)
        code, d = self.app.ask("search", Q_A, "5")
        self.assertEqual((code, d["kind"]), (500, "config"))
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(self.app.entry.public()["ready"])

    def test_key_header_parsing(self):
        self.assertEqual(server.parse_key_header(f"X-API-Key: {KEY}\n"), ("X-API-Key", KEY))
        self.assertEqual(server.parse_key_header(f"\n{KEY}\n\n"), ("X-API-Key", KEY))
        for bad in ("", "X-API-Key:", f"X-API-Key: {KEY}\nX-Other: 1", f"X-API-Key: {KEY} tail", "Bad Header: x",
                    f"X-API-Key: {KEY}\r\nInjected: 1"):
            with self.assertRaises(server.ConfigError) as ctx:
                server.parse_key_header(bad)
            self.assertNotIn(KEY, str(ctx.exception))

    def test_timeout_and_unreachable(self):
        self.fake.handler = lambda *a: None  # вход принял соединение и молчит
        self.app.entry.timeout_search = 0.3
        code, d = self.app.ask("search", Q_A, "5")
        self.assertEqual((code, d["kind"]), (504, "timeout"))
        app = self.make_app(server.Entry(url=closed_port_url(), key_file=self.key_file))
        code, d = app.ask("search", Q_A, "5")
        self.assertEqual((code, d["kind"]), (502, "unreachable"))

    def test_not_json_and_bad_entry_url(self):
        self.fake.handler = lambda *a: (200, b"<html>maintenance</html>")
        code, d = self.app.ask("search", Q_A, "5")
        self.assertEqual((code, d["kind"]), (502, "upstream"))
        for url in ("ftp://127.0.0.1:1", "http://127.0.0.1:1/search", "http://127.0.0.1:99999", "https://127.0.0.1:1"):
            app = self.make_app(server.Entry(url=url, key_file=self.key_file))  # https без УЦ — тоже не настроено
            code, d = app.ask("search", Q_A, "5")
            self.assertEqual((code, d["kind"]), (500, "config"), url)

    def test_key_never_reaches_page_or_log(self):
        h = self.http(verbose=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            bodies = [h.get("/api/config")[2], h.get("/api/search?" + urllib.parse.urlencode({"q": Q_A, "k": 2}))[2]]
            self.fake.handler = lambda *a: (401, {"error": "нужен ключ API"})
            bodies.append(h.get("/api/search?" + urllib.parse.urlencode({"q": Q_A}))[2])
            os.remove(self.key_file)
            bodies.append(h.get("/api/search?" + urllib.parse.urlencode({"q": Q_A}))[2])
            bodies.append(h.get("/api/config")[2])
        for b in bodies:
            self.assertNotIn(KEY, b.decode())
            self.assertNotIn(self.tmp, b.decode())  # и путей с диска на странице нет
        self.assertIn("/api/search", err.getvalue())  # журнал запросов включён
        self.assertNotIn(KEY, err.getvalue() + "\n".join(self.logs))


class AnswerTest(ShowcaseCase):
    answer_doc = {"query": Q_A, "model": "qwen3:8b", "collection": "contracts_rf2", "status": "answered",
                  "answer": "It ends on 31 December 2030 [1].",
                  "citations": [{"n": 1, "quote": "ends on 31 December 2030", "valid": True, "verified": True,
                                 "document": "Acme License", "section": "1. Term.", "file": FILE_A, "span": [500, 1000]}],
                  "sources": [hit(500, 1000), hit(1000, 1500)], "timing_ms": {"embed": 10.0, "search": 2.0, "generate": 4100.0},
                  "tokens": {"prompt": 900, "output": 40}, "qdrant_attempts": 1, "qdrant_route": "local",
                  "served_by": {"node": "machine-1", "pod": "search-x"}}

    def test_answer_sources_are_annotated(self):
        code, d = self.app.ask("answer", Q_A, "8")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["result"]["status"], "answered")
        self.assertEqual([a["match"] for a in d["annotations"]], [True, False])
        self.assertEqual(d["truth"]["snippets"][0]["found_in"], [1])
        self.assertEqual(urllib.parse.urlsplit(self.fake.paths()[0]).path, "/answer")


# --- выбор договора и гейт сервиса: поле route и отказ not_in_base -------------------------------------
Q_OUT = "Consider the Lease Agreement between Nobody Holdings and Nowhere Estates; What is the monthly rent?"
ROUTE_A = {"used": True, "reference": "Consider the License Agreement of Acme", "question": "When does the term end?",
           "document": {"file": FILE_A, "title": "Acme License", "score": 0.7162},
           "gate": {"score": 0.6271, "threshold": 0.585, "passed": True, "closest": "Acme License"}}
ROUTE_OUT = {"used": True, "reference": "Consider the Lease Agreement between Nobody Holdings and Nowhere Estates",
             "question": "What is the monthly rent?", "document": None,
             "gate": {"score": 0.4516, "threshold": 0.585, "passed": False, "closest": "Beta Notes"}}


class RouteProxyTest(ShowcaseCase):
    """Сервер витрины отдаёт тело сервиса как есть: поле route и отказ гейта доходят до страницы."""

    def entry(self, endpoint: str, doc: dict) -> None:
        def handler(method, path, headers, body):
            if headers.get("X-API-Key") != KEY:
                return 401, {"error": "нужен ключ API"}
            if urllib.parse.urlsplit(path).path != "/" + endpoint:
                return 404, {"error": "not found"}
            return 200, doc
        self.fake.handler = handler

    def search_doc(self, q: str, results: list, **extra) -> dict:
        return {"query": q, "model": "bge-m3", "collection": "contracts_rf2", "mode": "dense",
                "timing_ms": {"embed": 11.2, "search": 2.4}, "qdrant_attempts": 1, "qdrant_route": "local",
                "served_by": {"node": "machine-2", "pod": "search-abc"}, "results": results, **extra}

    def test_chosen_contract_passes_with_annotation(self):
        self.entry("search", self.search_doc(Q_A, [hit(500, 1000), hit(0, 500)], route=ROUTE_A))
        code, d = self.app.ask("search", Q_A, "2")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["result"]["route"], ROUTE_A)
        self.assertNotIn("not_in_base", d["result"])
        self.assertEqual([a["match"] for a in d["annotations"]], [True, True])  # разметка как раньше

    def test_not_in_base_passes_without_passages(self):
        self.entry("search", self.search_doc(Q_OUT, [], route=ROUTE_OUT, not_in_base=True,
                                             message="this contract is not in the database"))
        code, d = self.app.ask("search", Q_OUT, "5")
        self.assertEqual(code, 200, d)
        r = d["result"]
        self.assertEqual((r["not_in_base"], r["message"], r["results"]), (True, "this contract is not in the database", []))
        self.assertEqual(r["route"], ROUTE_OUT)
        self.assertEqual((d["annotations"], d["truth"], d["coverage"]), ([], None, None))
        body = json.loads(self.http().get("/api/search?" + urllib.parse.urlencode({"q": Q_OUT}))[2])
        self.assertEqual(body["result"]["route"]["gate"], ROUTE_OUT["gate"])  # и через HTTP сервера витрины

    def test_answer_refusal_passes(self):
        doc = {"query": Q_OUT, "model": "qwen3:8b", "embed_model": "bge-m3", "collection": "contracts_rf2",
               "status": "refused", "citations": [], "answer": "This contract is not in the database.",
               "sources": [], "timing_ms": {"embed": 10.0, "search": 1.2}, "tokens": {"prompt": None, "output": None},
               "qdrant_attempts": 1, "qdrant_route": "local", "served_by": {"node": "machine-1", "pod": "search-x"},
               "route": ROUTE_OUT, "not_in_base": True}
        self.entry("answer", doc)
        code, d = self.app.ask("answer", Q_OUT, "5")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["result"], doc)
        self.assertEqual(d["annotations"], [])


class HttpLayerTest(ShowcaseCase):
    hits = [hit(500, 1000, text=None), {**hit(0, 500), "text": DOC_A[:450] + "<img src=x onerror=alert(1)>"[:50]}]

    def test_api_is_json_with_nosniff_and_html_stays_text(self):
        h = self.http()
        code, headers, body = h.get("/api/search?" + urllib.parse.urlencode({"q": Q_A, "k": 2}))
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        d = json.loads(body)
        text = "".join(s["text"] for s in d["annotations"][1]["segments"])
        self.assertIn("<img src=x onerror=alert(1)>", text)  # строка данных: страница кладёт её через textContent
        self.assertEqual(text, d["result"]["results"][1]["text"])

    def test_pages_get_csp_with_hashes(self):
        h = self.http()
        for path, name in (("/", "index.html"), ("/board", "board.html")):
            code, headers, body = h.get(path)
            self.assertEqual(code, 200, path)
            csp = headers["Content-Security-Policy"]
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertIn("default-src 'none'", csp)
            self.assertEqual(len(re.findall(r"'sha256-", csp)), 2, path)  # один <script> и один <style>
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            with open(os.path.join(STATIC, name), "rb") as f:
                self.assertEqual(body, f.read())

    def test_static_pages_never_build_html_from_data(self):
        sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                           r"setAttribute\(\s*[\"']on|srcdoc|createContextualFragment|DOMParser")
        for name in ("index.html", "board.html"):
            with open(os.path.join(STATIC, name), encoding="utf-8") as f:
                page = f.read()
            self.assertIsNone(sinks.search(page), name)
            markup = re.sub(r"<(script|style)>.*?</\1>", "", page, flags=re.S)
            self.assertIsNone(re.search(r"\son\w+\s*=", markup), name)  # обработчики в разметке не ставим
            self.assertNotIn("http://", markup.lower() + "")  # ни шрифтов, ни CDN
            self.assertNotIn("https://", page)

    def test_host_and_cross_site_guards(self):
        h = self.http()
        q = "/api/search?" + urllib.parse.urlencode({"q": Q_A})
        self.assertEqual(h.get(q, {"Host": "evil.example:8095"})[0], 421)
        self.assertEqual(h.get(q, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.fake.requests, [])  # ни один чужой запрос не дошёл до входа
        self.assertEqual(h.get(q, {"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(h.get("/", {"Host": "localhost:8095", "Sec-Fetch-Site": "cross-site"})[0], 200)
        self.assertEqual(h.get(q, method="POST", data=b"{}")[0], 405)
        self.assertEqual(h.get("/../../etc/passwd")[0], 404)
        self.assertEqual(h.get("/static/index.html")[0], 404)

    def test_config_truth_numbers_endpoints(self):
        h = self.http()
        cfg = json.loads(h.get("/api/config")[2])
        self.assertTrue(cfg["entry"]["ready"])
        self.assertFalse(cfg["stub"])
        self.assertEqual(cfg["limits"]["search_k_max"], 20)
        self.assertGreaterEqual(len(cfg["presets"]), 5)
        self.assertEqual({p["in_benchmark"] for p in cfg["presets"]}, {False})  # в маленьком наборе их нет
        code, _, body = h.get("/api/truth?" + urllib.parse.urlencode({"q": Q_B}))
        t = json.loads(body)
        self.assertEqual((code, t["available"], t["truth"]["dataset_name"], len(t["truth"]["snippets"])), (200, True, "PrivacyQA", 2))
        self.assertIsNone(json.loads(h.get("/api/truth?q=unknown%20question")[2])["truth"])
        self.assertEqual(h.get("/api/truth?q=")[0], 400)
        code, _, body = h.get("/api/numbers")
        d = json.loads(body)
        self.assertEqual((code, d["ok"]), (200, True))
        self.assertEqual(d["numbers"]["schema"], board.SCHEMA)
        self.assertEqual(d["view"]["caption"], d["numbers"]["caption"])

    def test_broken_numbers_file(self):
        bad = os.path.join(self.tmp, "numbers.json")
        with open(bad, "w") as f:
            f.write("{not json")
        self.app = self.make_app(self.app.entry, numbers=bad)
        code, _, body = self.http().get("/api/numbers")
        self.assertEqual(code, 500)
        self.assertEqual(json.loads(body)["error"], "numbers.json is not readable")


class StaticTextTest(unittest.TestCase):
    """Доска: в разметке и строках скрипта нет ни одной цифры — все числа приходят из numbers.json."""

    class _Visible(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.skip, self.text = 0, []

        def handle_starttag(self, tag, attrs):
            self.skip += tag in ("script", "style")

        def handle_endtag(self, tag):
            self.skip -= tag in ("script", "style")

        def handle_data(self, data):
            if not self.skip:
                self.text.append(data)

    def test_board_has_no_numbers_of_its_own(self):
        with open(os.path.join(STATIC, "board.html"), encoding="utf-8") as f:
            page = f.read()
        p = self._Visible()
        p.feed(page)
        self.assertFalse(re.search(r"\d", "".join(p.text)), "цифры в тексте доски")
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        script = re.sub(r"//[^\n]*", "", script)  # комментарии не в кадре
        for lit in re.findall(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`", script):
            self.assertFalse(re.search(r"\d", lit), f"цифра в строке скрипта доски: {lit}")

    def test_pages_are_english_on_screen(self):
        for name in ("index.html", "board.html"):
            with open(os.path.join(STATIC, name), encoding="utf-8") as f:
                page = f.read()
            p = self._Visible()
            p.feed(page)
            self.assertFalse(re.search("[а-яё]", "".join(p.text), re.I), name)
            script = re.sub(r"//[^\n]*", "", re.search(r"<script>(.*?)</script>", page, re.S).group(1))
            self.assertFalse(re.search("[а-яё]", script, re.I), f"{name}: русский в строках скрипта")


def load_presets() -> list[dict]:
    with open(os.path.join(SHOWCASE, "presets.json"), encoding="utf-8") as f:
        return json.load(f)["presets"]


class PresetsTest(unittest.TestCase):
    def test_presets_shape(self):
        presets = load_presets()
        self.assertTrue(5 <= len(presets) <= 8)
        self.assertEqual(len({p["id"] for p in presets}), len(presets))
        bench = [p for p in presets if p.get("in_benchmark") is not False]
        self.assertEqual({p["dataset"] for p in bench}, set(lb.DATASETS))  # все четыре набора
        for p in presets:
            self.assertTrue(p["label"] and p["query"].startswith("Consider"), p["id"])
            self.assertTrue(1 <= p["k"] <= server.LIMITS["search_k_max"])

    def test_preset_with_contract_outside_the_base(self):
        presets = load_presets()
        self.assertEqual([p["id"] for p in presets[:7]],  # прежние семь на своих местах
                         ["supply-term", "merger-knowledge", "nda-archival-copy", "privacy-delete",
                          "merger-specific-performance", "nda-survival", "service-term"])
        outside = [p for p in presets if p.get("in_benchmark") is False]
        self.assertEqual([p["id"] for p in outside], ["lease-not-in-base"])
        p = outside[0]
        self.assertIs(p, presets[-1])  # клавиша 8 и ?preset=8
        self.assertEqual(p["query"], "Consider the Lease Agreement between Nobody Holdings and Nowhere Estates; "
                                     "What is the monthly rent?")
        self.assertIn("not in the base", p["label"])
        self.assertEqual(routing.split_question(p["query"]),
                         ("Consider the Lease Agreement between Nobody Holdings and Nowhere Estates",
                          "What is the monthly rent?"))  # ссылка на договор идёт в гейт

    def test_server_does_not_expect_truth_for_it(self):
        for data in (None, self._small_data()):  # с эталоном и без него
            app = server.Showcase(server.Entry(url="http://127.0.0.1:1"), Benchmark(data), "",
                                  os.path.join(SHOWCASE, "presets.json"), log=lambda m: None)
            got = {p["id"]: (p["in_benchmark"], p["expect_truth"]) for p in app.presets()}
            self.assertEqual(got.pop("lease-not-in-base"), (False, False))
            self.assertEqual(set(got.values()), {(None if data is None else False, True)})

    def _small_data(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        make_dataset(tmp.name)
        return tmp.name

    def test_presets_are_mini_questions_with_short_annotation(self):
        data = os.path.join(ROOT, "data", "legalbenchrag")
        bench = Benchmark(data)
        if not bench.available():
            self.skipTest("нет data/legalbenchrag (набор в git не идёт)")
        for p in load_presets():
            t = bench.lookup(p["query"])
            if p.get("in_benchmark") is False:  # договор не из базы: эталона нет
                self.assertIsNone(t, p["id"])
                continue
            self.assertIsNotNone(t, p["id"])
            self.assertTrue(t["in_mini"], p["id"])
            self.assertEqual(t["dataset"], p["dataset"])
            self.assertLessEqual(t["total_chars"], 450, p["id"])  # разметка читается с экрана
            for sn in t["snippets"]:
                self.assertEqual(bench.doc(sn["file"])[sn["span"][0]:sn["span"][1]], sn["text"])


# --- TLS по УЦ стенда ---------------------------------------------------------------------------------
def make_pki(d: str) -> None:
    """УЦ и сертификат входа по рецепту deploy/observer/install.sh, плюс чужой УЦ и сертификат без IP."""
    openssl = shutil.which("openssl")

    def run(*args: str) -> None:
        subprocess.run([openssl, *args], cwd=d, check=True, capture_output=True, timeout=60)

    for ca in ("ca", "other"):
        run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{ca}.key")
        run("req", "-x509", "-new", "-key", f"{ca}.key", "-sha256", "-days", "2", "-subj", f"/CN={ca} test CA",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-out", f"{ca}.pem")
    for name, san in (("entry", "DNS:stand.local,IP:127.0.0.1"), ("noip", "DNS:stand.local")):
        run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{name}.key")
        run("req", "-new", "-key", f"{name}.key", "-subj", "/CN=stand.local", "-out", f"{name}.csr")
        with open(os.path.join(d, f"{name}.ext"), "w") as f:
            f.write(f"subjectAltName={san}\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\n"
                    "basicConstraints=CA:FALSE\n")
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
            "-sha256", "-extfile", f"{name}.ext", "-out", f"{name}.crt")


@unittest.skipUnless(shutil.which("openssl"), "нет openssl для одноразовых сертификатов")
class TLSTest(ShowcaseCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._pki = tempfile.TemporaryDirectory()
        try:
            make_pki(cls._pki.name)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            cls._pki.cleanup()
            raise unittest.SkipTest(f"openssl не выпустил сертификаты: {type(e).__name__}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._pki.cleanup()

    def tls_entry(self, cert: str) -> FakeServer:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(self._pki.name, f"{cert}.crt"), os.path.join(self._pki.name, f"{cert}.key"))
        fake = FakeServer(entry_handler(self.hits), ssl_context=ctx)
        fake.srv.handle_error = lambda *a: None  # отвергнутое рукопожатие — ожидаемо, без трассы в вывод
        self.addCleanup(fake.close)
        return fake

    def ask_via(self, fake: FakeServer, ca: str | None) -> tuple[int, dict]:
        ca_path = os.path.join(self._pki.name, f"{ca}.pem") if ca else None
        app = self.make_app(server.Entry(url=fake.url, ca=ca_path, key_file=self.key_file, timeout_search=5.0))
        return app.ask("search", Q_A, "2")

    def test_stand_ca_accepted(self):
        fake = self.tls_entry("entry")
        code, d = self.ask_via(fake, "ca")
        self.assertEqual(code, 200, d)
        self.assertEqual(fake.requests[0][2].get("X-API-Key"), KEY)

    def test_other_ca_rejected(self):
        fake = self.tls_entry("entry")
        code, d = self.ask_via(fake, "other")
        self.assertEqual((code, d["kind"]), (502, "tls"))
        self.assertEqual(fake.requests, [])  # ключ не ушёл на непроверенный вход

    def test_address_must_match_certificate(self):
        fake = self.tls_entry("noip")
        code, d = self.ask_via(fake, "ca")
        self.assertEqual((code, d["kind"]), (502, "tls"))
        self.assertEqual(fake.requests, [])

    def test_missing_ca_is_config_error(self):
        fake = self.tls_entry("entry")
        code, d = self.ask_via(fake, "nonexistent")
        self.assertEqual((code, d["kind"]), (500, "config"))
        self.assertEqual(fake.requests, [])


# --- заглушка входа --------------------------------------------------------------------------------
class StubTest(unittest.TestCase):
    TEXT = "This Agreement is governed by the laws of the State of Delaware without regard to conflicts of law."

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = os.path.join(tmp.name, "naive500_title")
        os.makedirs(d)
        points = [{"title": "Acme", "section": "", "file": FILE_A, "start": 0, "end": 99, "text": self.TEXT},
                  {"title": "Acme", "section": "2. Law", "file": FILE_A, "start": 99, "end": 120, "text": "Second chunk text."},
                  {"title": "Beta", "section": "", "file": FILE_B, "start": 0, "end": 10, "text": "Third one."}]
        with open(os.path.join(d, "points.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(p) + "\n" for p in points)
        with open(os.path.join(d, "vectors.f32"), "wb") as f:
            array("f", [1, 0, 0, 0, 0.8, 0.6, 0, 0, 0, 0, 1, 0]).tofile(f)
        with open(os.path.join(tmp.name, "queries.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"query": Q_A}) + "\n" + json.dumps({"query": Q_B}) + "\n")
        with open(os.path.join(tmp.name, "query_vectors.f32"), "wb") as f:
            array("f", [2, 0, 0, 0, 0, 0, 3, 0]).tofile(f)
        self.index = stub.StubIndex(tmp.name)

    def entry(self, mode: str = "answered") -> stub.StubEntry:
        e = stub.StubEntry(self.index, KEY, answer_mode=mode)
        e.start()
        self.addCleanup(e.close)
        return e

    def test_exact_search_order(self):
        res = self.index.search(" " + Q_A, 2)
        self.assertEqual([(r["span"], r["score"]) for r in res], [([0, 99], 1.0), ([99, 120], 0.8)])
        self.assertEqual(res[1]["section"], "2. Law")
        self.assertIsNone(res[0]["section"])
        self.assertEqual(self.index.search(Q_B, 1)[0]["file"], FILE_B)
        self.assertIsNone(self.index.search("unknown question", 3))

    def test_answer_modes_use_the_service_check(self):
        e = stub.StubEntry(self.index, KEY)
        e.close()
        self.assertEqual(e.answer(Q_A, 2)[1]["status"], "answered")
        self.assertTrue(e.answer(Q_A, 2)[1]["citations"][0]["verified"])
        for mode, status in (("refused", "refused"), ("unsupported", "unsupported")):
            e.answer_mode = mode
            self.assertEqual(e.answer(Q_A, 2)[1]["status"], status)
        e.answer_mode = "busy"
        self.assertEqual(e.answer(Q_A, 2)[0], 429)
        self.assertEqual(e.search("unknown question", 2)[0], 503)
        with self.assertRaises(ValueError):
            stub.StubEntry(self.index, KEY, answer_mode="loud")

    def test_showcase_through_the_stub(self):
        e = self.entry()
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        make_dataset(data.name)
        app = server.Showcase(server.Entry(url=e.url, key=("X-API-Key", KEY)), Benchmark(data.name),
                              os.path.join(SHOWCASE, "numbers.json"), os.path.join(SHOWCASE, "presets.json"),
                              stub=True, log=lambda m: None)
        code, d = app.ask("search", Q_A, "2")
        self.assertEqual(code, 200, d)
        self.assertTrue(d["stub"] and d["result"]["stub"])
        self.assertEqual(d["result"]["served_by"]["node"], "stub")
        bad = server.Showcase(server.Entry(url=e.url, key=("X-API-Key", "wrong")), Benchmark(None), "", "", log=lambda m: None)
        self.assertEqual(bad.ask("search", Q_A, "2")[1]["kind"], "key")


# --- заглушка: выбор договора и гейт той же логикой, что сервис -------------------------------------
QA = f"{tr.REF_A}; {tr.ASK_A}"     # договор Acme есть в базе
QX = f"{tr.REF_OUT}; {tr.ASK_A}"   # договора Zeta в базе нет


def write_stub_cache(root: str, mq, questions: list[str]) -> None:
    """Кэш замера для заглушки из стенда в памяти тестов сервиса (tests/test_routing.py): те же куски,
    векторы договоров и вопросов, поэтому заглушка и сервис должны ответить одинаково. Векторы вопросов
    кладутся только для questions, остальные заглушка берёт у Ollama."""
    chunks = list(mq.cols["contracts_rf2"]["points"].values())
    d = os.path.join(root, "naive500_title")
    os.makedirs(d)
    with open(os.path.join(d, "points.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(c["payload"]) + "\n" for c in chunks)
    with open(os.path.join(d, "vectors.f32"), "wb") as f:
        array("f", [x for c in chunks for x in c["vector"]["dense"]]).tofile(f)
    with open(os.path.join(root, "queries.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps({"query": q}) + "\n" for q in questions)
    with open(os.path.join(root, "query_vectors.f32"), "wb") as f:
        array("f", [x for q in questions for x in tr.VECTORS[q]]).tofile(f)
    docs = list(mq.cols["contracts_rf2_docs"]["points"].items())
    dd = os.path.join(root, "naive500_title_docs")
    os.makedirs(dd)
    with open(os.path.join(dd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"model": "bge-m3", "dim": 4, "docs": len(docs), "source": "naive500_title"}, f)
    with open(os.path.join(dd, "points.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps({"id": i, **p["payload"]}) + "\n" for i, p in docs)
    for name in ("route", "gate"):
        with open(os.path.join(dd, f"{name}.f32"), "wb") as f:
            array("f", [x for _, p in docs for x in p["vector"][name]]).tofile(f)


class StubRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        write_stub_cache(self.root, tr.memory_stand(), [QA, "governing law"])  # QX нет в кэше замера
        self.index = stub.StubIndex(self.root)
        self.ollama = FakeServer(tr.ollama_vectors)
        self.addCleanup(self.ollama.close)
        env = mock.patch.dict(os.environ, {"GATE_MIN": ""})  # порог как у сервиса, без перекрытия
        env.start()
        self.addCleanup(env.stop)

    def entry(self, ollama_url: str | None = None, mode: str = "answered") -> stub.StubEntry:
        router = stub.open_router(self.index, self.root, ollama_url or self.ollama.url,
                                  os.path.join(self.root, "embeddings"))
        e = stub.StubEntry(self.index, KEY, answer_mode=mode, router=router)
        self.addCleanup(e.close)
        return e

    def test_contract_chosen_and_searched_inside(self):
        code, body = self.entry().search(QA, 3)
        self.assertEqual(code, 200, body)
        route = body["route"]
        self.assertEqual((route["used"], route["reference"], route["question"]), (True, tr.REF_A, tr.ASK_A))
        self.assertEqual((route["document"]["file"], route["document"]["title"]), ("cuad/acme.txt", "Acme License"))
        self.assertEqual(route["gate"], {"score": 0.9, "threshold": routing.gate_min("bge-m3"), "passed": True,
                                         "closest": "Acme License"})
        self.assertEqual(route["gate"]["threshold"], routing.gate_min("bge-m3"))
        self.assertNotIn("not_in_base", body)
        self.assertEqual({h["file"] for h in body["results"]}, {"cuad/acme.txt"})
        self.assertEqual([h["span"][0] for h in body["results"][:2]], [1000, 500])  # пункт, а не преамбула
        self.assertTrue(body["stub"])
        # весь вопрос из кэша замера, к Ollama одна поездка за ссылкой и самим вопросом
        self.assertEqual([b["input"] for b in self.ollama.json_bodies()], [[tr.REF_A, tr.ASK_A]])

    def test_next_contract_fills_when_first_is_short(self):
        code, body = self.entry().search(QA, 5)
        self.assertEqual([h["file"] for h in body["results"]], ["cuad/acme.txt"] * 4 + ["cuad/beta.txt"])

    def test_contract_not_in_base(self):
        e = self.entry()
        code, body = e.search(QX, 3)
        self.assertEqual(code, 200, body)
        self.assertEqual((body["not_in_base"], body["message"], body["results"]),
                         (True, "this contract is not in the database", []))
        self.assertIsNone(body["route"]["document"])
        self.assertFalse(body["route"]["gate"]["passed"])
        self.assertLess(body["route"]["gate"]["score"], body["route"]["gate"]["threshold"])
        code, ans = e.answer(QX, 3)
        self.assertEqual(code, 200, ans)
        self.assertEqual((ans["status"], ans["answer"], ans["citations"], ans["sources"], ans["not_in_base"]),
                         ("refused", "This contract is not in the database.", [], [], True))
        self.assertNotIn("generate", ans["timing_ms"])  # модель не зовётся
        self.assertEqual(ans["route"], body["route"])
        # вопроса нет в кэше замера: три вектора одной поездкой, второй раз всё из кэша, /api/chat ни разу
        self.assertEqual([b["input"] for b in self.ollama.json_bodies()], [[QX, tr.REF_OUT, tr.ASK_A]])
        self.assertEqual(self.ollama.paths(), ["/api/embed"])

    def test_gate_refusal_comes_before_model_failures(self):
        for mode in ("busy", "invalid"):
            code, ans = self.entry(mode=mode).answer(QX, 3)
            self.assertEqual((code, ans["status"]), (200, "refused"), mode)  # до модели дело не доходит

    def test_question_without_semicolon_has_no_route(self):
        code, body = self.entry().search("governing law", 3)
        self.assertEqual(code, 200, body)
        self.assertNotIn("route", body)
        self.assertEqual(len(body["results"]), 3)
        self.assertEqual(self.ollama.requests, [])  # вектор вопроса из кэша замера

    def test_gate_off_still_chooses_a_contract(self):
        with mock.patch.dict(os.environ, {"GATE_MIN": "off"}):
            code, body = self.entry().search(QX, 3)
        self.assertIsNone(body["route"]["gate"])
        self.assertTrue(body["results"])
        self.assertNotIn("not_in_base", body)

    def test_without_ollama_works_as_before(self):
        e = self.entry(ollama_url=closed_port_url())
        code, body = e.search(QA, 3)
        self.assertEqual(code, 200, body)
        self.assertNotIn("route", body)  # прежний поиск по всем кускам
        self.assertEqual(len(body["results"]), 3)
        self.assertEqual(e.search(QX, 3)[0], 503)  # вопроса нет в кэше, посчитать его нечем
        self.assertEqual(e.answer(QX, 3)[0], 503)

    def test_router_needs_documents_of_the_same_model(self):
        emb = os.path.join(self.root, "embeddings")
        with self.assertRaises(OSError):
            stub.open_router(self.index, self.root, self.ollama.url, emb, strategy="naive500")
        meta = os.path.join(self.root, "naive500_title_docs", "meta.json")
        for bad in ({"model": "snowflake-arctic-embed2", "dim": 4}, {"model": "bge-m3", "dim": 8}):
            with open(meta, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            with self.assertRaises(ValueError):
                stub.open_router(self.index, self.root, self.ollama.url, emb)

    def test_showcase_passes_the_stub_route(self):
        e = self.entry()
        e.start()
        app = server.Showcase(server.Entry(url=e.url, key=("X-API-Key", KEY)), Benchmark(None),
                              os.path.join(SHOWCASE, "numbers.json"), os.path.join(SHOWCASE, "presets.json"),
                              stub=True, log=lambda m: None)
        code, d = app.ask("search", QA, "3")
        self.assertEqual(code, 200, d)
        self.assertEqual(d["result"]["route"]["document"]["title"], "Acme License")
        code, d = app.ask("answer", QX, "3")
        self.assertEqual(code, 200, d)
        self.assertTrue(d["stub"] and d["result"]["not_in_base"])
        self.assertEqual(d["result"]["answer"], "This contract is not in the database.")

    @unittest.skipIf(tr.TestClient is None, "нужны fastapi и httpx")
    def test_same_answers_as_the_service(self):
        api = tr.api
        self.assertEqual((stub.ROUTE_DOCS, stub.ROUTE_MIX, stub.ROUTE_NEIGHBOR, stub.NOT_IN_BASE),
                         (api.ROUTE_DOCS, api.ROUTE_MIX, api.ROUTE_NEIGHBOR, api.NOT_IN_BASE))
        qdrant = FakeServer(tr.memory_stand())
        self.addCleanup(qdrant.close)
        key_file = os.path.join(self.root, "qdrant-key")
        with open(key_file, "w") as f:
            f.write(tr.KEY)
        p = mock.patch.multiple(api, QDRANT_URL=qdrant.url, OLLAMA_URL=self.ollama.url, KEY_FILE=key_file,
                                COLLECTION="contracts_rf2", SEARCH_MODE="dense", SEARCH_ALIAS="", ROUTE=True,
                                QDRANT_LOCAL_URL="", QDRANT_CONNECT_TIMEOUT=0.3, QDRANT_READ_TIMEOUT=0.3,
                                QDRANT_ATTEMPTS=2, _DOCS_MISSING={})
        p.start()
        self.addCleanup(p.stop)
        client = tr.TestClient(api.app)
        e = self.entry()
        for q, k in ((QA, 3), (QA, 5), (QX, 3), ("governing law", 3)):
            r = client.get("/search", params={"q": q, "k": k})
            self.assertEqual(r.status_code, 200, r.text)
            want, (code, got) = r.json(), e.search(q, k)
            self.assertEqual(code, 200, got)
            for key in ("not_in_base", "message"):
                self.assertEqual(got.get(key), want.get(key), (q, key))
            self.assertEqual(json.dumps(got.get("route"), sort_keys=True), json.dumps(want.get("route"), sort_keys=True), q)
            self.assertEqual([(h["file"], h["span"], h["text"], h["section"]) for h in got["results"]],
                             [(h["file"], h["span"], h["text"], h["section"]) for h in want["results"]], (q, k))
            for a, b in zip(got["results"], want["results"]):
                self.assertAlmostEqual(a["score"], b["score"], delta=1e-3)
        want = client.get("/answer", params={"q": QX, "k": 3}).json()
        got = e.answer(QX, 3)[1]
        for key in ("status", "answer", "citations", "sources", "not_in_base", "route"):
            self.assertEqual(got[key], want[key], key)


if __name__ == "__main__":
    unittest.main()
