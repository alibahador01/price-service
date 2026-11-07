import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quant  # noqa: E402


class TestEma(unittest.TestCase):
    def test_ema_known_sequence_by_hand(self):
        # period=3, seed = SMA(1,2,3)=2 در ایندکس 2؛ k=2/(3+1)=0.5
        values = [1, 2, 3, 4, 5]
        series = quant.ema_series(values, 3)
        self.assertEqual(series[0], None)
        self.assertEqual(series[1], None)
        self.assertAlmostEqual(series[2], 2.0)
        self.assertAlmostEqual(series[3], 3.0)   # 4*0.5 + 2*0.5
        self.assertAlmostEqual(series[4], 4.0)   # 5*0.5 + 3*0.5

    def test_ema_insufficient_data_returns_none(self):
        self.assertIsNone(quant.ema_last([1, 2], 20))

    def test_ema_last_matches_series_tail(self):
        values = [float(i) for i in range(1, 60)]
        self.assertAlmostEqual(quant.ema_last(values, 20), quant.ema_series(values, 20)[-1])


class TestRsi(unittest.TestCase):
    def test_rsi_monotonic_increasing_is_100(self):
        closes = [100 + i for i in range(30)]  # فقط سود، بدون زیان
        self.assertEqual(quant.wilder_rsi(closes), 100.0)
        self.assertEqual(quant.rsi_status(100.0), "overbought")

    def test_rsi_monotonic_decreasing_is_0(self):
        closes = [200 - i for i in range(30)]  # فقط زیان، بدون سود
        self.assertEqual(quant.wilder_rsi(closes), 0.0)
        self.assertEqual(quant.rsi_status(0.0), "oversold")

    def test_rsi_insufficient_data(self):
        self.assertIsNone(quant.wilder_rsi([1, 2, 3]))
        self.assertEqual(quant.rsi_status(None), "insufficient_data")

    def test_rsi_self_consistency_against_reference_loop(self):
        # پیاده‌سازی مرجع مستقل (ساده و کند) برای cross-check همان فرمول Wilder
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                  46.03, 46.41, 46.22, 45.64]
        period = 14

        def reference_rsi(values, period):
            gains, losses = [], []
            for i in range(1, len(values)):
                d = values[i] - values[i - 1]
                gains.append(max(d, 0.0))
                losses.append(max(-d, 0.0))
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            for i in range(period, len(gains)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return round(100.0 - (100.0 / (1.0 + rs)), 2)

        expected = reference_rsi(closes, period)
        self.assertEqual(quant.wilder_rsi(closes, period), expected)
        # این سری عمدتاً صعودی است؛ فقط محدوده منطقی (نه مقدار دقیق حفظی) را
        # بررسی می‌کنیم تا ادعای نادرست «مقدار مرجع کتاب» ثبت نشود.
        self.assertTrue(0.0 <= expected <= 100.0)
        self.assertGreater(expected, 50.0)


class TestMacd(unittest.TestCase):
    def test_macd_insufficient_data_returns_none(self):
        self.assertIsNone(quant.macd([1.0] * 10))

    def test_macd_histogram_sign_matches_trend(self):
        closes = [100 + i * 0.5 for i in range(60)]  # روند صعودی ملایم و پیوسته
        result = quant.macd(closes)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["histogram"], result["line"] - result["signal"], places=5)
        if result["histogram"] > 0:
            self.assertEqual(result["trend"], "bullish")
        elif result["histogram"] < 0:
            self.assertEqual(result["trend"], "bearish")
        else:
            self.assertEqual(result["trend"], "neutral")

    def test_macd_flat_series_is_zero(self):
        closes = [100.0] * 60
        result = quant.macd(closes)
        self.assertAlmostEqual(result["line"], 0.0)
        self.assertAlmostEqual(result["signal"], 0.0)
        self.assertAlmostEqual(result["histogram"], 0.0)
        self.assertEqual(result["trend"], "neutral")


class TestRelativeVolume(unittest.TestCase):
    def test_relative_volume_basic(self):
        # ۲۰ کندل قبلی با حجم ۱۰۰، کندل آخر ۲۰۰ -> نسبت ۲.۰
        volumes = [100.0] * 20 + [200.0]
        self.assertAlmostEqual(quant.relative_quote_volume(volumes), 2.0)

    def test_relative_volume_insufficient_data(self):
        self.assertIsNone(quant.relative_quote_volume([100.0] * 5))

    def test_relative_volume_zero_average_is_none(self):
        volumes = [0.0] * 20 + [50.0]
        self.assertIsNone(quant.relative_quote_volume(volumes))

    def test_relative_volume_excludes_last_candle_from_average(self):
        # کندل آخر نباید در میانگین خودش حساب شود
        volumes = [100.0] * 19 + [1000.0] + [100.0]
        # میانگین ۲۰ کندل قبلی: (19*100 + 1000)/20 = 145 ؛ آخرین=100 -> نسبت ~0.6897
        self.assertAlmostEqual(quant.relative_quote_volume(volumes), round(100.0 / 145.0, 4))


class TestChange24h(unittest.TestCase):
    def test_change_24h_basic(self):
        closes = [100.0] * 24 + [110.0]  # 25 کندل؛ ایندکس -25 پایه است
        self.assertAlmostEqual(quant.change_24h_pct(closes), 10.0)

    def test_change_24h_insufficient_data(self):
        self.assertIsNone(quant.change_24h_pct([100.0] * 10))

    def test_change_24h_zero_base_is_none(self):
        closes = [0.0] * 24 + [10.0]
        self.assertIsNone(quant.change_24h_pct(closes))


class TestBuildIndicators(unittest.TestCase):
    def test_build_indicators_no_nan_no_zero_fill(self):
        closes = [100 + math.sin(i / 5.0) * 3 for i in range(80)]
        volumes = [1000 + (i % 7) * 10 for i in range(80)]
        indicators = quant.build_indicators(closes, volumes)
        for key in ("rsi_14", "ema20", "ema50", "macd_line", "macd_signal", "macd_histogram"):
            self.assertIsNotNone(indicators[key], key)
            self.assertFalse(math.isnan(indicators[key]))

    def test_build_indicators_missing_stays_none_not_zero(self):
        indicators = quant.build_indicators([1.0, 2.0], [10.0, 20.0])
        self.assertIsNone(indicators["rsi_14"])
        self.assertIsNone(indicators["ema20"])
        self.assertEqual(indicators["macd_trend"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
