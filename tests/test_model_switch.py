"""Смена модели на ходу целиком, без стенда: настоящие загрузчик (eval/qdrant_load.py), реестр
(deploy/qdrant/registry.py) и сервис поиска (app/api.py) против Qdrant в памяти и подставной Ollama,
у которой вектор зависит от модели. Сервис читает только ключом на чтение, как на стенде.

    python3 -I -B -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from app import api, binding
from eval import fingerprint
from tests.fakes import ADMIN_KEY, KEY, FakeServer, MemoryQdrant, closed_port_url, model_ollama_handler, ollama_handler
from tests.test_registry import load, make_set, registry

try:
    from fastapi.testclient import TestClient
except ImportError:  # без httpx клиента нет; тесты ручек пропускаются
    TestClient = None

ARCTIC = "snowflake-arctic-embed2"


class Stand(unittest.TestCase):
    """Qdrant с тремя коллекциями и реестром, как после load.sh: contracts_rf2 (bge-m3),
    contracts_rf2_arctic (arctic) и ловушка contracts_mislabeled (векторы arctic, подписана bge-m3)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.mem = MemoryQdrant()
        self.qdrant = FakeServer(self.mem)
        self.addCleanup(self.qdrant.close)
        self.ollama = FakeServer(model_ollama_handler(allowed=("bge-m3", ARCTIC)))
        self.addCleanup(self.ollama.close)
        rw, ro = os.path.join(self.root, "rw"), os.path.join(self.root, "ro")
        for path, key in ((rw, ADMIN_KEY), (ro, KEY)):
            with open(path, "w") as f:
                f.write(key)
        bge, arctic = make_set(self.root, "stage1", "bge-m3"), make_set(self.root, "stage1_arctic", ARCTIC)
        self.admin = registry.Qdrant(self.qdrant.url, ADMIN_KEY)
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet:
            for src, name, extra, label in ((bge, "contracts_rf2", (), None), (arctic, "contracts_rf2_arctic", (), None),
                                            (arctic, "contracts_mislabeled", ("--limit", "2"), "bge-m3")):
                load(self.qdrant.url, rw, src, name, *extra)
                registry.register(self.admin, fingerprint.entry(src, label), name)
            registry.switch_alias(self.admin, "contracts", "contracts_rf2")
        self.log = io.StringIO()
        redirect = contextlib.redirect_stderr(self.log)  # журнал сервиса
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.patch(QDRANT_URL=self.qdrant.url, QDRANT_LOCAL_URL="", OLLAMA_URL=self.ollama.url, KEY_FILE=ro,
                   COLLECTION="contracts_rf2", MODEL="bge-m3", SEARCH_ALIAS="contracts", SEARCH_MODE="dense",
                   REGISTRY="embedding_registry", BACKGROUND=False, BINDING=None, NODE="machine-1", POD="search-a",
                   QDRANT_CONNECT_TIMEOUT=0.3, QDRANT_READ_TIMEOUT=1.0, QDRANT_ATTEMPTS=2, VERIFY_TIMEOUT=5.0)

    def patch(self, **values) -> None:
        p = mock.patch.multiple(api, **values)
        p.start()
        self.addCleanup(p.stop)

    def alias(self, target: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            registry.switch_alias(self.admin, "contracts", target)

    def refresh(self) -> binding.State:
        return api._binding().refresh()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.getvalue().splitlines() if line.startswith("{")]


class VerifyTest(Stand):
    def test_registered_collections(self):
        self.assertEqual(api._verify("contracts_rf2").model, "bge-m3")
        pair = api._verify("contracts_rf2_arctic")
        self.assertEqual((pair.model, pair.verified), (ARCTIC, True))
        self.assertGreater(pair.cosine, 0.9999)
        # отпечаток считается по тексту куска без префикса: префикс «query: » только у вопросов
        embeds = [b for p, b in zip(self.ollama.paths(), self.ollama.json_bodies()) if p == "/api/embed"]
        self.assertTrue(embeds[-1]["input"][0].startswith("Document: Doc 0"))
        self.assertEqual(embeds[-1]["model"], ARCTIC)

    def test_planted_collection_refused_by_fingerprint(self):
        with self.assertRaises(binding.Refused) as ctx:
            api._verify("contracts_mislabeled")
        self.assertTrue(ctx.exception.reason.startswith("fingerprint mismatch: cosine "))
        self.assertEqual(ctx.exception.model, "bge-m3")

    def test_unregistered_missing_and_broken(self):
        with contextlib.redirect_stdout(io.StringIO()):
            load(self.qdrant.url, os.path.join(self.root, "rw"), os.path.join(self.root, "stage1", "naive500_title"), "fresh")
        for name, reason in (("fresh", "fresh is not in the registry"), ("nope", "nope is not in the registry")):
            with self.assertRaises(binding.Refused) as ctx:
                api._verify(name)
            self.assertEqual(ctx.exception.reason, reason)
        # запись есть, а коллекции нет (удалили руками)
        self.mem.cols.pop("contracts_rf2_arctic")
        with self.assertRaises(binding.Refused) as ctx:
            api._verify("contracts_rf2_arctic")
        self.assertEqual(ctx.exception.reason, "collection contracts_rf2_arctic not found")
        # проба пропала из коллекции
        probe = registry.registry_id("contracts_rf2")
        pid = self.mem.cols["embedding_registry"]["points"][probe]["payload"]["probe_id"]
        self.mem.cols["contracts_rf2"]["points"].pop(pid)
        with self.assertRaises(binding.Refused) as ctx:
            api._verify("contracts_rf2")
        self.assertEqual(ctx.exception.reason, "fingerprint point is missing from the collection")

    def test_no_registry(self):
        self.mem.cols.pop("embedding_registry")
        with self.assertRaises(binding.Refused) as ctx:
            api._verify("contracts_rf2")
        self.assertEqual(ctx.exception.reason, "registry embedding_registry not found")

    def test_embedder_refuses_model(self):
        self.ollama.handler = model_ollama_handler(allowed=("bge-m3",))  # фильтр без второй модели
        with self.assertRaises(binding.Refused) as ctx:
            api._verify("contracts_rf2_arctic")
        self.assertEqual(ctx.exception.reason, f"embedder does not allow model {ARCTIC}")

    def test_embedder_down_is_not_a_verdict(self):
        self.patch(OLLAMA_URL=closed_port_url())
        with self.assertRaises(Exception) as ctx:
            api._verify("contracts_rf2_arctic")
        self.assertNotIsInstance(ctx.exception, binding.Refused)  # временный сбой: проверим ещё раз позже

    def test_service_cannot_write(self):
        """Ключ сервиса только на чтение: ни реестр, ни алиас им не поменять."""
        ro = registry.Qdrant(self.qdrant.url, KEY)
        with self.assertRaises(registry.Failed):
            registry.switch_alias(ro, "contracts", "contracts_mislabeled")
        self.assertEqual(self.mem.aliases, {"contracts": "contracts_rf2"})


@unittest.skipIf(TestClient is None, "нет fastapi.testclient (httpx)")
class SwitchEndpointsTest(Stand):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(api.app)

    def search(self, q: str = "Which law governs the agreement?") -> dict:
        r = self.client.get("/search", params={"q": q, "k": 3})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def last_embed(self) -> dict:
        return [b for p, b in zip(self.ollama.paths(), self.ollama.json_bodies()) if p == "/api/embed"][-1]

    def test_not_ready_until_alias_read(self):
        r = self.client.get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["model_check"]["status"], "unverified")
        self.refresh()
        r = self.client.get("/readyz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["model_check"]["status"], "verified")

    def test_switch_rollback_and_trap(self):
        self.refresh()
        body = self.search()
        self.assertEqual((body["collection"], body["model"]), ("contracts_rf2", "bge-m3"))
        self.assertEqual(body["model_check"]["status"], "verified")
        self.assertEqual(self.last_embed(), {"model": "bge-m3", "input": ["Which law governs the agreement?"]})

        self.alias("contracts_rf2_arctic")
        body = self.search()
        self.assertEqual(body["collection"], "contracts_rf2")  # до проверки отвечает прежняя пара
        self.refresh()
        body = self.search()
        self.assertEqual((body["collection"], body["model"]), ("contracts_rf2_arctic", ARCTIC))
        self.assertEqual(body["model_check"]["target"], "contracts_rf2_arctic")
        self.assertEqual(self.last_embed(), {"model": ARCTIC, "input": ["query: Which law governs the agreement?"]})
        self.assertEqual(self.qdrant.paths()[-1], "/collections/contracts_rf2_arctic/points/query")  # не алиас
        self.assertTrue(body["results"])
        self.assertIn("model_switch", [e["event"] for e in self.events()])

        self.alias("contracts_rf2")  # откат
        self.refresh()
        self.assertEqual(self.search()["model"], "bge-m3")

        self.alias("contracts_mislabeled")  # ловушка: векторы arctic, подписана bge-m3
        self.refresh()
        body = self.search()
        self.assertEqual((body["collection"], body["model"]), ("contracts_rf2", "bge-m3"))
        check = body["model_check"]
        self.assertEqual((check["status"], check["target"], check["target_model"], check["verified"]),
                         ("refused", "contracts_mislabeled", "bge-m3", True))
        self.assertTrue(check["reason"].startswith("fingerprint mismatch: cosine "))
        ready = self.client.get("/readyz")
        self.assertEqual(ready.status_code, 200)  # под исправен: отвечает проверенной парой
        self.assertEqual(ready.json()["model_check"]["status"], "refused")
        refused = [e for e in self.events() if e["event"] == "model_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual((refused[0]["target"], refused[0]["pod"]), ("contracts_mislabeled", "search-a"))

        self.alias("contracts_rf2")  # уборка
        self.refresh()
        self.assertEqual(self.search()["model_check"]["status"], "verified")

    def test_answer_carries_the_pair(self):
        self.refresh()
        self.alias("contracts_rf2_arctic")
        self.refresh()
        reply = json.dumps({"found": False, "answer": "", "citations": []})
        chat = ollama_handler(chat_content=reply)
        embed = model_ollama_handler(allowed=("bge-m3", ARCTIC))
        self.ollama.handler = lambda m, p, h, b: (embed if p == "/api/embed" else chat)(m, p, h, b)
        body = self.client.get("/answer", params={"q": "Which law governs the agreement?", "k": 2}).json()
        self.assertEqual((body["model"], body["embed_model"], body["collection"]), ("qwen3:8b", ARCTIC, "contracts_rf2_arctic"))
        self.assertEqual(body["model_check"]["status"], "verified")

    def test_without_alias_behaves_as_before(self):
        self.patch(SEARCH_ALIAS="", COLLECTION="contracts_rf2_arctic", MODEL=ARCTIC)
        body = self.search("governing law")
        self.assertEqual((body["collection"], body["model"]), ("contracts_rf2_arctic", ARCTIC))
        self.assertNotIn("model_check", body)
        self.assertEqual(self.last_embed(), {"model": ARCTIC, "input": ["query: governing law"]})  # префикс модели
        self.assertEqual(self.client.get("/readyz").json(), {"ready": True})
        self.assertIsNone(api.BINDING)  # без алиаса ни состояния, ни фонового потока


if __name__ == "__main__":
    unittest.main()
