"""Проверочная часть сцены смены модели (deploy/scenes/model_switch.py): wait, hold, quality, compare,
summary против подставного входа на 127.0.0.1. Оркестровка на shell проверяется только на стенде."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse

from eval import legalbench as lb
from tests.fakes import FakeServer
from tests.test_data_pipeline import write_dataset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("model_switch", os.path.join(ROOT, "deploy", "scenes", "model_switch.py"))
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

API_KEY = "entry-key"


class Pods:
    """Вход с тремя подами по кругу; у каждого своя пара и состояние проверки, сцена их меняет."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.n = 0
        self.state = {p: ("contracts_rf2", "bge-m3", {"status": "verified", "alias": "contracts",
                                                      "target": "contracts_rf2", "cosine": 1.0})
                      for p in ("search-a", "search-b", "search-c")}
        self.results = lambda q, k: []
        self.fail_every = 0
        self.sticky = None  # под, на который вход шлёт все пробы (leastconn под нагрузкой, стенд 19.11)

    def handler(self, method, path, headers, body):
        if headers.get("X-API-Key") != API_KEY:
            return 401, {"error": "key"}
        u = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(u.query)
        with self.lock:
            self.n += 1
            n = self.n
            pod = self.sticky or sorted(self.state)[n % 3]
            coll, model, check = self.state[pod]
        if self.fail_every and n % self.fail_every == 0:
            return 503, {"error": "busy"}
        return 200, {"query": qs["q"][0], "model": model, "collection": coll, "model_check": dict(check),
                     "served_by": {"node": "machine-1", "pod": pod}, "results": self.results(qs["q"][0], int(qs["k"][0]))}


class SceneToolTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.pods = Pods()
        self.srv = FakeServer(self.pods.handler)
        self.addCleanup(self.srv.close)
        self.key = os.path.join(self.dir, "key")
        with open(self.key, "w") as f:
            f.write(API_KEY + "\n")

    def run_tool(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ms.main(["--url", self.srv.url, "--api-key-file", self.key, "--rate", "500", *args])
        return code, out.getvalue()

    def out(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def read(self, name: str) -> dict:
        with open(self.out(name)) as f:
            return json.load(f)

    def test_pair(self):
        code, text = self.run_tool("pair", "--out", self.out("pair.json"))
        self.assertEqual(code, 0)
        self.assertIn("users are served by contracts_rf2 (bge-m3) · alias contracts -> contracts_rf2 · "
                      "fingerprint verified, cosine 1.000 · pods 3", text)
        self.assertEqual(sorted(self.read("pair.json")["pods"]), ["search-a", "search-b", "search-c"])

    def test_wait_until_every_pod_switched(self):
        new = ("contracts_rf2_arctic", "snowflake-arctic-embed2",
               {"status": "verified", "alias": "contracts", "target": "contracts_rf2_arctic", "cosine": 1.0})

        def switch_later():
            for pod in sorted(self.pods.state):
                threading.Event().wait(0.05)
                with self.pods.lock:
                    self.pods.state[pod] = new
        threading.Thread(target=switch_later, daemon=True).start()
        code, text = self.run_tool("wait", "--collection", "contracts_rf2_arctic", "--pods", "3", "--timeout", "10",
                                   "--out", self.out("wait.json"))
        self.assertEqual(code, 0, text)
        self.assertIn("all 3 pods answer contracts_rf2_arctic (snowflake-arctic-embed2)", text)
        res = self.read("wait.json")
        self.assertTrue(res["ok"])
        self.assertEqual({p["collection"] for p in res["pods"].values()}, {"contracts_rf2_arctic"})

    def test_wait_timeout_and_status(self):
        code, text = self.run_tool("wait", "--collection", "contracts_rf2_arctic", "--timeout", "0.3",
                                   "--out", self.out("wait.json"))
        self.assertEqual(code, 1)
        self.assertIn("timeout", text)
        self.assertFalse(self.read("wait.json")["ok"])
        # та же коллекция, но не то состояние проверки: тоже не готово
        code, _ = self.run_tool("wait", "--collection", "contracts_rf2", "--status", "refused", "--timeout", "0.3")
        self.assertEqual(code, 1)
        code, _ = self.run_tool("wait", "--collection", "contracts_rf2", "--status", "verified", "--timeout", "5")
        self.assertEqual(code, 0)

    def refuse_all(self) -> None:
        for pod in self.pods.state:
            self.pods.state[pod] = ("contracts_rf2", "bge-m3", {
                "status": "refused", "alias": "contracts", "target": "contracts_mislabeled", "target_model": "bge-m3",
                "reason": "fingerprint mismatch: cosine 0.031 < 0.99", "verified": True, "cosine": 1.0})

    def write_states(self, collection: str, model: str, check: dict) -> str:
        """Файл, который печатает --pods-cmd: строка «machine-N {/readyz}» на машину."""
        path = self.out("states.txt")
        with open(path, "w") as f:
            for m in ("machine-1", "machine-2", "machine-3"):
                f.write(m + " " + json.dumps({"ready": True, "collection": collection, "model": model,
                                              "model_check": check}) + "\n")
            f.write("machine-x not json\n")
        return "cat " + path

    def test_wait_by_pod_states_when_entry_sticks_to_one_pod(self):
        # 19.11: под нагрузкой все пробы через вход шли на один-два пода, ожидание «все три» не кончалось
        for p in self.pods.state:
            self.pods.state[p] = ("contracts_rf2_arctic", "snowflake-arctic-embed2", {"status": "verified"})
        self.pods.sticky = "search-a"
        cmd = self.write_states("contracts_rf2_arctic", "snowflake-arctic-embed2", {"status": "verified"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ms.main(["--url", self.srv.url, "--api-key-file", self.key, "--rate", "500", "--pods-cmd", cmd,
                            "wait", "--collection", "contracts_rf2_arctic", "--pods", "3", "--timeout", "5",
                            "--out", self.out("wait.json")])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("all 3 pods answer contracts_rf2_arctic", out.getvalue())
        self.assertEqual(sorted(self.read("wait.json")["pods"]), ["machine-1", "machine-2", "machine-3"])

    def test_hold_by_pod_states_when_entry_sticks_to_one_pod(self):
        self.refuse_all()
        self.pods.sticky = "search-a"
        check = {"status": "refused", "target": "contracts_mislabeled", "target_model": "bge-m3",
                 "reason": "fingerprint mismatch: cosine 0.031 < 0.99"}
        cmd = self.write_states("contracts_rf2", "bge-m3", check)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ms.main(["--url", self.srv.url, "--api-key-file", self.key, "--rate", "500", "--pods-cmd", cmd,
                            "hold", "--seconds", "0.3", "--collection", "contracts_rf2",
                            "--refused", "contracts_mislabeled", "--out", self.out("hold.json")])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("3 pods refused contracts_mislabeled", out.getvalue())

    def test_scene_invocations_parse(self):
        # каждый вызов ms … из model-switch.sh принимает разбор аргументов (19.11: --rate после подкоманды)
        import re
        import shlex
        with open(os.path.join(ROOT, "deploy", "scenes", "model-switch.sh"), encoding="utf-8") as f:
            text = f.read().replace("\\\n", " ")
        calls = [m.group(1) for m in re.finditer(r"(?:^|\s)ms ((?:--|pair|wait|hold|quality|compare)[^|\n]*)", text, re.M)]
        self.assertGreaterEqual(len(calls), 8, calls)
        for call in calls:
            args = [re.sub(r"\$\{?\w+\}?", "x", a) for a in shlex.split(call.split(" || ")[0])]
            args = ["8" if a == "x" and i and args[i - 1] in ("--k", "--per-dataset", "--rate", "--seconds", "--pods")
                    else a for i, a in enumerate(args)]
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    ms.parser().parse_args(["--url", "https://x", *args])
                except SystemExit:
                    self.fail(f"model_switch.py не принимает вызов из model-switch.sh: ms {call}")

    def test_hold_trap_refused(self):
        self.refuse_all()
        code, text = self.run_tool("hold", "--seconds", "0.3", "--collection", "contracts_rf2",
                                   "--refused", "contracts_mislabeled", "--out", self.out("hold.json"))
        self.assertEqual(code, 0, text)
        self.assertIn("0 from contracts_mislabeled, errors 0", text)
        self.assertIn("3 pods refused contracts_mislabeled: fingerprint mismatch: cosine 0.031 < 0.99", text)
        res = self.read("hold.json")
        self.assertEqual((res["ok"], res["target_model"], len(res["pods_refused"])), (True, "bge-m3", 3))

    def test_hold_fails_if_trap_answers_or_errors(self):
        self.refuse_all()
        self.pods.state["search-b"] = ("contracts_mislabeled", "bge-m3", {"status": "verified"})
        code, _ = self.run_tool("hold", "--seconds", "0.3", "--collection", "contracts_rf2",
                                "--refused", "contracts_mislabeled", "--out", self.out("hold.json"))
        self.assertEqual(code, 1)
        self.assertGreater(self.read("hold.json")["from_target"], 0)
        self.refuse_all()
        self.pods.fail_every = 4
        code, _ = self.run_tool("hold", "--seconds", "0.3", "--collection", "contracts_rf2",
                                "--refused", "contracts_mislabeled")
        self.assertEqual(code, 1)

    def test_quality_compare_and_summary(self):
        data = os.path.join(self.dir, "data")
        write_dataset(data)
        gt = {t["query"]: t["snippets"][0] for name in lb.DATASETS for t in lb.load_tests(data, name)}

        def exact(q, k):  # privacy_qa и contractnli находятся точно, остальные мимо
            s = gt[q]
            if s["file_path"].startswith(("privacy_qa", "contractnli")):
                return [{"file": s["file_path"], "span": s["span"], "text": "x"}]
            return [{"file": "cuad/Other.txt", "span": [0, 10], "text": "x"}]
        self.pods.results = exact
        code, text = self.run_tool("quality", "--data", data, "--k", "8", "--per-dataset", "2", "--expect", "contracts_rf2",
                                   "--label", "before: ", "--out", self.out("quality-before.json"))
        self.assertEqual(code, 0, text)
        self.assertIn("before: recall@8 50.0 % on 8 LegalBench-RAG-mini questions (2 per dataset), live through the entry", text)
        before = self.read("quality-before.json")
        self.assertEqual(before["recall_by_dataset"], {"privacy_qa": 100.0, "contractnli": 100.0, "maud": 0.0, "cuad": 0.0})

        self.pods.results = lambda q, k: [{"file": gt[q]["file_path"], "span": gt[q]["span"], "text": "x"}]
        code, text = self.run_tool("quality", "--data", data, "--per-dataset", "2", "--expect", "contracts_rf2_arctic",
                                   "--out", self.out("quality-after.json"))
        self.assertEqual(code, 1)  # ответы не из той коллекции: замер не засчитывается
        self.assertIn("warning: 8 answers did not come from contracts_rf2_arctic", text)
        code, text = self.run_tool("quality", "--data", data, "--per-dataset", "2", "--out", self.out("quality-after.json"))
        self.assertEqual(code, 0)

        code, text = self.run_tool("compare", self.out("quality-before.json"), self.out("quality-after.json"),
                                   "--out", self.out("quality-compare.json"))
        self.assertEqual(code, 0)
        self.assertIn("recall@8: 50.0 % -> 100.0 % (+50.0 points, 95 % CI", text)
        self.assertIn("paired bootstrap over the same 8 questions", text)
        lo, hi = self.read("quality-compare.json")["ci95"]
        self.assertTrue(lo <= 50.0 <= hi)

        for name, doc in (("wait-switch.json", {"ok": True, "collection": "contracts_rf2_arctic", "seconds": 1.4}),
                          ("wait-rollback.json", {"ok": True, "collection": "contracts_rf2", "seconds": 1.1}),
                          ("wait-cleanup.json", {"ok": True, "collection": "contracts_rf2", "seconds": 0.9}),
                          ("hold-trap.json", {"ok": True, "pods_refused": ["a", "b", "c"], "from_target": 0, "errors": 0,
                                              "reason": "fingerprint mismatch: cosine 0.031 < 0.99"}),
                          ("load.json", {"totals": {"count": 2400, "retries": 3, "with_retry": {"errors": 0}}})):
            with open(self.out(name), "w") as f:
                json.dump(doc, f)
        code, text = self.run_tool("summary", self.dir)
        self.assertEqual(code, 0, text)
        self.assertIn("users: 2400 requests under load, errors seen 0, retries 3", text)
        self.assertIn("switch: all pods on contracts_rf2_arctic in 1.4 s, no restart", text)
        self.assertIn("trap: 3 pods refused the mislabeled collection (fingerprint mismatch: cosine 0.031 < 0.99)", text)
        self.assertIn("result: passed", text)
        with open(self.out("load.json"), "w") as f:
            json.dump({"totals": {"count": 2400, "retries": 3, "with_retry": {"errors": 2}}}, f)
        code, text = self.run_tool("summary", self.dir)
        self.assertEqual(code, 1)
        self.assertIn("result: FAILED", text)

    def test_sample_is_fixed(self):
        data = os.path.join(self.dir, "data")
        write_dataset(data)
        a, b = ms.sample(data, 1), ms.sample(data, 1)
        self.assertEqual(a, b)
        self.assertEqual([q["dataset"] for q in a], list(lb.DATASETS))


if __name__ == "__main__":
    unittest.main()
