import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_guards  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "TRXUSDT", "TONUSDT"]


def make_fact_block(symbol, rsi=50.0, price=100.0):
    return {
        "symbol": symbol,
        "price": price,
        "change_24h_pct": 1.5,
        "indicators": {"rsi_14": rsi, "ema20": 99.0, "ema50": 98.0,
                        "macd_line": 0.1, "macd_signal": 0.05, "macd_histogram": 0.05},
        "large_trades": {"status": "valid", "buy_percentage": 60.0, "sell_percentage": 40.0,
                          "buy_notional_usdt": 60000.0, "sell_notional_usdt": 40000.0},
    }


def make_valid_asset(symbol):
    return {
        "symbol": symbol,
        "overall_bias": "neutral",
        "bullish_scenario": "در صورت تثبیت بالای مقاومت اخیر، مسیر صعودی تقویت می‌شود.",
        "bearish_scenario": "شکست حمایت اخیر ریسک افت بیشتر را افزایش می‌دهد.",
        "conclusion": "در حال حاضر وضعیت خنثی و محتاطانه است.",
        "limitations": [],
        "evidence_refs": ["{}.indicators.rsi_14".format(symbol), "{}.price".format(symbol)],
    }


def make_full_output():
    return {
        "schema_version": "1.0",
        "provider_used": "gemini",
        "assets": [make_valid_asset(s) for s in SYMBOLS],
    }


class TestEvidenceGuard(unittest.TestCase):
    def test_build_evidence_paths_excludes_none_values(self):
        fb = make_fact_block("BTCUSDT")
        fb["indicators"]["macd_line"] = None
        paths = ai_guards.build_evidence_paths(fb)
        self.assertIn("BTCUSDT.price", paths)
        self.assertIn("BTCUSDT.indicators.rsi_14", paths)
        self.assertNotIn("BTCUSDT.indicators.macd_line", paths)

    def test_evidence_refs_valid_detects_invalid(self):
        paths = {"BTCUSDT.price"}
        invalid = ai_guards.evidence_refs_valid(["BTCUSDT.price", "ETHUSDT.price"], paths)
        self.assertEqual(invalid, ["ETHUSDT.price"])

    def test_cross_symbol_reference_rejected(self):
        fact_blocks = {s: make_fact_block(s) for s in SYMBOLS}
        output = make_full_output()
        output["assets"][0]["evidence_refs"] = ["ETHUSDT.price"]  # نماد اشتباه برای BTCUSDT
        errors = ai_guards.guard_model_output(fact_blocks, output)
        self.assertTrue(any("invalid evidence_refs" in e for e in errors))


class TestNumericGuard(unittest.TestCase):
    def test_text_contains_digits_latin(self):
        self.assertTrue(ai_guards.text_contains_digits("قیمت به 77500 رسید"))

    def test_text_contains_digits_persian(self):
        self.assertTrue(ai_guards.text_contains_digits("قیمت به ۷۷۵۰۰ رسید"))

    def test_text_without_digits_passes(self):
        self.assertFalse(ai_guards.text_contains_digits("روند بدون تغییر محسوس است"))

    def test_guard_rejects_output_with_number_in_text(self):
        fact_blocks = {s: make_fact_block(s) for s in SYMBOLS}
        output = make_full_output()
        output["assets"][0]["conclusion"] = "قیمت به ۷۷۵۰۰ دلار رسید"
        errors = ai_guards.guard_model_output(fact_blocks, output)
        self.assertTrue(any("contains a free-form number" in e for e in errors))


class TestSchemaGuard(unittest.TestCase):
    def test_valid_schema_passes(self):
        errors = ai_guards.validate_model_schema(make_full_output(), SYMBOLS)
        self.assertEqual(errors, [])

    def test_missing_assets_key(self):
        errors = ai_guards.validate_model_schema({}, SYMBOLS)
        self.assertTrue(errors)

    def test_invalid_bias_rejected(self):
        output = make_full_output()
        output["assets"][0]["overall_bias"] = "mooning"
        errors = ai_guards.validate_model_schema(output, SYMBOLS)
        self.assertTrue(any("invalid overall_bias" in e for e in errors))

    def test_symbol_outside_allowlist_rejected(self):
        output = make_full_output()
        output["assets"][0]["symbol"] = "DOGEUSDT"
        errors = ai_guards.validate_model_schema(output, SYMBOLS)
        self.assertTrue(any("not in allowlist" in e for e in errors))

    def test_missing_symbol_detected(self):
        output = make_full_output()
        output["assets"] = output["assets"][:3]  # یک نماد جا افتاده
        errors = ai_guards.validate_model_schema(output, SYMBOLS)
        self.assertTrue(any("missing assets for symbols" in e for e in errors))

    def test_full_pipeline_accepts_well_formed_output(self):
        fact_blocks = {s: make_fact_block(s) for s in SYMBOLS}
        errors = ai_guards.guard_model_output(fact_blocks, make_full_output())
        self.assertEqual(errors, [])

    def test_directional_bias_requires_evidence(self):
        fact_blocks = {s: make_fact_block(s) for s in SYMBOLS}
        output = make_full_output()
        output["assets"][0]["overall_bias"] = "bullish"
        output["assets"][0]["evidence_refs"] = []
        errors = ai_guards.guard_model_output(fact_blocks, output)
        self.assertTrue(any("without any evidence_ref" in e for e in errors))


class TestCrossModelReview(unittest.TestCase):
    def test_reject_verdict_drops_output(self):
        output = make_full_output()
        result = ai_guards.apply_reviewer_verdict(output, {"verdict": "reject"})
        self.assertIsNone(result)

    def test_pass_verdict_keeps_output_unchanged(self):
        output = make_full_output()
        result = ai_guards.apply_reviewer_verdict(output, {"verdict": "pass"})
        self.assertEqual(result, output)

    def test_unsupported_claim_downgrades_bias_to_neutral(self):
        output = make_full_output()
        output["assets"][0]["overall_bias"] = "bullish"
        verdict = {"verdict": "pass", "unsupported_claims": [{"symbol": "BTCUSDT", "note": "x"}]}
        result = ai_guards.apply_reviewer_verdict(output, verdict)
        self.assertEqual(result["assets"][0]["overall_bias"], "neutral")
        # نمادهای دیگر دست‌نخورده می‌مانند
        self.assertEqual(result["assets"][1]["overall_bias"], "neutral")  # اینجا هم neutral بود از ابتدا

    def test_insufficient_data_never_upgraded_by_downgrade_path(self):
        output = make_full_output()
        output["assets"][0]["overall_bias"] = "insufficient_data"
        verdict = {"verdict": "pass", "contradictions": [{"symbol": "BTCUSDT"}]}
        result = ai_guards.apply_reviewer_verdict(output, verdict)
        self.assertEqual(result["assets"][0]["overall_bias"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
