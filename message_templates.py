"""
message_templates.py
قالب‌های دقیق پیام (بخش ۱۷ سند) — ساختار/نویسه‌ها/خطوط عیناً حفظ شده‌اند؛
فقط مقادیر از Fact Block/منابع معتبر پر می‌شوند. هیچ عددی اینجا hard-code
نیست. Footer تبلیغاتی و کیبورد جداگانه و بیرون از این توابع اضافه می‌شوند
(promo_messages.append_promo_footer / keyboard.build_channel_keyboard).
"""
from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

import formatting

PLACEHOLDER = formatting.PLACEHOLDER
DIVIDER = "〰️〰️〰️〰️〰️〰️〰️〰️〰️"
FRAME_TOP = '╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐-𝑯𝒖𝒃 ➫ 〰╮'  # به‌روزرسانی برند: Vochino -> Vochino-Hub

RSI_LABEL_FA = {
    "overbought": "اشباع خرید", "oversold": "اشباع فروش",
    "neutral": "خنثی", "insufficient_data": "داده کافی نیست",
}
MACD_LABEL_FA = {
    "bullish": "صعودی", "bearish": "نزولی",
    "neutral": "خنثی", "insufficient_data": "داده کافی نیست",
}
SYMBOL_DISPLAY = {
    "BTCUSDT": "BTC / USDT", "ETHUSDT": "ETH / USDT",
    "TRXUSDT": "TRX / USDT", "TONUSDT": "TON / USDT",
}
SYMBOL_ORDER = ("BTCUSDT", "ETHUSDT", "TRXUSDT", "TONUSDT")


def _e(text: Optional[str]) -> str:
    """Layer 7 Output Guard: هر متن آزاد (از مدل یا خارجی) قبل از درج در
    HTML باید escape شود."""
    return html.escape(text or "", quote=False)


def _radar_dominant_text(large_trades: Dict[str, Any]) -> str:
    if not large_trades or large_trades.get("status") != "valid":
        return PLACEHOLDER
    buy_pct = large_trades.get("buy_percentage")
    sell_pct = large_trades.get("sell_percentage")
    if buy_pct is None or sell_pct is None:
        return PLACEHOLDER
    if buy_pct >= sell_pct:
        return "{} خرید".format(formatting.format_persian_pct(buy_pct))
    return "{} فروش".format(formatting.format_persian_pct(sell_pct))


def render_crypto_asset_block(fact_block: Dict[str, Any], model_asset: Dict[str, Any]) -> str:
    symbol = fact_block.get("symbol", "")
    display = SYMBOL_DISPLAY.get(symbol, symbol)
    indicators = fact_block.get("indicators") or {}
    rsi = indicators.get("rsi_14")
    rsi_status_label = RSI_LABEL_FA.get(indicators.get("rsi_status"), "داده کافی نیست")
    macd_label = MACD_LABEL_FA.get(indicators.get("macd_trend"), "داده کافی نیست")
    rsi_text = formatting.format_number(rsi, 1) if rsi is not None else PLACEHOLDER

    bullish = _e(model_asset.get("bullish_scenario"))
    bearish = _e(model_asset.get("bearish_scenario"))
    conclusion = _e(model_asset.get("conclusion"))
    if fact_block.get("data_quality") in ("invalid",) or model_asset.get("overall_bias") == "insufficient_data":
        bullish = bullish or "داده کافی برای این سناریو موجود نیست."
        bearish = bearish or "داده کافی برای این سناریو موجود نیست."
        conclusion = conclusion or "داده کافی برای جمع‌بندی موجود نیست."

    lines = [
        "🪙 <b>{}</b> | Binance Spot".format(_e(display)),
        "💰 قیمت: <code>{}</code> دلار".format(formatting.format_price(fact_block.get("price"))),
        "📉 تغییر ۲۴س: <code>{}</code>".format(formatting.format_pct(fact_block.get("change_24h_pct"))),
        "",
        DIVIDER,
        "📊 <b>Fact Block:</b>",
        "▪️ RSI: <code>{}</code> ({})".format(rsi_text, rsi_status_label),
        "▪️ MACD: <code>{}</code>".format(macd_label),
        "▪️ رادار نهنگ: <code>{}</code>".format(_radar_dominant_text(fact_block.get("large_trades") or {})),
        "",
        DIVIDER,
        "🧠 <b>تحلیل (Gemini/Groq):</b>",
        "🔹 <b>سناریوی صعودی:</b> {}".format(bullish),
        "🔹 <b>سناریوی نزولی:</b> {}".format(bearish),
        "🔹 <b>نتیجه‌گیری:</b> {}".format(conclusion),
        DIVIDER,
    ]
    return "\n".join(lines)


def render_crypto_analysis_message(
    fact_blocks: Dict[str, Dict[str, Any]], model_output: Dict[str, Any]
) -> str:
    assets_by_symbol = {a.get("symbol"): a for a in (model_output or {}).get("assets", [])}
    header_text = "\n".join([
        FRAME_TOP,
        '   🧠 تحلیل عمیق و سناریومحور',
        '╰〰💎 𓆩𓆩⚡𓆪𓆪 💎〰╯',
    ])

    blocks = []
    for symbol in SYMBOL_ORDER:
        fb = fact_blocks.get(symbol)
        asset = assets_by_symbol.get(symbol)
        if fb is None or asset is None:
            continue
        blocks.append(render_crypto_asset_block(fb, asset))
    body_text = "\n\n".join(blocks)

    footer_text = "📌 تحلیل الگوریتمی، نه توصیه مالی."
    return "{}\n\n{}\n{}".format(header_text, body_text, footer_text)


def render_market_pulse_message(snapshot: Dict[str, Any], date_fa: str, time_fa: str) -> str:
    fng = snapshot.get("fear_greed") or {}
    fng_code_value = (
        formatting.format_number(fng.get("value"), 0) if fng.get("status") == "valid" else PLACEHOLDER
    )
    fng_classification_suffix = (
        " ({})".format(_e(fng.get("classification"))) if fng.get("status") == "valid" and fng.get("classification") else ""
    )
    dominance_text = (
        "{}%".format(formatting.format_number(snapshot.get("btc_dominance_pct"), 1))
        if snapshot.get("btc_dominance_status") == "valid" else PLACEHOLDER
    )
    avg = (snapshot.get("average_24h_change") or {}).get("average_pct")
    avg_text = formatting.format_pct(avg) if avg is not None else PLACEHOLDER

    sources = snapshot.get("sources_used") or []
    source_label_map = {"alternative.me": "Alternative.me", "coingecko": "CoinGecko"}
    source_names = [source_label_map.get(s, s) for s in sources]
    source_line = "📌 داده از {}".format(" و ".join(source_names)) if source_names else "📌 داده در دسترس نبود"

    lines = [
        FRAME_TOP,
        '   📊 نبض بازار کریپتو',
        '╰〰💲 𓆩𓆩🌐𓆪𓆪 💲〰╯',
        "",
        "📅 {} | ⏰ {}".format(_e(date_fa), _e(time_fa)),
        "",
        DIVIDER,
        "🌡 شاخص ترس و طمع: <code>{}</code>{}".format(fng_code_value, fng_classification_suffix),
        "👑 دامیننس بیت‌کوین: <code>{}</code>".format(dominance_text),
        "📈 میانگین تغییر ۲۴س: <code>{}</code>".format(avg_text),
        DIVIDER,
        source_line,
    ]
    return "\n".join(lines)


def render_whale_alert_message(alert: Dict[str, Any], time_local: str) -> str:
    symbol = alert.get("symbol", "")
    display = SYMBOL_DISPLAY.get(symbol, symbol)
    direction_fa = "خرید (Aggressive Buy)" if alert.get("direction") == "buy" else "فروش (Aggressive Sell)"
    lines = [
        FRAME_TOP,
        '   🐋 هشدار معامله بزرگ',
        '╰〰💎 𓆩𓆩⚡𓆪𓆪 💎〰╯',
        "",
        DIVIDER,
        "🪙 نماد: <b>{}</b>".format(_e(display)),
        "📊 حجم معامله: <code>{}</code>".format(formatting.format_usdt(alert.get("notional_usdt"))),
        "📈 جهت: <b>{}</b>".format(direction_fa),
        "⏰ زمان: <code>{}</code>".format(_e(time_local)),
        DIVIDER,
        "📌 رادار معاملات بزرگ Binance Spot",
    ]
    return "\n".join(lines)


def render_arbitrage_message(snapshot: Dict[str, Any]) -> str:
    direction = snapshot.get("direction")
    diff_pct = snapshot.get("difference_pct")
    if direction == "vochino_more_expensive":
        diff_line = "🚨 اختلاف: <b>{} گران‌تر!</b>".format(
            formatting.format_number(abs(diff_pct), 1) + "%" if diff_pct is not None else PLACEHOLDER
        )
    elif direction == "vochino_cheaper":
        diff_line = "🚨 اختلاف: <b>{} ارزان‌تر!</b>".format(
            formatting.format_number(abs(diff_pct), 1) + "%" if diff_pct is not None else PLACEHOLDER
        )
    else:
        diff_line = "🚨 اختلاف: <b>{}</b>".format(PLACEHOLDER)

    lines = [
        FRAME_TOP,
        '   ⚡️ رادار اختلاف قیمت',
        '╰〰💲 𓆩𓆩🏦𓆪𓆪 💲〰╯',
        "",
        DIVIDER,
        "💵 تتر در ووچینو: <code>{}</code>".format(formatting.format_toman(snapshot.get("vochino_rate_toman"))),
        "🌐 تتر در بازار جهانی: <code>{}</code>".format(formatting.format_toman(snapshot.get("global_benchmark_toman"))),
        "",
        diff_line,
        DIVIDER,
        "📌 مقایسه لحظه‌ای ووچینو با نرخ جهانی",
    ]
    return "\n".join(lines)


def render_football_message(league_fa: str, home_team: str, away_team: str, kickoff_time_local: str, date_fa: str) -> str:
    """طبق بخش ۷.۹: قالب تک‌بازی (نه نمونه چندبازی بخش ۱۷) مرجع اجرایی
    است — عنوان «بازی منتخب امروز» مفرد و خط لیگ با پیشوند 🏆."""
    lines = [
        FRAME_TOP,
        '   ⚽ بازی منتخب امروز',
        '╰〰🏆 𓆩𓆩🏟𓆪𓆪 🏆〰╯',
        "",
        "📅 {}".format(_e(date_fa)),
        "",
        DIVIDER,
        "🏆 {}".format(_e(league_fa)),
        "⚽ {} 🆚 {}".format(_e(home_team), _e(away_team)),
        "⏰ {}".format(_e(kickoff_time_local)),
        DIVIDER,
    ]
    return "\n".join(lines)
