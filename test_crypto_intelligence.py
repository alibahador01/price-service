import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto_intelligence as ci  # noqa: E402
from gemini_client import GeminiUnavailable  # noqa: E402
from groq_client import GroqUnavailable  # noqa: E402


def make_candles(n=80, start_close=100.0):
    # کندل‌ها طوری ساخته می‌شوند که آخرین‌شان همین الان بسته شده باشد، نه
    # از epoch 1970 - وگرنه Fact Block آن را stale می‌کند (رفتار صحیح،
    # نه باگ؛ این فقط داده تست را واقعی‌تر می‌کند).
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    last_close_time = now_ms - 2000
    candles = []
    for i in range(n):
        close = start_close + (i % 7) * 0.3
        offset = (n - 1 - i) * 3600_000
        candles.append({
            "open_time": last_close_time - offset - 3600_000 + 1,
            "close_time": last_close_time - offset,
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": 10.0, "quote_volume": 1000.0 + (i % 5) * 10,
        })
    return candles


def good_klines_fn(symbol):
    return make_candles()


def empty_klines_fn(symbol):
    return []


def no_trades_fn(symbol, start_ms, end_ms):
    return [], False


def make_valid_asset(symbol):
    return {
        "symbol": symbol, "overall_bias": "neutral",
        "bullish_scenario": "در صورت تثبیت بالای مقاومت اخیر، روند تقویت می‌شود.",
        "bearish_scenario": "شکست حمایت ریسک افت بیشتر را افزایش می‌دهد.",
        "conclusion": "وضعیت فعلاً خنثی و محتاطانه است.",
        "limitations": [], "evidence_refs": ["{}.indicators.rsi_14".format(symbol), "{}.price".format(symbol)],
    }


def make_full_output(provider="gemini"):
    return {
        "schema_version": "1.0", "provider_used": provider,
        "assets": [make_valid_asset(s) for s in ci.SYMBOLS],
    }


class TestCollectFactBlocks(unittest.TestCase):
    def test_builds_fact_block_for_each_symbol(self):
        fbs = ci.collect_fact_blocks(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            large_trade_threshold_usdt=10000,
        )
        self.assertEqual(set(fbs.keys()), set(ci.SYMBOLS))
        for s in ci.SYMBOLS:
            self.assertEqual(fbs[s]["data_quality"], "valid")

    def test_symbol_fetch_failure_does_not_crash_others(self):
        def flaky_klines(symbol):
            if symbol == "ETHUSDT":
                raise RuntimeError("network down")
            return make_candles()
        fbs = ci.collect_fact_blocks(
            fetch_klines_fn=flaky_klines, fetch_agg_trades_window_fn=no_trades_fn,
            large_trade_threshold_usdt=10000,
        )
        self.assertEqual(fbs["ETHUSDT"]["data_quality"], "invalid")
        self.assertEqual(fbs["BTCUSDT"]["data_quality"], "valid")


class TestRunCycleHappyPath(unittest.TestCase):
    def test_gemini_success_and_groq_pass_sends_message(self):
        def gemini_fn(system, payload, api_key):
            return make_full_output("gemini")

        def groq_fn(system, payload, api_key):
            return {"verdict": "pass", "unsupported_claims": [], "contradictions": []}

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "sent")
        self.assertIsNotNone(result["message_text"])
        self.assertIn("BTC / USDT", result["message_text"])


class TestRunCycleGuardFailures(unittest.TestCase):
    def test_gemini_output_fails_local_guard_is_skipped(self):
        def bad_gemini_fn(system, payload, api_key):
            out = make_full_output("gemini")
            out["assets"][0]["conclusion"] = "قیمت به ۱۲۳۴۵ رسید"  # عدد ممنوع -> Numeric Guard رد می‌کند
            return out

        def groq_fn(system, payload, api_key):
            return {"verdict": "pass"}

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=bad_gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["message_text"])

    def test_all_fact_blocks_invalid_skips_without_calling_ai(self):
        calls = {"gemini": 0, "groq": 0}

        def gemini_fn(system, payload, api_key):
            calls["gemini"] += 1
            return make_full_output()

        def groq_fn(system, payload, api_key):
            calls["groq"] += 1
            return {"verdict": "pass"}

        result = ci.run_cycle(
            fetch_klines_fn=empty_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "all_fact_blocks_invalid")
        self.assertEqual(calls["gemini"], 0)
        self.assertEqual(calls["groq"], 0)


class TestRunCycleReviewerRejectionAndCorrection(unittest.TestCase):
    def test_reviewer_reject_triggers_one_correction_that_succeeds(self):
        call_count = {"gemini": 0}

        def gemini_fn(system, payload, api_key):
            call_count["gemini"] += 1
            return make_full_output("gemini")

        def groq_fn(system, payload, api_key):
            if "previous_output" in payload:
                self.fail("reviewer should not be called again after correction in this flow")
            return {"verdict": "reject", "unsupported_claims": [{"symbol": "BTCUSDT"}]}

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        # تصحیح یک‌بار از Gemini درخواست می‌شود (بار دوم فراخوانی)
        self.assertEqual(call_count["gemini"], 2)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider_used"], "gemini_corrected")

    def test_reviewer_reject_and_correction_also_fails_skips(self):
        def gemini_fn(system, payload, api_key):
            out = make_full_output("gemini")
            if "previous_output" in payload:
                out["assets"][0]["conclusion"] = "عدد ۱۲۳"  # اصلاح هم خراب است
            return out

        def groq_fn(system, payload, api_key):
            return {"verdict": "reject"}

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "skipped")


class TestRunCycleProviderOutages(unittest.TestCase):
    def test_gemini_unavailable_groq_fallback_succeeds(self):
        def gemini_fn(system, payload, api_key):
            raise GeminiUnavailable("quota exceeded")

        def groq_fn(system, payload, api_key):
            return make_full_output("groq_fallback")

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider_used"], "groq_fallback")

    def test_both_providers_unavailable_skips_cleanly(self):
        def gemini_fn(system, payload, api_key):
            raise GeminiUnavailable("down")

        def groq_fn(system, payload, api_key):
            raise GroqUnavailable("down")

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_valid_model_output")
        self.assertIsNone(result["message_text"])

    def test_never_reuses_stale_analysis_as_fresh(self):
        # هیچ کش/حافظه‌ای از تحلیل قبلی به run_cycle داده نمی‌شود؛ هر skip
        # یعنی message_text=None، نه بازگرداندن متن قبلی.
        def gemini_fn(system, payload, api_key):
            raise GeminiUnavailable("down")

        def groq_fn(system, payload, api_key):
            raise GroqUnavailable("down")

        result = ci.run_cycle(
            fetch_klines_fn=good_klines_fn, fetch_agg_trades_window_fn=no_trades_fn,
            call_gemini_fn=gemini_fn, call_groq_fn=groq_fn,
            gemini_api_key="x", groq_api_key="y",
        )
        self.assertIsNone(result["message_text"])


if __name__ == "__main__":
    unittest.main()
