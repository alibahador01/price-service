"""
state_store.py
ابزار مشترک خواندن/نوشتن امن State برای همه ماژول‌های جدید (کریپتو، نبض بازار،
نهنگ، آربیتراژ، فوتبال، Scheduler، تبلیغات).

- هر فایل State مستقل از فایل قیمت فعلی (OUTPUT) است؛ کلیدهای سرویس قیمت هرگز
  از این مسیر لمس نمی‌شوند.
- نوشتن با فایل موقت + fsync + os.replace انجام می‌شود (همان روش render_app.py)
  تا داده ناقص هرگز جای State معتبر را نگیرد.
- هر مسیر یک قفل threading.RLock مستقل دارد تا نوشتن‌های هم‌زمان از چند ترد
  تداخل نکنند.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Callable, Dict

_locks_guard = threading.Lock()
_path_locks: Dict[str, threading.RLock] = {}


def _lock_for(path: str) -> threading.RLock:
    with _locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _path_locks[path] = lock
        return lock


def load_json(path: str, default_factory: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """State را از دیسک بخوان. اگر فایل نبود یا خراب بود، State تازه و سالم برگردان
    (هرگز استثنا raise نکن — سرویس باید بدون State قبلی هم سالم بالا بیاید)."""
    lock = _lock_for(path)
    with lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                base = default_factory()
                base.update(loaded)
                return base
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # داده ناقص/خراب را کنار بگذار و از نو بساز؛ هرگز crash نکن.
            pass
        return default_factory()


def save_json(path: str, data: Dict[str, Any]) -> bool:
    """نوشتن اتمیک JSON با lock اختصاصی همان مسیر. خطا فقط log می‌شود،
    استثنا بیرون نمی‌رود تا حلقه پس‌زمینه متوقف نشود."""
    lock = _lock_for(path)
    with lock:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(data, ensure_ascii=False, indent=2, separators=(",", ": "))
            fd, temp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
            return True
        except Exception as exc:  # noqa: BLE001 - باید log شود، نه crash
            print("[STATE_STORE] save failed for {}: {}".format(path, str(exc)[:300]), flush=True)
            return False


def mutate_json(
    path: str,
    default_factory: Callable[[], Dict[str, Any]],
    mutator: Callable[[Dict[str, Any]], None],
) -> Dict[str, Any]:
    """خواندن، اعمال تغییر، نوشتن اتمیک - همه زیر یک lock تا race نشود."""
    lock = _lock_for(path)
    with lock:
        state = load_json(path, default_factory)
        mutator(state)
        save_json(path, state)
        return state
