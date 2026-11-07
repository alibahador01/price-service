"""
whale_alerts.py
هشدار معامله بزرگ (بخش ۳.۸) — جدا از Large Trade Radar داخل Fact Block.
انتخاب بزرگ‌ترین معامله معتبر با binance_client.select_largest_trade انجام
می‌شود؛ این فایل فقط dedupe (بر اساس agg_trade_id) و ساخت رکورد هشدار را
اضافه می‌کند. اگر معامله واجدشرایطی نبود، هیچ پیامی ساخته نمی‌شود.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

MAX_REMEMBERED_IDS_PER_SYMBOL = 500  # جلوگیری از رشد نامحدود State


def already_reported(trade_id: int, reported_ids: Sequence[int]) -> bool:
    return trade_id in set(reported_ids)


def remember_reported_id(reported_ids: List[int], trade_id: int) -> List[int]:
    """شناسه را اضافه می‌کند و در صورت عبور از سقف، قدیمی‌ترین‌ها را دور
    می‌ریزد (FIFO ساده؛ فقط برای جلوگیری از رشد بی‌پایان State)."""
    updated = list(reported_ids)
    if trade_id not in updated:
        updated.append(trade_id)
    if len(updated) > MAX_REMEMBERED_IDS_PER_SYMBOL:
        updated = updated[-MAX_REMEMBERED_IDS_PER_SYMBOL:]
    return updated


def build_whale_alert(
    *,
    symbol: str,
    best_trade: Optional[Dict[str, Any]],
    window_start_utc: str,
    window_end_utc: str,
    reported_ids: Sequence[int],
    threshold_usdt: float,
) -> Optional[Dict[str, Any]]:
    """اگر هیچ معامله واجدشرایطی نبود یا قبلاً گزارش شده، None برمی‌گردد —
    یعنی «هیچ پیام ساختگی/خالی منتشر نشود» (بخش ۳.۸ بند ۷)."""
    if best_trade is None:
        return None
    trade_id = best_trade.get("agg_trade_id")
    if trade_id is None:
        return None
    if already_reported(trade_id, reported_ids):
        return None
    return {
        "symbol": symbol,
        "agg_trade_id": trade_id,
        "direction": best_trade.get("direction"),
        "notional_usdt": best_trade.get("notional_usdt"),
        "price": best_trade.get("price"),
        "quantity": best_trade.get("quantity"),
        "trade_time_utc": datetime.fromtimestamp(
            best_trade["trade_time"] / 1000.0, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z") if best_trade.get("trade_time") else None,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "threshold_usdt": threshold_usdt,
        "source": "BINANCE_SPOT",
    }
