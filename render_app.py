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
CH_LUI   = "@Luibitcom"
OUTPUT   = "Ali1377.01.05.json"

INTERVAL_FAST = 15 * 60
INTERVAL_SLOW = 40 * 60

TETHER_FALLBACK = 1.0
UUSD_FALLBACK   = 0.999

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
            log("[OK] tether = " + str(v) + " (" + fn.__name__ + ")")
            return v
    log("[!] tether fallback → " + str(TETHER_FALLBACK))
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
    log("[!] utopia fallback → " + str(UUSD_FALLBACK))
    return UUSD_FALLBACK


# ═══════════════════════════════════════════════
#  COMBINED CRYPTO
# ═══════════════════════════════════════════════
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


async def fetch_lui(client):
    try:
        if not await ensure_connected(client):
            return {}
        ch = await client.get_entity(CH_LUI)
        msgs = await client.get_messages(ch, limit=5)
        for m in msgs:
            if not m.text or "PS Voucher" not in m.text:
                continue
            t = m.text
            buy_m  = re.search(r"PS\s*Voucher\s*Buy[:\s]*`?([\d,]+)", t)
            sell_m = re.search(r"PS\s*Voucher\s*Sell[:\s]*`?([\d,]+)", t)
            if buy_m and sell_m:
                return {
                    "ps_buy":  fa_to_en(buy_m.group(1)),
                    "ps_sell": fa_to_en(sell_m.group(1))
                }
        log("[!] No PS Voucher in Luibit")
        return {}
    except Exception as e:
        log("[!] Lui: " + str(e))
        return {}


async def fetch_vh(client):
    try:
        if not await ensure_connected(client):
            return {}
        bot = await client.get_entity(BOT_VH)
        await client.send_message(bot, "/prices")
        await asyncio.sleep(5)
        msgs = await client.get_messages(bot, limit=5)
        text = ""
        for m in msgs:
            if m.text and "\u0642\u06cc\u0645\u062a" in m.text:
                text = m.text
                break
        if not text:
            log("[!] VoucherHub: no price message")
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
#  LOOPS
# ═══════════════════════════════════════════════
async def fast_loop(client):
    while True:
        start = time.monotonic()
        log("FAST start")
        try:
            ps     = await fetch_lui(client)
            crypto = await asyncio.to_thread(fetch_crypto)

            def updater(ali):
                apply_ps(ali, ps)
                apply_crypto(ali, crypto)

            update_json(updater)
        except Exception as e:
            log("[!] FAST loop: " + str(e))

        elapsed = time.monotonic() - start
        wait = max(5, INTERVAL_FAST - elapsed)
        log("FAST next in " + str(int(wait)) + "s")
        await asyncio.sleep(wait)


async def slow_loop(client):
    await asyncio.sleep(20)
    while True:
        start = time.monotonic()
        log("SLOW start")
        try:
            vh = await fetch_vh(client)

            def updater(ali):
                apply_vh(ali, vh)

            update_json(updater)
        except Exception as e:
            log("[!] SLOW loop: " + str(e))

        elapsed = time.monotonic() - start
        wait = max(5, INTERVAL_SLOW - elapsed)
        log("SLOW next in " + str(int(wait)) + "s")
        await asyncio.sleep(wait)


async def loops_main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log("[!] Session not authorized!")
        return
    log("[OK] Telegram connected. Starting loops.")
    try:
        await asyncio.gather(fast_loop(client), slow_loop(client))
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
