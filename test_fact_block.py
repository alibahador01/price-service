import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fact_block  # noqa: E402


def make_candles(n=80, start_close=100.0, start_time_ms=0, step_ms=3600_000):
    candles = []
    for i in range(n):
        close = start_close + math.sin(i / 5.0) * 2.0
        candles.append({
            "open_time": start_time_ms + i * step_ms,
            "close_time": start_time_ms + (i + 1) * step_ms - 1,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 10.0,
            "quote_volume": 1000.0 + (i % 5) * 10,
        })
    return candles


class TestBuildFactBlock(unittest.TestCase):
    def test_valid_snapshot_has_no_missing_fields(self):
        candles = make_candles(80)
        last_close_dt = datetime.fromtimestamp(candles[-1]["close_time"] / 1000.0, tz=timezone.utc)
        now = last_close_dt + timedelta(minutes=2)
        radar = {"status": "valid", "buy_percentage": 60.0, "sell_percentage": 40.0}
        fb = fact_block.build_fact_block(
            symbol="BTCUSDT", source="BINANCE_SPOT", closed_candles=candles,
            large_trade_radar=radar, now=now,
        )
        self.assertEqual(fb["data_quality"], "valid")
        self.assertEqual(fb["missing_fields"], [])
        self.assertEqual(fb["radar_status"], "valid")
        errors = fact_block.validate_fact_block(fb)
        self.assertEqual(errors, [])
        self.assertTrue(fact_block.is_valid(fb))

    def test_empty_candles_is_invalid_not_zero_filled(self):
        fb = fact_block.build_fact_block(
            symbol="ETHUSDT", source="BINANCE_SPOT", closed_candles=[],
            large_trade_radar={"status": "unavailable"},
        )
        self.assertEqual(fb["data_quality"], "invalid")
        self.assertIsNone(fb["price"])
        self.assertNotEqual(fb["price"], 0)  # هرگز صفر جایگزین نشود

    def test_stale_data_detected_by_age(self):
        candles = make_candles(80)
        last_close_dt = datetime.fromtimestamp(candles[-1]["close_time"] / 1000.0, tz=timezone.utc)
        far_future = last_close_dt + timedelta(hours=10)  # بیش از حد تازگی پیش‌فرض (۳ ساعت)
        radar = {"status": "unavailable"}
        fb = fact_block.build_fact_block(
            symbol="TRXUSDT", source="BINANCE_SPOT", closed_candles=candles,
            large_trade_radar=radar, now=far_future,
        )
        self.assertEqual(fb["data_quality"], "stale")

    def test_partial_when_indicators_missing_but_price_present(self):
        candles = make_candles(5)  # خیلی کم برای RSI/EMA/MACD
        fb = fact_block.build_fact_block(
            symbol="TONUSDT", source="BINANCE_SPOT", closed_candles=candles,
            large_trade_radar={"status": "unavailable"},
            now=datetime.fromtimestamp(candles[-1]["close_time"] / 1000.0, tz=timezone.utc),
        )
        self.assertEqual(fb["data_quality"], "partial")
        self.assertIn("indicators.rsi_14", fb["missing_fields"])
        self.assertIsNotNone(fb["price"])  # قیمت موجود است، فقط اندیکاتورها کمند


class TestValidateFactBlock(unittest.TestCase):
    def test_detects_missing_top_level_field(self):
        errors = fact_block.validate_fact_block({"schema_version": "1.0"})
        self.assertTrue(any("missing field" in e for e in errors))

    def test_detects_nan_price(self):
        fb = {
            "schema_version": "1.0", "snapshot_id": "x", "symbol": "BTCUSDT",
            "source": "BINANCE_SPOT", "timestamp_utc": "now",
            "price": float("nan"), "change_24h_pct": 1.0,
            "indicators": {}, "large_trades": {"status": "unavailable"},
            "data_quality": "valid", "missing_fields": [], "radar_status": "unavailable",
        }
        errors = fact_block.validate_fact_block(fb)
        self.assertTrue(any("NaN" in e for e in errors))

    def test_detects_rsi_out_of_range(self):
        fb = {
            "schema_version": "1.0", "snapshot_id": "x", "symbol": "BTCUSDT",
            "source": "BINANCE_SPOT", "timestamp_utc": "now",
            "price": 100.0, "change_24h_pct": 1.0,
            "indicators": {"rsi_14": 150.0}, "large_trades": {"status": "unavailable"},
            "data_quality": "valid", "missing_fields": [], "radar_status": "unavailable",
        }
        errors = fact_block.validate_fact_block(fb)
        self.assertTrue(any("rsi_14 out of range" in e for e in errors))

    def test_detects_percentages_not_summing_to_100(self):
        fb = {
            "schema_version": "1.0", "snapshot_id": "x", "symbol": "BTCUSDT",
            "source": "BINANCE_SPOT", "timestamp_utc": "now",
            "price": 100.0, "change_24h_pct": 1.0,
            "indicators": {}, "large_trades": {
                "status": "valid", "buy_percentage": 30.0, "sell_percentage": 30.0,
            },
            "data_quality": "valid", "missing_fields": [], "radar_status": "valid",
        }
        errors = fact_block.validate_fact_block(fb)
        self.assertTrue(any("do not sum to ~100" in e for e in errors))

    def test_valid_minimal_block_passes(self):
        fb = {
            "schema_version": "1.0", "snapshot_id": "x", "symbol": "BTCUSDT",
            "source": "BINANCE_SPOT", "timestamp_utc": "now",
            "price": 100.0, "change_24h_pct": 1.0,
            "indicators": {"rsi_14": 50.0}, "large_trades": {"status": "unavailable"},
            "data_quality": "valid", "missing_fields": [], "radar_status": "unavailable",
        }
        self.assertEqual(fact_block.validate_fact_block(fb), [])


if __name__ == "__main__":
    unittest.main()
