import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_pulse as mp  # noqa: E402


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class TestAverage(unittest.TestCase):
    def test_average_with_all_valid(self):
        result = mp.compute_average_24h_change({"bitcoin": 2.0, "ethereum": 4.0})
        self.assertEqual(result["average_pct"], 3.0)
        self.assertEqual(result["sample_count"], 2)

    def test_average_skips_none_values(self):
        result = mp.compute_average_24h_change({"bitcoin": 2.0, "ethereum": None, "tron": 4.0})
        self.assertEqual(result["average_pct"], 3.0)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(sorted(result["sample_assets"]), ["bitcoin", "tron"])

    def test_average_all_none_returns_none_not_zero(self):
        result = mp.compute_average_24h_change({"bitcoin": None, "ethereum": None})
        self.assertIsNone(result["average_pct"])
        self.assertEqual(result["sample_count"], 0)

    def test_average_empty_dict(self):
        result = mp.compute_average_24h_change({})
        self.assertIsNone(result["average_pct"])


class TestSnapshot(unittest.TestCase):
    def test_all_sources_valid(self):
        now = datetime.now(timezone.utc)
        snap = mp.build_market_pulse_snapshot(
            fng_value=58, fng_classification="Greed", fng_timestamp_utc=iso(now - timedelta(hours=2)),
            btc_dominance_pct=54.3,
            change_24h_by_asset={"bitcoin": 1.2, "ethereum": 2.1, "tron": -0.5, "the-open-network": 0.8},
            now=now,
        )
        self.assertEqual(snap["fear_greed"]["status"], "valid")
        self.assertEqual(snap["fear_greed"]["value"], 58)
        self.assertEqual(snap["btc_dominance_status"], "valid")
        self.assertIsNotNone(snap["average_24h_change"]["average_pct"])
        self.assertIn("alternative.me", snap["sources_used"])
        self.assertIn("coingecko", snap["sources_used"])
        self.assertTrue(snap["has_any_data"])

    def test_one_source_failing_does_not_break_others(self):
        now = datetime.now(timezone.utc)
        snap = mp.build_market_pulse_snapshot(
            fng_value=None, fng_classification=None, fng_timestamp_utc=None,
            btc_dominance_pct=54.3,
            change_24h_by_asset={"bitcoin": 1.2},
            now=now,
        )
        self.assertEqual(snap["fear_greed"]["status"], "unavailable")
        self.assertIsNone(snap["fear_greed"]["value"])
        self.assertEqual(snap["btc_dominance_status"], "valid")
        self.assertIsNotNone(snap["average_24h_change"]["average_pct"])
        self.assertNotIn("alternative.me", snap["sources_used"])

    def test_stale_fng_marked_unavailable_not_shown_as_fresh(self):
        now = datetime.now(timezone.utc)
        snap = mp.build_market_pulse_snapshot(
            fng_value=70, fng_classification="Greed",
            fng_timestamp_utc=iso(now - timedelta(days=3)),  # خیلی قدیمی
            btc_dominance_pct=None,
            change_24h_by_asset={},
            now=now,
        )
        self.assertEqual(snap["fear_greed"]["status"], "unavailable")
        self.assertIsNone(snap["fear_greed"]["value"])  # مقدار قدیمی منتشر نمی‌شود

    def test_no_data_at_all(self):
        now = datetime.now(timezone.utc)
        snap = mp.build_market_pulse_snapshot(
            fng_value=None, fng_classification=None, fng_timestamp_utc=None,
            btc_dominance_pct=None, change_24h_by_asset=None, now=now,
        )
        self.assertFalse(snap["has_any_data"])
        self.assertEqual(snap["sources_used"], [])


if __name__ == "__main__":
    unittest.main()
