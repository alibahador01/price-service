import os, threading, json, re, requests, random, asyncio
from datetime import datetime, timezone
from flask import Flask
from telethon import TelegramClient

API_ID   = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION  = "session3"
BOT_VH   = "@VoucherHub_bot"
CH_LUI   = "@Luibitcom"
OUTPUT   = "Ali1377.01.05.json"

INTERVAL_FAST = 15 * 60
INTERVAL_SLOW = 40 * 60

app = Flask(__name__)

def fa_to_en(s):
    if not s: return None
    for ch in ["\u066c", ",", "\u060c", " ", "\u00a0", "\u200c"]:
        s = s.replace(ch, "")
    for p, e in zip("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "0123456789"):
        s = s.replace(p, e)
    try: return int(s)
    except: return None

def fluctuate(price):
    if price is None: return None
    first_part = (price // 1000) * 1000
    return first_part + random.randint(0, 999)

def get_crypto():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": "usd"},
headers={"User-Agent": "Mozilla/5.0"},
            timeout=10)
        d = r.json()
        return {
            "tether_usd": d.get("tether", {}).get("usd"),
            "utopia_usd": 0.999,
        }
    except: return {}

def load_old():
    try:
        with open(OUTPUT, "r", encoding="utf-8") as f:
            return json.load(f).get("ali1377", {})
    except: return {}

def save_json(ali):
    data = {"ali1377": ali, "last_updated": datetime.now(timezone.utc).isoformat()}
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("[OK] Saved")

def run_async_loops():
    asyncio.run(loops_main())

async def fetch_lui(client):
    try:
        ch = await client.get_entity(CH_LUI)
        msgs = await client.get_messages(ch, limit=5)
        for m in msgs:
            if not m.text or "PS Voucher" not in m.text: continue
            t = m.text
            buy_m  = re.search(r"PS\s*Voucher\s*Buy[:\s]*`?([\d,]+)", t)
            sell_m = re.search(r"PS\s*Voucher\s*Sell[:\s]*`?([\d,]+)", t)
            if buy_m and sell_m:
                return {"ps_buy": fa_to_en(buy_m.group(1)), "ps_sell": fa_to_en(sell_m.group(1))}
        return {}
    except: return {}

async def fetch_vh(client):
    try:
        bot = await client.get_entity(BOT_VH)
        await client.send_message(bot, "/prices")
        await asyncio.sleep(5)
        msgs = await client.get_messages(bot, limit=5)
        text = ""
        for m in msgs:
            if m.text and "\u0642\u06cc\u0645\u062a" in m.text:
                text = m.text
                break
        if not text: return {}
        def ext(pat):
            m = re.search(pat + r"[^:]*:\s*\*{0,2}([\d,\u060c\u06f0-\u06f9]+)", text)
            return fa_to_en(m.group(1)) if m else None
        return {
            "premium_buy":  ext(r"\u062e\u0631\u06cc\u062f \u067e\u0631\u06cc\u0645\u06cc\u0648\u0645"),
            "premium_sell": ext(r"\u0641\u0631\u0648\u0634 \u067e\u0631\u06cc\u0645\u06cc\u0648\u0645"),
            "u_buy":        ext(r"\u062e\u0631\u06cc\u062f \u06cc\u0648"),
            "u_sell":       ext(r"\u0641\u0631\u0648\u0634 \u06cc\u0648"),
        }
    except: return {}

async def fast_loop(client):
    while True:
        try:
            ps = await fetch_lui(client)
            crypto = get_crypto()
            ali = load_old()
            if ps.get("ps_buy"):
                ali["ps_buy_base"] = ps["ps_buy"]; ali["ps_buy"] = fluctuate(ps["ps_buy"])
            if ps.get("ps_sell"):
                ali["ps_sell_base"] = ps["ps_sell"]; ali["ps_sell"] = fluctuate(ps["ps_sell"])
            ali["tether_usd"] = crypto.get("tether_usd")
            ali["utopia_usd"] = crypto.get("utopia_usd")
            save_json(ali)
            print("[" + str(datetime.now()) + "] FAST done")
        except Exception as e:
            print("[!] FAST: " + str(e))
        await asyncio.sleep(INTERVAL_FAST)

async def slow_loop(client):
    await asyncio.sleep(10)
    while True:
        try:
            vh = await fetch_vh(client)
            ali = load_old()
            for k in ["premium_buy", "premium_sell", "u_buy", "u_sell"]:
                if vh.get(k):
                    ali[k + "_base"] = vh[k]; ali[k] = fluctuate(vh[k])
            save_json(ali)
            print("[" + str(datetime.now()) + "] SLOW done")
        except Exception as e:
            print("[!] SLOW: " + str(e))
        await asyncio.sleep(INTERVAL_SLOW)

async def loops_main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    await asyncio.gather(fast_loop(client), slow_loop(client))

@app.route("/")
@app.route("/health")
def health():
    try:
        with open(OUTPUT, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "application/json; charset=utf-8"}
    except:
        return '{"status":"starting"}', 200

if __name__ == "__main__":
    threading.Thread(target=run_async_loops, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
