import os
import json
import re
import time
import random
import asyncio
import threading
import tempfile
import signal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, Optional, List, Tuple

import requests
import jdatetime
from flask import Flask, Response
from telethon import TelegramClient
try:
    from telethon.sessions import StringSession
except Exception:
    StringSession = None

# ============================================================
# CONFIG
# ============================================================
# Secrets را داخل Environment Variable نگه دار.
# مثال Render:
# TG_API_ID=2040
# TG_API_HASH=...
# TG_SESSION=session3

API_ID = int(os.environ.get("TG_API_ID", "2040"))
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION = os.environ.get("TG_SESSION", "session3").strip()
TG_SESSION_STRING = os.environ.get("TG_SESSION_STRING", "").strip()

BOT_VH = os.environ.get("BOT_VH", "@VoucherHub_bot").strip()
OUTPUT = os.environ.get("OUTPUT_FILE", "Ali1377.01.05.json").strip()

# Telegram channel poster. Token and channel ID are intentionally required;
# public URLs/usernames have safe defaults and can be overridden in Render.
SENDER_BOT_TOKEN = os.environ.get("SENDER_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@Vochino_bh01").strip()
MAIN_BOT_URL = os.environ.get(
    "MAIN_BOT_URL", "https://t.me/Vochino_bh01_bot"
).strip()
SWAPWALLET_PRICES_URL = "https://swapwallet.app/api/v1/market/prices"
POSTER_INTERVAL_SECONDS = 3600
POSTER_JITTER_SECONDS = 60
POSTER_INITIAL_DELAY_SECONDS = 60
POSTER_HTTP_TIMEOUT = 10
POSTER_RETRIES = 1  # one retry means at most two attempts per request

PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")

# Keepalive واقعیِ کم‌مصرف: هر حدود 11.5 تا 13.5 دقیقه یک درخواست به URL عمومی.
# این فقط وقتی PUBLIC_BASE_URL/RENDER_EXTERNAL_URL وجود داشته باشد فعال است.
KEEPALIVE_MIN_SECONDS = 11 * 60 + 30
KEEPALIVE_MAX_SECONDS = 13 * 60 + 30

# VoucherHub and crypto remain on their existing schedule.
API_FETCHES_PER_HOUR = 2

 # Asset groups: Telegram requests are subject to MIN_TELEGRAM_GAP;
# API requests are completely independent from that gap.
TELEGRAM_ASSETS = ("u_prem",)
API_ASSETS = ("crypto",)

# حداقل فاصله بین دو عملیات Telegram برای جلوگیری از فشار اضافه.
MIN_TELEGRAM_GAP = 10 * 60

# اگر یک Slot بیشتر از این مقدار عقب افتاده باشد، به‌جای ایجاد Burst از آن عبور می‌کنیم.
MAX_CATCHUP_AGE = 6 * 60

# بعد از هر Fetch ناموفق، فقط یک Retry محدود انجام می‌شود.
FETCH_RETRIES = 1

# نوسان نمایشی؛ هیچ Request خارجی در این فاصله انجام نمی‌شود.
DISPLAY_MIN_SECONDS = 18
DISPLAY_MAX_SECONDS = 34

# یک Tick مدیریتی سبک برای تشخیص زنده بودن Loopها.
HEARTBEAT_SECONDS = 30

# محدوده منطقی برای نرخ‌های تومانی محصولات.
MIN_TOMAN_RATE = 10_000
MAX_TOMAN_RATE = 1_000_000_000

# محدوده منطقی نرخ‌های USD API.
MIN_USD_RATE = 0.50
MAX_USD_RATE = 2.00

# ============================================================
# APP + LOCKS + RUNTIME STATE
# ============================================================
app = Flask(__name__)
state_lock = threading.RLock()
print_lock = threading.Lock()
telegram_lock: Optional[asyncio.Lock] = None

RUNTIME = {
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

# آخرین suffix نمایشی هر نرخ؛ فقط در RAM است و نیازی به ذخیره دائمی ندارد.
DISPLAY_RUNTIME: Dict[str, int] = {}

# Scheduler یک صف Slot سبک در RAM دارد.
SCHEDULES: Dict[str, Dict[str, Any]] = {}

SHUTDOWN = threading.Event()

# ============================================================
# LOGGING
# ============================================================
def log(msg: str) -> None:
    with print_lock:
        print(
            "[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg),
            flush=True,
        )


# ============================================================
# TEXT / NUMBER HELPERS
# ============================================================
def normalize_fa_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "‌": " ",
        "‍": " ",
        "ـ": "",
        "أ": "ا",
        "إ": "ا",
        "ؤ": "و",
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    # حذف اعراب رایج
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def digits_to_ascii(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    return s.translate(table)


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
    if value is None:
        return None
    s = digits_to_ascii(value)
    s = s.replace(",", ".").replace("٬", ".").replace("،", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except (TypeError, ValueError):
        return None


def valid_toman_rate(value: Any) -> bool:
    return (
        isinstance(value, int)
        and MIN_TOMAN_RATE <= value <= MAX_TOMAN_RATE
    )


def valid_usd_rate(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and MIN_USD_RATE <= float(value) <= MAX_USD_RATE
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# DISPLAY FLUCTUATION
# ============================================================
def prefix_from_base(price: int) -> int:
    """همه ارقام قبل از سه رقم آخر را ثابت نگه می‌دارد."""
    return (price // 1000) * 1000


def suffix_from_value(value: int) -> int:
    return int(value % 1000)


def compose_display(base_price: int, suffix: int) -> int:
    """هرگز اجازه نمی‌دهد نمایش از Prefix + 999 عبور کند."""
    suffix = max(0, min(999, int(suffix)))
    return prefix_from_base(base_price) + suffix


def initial_suffix(base_price: int, previous: Optional[int] = None) -> int:
    # نوسان اولیه واقعی ولی بدون لو دادن اجباری سه رقم آخر منبع.
    if previous is not None:
        old = suffix_from_value(previous)
        candidate = old + random.randint(-80, 80)
        candidate = max(0, min(999, candidate))
        if candidate == old:
            candidate = (old + random.randint(11, 47)) % 1000
        return candidate
    return random.randint(20, 979)


def next_suffix(current: int) -> int:
    """Random Walk سبک برای نمودار؛ بدون Request و بدون جهش مصنوعی دائمی."""
    current = max(0, min(999, int(current)))

    step = random.randint(7, 42)
    if random.random() < 0.08:
        step = random.randint(43, 78)

    # نزدیک لبه‌ها جهت را ترجیحاً به سمت داخل می‌بریم.
    if current <= 70:
        direction = 1
    elif current >= 929:
        direction = -1
    else:
        direction = 1 if random.random() >= 0.50 else -1

    candidate = current + direction * step

    # بازتاب نرم در محدوده 0..999؛ بنابراین هرگز 1000 نمی‌شود.
    while candidate < 0 or candidate > 999:
        if candidate < 0:
            candidate = abs(candidate)
        elif candidate > 999:
            candidate = 1998 - candidate

    # جلوگیری از تکرار بی‌دلیل
    if candidate == current:
        candidate = min(999, current + 9) if current < 500 else max(0, current - 9)

    return candidate


# ============================================================
# JSON STATE — IN MEMORY + ATOMIC PERSISTENCE
# ============================================================
def _fresh_state() -> Dict[str, Any]:
    return {
        "ali1377": {},
        "meta": {
            "schema_version": 2,
            "last_real_update": None,
            "display_updated_at": None,
            "sources": {},
        },
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

# PS Voucher service has been disabled. Remove any stale PS fields from an older JSON
# so the endpoint cannot continue exposing obsolete PS prices.
def remove_disabled_ps_state() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        removed = [key for key in list(ali.keys()) if key == "ps_buy" or key == "ps_sell" or key.startswith("ps_buy_") or key.startswith("ps_sell_")]
        for key in removed:
            ali.pop(key, None)
        sources = STATE.setdefault("meta", {}).setdefault("sources", {})
        sources.pop("ps", None)
        if removed:
            STATE["last_updated"] = iso_now()

remove_disabled_ps_state()


def _atomic_write_locked(state: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(OUTPUT)) or "."
    os.makedirs(directory, exist_ok=True)

    payload = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
    )

    fd = None
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=".prices-",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
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


# Persist once after startup cleanup so disabled PS fields do not return after restart.
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


def mark_source(
    source_name: str,
    *,
    success: bool,
    message: str = "",
    persist: bool = True,
) -> None:
    stamp = iso_now()

    def _mutate(state: Dict[str, Any]) -> None:
        meta = state.setdefault("meta", {})
        sources = meta.setdefault("sources", {})
        item = sources.setdefault(source_name, {})
        item["last_attempt"] = stamp
        if success:
            item["last_success"] = stamp
            item["last_error"] = None
        else:
            item["last_error"] = message[:500]

    mutate_state(_mutate, persist=persist)


# ============================================================
# DISPLAY STATE INIT / UPDATE
# ============================================================
def init_display_runtime() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        for key, value in list(ali.items()):
            if not key.endswith("_base"):
                continue
            base = value
            if not valid_toman_rate(base):
                continue
            display_key = key[:-5]
            old_display = ali.get(display_key)
            if isinstance(old_display, int):
                # اگر Prefix با Base هم‌خوان باشد، از همان suffix ادامه می‌دهیم.
                if prefix_from_base(old_display) == prefix_from_base(base):
                    DISPLAY_RUNTIME[display_key] = suffix_from_value(old_display)
                else:
                    DISPLAY_RUNTIME[display_key] = initial_suffix(base)
            else:
                DISPLAY_RUNTIME[display_key] = initial_suffix(base)
                ali[display_key] = compose_display(base, DISPLAY_RUNTIME[display_key])


init_display_runtime()


def set_new_real_rate(ali_key: str, value: int) -> None:
    if not valid_toman_rate(value):
        raise ValueError("invalid toman rate for " + ali_key)

    base_key = ali_key + "_base"
    previous_display = STATE.get("ali1377", {}).get(ali_key)
    STATE["ali1377"][base_key] = value
    DISPLAY_RUNTIME[ali_key] = initial_suffix(value, previous_display)
    STATE["ali1377"][ali_key] = compose_display(value, DISPLAY_RUNTIME[ali_key])


def update_display_tick() -> None:
    with state_lock:
        ali = STATE.setdefault("ali1377", {})
        changed = False
        for ali_key, base_value in list(ali.items()):
            if not ali_key.endswith("_base"):
                continue
            display_key = ali_key[:-5]
            if not valid_toman_rate(base_value):
                continue

            current_suffix = DISPLAY_RUNTIME.get(display_key)
            if current_suffix is None:
                current_suffix = initial_suffix(base_value)

            new_suffix = next_suffix(current_suffix)
            DISPLAY_RUNTIME[display_key] = new_suffix
            new_display = compose_display(base_value, new_suffix)
            if ali.get(display_key) != new_display:
                ali[display_key] = new_display
                changed = True

        if changed:
            STATE.setdefault("meta", {})["display_updated_at"] = iso_now()
        RUNTIME["display_heartbeat"] = time.time()


# ============================================================
# TELEGRAM HELPERS
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


async def recent_messages_text(client: TelegramClient, bot: Any, limit: int = 12) -> str:
    messages = await client.get_messages(bot, limit=limit)
    parts: List[str] = []
    for msg in messages:
        if getattr(msg, "text", None):
            parts.append(str(msg.text))
    return "\n".join(parts)


async def click_button(client: TelegramClient, bot: Any, patterns: List[str], timeout: float = 9.0) -> bool:
    normalized_patterns = [normalize_fa_text(p) for p in patterns]
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        messages = await client.get_messages(bot, limit=8)
        for msg in messages:
            buttons = getattr(msg, "buttons", None)
            if not buttons:
                continue
            for row in buttons:
                for button in row:
                    text = normalize_fa_text(getattr(button, "text", ""))
                    if not text:
                        continue
                    if any(pattern in text for pattern in normalized_patterns):
                        log("[TELEGRAM] Click: " + text)
                        await button.click()
                        await asyncio.sleep(random.uniform(1.8, 3.0))
                        return True
        await asyncio.sleep(0.8)
    return False


async def get_price_text(client: TelegramClient, bot: Any, must_contain: Optional[List[str]] = None) -> str:
    deadline = time.monotonic() + 8.0
    patterns = [normalize_fa_text(x) for x in (must_contain or [])]
    last = ""

    while time.monotonic() < deadline:
        last = await recent_messages_text(client, bot, limit=12)
        normalized = normalize_fa_text(last)
        if last and (not patterns or all(p in normalized for p in patterns)):
            return last
        await asyncio.sleep(0.9)

    return last


async def fetch_vh(client: TelegramClient) -> Dict[str, int]:
    """U Voucher + Premium Voucher خرید و فروش از BOT_VH.

    این تابع عمداً به وجود کلمه «قیمت» در پیام وابسته نیست؛ چون ممکن است
    متن Bot بین نسخه‌ها/پیام‌ها تغییر کند. کل پیام‌های اخیر جمع می‌شوند و
    چهار نرخ مستقل از روی برچسب خودشان استخراج می‌شوند.
    """
    if not await ensure_connected(client):
        return {}

    bot = await client.get_entity(BOT_VH)

    # بعضی نسخه‌های Bot قبل از /prices به /start نیاز دارند.
    try:
        await client.send_message(bot, "/start")
        await asyncio.sleep(random.uniform(1.2, 2.0))
    except Exception as exc:
        log("[VH] /start warning: " + str(exc))

    await client.send_message(bot, "/prices")
    await asyncio.sleep(random.uniform(2.0, 3.4))

    # فقط به «قیمت» وابسته نباش؛ پیام ممکن است «نرخ»، «لیست نرخ‌ها» یا متن
    # دیگری داشته باشد. get_price_text در اینجا صرفاً برای صبر کردن تا پیام است.
    text = await get_price_text(client, bot)
    normalized = normalize_fa_text(text)
    normalized_digits = digits_to_ascii(normalized)

    if not normalized_digits:
        log("[VH] no text received after /prices")
        return {}

    def extract_variants(variants: List[str]) -> Optional[int]:
        """چند فرم رایج برچسب را امتحان می‌کند و اولین نرخ معتبر را برمی‌گرداند."""
        for label in variants:
            label_pattern = label
            patterns = [
                # label : 123456
                re.compile(
                    label_pattern + r"\s*(?:[:：]|=>|=|-)\s*\**([\d,٬،\u06f0-\u06f9]+)",
                    flags=re.IGNORECASE,
                ),
                # label ... 123456  (برای پیام‌هایی که colon ندارند)
                re.compile(
                    label_pattern + r"[^\n\d]{0,80}(\d[\d,٬،\u06f0-\u06f9]{3,})",
                    flags=re.IGNORECASE,
                ),
            ]
            for pattern in patterns:
                match = pattern.search(normalized_digits)
                if not match:
                    continue
                value = parse_int(match.group(1))
                if valid_toman_rate(value):
                    return value
        return None

    result = {
        "premium_buy": extract_variants([
            r"خرید\s+پریمیوم(?:\s+ووچر)?",
            r"پریمیوم(?:\s+ووچر)?\s+خرید",
        ]),
        "premium_sell": extract_variants([
            r"فروش\s+پریمیوم(?:\s+ووچر)?",
            r"پریمیوم(?:\s+ووچر)?\s+فروش",
        ]),
        "u_buy": extract_variants([
            r"خرید\s+یو(?:\s+ووچر)?",
            r"یو(?:\s+ووچر)?\s+خرید",
        ]),
        "u_sell": extract_variants([
            r"فروش\s+یو(?:\s+ووچر)?",
            r"یو(?:\s+ووچر)?\s+فروش",
        ]),
    }

    clean = {k: v for k, v in result.items() if v is not None}
    for k, v in clean.items():
        log("[VH {} OK] {}".format(k.upper(), v))

    if not clean:
        # لاگ تشخیصی محدود؛ برای پیدا کردن تغییر متن Bot در Render مفید است.
        log("[VH] no valid prices extracted. Recent text:")
        log(normalized_digits[:1800].replace("\n", " | "))

    return clean


# ============================================================
# EXTERNAL APIs
# ============================================================
def _http_get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """GET JSON with explicit status/error logging; never returns fake data."""
    request_headers = {
        "User-Agent": "Mozilla/5.0 PriceService/3.0",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    try:
        response = requests.get(
            url,
            params=params,
            headers=request_headers,
            timeout=timeout,
        )
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
        return None
    except ValueError as exc:
        log("[API] invalid JSON {}: {}".format(url, exc))
        return None
    except Exception as exc:
        log("[API] unexpected error {}: {}".format(url, exc))
        return None


def _coingecko_headers() -> Dict[str, str]:
    """اگر Demo API Key تنظیم شده باشد، همان را استفاده می‌کنیم؛ وگرنه public endpoint."""
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": key} if key else {}


def _tether_coingecko() -> Optional[float]:
    data = _http_get_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "tether", "vs_currencies": "usd"},
        headers=_coingecko_headers(),
    )
    try:
        value = parse_float(data.get("tether", {}).get("usd")) if data else None
        return value if valid_usd_rate(value) else None
    except Exception:
        return None


def _tether_binance() -> Optional[float]:
    data = _http_get_json(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "USDCUSDT"},
    )
    try:
        v = parse_float(data.get("price")) if data else None
        if v is None or v <= 0:
            return None
        value = 1.0 / v
        return value if valid_usd_rate(value) else None
    except Exception:
        return None


def _tether_kraken() -> Optional[float]:
    data = _http_get_json(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": "USDTZUSD"},
    )
    try:
        result = data.get("result", {}) if data else {}
        for item in result.values():
            last = item.get("c", [None])[0]
            value = parse_float(last)
            if valid_usd_rate(value):
                return value
    except Exception:
        return None
    return None


def _tether_coinpaprika() -> Optional[float]:
    # منبع واقعی جایگزین برای زمانی که CoinGecko/Binance محدود یا مسدود باشند.
    data = _http_get_json("https://api.coinpaprika.com/v1/tickers/usdt-tether")
    try:
        value = parse_float(data.get("quotes", {}).get("USD", {}).get("price")) if data else None
        return value if valid_usd_rate(value) else None
    except Exception:
        return None


def fetch_tether() -> Optional[float]:
    for name, fn in (
        ("CoinGecko", _tether_coingecko),
        ("Kraken", _tether_kraken),
        ("Binance", _tether_binance),
        ("CoinPaprika", _tether_coinpaprika),
    ):
        value = fn()
        if value is not None:
            value = round(float(value), 6)
            log("[OK] tether_usd={} source={}".format(value, name))
            return value
    log("[WARN] tether_usd: all real sources failed; previous value is preserved")
    return None


def _utopia_coinpaprika() -> Optional[float]:
    # این ID متعلق به Utopia USD (UUSD) است؛ با UTOPIA توکن اشتباه نشود.
    data = _http_get_json(
        "https://api.coinpaprika.com/v1/tickers/uusd-utopia-usd"
    )
    try:
        value = parse_float(
            data.get("quotes", {}).get("USD", {}).get("price")
        ) if data else None
        return value if valid_usd_rate(value) else None
    except Exception:
        return None


def fetch_utopia() -> Optional[float]:
    value = _utopia_coinpaprika()
    if value is not None:
        value = round(float(value), 6)
        log("[OK] utopia_usd={} source=CoinPaprika/Utopia-USD".format(value))
        return value
    log("[WARN] utopia_usd: real source failed; previous value is preserved")
    return None


def fetch_crypto() -> Dict[str, float]:
    """هر دو نرخ را مستقل می‌گیرد؛ شکست یکی مانع ثبت دیگری نمی‌شود."""
    result: Dict[str, float] = {}

    tether = fetch_tether()
    if tether is not None:
        result["tether_usd"] = tether

    utopia = fetch_utopia()
    if utopia is not None:
        result["utopia_usd"] = utopia

    log("[CRYPTO] fetched keys=" + ",".join(sorted(result.keys())) if result else "[CRYPTO] no valid values")
    return result


# ============================================================
# APPLY REAL FETCHES
# ============================================================
def apply_vh_result(result: Dict[str, int]) -> int:
    applied = 0

    def _mutate(state: Dict[str, Any]) -> None:
        nonlocal applied
        ali = state.setdefault("ali1377", {})
        for key in ("premium_buy", "premium_sell", "u_buy", "u_sell"):
            value = result.get(key)
            if valid_toman_rate(value):
                previous_display = ali.get(key)
                ali[key + "_base"] = value
                DISPLAY_RUNTIME[key] = initial_suffix(value, previous_display)
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
                ali[key] = round(float(value), 6)
                applied += 1
        if applied:
            state.setdefault("meta", {})["last_real_update"] = iso_now()

    mutate_state(_mutate, persist=True)
    return applied


# ============================================================
# RETRY WRAPPER
# ============================================================
async def run_with_retry(
    name: str,
    fn: Callable[[], Any],
    retries: int = FETCH_RETRIES,
) -> Any:
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                delay = 2.0 + attempt * 3.0 + random.uniform(0.0, 1.5)
                log("[RETRY] {} in {:.1f}s — {}".format(name, delay, exc))
                await asyncio.sleep(delay)
            else:
                log("[ERROR] {} failed: {}".format(name, exc))

    raise last_error if last_error else RuntimeError(name + " failed")


# ============================================================
# RANDOM 2x/HOUR SCHEDULER
# ============================================================
def hour_start_epoch(ts: float) -> int:
    return int(ts // 3600) * 3600


def generate_two_slots(hour_epoch: int) -> List[float]:
    # Crypto/API assets: دو Fetch واقعی در هر ساعت.
    candidates = list(range(60, 3540))
    for _ in range(80):
        chosen = sorted(random.sample(candidates, API_FETCHES_PER_HOUR))
        a, b = chosen
        if b - a >= 12 * 60:
            return [hour_epoch + a, hour_epoch + b]
    # Fallback الگوریتمی، بدون حدس نرخ یا داده.
    return [hour_epoch + 10 * 60, hour_epoch + 45 * 60]


def init_schedules(now: float) -> None:
    # Warm-up جداگانه انجام می‌شود؛ بنابراین برنامه‌ی منظم از ساعت بعد
    # شروع می‌شود و Warm-up باعث سه Fetch در یک ساعت نمی‌شود.
    next_hour = hour_start_epoch(now) + 3600
    for asset in ("u_prem", "crypto"):
        SCHEDULES[asset] = {
            "slots": generate_two_slots(next_hour) + generate_two_slots(next_hour + 3600),
            "through_hour": next_hour + 3600,
        }
        SCHEDULES[asset]["slots"].sort()


def extend_schedule(asset: str, now: float) -> None:
    item = SCHEDULES[asset]
    target_hour = hour_start_epoch(now) + 3600
    while item["through_hour"] < target_hour:
        item["through_hour"] += 3600
        item["slots"].extend(generate_two_slots(item["through_hour"]))
    item["slots"].sort()


def pop_due_slot(asset: str, now: float) -> Optional[float]:
    extend_schedule(asset, now)
    slots: List[float] = SCHEDULES[asset]["slots"]

    while slots and slots[0] < now - MAX_CATCHUP_AGE:
        missed = slots.pop(0)
        log("[SCHED] {} missed stale slot at {}".format(asset, datetime.fromtimestamp(missed, timezone.utc).isoformat()))

    if slots and slots[0] <= now:
        return slots.pop(0)
    return None


def discard_stale_slots(asset: str, now: float) -> None:
    """بعد از خواب/وقفه، Slotهای قدیمی را کنار می‌گذارد تا Requestها Burst نشوند."""
    extend_schedule(asset, now)
    slots: List[float] = SCHEDULES[asset]["slots"]
    while slots and slots[0] < now - MAX_CATCHUP_AGE:
        missed = slots.pop(0)
        log(
            "[SCHED] {} skipped stale slot at {}".format(
                asset,
                datetime.fromtimestamp(missed, timezone.utc).isoformat(),
            )
        )


def next_scheduled_time(asset: str, now: float) -> float:
    extend_schedule(asset, now)
    slots = SCHEDULES[asset]["slots"]
    return slots[0] if slots else now + 60


# ============================================================
# SCHEDULER LOOP
# ============================================================
async def scheduler_loop(client: TelegramClient) -> None:
    global telegram_lock

    if telegram_lock is None:
        telegram_lock = asyncio.Lock()

    log("[SCHED] Scheduler started — VH + crypto scheduled fetches/hour")
    init_schedules(time.time())

    # Warm-up کوتاه؛ باعث می‌شود بعد از Restart لازم نباشد تا Slot دور بعد صبر کنیم.
    warmups = [
        ("u_prem", random.uniform(42, 65)),
        ("crypto", random.uniform(72, 95)),
    ]
    warmup_done: Dict[str, bool] = {asset: False for asset, _ in warmups}
    warmup_target: Dict[str, float] = {
        asset: time.time() + delay for asset, delay in warmups
    }

    last_tg = 0.0

    while not SHUTDOWN.is_set():
        RUNTIME["scheduler_heartbeat"] = time.time()
        now = time.time()

        # ---------------- Warm-up ----------------
        # API warm-ups are completely independent from Telegram warm-ups.
        # مهم: اگر u_prem به‌دلیل MIN_TELEGRAM_GAP فعلاً قابل اجرا نباشد،
        # نباید با continue جلوی crypto را بگیرد.
        warm_due_api = [
            asset for asset in warmup_done
            if (
                not warmup_done[asset]
                and asset in API_ASSETS
                and now >= warmup_target[asset]
            )
        ]

        if warm_due_api:
            for asset in warm_due_api:
                try:
                    result = await run_with_retry(
                        "{} warm-up".format(asset),
                        lambda: asyncio.to_thread(fetch_crypto),
                    )
                    applied = apply_crypto_result(result)
                    mark_source(
                        asset,
                        success=applied > 0,
                        message="no valid values" if applied == 0 else "",
                        persist=False,
                    )
                    if applied:
                        RUNTIME["last_real_update"] = iso_now()
                    with state_lock:
                        _atomic_write_locked(STATE)
                except Exception as exc:
                    RUNTIME["scheduler_errors"] += 1
                    mark_source(asset, success=False, message=str(exc), persist=False)
                    with state_lock:
                        _atomic_write_locked(STATE)
                    log("[WARMUP ERROR] crypto: {}".format(exc))
                finally:
                    # Crypto must never be blocked by Telegram's MIN_TELEGRAM_GAP.
                    warmup_done[asset] = True
            # اجازه بده در همان چرخه سراغ Telegram هم برویم، بدون continue اجباری.

        warm_due_tg = [
            asset for asset in warmup_done
            if (
                not warmup_done[asset]
                and asset in TELEGRAM_ASSETS
                and now >= warmup_target[asset]
            )
        ]

        if warm_due_tg:
            # قدیمی‌ترین Telegram warm-up را انتخاب کن؛ APIها بالاتر رسیدگی شدند.
            asset = min(warm_due_tg, key=lambda x: warmup_target[x])
            gap = now - last_tg

            if gap < MIN_TELEGRAM_GAP:
                # u_prem را در همین حالت pending نگه می‌داریم، اما target را
                # به اولین زمان مجاز منتقل می‌کنیم تا loop بیهوده تکرار نشود.
                next_allowed = last_tg + MIN_TELEGRAM_GAP
                if warmup_target[asset] < next_allowed:
                    warmup_target[asset] = next_allowed
                log(
                    "[SCHED] Telegram warm-up {} delayed for {:.0f}s; API warm-ups remain independent"
                    .format(asset, max(0.0, next_allowed - now))
                )
            else:
                try:
                    async with telegram_lock:
                        result = await run_with_retry("VH warm-up", lambda: fetch_vh(client))
                        applied = apply_vh_result(result)
                    last_tg = time.time()
                    RUNTIME["telegram_last_fetch"] = last_tg
                    mark_source(
                        asset,
                        success=applied > 0,
                        message="no valid values" if applied == 0 else "",
                        persist=False,
                    )
                    if applied:
                        RUNTIME["last_real_update"] = iso_now()
                    with state_lock:
                        _atomic_write_locked(STATE)
                except Exception as exc:
                    RUNTIME["scheduler_errors"] += 1
                    mark_source(asset, success=False, message=str(exc), persist=False)
                    with state_lock:
                        _atomic_write_locked(STATE)
                    log("[WARMUP ERROR] {}: {}".format(asset, exc))
                finally:
                    # حتی اگر Fetch ناموفق باشد، Scheduler نباید در این Warm-up قفل شود.
                    warmup_done[asset] = True

                continue

        # ---------------- Regular scheduled fetch ----------------
        # اگر Process بعد از Sleep/Restart با چند Slot قدیمی برگشت، آنها را
        # Burst نمی‌کنیم؛ فقط نزدیک‌ترین Slot معتبر باقی می‌ماند.
        for asset in ("u_prem", "crypto"):
            discard_stale_slots(asset, now)

        due_assets = []
        for asset in ("u_prem", "crypto"):
            due = SCHEDULES[asset]["slots"]
            if due and due[0] <= now:
                due_assets.append((due[0], asset))

        if due_assets:
            due_assets.sort()
            asset = due_assets[0][1]
            # Remove the due slot now; next hour is already queued.
            SCHEDULES[asset]["slots"].pop(0)

            applied = 0

            if asset in TELEGRAM_ASSETS:
                gap = now - last_tg
                if gap < MIN_TELEGRAM_GAP:
                    wait = MIN_TELEGRAM_GAP - gap
                    log("[SCHED] Telegram gap %.0fs — delaying %s" % (wait, asset))
                    # همان Slot را با تأخیر کنترل‌شده برمی‌گردانیم.
                    delayed = time.time() + wait
                    SCHEDULES[asset]["slots"].insert(0, delayed)
                    SCHEDULES[asset]["slots"].sort()
                    await asyncio.sleep(min(30, max(5, wait)))
                    continue

                try:
                    async with telegram_lock:
                        result = await run_with_retry("VH fetch", lambda: fetch_vh(client))
                        applied = apply_vh_result(result)
                    last_tg = time.time()
                    RUNTIME["telegram_last_fetch"] = last_tg
                    mark_source(asset, success=applied > 0, message="no valid values" if applied == 0 else "", persist=False)
                    if applied:
                        RUNTIME["last_real_update"] = iso_now()
                    with state_lock:
                        _atomic_write_locked(STATE)
                except Exception as exc:
                    RUNTIME["scheduler_errors"] += 1
                    mark_source(asset, success=False, message=str(exc), persist=False)
                    log("[SCHED ERROR] {}: {}".format(asset, exc))
                    # وضعیت خطا را یک بار در فایل ذخیره می‌کنیم، نه با دو Write.
                    with state_lock:
                        _atomic_write_locked(STATE)
            else:
                try:
                    result = await run_with_retry(
                        "Crypto fetch",
                        lambda: asyncio.to_thread(fetch_crypto),
                    )
                    applied = apply_crypto_result(result)
                    mark_source(asset, success=applied > 0, message="no valid values" if applied == 0 else "", persist=False)
                    if applied:
                        RUNTIME["last_real_update"] = iso_now()
                    with state_lock:
                        _atomic_write_locked(STATE)
                except Exception as exc:
                    RUNTIME["scheduler_errors"] += 1
                    mark_source(asset, success=False, message=str(exc), persist=False)
                    log("[SCHED ERROR] crypto: {}".format(exc))
                    with state_lock:
                        _atomic_write_locked(STATE)

            # apply_* ابتدا State را در RAM به‌روز می‌کند و سپس یک Atomic Write
            # در همین چرخه انجام می‌شود تا Base/Display و Metadata هماهنگ باشند.
            log("[SCHED] {} completed; applied={}".format(asset, applied))
            continue

        # ---------------- Smart sleep ----------------
        next_times = [next_scheduled_time(asset, now) for asset in ("u_prem", "crypto")]
        if not all(warmup_done.values()):
            next_times.extend(
                warmup_target[a]
                for a, done in warmup_done.items()
                if not done
            )
        wait = max(2.0, min(45.0, min(next_times) - time.time()))
        await asyncio.sleep(wait)


# ============================================================
# DISPLAY LOOP
# ============================================================
async def display_loop() -> None:
    log("[DISPLAY] Low-cost display fluctuation started")
    while not SHUTDOWN.is_set():
        try:
            update_display_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            RUNTIME["display_errors"] += 1
            log("[DISPLAY ERROR] " + str(exc))
        await asyncio.sleep(random.uniform(DISPLAY_MIN_SECONDS, DISPLAY_MAX_SECONDS))


# ============================================================
# APPLICATION WATCHDOG / SUPERVISOR
# ============================================================
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
            RUNTIME["scheduler_errors"] += 1 if name == "scheduler" else 0
            log("[SUPERVISOR] {} stopped: {}".format(name, exc))
            backoff = min(60, 3 * failures) + random.uniform(0, 2)
            await asyncio.sleep(backoff)


async def heartbeat_loop() -> None:
    while not SHUTDOWN.is_set():
        RUNTIME["scheduler_heartbeat"] = RUNTIME.get("scheduler_heartbeat") or time.time()
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def keepalive_loop() -> None:
    """
    Keepalive شبکه‌ایِ کم‌مصرف برای سرویس‌های Web روی Render.

    نکته: این مکانیزم فقط تا وقتی Process در حال اجراست کار می‌کند و
    تضمین مطلق برای اجرای دائمی زیرساخت نیست.
    """
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
            # درخواست عمداً فقط به Health endpoint می‌رود؛ هیچ Fetch قیمت انجام نمی‌شود.
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


def _ping_public_health(url: str) -> bool:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "PriceService-Keepalive/2.0"},
            timeout=10,
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def build_telegram_client() -> TelegramClient:
    """در صورت وجود String Session از آن استفاده می‌کنیم تا Auth با Restart/Spin-down فایل محلی وابسته نباشد."""
    if TG_SESSION_STRING:
        if StringSession is None:
            raise RuntimeError("Telethon StringSession is unavailable")
        return TelegramClient(StringSession(TG_SESSION_STRING), API_ID, API_HASH)
    return TelegramClient(SESSION, API_ID, API_HASH)


async def loops_main() -> None:
    if not API_HASH:
        log("[FATAL] TG_API_HASH environment variable is missing. No Telegram connection will be attempted.")
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
            asyncio.create_task(
                supervised_loop(
                    "scheduler",
                    lambda: scheduler_loop(client),
                )
            ),
            asyncio.create_task(
                supervised_loop("display", display_loop)
            ),
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
# TELEGRAM PRICE POSTER
# ============================================================
_POSTER_SWAP_KEYS = (
    "USDT/IRT",
    "TRX/IRT",
    "TON/IRT",
    "BTC/IRT",
    "ETH/IRT",
    "FET/IRT",
)
_PERSIAN_WEEKDAYS = (
    "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"
)
_PERSIAN_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _poster_persian_digits(value: Any) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def _poster_parse_price(value: Any) -> Optional[int]:
    """Parse an actual positive price; invalid/missing values are never fabricated."""
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
        return int(round(number))
    except (TypeError, ValueError, OverflowError):
        return None


def _poster_format_price(value: Any) -> str:
    parsed = _poster_parse_price(value)
    return f"{parsed:,}" if parsed is not None else "ناموجود"


def _poster_cached_swap_prices() -> Dict[str, int]:
    with state_lock:
        meta = STATE.get("meta", {})
        poster_meta = meta.get("poster", {}) if isinstance(meta, dict) else {}
        cached = poster_meta.get("swapwallet_prices", {}) if isinstance(poster_meta, dict) else {}
        if not isinstance(cached, dict):
            return {}
        clean: Dict[str, int] = {}
        for key in _POSTER_SWAP_KEYS:
            parsed = _poster_parse_price(cached.get(key))
            if parsed is not None:
                clean[key] = parsed
        return clean


def _poster_fetch_swap_prices() -> Dict[str, int]:
    """Fetch SwapWallet prices with timeout=10s and one retry; keep a durable cache."""
    last_error: Optional[str] = None

    for attempt in range(POSTER_RETRIES + 1):
        try:
            response = requests.get(SWAPWALLET_PRICES_URL, timeout=POSTER_HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or str(payload.get("status", "")).upper() != "OK":
                raise ValueError("SwapWallet returned an unexpected status/payload")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("SwapWallet result is not an object")

            fresh: Dict[str, int] = {}
            for key in _POSTER_SWAP_KEYS:
                parsed = _poster_parse_price(result.get(key))
                if parsed is not None:
                    fresh[key] = parsed
            if not fresh:
                raise ValueError("SwapWallet response contained no valid required prices")

            with state_lock:
                meta = STATE.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                    STATE["meta"] = meta
                poster_meta = meta.get("poster")
                if not isinstance(poster_meta, dict):
                    poster_meta = {}
                    meta["poster"] = poster_meta
                cached = poster_meta.get("swapwallet_prices")
                if not isinstance(cached, dict):
                    cached = {}
                    poster_meta["swapwallet_prices"] = cached
                # Merge partial responses with cached values so a missing API key
                # does not erase its last known good price.
                cached.update(fresh)
                poster_meta["swapwallet_last_success"] = iso_now()
                poster_meta["swapwallet_last_error"] = None
                STATE["last_updated"] = iso_now()
                _atomic_write_locked(STATE)
                merged = {
                    key: parsed
                    for key in _POSTER_SWAP_KEYS
                    if (parsed := _poster_parse_price(cached.get(key))) is not None
                }

            log("[POSTER] SwapWallet prices updated: {} key(s)".format(len(fresh)))
            return merged
        except Exception as exc:
            last_error = str(exc)
            log(
                "[POSTER] SwapWallet attempt {}/{} failed: {}".format(
                    attempt + 1, POSTER_RETRIES + 1, last_error
                )
            )
            if attempt < POSTER_RETRIES:
                time.sleep(1.0)

    cached_prices = _poster_cached_swap_prices()
    with state_lock:
        meta = STATE.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            STATE["meta"] = meta
        poster_meta = meta.get("poster")
        if not isinstance(poster_meta, dict):
            poster_meta = {}
            meta["poster"] = poster_meta
        poster_meta["swapwallet_last_error"] = (last_error or "unknown error")[:500]
        STATE["last_updated"] = iso_now()
        _atomic_write_locked(STATE)

    if cached_prices:
        log("[POSTER] SwapWallet unavailable; using cached STATE prices.")
    else:
        log("[POSTER] SwapWallet unavailable and no cached prices exist.")
    return cached_prices


def _poster_tehran_date_time() -> Tuple[str, str]:
    tehran = datetime.now(ZoneInfo("Asia/Tehran"))
    jdate = jdatetime.datetime.fromgregorian(datetime=tehran)
    # Explicit localized names/digits avoid locale-dependent English output from
    # strftime while keeping jdatetime as the Jalali calendar conversion source.
    weekday = _PERSIAN_WEEKDAYS[tehran.weekday()]
    month = _PERSIAN_MONTHS[jdate.month - 1]
    date_text = "{} {} {} {}".format(
        weekday,
        _poster_persian_digits(f"{jdate.day:02d}"),
        month,
        _poster_persian_digits(jdate.year),
    )
    time_text = _poster_persian_digits(tehran.strftime("%H:%M:%S"))
    return date_text, time_text


def _poster_spread_percent(buy_value: Any, sell_value: Any) -> Optional[int]:
    buy = _poster_parse_price(buy_value)
    sell = _poster_parse_price(sell_value)
    if buy is None or sell is None or buy <= 0:
        return None
    # Required formula, constrained to the requested 0..100 display range.
    raw_percent = ((sell - buy) / buy) * 100.0
    return int(round(max(0.0, min(100.0, raw_percent))))


def _poster_progress_bar(percent: Optional[int]) -> str:
    if percent is None:
        filled = 0
    else:
        # Ten percentage points per block gives the requested 83% -> 8/9 example.
        filled = min(9, max(0, int(percent // 10)))
    return "▰" * filled + "▱" * (9 - filled)


def _poster_support_url() -> str:
    support = SUPPORT_USERNAME.strip()
    if support.startswith(("https://t.me/", "http://t.me/")):
        return support
    return "https://t.me/" + support.lstrip("@/")


def _poster_build_message(
    ali_prices: Dict[str, Any],
    swap_prices: Dict[str, Any],
) -> str:
    date_text, time_text = _poster_tehran_date_time()

    u_buy = ali_prices.get("u_buy")
    u_sell = ali_prices.get("u_sell")
    premium_buy = ali_prices.get("premium_buy")
    premium_sell = ali_prices.get("premium_sell")

    u_percent = _poster_spread_percent(u_buy, u_sell)
    premium_percent = _poster_spread_percent(premium_buy, premium_sell)
    u_percent_text = "{}%".format(u_percent) if u_percent is not None else "ناموجود"
    premium_percent_text = "{}%".format(premium_percent) if premium_percent is not None else "ناموجود"

    usdt_irt = _poster_parse_price(swap_prices.get("USDT/IRT"))
    utopia_usd = None
    try:
        with state_lock:
            utopia_usd = parse_float(STATE.get("ali1377", {}).get("utopia_usd"))
    except Exception:
        utopia_usd = None
    utopia_toman = (
        int(round(utopia_usd * usdt_irt))
        if utopia_usd is not None and utopia_usd > 0 and usdt_irt is not None
        else None
    )

    lines = [
        "╭✪ ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫ ✪╮",
        "        🜲 صرافی ووچینو⁰¹",
        "╰┉💲 𓆩𓆩🏦𓆪𓆪 💲┉╯",
        "",
        "📅 {} | ⏰ {}".format(date_text, time_text),
        "",
        "━━━━━━━━━━━━━━━━━━",
        "       🎫 𝒗𝒐𝒖𝒄𝒉𝒆𝒓 🎫",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "🔷 𝑩𝒖𝒚    𝑼-𝑽𝒐𝒖𝒄𝒉𝒆𝒓",
        "     ➫ {} تومان".format(_poster_format_price(u_buy)),
        "🔶 𝑺𝒆𝒍𝒍   𝑼-𝑽𝒐𝒖𝒄𝒉𝒆𝒓",
        "     ➫ {} تومان".format(_poster_format_price(u_sell)),
        "     ✪ {} {}".format(_poster_progress_bar(u_percent), u_percent_text),
        "",
        "🔷 𝑩𝒖𝒚    𝑷𝒓𝒆𝒎𝒊𝒖𝒎",
        "     ➫ {} تومان".format(_poster_format_price(premium_buy)),
        "🔶 𝑺𝒆𝒍𝒍   𝑷𝒓𝒆𝒎𝒊𝒖𝒎",
        "     ➫ {} تومان".format(_poster_format_price(premium_sell)),
        "     ✪ {} {}".format(_poster_progress_bar(premium_percent), premium_percent_text),
        "",
        "━━━━━━━━━━━━━━━━━━",
        "        💎 𝒄𝒓𝒚𝒑𝒕𝒐 💎",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "💵 𝒕𝒆𝒕𝒉𝒆𝒓       ➫ {} تومان".format(_poster_format_price(usdt_irt)),
        "✨ 𝒖𝒕𝒐𝒑𝒊𝒂       ➫ {} تومان".format(_poster_format_price(utopia_toman)),
        "",
        "🪙 𝒕𝒓𝒐𝒏         ➫ {} تومان".format(_poster_format_price(swap_prices.get("TRX/IRT"))),
        "💎 𝒕𝒐𝒏          ➫ {} تومان".format(_poster_format_price(swap_prices.get("TON/IRT"))),
        "",
        "🟠 𝒃𝒊𝒕𝒄𝒐𝒊𝒏      ➫ {} تومان".format(_poster_format_price(swap_prices.get("BTC/IRT"))),
        "🔷 𝒆𝒕𝒉𝒆𝒓𝒆𝒖𝒎     ➫ {} تومان".format(_poster_format_price(swap_prices.get("ETH/IRT"))),
        "",
        "🤖 𝒂𝒊 (𝑭𝑬𝑻)      ➫ {} تومان".format(_poster_format_price(swap_prices.get("FET/IRT"))),
        "",
        "━━━━━━━━━━━━━━━━━━",
        "   ⏰ بروزرسانی: هر ۱ ساعت",
        "━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def _poster_send_message(message_text: str) -> bool:
    """Send through Bot API with a 10s timeout and one retry."""
    url = "https://api.telegram.org/bot{}/sendMessage".format(SENDER_BOT_TOKEN)
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✪ ورود", "url": MAIN_BOT_URL},
                {"text": "✆ پشتیبانی", "url": _poster_support_url()},
            ]]
        },
    }

    for attempt in range(POSTER_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=POSTER_HTTP_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("ok") is True:
                log("[POSTER] Sent OK")
                return True
            description = result.get("description", "Telegram API returned ok=false") if isinstance(result, dict) else "Invalid Telegram API response"
            raise ValueError(str(description)[:300])
        except Exception as exc:
            log(
                "[POSTER] Telegram send attempt {}/{} failed: {}".format(
                    attempt + 1, POSTER_RETRIES + 1, str(exc)[:400]
                )
            )
            if attempt < POSTER_RETRIES:
                time.sleep(1.0)
    return False


def poster_loop() -> None:
    """Low-frequency poster worker; independent of the pricing scheduler's async loop."""
    if not SENDER_BOT_TOKEN or not CHANNEL_ID:
        log("[POSTER] Disabled — SENDER_BOT_TOKEN or CHANNEL_ID is missing.")
        return

    log("[POSTER] Thread started; first send in 60 seconds.")
    if SHUTDOWN.wait(POSTER_INITIAL_DELAY_SECONDS):
        return

    while not SHUTDOWN.is_set():
        try:
            swap_prices = _poster_fetch_swap_prices()
            with state_lock:
                ali_prices = dict(STATE.get("ali1377", {}))
            message_text = _poster_build_message(ali_prices, swap_prices)
            _poster_send_message(message_text)
        except Exception as exc:
            # Poster errors must not kill this thread or the existing service loops.
            log("[POSTER] Unexpected loop error: {}".format(str(exc)[:500]))

        delay = POSTER_INTERVAL_SECONDS + random.uniform(
            -POSTER_JITTER_SECONDS, POSTER_JITTER_SECONDS
        )
        if SHUTDOWN.wait(delay):
            break


# ============================================================
# BACKGROUND THREAD STARTUP
# ============================================================
# IMPORTANT FOR GUNICORN:
# Do NOT start background threads at module import time. With Gunicorn, the
# master imports this module before forking the worker. A module-level startup
# would therefore start the scheduler in the master, while Flask serves from
# a separate worker process with a separate memory/STATE.
#
# Instead, start the background core lazily on the first request inside the
# worker process. The lock + flag make startup idempotent within that worker.
_startup_lock = threading.Lock()
_startup_done = False


def start_background_threads() -> None:
    global _startup_done

    with _startup_lock:
        if _startup_done:
            return
        _startup_done = True

    log("[BOOT] Background threads started (in worker).")
    threading.Thread(
        target=run_async_loops,
        daemon=True,
        name="async-core",
    ).start()
    threading.Thread(
        target=poster_loop,
        daemon=True,
        name="poster_loop",
    ).start()


@app.before_request
def _ensure_threads_started() -> None:
    start_background_threads()


# ============================================================
# FLASK ROUTES
# ============================================================
@app.after_request
def add_headers(response: Response) -> Response:
    # برای GitHub Pages / Frontend جدا از Render.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/")
def prices() -> Response:
    with state_lock:
        payload = json.dumps(
            STATE,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return Response(payload, status=200, content_type="application/json; charset=utf-8")


@app.route("/health")
def health() -> Response:
    now = time.time()
    with state_lock:
        scheduler_age = now - RUNTIME.get("scheduler_heartbeat", 0.0) if RUNTIME.get("scheduler_heartbeat") else None
        display_age = now - RUNTIME.get("display_heartbeat", 0.0) if RUNTIME.get("display_heartbeat") else None

    # Health فقط وضعیت Process را بررسی می‌کند؛ هیچ Request خارجی و هیچ Fetch انجام نمی‌دهد.
    # در شروع اولیه اجازه می‌دهیم سرویس چند ثانیه آماده شود.
    started_age = now - RUNTIME["started_at"]
    if started_age < 60:
        return Response("OK", status=200, content_type="text/plain; charset=utf-8")

    # اگر Loop اصلی واقعاً بیش از 10 دقیقه متوقف شده باشد، 503 می‌دهیم تا زیرساخت فرصت Recovery داشته باشد.
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
    return Response(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        status=200,
        content_type="application/json; charset=utf-8",
    )


# ============================================================
# GRACEFUL SHUTDOWN
# ============================================================
def _handle_signal(signum: int, _frame: Any) -> None:
    log("[SYSTEM] Shutdown signal received: {}".format(signum))
    SHUTDOWN.set()


# Only the standalone local runner installs these handlers.
# Under Gunicorn, Gunicorn itself owns process signals and worker lifecycle.
if __name__ == "__main__":
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except Exception:
        pass


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    # For direct local execution, start the same background core explicitly
    # before launching Flask. Under Gunicorn this block is not executed.
    start_background_threads()
    log("[BOOT] Starting local Flask server...")
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
