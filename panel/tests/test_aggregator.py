"""Сборка состояния: карточки машин, хронология сбоя, события из переходов и здоровья источников."""
import json
import os
import unittest

import aggregator
from aggregator import Config, IncidentTracker, Panel, build_machines, machine_colors, pod_state
from sources import EventsSource, KubeSource, parse_kube, parse_leases, parse_time

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = parse_time("2026-11-18T10:06:31Z")


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def kube_data(fetched_at=NOW) -> dict:
    k = parse_kube(fixture("kubectl_nodes_pods.json"))
    k.update(leases=parse_leases(fixture("kube_leases.json")), server=None, fetched_at=fetched_at)
    return k


class MachinesTest(unittest.TestCase):
    def test_cards_from_kubectl(self):
        ms = {m["name"]: m for m in build_machines(Config(), kube_data(), True, None, None, None, NOW)}
        self.assertEqual(list(ms), ["machine-1", "machine-2", "machine-3", "machine-4"])
        self.assertEqual(ms["machine-1"]["status"], "ready")
        self.assertAlmostEqual(ms["machine-1"]["heartbeat_age_s"], 0.9, delta=0.1)
        self.assertEqual(ms["machine-2"]["status"], "notready")
        self.assertEqual(ms["machine-2"]["status_since"], parse_time("2026-11-18T10:05:44Z"))
        self.assertEqual(ms["machine-3"]["status"], "ready")
        self.assertTrue(ms["machine-3"]["kube"]["unschedulable"])
        self.assertEqual(ms["machine-4"]["status"], "observer")
        self.assertEqual([s["name"] for s in ms["machine-4"]["services"]], ["Load generator", "Panel"])
        names1 = sorted(p["name"] for p in ms["machine-1"]["pods"])
        self.assertEqual(names1, ["qdrant-0", "search-7f9c6d8b5-dfzb8", "warmup-6c9d4b7f8-k2m4n"])
        self.assertEqual(ms["machine-1"]["system_pods"], 1)  # coredns; завершённый helm-install не в счёт
        short = {p["name"]: p["short"] for p in ms["machine-1"]["pods"]}
        self.assertEqual(short["search-7f9c6d8b5-dfzb8"], "dfzb8")
        self.assertEqual(short["qdrant-0"], "qdrant-0")
        states = {p["name"]: p["state"] for p in ms["machine-3"]["pods"]}
        self.assertEqual(states, {"search-7f9c6d8b5-jd2w8": "starting", "search-7f9c6d8b5-ev1ct": "failed",
                                  "qdrant-2": "ready"})
        self.assertEqual({p["state"] for p in ms["machine-2"]["pods"]}, {"terminating", "notready"})
        self.assertEqual([m["color"] for m in ms.values()], ["#3987e5", "#d95926", "#199e70", "#8a93a5"])

    def test_missing_heartbeat_before_notready(self):
        k = kube_data()
        k["nodes"][0]["ready"] = "True"
        k["leases"]["machine-1"]["renew"] = NOW - 20
        m1 = build_machines(Config(), k, True, None, None, None, NOW)[0]
        self.assertEqual(m1["status"], "unreachable")
        self.assertEqual(m1["status_since"], NOW - 20)

    def test_hanging_api_does_not_age_heartbeats(self):
        # API не отвечает 10 минут: показываем последние данные, а не «у всех нет пульса»
        k = kube_data(fetched_at=NOW)
        ms = {m["name"]: m for m in build_machines(Config(), k, False, None, None, None, NOW + 600)}
        self.assertEqual(ms["machine-1"]["status"], "ready")
        self.assertAlmostEqual(ms["machine-1"]["heartbeat_age_s"], 0.9, delta=0.1)

    def test_no_kube_source(self):
        ms = build_machines(Config(), None, False, None, None, None, NOW)
        self.assertEqual([m["status"] for m in ms], ["unknown", "unknown", "unknown", "observer"])

    def test_colors_are_fixed_by_order_and_never_cycle(self):
        names = [f"m{i}" for i in range(11)]
        c = machine_colors(names, observer="m3")
        self.assertEqual(c["m0"], "#3987e5")
        self.assertEqual(c["m3"], aggregator.OBSERVER_COLOR)
        self.assertEqual(c["m4"], aggregator.CLUSTER_COLORS[3])
        self.assertEqual(c["m10"], aggregator.EXTRA_COLOR)

    def test_pod_state(self):
        self.assertEqual(pod_state({"deleting": True, "ready": True}), "terminating")
        self.assertEqual(pod_state({"ready": True}), "ready")
        self.assertEqual(pod_state({"status": "CrashLoopBackOff", "phase": "Running"}), "failed")
        self.assertEqual(pod_state({"status": "Init:0/1", "phase": "Pending"}), "starting")
        self.assertEqual(pod_state({"status": "Running", "phase": "Running"}), "notready")


def machine(status, since=None, hb=None, name="machine-2"):
    return {"name": name, "role": "server", "color": "#d95926", "status": status, "status_since": since,
            "heartbeat_at": hb}


PODS = [{"name": "search-a", "app": "search", "node": "machine-1", "owner_kind": "ReplicaSet", "ready": True},
        {"name": "search-b", "app": "search", "node": "machine-2", "owner_kind": "ReplicaSet", "ready": True}]
HEALTHY = {"ok": True, "summary": {"healthy": True}}
DEGRADED = {"ok": True, "summary": {"healthy": False}}


class IncidentTest(unittest.TestCase):
    def test_full_story(self):
        tr = IncidentTracker(Config())
        tr.update(100, [machine("ready", hb=95)], PODS, HEALTHY, None, {})
        self.assertIsNone(tr.cur)
        # пир Qdrant пропал в 101, пульс — в 117: момент потери — по Qdrant
        tr.update(117, [machine("unreachable", since=95, hb=95)], PODS, DEGRADED, None, {"machine-2": 101.0})
        self.assertEqual(tr.cur["t"]["lost"], 101.0)
        tr.update(146, [machine("notready", since=145, hb=95)], PODS, DEGRADED, None, {"machine-2": 101.0})
        moved = PODS[:1] + [dict(PODS[1], deleting=True, ready=False),
                            {"name": "search-c", "app": "search", "node": "machine-3", "owner_kind": "ReplicaSet", "ready": True}]
        tr.update(160, [machine("notready", since=145, hb=95)], moved, DEGRADED, None, {})
        tr.update(200, [machine("ready", since=198, hb=199)], moved, DEGRADED, None, {})
        self.assertTrue(tr.cur["active"])
        tr.update(220, [machine("ready", since=198, hb=219)], moved, HEALTHY, None, {})
        snap = tr.snapshot(230)
        self.assertFalse(snap["active"])
        self.assertEqual({s["key"]: s["offset_s"] for s in snap["steps"]},
                         {"lost": 0, "notready": 44, "moved": 59, "back": 97, "recovered": 119})
        self.assertEqual((snap["downtime_s"], snap["recovery_s"], snap["total_s"]), (97, 22, 119))
        self.assertEqual(snap["apps"], {"search": 2})

    def test_short_blip_without_notready_and_without_qdrant(self):
        tr = IncidentTracker(Config())
        tr.update(50, [machine("unreachable", since=30, hb=30)], [], None, None, {})
        self.assertEqual(tr.cur["t"]["lost"], 35.0)  # аренда + половина интервала продления
        tr.update(60, [machine("ready", since=10, hb=59)], [], None, None, {})
        snap = tr.snapshot(61)
        self.assertEqual(snap["phase"], "recovered")
        self.assertEqual([s["key"] for s in snap["steps"]], ["lost", "notready", "back", "recovered"])  # без «moved»
        self.assertFalse(snap["steps"][1]["done"])
        self.assertEqual(snap["recovery_s"], 0)

    def test_flapping_machine_resets_back(self):
        tr = IncidentTracker(Config())
        tr.update(10, [machine("notready", since=9)], PODS, DEGRADED, None, {})
        tr.update(20, [machine("ready", since=19)], PODS, DEGRADED, None, {})
        self.assertIn("back", tr.cur["t"])
        tr.update(25, [machine("notready", since=24)], PODS, DEGRADED, None, {})
        self.assertNotIn("back", tr.cur["t"])
        self.assertTrue(tr.cur["active"])

    def test_load_is_accumulated_once_per_second(self):
        tr = IncidentTracker(Config())
        series = [{"t": t, "qps": 10, "errors_without_retry": 2 if t >= 100 else 0, "errors_with_retry": 0,
                   "p99": 2000.0 if t >= 100 else 40.0} for t in range(90, 110)]
        load = {"series": series}
        tr.update(110, [machine("notready", since=100.5)], [], DEGRADED, load, {})
        tr.update(110.5, [machine("notready", since=100.5)], [], DEGRADED, load, {})  # тот же ряд ещё раз
        acc = tr.snapshot(111)["load"]
        self.assertEqual((acc["requests"], acc["without_retry"], acc["with_retry"], acc["p99_max"]), (100, 20, 0, 2000.0))


class SourceEventsTest(unittest.TestCase):
    def test_api_down_and_back_events(self):
        good = json.dumps(fixture("kubectl_nodes_pods.json"))
        script = [good, good, RuntimeError("Unable to connect to the server: dial tcp: i/o timeout"), good]

        def run(cmd, timeout):
            step = script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        kube = KubeSource(runner=run, leases=False)
        panel = Panel(Config(), EventsSource(), kube=kube)
        for t in (1.0, 2.0, 3.0):
            kube.poll(now=t)
            panel.tick(now=t)
        titles = [e["title"] for e in panel.events.recent()]
        self.assertIn("Kubernetes API not responding", titles)
        self.assertNotIn("Kubernetes API responding again", titles)
        kube.poll(now=4.0)
        st = panel.tick(now=4.0)
        self.assertIn("Kubernetes API responding again", [e["title"] for e in st["events"]])
        self.assertEqual(st["events"][-1]["title"], "Panel started")

    def test_derived_pod_events(self):
        k1 = parse_kube(fixture("kubectl_nodes_pods.json"))
        k2 = parse_kube(fixture("kubectl_nodes_pods.json"))
        pods = {p["name"]: p for p in k2["pods"]}
        pods["search-7f9c6d8b5-jd2w8"].update(ready=True, status="Running", phase="Running")
        pods["qdrant-1"]["restarts"] = 4
        k2["pods"] = [p for p in k2["pods"] if p["name"] != "search-7f9c6d8b5-8szvd"]
        docs = [k1, k2]

        class Fake:
            name = "kube"

            def data(self):
                return dict(docs[0], leases=None, server=None, fetched_at=0)

            def status(self, now):
                return {"ok": True, "age_s": 0.0, "error": None}

            def start(self):
                pass

            def stop(self):
                pass
        panel = Panel(Config(), EventsSource(), kube=Fake())
        panel.tick(now=10.0)
        docs.pop(0)
        st = panel.tick(now=11.0)
        titles = {e["title"] for e in st["events"]}
        self.assertIn("search pod Ready on machine-3", titles)
        self.assertIn("qdrant restarted on machine-2", titles)
        self.assertIn("search pod removed", titles)


if __name__ == "__main__":
    unittest.main()
