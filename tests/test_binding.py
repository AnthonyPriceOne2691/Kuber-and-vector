"""Пара (коллекция, модель) и её смена по алиасу (app/binding.py): без сети, resolve и verify подставные.

    python3 -I -B -m unittest discover -s tests -t .
"""
from __future__ import annotations

import threading
import time
import unittest

from app import binding
from app.binding import Binding, Pair, Refused


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class World:
    """Что видит поток: куда указывает алиас и какие коллекции проходят проверку."""

    def __init__(self) -> None:
        self.alias: str | None = "contracts_rf2"
        self.models = {"contracts_rf2": "bge-m3", "contracts_rf2_arctic": "snowflake-arctic-embed2"}
        self.refuse: dict[str, Exception] = {}
        self.verified: list[str] = []
        self.alias_error: Exception | None = None
        self.events: list[tuple[str, dict]] = []

    def resolve(self) -> str | None:
        if self.alias_error:
            raise self.alias_error
        return self.alias

    def verify(self, collection: str) -> Pair:
        self.verified.append(collection)
        if collection in self.refuse:
            raise self.refuse[collection]
        if collection not in self.models:
            raise Refused(f"{collection} is not in the registry")
        return Pair(collection, self.models[collection], verified=True, cosine=0.9999)

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


class BindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
        self.clock = Clock()
        self.b = Binding("contracts", "contracts_rf2", "bge-m3", resolve=self.w.resolve, verify=self.w.verify,
                         recheck=5.0, log=self.w.log, clock=self.clock)

    def test_before_first_refresh_not_resolved(self):
        st = self.b.state
        self.assertFalse(st.resolved)
        self.assertEqual((st.pair.collection, st.pair.model, st.pair.verified), ("contracts_rf2", "bge-m3", False))
        self.assertEqual(self.b.view()["status"], "unverified")

    def test_start_pair_is_verified(self):
        st = self.b.refresh()
        self.assertTrue(st.resolved)
        self.assertEqual(st.pair, Pair("contracts_rf2", "bge-m3", True, 0.9999))
        self.assertEqual(self.b.view(), {"status": "verified", "alias": "contracts", "target": "contracts_rf2",
                                         "verified": True, "cosine": 0.9999})
        self.assertEqual(self.w.verified, ["contracts_rf2"])
        self.b.refresh()
        self.assertEqual(self.w.verified, ["contracts_rf2"])  # проверенную пару каждую секунду не проверяем

    def test_switch_takes_the_whole_pair(self):
        self.b.refresh()
        before = self.b.state
        self.w.alias = "contracts_rf2_arctic"
        after = self.b.refresh()
        self.assertEqual(after.pair, Pair("contracts_rf2_arctic", "snowflake-arctic-embed2", True, 0.9999))
        self.assertEqual(before.pair.collection, "contracts_rf2")  # снимок запроса не меняется задним числом
        self.assertIn("model_switch", self.w.names())
        switch = dict(self.w.events)["model_switch"]
        self.assertEqual((switch["from"]["model"], switch["to"]["model"]), ("bge-m3", "snowflake-arctic-embed2"))
        self.assertEqual(self.b.view()["status"], "verified")

    def test_refused_target_keeps_previous_pair(self):
        self.b.refresh()
        self.w.models["contracts_mislabeled"] = "bge-m3"
        self.w.refuse["contracts_mislabeled"] = Refused("fingerprint mismatch: cosine 0.031 < 0.99", "bge-m3")
        self.w.alias = "contracts_mislabeled"
        st = self.b.refresh()
        self.assertEqual(st.pair.collection, "contracts_rf2")
        view = self.b.view()
        self.assertEqual((view["status"], view["target"], view["target_model"]), ("refused", "contracts_mislabeled", "bge-m3"))
        self.assertEqual(view["reason"], "fingerprint mismatch: cosine 0.031 < 0.99")
        self.assertTrue(view["verified"])  # отвечаем проверенной парой
        refused = [f for e, f in self.w.events if e == "model_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["serving"], {"collection": "contracts_rf2", "model": "bge-m3"})

    def test_refused_target_rechecked_not_every_second(self):
        self.b.refresh()
        self.w.refuse["bad"] = Refused("fingerprint mismatch: cosine 0.1 < 0.99")
        self.w.alias = "bad"
        self.b.refresh()
        self.clock.t += 1
        self.b.refresh()
        self.assertEqual(self.w.verified.count("bad"), 1)
        self.clock.t += 5
        self.b.refresh()
        self.assertEqual(self.w.verified.count("bad"), 2)
        self.assertEqual(self.w.names().count("model_refused"), 1)  # та же причина: в журнал второй раз не пишем
        del self.w.refuse["bad"]
        self.w.models["bad"] = "bge-m3"  # реестр поправили: следующая проверка проходит
        self.clock.t += 5
        st = self.b.refresh()
        self.assertEqual(st.pair.collection, "bad")
        self.assertIsNone(st.refused)

    def test_alias_back_clears_refusal_at_once(self):
        self.b.refresh()
        self.w.refuse["bad"] = Refused("not in the registry")
        self.w.alias = "bad"
        self.b.refresh()
        self.w.alias = "contracts_rf2"
        st = self.b.refresh()
        self.assertIsNone(st.refused)
        self.assertEqual(self.b.view()["status"], "verified")
        self.w.alias = "bad"  # ловушку подложили снова: отказ снова в журнале
        self.b.refresh()
        self.assertEqual(self.w.names().count("model_refused"), 2)

    def test_transient_error_is_a_refusal_with_reason(self):
        self.b.refresh()
        self.w.refuse["contracts_rf2_arctic"] = ConnectionRefusedError("embedder down")
        self.w.alias = "contracts_rf2_arctic"
        self.b.refresh()
        view = self.b.view()
        self.assertEqual(view["status"], "refused")
        self.assertTrue(view["reason"].startswith("cannot verify: ConnectionRefusedError"))
        del self.w.refuse["contracts_rf2_arctic"]
        self.clock.t += 5
        self.assertEqual(self.b.refresh().pair.model, "snowflake-arctic-embed2")

    def test_unregistered_start_pair_still_serves(self):
        self.w.models.pop("contracts_rf2")  # реестра ещё нет: отвечаем как до реестра
        st = self.b.refresh()
        self.assertEqual((st.pair.collection, st.pair.verified), ("contracts_rf2", False))
        view = self.b.view()
        self.assertEqual((view["status"], view["reason"]), ("unverified", "contracts_rf2 is not in the registry"))
        self.assertEqual(self.w.names(), ["model_unverified"])
        self.clock.t += 5
        self.b.refresh()
        self.assertEqual(self.w.names(), ["model_unverified"])  # повтор той же причины не пишем
        self.w.models["contracts_rf2"] = "bge-m3"  # коллекцию записали в реестр
        self.clock.t += 5
        self.assertTrue(self.b.refresh().pair.verified)

    def test_registry_model_wins_over_configured(self):
        self.w.models["contracts_rf2"] = "snowflake-arctic-embed2"
        st = self.b.refresh()
        self.assertEqual(st.pair.model, "snowflake-arctic-embed2")
        self.assertEqual(dict(self.w.events)["model_verified"]["configured_model"], "bge-m3")

    def test_alias_missing_and_unreadable(self):
        self.w.alias = None
        st = self.b.refresh()
        self.assertTrue(st.resolved)
        self.assertIsNone(st.target)
        self.assertEqual(st.pair.collection, "contracts_rf2")
        self.w.alias_error = OSError("qdrant down")
        same = self.b.refresh()
        self.assertIs(same, self.b.state)
        self.b.refresh()
        self.assertEqual(self.w.names().count("alias_unreadable"), 1)

    def test_view_of_a_snapshot(self):
        self.b.refresh()
        snap = self.b.state
        self.w.alias = "contracts_rf2_arctic"
        self.b.refresh()
        self.assertEqual(self.b.view(snap)["target"], "contracts_rf2")

    def test_background_thread(self):
        w = World()
        b = Binding("contracts", "contracts_rf2", "bge-m3", resolve=w.resolve, verify=w.verify, ttl=0.01, log=w.log)
        self.addCleanup(b.stop)
        b.ensure_running()
        b.ensure_running()  # второй поток не стартует
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "alias-watch" and t is b._thread), 1)
        w.alias = "contracts_rf2_arctic"
        for _ in range(200):
            if b.state.pair.collection == "contracts_rf2_arctic":
                break
            time.sleep(0.01)
        self.assertEqual(b.state.pair.model, "snowflake-arctic-embed2")


class HelpersTest(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(binding.cosine([1.0, 0.0], [2.0, 0.0]), 1.0)
        self.assertAlmostEqual(binding.cosine([1.0, 0.0], [0.0, 3.0]), 0.0)
        self.assertEqual(binding.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        with self.assertRaises(ValueError):
            binding.cosine([1.0], [1.0, 2.0])

    def test_registry_id_is_stable(self):
        self.assertEqual(binding.registry_id("contracts_rf2"), binding.registry_id("contracts_rf2"))
        self.assertNotEqual(binding.registry_id("contracts_rf2"), binding.registry_id("contracts_rf3"))


if __name__ == "__main__":
    unittest.main()
