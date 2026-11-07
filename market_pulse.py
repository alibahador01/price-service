"""
market_pulse.py
نبض بازار (بخش ۳.۷). بدون AI؛ فقط عدد خام از سه منبع + میانگین‌گیری Python.
هر فیلد مستقل fail می‌شود؛ یک منبع خراب بقیه پیام را خراب نمی‌کند.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

FNG_MAX_AGE_SECONDS = 24 * 60 * 60  # شاخص روزانه است؛ اگر از یک روز قدیمی‌تر شد stale
CHANGE_24H_SYMBOLS = ("bitcoin", "ethereum", "tron", "the-open-network")


def compute_average_24h_change(values_by_asset: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """میانگین فقط از مقادیر معتبر؛ اگر هیچ‌کدام معتبر نبود average=None و
    sample_count=0 (هرگز صفر جایگزین عدد گم‌شده نمی‌شود)."""
    valid = [v for v in values_by_asset.values() if v is not None]
    if not valid:
        return {"average_pct": None, "sample_count": 0, "sample_assets": []}
    avg = sum(valid) / len(valid)
    sample_assets = [k for k, v in values_by_asset.items() if v is not None]
    return {"average_pct": round(avg, 2), "sample_count": len(valid), "sample_assets": sample_assets}


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_fresh(ts: Optional[str], now: datetime, max_age_seconds: int) -> bool:
    dt = _parse_iso(ts)
    if dt is None:
        return False
    return (now - dt).total_seconds() <= max_age_seconds


def build_market_pulse_snapshot(
    *,
    fng_value: Optional[int],
    fng_classification: Optional[str],
    fng_timestamp_utc: Optional[str],
    btc_dominance_pct: Optional[float],
    change_24h_by_asset: Optional[Dict[str, Optional[float]]],
    now: Optional[datetime] = None,
    fng_max_age_seconds: int = FNG_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sources_used: List[str] = []

    fng_ok = fng_value is not None and fng_classification is not None
    if fng_ok and not _is_fresh(fng_timestamp_utc, now, fng_max_age_seconds):
        fng_ok = False  # داده قدیمی را به‌جای تازه جا نزن
    if fng_ok:
        sources_used.append("alternative.me")

    dominance_ok = btc_dominance_pct is not None
    if dominance_ok:
        sources_used.append("coingecko")

    change_result = compute_average_24h_change(change_24h_by_asset or {})
    if change_result["average_pct"] is not None and "coingecko" not in sources_used:
        sources_used.append("coingecko")

    return {
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "fear_greed": {
            "value": fng_value if fng_ok else None,
            "classification": fng_classification if fng_ok else None,
            "status": "valid" if fng_ok else "unavailable",
        },
        "btc_dominance_pct": btc_dominance_pct if dominance_ok else None,
        "btc_dominance_status": "valid" if dominance_ok else "unavailable",
        "average_24h_change": change_result,
        "sources_used": sources_used,
        "has_any_data": bool(fng_ok or dominance_ok or change_result["average_pct"] is not None),
    }
