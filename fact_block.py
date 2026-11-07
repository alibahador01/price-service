"""
fact_block.py
ساخت و اعتبارسنجی Fact Block (بخش ۳.۵ و ۳.۶ سند). این ساختار تنها مرجع
عددی است که به مدل‌های زبانی داده می‌شود؛ هیچ عددی در متن تولیدی مدل
پذیرفته نمی‌شود، فقط همین داده‌ها.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import quant

SCHEMA_VERSION = "1.0"
FRESHNESS_LIMIT_SECONDS_DEFAULT = 3 * 60 * 60  # اگر snapshot از این قدیمی‌تر شد، stale
VALID_DATA_QUALITY = ("valid", "partial", "stale", "invalid")
VALID_RADAR_STATUS = ("valid", "incomplete", "unavailable")


def _is_finite_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return not (math.isnan(value) or math.isinf(value))


def build_fact_block(
    *,
    symbol: str,
    source: str,
    closed_candles: Sequence[Dict[str, Any]],
    large_trade_radar: Dict[str, Any],
    now: Optional[datetime] = None,
    freshness_limit_seconds: int = FRESHNESS_LIMIT_SECONDS_DEFAULT,
) -> Dict[str, Any]:
    """Fact Block یک نماد را از کندل‌های بسته‌شده و رادار معاملات بزرگ
    می‌سازد. هیچ مقدار گم‌شده‌ای صفر نمی‌شود؛ `None` می‌ماند."""
    now = now or datetime.now(timezone.utc)
    missing_fields: List[str] = []

    closes = [c["close"] for c in closed_candles if "close" in c]
    volumes = [c["quote_volume"] for c in closed_candles if "quote_volume" in c]
    last_candle = closed_candles[-1] if closed_candles else None

    price = closes[-1] if closes else None
    if price is None:
        missing_fields.append("price")

    change_pct = quant.change_24h_pct(closes) if closes else None
    if change_pct is None:
        missing_fields.append("change_24h_pct")

    indicators = quant.build_indicators(closes, volumes) if closes else {
        "rsi_14": None, "rsi_status": "insufficient_data", "ema20": None, "ema50": None,
        "macd_line": None, "macd_signal": None, "macd_histogram": None,
        "macd_trend": "insufficient_data", "relative_quote_volume_20": None,
    }
    for key in ("rsi_14", "ema20", "ema50", "macd_line"):
        if indicators.get(key) is None:
            missing_fields.append("indicators.{}".format(key))

    last_closed_candle_utc = None
    data_age_seconds: Optional[float] = None
    if last_candle is not None and "close_time" in last_candle:
        last_dt = datetime.fromtimestamp(last_candle["close_time"] / 1000.0, tz=timezone.utc)
        last_closed_candle_utc = last_dt.isoformat().replace("+00:00", "Z")
        data_age_seconds = max(0.0, (now - last_dt).total_seconds())

    radar_status = large_trade_radar.get("status", "unavailable") if large_trade_radar else "unavailable"
    if radar_status not in VALID_RADAR_STATUS:
        radar_status = "unavailable"

    if price is None or not closed_candles:
        data_quality = "invalid"
    elif data_age_seconds is not None and data_age_seconds > freshness_limit_seconds:
        data_quality = "stale"
    elif missing_fields:
        data_quality = "partial"
    else:
        data_quality = "valid"

    snapshot_id = "{}-{}".format(
        symbol,
        (last_closed_candle_utc or now.isoformat()).replace("-", "").replace(":", "").replace("Z", "Z"),
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "source": source,
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "last_closed_candle_utc": last_closed_candle_utc,
        "price": price,
        "change_24h_pct": change_pct,
        "indicators": indicators,
        "large_trades": large_trade_radar or {"status": "unavailable"},
        "data_quality": data_quality,
        "data_age_seconds": data_age_seconds,
        "missing_fields": missing_fields,
        "radar_status": radar_status,
    }


def validate_fact_block(fact_block: Dict[str, Any]) -> List[str]:
    """لیست خطاها را برمی‌گرداند؛ لیست خالی یعنی معتبر. این تابع همان Source
    Guard / Quant Guard سطح داده است و باید پیش از ارسال به هر مدل اجرا شود."""
    errors: List[str] = []
    required_top = (
        "schema_version", "snapshot_id", "symbol", "source", "timestamp_utc",
        "price", "change_24h_pct", "indicators", "large_trades",
        "data_quality", "missing_fields", "radar_status",
    )
    for key in required_top:
        if key not in fact_block:
            errors.append("missing field: {}".format(key))

    if fact_block.get("data_quality") not in VALID_DATA_QUALITY:
        errors.append("invalid data_quality: {}".format(fact_block.get("data_quality")))
    if fact_block.get("radar_status") not in VALID_RADAR_STATUS:
        errors.append("invalid radar_status: {}".format(fact_block.get("radar_status")))

    price = fact_block.get("price")
    if price is not None and not _is_finite_number(price):
        errors.append("price is NaN/Infinity")
    if price is not None and price < 0:
        errors.append("price is negative")

    change_pct = fact_block.get("change_24h_pct")
    if change_pct is not None and not _is_finite_number(change_pct):
        errors.append("change_24h_pct is NaN/Infinity")

    indicators = fact_block.get("indicators") or {}
    rsi = indicators.get("rsi_14")
    if rsi is not None:
        if not _is_finite_number(rsi):
            errors.append("rsi_14 is NaN/Infinity")
        elif not (0.0 <= rsi <= 100.0):
            errors.append("rsi_14 out of range 0..100")

    for key in ("ema20", "ema50", "macd_line", "macd_signal", "macd_histogram", "relative_quote_volume_20"):
        value = indicators.get(key)
        if value is not None and not _is_finite_number(value):
            errors.append("indicators.{} is NaN/Infinity".format(key))

    rel_vol = indicators.get("relative_quote_volume_20")
    if rel_vol is not None and rel_vol < 0:
        errors.append("relative_quote_volume_20 is negative")

    large_trades = fact_block.get("large_trades") or {}
    if large_trades.get("status") == "valid":
        buy_pct = large_trades.get("buy_percentage")
        sell_pct = large_trades.get("sell_percentage")
        if buy_pct is None or sell_pct is None:
            errors.append("large_trades valid but percentages missing")
        elif not math.isclose(buy_pct + sell_pct, 100.0, abs_tol=0.5):
            errors.append("large_trades percentages do not sum to ~100")

    return errors


def is_valid(fact_block: Dict[str, Any]) -> bool:
    return len(validate_fact_block(fact_block)) == 0
