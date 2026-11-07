"""
quant.py
موتور محاسبات قطعی تحلیل کریپتو (بخش ۳.۴ سند). هیچ مدل زبانی اجازه محاسبه
این مقادیر را ندارد؛ همه چیز اینجا با Python استاندارد و قابل تست واحد انجام
می‌شود. به‌عمد از pandas/numpy استفاده نشده (طبق سند، برای این حجم داده لازم
نیست).

ورودی همه توابع، لیست کندل‌های **بسته‌شده** و مرتب از قدیم به جدید است؛
حذف آخرین کندل باز باید قبل از فراخوانی این توابع در binance_client انجام
شده باشد.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

RSI_PERIOD = 14
EMA_FAST = 12
EMA_SLOW = 26
EMA_SIGNAL = 9
EMA_SHORT = 20
EMA_LONG = 50
RELATIVE_VOLUME_LOOKBACK = 20
CHANGE_24H_HOURS = 24  # کندل‌ها یک‌ساعته‌اند؛ ۲۴ کندل = ۲۴ ساعت


def _clean_floats(values: Sequence[float]) -> List[float]:
    out: List[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return []
        if math.isnan(f) or math.isinf(f):
            return []
        out.append(f)
    return out


def wilder_rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> Optional[float]:
    """RSI به روش Wilder (نه میانگین ساده). حداقل period+1 کندل بسته‌شده لازم
    است. seed اول میانگین ساده سود/زیان دوره اول است؛ از آن به بعد میانگین
    نمایی Wilder (smoothing factor = 1/period) اعمال می‌شود."""
    values = _clean_floats(closes)
    if len(values) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def rsi_status(rsi_value: Optional[float]) -> str:
    if rsi_value is None:
        return "insufficient_data"
    if rsi_value >= 70:
        return "overbought"
    if rsi_value <= 30:
        return "oversold"
    return "neutral"


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    """سری کامل EMA با seed میانگین ساده دوره اول؛ قبل از warm-up مقدار None
    برمی‌گردد تا مقدار خام جای EMA نهایی منتشر نشود."""
    data = _clean_floats(values)
    out: List[Optional[float]] = [None] * len(data)
    if len(data) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(data[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(data)):
        prev = data[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def ema_last(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    if not series or series[-1] is None:
        return None
    return round(series[-1], 8)


def macd(
    closes: Sequence[float],
    fast: int = EMA_FAST,
    slow: int = EMA_SLOW,
    signal_period: int = EMA_SIGNAL,
) -> Optional[Dict[str, object]]:
    """MACD line = EMA(fast) - EMA(slow)؛ signal = EMA(signal_period) روی خود
    خط MACD؛ histogram = line - signal. برای داده کم (کمتر از slow+signal
    کندل بسته‌شده)، None برمی‌گردد تا مقدار warm-up نشده منتشر نشود."""
    data = _clean_floats(closes)
    if len(data) < slow + signal_period:
        return None
    ema_fast_series = ema_series(data, fast)
    ema_slow_series = ema_series(data, slow)
    macd_line_series: List[float] = []
    for f, s in zip(ema_fast_series, ema_slow_series):
        if f is None or s is None:
            continue
        macd_line_series.append(f - s)
    if len(macd_line_series) < signal_period:
        return None
    signal_series = ema_series(macd_line_series, signal_period)
    if not signal_series or signal_series[-1] is None:
        return None
    line = round(macd_line_series[-1], 6)
    signal_value = round(signal_series[-1], 6)
    histogram = round(line - signal_value, 6)
    if histogram > 0:
        trend = "bullish"
    elif histogram < 0:
        trend = "bearish"
    else:
        trend = "neutral"
    return {"line": line, "signal": signal_value, "histogram": histogram, "trend": trend}


def relative_quote_volume(
    quote_volumes: Sequence[float], lookback: int = RELATIVE_VOLUME_LOOKBACK
) -> Optional[float]:
    """حجم quote کندل آخر تقسیم بر میانگین quote-volume بیست کندل *قبل* از
    کندل آخر (خود کندل آخر در میانگین حساب نمی‌شود)."""
    data = _clean_floats(quote_volumes)
    if len(data) < lookback + 1:
        return None
    last = data[-1]
    window = data[-(lookback + 1):-1]
    if not window:
        return None
    avg = sum(window) / len(window)
    if avg <= 0:
        return None
    return round(last / avg, 4)


def change_24h_pct(closes: Sequence[float], hours: int = CHANGE_24H_HOURS) -> Optional[float]:
    """تغییر close آخرین کندل بسته‌شده نسبت به close مربوط به `hours` کندل
    قبل (کندل‌های ۱ساعته → ۲۴ کندل = ۲۴ ساعت). روش: close-to-close."""
    data = _clean_floats(closes)
    if len(data) < hours + 1:
        return None
    base = data[-(hours + 1)]
    last = data[-1]
    if base == 0:
        return None
    return round(((last - base) / base) * 100.0, 2)


def build_indicators(
    closes: Sequence[float], quote_volumes: Sequence[float]
) -> Dict[str, object]:
    """همه شاخص‌های یک نماد را در یک دیکشنری آماده می‌کند (برای Fact Block)."""
    rsi_value = wilder_rsi(closes)
    ema20 = ema_last(closes, EMA_SHORT)
    ema50 = ema_last(closes, EMA_LONG)
    macd_result = macd(closes)
    rel_vol = relative_quote_volume(quote_volumes)
    return {
        "rsi_14": rsi_value,
        "rsi_status": rsi_status(rsi_value),
        "ema20": ema20,
        "ema50": ema50,
        "macd_line": macd_result["line"] if macd_result else None,
        "macd_signal": macd_result["signal"] if macd_result else None,
        "macd_histogram": macd_result["histogram"] if macd_result else None,
        "macd_trend": macd_result["trend"] if macd_result else "insufficient_data",
        "relative_quote_volume_20": rel_vol,
    }
