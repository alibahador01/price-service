"""
crypto_intelligence.py
هماهنگ‌کننده تحلیل عمیق کریپتو (بخش ۴.۳: ترتیب ۹مرحله‌ای). هر فراخوانی
شبکه‌ای (Binance، Gemini، Groq) از بیرون تزریق می‌شود تا این فایل بدون
اینترنت هم قابل تست کامل باشد؛ render_app.py/central_scheduler توابع
واقعی را پاس می‌دهند.

مراحل (طبق بخش ۴.۳):
1) Python داده Binance را می‌گیرد و شاخص‌ها را حساب می‌کند.
2) Python چهار Fact Block را validate می‌کند.
3) Gemini خروجی ساختاریافته برای هر چهار دارایی می‌سازد.
4) Guardهای محلی (JSON/عدد/شواهد/طول/ساختار) بررسی می‌کنند.
5) Groq به‌عنوان بازبین مستقل، فقط خطا/ادعای بی‌پشتوانه را گزارش می‌کند.
6) اگر بازبین ایراد جدی یافت، حداکثر یک اصلاح از Gemini درخواست شود.
7) اگر بعد از اصلاح هم Guardها رد شدند، منتشر نشود (تحلیل قدیمی هم جایگزین نشود).
8) اگر Gemini در دسترس نبود، Groq با همان Schema جایگزین بسازد و باز هم Guard شود.
9) اگر هیچ Provider در دسترس نبود یا خروجی نامعتبر بود، آن نوبت skip شود.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import ai_guards
import binance_client
import fact_block as fact_block_mod
import message_templates
from gemini_client import GeminiUnavailable
from groq_client import GroqUnavailable

SYMBOLS = binance_client.ALLOWED_SYMBOLS  # ("BTCUSDT","ETHUSDT","TRXUSDT","TONUSDT")
AGG_TRADES_WINDOW_SECONDS = 60 * 60

GEMINI_SYSTEM_PROMPT = (
    "تو تحلیل\u200cگر تکنیکال و سناریومحور بازار کریپتو هستی.\n"
    "ورودی تو Fact Blockهای ساختاریافته و داده\u200cهای محاسبه\u200cشده توسط Python است.\n"
    "وظیفه تو تفسیر محتاطانه شواهد است؛ نه پیش\u200cبینی قطعی و نه تولید عدد.\n\n"
    "قوانین قطعی:\n"
    "1. Fact Block تنها منبع حقیقت است. از دانش عمومی برای ساختن وضعیت لحظه\u200cای بازار استفاده نکن.\n"
    "2. هیچ قیمت، درصد، سطح، مقدار اندیکاتور، احتمال عددی، هدف، حد ضرر یا حد سود جدید نساز.\n"
    "3. در متن توضیحی عدد ننویس؛ اعداد فقط توسط برنامه از Fact Block در قالب نهایی درج می\u200cشوند.\n"
    "4. هر ادعای تحلیلی باید به یک یا چند evidence_ref معتبر متصل باشد.\n"
    "5. فقط چهار نماد موجود در ورودی را تحلیل کن.\n"
    "6. برای هر نماد سه بخش مستقل برگردان: سناریوی صعودی، سناریوی نزولی، جمع\u200cبندی.\n"
    "7. سناریوها باید شرطی و قابل\u200cآزمون باشند و به شاخص\u200cهای موجود ارجاع دهند.\n"
    "8. اگر داده ناقص، stale، invalid یا ناکافی است، صریحاً insufficient_data برگردان.\n"
    "9. تناقض بین RSI، MACD، EMA و حجم را پنهان نکن؛ اگر شاخص\u200cها همسو نیستند، bias را neutral یا mixed اعلام کن.\n"
    "10. از واژه\u200cهایی مثل تضمین سود، بدون ریسک، قطعاً صعود می\u200cکند، سیگنال قطعی خرید یا فروش استفاده نکن.\n"
    "11. هیچ\u200cگونه توصیه شخصی سرمایه\u200cگذاری نده.\n"
    "12. فقط JSON مطابق Schema درخواستی خروجی بده؛ Markdown، توضیح اضافه یا HTML ممنوع است."
)

GROQ_REVIEWER_SYSTEM_PROMPT = (
    "نقش تو بازبین مستقل خروجی یک مدل دیگر (Gemini) است، نه تولیدکننده تحلیل تازه.\n"
    "به تو Fact Block اصلی، خروجی JSON مدل اول و فهرست evidence_ref مجاز داده می‌شود.\n"
    "قیمت یا تحلیل جدید اختراع نکن و Fact Block را تغییر نده.\n"
    "فقط همین JSON را برگردان:\n"
    "{\"verdict\": \"pass\"|\"reject\", \"unsupported_claims\": [...], "
    "\"contradictions\": [...], \"invalid_evidence_refs\": [...], \"notes\": \"...\"}"
)

GROQ_FALLBACK_SYSTEM_PROMPT = GEMINI_SYSTEM_PROMPT  # اگر Gemini نبود، Groq همان Schema را می‌سازد


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def collect_fact_blocks(
    *,
    fetch_klines_fn: Callable[[str], List[Dict[str, Any]]],
    fetch_agg_trades_window_fn: Callable[[str, int, int], Tuple[List[Dict[str, Any]], bool]],
    large_trade_threshold_usdt: float,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """مرحله ۱+۲: برای هر نماد کندل + رادار معاملات بزرگ را می‌گیرد، Fact
    Block می‌سازد. خطای یک نماد بقیه را متوقف نمی‌کند — همان نماد invalid
    می‌شود و سرویس ادامه پیدا می‌کند."""
    now = now or _now_utc()
    fact_blocks: Dict[str, Dict[str, Any]] = {}
    for symbol in SYMBOLS:
        try:
            candles = fetch_klines_fn(symbol)
        except Exception as exc:  # noqa: BLE001
            candles = []
            print("[CRYPTO_INTEL] klines fetch failed for {}: {}".format(symbol, str(exc)[:200]), flush=True)

        radar: Dict[str, Any] = {"status": "unavailable"}
        if candles:
            last_close_ms = candles[-1]["close_time"]
            window_start = last_close_ms - AGG_TRADES_WINDOW_SECONDS * 1000 + 1
            try:
                trades, truncated = fetch_agg_trades_window_fn(symbol, window_start, last_close_ms)
                radar = binance_client.compute_large_trade_radar(
                    trades,
                    threshold_usdt=large_trade_threshold_usdt,
                    window_start_utc=datetime.fromtimestamp(window_start / 1000.0, tz=timezone.utc).isoformat(),
                    window_end_utc=datetime.fromtimestamp(last_close_ms / 1000.0, tz=timezone.utc).isoformat(),
                    truncated=truncated,
                )
            except Exception as exc:  # noqa: BLE001
                print("[CRYPTO_INTEL] aggTrades fetch failed for {}: {}".format(symbol, str(exc)[:200]), flush=True)

        fact_blocks[symbol] = fact_block_mod.build_fact_block(
            symbol=symbol, source="BINANCE_SPOT", closed_candles=candles,
            large_trade_radar=radar, now=now,
        )
    return fact_blocks


def _fact_blocks_payload(fact_blocks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {"schema_version": "1.0", "assets": [fact_blocks[s] for s in SYMBOLS if s in fact_blocks]}


def _insufficient_output(fact_blocks: Dict[str, Dict[str, Any]], provider: str) -> Dict[str, Any]:
    """خروجی ایمن insufficient_data برای وقتی هیچ Provider موفق نشد ولی
    لازم باشد (در عمل مرحله ۹ این مسیر اصلاً پیام نمی‌فرستد، نه این خروجی)."""
    return {
        "schema_version": "1.0", "provider_used": provider,
        "assets": [
            {
                "symbol": s, "overall_bias": "insufficient_data",
                "bullish_scenario": "", "bearish_scenario": "", "conclusion": "",
                "limitations": ["no_provider_available"], "evidence_refs": [],
            }
            for s in SYMBOLS if s in fact_blocks
        ],
    }


def run_cycle(
    *,
    fetch_klines_fn: Callable[[str], List[Dict[str, Any]]],
    fetch_agg_trades_window_fn: Callable[[str, int, int], Tuple[List[Dict[str, Any]], bool]],
    call_gemini_fn: Callable[..., Dict[str, Any]],
    call_groq_fn: Callable[..., Dict[str, Any]],
    gemini_api_key: str,
    groq_api_key: str,
    large_trade_threshold_usdt: float = binance_client.DEFAULT_LARGE_TRADE_THRESHOLD_USDT,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """کل چرخه ۹مرحله‌ای. خروجی: {"status": "sent"|"skipped", "reason":...,
    "message_text": str|None, "fact_blocks":..., "model_output":...}"""
    now = now or _now_utc()

    # مرحله ۱+۲
    fact_blocks = collect_fact_blocks(
        fetch_klines_fn=fetch_klines_fn,
        fetch_agg_trades_window_fn=fetch_agg_trades_window_fn,
        large_trade_threshold_usdt=large_trade_threshold_usdt,
        now=now,
    )
    block_errors = {s: fact_block_mod.validate_fact_block(fb) for s, fb in fact_blocks.items()}
    if all(fact_blocks[s]["data_quality"] == "invalid" for s in fact_blocks):
        return {"status": "skipped", "reason": "all_fact_blocks_invalid",
                "fact_blocks": fact_blocks, "model_output": None, "message_text": None}

    payload = _fact_blocks_payload(fact_blocks)
    evidence_context = {
        s: sorted(ai_guards.build_evidence_paths(fb)) for s, fb in fact_blocks.items()
    }

    model_output: Optional[Dict[str, Any]] = None
    provider_used: Optional[str] = None
    gemini_failed = False

    # مرحله ۳: Gemini
    try:
        model_output = call_gemini_fn(
            GEMINI_SYSTEM_PROMPT, {"fact_blocks": payload, "evidence_refs": evidence_context},
            api_key=gemini_api_key,
        )
        provider_used = "gemini"
    except GeminiUnavailable as exc:
        gemini_failed = True
        print("[CRYPTO_INTEL] Gemini unavailable: {}".format(str(exc)[:200]), flush=True)

    # مرحله ۴: Guard محلی خروجی اول
    if model_output is not None:
        errors = ai_guards.guard_model_output(fact_blocks, model_output)
        if errors:
            print("[CRYPTO_INTEL] Gemini output failed guards: {}".format(errors[:5]), flush=True)
            model_output = None

    # مرحله ۵+۶: بازبینی Groq + حداکثر یک اصلاح از Gemini
    if model_output is not None and groq_api_key:
        try:
            verdict = call_groq_fn(
                GROQ_REVIEWER_SYSTEM_PROMPT,
                {"fact_blocks": payload, "model_output": model_output, "evidence_refs": evidence_context},
                api_key=groq_api_key,
            )
        except GroqUnavailable as exc:
            verdict = {"verdict": "pass", "notes": "groq_unavailable: {}".format(str(exc)[:150])}
            print("[CRYPTO_INTEL] Groq reviewer unavailable, proceeding with local guards only", flush=True)

        reviewed = ai_guards.apply_reviewer_verdict(model_output, verdict)
        if reviewed is None:
            # verdict=reject -> حداکثر یک تلاش اصلاح از Gemini (فقط اگر Gemini خودش موفق بود)
            model_output = None
            if not gemini_failed:
                try:
                    correction_payload = {
                        "fact_blocks": payload, "evidence_refs": evidence_context,
                        "previous_output": model_output, "reviewer_feedback": verdict,
                        "instruction": "خروجی قبلی رد شد؛ فقط یک‌بار طبق Guardها و evidence مجاز اصلاح کن.",
                    }
                    corrected = call_gemini_fn(GEMINI_SYSTEM_PROMPT, correction_payload, api_key=gemini_api_key)
                    if not ai_guards.guard_model_output(fact_blocks, corrected):
                        model_output = corrected
                        provider_used = "gemini_corrected"
                except GeminiUnavailable:
                    pass
        else:
            model_output = reviewed

    # مرحله ۸: اگر Gemini از ابتدا در دسترس نبود، Groq با همان Schema جایگزین بسازد
    if model_output is None and gemini_failed and groq_api_key:
        try:
            fallback = call_groq_fn(
                GROQ_FALLBACK_SYSTEM_PROMPT, {"fact_blocks": payload, "evidence_refs": evidence_context},
                api_key=groq_api_key,
            )
            if not ai_guards.guard_model_output(fact_blocks, fallback):
                model_output = fallback
                provider_used = "groq_fallback"
        except GroqUnavailable as exc:
            print("[CRYPTO_INTEL] Groq fallback also unavailable: {}".format(str(exc)[:200]), flush=True)

    # مرحله ۹: هیچ خروجی معتبری نساخته نشد -> این نوبت را skip کن
    if model_output is None:
        return {"status": "skipped", "reason": "no_valid_model_output",
                "fact_blocks": fact_blocks, "model_output": None, "message_text": None}

    final_errors = ai_guards.guard_model_output(fact_blocks, model_output)
    if final_errors:
        return {"status": "skipped", "reason": "final_guard_failed:{}".format(final_errors[:3]),
                "fact_blocks": fact_blocks, "model_output": None, "message_text": None}

    message_text = message_templates.render_crypto_analysis_message(fact_blocks, model_output)
    return {
        "status": "sent", "reason": None, "provider_used": provider_used,
        "fact_blocks": fact_blocks, "model_output": model_output, "message_text": message_text,
    }
