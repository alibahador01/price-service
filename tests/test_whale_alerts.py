import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whale_alerts as wa  # noqa: E402


def make_trade(agg_id=1, notional=50000.0, direction="buy", price=100.0, qty=500.0, trade_time=1700000000000):
    return {
        "agg_trade_id": agg_id, "notional_usdt": notional, "direction": direction,
        "price": price, "quantity": qty, "trade_time": trade_time,
    }


class TestDedupe(unittest.TestCase):
    def test_already_reported_true(self):
        self.assertTrue(wa.already_reported(5, [1, 2, 5]))

    def test_already_reported_false(self):
        self.assertFalse(wa.already_reported(9, [1, 2, 5]))

    def test_remember_adds_new_id(self):
        updated = wa.remember_reported_id([1, 2], 3)
        self.assertEqual(updated, [1, 2, 3])

    def test_remember_does_not_duplicate(self):
        updated = wa.remember_reported_id([1, 2, 3], 2)
        self.assertEqual(updated, [1, 2, 3])

    def test_remember_caps_at_max(self):
        ids = list(range(wa.MAX_REMEMBERED_IDS_PER_SYMBOL))
        updated = wa.remember_reported_id(ids, 999999)
        self.assertEqual(len(updated), wa.MAX_REMEMBERED_IDS_PER_SYMBOL)
        self.assertIn(999999, updated)
        self.assertNotIn(0, updated)  # قدیمی‌ترین دور ریخته شده


class TestBuildWhaleAlert(unittest.TestCase):
    def test_no_trade_produces_no_alert(self):
        alert = wa.build_whale_alert(
            symbol="BTCUSDT", best_trade=None,
            window_start_utc="x", window_end_utc="y",
            reported_ids=[], threshold_usdt=10000,
        )
        self.assertIsNone(alert)

    def test_valid_trade_produces_alert(self):
        trade = make_trade(agg_id=42)
        alert = wa.build_whale_alert(
            symbol="BTCUSDT", best_trade=trade,
            window_start_utc="2026-01-01T00:00:00Z", window_end_utc="2026-01-01T01:00:00Z",
            reported_ids=[], threshold_usdt=10000,
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert["agg_trade_id"], 42)
        self.assertEqual(alert["symbol"], "BTCUSDT")
        self.assertEqual(alert["source"], "BINANCE_SPOT")

    def test_already_reported_trade_is_skipped(self):
        trade = make_trade(agg_id=42)
        alert = wa.build_whale_alert(
            symbol="BTCUSDT", best_trade=trade,
            window_start_utc="x", window_end_utc="y",
            reported_ids=[42], threshold_usdt=10000,
        )
        self.assertIsNone(alert)

    def test_missing_agg_trade_id_is_skipped(self):
        trade = make_trade()
        del trade["agg_trade_id"]
        alert = wa.build_whale_alert(
            symbol="BTCUSDT", best_trade=trade,
            window_start_utc="x", window_end_utc="y",
            reported_ids=[], threshold_usdt=10000,
        )
        self.assertIsNone(alert)


if __name__ == "__main__":
    unittest.main()
