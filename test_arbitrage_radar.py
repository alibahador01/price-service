import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arbitrage_radar as ar  # noqa: E402


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class TestFormula(unittest.TestCase):
    def test_spec_example_vochino_more_expensive(self):
        # طبق متن سند: نرخ ۲۷۰,۴۴۰ بالاتر از ۲۶۶,۱۲۰ است -> باید «گران‌تر» باشد
        benchmark = 266120.0
        vochino = 270440.0
        diff = ar.compute_difference_pct(vochino, benchmark)
        self.assertGreater(diff, 0)
        self.assertEqual(ar.classify_direction(diff), "vochino_more_expensive")

    def test_vochino_cheaper(self):
        diff = ar.compute_difference_pct(260000.0, 266120.0)
        self.assertLess(diff, 0)
        self.assertEqual(ar.classify_direction(diff), "vochino_cheaper")

    def test_equal_rates(self):
        diff = ar.compute_difference_pct(100000.0, 100000.0)
        self.assertEqual(diff, 0.0)
        self.assertEqual(ar.classify_direction(diff), "equal")

    def test_none_inputs_return_none(self):
        self.assertIsNone(ar.compute_difference_pct(None, 100.0))
        self.assertIsNone(ar.compute_difference_pct(100.0, None))
        self.assertIsNone(ar.compute_difference_pct(100.0, 0.0))

    def test_global_benchmark_multiplication(self):
        self.assertAlmostEqual(ar.compute_global_benchmark_toman(1.0005, 110000.0), 1.0005 * 110000.0)

    def test_global_benchmark_invalid_inputs(self):
        self.assertIsNone(ar.compute_global_benchmark_toman(None, 100.0))
        self.assertIsNone(ar.compute_global_benchmark_toman(1.0, 0.0))
        self.assertIsNone(ar.compute_global_benchmark_toman(-1.0, 100.0))


class TestSnapshot(unittest.TestCase):
    def test_valid_snapshot(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=270440.0,
            usdt_usd_rate=1.0005,
            usdt_usd_updated_at=iso(now - timedelta(minutes=30)),
            usd_irt_toman=266000.0,
            usd_irt_updated_at=iso(now - timedelta(minutes=10)),
            now=now,
        )
        self.assertEqual(snap["status"], "valid")
        self.assertIsNotNone(snap["difference_pct"])
        self.assertEqual(snap["direction"], "vochino_more_expensive")

    def test_missing_vochino_rate_skips(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=None,
            usdt_usd_rate=1.0, usdt_usd_updated_at=iso(now),
            usd_irt_toman=100000.0, usd_irt_updated_at=iso(now),
            now=now,
        )
        self.assertEqual(snap["status"], "skip")
        self.assertEqual(snap["reason"], "vochino_rate_missing_or_invalid")

    def test_missing_benchmark_source_skips(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=270000.0,
            usdt_usd_rate=None, usdt_usd_updated_at=None,
            usd_irt_toman=100000.0, usd_irt_updated_at=iso(now),
            now=now,
        )
        self.assertEqual(snap["status"], "skip")
        self.assertEqual(snap["reason"], "benchmark_source_missing")

    def test_stale_usdt_rate_skips(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=270000.0,
            usdt_usd_rate=1.0, usdt_usd_updated_at=iso(now - timedelta(hours=10)),
            usd_irt_toman=100000.0, usd_irt_updated_at=iso(now),
            now=now,
            max_age_seconds=3 * 3600,
        )
        self.assertEqual(snap["status"], "skip")
        self.assertEqual(snap["reason"], "benchmark_stale")

    def test_timestamp_skew_too_large_skips(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=270000.0,
            usdt_usd_rate=1.0, usdt_usd_updated_at=iso(now - timedelta(minutes=5)),
            usd_irt_toman=100000.0, usd_irt_updated_at=iso(now - timedelta(hours=5)),
            now=now,
            max_age_seconds=6 * 3600,
            max_skew_seconds=3 * 3600,
        )
        self.assertEqual(snap["status"], "skip")
        self.assertEqual(snap["reason"], "benchmark_timestamps_too_far_apart")

    def test_never_fabricates_on_skip(self):
        now = datetime.now(timezone.utc)
        snap = ar.build_arbitrage_snapshot(
            vochino_rate_toman=None,
            usdt_usd_rate=None, usdt_usd_updated_at=None,
            usd_irt_toman=None, usd_irt_updated_at=None,
            now=now,
        )
        self.assertIsNone(snap["difference_pct"])
        self.assertIsNone(snap["global_benchmark_toman"])
        self.assertEqual(snap["direction"], "unavailable")


if __name__ == "__main__":
    unittest.main()
