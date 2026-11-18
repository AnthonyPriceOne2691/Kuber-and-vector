"""Mock-сценарий «machine-2 выключена»: мир по секундам и полный прогон панели два круга подряд."""
import unittest

import aggregator
import mockstand as ms
from sources import EventsSource, KubeSource, LoadSource, QdrantSource, ServiceChecks, parse_kube, parse_leases

ORIGIN = 1_800_000_000.0


def mock_panel(start_offset: float = 0.0, seed: int = 7):
    """Панель как в `aggregator.py --mock`, но на подменённых часах."""
    clock = [ORIGIN]
    mock = ms.MockSource(start_offset=start_offset, seed=seed, clock=lambda: clock[0])
    cfg = aggregator.Config(mode="mock", cycle_s=ms.CYCLE)
    panel = aggregator.Panel(cfg, EventsSource(),
                             load=LoadSource(provider=mock.load_provider),
                             kube=KubeSource(runner=mock.kubectl),
                             qdrant=QdrantSource(mock.qdrant_targets(), None, mock.collection, fetch=mock.qdrant_fetch),
                             observer=ServiceChecks(mock.observer_checks(), connect=mock.connect),
                             mock=mock)
    mock.replay(panel, now=ORIGIN)
    return panel, mock


def run(panel, mock, seconds: int, start: float = ORIGIN) -> dict:
    states = {}
    for i in range(1, seconds + 1):
        t = start + i
        mock._now = t
        for s in panel.sources():
            s.poll(now=t)
        states[i] = panel.tick(now=t)
    mock._now = None
    return states


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.sc = ms.MockScenario(ORIGIN)

    def kube(self, ct: float, cycle: int = 0) -> dict:
        return parse_kube(self.sc.kubectl_doc(ORIGIN + cycle * ms.CYCLE + ct))

    def search_pods(self, ct: float) -> dict:
        return {p["name"][-5:]: (p["node"], p["status"], p["ready"]) for p in self.kube(ct)["pods"] if p["app"] == "search"}

    def test_cycle_math(self):
        self.assertEqual(self.sc.at(ORIGIN + 10), (0, 10.0))
        self.assertEqual(self.sc.at(ORIGIN + ms.CYCLE + 5), (1, 5.0))
        self.assertEqual(self.sc.at(ORIGIN - 5), (-1, ms.CYCLE - 5))

    def test_node_goes_notready_44s_after_loss_and_comes_back(self):
        def ready(ct):
            return {n["name"]: n["ready"] for n in self.kube(ct)["nodes"]}
        self.assertEqual(set(ready(10).values()), {"True"})
        self.assertEqual(ready(30)["machine-2"], "True")  # Kubernetes ещё не заметил
        self.assertEqual(ready(70)["machine-2"], "Unknown")
        self.assertEqual(ready(120)["machine-2"], "True")
        n2 = next(n for n in self.kube(70)["nodes"] if n["name"] == "machine-2")
        self.assertAlmostEqual(n2["ready_since"] - ORIGIN, ms.T_NOTREADY, delta=1)
        self.assertEqual(ms.T_NOTREADY - ms.T_LOSS, 44)

    def test_search_pod_is_evicted_and_replaced_on_another_machine(self):
        before = self.search_pods(10)
        self.assertEqual(sorted(v[0] for v in before.values()), ["machine-1", "machine-2"])
        on2 = [k for k, v in self.search_pods(70).items() if v[0] == "machine-2"]
        self.assertEqual(self.search_pods(70)[on2[0]], ("machine-2", "Running", False))
        mid = self.search_pods(75.5)
        self.assertEqual(mid[on2[0]][1], "Terminating")
        new = [k for k, v in mid.items() if v[0] == "machine-3"]
        self.assertEqual(mid[new[0]][1], "ContainerCreating")
        self.assertEqual(self.search_pods(85)[new[0]], ("machine-3", "Running", True))
        self.assertNotIn(on2[0], self.search_pods(118))
        # «ребалансировка» к концу круга: снова по поду на machine-1 и machine-2
        end = self.search_pods(160)
        self.assertEqual(sorted(v[0] for v in end.values()), ["machine-1", "machine-2"])

    def test_qdrant_pod_stays_and_restarts_once_per_round(self):
        def q1(ct, cycle=0):
            return next(p for p in self.kube(ct, cycle)["pods"] if p["name"] == "qdrant-1")
        self.assertEqual(q1(70)["node"], "machine-2")
        self.assertFalse(q1(70)["ready"])
        self.assertEqual(q1(119)["restarts"], q1(100)["restarts"] + 1)
        self.assertEqual(q1(5, cycle=1)["restarts"], q1(150)["restarts"])  # на стыке кругов без скачка
        self.assertTrue(q1(124)["ready"])

    def test_lease_freezes_while_machine_is_off(self):
        def renew(ct, name):
            return parse_leases(self.sc.leases_doc(ORIGIN + ct))[name]["renew"]
        frozen = renew(25, "machine-2")
        self.assertLessEqual(frozen, ORIGIN + ms.T_LOSS)
        self.assertEqual(renew(60, "machine-2"), frozen)
        self.assertGreater(renew(60, "machine-1"), renew(25, "machine-1"))
        self.assertGreater(renew(120, "machine-2"), ORIGIN + 110)

    def test_replicas_and_transfers(self):
        rs = self.sc.replica_state
        self.assertEqual(rs(0, "machine-2", 22), "Active")
        self.assertEqual(rs(0, "machine-2", 24), "Dead")
        self.assertEqual(rs(0, "machine-2", 122), "Partial")
        self.assertEqual(rs(1, "machine-2", 122), "Dead")
        self.assertEqual({rs(s, "machine-2", 136) for s in ms.VICTIM_SHARDS}, {"Active"})
        self.assertEqual(rs(0, "machine-1", 60), "Active")
        tr = self.sc.transfers(122.0)
        self.assertEqual(len(tr), 1)
        self.assertEqual((tr[0]["shard_id"], tr[0]["to"]), (0, ms.PEERS["machine-2"]))
        self.assertIn("Transferring records", tr[0]["comment"])
        self.assertEqual(self.sc.transfers(60.0), [])

    def test_victim_peer_errors(self):
        mock = ms.MockSource(clock=lambda: ORIGIN)
        url = dict(mock.qdrant_targets())["machine-2"]
        mock._now = ORIGIN + 50
        with self.assertRaises(TimeoutError):
            mock.qdrant_fetch(url, "/cluster")
        mock._now = ORIGIN + 110
        with self.assertRaises(ConnectionRefusedError):
            mock.qdrant_fetch(url, "/cluster")
        mock._now = ORIGIN + 125
        self.assertEqual(mock.qdrant_fetch(url, "/cluster")["result"]["peer_id"], ms.PEERS["machine-2"])

    def test_load_profile(self):
        self.assertEqual(ms.load_profile(10)["fail"], 0.0)
        self.assertEqual(ms.load_profile(21)["fail"], 0.33)
        self.assertEqual((ms.load_profile(40)["fail"], ms.load_profile(40)["slow"]), (0.004, 0.025))
        self.assertEqual(ms.load_profile(80)["fail"], 0.0)

    def test_synthetic_load_is_deterministic(self):
        def totals():
            m = ms.MockSource(seed=11, clock=lambda: ORIGIN)
            snap = None
            for i in range(1, 61):
                snap = m.load_provider(ORIGIN + i)
            return snap["totals"], snap["windows"]["60s"]["p99_ms"]
        self.assertEqual(totals(), totals())


class FullCycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel, cls.mock = mock_panel()
        cls.s = run(cls.panel, cls.mock, int(2 * ms.CYCLE))

    def machines(self, i):
        return {m["name"]: m for m in self.s[i]["machines"]}

    def test_calm_before_loss(self):
        st = self.s[10]
        self.assertEqual({m["status"] for m in st["machines"]}, {"ready", "observer"})
        self.assertIsNone(st["incident"])
        self.assertTrue(st["qdrant"]["summary"]["healthy"])
        self.assertAlmostEqual(st["load"]["windows"]["10s"]["qps"], 25.0, delta=0.3)

    def test_loss_is_seen_by_qdrant_and_load_first(self):
        st = self.s[22]
        self.assertGreater(st["qdrant"]["summary"]["offline"] + st["qdrant"]["summary"]["dead"], 0)
        self.assertGreater(st["load"]["windows"]["1s"]["without_retry"]["errors"], 0)
        self.assertEqual(st["load"]["windows"]["1s"]["with_retry"]["errors"], 0)
        self.assertGreater(st["load"]["windows"]["10s"]["p99_ms"], 1900)
        self.assertEqual(self.machines(22)["machine-2"]["status"], "ready")  # Kubernetes ещё не знает
        self.assertEqual(self.machines(35)["machine-2"]["status"], "unreachable")  # нет пульса kubelet
        self.assertEqual(self.machines(70)["machine-2"]["status"], "notready")

    def test_pods_move_and_replicas_catch_up(self):
        m3 = self.machines(90)["machine-3"]
        self.assertTrue(any(p["app"] == "search" and p["state"] == "ready" for p in m3["pods"]))
        self.assertGreater(self.s[130]["qdrant"]["summary"]["recovering"], 0)
        self.assertTrue(self.s[140]["qdrant"]["summary"]["healthy"])

    def test_incident_timeline_and_recovery_time(self):
        inc = self.s[150]["incident"]
        self.assertFalse(inc["active"])
        self.assertEqual((inc["machine"], inc["phase"]), ("machine-2", "recovered"))
        off = {s["key"]: s["offset_s"] for s in inc["steps"]}
        self.assertEqual(off["lost"], 0)
        self.assertAlmostEqual(off["notready"], 44, delta=1.5)
        self.assertAlmostEqual(off["moved"], 61, delta=2.5)
        self.assertAlmostEqual(off["back"], 96, delta=1.5)
        self.assertAlmostEqual(off["recovered"], 115, delta=2)
        self.assertAlmostEqual(inc["recovery_s"], 19, delta=2.5)
        self.assertAlmostEqual(inc["started"] - ORIGIN, ms.T_LOSS, delta=1)
        self.assertEqual(inc["load"]["with_retry"], 0)
        self.assertGreater(inc["load"]["without_retry"], 20)
        self.assertGreater(inc["load"]["p99_max"], 1900)
        self.assertEqual(inc["apps"], {"search": 2})

    def test_active_incident_while_machine_is_down(self):
        inc = self.s[70]["incident"]
        self.assertTrue(inc["active"])
        self.assertEqual(inc["phase"], "notready")
        self.assertIsNone(inc["recovery_s"])

    def test_second_round_repeats(self):
        last = self.s[int(2 * ms.CYCLE)]
        inc = last["incident"]
        self.assertAlmostEqual(inc["started"] - ORIGIN, ms.CYCLE + ms.T_LOSS, delta=1)
        self.assertEqual(inc["phase"], "recovered")
        self.assertAlmostEqual(inc["recovery_s"], 19, delta=2.5)
        self.assertEqual(last["load"]["totals"]["with_retry"]["errors"], 0)
        self.assertGreater(last["load"]["totals"]["without_retry"]["errors"], 60)

    def test_event_story_in_order(self):
        events = sorted((e for e in self.panel.events.recent(300)
                         if ORIGIN <= e["ts"] <= ORIGIN + ms.CYCLE), key=lambda e: (e["ts"], e["id"]))
        titles = [e["title"] for e in events]
        story = ["machine-2 powered off", "Qdrant peer on machine-2 down", "machine-2: 4 replicas offline",
                 "Retry absorbing failed attempts", "machine-2: 4 replicas Dead", "machine-2: no kubelet heartbeat",
                 "machine-2 NotReady", "search pod evicted from machine-2", "search pod Ready on machine-3",
                 "machine-2 powered on", "machine-2 Ready again",
                 "Qdrant peer on machine-2 back", "All 12 replicas Active", "search pod rebalanced to machine-2"]
        positions = []
        for s in story:
            self.assertIn(s, titles)
            positions.append(titles.index(s))
        self.assertEqual(positions, sorted(positions))
        self.assertIn("No failed attempts for 10 s", titles)  # конец эпизода — в пределах 10 с после NotReady
        self.assertTrue(any(t.startswith("Latency spike: p99") for t in titles))
        self.assertTrue(any(t.startswith("Latency back to normal") for t in titles))
        self.assertFalse(any(e["level"] == "error" and e["kind"] == "load" for e in events))  # пользователь ошибок не видел


class StartOffsetTest(unittest.TestCase):
    def test_mock_start_replays_history(self):
        panel, mock = mock_panel(start_offset=84)
        st = panel.tick(now=ORIGIN)
        self.assertEqual({m["name"]: m["status"] for m in st["machines"]}["machine-2"], "notready")
        inc = st["incident"]
        self.assertTrue(inc["active"])
        self.assertIn("moved", {s["key"] for s in inc["steps"] if s["done"]})
        # история графика уже есть: всплеск задержки в прошлом
        self.assertGreater(max(p["p99"] or 0 for p in st["load"]["series"]), 1900)
        self.assertTrue(any(e["title"] == "machine-2 NotReady" for e in st["events"]))


if __name__ == "__main__":
    unittest.main()
