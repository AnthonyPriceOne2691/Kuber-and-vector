"""Лента событий: склейка повторов, порядок, внешний вход (общий формат и формат Falco), лимиты."""
import unittest

from sources import EventsSource, RateLimited, parse_time

FALCO = {
    "uuid": "3b0f1c9e-9c3c-4a9a-8d2e-1f9c0e7c5a11",
    "output": "10:12:01.123456789: Notice A shell was spawned in a container with an attached terminal "
              "(user=root k8s.ns=search k8s.pod=search-7f9c6d8b5-dfzb8 container=search shell=sh)",
    "priority": "Notice",
    "rule": "Terminal shell in container",
    "time": "2026-11-18T10:12:01.123456789Z",
    "source": "syscall",
    "hostname": "machine-1",
    "tags": ["container", "shell", "mitre_execution", "T1059"],
    "output_fields": {"k8s.ns.name": "search", "k8s.pod.name": "search-7f9c6d8b5-dfzb8", "user.name": "root"},
}


class EventsTest(unittest.TestCase):
    def test_add_and_order_newest_first(self):
        ev = EventsSource()
        ev.add("first", ts=100.0)
        ev.add("second", level="warn", ts=105.0)
        ev.add("late but older", ts=102.0)  # переход узла приходит со временем перехода, а не опроса
        titles = [e["title"] for e in ev.recent()]
        self.assertEqual(titles, ["second", "late but older", "first"])
        self.assertEqual(ev.recent()[0]["level"], "warn")
        self.assertEqual(sorted(e["id"] for e in ev.recent()), [1, 2, 3])

    def test_duplicates_are_merged_with_a_counter(self):
        ev = EventsSource(dedupe_s=10)
        ev.add("Terminal shell in container", kind="security", machine="machine-1", pod="p", ts=10.0)
        ev.add("Terminal shell in container", kind="security", machine="machine-1", pod="p", ts=14.0, detail="again")
        ev.add("Terminal shell in container", kind="security", machine="machine-1", pod="p", ts=30.0)
        items = ev.recent()
        self.assertEqual(len(items), 2)
        newest, merged = items
        self.assertEqual(newest["count"], 1)
        self.assertEqual((merged["count"], merged["last_ts"], merged["detail"]), (2, 14.0, "again"))

    def test_text_is_cleaned_and_limited(self):
        ev = EventsSource()
        e = ev.add("bad\x1b[31m title\n" + "x" * 300, level="nonsense", detail="a\tb")
        self.assertNotIn("\x1b", e["title"])
        self.assertNotIn("\n", e["title"])
        self.assertLessEqual(len(e["title"]), 140)
        self.assertTrue(e["title"].endswith("…"))
        self.assertEqual(e["level"], "info")  # неизвестный уровень — info
        self.assertEqual(e["detail"], "a b")

    def test_ring_buffer(self):
        ev = EventsSource(maxlen=5, dedupe_s=0)
        for i in range(8):
            ev.add(f"e{i}", ts=float(i))
        self.assertEqual([e["title"] for e in ev.recent()], ["e7", "e6", "e5", "e4", "e3"])
        self.assertEqual(len(ev.recent(limit=2)), 2)


class IngestTest(unittest.TestCase):
    def test_falco_alert(self):
        ev = EventsSource()
        e = ev.ingest(dict(FALCO), now=parse_time("2026-11-18T10:12:02Z"))
        self.assertEqual(e["title"], "Terminal shell in container")
        self.assertEqual((e["kind"], e["source"], e["level"]), ("security", "falco", "warn"))
        self.assertEqual(e["machine"], "machine-1")
        self.assertEqual(e["pod"], "search-7f9c6d8b5-dfzb8")
        self.assertAlmostEqual(e["ts"], parse_time("2026-11-18T10:12:01.123456Z"), places=3)
        self.assertIn("shell was spawned", e["detail"])
        # время и важность из начала output в ленте не повторяются: у события свои поля ts и level
        self.assertTrue(e["detail"].startswith("A shell was spawned"), e["detail"])

    def test_falco_output_head_variants(self):
        ev = EventsSource(dedupe_s=0)
        iso = dict(FALCO, output="2026-11-18T10:12:01.123456789+0000: Critical Qdrant key read (pod=x)", rule="r1")
        self.assertEqual(ev.ingest(iso, now=parse_time("2026-11-18T10:12:02Z"))["detail"], "Qdrant key read (pod=x)")
        plain = dict(FALCO, output="no time prefix here: Warning stays as is", rule="r2")
        self.assertEqual(ev.ingest(plain, now=parse_time("2026-11-18T10:12:02Z"))["detail"], "no time prefix here: Warning stays as is")

    def test_falco_priorities(self):
        ev = EventsSource(dedupe_s=0)
        now = parse_time("2026-11-18T10:12:02Z")
        for prio, level in (("Critical", "error"), ("Warning", "warn"), ("Informational", "info"), ("Alert", "error")):
            self.assertEqual(ev.ingest(dict(FALCO, priority=prio), now=now)["level"], level)

    def test_generic_event_from_responder(self):
        ev = EventsSource()
        e = ev.ingest({"title": "Pod quarantined", "level": "ok", "pod": "search-7f9c6d8b5-dfzb8",
                       "machine": "machine-1", "detail": "label quarantine=true, replacement scheduled"}, now=50.0)
        self.assertEqual((e["title"], e["level"], e["kind"], e["source"]), ("Pod quarantined", "ok", "security", "external"))
        self.assertEqual(e["ts"], 50.0)  # без времени — время приёма

    def test_bad_payloads(self):
        ev = EventsSource()
        with self.assertRaises(ValueError):
            ev.ingest({"detail": "no title"})
        with self.assertRaises(ValueError):
            ev.ingest(["not", "an", "object"])
        e = ev.ingest({"title": "clock skew", "ts": 1.0}, now=1_000_000.0)
        self.assertEqual(e["ts"], 1_000_000.0)  # время из будущего или прошлого — заменяем временем приёма

    def test_rate_limit(self):
        ev = EventsSource(rate=0.001, burst=3)
        for i in range(3):
            ev.ingest({"title": f"e{i}"}, now=1.0)
        with self.assertRaises(RateLimited):
            ev.ingest({"title": "one too many"}, now=1.0)


if __name__ == "__main__":
    unittest.main()
