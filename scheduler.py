"""
scheduler.py
تولید زمان‌بندی روزانه پیام‌های کانال (بخش ۲.۳ سند) — فقط منطق خالص
(بدون شبکه، بدون I/O). یک Scheduler مرکزی سبک این زمان‌بندی را می‌خواند
و در central_scheduler.py دیسپچ می‌کند؛ اینجا فقط «کِی» تعیین می‌شود.

مهم: این فایل به سیستم دریافت/ارسال قیمت ووچر-دلار-یوتوپیا کاری ندارد؛
فقط زمان ارسال پیام‌های کانال (که طبق تأیید کاربر قابل تغییر است) را
تولید می‌کند.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - پایتون خیلی قدیمی
    ZoneInfo = None  # type: ignore

DEFAULT_TIMEZONE = "Asia/Tehran"

# طبق بخش ۲.۳: تعداد دقیق ارسال هر نوع پیام در روز
JOB_COUNTS: Dict[str, int] = {
    "small_list": 4,
    "big_list": 2,
    "crypto_intel": 3,
    "market_pulse": 2,
    "whale_alert": 2,
    "arbitrage": 2,
}
GOODNIGHT_JOB = "goodnight"

# حداقل فاصله بین دو رخداد از همان نوع پیام (دقیقه)
MIN_SPACING_MINUTES: Dict[str, int] = {
    "small_list": 120,
    "big_list": 300,
    "crypto_intel": 300,
    "market_pulse": 300,
    "whale_alert": 300,
    "arbitrage": 300,
}
# حداقل فاصله بین هر دو پیام از نوع‌های متفاوت
GLOBAL_MIN_GAP_MINUTES = 30

# بازه فعال روزانه، هم‌راستا با ریتم فعلی کانال (۱۱:۰۰ تا ۰۲:۰۰ بامداد روز بعد)
ACTIVE_WINDOW_START_MIN = 11 * 60         # 11:00
ACTIVE_WINDOW_END_MIN = 26 * 60           # 02:00 (روز بعد) به‌صورت دقیقه از نیمه‌شب محلی


def _segment_pick(rng: random.Random, seg_start: float, seg_end: float) -> int:
    span = seg_end - seg_start
    margin = min(span * 0.15, 20.0)
    if span - 2 * margin <= 1:
        return round((seg_start + seg_end) / 2.0)
    return round(rng.uniform(seg_start + margin, seg_end - margin))


def _enforce_global_min_gap(
    entries: List[Tuple[str, int]], min_gap: int, window_end: int
) -> List[Tuple[str, int]]:
    """ورودی: [(job_type, minute), ...] مرتب‌نشده. بعد از مرتب‌سازی بر اساس
    زمان، اگر فاصله دو رخداد متوالی از نوع‌های متفاوت کمتر از min_gap بود،
    رخداد بعدی را جلو می‌کشد (best-effort؛ کاملاً بهینه نیست ولی قطعی و
    امن است)."""
    ordered = sorted(entries, key=lambda e: e[1])
    adjusted: List[Tuple[str, int]] = []
    last_time: Optional[int] = None
    for job, minute in ordered:
        t = minute
        if last_time is not None and (t - last_time) < min_gap:
            t = last_time + min_gap
        t = min(t, window_end + 15)  # هرگز بیش از حد از پنجره فعال فاصله نگیرد
        adjusted.append((job, t))
        last_time = t
    return adjusted


def generate_minute_offsets(local_date: date, seed_extra: str = "") -> Dict[str, List[int]]:
    """خروجی: نگاشت نوع پیام -> لیست «دقیقه از نیمه‌شب محلی» (می‌تواند از
    ۱۴۴۰ عبور کند یعنی بامداد روز بعد). deterministic بر اساس تاریخ، یعنی
    اگر همان روز دوباره تولید شود (مثلاً بعد از ری‌استارت Render)، همان
    زمان‌ها برمی‌گردد؛ ولی هر روز نسبت به روز قبل جابه‌جا می‌شود."""
    rng = random.Random("{}|{}".format(local_date.isoformat(), seed_extra))
    raw: Dict[str, List[int]] = {}
    for job, count in JOB_COUNTS.items():
        segment_len = (ACTIVE_WINDOW_END_MIN - ACTIVE_WINDOW_START_MIN) / count
        times = []
        for i in range(count):
            seg_start = ACTIVE_WINDOW_START_MIN + i * segment_len
            seg_end = seg_start + segment_len
            times.append(_segment_pick(rng, seg_start, seg_end))
        raw[job] = sorted(times)

    # اعمال حداقل فاصله سراسری بین انواع مختلف پیام (۳۰ دقیقه)
    flat: List[Tuple[str, int]] = []
    for job, times in raw.items():
        for t in times:
            flat.append((job, t))
    adjusted = _enforce_global_min_gap(flat, GLOBAL_MIN_GAP_MINUTES, ACTIVE_WINDOW_END_MIN)

    result: Dict[str, List[int]] = {job: [] for job in JOB_COUNTS}
    for job, t in adjusted:
        result[job].append(t)
    for job in result:
        result[job].sort()

    # شب‌بخیر: همیشه بعد از آخرین پیام دیگر روز، با یک مکث کوتاه تصادفی
    all_times = [t for times in result.values() for t in times]
    last_other = max(all_times) if all_times else ACTIVE_WINDOW_END_MIN
    result[GOODNIGHT_JOB] = [last_other + rng.randint(2, 4)]
    return result


def _minute_offset_to_datetime(local_date: date, minute_offset: int, tz: "ZoneInfo") -> datetime:
    base = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0, tzinfo=tz)
    return base + timedelta(minutes=minute_offset)


def build_schedule_state(
    local_date: date, tz_name: str = DEFAULT_TIMEZONE, seed_extra: str = ""
) -> Dict[str, Any]:
    """ساختار کامل State روز را می‌سازد (با پرچم sent=False برای هر رخداد)،
    آماده ذخیره با state_store.save_json."""
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo unavailable on this Python runtime")
    tz = ZoneInfo(tz_name)
    offsets = generate_minute_offsets(local_date, seed_extra=seed_extra)
    jobs: Dict[str, List[Dict[str, Any]]] = {}
    for job, minutes in offsets.items():
        jobs[job] = [
            {"time": _minute_offset_to_datetime(local_date, m, tz).isoformat(), "sent": False}
            for m in minutes
        ]
    return {"date": local_date.isoformat(), "timezone": tz_name, "jobs": jobs}


def get_or_create_today_schedule(
    stored_state: Optional[Dict[str, Any]],
    local_date: date,
    tz_name: str = DEFAULT_TIMEZONE,
    seed_extra: str = "",
) -> Dict[str, Any]:
    """اگر State ذخیره‌شده برای همین روز است، همان را برمی‌گرداند (پایداری
    بعد از ری‌استارت)؛ وگرنه زمان‌بندی تازه می‌سازد."""
    if stored_state and stored_state.get("date") == local_date.isoformat():
        return stored_state
    return build_schedule_state(local_date, tz_name=tz_name, seed_extra=seed_extra)


def due_jobs(schedule_state: Dict[str, Any], now: datetime) -> List[Tuple[str, int]]:
    """رخدادهایی که زمانشان رسیده و هنوز ارسال نشده‌اند را برمی‌گرداند،
    به‌صورت [(job_type, index), ...] به ترتیب زمان."""
    due: List[Tuple[str, int, datetime]] = []
    for job, occurrences in schedule_state.get("jobs", {}).items():
        for idx, occ in enumerate(occurrences):
            if occ.get("sent"):
                continue
            try:
                occ_time = datetime.fromisoformat(occ["time"])
            except (KeyError, ValueError, TypeError):
                continue
            if occ_time <= now:
                due.append((job, idx, occ_time))
    due.sort(key=lambda item: item[2])
    return [(job, idx) for job, idx, _ in due]


def mark_sent(schedule_state: Dict[str, Any], job: str, index: int) -> None:
    try:
        schedule_state["jobs"][job][index]["sent"] = True
    except (KeyError, IndexError, TypeError):
        pass
