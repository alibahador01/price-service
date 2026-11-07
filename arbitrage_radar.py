"""
arbitrage_radar.py
رادار اختلاف قیمت (بخش ۳.۹). این فایل فرمول Buy/Sell و QUOTE_SPREAD موجود
در render_app.py را دوباره پیاده‌سازی **نمی‌کند** — آن مقدار باید از همان
تابع موجود (`_poster_crypto_quote`) گرفته و به اینجا پاس داده شود. اینجا
فقط درصد اختلاف، جهت و تازگی را محاسبه می‌کند.

Benchmark جهانی تومانی = USDT/USD (از cache موجود CoinPaprika/CoinGecko
پروژه) × USD/IRT (منبع مستقل TGJU که در ۳.۹ اجازه افزوده‌شدنش داده شده،
چون کد موجود منبع تومانی دلار نداشت).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

DEFAULT_MAX_AGE_SECONDS = 3 * 60 * 60          # هر نرخ باید در ۳ ساعت اخیر معتبر شده باشد
DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS = 3 * 60 * 60  # دو نرخ نباید بیش از ۳ ساعت از هم فاصله زمانی داشته باشند

VOCHINO_SIDE_LABEL = "sell"  # طبق برچسب موجود در فهرست قیمت (𝙎𝙚𝙡𝙡)؛ عمداً ثابت و مستند


def compute_global_benchmark_toman(
    usdt_usd_rate: Optional[float], usd_irt_toman: Optional[float]
) -> Optional[float]:
    """هر USDT جهانی چند تومان ارزش دارد (benchmark غیر-ووچینویی)."""
    if usdt_usd_rate is None or usd_irt_toman is None:
        return None
    if usdt_usd_rate <= 0 or usd_irt_toman <= 0:
        return None
    return usdt_usd_rate * usd_irt_toman


def compute_difference_pct(
    vochino_rate_toman: Optional[float], global_benchmark_toman: Optional[float]
) -> Optional[float]:
    """فرمول دقیق بخش ۳.۹؛ مثبت یعنی ووچینو گران‌تر، منفی یعنی ارزان‌تر."""
    if vochino_rate_toman is None or global_benchmark_toman is None or global_benchmark_toman <= 0:
        return None
    return ((vochino_rate_toman - global_benchmark_toman) / global_benchmark_toman) * 100.0


def classify_direction(difference_pct: Optional[float]) -> str:
    if difference_pct is None:
        return "unavailable"
    if difference_pct > 0:
        return "vochino_more_expensive"
    if difference_pct < 0:
        return "vochino_cheaper"
    return "equal"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_seconds(ts: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())


def build_arbitrage_snapshot(
    *,
    vochino_rate_toman: Optional[float],
    vochino_side: str = VOCHINO_SIDE_LABEL,
    usdt_usd_rate: Optional[float],
    usdt_usd_updated_at: Optional[str],
    usd_irt_toman: Optional[float],
    usd_irt_updated_at: Optional[str],
    now: Optional[datetime] = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_skew_seconds: int = DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS,
) -> Dict[str, Any]:
    """یک اسنپ‌شات کامل با وضعیت valid/skip می‌سازد. هرگز چیزی را حدس یا
    hard-code نمی‌کند؛ اگر هر شرط رعایت نشود status=skip با reason مشخص."""
    now = now or datetime.now(timezone.utc)
    base: Dict[str, Any] = {
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "vochino_rate_toman": vochino_rate_toman,
        "vochino_side": vochino_side,
        "global_benchmark_toman": None,
        "difference_pct": None,
        "direction": "unavailable",
        "status": "skip",
        "reason": None,
    }

    if vochino_rate_toman is None or vochino_rate_toman <= 0:
        base["reason"] = "vochino_rate_missing_or_invalid"
        return base
    if usdt_usd_rate is None or usd_irt_toman is None:
        base["reason"] = "benchmark_source_missing"
        return base

    age_usdt = _age_seconds(usdt_usd_updated_at, now)
    age_irt = _age_seconds(usd_irt_updated_at, now)
    if age_usdt is None or age_irt is None:
        base["reason"] = "benchmark_timestamp_missing"
        return base
    if age_usdt > max_age_seconds or age_irt > max_age_seconds:
        base["reason"] = "benchmark_stale"
        return base

    dt_usdt = _parse_iso(usdt_usd_updated_at)
    dt_irt = _parse_iso(usd_irt_updated_at)
    if dt_usdt and dt_irt:
        skew = abs((dt_usdt - dt_irt).total_seconds())
        if skew > max_skew_seconds:
            base["reason"] = "benchmark_timestamps_too_far_apart"
            return base

    benchmark = compute_global_benchmark_toman(usdt_usd_rate, usd_irt_toman)
    if benchmark is None:
        base["reason"] = "benchmark_computation_failed"
        return base

    diff = compute_difference_pct(vochino_rate_toman, benchmark)
    base["global_benchmark_toman"] = round(benchmark, 2)
    base["difference_pct"] = round(diff, 3) if diff is not None else None
    base["direction"] = classify_direction(diff)
    base["status"] = "valid"
    base["reason"] = None
    return base
