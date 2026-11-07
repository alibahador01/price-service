"""
http_retry.py
کمک‌تابع مشترک برای فراخوانی HTTP با timeout مشخص، retry محدود، backoff
تصاعدی با jitter و احترام به هدر Retry-After در پاسخ 429.
همه کلاینت‌های جدید (Binance، Bzzoiro، Football-Data.org، Alternative.me،
CoinGecko، Gemini، Groq) از همین تابع استفاده می‌کنند تا رفتار retry یکدست
و قابل‌تست باشد.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional

import requests


class HttpCallFailed(Exception):
    """بعد از اتمام تمام تلاش‌ها raise می‌شود."""


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
    max_attempts: int = 3,
    backoff_base: float = 1.5,
    sleep_fn: Callable[[float], None] = time.sleep,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """
    حداکثر max_attempts تلاش انجام می‌دهد. برای 429 به Retry-After احترام
    می‌گذارد؛ برای 5xx/خطای شبکه backoff تصاعدی با jitter اعمال می‌شود.
    برای 4xx غیر از 429 بلافاصله raise می‌شود (retry معنی ندارد).
    """
    caller = session or requests
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = caller.request(
                method, url, headers=headers, params=params, json=json_body, timeout=timeout
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = _backoff_delay(attempt, backoff_base)
            sleep_fn(delay)
            continue

        if response.status_code == 429:
            if attempt >= max_attempts:
                last_exc = HttpCallFailed("429 rate limited after {} attempts".format(attempt))
                break
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else _backoff_delay(attempt, backoff_base)
            sleep_fn(delay)
            continue

        if 500 <= response.status_code < 600:
            last_exc = HttpCallFailed("{} server error".format(response.status_code))
            if attempt >= max_attempts:
                break
            sleep_fn(_backoff_delay(attempt, backoff_base))
            continue

        return response

    raise HttpCallFailed(str(last_exc) if last_exc else "request failed with no response")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, base: float) -> float:
    raw = (base ** attempt)
    return raw + random.uniform(0, raw * 0.3)
