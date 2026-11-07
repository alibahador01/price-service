import os
import json
import re
import time
import random
import asyncio
import threading
import tempfile
import signal
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, Optional, List, Tuple, Union

import requests
import jdatetime
from flask import Flask, Response
from telethon import TelegramClient
try:
    from telethon.sessions import StringSession
except Exception:
    StringSession = None

# ماژول‌های جدید (بخش قابلیت‌های تحلیل/فوتبال/زمان‌بندی مرکزی) — کاملاً جدا
# از منطق قیمت‌دهی ووچر/دلار/یوتوپیا بالا؛ فقط در پایین همین فایل مصرف می‌شوند.
import binance_client
import central_scheduler
import football_client
import football_images
import gemini_client
import groq_client
import keyboard
import shared_cache
import state_store

# ============================================================
# CONFIG
# ============================================================
# Secrets را فقط در Environment Variables مربوط به Render قرار دهید.
API_ID = int(os.environ.get("TG_API_ID", "2040"))
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION = os.environ.get("TG_SESSION", "utopia_session").strip()
TG_SESSION_STRING = os.environ.get("TG_SESSION_STRING", "").strip()
BOT_VH = os.environ.get("BOT_VH", "@utopiaex_bot").strip()
OUTPUT = os.environ.get("OUTPUT_FILE", "Ali1377.01.05.json").strip()

SENDER_BOT_TOKEN = os.environ.get("SENDER_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@Vochino_bh01").strip()
MAIN_BOT_URL = os.environ.get("MAIN_BOT_URL", "https://t.me/Vochino_bh01_bot").strip()

# ============================================================
# متغیرهای محیطی قابلیت‌های جدید (تحلیل کریپتو/نبض بازار/نهنگ/آربیتراژ/فوتبال)
# هیچ‌کدام روی سیستم فوق (دریافت/ارائه قیمت ووچر-دلار-یوتوپیا) اثر ندارند؛
# فقط ورودی central_scheduler و ماژول‌های جدید هستند.
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", gemini_client.GEMINI_MODEL_DEFAULT).strip()
GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", gemini_client.GEMINI_THINKING_LEVEL_DEFAULT).strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", groq_client.GROQ_MODEL_DEFAULT).strip()
LARGE_TRADE_THRESHOLD_USDT = float(os.environ.get("LARGE_TRADE_THRESHOLD_USDT", "10000") or "10000")

BZZOIRO_API_KEY = os.environ.get("BZZOIRO_API_KEY", "").strip()
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
FOOTBALL_TIMEZONE = os.environ.get("FOOTBALL_TIMEZONE", "Asia/Tehran").strip()
FOOTBALL_LOOKAHEAD_DAYS = int(os.environ.get("FOOTBALL_LOOKAHEAD_DAYS", "7") or "7")
FOOTBALL_POLL_INTERVAL_MINUTES = int(os.environ.get("FOOTBALL_POLL_INTERVAL_MINUTES", "15") or "15")
# fail-closed عمدی: فقط رشته دقیق "true" مجوز انتشار عکس مسابقه را فعال می‌کند (بخش ۷.۵).
FOOTBALL_IMAGE_LICENSE_CONFIRMED_RAW = os.environ.get("FOOTBALL_IMAGE_LICENSE_CONFIRMED", "").strip()

CENTRAL_SCHEDULER_TICK_SECONDS = int(os.environ.get("CENTRAL_SCHEDULER_TICK_SECONDS", "45") or "45")
CENTRAL_SCHEDULER_STATE_PATH = os.environ.get("CENTRAL_SCHEDULER_STATE_PATH", "central_scheduler_state.json").strip()
WHALE_STATE_PATH = os.environ.get("WHALE_STATE_PATH", "whale_state.json").strip()

SWAPWALLET_PRICES_URL = "https://swapwallet.app/api/v1/market/prices"
# فهرست کانال حدود 2 دقیقه پیش از هر نوبت آماده می‌شود.
POSTER_PRE_FETCH_SECONDS = 120
POSTER_HTTP_TIMEOUT = 10
POSTER_RETRIES = 1  # حداکثر دو تلاش برای منابع بازار و ارسال کانال

# دریافت ووچرها و نرخ‌های دلاری: حداقل 120 دقیقه فاصله + جیتر مثبت.
# بازه 120 تا 130 دقیقه است؛ بنابراین هیچ چرخه‌ای زودتر از دو ساعت شروع نمی‌شود.
PRIMARY_FETCH_MIN_SECONDS = 120 * 60
PRIMARY_FETCH_MAX_SECONDS = 130 * 60
FETCH_RETRIES = 1  # فقط برای APIهای قیمت؛ درخواست تلگرام ووچر در هر چرخه تکرار نمی‌شود.

PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")

# فقط برای جلوگیری از خواب سرویس Render؛ این کار ساعت اجرای رایگان را کم نمی‌کند.
KEEPALIVE_MIN_SECONDS = 11 * 60 + 30
KEEPALIVE_MAX_SECONDS = 13 * 60 + 30

# نوسان محلی سه رقم پایانی ووچرها تقریباً 10 بار در ساعت؛ بدون درخواست اینترنتی.
DISPLAY_MIN_SECONDS = 5 * 60
DISPLAY_MAX_SECONDS = 7 * 60
HEARTBEAT_SECONDS = 30

MIN_TOMAN_RATE = 10_000
MAX_TOMAN_RATE = 1_000_000_000
MIN_USD_RATE = 0.50
MAX_USD_RATE = 2.00

# SwapWallet در خروجی فعلی، قیمت‌های /IRT را در مقیاس تومان برمی‌گرداند.
# نمونهٔ بررسی‌شده: USDT/IRT حدود 229000 و BTC/IRT حدود 17.57 میلیارد؛ تبدیل /10 نکنید.
# خود API قیمت لحظه‌ای می‌دهد، نه دفتر سفارش Bid/Ask؛ بنابراین حاشیهٔ فعلی 0.3٪
# فقط نرخ نمایشی خرید/فروش ووچینو است، نه ادعای نرخ Bid/Ask مستقیم صرافی منبع.
QUOTE_SPREAD = 0.003

# منابع عمومی بدون API key برای بازار داخلی ایران.
# TGJU اصلی نرخ‌ها را به ریال می‌دهد؛ کد نرخ را دقیقاً یک‌بار بر 10 تقسیم می‌کند.
IRAN_GOLD_TGJU_URL = "https://www.tgju.org/profile/geram18"
IRAN_GOLD_TGJU_FALLBACK_URL = "https://www.tgju.live/profile/geram18"
IRAN_COIN_EMAMI_TGJU_URL = "https://www.tgju.org/profile/sekee"
IRAN_COIN_EMAMI_FALLBACK_URL = "https://gem.tgju.org/coin"
IRAN_EURO_TGJU_URL = "https://www.tgju.org/profile/price_eur"
IRAN_EURO_FALLBACK_URL = "https://www.tgju.live/profile/price_eur"
# فقط برای Arbitrage Radar (بخش ۳.۹): کد قبلی هیچ منبع تومانی دلار نداشت؛
# طبق سند، افزودن یک منبع مستقل و مستند مجاز است. از همان الگوی TGJU
# موجود (gold/coin/euro) استفاده می‌شود؛ هیچ تابعی بازنویسی نشده.
IRAN_USD_TGJU_URL = "https://www.tgju.org/profile/price_dollar_rl"
IRAN_USD_TGJU_FALLBACK_URL = "https://www.tgju.live/profile/price_dollar_rl"
GOLD_API_TIMEOUT = 10

app = Flask(__name__)
state_lock = threading.RLock()
print_lock = threading.Lock()
telegram_lock: Optional[asyncio.Lock] = None

RUNTIME: Dict[str, Any] = {
    "started_at": time.time(),
    "scheduler_heartbeat": 0.0,
    "display_heartbeat": 0.0,
    "telegram_last_fetch": 0.0,
    "scheduler_errors": 0,
    "display_errors": 0,
    "last_real_update": None,
    "keepalive_last_success": 0.0,
    "keepalive_count": 0,
}
DISPLAY_RUNTIME: Dict[str, int] = {}
SHUTDOWN = threading.Event()


def log(msg: str) -> None:
    with print_lock:
        print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ============================================================
# TEXT / NUMBER HELPERS
# ============================================================
def normalize_fa_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    replacements = {
        "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه",
        "‌": " ", "‍": " ", "ـ": "", "أ": "ا", "إ": "ا", "ؤ": "و",
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def digits_to_ascii(value: Any) -> str:
    if value is None:
        return ""
    return str(value).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = digits_to_ascii(value)
    for ch in [",", "٬", "،", " ", "\u00a0", "\u200c", "_"]:
        s = s.replace(ch, "")
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    s = digits_to_ascii(value).strip()
    s = s.replace(",", "").replace("٬", "").replace("،", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except (TypeError, ValueError):
        return None


def valid_toman_rate(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_TOMAN_RATE <= value <= MAX_TOMAN_RATE


def valid_usd_rate(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and MIN_USD_RATE <= float(value) <= MAX_USD_RATE


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# VOUCHER DISPLAY SUFFIX: REAL PREFIX + RANDOM LAST THREE DIGITS
# ============================================================
def prefix_from_base(price: int) -> int:
    return (int(price) // 1000) * 1000


def suffix_from_value(value: int) -> int:
    return int(value) % 1000


def compose_display(base_price: int, suffix: int) -> int:
    return prefix_from_base(base_price) + max(0, min(999, int(suffix)))


def initial_suffix(base_price: int, previous: Optional[int] = None) -> int:
    if previous is not None and prefix_from_base(previous) == prefix_from_base(base_price):
        old = suffix_from_value(previous)
        candidate = max(0, min(999, old + random.randint(-80, 80)))
        if candidate == old:
            candidate = (old + random.randint(11, 47)) % 1000
        return candidate
    return random.randint(0, 999)


def next_suffix(current: int) -> int:
    current = max(0, min(999, int(current)))
    step = random.randint(7, 42)
    if random.random() < 0.08:
        step = random.randint(43, 78)
    if current <= 70:
        direction = 1
    elif current >= 929:
        direction = -1
    else:
        direction = 1 if random.random() >= 0.5 else -1
    candidate = current + direction * step
    while candidate < 0 or candidate > 999:
        candidate = abs(candidate) if candidate < 0 else 1998 - candidate
    if candidate == current:
        candidate = min(999, current + 9) if current < 500 else max(0, current - 9)
    return candidate


# ============================================================
# JSON STATE — RAM + ATOMIC SAVE
# ============================================================
def _fresh_state() -> Dict[str, Any]:
    return {
        "ali1377": {},
        "meta": {"schema_version": 3, "last_real_update": None, "display_updated_at": None, "sources": {}},
        "last_updated": None,
    }


def load_state_from_disk() -> Dict[str, Any]:
    state = _fresh_state()
    try:
        with open(OUTPUT, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            if isinstance(loaded.get("ali1377"), dict):
                state["ali1377"].update(loaded["ali1377"])
            if isinstance(loaded.get("meta"), dict):
                state["meta"].update(loaded["meta"])
            if loaded.get("last_updated"):
                state["last_updated"] = loaded["last_updated"]
    except FileNotFoundError:
        log("[STATE] JSON file does not exist yet; starting clean.")
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        log("[STATE] Invalid JSON; starting from safe empty state: " + str(exc))
    return state


STATE: Dict[str, Any] = load_state_from_disk()


def remove_disabled_ps_state() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        removed = [k for k in list(ali) if k in ("ps_buy", "ps_sell") or k.startswith("ps_buy_") or k.startswith("ps_sell_")]
        for key in removed:
            ali.pop(key, None)
        STATE.setdefault("meta", {}).setdefault("sources", {}).pop("ps", None)
        if removed:
            STATE["last_updated"] = iso_now()


def _atomic_write_locked(state: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(OUTPUT)) or "."
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, separators=(",", ": "))
    fd = None
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".prices-", suffix=".tmp", dir=directory, text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, OUTPUT)
        temp_path = None
    except Exception as exc:
        log("[STATE] Atomic save failed: " + str(exc))
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


remove_disabled_ps_state()
with state_lock:
    _atomic_write_locked(STATE)


def mutate_state(mutator: Callable[[Dict[str, Any]], None], persist: bool = True) -> bool:
    with state_lock:
        try:
            mutator(STATE)
            STATE.setdefault("ali1377", {})
            STATE.setdefault("meta", {})
            STATE["last_updated"] = iso_now()
            if persist:
                _atomic_write_locked(STATE)
            return True
        except Exception as exc:
            log("[STATE] Mutation failed: " + str(exc))
            return False


def mark_source(source_name: str, *, success: bool, message: str = "", persist: bool = True) -> None:
    stamp = iso_now()

    def _mutate(state: Dict[str, Any]) -> None:
        item = state.setdefault("meta", {}).setdefault("sources", {}).setdefault(source_name, {})
        item["last_attempt"] = stamp
        if success:
            item["last_success"] = stamp
            item["last_error"] = None
        else:
            item["last_error"] = message[:500]
    mutate_state(_mutate, persist=persist)


def mark_source_attempt_started(source_name: str) -> None:
    """Persist the start of a source request before contacting it (restart-safe cooldown)."""
    stamp = iso_now()

    def _mutate(state: Dict[str, Any]) -> None:
        item = state.setdefault("meta", {}).setdefault("sources", {}).setdefault(source_name, {})
        item["last_attempt"] = stamp

    mutate_state(_mutate, persist=True)


def source_last_attempt_epoch(source_name: str) -> Optional[float]:
    """Read a persisted UTC attempt timestamp; malformed legacy values are ignored safely."""
    with state_lock:
        sources = STATE.get("meta", {}).get("sources", {})
        item = sources.get(source_name, {}) if isinstance(sources, dict) else {}
        stamp = item.get("last_attempt") if isinstance(item, dict) else None
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        log("[SCHED] Ignoring invalid persisted timestamp for {}".format(source_name))
        return None


def init_display_runtime() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        for key, value in list(ali.items()):
            if not key.endswith("_base") or not valid_toman_rate(value):
                continue
            display_key = key[:-5]
            old_display = ali.get(display_key)
            if isinstance(old_display, int) and prefix_from_base(old_display) == prefix_from_base(value):
                DISPLAY_RUNTIME[display_key] = suffix_from_value(old_display)
            else:
                DISPLAY_RUNTIME[display_key] = initial_suffix(value)
                ali[display_key] = compose_display(value, DISPLAY_RUNTIME[display_key])


init_display_runtime()


def apply_vh_result(result: Dict[str, Optional[int]]) -> int:
    """Apply available voucher prices; hot prices remain exact source values."""
    applied = 0

    def _mutate(state: Dict[str, Any]) -> None:
        nonlocal applied
        ali = state.setdefault("ali1377", {})
        for key in ("premium_buy", "premium_sell", "u_buy", "u_sell", "hot_buy", "hot_sell"):
            value = result.get(key)
            if not valid_toman_rate(value):
                continue

            # Preserve the existing randomized display behavior for U and Premium.
            # Hot prices are exposed directly and are not routed through the display loop.
            if key in ("hot_buy", "hot_sell"):
                ali[key] = value
            else:
                old_display = ali.get(key)
                ali[key + "_base"] = value
                DISPLAY_RUNTIME[key] = initial_suffix(value, old_display if isinstance(old_display, int) else None)
                ali[key] = compose_display(value, DISPLAY_RUNTIME[key])
            applied += 1
        if applied:
            state.setdefault("meta", {})["last_real_update"] = iso_now()
    mutate_state(_mutate, persist=False)
    return applied


def apply_crypto_result(result: Dict[str, float]) -> int:
    applied = 0

    def _mutate(state: Dict[str, Any]) -> None:
        nonlocal applied
        ali = state.setdefault("ali1377", {})
        for key in ("tether_usd", "utopia_usd"):
            value = result.get(key)
            if valid_usd_rate(value):
                ali[key] = round(float(value), 8)
                applied += 1
        if applied:
            state.setdefault("meta", {})["last_real_update"] = iso_now()
    mutate_state(_mutate, persist=False)
    return applied


# ============================================================
# TELEGRAM VOUCHER SOURCE
# ============================================================
async def ensure_connected(client: TelegramClient) -> bool:
    if client.is_connected():
        return True
    log("[TELEGRAM] Reconnecting...")
    try:
        await client.connect()
        return client.is_connected()
    except Exception as exc:
        log("[TELEGRAM] Reconnect failed: " + str(exc))
        return False


def _inline_price_button_texts(message: Any) -> List[str]:
    """Return visible button labels only from Telegram inline keyboards."""
    markup = getattr(message, "reply_markup", None)
    if markup is None or "InlineMarkup" not in type(markup).__name__:
        return []

    labels: List[str] = []
    for row in getattr(markup, "rows", []) or []:
        for button in getattr(row, "buttons", []) or []:
            label = getattr(button, "text", None)
            if label:
                labels.append(str(label).strip())
    return labels


async def _get_inline_price_buttons_since(
    client: TelegramClient,
    bot: Any,
    after_message_id: int,
    timeout: float = 7.0,
) -> List[str]:
    """Wait briefly for a new bot reply and read its inline keyboard labels."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = await client.get_messages(bot, limit=20)
        if not isinstance(messages, (list, tuple)):
            messages = [messages] if messages is not None else []

        labels: List[str] = []
        for message in sorted(messages, key=lambda item: getattr(item, "id", 0)):
            if getattr(message, "id", 0) <= after_message_id:
                continue
            if getattr(message, "out", False):
                continue
            labels.extend(_inline_price_button_texts(message))
        if labels:
            return labels
        await asyncio.sleep(0.8)
    return []


def _voucher_kind_from_button(label: str) -> Optional[str]:
    text = normalize_fa_text(digits_to_ascii(label)).lower()
    if any(token in text for token in ("پریمیوم", "پرمیوم", "premium")):
        return "premium"
    if any(token in text for token in ("هات", "هت", "هوت", "hot", "داغ")):
        return "hot"
    if re.search(r"یو\s*ووچر|ووچر\s*یو|\bu[\s_-]*voucher\b", text, re.IGNORECASE):
        return "u"
    return None


def _toman_from_rial_button(label: str) -> Optional[int]:
    """Extract a Rial quote from a button and convert it to Toman exactly once."""
    text = digits_to_ascii(normalize_fa_text(label))
    patterns = (
        r"([0-9][0-9,٬،\s\u00a0]*)\s*(?:ریال|rial|irr)",
        r"(?:ریال|rial|irr)\s*[:：|]?\s*([0-9][0-9,٬،\s\u00a0]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        rial = parse_int(match.group(1))
        if rial is None or rial <= 0:
            continue
        toman = rial // 10
        return toman if valid_toman_rate(toman) else None
    return None


async def fetch_vh(client: TelegramClient) -> Dict[str, Optional[int]]:
    """Read buy/sell prices from Utopiaex inline buttons; failures stay non-fatal."""
    result: Dict[str, Optional[int]] = {
        "u_buy": None,
        "u_sell": None,
        "premium_buy": None,
        "premium_sell": None,
        "hot_buy": None,
        "hot_sell": None,
    }
    errors: List[str] = []

    try:
        if not await ensure_connected(client):
            return result
        bot = await client.get_entity(BOT_VH)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log("[UTOPIAEX ERROR] Could not open {}: {}".format(BOT_VH, str(exc)[:300]))
        return result

    async def request_direction(prompt: str, direction: str, delay_range: Tuple[float, float], suffix: str) -> None:
        try:
            sent_message = await client.send_message(bot, prompt)
            await asyncio.sleep(random.uniform(*delay_range))
            labels = await _get_inline_price_buttons_since(
                client, bot, int(getattr(sent_message, "id", 0))
            )
            if not labels:
                errors.append("{}: no inline price buttons received".format(direction))
                return

            extracted = 0
            for label in labels:
                kind = _voucher_kind_from_button(label)
                if kind is None:
                    continue
                toman = _toman_from_rial_button(label)
                if toman is None:
                    continue
                key = kind + "_" + suffix
                if result[key] is None:
                    result[key] = toman
                    extracted += 1
            if extracted == 0:
                errors.append("{}: inline buttons contained no valid voucher prices".format(direction))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append("{} request failed: {}".format(direction, str(exc)[:250]))

    # The randomized pauses reduce rapid repeated interactions with Telegram.
    await request_direction("خرید انواع ووچر 🟢", "buy", (2.5, 4.5), "buy")
    await request_direction("فروش انواع ووچر 🔴", "sell", (3.0, 6.0), "sell")

    for key, value in result.items():
        if value is not None:
            log("[UTOPIAEX {} OK] {} تومان".format(key.upper(), value))

    missing = [key for key, value in result.items() if value is None]
    if missing:
        errors.append("missing valid prices: " + ", ".join(missing))
    if errors:
        # Emit one consolidated error line while preserving partial valid results.
        log("[UTOPIAEX ERROR] " + "; ".join(dict.fromkeys(errors))[:700])
    return result


# ============================================================
# HTTP / EXTERNAL PRICE SOURCES
# ============================================================
def _http_get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    request_headers = {"User-Agent": "Mozilla/5.0 VochinoPriceService/4.0", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    try:
        response = requests.get(url, params=params, headers=request_headers, timeout=timeout)
        if response.status_code != 200:
            log("[API] HTTP {} from {}".format(response.status_code, url))
            return None
        data = response.json()
        if not isinstance(data, dict):
            log("[API] invalid JSON object from {}".format(url))
            return None
        return data
    except requests.RequestException as exc:
        log("[API] network error {}: {}".format(url, exc))
    except (ValueError, TypeError) as exc:
        log("[API] invalid response {}: {}".format(url, exc))
    except Exception as exc:
        log("[API] unexpected error {}: {}".format(url, exc))
    return None


def _tether_coinpaprika() -> Optional[float]:
    data = _http_get_json("https://api.coinpaprika.com/v1/tickers/usdt-tether")
    value = parse_float(data.get("quotes", {}).get("USD", {}).get("price")) if data else None
    return value if valid_usd_rate(value) else None


def _utopia_coinpaprika() -> Optional[float]:
    data = _http_get_json("https://api.coinpaprika.com/v1/tickers/uusd-utopia-usd")
    value = parse_float(data.get("quotes", {}).get("USD", {}).get("price")) if data else None
    return value if valid_usd_rate(value) else None


def _tether_coingecko() -> Optional[float]:
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    headers = {"x-cg-demo-api-key": key} if key else None
    data = _http_get_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "tether", "vs_currencies": "usd"}, headers=headers,
    )
    value = parse_float(data.get("tether", {}).get("usd")) if data else None
    return value if valid_usd_rate(value) else None


def fetch_crypto() -> Dict[str, float]:
    """دو نرخ USD را در هر نوبت می‌گیرد؛ fallback فقط هنگام خرابی منبع اصلی فعال می‌شود."""
    result: Dict[str, float] = {}
    tether = _tether_coinpaprika()
    if tether is None:
        tether = _tether_coingecko()
    utopia = _utopia_coinpaprika()
    if tether is not None:
        result["tether_usd"] = round(float(tether), 8)
        log("[CRYPTO] tether_usd={} USD".format(result["tether_usd"]))
    else:
        log("[CRYPTO] USDT/USD unavailable; previous value preserved")
    if utopia is not None:
        result["utopia_usd"] = round(float(utopia), 8)
        log("[CRYPTO] utopia_usd={} USD".format(result["utopia_usd"]))
    else:
        log("[CRYPTO] Utopia/USD unavailable; previous value preserved")
    return result


async def run_with_retry(name: str, fn: Callable[[], Any], retries: int = FETCH_RETRIES) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                delay = random.uniform(2.0, 4.0)
                log("[RETRY] {} in {:.1f}s — {}".format(name, delay, exc))
                await asyncio.sleep(delay)
            else:
                log("[ERROR] {} failed: {}".format(name, exc))
    raise last_error if last_error else RuntimeError(name + " failed")


# ============================================================
# SCHEDULER: VOUCHERS + USDT/USD + UTOPIA/USD EVERY ~2 HOURS
# ============================================================
async def scheduler_loop(client: TelegramClient) -> None:
    global telegram_lock
    if telegram_lock is None:
        telegram_lock = asyncio.Lock()
    log("[SCHED] Started — voucher + USDT/USD + Utopia/USD; minimum 120 minutes, randomized 120–130 minute spacing")
    first_run = True

    while not SHUTDOWN.is_set():
        RUNTIME["scheduler_heartbeat"] = time.time()

        if first_run:
            # On a Render restart, honor the previously persisted request time.
            last_attempt = source_last_attempt_epoch("u_prem")
            if last_attempt is None:
                delay = 0.0
            else:
                elapsed = max(0.0, time.time() - last_attempt)
                cooldown = max(0.0, PRIMARY_FETCH_MIN_SECONDS - elapsed)
                extra_jitter = random.uniform(0.0, PRIMARY_FETCH_MAX_SECONDS - PRIMARY_FETCH_MIN_SECONDS)
                delay = cooldown + extra_jitter
        else:
            delay = random.uniform(PRIMARY_FETCH_MIN_SECONDS, PRIMARY_FETCH_MAX_SECONDS)

        if delay > 0:
            log("[SCHED] Next price cycle in {:.1f} minutes".format(delay / 60.0))
            due = time.monotonic() + delay
            while not SHUTDOWN.is_set() and time.monotonic() < due:
                RUNTIME["scheduler_heartbeat"] = time.time()
                await asyncio.sleep(min(30.0, max(1.0, due - time.monotonic())))
            if SHUTDOWN.is_set():
                break

        first_run = False
        tg_result: Dict[str, Optional[int]] = {}
        crypto_result: Dict[str, float] = {}

        async def telegram_job() -> Dict[str, Optional[int]]:
            # One buy/sell pair per cycle; do not issue rapid retry pairs on Telegram errors.
            async with telegram_lock:
                mark_source_attempt_started("u_prem")
                return await fetch_vh(client)

        async def crypto_job() -> Dict[str, float]:
            # Crypto price API retries are independent of the voucher bot request cadence.
            return await run_with_retry("USDT/Utopia fetch", lambda: asyncio.to_thread(fetch_crypto))

        results = await asyncio.gather(telegram_job(), crypto_job(), return_exceptions=True)
        if isinstance(results[0], Exception):
            tg_error = str(results[0])
            RUNTIME["scheduler_errors"] += 1
            mark_source("u_prem", success=False, message=tg_error, persist=False)
            log("[SCHED ERROR] Utopiaex: " + tg_error)
        else:
            tg_result = results[0]
            applied = apply_vh_result(tg_result)
            mark_source("u_prem", success=applied > 0, message="no valid values" if applied == 0 else "", persist=False)
            RUNTIME["telegram_last_fetch"] = time.time()
            log("[SCHED] voucher fetch completed; applied={}".format(applied))

        if isinstance(results[1], Exception):
            crypto_error = str(results[1])
            RUNTIME["scheduler_errors"] += 1
            mark_source("crypto", success=False, message=crypto_error, persist=False)
            log("[SCHED ERROR] USDT/Utopia: " + crypto_error)
        else:
            crypto_result = results[1]
            applied_crypto = apply_crypto_result(crypto_result)
            mark_source("crypto", success=applied_crypto > 0, message="no valid values" if applied_crypto == 0 else "", persist=False)
            log("[SCHED] USD-price fetch completed; applied={}".format(applied_crypto))

        if tg_result or crypto_result:
            RUNTIME["last_real_update"] = iso_now()
        with state_lock:
            _atomic_write_locked(STATE)


# ============================================================
# DISPLAY LOOP — NO NETWORK REQUESTS, NO DISK WRITE PER TICK
# ============================================================

def update_display_tick() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        changed = False
        for key, base in list(ali.items()):
            if not key.endswith("_base") or not valid_toman_rate(base):
                continue
            display_key = key[:-5]
            current_suffix = DISPLAY_RUNTIME.get(display_key)
            if current_suffix is None:
                current_suffix = initial_suffix(base)
            new_suffix = next_suffix(current_suffix)
            DISPLAY_RUNTIME[display_key] = new_suffix
            new_display = compose_display(base, new_suffix)
            if ali.get(display_key) != new_display:
                ali[display_key] = new_display
                changed = True
        if changed:
            STATE.setdefault("meta", {})["display_updated_at"] = iso_now()
        RUNTIME["display_heartbeat"] = time.time()


async def display_loop() -> None:
    log("[DISPLAY] Local suffix fluctuation active; average ~10 ticks/hour; no external requests")
    while not SHUTDOWN.is_set():
        try:
            update_display_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            RUNTIME["display_errors"] += 1
            log("[DISPLAY ERROR] " + str(exc))
        await asyncio.sleep(random.uniform(DISPLAY_MIN_SECONDS, DISPLAY_MAX_SECONDS))


async def supervised_loop(name: str, factory: Callable[[], Any]) -> None:
    failures = 0
    while not SHUTDOWN.is_set():
        try:
            await factory()
            failures = 0
            if SHUTDOWN.is_set():
                break
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            if name == "scheduler":
                RUNTIME["scheduler_errors"] += 1
            log("[SUPERVISOR] {} stopped: {}".format(name, exc))
            await asyncio.sleep(min(60, 3 * failures) + random.uniform(0, 2))


async def heartbeat_loop() -> None:
    while not SHUTDOWN.is_set():
        RUNTIME["scheduler_heartbeat"] = time.time()
        RUNTIME["display_heartbeat"] = RUNTIME.get("display_heartbeat") or time.time()
        await asyncio.sleep(HEARTBEAT_SECONDS)


def _ping_public_health(url: str) -> bool:
    try:
        response = requests.get(url, headers={"User-Agent": "VochinoPrice-Keepalive/3.0"}, timeout=10)
        return 200 <= response.status_code < 300
    except Exception:
        return False


async def keepalive_loop() -> None:
    if not PUBLIC_BASE_URL:
        log("[KEEPALIVE] Disabled — PUBLIC_BASE_URL/RENDER_EXTERNAL_URL not set.")
        while not SHUTDOWN.is_set():
            await asyncio.sleep(300)
        return
    url = PUBLIC_BASE_URL + "/health"
    log("[KEEPALIVE] Enabled: " + url)
    await asyncio.sleep(random.uniform(45, 90))
    while not SHUTDOWN.is_set():
        try:
            ok = await asyncio.to_thread(_ping_public_health, url)
            if ok:
                RUNTIME["keepalive_last_success"] = time.time()
                RUNTIME["keepalive_count"] += 1
                log("[KEEPALIVE] ping OK #{}".format(RUNTIME["keepalive_count"]))
            else:
                log("[KEEPALIVE] ping failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("[KEEPALIVE] error: " + str(exc))
        await asyncio.sleep(random.uniform(KEEPALIVE_MIN_SECONDS, KEEPALIVE_MAX_SECONDS))


def build_telegram_client() -> TelegramClient:
    if TG_SESSION_STRING:
        if StringSession is None:
            raise RuntimeError("Telethon StringSession is unavailable")
        return TelegramClient(StringSession(TG_SESSION_STRING), API_ID, API_HASH)
    return TelegramClient(SESSION, API_ID, API_HASH)


async def loops_main() -> None:
    if not API_HASH:
        log("[FATAL] TG_API_HASH environment variable is missing. Background Telegram scheduler will not start.")
        return
    client = None
    try:
        client = build_telegram_client()
        await client.connect()
        if not await client.is_user_authorized():
            log("[FATAL] Telegram Session is not authorized.")
            return
        log("[OK] Telegram connected.")
        tasks = [
            asyncio.create_task(supervised_loop("scheduler", lambda: scheduler_loop(client))),
            asyncio.create_task(supervised_loop("display", display_loop)),
            asyncio.create_task(heartbeat_loop()),
            asyncio.create_task(keepalive_loop()),
        ]
        while not SHUTDOWN.is_set():
            await asyncio.sleep(2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        log("[TELEGRAM] Client disconnected.")


def run_async_loops() -> None:
    try:
        asyncio.run(loops_main())
    except Exception as exc:
        log("[ASYNC FATAL] " + str(exc))


# ============================================================
# TELEGRAM CHANNEL POSTER
# ============================================================
_POSTER_SWAP_KEYS = (
    "USDT/IRT", "TRX/IRT", "TON/IRT", "BTC/IRT", "ETH/IRT", "FET/IRT",
    "PAXG/IRT", "XAUT/IRT",
)
_PERSIAN_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
_PERSIAN_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _poster_persian_digits(value: Any) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def _poster_parse_price(value: Any) -> Optional[float]:
    """Parse a real positive price while preserving fractional token prices."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            normalized = digits_to_ascii(str(value)).strip()
            normalized = normalized.replace(",", "").replace("٬", "").replace("،", "")
            match = re.search(r"\d+(?:\.\d+)?", normalized)
            if not match:
                return None
            number = float(match.group(0))
        if not (number > 0 and number < 10**18):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _poster_format_price(value: Any) -> str:
    parsed = _poster_parse_price(value)
    if parsed is None:
        return "ناموجود"
    if parsed >= 1000 or abs(parsed - round(parsed)) < 1e-9:
        return "{:,}".format(int(round(parsed)))
    decimals = 4 if parsed >= 1 else 12
    text = ("{:,.%df}" % decimals).format(parsed).rstrip("0").rstrip(".")
    return text


def _poster_cached_swap_prices() -> Dict[str, float]:
    with state_lock:
        meta = STATE.get("meta", {})
        poster_meta = meta.get("poster", {}) if isinstance(meta, dict) else {}
        cached = poster_meta.get("swapwallet_prices", {}) if isinstance(poster_meta, dict) else {}
        if not isinstance(cached, dict):
            return {}
        clean: Dict[str, float] = {}
        for key in _POSTER_SWAP_KEYS:
            parsed = _poster_parse_price(cached.get(key))
            if parsed is not None:
                clean[key] = parsed
        return clean


def _poster_fetch_swap_prices() -> Dict[str, float]:
    """One SwapWallet request, only shortly before a channel post; cached on disk."""
    last_error: Optional[str] = None
    for attempt in range(POSTER_RETRIES + 1):
        try:
            response = requests.get(
                SWAPWALLET_PRICES_URL,
                headers={"User-Agent": "Mozilla/5.0 VochinoChannelPoster/4.0", "Accept": "application/json"},
                timeout=POSTER_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("status", "")).upper() != "OK":
                raise ValueError("SwapWallet returned unexpected status/payload")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("SwapWallet result is not an object")
            fresh: Dict[str, float] = {}
            for key in _POSTER_SWAP_KEYS:
                parsed = _poster_parse_price(result.get(key))
                if parsed is not None:
                    fresh[key] = parsed
            if not fresh:
                raise ValueError("SwapWallet returned no valid required prices")
            with state_lock:
                meta = STATE.setdefault("meta", {})
                poster_meta = meta.setdefault("poster", {})
                cached = poster_meta.setdefault("swapwallet_prices", {})
                cached.update(fresh)
                poster_meta["swapwallet_last_success"] = iso_now()
                poster_meta["swapwallet_last_error"] = None
                STATE["last_updated"] = iso_now()
                _atomic_write_locked(STATE)
                merged = {
                    key: parsed for key in _POSTER_SWAP_KEYS
                    if (parsed := _poster_parse_price(cached.get(key))) is not None
                }
            log("[POSTER] SwapWallet refreshed {} market pairs (including PAXG if available).".format(len(fresh)))
            return merged
        except Exception as exc:
            last_error = str(exc)
            log("[POSTER] SwapWallet attempt {}/{} failed: {}".format(attempt + 1, POSTER_RETRIES + 1, last_error[:300]))
            if attempt < POSTER_RETRIES:
                time.sleep(1.0)
    cached_prices = _poster_cached_swap_prices()
    with state_lock:
        poster_meta = STATE.setdefault("meta", {}).setdefault("poster", {})
        poster_meta["swapwallet_last_error"] = (last_error or "unknown error")[:500]
        STATE["last_updated"] = iso_now()
        _atomic_write_locked(STATE)
    if cached_prices:
        log("[POSTER] SwapWallet unavailable; using saved last-known prices.")
    else:
        log("[POSTER] SwapWallet unavailable and no saved prices exist.")
    return cached_prices


def _poster_cached_local_toman(asset_key: str) -> Optional[float]:
    """Return the last-known cached Toman quote for a local-market asset."""
    field = asset_key + "_toman"
    with state_lock:
        meta = STATE.get("meta", {})
        poster = meta.get("poster", {}) if isinstance(meta, dict) else {}
        cached = _poster_parse_price(poster.get(field)) if isinstance(poster, dict) else None
    return cached


def _poster_save_local_toman(
    asset_key: str,
    price_toman: int,
    source_url: str,
    source_time: str = "",
) -> None:
    """Atomically cache one verified Iranian-market quote (already converted to Toman)."""
    if not isinstance(price_toman, int) or price_toman <= 0:
        raise ValueError("invalid Toman quote for " + asset_key)
    stamp = iso_now()
    with state_lock:
        poster = STATE.setdefault("meta", {}).setdefault("poster", {})
        poster[asset_key + "_toman"] = price_toman
        poster[asset_key + "_last_success"] = stamp
        poster[asset_key + "_source"] = source_url
        poster[asset_key + "_source_unit"] = "Rial converted to Toman (divide by 10)"
        poster[asset_key + "_source_time"] = str(source_time or "")[:100]
        poster[asset_key + "_last_error"] = None
        STATE["last_updated"] = stamp
        _atomic_write_locked(STATE)


def _poster_fetch_tgju_rial_quote(
    url: str,
    *,
    asset_label: str,
    min_rial: int,
    max_rial: int,
) -> Tuple[int, str]:
    """Fetch and validate a current local-market quote from a public TGJU HTML page.

    TGJU's Iranian market profile pages publish Rial quotes. Parsing is limited to
    values near the explicit «نرخ فعلی» label and a strict asset-specific range.
    The function returns the source Rial quote and source URL; it never fabricates a rate.
    """
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fa,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.tgju.org/",
        },
        timeout=GOLD_API_TIMEOUT,
    )
    response.raise_for_status()

    import html as _html
    page = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>",
        " ",
        response.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    page_text = _html.unescape(re.sub(r"<[^>]+>", " ", page))
    page_text = re.sub(r"\s+", " ", page_text)

    # Support TGJU profile syntax («نرخ فعلی:: 1,234,567») and table syntax
    # («نرخ فعلی | 1,234,567»). Candidate numbers are then range-checked.
    number_pattern = r"([0-9۰-۹٠-٩]+(?:[,٬،][0-9۰-۹٠-٩]{3})+|[0-9۰-۹٠-٩]{7,13})"
    quote_pattern = re.compile(
        r"نرخ\s*فعلی\s*[:：|│\-]*\s*" + number_pattern,
        flags=re.IGNORECASE,
    )
    for match in quote_pattern.finditer(page_text):
        rial_price = parse_int(match.group(1))
        if rial_price is not None and min_rial <= rial_price <= max_rial:
            return rial_price, url

    # The public fallback coin page presents a table before its detailed quote.
    # Some page versions omit the «نرخ فعلی» label for that table, so allow its
    # explicitly named «سکه امامی» row only on the coin fallback page.
    if "gem.tgju.org/coin" in url:
        row_pattern = re.compile(
            r"سکه\s*امامی\s*(?:\|\s*)?(" + number_pattern + r")",
            flags=re.IGNORECASE,
        )
        for match in row_pattern.finditer(page_text):
            rial_price = parse_int(match.group(1))
            if rial_price is not None and min_rial <= rial_price <= max_rial:
                return rial_price, url

    raise ValueError("no valid current Rial quote found for " + asset_label)


def _poster_fetch_local_toman_asset(
    asset_key: str,
    asset_label: str,
    primary_url: str,
    fallback_url: str,
    *,
    min_rial: int,
    max_rial: int,
) -> Optional[float]:
    """Primary -> no-key fallback -> persistent last-known cache for local assets."""
    errors: List[str] = []
    for source_name, url in (("primary", primary_url), ("fallback", fallback_url)):
        try:
            rial_price, used_url = _poster_fetch_tgju_rial_quote(
                url,
                asset_label=asset_label,
                min_rial=min_rial,
                max_rial=max_rial,
            )
            toman_price = int(round(rial_price / 10.0))
            if toman_price <= 0:
                raise ValueError("converted Toman quote is invalid")
            _poster_save_local_toman(asset_key, toman_price, used_url)
            log(
                "[{}] {} refreshed: {:,} Toman (source Rial ÷ 10; {})".format(
                    asset_key.upper(), asset_label, toman_price, source_name
                )
            )
            return float(toman_price)
        except Exception as exc:
            errors.append("{}: {}".format(source_name, str(exc)[:220]))
            log(
                "[{}] {} source failed ({}): {}".format(
                    asset_key.upper(), asset_label, source_name, str(exc)[:180]
                )
            )

    error_text = " | ".join(errors)[:500]
    with state_lock:
        poster = STATE.setdefault("meta", {}).setdefault("poster", {})
        poster[asset_key + "_last_error"] = error_text
        _atomic_write_locked(STATE)
    cached = _poster_cached_local_toman(asset_key)
    if cached is not None:
        log("[{}] Both live sources failed; using cached {:,} Toman.".format(asset_key.upper(), int(cached)))
    else:
        log("[{}] Both live sources failed and no cached quote exists.".format(asset_key.upper()))
    return cached


def _poster_fetch_coin_emami_toman() -> Optional[float]:
    """Live Imami coin price: TGJU primary, public TGJU market table fallback, then cache."""
    return _poster_fetch_local_toman_asset(
        "coin_emami",
        "سکه امام",
        IRAN_COIN_EMAMI_TGJU_URL,
        IRAN_COIN_EMAMI_FALLBACK_URL,
        min_rial=1_000_000_000,
        max_rial=5_000_000_000,
    )


def _poster_fetch_euro_toman() -> Optional[float]:
    """Live Iranian free-market EUR quote: TGJU primary/fallback, then Toman cache."""
    return _poster_fetch_local_toman_asset(
        "euro",
        "یورو",
        IRAN_EURO_TGJU_URL,
        IRAN_EURO_FALLBACK_URL,
        min_rial=1_000_000,
        max_rial=100_000_000,
    )


def _poster_fetch_usd_irt_toman() -> Optional[float]:
    """نرخ آزاد دلار/تومان، فقط برای Arbitrage Radar (بخش ۳.۹) — هرگز برای
    نمایش در فهرست قیمت ووچینو استفاده نمی‌شود و به سیستم ووچر/دلار/یوتوپیا
    کاری ندارد. از همان تابع عمومی TGJU استفاده می‌کند (بدون تغییر)."""
    return _poster_fetch_local_toman_asset(
        "usd_irt",
        "دلار آزاد",
        IRAN_USD_TGJU_URL,
        IRAN_USD_TGJU_FALLBACK_URL,
        min_rial=1_000_000,
        max_rial=200_000_000,
    )


def _poster_usd_irt_last_success() -> Optional[str]:
    with state_lock:
        meta = STATE.get("meta", {})
        poster = meta.get("poster", {}) if isinstance(meta, dict) else {}
        return poster.get("usd_irt_last_success") if isinstance(poster, dict) else None


def _poster_cached_gold18() -> Optional[float]:
    with state_lock:
        meta = STATE.get("meta", {})
        poster = meta.get("poster", {}) if isinstance(meta, dict) else {}
        return _poster_parse_price(poster.get("gold18_toman")) if isinstance(poster, dict) else None


def _poster_fetch_gold18_toman() -> Optional[float]:
    """Fetch 18k gold per gram in Toman: TGJU primary -> TGJU mirror -> cache."""
    return _poster_fetch_local_toman_asset(
        "gold18",
        "طلای ۱۸ عیار",
        IRAN_GOLD_TGJU_URL,
        IRAN_GOLD_TGJU_FALLBACK_URL,
        min_rial=100_000_000,
        max_rial=2_000_000_000,
    )


def _poster_tehran_date_time() -> Tuple[str, str]:
    tehran = datetime.now(ZoneInfo("Asia/Tehran"))
    jdate = jdatetime.datetime.fromgregorian(datetime=tehran)
    weekday = _PERSIAN_WEEKDAYS[tehran.weekday()]
    month = _PERSIAN_MONTHS[jdate.month - 1]
    date_text = "{} {} {} {}".format(weekday, _poster_persian_digits("{:02d}".format(jdate.day)), month, _poster_persian_digits(jdate.year))
    return date_text, _poster_persian_digits(tehran.strftime("%H:%M:%S"))


def _poster_progress_bar(percent: Optional[int]) -> str:
    filled = 0 if percent is None else min(9, max(0, int(percent) // 10))
    return "▰" * filled + "▱" * (9 - filled)


def _poster_crypto_quote(base_value: Any) -> Tuple[Optional[float], Optional[float]]:
    """Build the existing Vochino buy/sell quote from the source's live toman reference."""
    base = _poster_parse_price(base_value)
    if base is None:
        return None, None
    buy = base * (1.0 + QUOTE_SPREAD)
    sell = base * (1.0 - QUOTE_SPREAD)
    if sell <= 0:
        return None, None
    return buy, sell


def _poster_market_buy_prices(
    ali_prices: Dict[str, Any],
    swap_prices: Dict[str, Any],
    gold18_toman: Optional[float],
    coin_emami_toman: Optional[float] = None,
    euro_toman: Optional[float] = None,
) -> Dict[str, float]:
    """Use real source prices for the bar reference; never use the randomized voucher suffix."""
    current: Dict[str, float] = {}
    voucher_buy_fields = (
        ("u_voucher", "u_buy_base", "u_buy"),
        ("premium_voucher", "premium_buy_base", "premium_buy"),
        ("hot_voucher", "hot_buy", "hot_buy"),
    )
    for bar_key, source_key, display_key in voucher_buy_fields:
        price = _poster_parse_price(ali_prices.get(source_key))
        if price is None:
            display_price = _poster_parse_price(ali_prices.get(display_key))
            if display_price is not None and display_price >= 1000:
                price = float((int(display_price) // 1000) * 1000)
        if price is not None:
            current[bar_key] = price

    usdt_irt = _poster_parse_price(swap_prices.get("USDT/IRT"))
    utopia_usd = parse_float(ali_prices.get("utopia_usd"))
    utopia_base = utopia_usd * usdt_irt if utopia_usd and usdt_irt else None
    crypto_bases: Dict[str, Optional[float]] = {
        "usdt": usdt_irt,
        "utopia": utopia_base,
        "trx": _poster_parse_price(swap_prices.get("TRX/IRT")),
        "ton": _poster_parse_price(swap_prices.get("TON/IRT")),
        "btc": _poster_parse_price(swap_prices.get("BTC/IRT")),
        "eth": _poster_parse_price(swap_prices.get("ETH/IRT")),
        "fet": _poster_parse_price(swap_prices.get("FET/IRT")),
        "paxg": _poster_parse_price(swap_prices.get("PAXG/IRT")),
        "xaut": _poster_parse_price(swap_prices.get("XAUT/IRT")),
        "gold18": gold18_toman,
        "coin_emami": coin_emami_toman,
        "euro": euro_toman,
    }
    for bar_key, base in crypto_bases.items():
        buy, _ = _poster_crypto_quote(base)
        if buy is not None:
            current[bar_key] = buy
    return current


def _poster_cached_buy_prices() -> Dict[str, float]:
    with state_lock:
        meta = STATE.get("meta", {})
        poster = meta.get("poster", {}) if isinstance(meta, dict) else {}
        cached = poster.get("previous_buy_prices", {}) if isinstance(poster, dict) else {}
        if not isinstance(cached, dict):
            return {}
        return {key: parsed for key, value in cached.items() if (parsed := _poster_parse_price(value)) is not None}


def _poster_save_buy_prices(current: Dict[str, float]) -> None:
    if not current:
        return
    with state_lock:
        poster = STATE.setdefault("meta", {}).setdefault("poster", {})
        previous = poster.setdefault("previous_buy_prices", {})
        for key, value in current.items():
            parsed = _poster_parse_price(value)
            if parsed is not None:
                previous[key] = parsed
        poster["previous_buy_prices_updated_at"] = iso_now()
        STATE["last_updated"] = iso_now()
        _atomic_write_locked(STATE)


def _poster_change_percent(bar_key: str, current_buy: Any, previous_buy_prices: Dict[str, Any]) -> Optional[float]:
    """Actual percentage movement of the buy quote against the last successfully sent list."""
    current = _poster_parse_price(current_buy)
    previous = _poster_parse_price(previous_buy_prices.get(bar_key))
    if current is None or previous is None or previous <= 0:
        return None
    return ((current - previous) / previous) * 100.0


def _poster_dynamic_percent(bar_key: str, current_buy: Any, previous_buy_prices: Dict[str, Any]) -> Optional[int]:
    """Bar fill shows magnitude of measured buy-price movement; 5% movement fills the bar."""
    movement = _poster_change_percent(bar_key, current_buy, previous_buy_prices)
    if movement is None:
        return None
    return max(0, min(100, int(round(abs(movement) * 20.0))))


def _poster_trend_symbol(bar_key: str, current_buy: Any, previous_buy_prices: Dict[str, Any]) -> str:
    current = _poster_parse_price(current_buy)
    previous = _poster_parse_price(previous_buy_prices.get(bar_key))
    if current is None or previous is None or previous <= 0 or current == previous:
        return "➡️"
    return "🔺" if current > previous else "🔻"


def _poster_percent_text(movement: Optional[float]) -> str:
    if movement is None:
        return "مبنای اولیه"
    signed = "{:+.2f}".format(movement)
    return _poster_persian_digits(signed) + "٪"


def _poster_asset_block(bar_key: str, emoji: str, ticker: str, label: str, base_value: Any, current_buy_prices: Dict[str, float], previous_buy_prices: Dict[str, Any]) -> List[str]:
    buy, sell = _poster_crypto_quote(base_value)
    current_buy = current_buy_prices.get(bar_key)
    bar_fill = _poster_dynamic_percent(bar_key, current_buy, previous_buy_prices)
    movement = _poster_change_percent(bar_key, current_buy, previous_buy_prices)
    trend = _poster_trend_symbol(bar_key, current_buy, previous_buy_prices)
    return [
        f"{emoji} {ticker} | {label}",
        "     ➫ 𝘽𝙪𝙮 ......... {} تومان".format(_poster_format_price(buy)),
        "     ➫ 𝙎𝙚𝙡𝙡 ........ {} تومان".format(_poster_format_price(sell)),
        "     ✪ {} {} {}".format(trend, _poster_progress_bar(bar_fill), _poster_percent_text(movement)),
    ]


def _poster_support_url() -> str:
    support = SUPPORT_USERNAME.strip()
    if support.startswith(("https://t.me/", "http://t.me/")):
        return support
    return "https://t.me/" + support.lstrip("@/")


def _poster_compute_price_context(
    ali_prices: Dict[str, Any],
    swap_prices: Dict[str, Any],
    gold18_toman: Optional[float] = None,
    previous_buy_prices: Optional[Dict[str, Any]] = None,
    current_buy_prices: Optional[Dict[str, float]] = None,
    coin_emami_toman: Optional[float] = None,
    euro_toman: Optional[float] = None,
) -> Tuple[Dict[str, Any], Dict[str, float]]:
    """محاسبات مشترک فهرست کوچک و بزرگ (بدون تغییر فرمول). previous/current
    همیشه روی مجموعه کامل دارایی‌ها محاسبه می‌شود؛ هرکدام از دو پیام فقط
    زیرمجموعه‌ای از این بلوک‌ها را نمایش می‌دهند (بخش ۲.۳ سند)."""
    if previous_buy_prices is None:
        previous_buy_prices = _poster_cached_buy_prices()
    if current_buy_prices is None:
        current_buy_prices = _poster_market_buy_prices(
            ali_prices, swap_prices, gold18_toman, coin_emami_toman, euro_toman
        )
    return previous_buy_prices, current_buy_prices


def _poster_voucher_blocks(ali_prices: Dict[str, Any], current_buy_prices: Dict[str, float], previous_buy_prices: Dict[str, Any]) -> List[str]:
    u_buy, u_sell = ali_prices.get("u_buy"), ali_prices.get("u_sell")
    premium_buy, premium_sell = ali_prices.get("premium_buy"), ali_prices.get("premium_sell")
    hot_buy, hot_sell = ali_prices.get("hot_buy"), ali_prices.get("hot_sell")
    u_current_buy = current_buy_prices.get("u_voucher")
    premium_current_buy = current_buy_prices.get("premium_voucher")
    hot_current_buy = current_buy_prices.get("hot_voucher")
    u_percent = _poster_dynamic_percent("u_voucher", u_current_buy, previous_buy_prices)
    premium_percent = _poster_dynamic_percent("premium_voucher", premium_current_buy, previous_buy_prices)
    hot_percent = _poster_dynamic_percent("hot_voucher", hot_current_buy, previous_buy_prices)
    u_change = _poster_change_percent("u_voucher", u_current_buy, previous_buy_prices)
    premium_change = _poster_change_percent("premium_voucher", premium_current_buy, previous_buy_prices)
    hot_change = _poster_change_percent("hot_voucher", hot_current_buy, previous_buy_prices)
    u_trend = _poster_trend_symbol("u_voucher", u_current_buy, previous_buy_prices)
    premium_trend = _poster_trend_symbol("premium_voucher", premium_current_buy, previous_buy_prices)
    hot_trend = _poster_trend_symbol("hot_voucher", hot_current_buy, previous_buy_prices)
    return [
        "🔷 𝑼-𝑽𝒐𝒖𝒄𝒉𝒆𝒓 | یو ووچر",
        "     ➫ 𝘽𝙪𝙮 ......... {} تومان".format(_poster_format_price(u_buy)),
        "     ➫ 𝙎𝙚𝙡𝙡 ........ {} تومان".format(_poster_format_price(u_sell)),
        "     ✪ {} {} {}".format(u_trend, _poster_progress_bar(u_percent), _poster_percent_text(u_change)),
        "",
        "🔶 𝑷𝒓𝒆𝒎𝒊𝒖𝒎 | پریمیوم ووچر",
        "     ➫ 𝘽𝙪𝙮 ......... {} تومان".format(_poster_format_price(premium_buy)),
        "     ➫ 𝘚𝘦𝘭𝘭 ........ {} تومان".format(_poster_format_price(premium_sell)),
        "     ✪ {} {} {}".format(premium_trend, _poster_progress_bar(premium_percent), _poster_percent_text(premium_change)),
        "",
        "🔥 𝑯𝒐𝒕 𝑽𝒐𝒖𝒄𝒉𝒆𝒓 | هات ووچر",
        "     ➫ 𝘽𝙪𝙮 ......... {} تومان".format(_poster_format_price(hot_buy)),
        "     ➫ 𝘚𝘦𝘭𝘭 ........ {} تومان".format(_poster_format_price(hot_sell)),
        "     ✪ {} {} {}".format(hot_trend, _poster_progress_bar(hot_percent), _poster_percent_text(hot_change)),
    ]


def _poster_build_small_list_message(
    ali_prices: Dict[str, Any],
    swap_prices: Dict[str, Any],
    gold18_toman: Optional[float] = None,
    previous_buy_prices: Optional[Dict[str, Any]] = None,
    current_buy_prices: Optional[Dict[str, float]] = None,
    coin_emami_toman: Optional[float] = None,
    euro_toman: Optional[float] = None,
) -> str:
    """فهرست کوچک: ووچرها + دلار (USDT) + یوتوپیا؛ روزانه ۴ نوبت (بخش ۱.۲/۲.۳).
    فرمول/محاسبه هیچ دارایی تغییر نکرده؛ فقط همین دارایی‌ها در خروجی هستند."""
    date_text, time_text = _poster_tehran_date_time()
    previous_buy_prices, current_buy_prices = _poster_compute_price_context(
        ali_prices, swap_prices, gold18_toman, previous_buy_prices, current_buy_prices,
        coin_emami_toman, euro_toman,
    )

    usdt_irt = _poster_parse_price(swap_prices.get("USDT/IRT"))
    utopia_usd = parse_float(ali_prices.get("utopia_usd"))
    utopia_base = utopia_usd * usdt_irt if utopia_usd and usdt_irt else None
    crypto_specs = [
        ("usdt", "💵", "USDT", "تتر", swap_prices.get("USDT/IRT")),
        ("utopia", "✨", "UUSD", "یوتوپیا", utopia_base),
    ]
    crypto_blocks = [
        _poster_asset_block(bar_key, emoji, ticker, label, base, current_buy_prices, previous_buy_prices)
        for bar_key, emoji, ticker, label, base in crypto_specs
    ]

    divider = "〰️" * 9
    lines = [
        "╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐-𝑯𝒖𝒃 ➫ 〰╮",
        " ⫹⫺ صرافی ووچینو-هاب⁰¹",
        "╰〰💲 𓆩𓆩🏦𓆪𓆪 💲〰╯",
        "",
        "📅 {} | ⏰ {}".format(date_text, time_text),
        "",
        divider,
        "       🎫 𝒗𝒐𝒖𝒄𝒉𝒆𝒓 🎫",
        divider,
        "",
    ]
    lines.extend(_poster_voucher_blocks(ali_prices, current_buy_prices, previous_buy_prices))
    lines.extend(["", divider, "        💲 𝒅𝒐𝒍𝒍𝒂𝒓 💲", divider, ""])
    for index, block in enumerate(crypto_blocks):
        lines.extend(block)
        if index < len(crypto_blocks) - 1:
            lines.append("")
    lines.extend(["", divider, "   📊 درصد = تغییر واقعی قیمت خرید نسبت به فهرست قبلی؛ ارسال اول مبناست", "   ⏰ بروزرسانی روزانه: ۴ نوبت", divider])
    return "\n".join(lines)


def _poster_build_large_list_message(
    ali_prices: Dict[str, Any],
    swap_prices: Dict[str, Any],
    gold18_toman: Optional[float] = None,
    previous_buy_prices: Optional[Dict[str, Any]] = None,
    current_buy_prices: Optional[Dict[str, float]] = None,
    coin_emami_toman: Optional[float] = None,
    euro_toman: Optional[float] = None,
) -> str:
    """فهرست بزرگ: ارزها (TRX/TON/BTC/ETH/FET/PAXG/XAUT) + طلا/سکه/یورو؛
    روزانه ۲ نوبت (بخش ۱.۲/۲.۳). فرمول/محاسبه تغییر نکرده."""
    date_text, time_text = _poster_tehran_date_time()
    previous_buy_prices, current_buy_prices = _poster_compute_price_context(
        ali_prices, swap_prices, gold18_toman, previous_buy_prices, current_buy_prices,
        coin_emami_toman, euro_toman,
    )

    crypto_specs = [
        ("trx", "🪙", "TRX", "ترون", swap_prices.get("TRX/IRT")),
        ("ton", "💎", "TON", "تون", swap_prices.get("TON/IRT")),
        ("btc", "🟠", "BTC", "بیت‌کوین", swap_prices.get("BTC/IRT")),
        ("eth", "🔷", "ETH", "اتریوم", swap_prices.get("ETH/IRT")),
        ("fet", "🤖", "FET", "هوش مصنوعی", swap_prices.get("FET/IRT")),
        ("paxg", "🟨", "PAXG", "پکس گلد (هر توکن)", swap_prices.get("PAXG/IRT")),
        ("xaut", "🟨", "XAUT", "تتر گلد (هر توکن)", swap_prices.get("XAUT/IRT")),
        ("gold18", "🟡", "18K GOLD", "طلای ۱۸ عیار (هر گرم)", gold18_toman),
        ("coin_emami", "🪙", "IMAMI", "سکه امام (هر سکه)", coin_emami_toman),
        ("euro", "💶", "EUR", "یورو", euro_toman),
    ]
    crypto_blocks = [
        _poster_asset_block(bar_key, emoji, ticker, label, base, current_buy_prices, previous_buy_prices)
        for bar_key, emoji, ticker, label, base in crypto_specs
    ]

    divider = "〰️" * 9
    lines = [
        "╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐-𝑯𝒖𝒃 ➫ 〰╮",
        " ⫹⫺ صرافی ووچینو-هاب⁰¹",
        "╰〰💲 𓆩𓆩🏦𓆪𓆪 💲〰╯",
        "",
        "📅 {} | ⏰ {}".format(date_text, time_text),
        "",
        divider,
        "        💎 𝒄𝒓𝒚𝒑𝒕𝒐 💎",
        divider,
        "",
    ]
    for index, block in enumerate(crypto_blocks):
        lines.extend(block)
        if index < len(crypto_blocks) - 1:
            lines.append("")
    lines.extend(["", divider, "   📊 درصد = تغییر واقعی قیمت خرید نسبت به فهرست قبلی؛ ارسال اول مبناست", "   ⏰ بروزرسانی روزانه: ۲ نوبت", divider])
    return "\n".join(lines)


def _poster_send_message(message_text: str, *, include_buttons: bool = True) -> bool:
    url = "https://api.telegram.org/bot{}/sendMessage".format(SENDER_BOT_TOKEN)
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if include_buttons:
        payload["reply_markup"] = keyboard.build_channel_keyboard(MAIN_BOT_URL)
    for attempt in range(POSTER_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=POSTER_HTTP_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("ok") is True:
                log("[POSTER] Sent OK")
                return True
            detail = result.get("description", "Telegram API returned ok=false") if isinstance(result, dict) else "Invalid Telegram API response"
            raise ValueError(str(detail)[:300])
        except Exception as exc:
            log("[POSTER] Telegram send attempt {}/{} failed: {}".format(attempt + 1, POSTER_RETRIES + 1, str(exc)[:400]))
            if attempt < POSTER_RETRIES:
                time.sleep(1.0)
    return False


# ============================================================
# CENTRAL SCHEDULER INTEGRATION
# طبق درخواست صریح کاربر: یک Scheduler مرکزی و سبک به‌جای چند Thread.
# این بخش poster_loop قدیمی (۹ نوبت ثابت، یک پیام ترکیبی) را با پل اتصال
# به central_scheduler.py جایگزین می‌کند. سیستم دریافت قیمت ووچر/دلار/
# یوتوپیا (اسکرول بالا) کاملاً دست‌نخورده می‌ماند؛ این بخش فقط می‌خواند.
# ============================================================
_CRYPTO_24H_IDS = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum",
    "TRXUSDT": "tron", "TONUSDT": "the-open-network",
}


def _central_fetch_swap_prices_cached() -> Dict[str, Any]:
    return shared_cache.cached_call("swap_prices", 90, _poster_fetch_swap_prices)


def _central_fetch_gold18_cached() -> Optional[float]:
    return shared_cache.cached_call("gold18_toman", 90, _poster_fetch_gold18_toman)


def _central_fetch_coin_emami_cached() -> Optional[float]:
    return shared_cache.cached_call("coin_emami_toman", 90, _poster_fetch_coin_emami_toman)


def _central_fetch_euro_cached() -> Optional[float]:
    return shared_cache.cached_call("euro_toman", 90, _poster_fetch_euro_toman)


def _central_fetch_usd_irt_cached() -> Optional[float]:
    return shared_cache.cached_call("usd_irt_toman", 90, _poster_fetch_usd_irt_toman)


def _central_build_small_list_message() -> Optional[str]:
    """نسخه غیرمسدودکننده: اگر u_buy هنوز نرسیده، همین حالا None برمی‌گرداند
    تا central_scheduler در tick بعدی دوباره امتحان کند — به‌جای قفل‌کردن
    تِرد مشترک با sleep طولانی (بهبود مصرف منابع نسبت به poster_loop قدیمی)."""
    with state_lock:
        ali_prices = dict(STATE.get("ali1377", {}))
    if _poster_parse_price(ali_prices.get("u_buy")) is None:
        log("[CENTRAL] small_list skipped: voucher data not ready yet")
        return None
    swap_prices = _central_fetch_swap_prices_cached()
    gold18_toman = _central_fetch_gold18_cached()
    coin_emami_toman = _central_fetch_coin_emami_cached()
    euro_toman = _central_fetch_euro_cached()
    previous = _poster_cached_buy_prices()
    current = _poster_market_buy_prices(ali_prices, swap_prices, gold18_toman, coin_emami_toman, euro_toman)
    message = _poster_build_small_list_message(
        ali_prices, swap_prices, gold18_toman=gold18_toman,
        previous_buy_prices=previous, current_buy_prices=current,
        coin_emami_toman=coin_emami_toman, euro_toman=euro_toman,
    )
    _poster_save_buy_prices(current)
    return message


def _central_build_large_list_message() -> Optional[str]:
    with state_lock:
        ali_prices = dict(STATE.get("ali1377", {}))
    swap_prices = _central_fetch_swap_prices_cached()
    if not swap_prices:
        log("[CENTRAL] big_list skipped: swap prices unavailable")
        return None
    gold18_toman = _central_fetch_gold18_cached()
    coin_emami_toman = _central_fetch_coin_emami_cached()
    euro_toman = _central_fetch_euro_cached()
    previous = _poster_cached_buy_prices()
    current = _poster_market_buy_prices(ali_prices, swap_prices, gold18_toman, coin_emami_toman, euro_toman)
    message = _poster_build_large_list_message(
        ali_prices, swap_prices, gold18_toman=gold18_toman,
        previous_buy_prices=previous, current_buy_prices=current,
        coin_emami_toman=coin_emami_toman, euro_toman=euro_toman,
    )
    _poster_save_buy_prices(current)
    return message


def _central_build_goodnight_message() -> str:
    return "🌙 شب‌تون آرام و خوش؛ تا فردا ساعت ۱۱ با نرخ‌های تازهٔ ووچینو-هاب. ✨"


def _central_send_text_message(text: str, with_keyboard: bool = True) -> bool:
    return _poster_send_message(text, include_buttons=with_keyboard)


def _central_send_football_match(image: Dict[str, Any], caption: str) -> Optional[int]:
    """عکس (اگر مجوز و ID معتبر باشد) + کپشن را می‌فرستد؛ اگر عکس در
    دسترس نبود، فقط کپشن به‌صورت پیام متنی می‌رود (بخش ۷.۵: هرگز placeholder
    یا عکس اشتباه جایگزین نمی‌شود)."""
    if not SENDER_BOT_TOKEN or not CHANNEL_ID:
        return None
    base_url = "https://api.telegram.org/bot{}/".format(SENDER_BOT_TOKEN)
    reply_markup = keyboard.build_channel_keyboard(MAIN_BOT_URL)
    if image.get("status") == "ready" and image.get("url"):
        payload = {
            "chat_id": CHANNEL_ID, "photo": image["url"], "caption": caption,
            "parse_mode": "HTML", "reply_markup": reply_markup,
        }
        endpoint = base_url + "sendPhoto"
    else:
        payload = {
            "chat_id": CHANNEL_ID, "text": caption, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": reply_markup,
        }
        endpoint = base_url + "sendMessage"
    try:
        response = requests.post(endpoint, json=payload, timeout=POSTER_HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and result.get("ok") is True:
            return result.get("result", {}).get("message_id")
        log("[CENTRAL] football send failed: {}".format(str(result)[:300]))
    except Exception as exc:
        log("[CENTRAL] football send exception: {}".format(str(exc)[:300]))
    return None


def _central_delete_football_message(message_id: Any) -> bool:
    if not SENDER_BOT_TOKEN or not CHANNEL_ID or message_id is None:
        return False
    url = "https://api.telegram.org/bot{}/deleteMessage".format(SENDER_BOT_TOKEN)
    try:
        response = requests.post(url, json={"chat_id": CHANNEL_ID, "message_id": message_id}, timeout=POSTER_HTTP_TIMEOUT)
        return response.ok
    except Exception as exc:
        log("[CENTRAL] football delete failed: {}".format(str(exc)[:200]))
        return False


def _central_edit_football_message_caption(message_id: Any, caption: str) -> bool:
    if not SENDER_BOT_TOKEN or not CHANNEL_ID or message_id is None:
        return False
    url = "https://api.telegram.org/bot{}/editMessageCaption".format(SENDER_BOT_TOKEN)
    payload = {"chat_id": CHANNEL_ID, "message_id": message_id, "caption": caption, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=POSTER_HTTP_TIMEOUT)
        return response.ok
    except Exception as exc:
        log("[CENTRAL] football edit failed: {}".format(str(exc)[:200]))
        return False


def _central_fetch_fear_greed() -> Dict[str, Any]:
    """Alternative.me Fear & Greed Index — بخش ۳.۷. خطا را بی‌صدا به
    مقدار unavailable تبدیل می‌کند؛ هرگز مقدار قدیمی را «تازه» جا نمی‌زند."""
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=POSTER_HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json().get("data") or []
        if not data:
            return {"value": None, "classification": None, "timestamp_utc": None}
        entry = data[0]
        ts = datetime.fromtimestamp(int(entry["timestamp"]), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "value": int(entry["value"]), "classification": entry.get("value_classification"),
            "timestamp_utc": ts,
        }
    except Exception as exc:
        log("[CENTRAL] fear_greed fetch failed: {}".format(str(exc)[:200]))
        return {"value": None, "classification": None, "timestamp_utc": None}


def _central_fetch_btc_dominance() -> Optional[float]:
    try:
        response = requests.get("https://api.coingecko.com/api/v3/global", timeout=POSTER_HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json().get("data") or {}
        dominance = (data.get("market_cap_percentage") or {}).get("btc")
        return float(dominance) if dominance is not None else None
    except Exception as exc:
        log("[CENTRAL] btc_dominance fetch failed: {}".format(str(exc)[:200]))
        return None


def _central_fetch_24h_changes() -> Dict[str, Optional[float]]:
    ids = ",".join(_CRYPTO_24H_IDS.values())
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=POSTER_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return {asset_id: (data.get(asset_id) or {}).get("usd_24h_change") for asset_id in _CRYPTO_24H_IDS.values()}
    except Exception as exc:
        log("[CENTRAL] 24h change fetch failed: {}".format(str(exc)[:200]))
        return {asset_id: None for asset_id in _CRYPTO_24H_IDS.values()}


def _central_format_local_datetime(_now: datetime) -> Tuple[str, str]:
    return _poster_tehran_date_time()


def _central_format_local_time(_now: datetime) -> str:
    _, time_text = _poster_tehran_date_time()
    return time_text


def _central_format_local_date_fa(local_dt: datetime) -> str:
    jdate = jdatetime.datetime.fromgregorian(datetime=local_dt)
    weekday = _PERSIAN_WEEKDAYS[local_dt.weekday()]
    month = _PERSIAN_MONTHS[jdate.month - 1]
    return "{} {} {} {}".format(weekday, _poster_persian_digits("{:02d}".format(jdate.day)), month, _poster_persian_digits(jdate.year))


def _central_format_local_time_fa(local_dt: datetime) -> str:
    return _poster_persian_digits(local_dt.strftime("%H:%M"))


def _central_load_whale_state() -> Dict[str, Any]:
    return state_store.load_json(WHALE_STATE_PATH, lambda: {})


def _central_save_whale_state(data: Dict[str, Any]) -> None:
    state_store.save_json(WHALE_STATE_PATH, data)


def _central_get_vochino_usdt_sell_toman() -> Optional[float]:
    """نرخ «Sell» خود ووچینو-هاب برای USDT، دقیقاً با همان فرمول/QUOTE_SPREAD
    موجود — فقط خوانده می‌شود، بازنویسی نشده (بخش ۳.۹)."""
    swap_prices = _central_fetch_swap_prices_cached()
    usdt_irt = _poster_parse_price(swap_prices.get("USDT/IRT"))
    if usdt_irt is None:
        return None
    _, sell = _poster_crypto_quote(usdt_irt)
    return sell


def _central_get_cached_tether_usd() -> Tuple[Optional[float], Optional[str]]:
    with state_lock:
        value = _poster_parse_price(STATE.get("ali1377", {}).get("tether_usd"))
        last_success = STATE.get("meta", {}).get("sources", {}).get("crypto", {}).get("last_success")
    return value, last_success


def _central_get_cached_usd_irt_toman() -> Tuple[Optional[float], Optional[str]]:
    value = _central_fetch_usd_irt_cached()
    return value, _poster_usd_irt_last_success()


def _build_central_context() -> Dict[str, Any]:
    tz = ZoneInfo(FOOTBALL_TIMEZONE)
    return {
        "tz": tz, "tz_name": FOOTBALL_TIMEZONE,
        "tick_seconds": CENTRAL_SCHEDULER_TICK_SECONDS,
        "send_text_message": _central_send_text_message,
        "build_small_list_message": _central_build_small_list_message,
        "build_large_list_message": _central_build_large_list_message,
        "build_goodnight_message": _central_build_goodnight_message,
        "fetch_klines": binance_client.fetch_klines,
        "fetch_agg_trades_window": binance_client.fetch_agg_trades_window,
        "call_gemini": lambda system, payload, api_key: gemini_client.call_gemini_structured(
            system, payload, api_key=api_key, model=GEMINI_MODEL, thinking_level=GEMINI_THINKING_LEVEL,
        ),
        "call_groq": lambda system, payload, api_key: groq_client.call_groq_json(
            system, payload, api_key=api_key, model=GROQ_MODEL,
        ),
        "gemini_api_key": GEMINI_API_KEY, "groq_api_key": GROQ_API_KEY,
        "large_trade_threshold_usdt": LARGE_TRADE_THRESHOLD_USDT,
        "fetch_fear_greed": _central_fetch_fear_greed,
        "fetch_btc_dominance": _central_fetch_btc_dominance,
        "fetch_24h_changes": _central_fetch_24h_changes,
        "format_local_datetime": _central_format_local_datetime,
        "format_local_time": _central_format_local_time,
        "format_local_date_fn": _central_format_local_date_fa,
        "format_local_time_fn": _central_format_local_time_fa,
        "load_whale_state": _central_load_whale_state,
        "save_whale_state": _central_save_whale_state,
        "get_vochino_usdt_sell_toman": _central_get_vochino_usdt_sell_toman,
        "get_cached_tether_usd": _central_get_cached_tether_usd,
        "get_cached_usd_irt_toman": _central_get_cached_usd_irt_toman,
        "fetch_bzzoiro_events": lambda **kwargs: football_client.fetch_bzzoiro_events(**kwargs),
        "fetch_football_data_matches": lambda **kwargs: football_client.fetch_football_data_matches(**kwargs),
        "bzzoiro_api_key": BZZOIRO_API_KEY, "football_data_api_key": FOOTBALL_DATA_API_KEY,
        "football_poll_interval_minutes": FOOTBALL_POLL_INTERVAL_MINUTES,
        "football_lookahead_days": FOOTBALL_LOOKAHEAD_DAYS,
        "football_image_license_confirmed": football_images_is_license_confirmed(),
        "send_football_match": _central_send_football_match,
        "delete_football_message": _central_delete_football_message,
        "edit_football_message_caption": _central_edit_football_message_caption,
        "log": log,
    }


def football_images_is_license_confirmed() -> bool:
    return football_images.is_license_confirmed(FOOTBALL_IMAGE_LICENSE_CONFIRMED_RAW)


def central_scheduler_loop() -> None:
    """جایگزین poster_loop: یک Thread واحد برای هر هفت نوع پیام روزانه +
    فوتبال. اگر توکن/کانال نباشد، مثل قبل غیرفعال می‌ماند."""
    if not SENDER_BOT_TOKEN or not CHANNEL_ID:
        log("[CENTRAL] Disabled — SENDER_BOT_TOKEN or CHANNEL_ID is missing.")
        return
    context = _build_central_context()
    state = state_store.load_json(CENTRAL_SCHEDULER_STATE_PATH, lambda: {})

    def _persist(s: Dict[str, Any]) -> None:
        state_store.save_json(CENTRAL_SCHEDULER_STATE_PATH, s)

    log("[CENTRAL] central_scheduler started (tick={}s, tz={})".format(CENTRAL_SCHEDULER_TICK_SECONDS, FOOTBALL_TIMEZONE))
    central_scheduler.run_forever(context, state, SHUTDOWN, _persist)


# ============================================================
# BACKGROUND THREAD STARTUP (GUNICORN-SAFE PER WORKER)
# ============================================================
_startup_lock = threading.Lock()
_startup_done = False


def start_background_threads() -> None:
    global _startup_done
    with _startup_lock:
        if _startup_done:
            return
        _startup_done = True
    log("[BOOT] Background threads started in worker.")
    threading.Thread(target=run_async_loops, daemon=True, name="async-core").start()
    threading.Thread(target=central_scheduler_loop, daemon=True, name="central_scheduler").start()


@app.before_request
def _ensure_threads_started() -> None:
    start_background_threads()


# ============================================================
# FLASK ROUTES
# ============================================================
@app.after_request
def add_headers(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/")
def prices() -> Response:
    with state_lock:
        payload = json.dumps(STATE, ensure_ascii=False, separators=(",", ":"))
    return Response(payload, status=200, content_type="application/json; charset=utf-8")


@app.route("/health")
def health() -> Response:
    now = time.time()
    with state_lock:
        scheduler_age = now - RUNTIME.get("scheduler_heartbeat", 0.0) if RUNTIME.get("scheduler_heartbeat") else None
        display_age = now - RUNTIME.get("display_heartbeat", 0.0) if RUNTIME.get("display_heartbeat") else None
    if now - RUNTIME["started_at"] < 60:
        return Response("OK", status=200, content_type="text/plain; charset=utf-8")
    if scheduler_age is None or scheduler_age > 10 * 60:
        return Response("scheduler_stale", status=503, content_type="text/plain; charset=utf-8")
    if display_age is None or display_age > 10 * 60:
        return Response("display_stale", status=503, content_type="text/plain; charset=utf-8")
    return Response("OK", status=200, content_type="text/plain; charset=utf-8")


@app.route("/health/details")
def health_details() -> Response:
    with state_lock:
        body = {
            "status": "ok",
            "started_at": datetime.fromtimestamp(RUNTIME["started_at"], timezone.utc).isoformat(),
            "scheduler_heartbeat": RUNTIME.get("scheduler_heartbeat"),
            "display_heartbeat": RUNTIME.get("display_heartbeat"),
            "telegram_last_fetch": RUNTIME.get("telegram_last_fetch"),
            "scheduler_errors": RUNTIME.get("scheduler_errors"),
            "display_errors": RUNTIME.get("display_errors"),
            "last_real_update": RUNTIME.get("last_real_update"),
        }
    return Response(json.dumps(body, ensure_ascii=False, separators=(",", ":")), status=200, content_type="application/json; charset=utf-8")


def _handle_signal(signum: int, _frame: Any) -> None:
    log("[SYSTEM] Shutdown signal received: {}".format(signum))
    SHUTDOWN.set()


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except Exception:
        pass

    start_background_threads()
    log("[BOOT] Starting local Flask server...")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
