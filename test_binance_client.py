import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import binance_client as bc  # noqa: E402


def make_kline_row(open_time, close, close_time, quote_volume=1000.0):
    return [open_time, str(close), str(close + 1), str(close - 1), str(close),
            "10.0", close_time, str(quote_volume), 100, "5.0", "500.0", "0"]


class TestParsing(unittest.TestCase):
    def test_parse_kline_row_valid(self):
        row = make_kline_row(1000, 50000.0, 4600000)
        parsed = bc.parse_kline_row(row)
        self.assertEqual(parsed["open_time"], 1000)
        self.assertEqual(parsed["close"], 50000.0)
        self.assertEqual(parsed["close_time"], 4600000)
        self.assertEqual(parsed["quote_volume"], 1000.0)

    def test_parse_kline_row_invalid_short_row(self):
        self.assertIsNone(bc.parse_kline_row([1, 2]))

    def test_parse_kline_row_invalid_types(self):
        self.assertIsNone(bc.parse_kline_row(["x", "y", "z", "a", "b", "c", "d", "e"]))

    def test_validate_symbol(self):
        self.assertTrue(bc.validate_symbol("BTCUSDT"))
        self.assertFalse(bc.validate_symbol("DOGEUSDT"))

    def test_parse_agg_trade_row(self):
        row = {"a": 1, "p": "100.5", "q": "2.0", "T": 123456, "m": False}
        parsed = bc.parse_agg_trade_row(row)
        self.assertEqual(parsed["price"], 100.5)
        self.assertEqual(parsed["quantity"], 2.0)
        self.assertEqual(parsed["is_buyer_maker"], False)

    def test_parse_agg_trade_row_missing_field(self):
        self.assertIsNone(bc.parse_agg_trade_row({"a": 1, "p": "1"}))


class TestDropOpenCandle(unittest.TestCase):
    def test_drops_last_if_still_open(self):
        candles = [
            {"close_time": 1000, "close": 1.0},
            {"close_time": 2000, "close": 2.0},
        ]
        result = bc.drop_open_candle(candles, now_ms=1500)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["close_time"], 1000)

    def test_keeps_all_if_last_already_closed(self):
        candles = [
            {"close_time": 1000, "close": 1.0},
            {"close_time": 2000, "close": 2.0},
        ]
        result = bc.drop_open_candle(candles, now_ms=5000)
        self.assertEqual(len(result), 2)

    def test_empty_list(self):
        self.assertEqual(bc.drop_open_candle([], now_ms=1000), [])


class TestLargeTradeRadar(unittest.TestCase):
    def test_basic_buy_sell_split(self):
        trades = [
            {"price": 100.0, "quantity": 200.0, "is_buyer_maker": False},  # 20000 buy
            {"price": 100.0, "quantity": 100.0, "is_buyer_maker": True},   # 10000 sell
            {"price": 1.0, "quantity": 1.0, "is_buyer_maker": False},      # below threshold, ignored
        ]
        radar = bc.compute_large_trade_radar(trades, threshold_usdt=10000)
        self.assertEqual(radar["status"], "valid")
        self.assertAlmostEqual(radar["buy_notional_usdt"], 20000.0)
        self.assertAlmostEqual(radar["sell_notional_usdt"], 10000.0)
        self.assertAlmostEqual(radar["buy_percentage"], 66.67, places=1)
        self.assertAlmostEqual(radar["sell_percentage"], 33.33, places=1)

    def test_no_qualifying_trades_is_unavailable(self):
        trades = [{"price": 1.0, "quantity": 1.0, "is_buyer_maker": False}]
        radar = bc.compute_large_trade_radar(trades, threshold_usdt=10000)
        self.assertEqual(radar["status"], "unavailable")
        self.assertIsNone(radar["buy_percentage"])

    def test_truncated_never_produces_percentage(self):
        trades = [{"price": 100.0, "quantity": 500.0, "is_buyer_maker": False}]
        radar = bc.compute_large_trade_radar(trades, threshold_usdt=10000, truncated=True)
        self.assertEqual(radar["status"], "incomplete")
        self.assertIsNone(radar["buy_percentage"])
        self.assertIsNone(radar["sell_percentage"])

    def test_is_buyer_maker_direction_mapping_not_reversed(self):
        # isBuyerMaker=False یعنی خریدار taker بوده (فشار خرید)
        buy_trade = [{"price": 100.0, "quantity": 200.0, "is_buyer_maker": False}]
        radar = bc.compute_large_trade_radar(buy_trade, threshold_usdt=10000)
        self.assertEqual(radar["buy_percentage"], 100.0)
        self.assertEqual(radar["sell_notional_usdt"], 0.0)
        self.assertEqual(radar["sell_percentage"], 0.0)


class TestSelectLargestTrade(unittest.TestCase):
    def test_picks_largest_qualifying(self):
        trades = [
            {"price": 100.0, "quantity": 101.0, "is_buyer_maker": False},  # 10100
            {"price": 100.0, "quantity": 500.0, "is_buyer_maker": True},   # 50000 largest
            {"price": 1.0, "quantity": 1.0, "is_buyer_maker": False},      # too small
        ]
        best = bc.select_largest_trade(trades, threshold_usdt=10000)
        self.assertIsNotNone(best)
        self.assertEqual(best["notional_usdt"], 50000.0)
        self.assertEqual(best["direction"], "sell")

    def test_returns_none_if_nothing_qualifies(self):
        trades = [{"price": 1.0, "quantity": 1.0, "is_buyer_maker": False}]
        self.assertIsNone(bc.select_largest_trade(trades, threshold_usdt=10000))


class TestFetchKlinesMocked(unittest.TestCase):
    def test_fetch_klines_rejects_unknown_symbol(self):
        with self.assertRaises(ValueError):
            bc.fetch_klines("DOGEUSDT")

    @mock.patch("binance_client.request_with_retry")
    def test_fetch_klines_drops_open_candle_and_sorts(self, mock_request):
        rows = [make_kline_row(i * 1000, float(i), i * 1000 + 999) for i in range(1, 103)]
        # آخرین کندل را "باز" کن (close_time در آینده)
        far_future = 99999999999999
        rows[-1] = make_kline_row(103000, 999.0, far_future)
        mock_response = mock.Mock()
        mock_response.json.return_value = rows
        mock_request.return_value = mock_response

        result = bc.fetch_klines("BTCUSDT")
        # آخرین کندل (باز) باید حذف شده باشد
        self.assertTrue(all(c["close_time"] != far_future for c in result))
        self.assertLessEqual(len(result), bc.KLINES_REQUIRED_CLOSED)

    @mock.patch("binance_client.request_with_retry")
    def test_fetch_agg_trades_window_single_page(self, mock_request):
        rows = [{"a": i, "p": "100.0", "q": "1.0", "T": 1000 + i, "m": False} for i in range(5)]
        mock_response = mock.Mock()
        mock_response.json.return_value = rows
        mock_request.return_value = mock_response
        trades, truncated = bc.fetch_agg_trades_window("BTCUSDT", 1000, 2000)
        self.assertEqual(len(trades), 5)
        self.assertFalse(truncated)

    @mock.patch("binance_client.request_with_retry")
    def test_fetch_agg_trades_window_detects_truncation_at_page_cap(self, mock_request):
        full_page = [
            {"a": i, "p": "100.0", "q": "1.0", "T": 1000 + i, "m": False}
            for i in range(bc.AGG_TRADES_PAGE_LIMIT)
        ]
        mock_response = mock.Mock()
        mock_response.json.return_value = full_page
        mock_request.return_value = mock_response  # هر صفحه پر برمی‌گردد؛ هرگز به endTime نمی‌رسد
        trades, truncated = bc.fetch_agg_trades_window(
            "BTCUSDT", 1000, 1000 + bc.AGG_TRADES_PAGE_LIMIT * 100, max_pages=3
        )
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
