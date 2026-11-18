"""Перцентили, скользящие окна и учёт ошибок «с повтором / без повтора» в loadgen.Stats."""
import unittest

from loadgen import KINDS, Stats, percentile


class PercentileTest(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(percentile([], 99))

    def test_nearest_rank_on_1_to_100(self):
        v = list(range(1, 101))
        self.assertEqual(percentile(v, 50), 50)
        self.assertEqual(percentile(v, 95), 95)
        self.assertEqual(percentile(v, 99), 99)
        self.assertEqual(percentile(v, 100), 100)
        self.assertEqual(percentile(v, 0), 1)

    def test_small_samples_take_observed_values(self):
        self.assertEqual(percentile([7.0], 99), 7.0)
        # 10 значений: p95 и p99 — это максимум (ранг 10), p50 — пятое
        v = [float(x) for x in range(10, 110, 10)]
        self.assertEqual(percentile(v, 50), 50.0)
        self.assertEqual(percentile(v, 95), 100.0)
        self.assertEqual(percentile(v, 99), 100.0)

    def test_one_slow_request_in_a_hundred_is_p100_not_p99(self):
        v = sorted([10.0] * 99 + [2000.0])
        self.assertEqual(percentile(v, 99), 10.0)
        v = sorted([10.0] * 98 + [2000.0] * 2)
        self.assertEqual(percentile(v, 99), 2000.0)


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.s = Stats(seed=1)

    def test_current_second_is_excluded(self):
        self.s.record(100.2, 10)
        self.s.record(100.8, 20)
        self.s.record(101.5, 30)
        w = self.s.window(1, now=101.9)  # завершённая секунда — только 100
        self.assertEqual(w["count"], 2)
        self.assertEqual(w["qps"], 2.0)
        self.assertEqual(w["max_ms"], 20)
        w = self.s.window(1, now=102.0)
        self.assertEqual(w["count"], 1)
        self.assertEqual(w["p50_ms"], 30)

    def test_10s_and_60s_windows(self):
        for sec in range(0, 70):
            for i in range(4):
                self.s.record(1000 + sec + i * 0.2, 10 + sec)
        now = 1070.5
        w10, w60 = self.s.window(10, now), self.s.window(60, now)
        self.assertEqual(w10["count"], 40)
        self.assertEqual(w10["qps"], 4.0)
        self.assertEqual(w60["count"], 240)
        self.assertEqual(w60["qps"], 4.0)
        self.assertEqual(w10["max_ms"], 79)  # последняя завершённая секунда — 1069
        self.assertEqual(w10["p50_ms"], 74)
        self.assertGreater(w60["p99_ms"], w60["p50_ms"])

    def test_old_buckets_are_dropped(self):
        s = Stats(keep_s=30)
        s.record(1000.1, 5)
        s.record(1100.1, 5)
        self.assertEqual(s.window(60, now=1040)["count"], 0)
        self.assertEqual(s.window(60, now=1101)["count"], 1)
        self.assertEqual(s.totals()["count"], 2)  # итоги не теряются

    def test_reservoir_caps_samples_but_counts_everything(self):
        s = Stats(max_samples=50, seed=3)
        for i in range(1000):
            s.record(500 + i / 1000, float(i % 100))
        w = s.window(1, now=501.5)
        self.assertEqual(w["count"], 1000)
        self.assertLessEqual(len(s._b[500].lat), 50)
        self.assertTrue(20 <= w["p50_ms"] <= 80)


class RetryAccountingTest(unittest.TestCase):
    def test_without_and_with_retry_are_counted_separately(self):
        s = Stats()
        s.record(10.1, 20)                                            # успех
        s.record(10.2, 2020, without_retry="timeout", with_retry=None, retried=True)
        s.record(10.3, 2040, without_retry="timeout", with_retry="5xx", retried=True)
        s.record(10.4, 3, without_retry="conn", with_retry=None, retried=True)
        s.record(10.5, 15, without_retry="4xx", with_retry="4xx")     # 4xx не повторяется
        w = s.window(1, now=11.0)
        self.assertEqual(w["count"], 5)
        self.assertEqual(w["without_retry"]["errors"], 4)
        self.assertEqual(w["without_retry"]["by_kind"], {"timeout": 2, "conn": 1, "5xx": 0, "4xx": 1, "bad": 0})
        self.assertEqual(w["with_retry"]["errors"], 2)
        self.assertEqual(w["with_retry"]["by_kind"]["5xx"], 1)
        self.assertEqual(w["with_retry"]["by_kind"]["4xx"], 1)
        self.assertEqual(w["retries"], 3)
        self.assertAlmostEqual(w["without_retry"]["rate"], 0.8)
        self.assertAlmostEqual(w["with_retry"]["rate"], 0.4)
        t = s.totals()
        self.assertEqual(t["without_retry"]["errors"], 4)
        self.assertEqual(t["with_retry"]["errors"], 2)
        self.assertEqual(set(t["without_retry"]["by_kind"]), set(KINDS))

    def test_skipped_requests(self):
        s = Stats()
        s.skip(5.5)
        s.skip(5.6)
        self.assertEqual(s.window(1, now=6.1)["skipped"], 2)
        self.assertEqual(s.totals()["skipped"], 2)
        self.assertEqual(s.window(1, now=6.1)["count"], 0)


class SeriesAndSnapshotTest(unittest.TestCase):
    def test_series_smooths_percentiles_but_not_counts(self):
        s = Stats(smooth_s=5)
        for sec in range(100, 110):
            for i in range(10):
                lat = 2000.0 if (sec == 105 and i < 3) else 10.0
                kind = "timeout" if lat > 1000 else None
                s.record(sec + i / 10, lat, without_retry=kind, retried=bool(kind))
        pts = {p["t"]: p for p in s.series(now=110.2, seconds=10)}
        self.assertEqual(len(pts), 10)
        self.assertEqual(pts[105]["errors_without_retry"], 3)
        self.assertEqual(pts[106]["errors_without_retry"], 0)
        self.assertEqual(pts[105]["qps"], 10)
        # p99 по 5 секундам: таймауты держатся с 105 по 109 включительно, до 105 — нет
        self.assertEqual(pts[104]["p99"], 10.0)
        self.assertEqual(pts[105]["p99"], 2000.0)
        self.assertEqual(pts[109]["p99"], 2000.0)

    def test_series_cache_follows_new_records(self):
        s = Stats(smooth_s=1)
        s.record(50.5, 10)
        self.assertEqual(s.series(now=51.5, seconds=1)[0]["p99"], 10)
        s.record(50.7, 99)  # поздняя запись в уже посчитанную секунду
        self.assertEqual(s.series(now=51.5, seconds=1)[0]["p99"], 99)

    def test_snapshot_shape(self):
        s = Stats()
        s.record(200.1, 12, serving=("bge-m3", "c1"), timing=(17.0, 1.2))
        s.record(200.2, 14, serving=("bge-m3", "c1"), timing=(19.0, 1.4))
        s.record(200.3, 16, serving=("e5", "c2"))
        snap = s.snapshot(now=201.0)
        self.assertEqual(snap["schema"], "loadgen/1")
        self.assertEqual(set(snap["windows"]), {"1s", "10s", "60s"})
        self.assertEqual(len(snap["series"]), 180)
        self.assertEqual(snap["serving"][0], {"model": "bge-m3", "collection": "c1", "count": 2, "share": 0.6667})
        self.assertEqual(snap["server_timing_ms"], {"embed": 18.0, "search": 1.3})
        self.assertEqual(snap["started_at"], 200.1)


if __name__ == "__main__":
    unittest.main()
