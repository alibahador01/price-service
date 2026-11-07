# Vochino-Hub⁰¹ — Price Service + Channel Intelligence

این سرویس دو بخش کاملاً مجزا دارد:

1. **سیستم دریافت و ارائه قیمت ووچر/دلار/یوتوپیا** — دست‌نخورده، طبق تأیید صریح کاربر. ربات مستقل خریدوفروش از همین بخش و همین Endpoint (`/`) استفاده می‌کند.
2. **قابلیت‌های جدید کانال** — تحلیل عمیق کریپتو، نبض بازار، هشدار نهنگ، رادار اختلاف قیمت، و فوتبال. اینها فقط *می‌خوانند* یا در کنار بخش ۱ کار می‌کنند؛ هیچ‌کدام فرمول/زمان‌بندی/Endpoint بخش ۱ را تغییر نداده‌اند.

---

## بخش ۱ — سیستم قیمت (دست‌نخورده)

- قیمت U-Voucher، Premium Voucher و Hot Voucher از ربات تلگرامی `@utopiaex_bot` دریافت می‌شود.
- درخواست چرخه‌ای ووچر و نرخ‌های دلاری با جیتر مثبت بین ۱۲۰ تا ۱۳۰ دقیقه انجام می‌شود؛ فاصلهٔ دو چرخه هیچ‌وقت کمتر از دو ساعت نیست.
- قیمت USDT/USD و Utopia/USD از CoinPaprika + fallback اختیاری CoinGecko.
- Endpoint `/` همان JSON کامل State قبلی را برمی‌گرداند؛ هیچ کلید حذف یا تغییر معنا نداده.
- فرمول Buy/Sell، QUOTE_SPREAD، نوار درصد تغییر و منطق «مبنای اولیه» دقیقاً همان کد قبلی است.

**تنها تغییر مجاز و اعمال‌شده در این بخش:** زمان‌بندی *ارسال* فهرست قیمت به کانال (نه دریافت/محاسبه) — طبق تأیید کاربر برای تطبیق با سند نسخه ۳:

| قبل | بعد |
|---|---|
| یک پیام ترکیبی (ووچر+ارز+طلا)، ۹ نوبت ثابت در روز | دو پیام جدا: **فهرست کوچک** (ووچرها + USDT + یوتوپیا) ۴ نوبت، **فهرست بزرگ** (TRX/TON/BTC/ETH/FET/PAXG/XAUT + طلا + سکه + یورو) ۲ نوبت |
| ساعات ثابت هرروز یکسان | زمان‌بندی روزانه با جیتر کنترل‌شده (`scheduler.py`)، پایدار در هر روز (ذخیره در State) |

فرمول/محاسبه هیچ دارایی تغییر نکرده — فقط کدام دارایی‌ها در کدام پیام نمایش داده می‌شوند، تغییر کرده.

---

## بخش ۲ — قابلیت‌های جدید کانال

### معماری منابع (طبق درخواست صریح کاربر برای کمترین مصرف Render)

**یک Thread واحد جدید** (`central_scheduler_loop`، جایگزین `poster_loop` قدیمی) هر هفت نوع پیام روزانه زیر + دیسپچ فوتبال را مدیریت می‌کند — نه هفت Thread جدا:

| نوع پیام | تعداد در روز | ماژول اصلی |
|---|---|---|
| فهرست کوچک | ۴ | `render_app.py` (دست‌نخورده) |
| فهرست بزرگ | ۲ | `render_app.py` (دست‌نخورده) |
| تحلیل عمیق کریپتو (BTC/ETH/TRX/TON) | ۳ | `crypto_intelligence.py` |
| نبض بازار | ۲ | `market_pulse.py` |
| هشدار معامله بزرگ | ۲ | `whale_alerts.py` |
| رادار اختلاف قیمت | ۲ | `arbitrage_radar.py` |
| شب‌بخیر | ۱ | — |
| فوتبال (حداکثر ۱۰ مسابقه/روز، هرکدام ۹۰ دقیقه قبل از شروع) | متغیر | `football_*.py` |

Tick هر ۴۵ ثانیه (`CENTRAL_SCHEDULER_TICK_SECONDS`، قابل تنظیم ۳۰-۶۰). داده‌های مشترک (قیمت Swap، طلا، سکه، یورو) با TTL کوتاه (۹۰ ثانیه، `shared_cache.py`) کش می‌شوند تا دو پیام نزدیک به هم دوباره همان API را صدا نزنند.

### تحلیل عمیق کریپتو (`crypto_intelligence.py`, `binance_client.py`, `quant.py`, `fact_block.py`, `ai_guards.py`)

- داده خام: Binance Spot (کندل یک‌ساعته + aggTrades)، بدون کلید API.
- اندیکاتورها (RSI-14 به روش Wilder، EMA20/50، MACD، حجم نسبی) با Python خالص محاسبه می‌شوند — بدون pandas/numpy، بدون دخالت مدل زبانی در عدد.
- Fact Block ساختاریافته تنها منبع عددی است که به Gemini/Groq داده می‌شود.
- زنجیره Guard (JSON Schema، Evidence، Numeric) هر خروجی مدل را قبل از انتشار بررسی می‌کند؛ هر عدد آزاد در متن تحلیل رد می‌شود.
- Groq به‌عنوان بازبین مستقل عمل می‌کند؛ در اختلاف، نتیجه به `neutral` تنزل می‌یابد نه حذف می‌شود.
- اگر هیچ Provider خروجی معتبر ندهد، آن نوبت **skip** می‌شود؛ تحلیل قدیمی دوباره منتشر نمی‌شود.

### نبض بازار (`market_pulse.py`)

شاخص ترس و طمع (Alternative.me) + دامیننس بیت‌کوین و میانگین تغییر ۲۴ساعته چهار دارایی (CoinGecko). هر منبع مستقل fail می‌شود؛ خرابی یکی بقیه پیام را خراب نمی‌کند.

### هشدار معامله بزرگ (`whale_alerts.py`)

هر دو نوبت، بزرگ‌ترین معامله بالای آستانه (پیش‌فرض ۱۰,۰۰۰ دلار، `LARGE_TRADE_THRESHOLD_USDT`) بین چهار نماد را گزارش می‌کند؛ با dedupe بر اساس `agg_trade_id` تا تکراری گزارش نشود.

### رادار اختلاف قیمت (`arbitrage_radar.py`)

نرخ Sell خود Vochino-Hub⁰¹ (از همان فرمول/QUOTE_SPREAD موجود) در برابر benchmark جهانی = `USDT/USD` (از cache موجود پروژه) × `USD/IRT آزاد`. **کد قبلی منبع تومانی دلار نداشت**؛ طبق مجوز صریح سند، یک منبع مستقل تازه اضافه شد: صفحه عمومی `tgju.org/profile/price_dollar_rl` با همان تابع عمومی TGJU موجود (بدون بازنویسی). اگر هر نرخ stale/گم باشد یا دو نرخ بیش از ۳ ساعت از هم فاصله زمانی داشته باشند، پیام **skip** می‌شود؛ هرگز مقایسه حلقوی یا عدد ساختگی ساخته نمی‌شود.

### فوتبال (`football_client.py`, `football_fixtures.py`, `football_images.py`, `football_dispatch.py`, `football_config.py`)

- منبع اصلی: Bzzoiro Sports Data API v2. Fallback: Football-Data.org v4.
- Circuit breaker: ۲ شکست متوالی → سوییچ به fallback؛ ۲ موفقیت متوالی probe → بازگشت به منبع اصلی. دلیل/زمان هر failover در State ثبت می‌شود.
- حداکثر ۱۰ مسابقه در روز محلی (`Asia/Tehran` پیش‌فرض)، فقط از allowlist لیگ‌ها/تیم‌های `football_config.py`.
- هر مسابقه دقیقاً ۹۰ دقیقه قبل از kickoff ارسال می‌شود؛ کشف دیرهنگام → `missed_window` (نه ارسال دیرهنگام نادرست).
- تغییر kickoff بعد از ارسال → ویرایش کپشن پیام موجود؛ لغو/ناپدیدشدن مسابقه بعد از ارسال → حذف پیام.
- **عکس مسابقه fail-closed است**: فقط وقتی `FOOTBALL_IMAGE_LICENSE_CONFIRMED=true` (دقیقاً این رشته) باشد عکس ارسال می‌شود؛ در غیر این صورت فقط کپشن متنی می‌رود. هیچ placeholder یا عکس اشتباه جایگزین نمی‌شود.

### بانک تبلیغاتی (`promo_messages.py`)

۳۰ متن ثابت (عیناً از سند استخراج‌شده با اسکریپت، به‌جز به‌روزرسانی نام برند طبق درخواست کاربر) با چرخش گردشی بدون تکرار. `index` فقط بعد از تأیید ارسال موفق پیشرفت می‌کند.

### کیبورد کانال (`keyboard.py`)

دو دکمه در یک ردیف، هر دو `style: danger`:

| دکمه | لینک |
|---|---|
| `Login App ➫` | `MAIN_BOT_URL + "/app"` |
| `Enter Bot ➬` | `MAIN_BOT_URL` |

پیام شب‌بخیر طبق قانون قبلی بدون این کیبورد می‌ماند.

---

## متغیرهای محیطی

### بخش ۱ (موجود، دست‌نخورده)

`TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`/`TG_SESSION`, `BOT_VH`, `SENDER_BOT_TOKEN`, `CHANNEL_ID`, `COINGECKO_API_KEY`, `OUTPUT_FILE`, `SUPPORT_USERNAME`, `MAIN_BOT_URL`, `PORT`, `PUBLIC_BASE_URL`/`RENDER_EXTERNAL_URL`.

### بخش ۲ (جدید)

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `GEMINI_API_KEY` | (خالی) | بدون آن، تحلیل کریپتو فقط از Groq (در صورت وجود کلید) یا اصلاً تلاش نمی‌کند |
| `GEMINI_MODEL` | `gemini-3.8-flash` | |
| `GEMINI_THINKING_LEVEL` | `high` | طبق سند، `minimal` ممنوع است |
| `GROQ_API_KEY` | (خالی) | برای بازبینی + fallback |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | |
| `LARGE_TRADE_THRESHOLD_USDT` | `10000` | آستانه مشترک Large Trade Radar و Whale Alert |
| `BZZOIRO_API_KEY` | (خالی) | بدون آن، فوتبال مستقیم به fallback می‌رود |
| `FOOTBALL_DATA_API_KEY` | (خالی) | |
| `FOOTBALL_TIMEZONE` | `Asia/Tehran` | |
| `FOOTBALL_LOOKAHEAD_DAYS` | `7` | |
| `FOOTBALL_POLL_INTERVAL_MINUTES` | `15` | |
| `FOOTBALL_IMAGE_LICENSE_CONFIRMED` | (خالی = غیرفعال) | فقط رشته دقیق `true` عکس را فعال می‌کند |
| `CENTRAL_SCHEDULER_TICK_SECONDS` | `45` | |
| `CENTRAL_SCHEDULER_STATE_PATH` | `central_scheduler_state.json` | |
| `WHALE_STATE_PATH` | `whale_state.json` | |

بدون `SENDER_BOT_TOKEN`/`CHANNEL_ID`، کل `central_scheduler` مثل `poster_loop` قدیمی غیرفعال می‌ماند — هیچ کرش رخ نمی‌دهد.

---

## ⚠️ محدودیت صادقانه تست (مهم، لطفاً قبل از Deploy بخوانید)

این پیاده‌سازی در محیطی **بدون دسترسی اینترنت** ساخته شد. یعنی:

**چه چیزی واقعاً تست شد (۲۳۸ تست واحد، آفلاین، قابل اجرا با `python3 -m unittest discover -s tests`):**
- تمام محاسبات قطعی: RSI/EMA/MACD/حجم نسبی، Fact Block، همه Guardها، فرمول Arbitrage، میانگین‌گیری Market Pulse، انتخاب/dedupe فوتبال، ریاضی ۹۰دقیقه‌ای، circuit breaker، چرخش تبلیغات، تطبیق قالب‌های پیام (بخش ۱۷ سند) **بایت‌به‌بایت**.
- منطق HTTP (صفحه‌بندی، تشخیص truncation، retry/backoff) با پاسخ‌های HTTP **شبیه‌سازی‌شده** (mock)، نه واقعی.
- دیسپچ central_scheduler با context کاملاً جعلی (fake).

**چه چیزی تست *نشد* (و نمی‌شد، بدون اینترنت):**
- فراخوانی واقعی Binance / Bzzoiro / Football-Data.org / Alternative.me / CoinGecko / TGJU `price_dollar_rl` / Gemini / Groq.
- امضای دقیق SDK `google-genai` و `groq` (نام پارامتر Thinking Level، ساختار پاسخ) — کدهای `gemini_client.py`/`groq_client.py` طبق بهترین دانش موجود نوشته شده‌اند ولی **باید قبل از Deploy با مستندات فعلی تطبیق داده شوند**.
- نسخه‌های پین‌شده `google-genai`/`groq` در `requirements.txt`.
- مجوز واقعی تصویر Bzzoiro (پرچم `FOOTBALL_IMAGE_LICENSE_CONFIRMED` را خودتان، نه من، باید تأیید کنید).
- پوشش واقعی لیگ‌ها/تیم‌ها در Bzzoiro (آیا همه آنچه در `football_config.py` است واقعاً پوشش داده می‌شود).

هیچ مرحله‌ای به‌عنوان «تست‌شده و موفق» ادعا نشده مگر واقعاً با `unittest` اجرا و PASS شده باشد.

---

## اجرای Render

فرمان پیشنهادی بدون تغییر: `gunicorn render_app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`

یک Gunicorn worker الزامی است (همان دلیل قبلی: Threadهای پس‌زمینه در چند worker باعث ارسال تکراری می‌شوند).

## امنیت Session تلگرام

بدون تغییر — فایل‌های `*.session` در این خروجی نیستند؛ از `TG_SESSION_STRING` به‌عنوان Secret در Render استفاده کنید.
