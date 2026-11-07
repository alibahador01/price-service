"""
shared_cache.py
کش کوتاه‌مدت درون‌فرایندی برای جلوگیری از فراخوانی تکراری شبکه وقتی چند
job (مثلاً small_list و big_list) در یک بازه کوتاه به همان داده نیاز
دارند — طبق درخواست صریح کاربر: «اطلاعات مشترک را Cache کن و برای هر
پیام دوباره دریافت نکن». این یک حافظهٔ درون‌فرایندی ساده است، نه دیسک؛
در هر ری‌استارت Render خالی می‌شود که مشکلی نیست چون TTL کوتاه است.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Tuple

_lock = threading.Lock()
_store: Dict[str, Tuple[float, Any]] = {}


def cached_call(key: str, ttl_seconds: float, fetch_fn: Callable[[], Any]) -> Any:
    """اگر مقدار کش‌شده برای key از ttl_seconds تازه‌تر باشد همان را
    برمی‌گرداند؛ وگرنه fetch_fn را صدا می‌زند و نتیجه را کش می‌کند. خطای
    fetch_fn کنترل نمی‌شود — فراخوان مسئول try/except خودش است."""
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is not None and (now - entry[0]) < ttl_seconds:
            return entry[1]
    value = fetch_fn()
    with _lock:
        _store[key] = (time.monotonic(), value)
    return value


def invalidate(key: str) -> None:
    with _lock:
        _store.pop(key, None)


def clear_all() -> None:
    with _lock:
        _store.clear()
