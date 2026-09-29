import os
import json
import re
import time
import random
import asyncio
import threading
from datetime import datetime, timezone

import requests
from flask import Flask
from telethon import TelegramClient

# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════
API_ID   = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION  = "session3"
BOT_VH   = "@VoucherHub_bot"
BOT_HOT  = "@hot_voucher_bot"
OUTPUT   = "Ali1377.01.05.json"

# بازه تصادفی هر دارایی (ثانیه)
ASSETS_CONFIG = {
    "ps":      {"min": 25 * 60, "max": 35 * 60},   # PS: 25-35 دقیقه
    "u_prem":  {"min": 30 * 60, "max": 50 * 60},   # U + Premium: 30-50 دقیقه
    "crypto":  {"min": 12 * 60, "max": 18 * 60},   # Tether + Utopia: 12-18 دقیقه
}

MIN_GAP = 15 * 60   # حداقل فاصله بین دو fetch تلگرام
MAX_SLEEP = 60      # حداکثر زمان بین هر چک scheduler

TETHER_FALLBACK = 1.0
UUSD_FALLBACK   = 0.999

TELEGRAM_ASSETS = ["ps", "u_prem"]
API_ASSETS      = ["crypto"]

# ═══════════════════════════════════════════════
#  APP + LOCKS
# ═══════════════════════════════════════════════
app = Flask(__name__)
file_lock = threading.Lock()
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print("[" + datetime.now().strftime("%H:%M:%S") + "] " + msg, flush=True)


# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════
def fa_to_en(s):
    if not s:
        return None
    for ch in ["\u066c", ",", "\u060c", " ", "\u00a0", "\u200c"]:
        s = s.replace(ch, "")
    for p, e in zip("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9",
                    "0123456789"):
        s = s.replace(p, e)
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def fluctuate(price):
    """۳ رقم اول ثابت از منبع، ۳ رقم آخر نوسان 0-999"""
    if price is None:
        return None
    first_part = (price // 1000) * 1000
    return first_part + random.randint(0, 999)


# ═══════════════════════════════════════════════
#  TETHER (3 fallback sources)
# ═══════════════════════════════════════════════
def _tether_coingecko():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": "usd"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        if r.status_code == 200:
            v = r.json().get("tether", {}).get("usd")
            if v is not None:
                return round(float(v), 4)
    except Exception as e:
        log("[!] CoinGecko tether: " + str(e))
    return None


def _tether_binance():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "USDCUSDT"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        if r.status_code == 200:
            v = r.json().get("price")
            if v is not None:
                val = 1.0 / float(v)
                if 0.9 < val < 1.1:
                    return round(val, 4)
    except Exception as e:
        log("[!] Binance tether: " + str(e))
    return None


def _tether_kraken():
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "USDTZUSD"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json().get("result", {})
            for key in data:
                last = data[key].get("c", [None])[0]
                if last is not None:
                    val = float(last)
                    if 0.9 < val < 1.1:
                        return round(val, 4)
    except Exception as e:
        log("[!] Kraken tether: " + str(e))
    return None


def fetch_tether():
    for fn in (_tether_coingecko, _tether_binance, _tether_kraken):
        v = fn()
        if v is not None:
            log("[OK] tether = " + str(v))
            return v
    log("[!] tether fallback")
    return TETHER_FALLBACK


# ═══════════════════════════════════════════════
#  UTOPIA (UUSD)
# ═══════════════════════════════════════════════
def _utopia_coinpaprika():
    try:
        r = requests.get(
            "https://api.coinpaprika.com/v1/tickers/uusd-utopia-usd",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        if r.status_code == 200:
            v = (r.json()
                    .get("quotes", {})
                    .get("USD", {})
                    .get("price"))
            if v is not None:
                return round(float(v), 4)
    except Exception as e:
        log("[!] CoinPaprika utopia: " + str(e))
    return None


def fetch_utopia():
    v = _utopia_coinpaprika()
    if v is not None:
        log("[OK] utopia = " + str(v))
        return v
    log("[!] utopia fallback")
    return UUSD_FALLBACK


def fetch_crypto():
    return {
        "tether_usd": fetch_tether(),
        "utopia_usd": fetch_utopia(),
    }


# ═══════════════════════════════════════════════
#  JSON R/W (atomic)
# ═══════════════════════════════════════════════
def update_json(updater_fn):
    with file_lock:
        try:
            with open(OUTPUT, "r", encoding="utf-8") as f:
                old = json.load(f).get("ali1377", {})
        except (FileNotFoundError, json.JSONDecodeError):
            old = {}

        try:
            updater_fn(old)
        except Exception as e:
            log("[!] updater_fn: " + str(e))
            return

        data = {
            "ali1377": old,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
        try:
            with open(OUTPUT, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log("[OK] Saved")
        except Exception as e:
            log("[!] Save: " + str(e))


# ═══════════════════════════════════════════════
#  TELEGRAM HELPERS
# ═══════════════════════════════════════════════
async def ensure_connected(client):
    if client.is_connected():
        return True
    log("[*] Reconnecting Telegram...")
    try:
        await client.connect()
        return True
    except Exception as e:
        log("[!] Reconnect: " + str(e))
        return False


async def _click_first_match(client, bot, patterns):
    """روی اولین دکمه‌ای که با یکی از pattern ها مچ شد، کلیک کن"""
    msgs = await client.get_messages(bot, limit=1)
    if not msgs or not msgs[0].buttons:
        return False
    for pattern in patterns:
        for row in msgs[0].buttons:
            for b in row:
                if pattern in b.text:
                    log("[*] Click: " + b.text)
                    await b.click()
                    await asyncio.sleep(random.uniform(3, 5))
                    return True
    return False


async def fetch_ps(client):
    """PS Voucher خرید و فروش از @hot_voucher_bot"""
    try:
        if not await ensure_connected(client):
            return {}
        bot = await client.get_entity(BOT_HOT)
        result = {}

        # ═══ خرید PS (تبدیل یووچر به پی اس) ═══
        await client.send_message(bot, "/start")
        await asyncio.sleep(random.uniform(3, 5))

        buy_paths = [
            ["تبدیل ووچر ها", "یووچر به پی اس ووچر"],
            ["خرید از موجودی", "پی اس ووچر"],
        ]
        path = random.choice(buy_paths)

        for step in path:
            ok = await _click_first_match(client, bot, [step])
            if not ok:
                break

        msgs = await client.get_messages(bot, limit=1)
        text = (msgs[0].text if msgs else "") or ""
        log("[PS BUY] " + text[:150])

        m = re.search(
            r"(?:نرخ\s*خرید\s*دلار\s*پی\s*اس\s*ووچر|قیمت\s*واحد)[:\s]*\*{0,2}([\d,\u060c\u06f0-\u06f9]+)",
            text
        )
        if m:
            result["ps_buy"] = fa_to_en(m.group(1))

        # ═══ فروش PS (افزایش موجودی → پی اس) ═══
        await asyncio.sleep(random.uniform(2, 4))
        await client.send_message(bot, "/start")
        await asyncio.sleep(random.uniform(3, 5))

        sell_paths = [
            ["افزایش موجودی", "پی اس ووچر"],
            ["تبدیل ووچر ها", "پی اس ووچر به یو"],
        ]
        path = random.choice(sell_paths)

        for step in path:
            ok = await _click_first_match(client, bot, [step])
            if not ok:
                break

        msgs = await client.get_messages(bot, limit=1)
        text = (msgs[0].text if msgs else "") or ""
        log("[PS SELL] " + text[:150])

        m = re.search(
            r"نرخ\s*دلار\s*پی\s*اس\s*ووچر[:\s]*\*{0,2}([\d,\u060c\u06f0-\u06f9]+)",
            text
        )
        if m:
            result["ps_sell"] = fa_to_en(m.group(1))

        return result
    except Exception as e:
        log("[!] PS: " + str(e))
        return {}


async def fetch_vh(client):
    """U Voucher + Premium از @VoucherHub_bot"""
    try:
        if not await ensure_connected(client):
            return {}
        bot = await client.get_entity(BOT_VH)
        await client.send_message(bot, "/prices")
        await asyncio.sleep(random.uniform(4, 6))
        msgs = await client.get_messages(bot, limit=5)
        text = ""
        for m in msgs:
            if m.text and "\u0642\u06cc\u0645\u062a" in m.text:
                text = m.text
                break
        if not text:
            log("[!] VH: no price message")
            return {}

        def ext(pat):
            mm = re.search(
                pat + r"[^:]*:\s*\*{0,2}([\d,\u060c\u06f0-\u06f9]+)",
                text
            )
            return fa_to_en(mm.group(1)) if mm else None

        return {
            "premium_buy":  ext(r"\u062e\u0631\u06cc\u062f \u067e\u0631\u06cc\u0645\u06cc\u0648\u0645"),
            "premium_sell": ext(r"\u0641\u0631\u0648\u0634 \u067e\u0631\u06cc\u0645\u06cc\u0648\u0645"),
            "u_buy":        ext(r"\u062e\u0631\u06cc\u062f \u06cc\u0648"),
            "u_sell":       ext(r"\u0641\u0631\u0648\u0634 \u06cc\u0648")
        }
    except Exception as e:
        log("[!] VH: " + str(e))
        return {}


# ═══════════════════════════════════════════════
#  APPLIERS
# ═══════════════════════════════════════════════
def apply_ps(ali, ps):
    if ps.get("ps_buy"):
        ali["ps_buy_base"] = ps["ps_buy"]
        ali["ps_buy"]      = fluctuate(ps["ps_buy"])
    if ps.get("ps_sell"):
        ali["ps_sell_base"] = ps["ps_sell"]
        ali["ps_sell"]      = fluctuate(ps["ps_sell"])


def apply_crypto(ali, crypto):
    if crypto.get("tether_usd") is not None:
        ali["tether_usd"] = crypto["tether_usd"]
    if crypto.get("utopia_usd") is not None:
        ali["utopia_usd"] = crypto["utopia_usd"]


def apply_vh(ali, vh):
    for k in ["premium_buy", "premium_sell", "u_buy", "u_sell"]:
        if vh.get(k):
            ali[k + "_base"] = vh[k]
            ali[k]           = fluctuate(vh[k])


# ═══════════════════════════════════════════════
#  SCHEDULER (مرکزی، کم‌مصرف)
# ═══════════════════════════════════════════════
async def scheduler_loop(client):
    log("[SCHED] Starting scheduler...")

    now = time.time()
    next_fetch = {k: now + random.uniform(10, 30) for k in ASSETS_CONFIG}
    last_tg = 0

    while True:
        now = time.time()
        due = [k for k in ASSETS_CONFIG if now >= next_fetch[k]]

        if due:
            api_due = sorted([k for k in due if k in API_ASSETS],
                             key=lambda k: next_fetch[k])
            tg_due = sorted([k for k in due if k in TELEGRAM_ASSETS],
                            key=lambda k: next_fetch[k])

            # ─── اول API (بدون محدودیت تلگرام) ───
            if api_due:
                asset = api_due[0]
                log("[SCHED] " + asset)
                try:
                    crypto = await asyncio.to_thread(fetch_crypto)
                    update_json(lambda a: apply_crypto(a, crypto))
                except Exception as e:
                    log("[!] crypto: " + str(e))
                cfg = ASSETS_CONFIG[asset]
                next_fetch[asset] = time.time() + random.uniform(
                    cfg["min"], cfg["max"])
                continue

            # ─── بعد تلگرام (با MIN_GAP) ───
            if tg_due:
                asset = tg_due[0]
                gap = now - last_tg
                if gap < MIN_GAP:
                    wait = MIN_GAP - gap
                    log("[SCHED] min gap wait " + str(int(wait)) + "s")
                    await asyncio.sleep(wait)
                    continue

                log("[SCHED] " + asset)
                try:
                    if asset == "ps":
                        ps = await fetch_ps(client)
                        update_json(lambda a: apply_ps(a, ps))
                    elif asset == "u_prem":
                        vh = await fetch_vh(client)
                        update_json(lambda a: apply_vh(a, vh))
                except Exception as e:
                    log("[!] " + asset + ": " + str(e))
                last_tg = time.time()
                cfg = ASSETS_CONFIG[asset]
                next_fetch[asset] = time.time() + random.uniform(
                    cfg["min"], cfg["max"])
                continue

        # ─── Sleep هوشمند ───
        next_time = min(next_fetch.values())
        wait = max(5, next_time - time.time())
        wait = min(wait, MAX_SLEEP)
        await asyncio.sleep(wait)


async def loops_main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log("[!] Session not authorized!")
        return
    log("[OK] Telegram connected.")
    try:
        await scheduler_loop(client)
    except Exception as e:
        log("[!] loops_main: " + str(e))


def run_async_loops():
    try:
        asyncio.run(loops_main())
    except Exception as e:
        log("[!] run_async_loops: " + str(e))


# ═══════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════
@app.route("/")
def prices():
    try:
        with file_lock:
            with open(OUTPUT, "r", encoding="utf-8") as f:
                content = f.read()
        return content, 200, {"Content-Type": "application/json; charset=utf-8"}
    except Exception:
        return ('{"status":"starting"}', 200,
                {"Content-Type": "application/json; charset=utf-8"})


@app.route("/health")
def health():
    return "OK", 200, {"Content-Type": "text/plain"}


# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    t = threading.Thread(target=run_async_loops, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
