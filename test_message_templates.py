import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import message_templates as mt  # noqa: E402

# این رشته‌ها مستقیماً با اسکریپت از بخش ۱۷ سند (نمونه‌های اجباری) استخراج
# شده‌اند، نه با تایپ دستی، تا خطای کاراکتر یونیکد رخ ندهد.
EXPECTED_CRYPTO = (
    "╭〰 ➫ \U0001d495\U0001d44c\U0001d454\U0001d462\U0001d456\U0001d45b\U0001d45c ➫ 〰╮"
)


def _rebrand(text: str) -> str:
    """سند مرجع هنوز نام قدیمی را دارد؛ طبق درخواست صریح کاربر برند به
    Vochino-Hub⁰¹ تغییر کرد، پس این یک تفاوت عمدی و مستندشده است، نه خطا."""
    return text.replace("𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫", "𝑽𝒐𝒄𝒉𝒊𝒏𝒐-𝑯𝒖𝒃 ➫")


def load_spec_block(label_start):
    path = "/mnt/user-data/uploads/Vochino_Claude_Implementation_Spec_v3.md"
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        spec = f.read()
    start = spec.index(label_start)
    block_start = spec.index("```html\n", start) + len("```html\n")
    block_end = spec.index("```", block_start)
    return spec[block_start:block_end].rstrip("\n")


class TestFrameConstants(unittest.TestCase):
    def test_frame_top_is_nonempty_and_consistent_across_renders(self):
        # به‌جای تایپ مجدد گلیف‌های خاص، فقط سازگاری داخلی را تست می‌کنیم؛
        # تطابق کامل با سند در تست‌های زیر (در صورت وجود فایل سند) انجام می‌شود.
        self.assertTrue(mt.FRAME_TOP)
        self.assertIn(mt.FRAME_TOP, mt.render_market_pulse_message(
            {"fear_greed": {"status": "unavailable"}, "btc_dominance_status": "unavailable",
             "average_24h_change": {"average_pct": None}, "sources_used": []},
            "d", "t",
        ))


@unittest.skipIf(load_spec_block("### نمونه ۱:") is None, "spec document not available in this environment")
class TestAgainstSpecExamples(unittest.TestCase):
    def test_crypto_message_matches_spec_example_structure(self):
        # نمونه سند خام است (بدون escape)؛ چون Layer 7 Output Guard هر متن
        # آزاد مدل را escape می‌کند، تنها تفاوت مجاز همان "&" -> "&amp;"
        # است — این خودش رفتار صحیح Guard را تأیید می‌کند، نه یک باگ.
        expected = _rebrand(load_spec_block("### نمونه ۱:").rsplit("\n", 1)[0].replace("&", "&amp;"))
        fb = {
            "symbol": "BTCUSDT", "price": 77262.00, "change_24h_pct": -1.77,
            "indicators": {"rsi_14": 44.1, "rsi_status": "neutral", "macd_trend": "bearish"},
            "large_trades": {"status": "valid", "buy_percentage": 63.3, "sell_percentage": 36.7},
            "data_quality": "valid",
        }
        asset = {
            "symbol": "BTCUSDT", "overall_bias": "neutral",
            "bullish_scenario": "در صورت تثبیت بالای ۷۷,۵۰۰، مسیر برای مقاومت بعدی هموار است.",
            "bearish_scenario": "شکست حمایت ۷۶,۸۰۰، ریسک ریزش تا ۷۵,۲۰۰ را افزایش می‌دهد.",
            "conclusion": "استراتژی محتاطانه (Wait & See) منطقی‌تر است.",  # متن خام مدل؛ renderer خودش escape می‌کند
        }
        got = mt.render_crypto_analysis_message({"BTCUSDT": fb}, {"assets": [asset]})
        self.assertEqual(got, expected)

    def test_market_pulse_matches_spec_example(self):
        expected = _rebrand(load_spec_block("### نمونه ۲:").rsplit("\n", 1)[0])
        snap = {
            "fear_greed": {"status": "valid", "value": 72, "classification": "طمع"},
            "btc_dominance_status": "valid", "btc_dominance_pct": 58.7,
            "average_24h_change": {"average_pct": 2.84},
            "sources_used": ["alternative.me", "coingecko"],
        }
        got = mt.render_market_pulse_message(snap, "{تاریخ}", "{ساعت}")
        self.assertEqual(got, expected)

    def test_whale_alert_matches_spec_example(self):
        expected = _rebrand(load_spec_block("### نمونه ۳:").rsplit("\n", 1)[0])
        alert = {"symbol": "BTCUSDT", "notional_usdt": 500000, "direction": "buy"}
        got = mt.render_whale_alert_message(alert, "14:30")
        self.assertEqual(got, expected)


class TestOutputGuardEscaping(unittest.TestCase):
    def test_model_text_is_html_escaped(self):
        fb = {
            "symbol": "BTCUSDT", "price": 100.0, "change_24h_pct": 1.0,
            "indicators": {"rsi_14": 50.0, "rsi_status": "neutral", "macd_trend": "neutral"},
            "large_trades": {"status": "unavailable"}, "data_quality": "valid",
        }
        asset = {
            "symbol": "BTCUSDT", "overall_bias": "neutral",
            "bullish_scenario": "A < B & C", "bearish_scenario": "x", "conclusion": "y",
        }
        out = mt.render_crypto_asset_block(fb, asset)
        self.assertIn("A &lt; B &amp; C", out)
        self.assertNotIn("A < B & C", out)


class TestArbitrageDirection(unittest.TestCase):
    def test_direction_follows_sign_not_sample_wording(self):
        # طبق نکته اجرایی بخش ۱۷: برای 270440 در برابر 266120 باید «گران‌تر» باشد
        snap = {
            "vochino_rate_toman": 270440, "global_benchmark_toman": 266120,
            "direction": "vochino_more_expensive", "difference_pct": 1.6,
        }
        out = mt.render_arbitrage_message(snap)
        self.assertIn("گران‌تر", out)
        self.assertNotIn("ارزان‌تر", out)
        # ادعای «فرصت مناسب برای خرید» هرگز نباید drop شود مگر با محاسبه هزینه
        self.assertNotIn("فرصت مناسب", out)
        self.assertNotIn("فرصت قطعی", out)
        self.assertNotIn("سود تضمینی", out)

    def test_cheaper_direction(self):
        snap = {
            "vochino_rate_toman": 260000, "global_benchmark_toman": 266120,
            "direction": "vochino_cheaper", "difference_pct": -2.3,
        }
        out = mt.render_arbitrage_message(snap)
        self.assertIn("ارزان‌تر", out)

    def test_unavailable_direction_shows_placeholder_not_fabricated_number(self):
        snap = {"vochino_rate_toman": None, "global_benchmark_toman": None,
                "direction": "unavailable", "difference_pct": None}
        out = mt.render_arbitrage_message(snap)
        self.assertIn(mt.PLACEHOLDER, out)


class TestFootballFooterNotHardcoded(unittest.TestCase):
    def test_no_fee_claim_not_injected_automatically(self):
        # طبق نکات اجرایی بخش ۱۷: «بدون کارمزد» ادعای عمومی نیست و نباید در
        # هر پیام فوتبال خودکار درج شود.
        out = mt.render_football_message("لیگ انگلیس", "A", "B", "23:30", "{d}")
        self.assertNotIn("بدون کارمزد", out)
        self.assertNotIn("کارمزد صفر", out)

    def test_single_match_template_per_section_7_9_not_multi_match_sample(self):
        # بخش ۷.۹ صریحاً می‌گوید قالب تک‌بازی (نه نمونه چندبازی بخش ۱۷)
        # مرجع اجرایی است: عنوان مفرد «بازی منتخب امروز» و خط لیگ با 🏆.
        out = mt.render_football_message("Premier League", "Man United", "Liverpool", "23:30", "امروز")
        self.assertIn("بازی منتخب امروز", out)
        self.assertNotIn("بازی‌های منتخب امروز", out)
        self.assertIn("🏆 Premier League", out)
        self.assertEqual(out.count("🆚"), 1)  # دقیقاً یک بازی در هر caption


class TestCryptoAggregation(unittest.TestCase):
    def test_skips_symbol_missing_fact_block_or_model_asset(self):
        fb = {
            "symbol": "BTCUSDT", "price": 1.0, "change_24h_pct": 1.0,
            "indicators": {"rsi_14": 50.0, "rsi_status": "neutral", "macd_trend": "neutral"},
            "large_trades": {"status": "unavailable"}, "data_quality": "valid",
        }
        asset = {"symbol": "BTCUSDT", "overall_bias": "neutral",
                  "bullish_scenario": "x", "bearish_scenario": "y", "conclusion": "z"}
        out = mt.render_crypto_analysis_message({"BTCUSDT": fb}, {"assets": [asset]})
        self.assertIn("BTC / USDT", out)
        self.assertNotIn("ETH / USDT", out)

    def test_four_assets_all_rendered_in_fixed_order(self):
        def make(sym):
            return {
                "symbol": sym, "price": 1.0, "change_24h_pct": 1.0,
                "indicators": {"rsi_14": 50.0, "rsi_status": "neutral", "macd_trend": "neutral"},
                "large_trades": {"status": "unavailable"}, "data_quality": "valid",
            }, {"symbol": sym, "overall_bias": "neutral",
                "bullish_scenario": "x", "bearish_scenario": "y", "conclusion": "z"}

        fbs, assets = {}, []
        for sym in ("TONUSDT", "BTCUSDT", "TRXUSDT", "ETHUSDT"):  # ترتیب ورودی عمداً به‌هم‌ریخته
            fb, asset = make(sym)
            fbs[sym] = fb
            assets.append(asset)
        out = mt.render_crypto_analysis_message(fbs, {"assets": assets})
        pos_btc = out.index("BTC / USDT")
        pos_eth = out.index("ETH / USDT")
        pos_trx = out.index("TRX / USDT")
        pos_ton = out.index("TON / USDT")
        self.assertTrue(pos_btc < pos_eth < pos_trx < pos_ton)  # ترتیب ثابت طبق SYMBOL_ORDER


if __name__ == "__main__":
    unittest.main()
