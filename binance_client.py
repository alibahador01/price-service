"""
binance_client.py
کلاینت داده عمومی Binance Spot (بدون نیاز به کلید API) برای کندل یک‌ساعته و
aggTrades. طبق بخش ۳.۲ سند، رادار معاملات بزرگ باید دقیقاً روی یک بازه
یک‌ساعتهٔ کامل (startTime/endTime) محاسبه شود، نه صرفاً آخرین ۱۰۰۰ معامله.

توابع شبکه‌ای (fetch_*) از http_retry استفاده می‌کنند. توابع محاسباتی
(parse_kline_row، drop_open_candle، compute_large_trade_radar،
select_largest_trade) خالص و بدون شبکه‌اند تا بدون اتصال اینترنت هم قابل
تست باشند.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from http_retry import HttpCallFailed, request_with_retry

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_MARKET_DATA_URL = "https://data-api.binance.vision"
KLINES_PATH = "/api/v3/klines"
AGG_TRADES_PATH = "/api/v3/aggTrades"

ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "TRXUSDT", "TONUSDT")
KLINES_FETCH_LIMIT = 101  # یکی بیشتر تا کندل باز حذف شود و ۱۰۰ کندل بسته بماند
KLINES_REQUIRED_CLOSED = 100
AGG_TRADES_PAGE_LIMIT = 1000
AGG_TRADES_MAX_PAGES = 10  # سقف صفحه‌بندی امن؛ عبور از آن یعنی radar=incomplete

DEFAULT_LARGE_TRADE_THRESHOLD_USDT = 10_000.0


# ============================================================
# Pure parsing / validation (قابل تست بدون شبکه)
# ============================================================
def parse_kline_row(row: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """یک ردیف خام kline Binance را به دیکشنری تمیز تبدیل می‌کند.
    [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume,
     numberOfTrades, takerBuyBaseVol, takerBuyQuoteVol, ignore]"""
    if not isinstance(row, (list, tuple)) or len(row) < 8:
        return None
    try:
        return {
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": int(row[6]),
            "quote_volume": float(row[7]),
        }
    except (TypeError, ValueError, IndexError):
        return None


def drop_open_candle(
    candles: List[Dict[str, Any]], now_ms: Optional[int] = None
) -> List[Dict[str, Any]]:
    """فرض نکن آخرین کندل برگشتی بسته شده؛ اگر close_time در آینده/اکنون
    است، کندل باز را حذف کن."""
    if not candles:
        return candles
    reference = now_ms if now_ms is not None else int(time.time() * 1000)
    last = candles[-1]
    if last.get("close_time", 0) >= reference:
        return candles[:-1]
    return candles


def validate_symbol(symbol: str) -> bool:
    return symbol in ALLOWED_SYMBOLS


# ============================================================
# Network fetch (requests + retry/backoff)
# ============================================================
def fetch_klines(
    symbol: str,
    *,
    interval: str = "1h",
    limit: int = KLINES_FETCH_LIMIT,
    base_url: str = BINANCE_MARKET_DATA_URL,
    timeout: float = 10.0,
    max_attempts: int = 3,
) -> List[Dict[str, Any]]:
    """۱۰۱ کندل می‌گیرد، کندل باز را حذف می‌کند و ۱۰۰ کندل بسته برمی‌گرداند.
    اگر داده کافی نبود، لیست کوتاه‌تر برمی‌گردد (فراخوان باید quality را
    بررسی کند؛ این تابع هرگز داده جعلی تولید نمی‌کند)."""
    if not validate_symbol(symbol):
        raise ValueError("symbol {} not in allowlist".format(symbol))
    response = request_with_retry(
        "GET",
        base_url + KLINES_PATH,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=timeout,
        max_attempts=max_attempts,
    )
    raw = response.json()
    if not isinstance(raw, list):
        raise HttpCallFailed("unexpected klines schema for {}".format(symbol))
    parsed = [parse_kline_row(row) for row in raw]
    parsed = [row for row in parsed if row is not None]
    parsed.sort(key=lambda r: r["open_time"])
    closed = drop_open_candle(parsed)
    return closed[-KLINES_REQUIRED_CLOSED:]


def fetch_agg_trades_window(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = BINANCE_MARKET_DATA_URL,
    timeout: float = 10.0,
    max_attempts: int = 3,
    max_pages: int = AGG_TRADES_MAX_PAGES,
) -> Tuple[List[Dict[str, Any]], bool]:
    """تمام معاملات تجمیعی یک بازه را (با صفحه‌بندی startTime/endTime میلی‌ثانیه
    UTC) می‌گیرد. خروجی: (trades, truncated). truncated=True یعنی احتمال از
    قلم‌افتادن داده وجود دارد و radar نباید «قطعی» اعلام شود."""
    if not validate_symbol(symbol):
        raise ValueError("symbol {} not in allowlist".format(symbol))
    trades: List[Dict[str, Any]] = []
    cursor = start_ms
    truncated = False
    for _ in range(max_pages):
        response = request_with_retry(
            "GET",
            base_url + AGG_TRADES_PATH,
            params={
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": AGG_TRADES_PAGE_LIMIT,
            },
            timeout=timeout,
            max_attempts=max_attempts,
        )
        raw = response.json()
        if not isinstance(raw, list):
            raise HttpCallFailed("unexpected aggTrades schema for {}".format(symbol))
        page = [parse_agg_trade_row(row) for row in raw]
        page = [t for t in page if t is not None]
        trades.extend(page)
        if len(raw) < AGG_TRADES_PAGE_LIMIT:
            break
        # صفحه پر بود؛ احتمال باقی‌ماندن داده هست — صفحه بعد را با آخرین زمان+1 بخوان.
        last_time = page[-1]["trade_time"] if page else cursor
        next_cursor = last_time + 1
        if next_cursor <= cursor:
            truncated = True
            break
        cursor = next_cursor
        if cursor > end_ms:
            break
    else:
        # به سقف صفحه‌بندی امن رسیدیم و هنوز معلوم نیست همه را گرفتیم.
        truncated = True
    # فقط معاملات داخل بازه دقیق را نگه دار (endTime ممکن است توسط صفحه آخر رد شود)
    trades = [t for t in trades if start_ms <= t["trade_time"] <= end_ms]
    return trades, truncated


def parse_agg_trade_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ردیف aggTrades: a=aggTradeId, p=price, q=quantity, T=timestamp,
    m=isBuyerMaker"""
    if not isinstance(row, dict):
        return None
    try:
        return {
            "agg_trade_id": int(row["a"]),
            "price": float(row["p"]),
            "quantity": float(row["q"]),
            "trade_time": int(row["T"]),
            "is_buyer_maker": bool(row["m"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


# ============================================================
# Pure computation — Large Trade Radar (بخش ۳.۳)
# ============================================================
def compute_large_trade_radar(
    trades: Sequence[Dict[str, Any]],
    *,
    threshold_usdt: float = DEFAULT_LARGE_TRADE_THRESHOLD_USDT,
    window_start_utc: str = "",
    window_end_utc: str = "",
    truncated: bool = False,
) -> Dict[str, Any]:
    """طبق بخش ۳.۳: جهت از isBuyerMaker استخراج می‌شود
    (isBuyerMaker=False → خرید تهاجمی taker؛ True → فروش تهاجمی taker).
    نسبت فقط بر اساس notional (price*qty) جمع می‌شود، نه تعداد ردیف."""
    base = {
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "threshold_usdt": threshold_usdt,
        "buy_notional_usdt": None,
        "sell_notional_usdt": None,
        "buy_percentage": None,
        "sell_percentage": None,
        "status": "unavailable",
    }
    if truncated:
        base["status"] = "incomplete"
        return base
    buy_notional = 0.0
    sell_notional = 0.0
    qualifying = 0
    for t in trades:
        try:
            notional = float(t["price"]) * float(t["quantity"])
        except (KeyError, TypeError, ValueError):
            continue
        if notional < threshold_usdt:
            continue
        qualifying += 1
        if t.get("is_buyer_maker") is False:
            buy_notional += notional
        elif t.get("is_buyer_maker") is True:
            sell_notional += notional
    total = buy_notional + sell_notional
    if qualifying == 0 or total <= 0:
        base["status"] = "unavailable"
        return base
    base["buy_notional_usdt"] = round(buy_notional, 2)
    base["sell_notional_usdt"] = round(sell_notional, 2)
    base["buy_percentage"] = round(buy_notional / total * 100.0, 2)
    base["sell_percentage"] = round(sell_notional / total * 100.0, 2)
    base["status"] = "valid"
    return base


def select_largest_trade(
    trades: Sequence[Dict[str, Any]],
    *,
    threshold_usdt: float = DEFAULT_LARGE_TRADE_THRESHOLD_USDT,
) -> Optional[Dict[str, Any]]:
    """برای Whale Alert: فقط بزرگ‌ترین معامله معتبر بالاتر از آستانه را
    انتخاب می‌کند (بر اساس notional)."""
    best: Optional[Dict[str, Any]] = None
    best_notional = 0.0
    for t in trades:
        try:
            notional = float(t["price"]) * float(t["quantity"])
        except (KeyError, TypeError, ValueError):
            continue
        if notional < threshold_usdt:
            continue
        if notional > best_notional:
            best_notional = notional
            best = dict(t)
            best["notional_usdt"] = round(notional, 2)
            best["direction"] = "buy" if t.get("is_buyer_maker") is False else "sell"
    return best
