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

# فقط دو فهرست قیمت اجازه انتشار دارند؛ تمام jobهای دیگر در کدهای
# dispatch باقی می‌مانند اما در برنامه روزانه ثبت نمی‌شوند.
SCHEDULE_VERSION = 2
JOB_COUNTS: Dict[str, int] = {
    "big_list": 1,    # فهرست کامل ارزها و دارایی‌ها: روزی یک بار
    "small_list": 1,  # فهرست ووچرها + تتر + یوتوپیا: روزی یک بار
}
JOB_WINDOWS_MINUTES: Dict[str, Tuple[int, int]] = {
    "big_list": (11 * 60 + 30, 14 * 60),  # 11:30 تا 14:00 تهران
    "small_list": (17 * 60, 20 * 60),     # 17:00 تا 20:00 تهران
}
GOODNIGHT_JOB = "goodnight"  # شناسه قدیمی؛ عمداً دیگر زمان‌بندی نمی‌شود

# تنظیمات قدیمی زمان‌بندی در این فایل نگه داشته شده‌اند تا سابقه/سازگاری
# کد از بین نرود؛ توابع تولید زمان جدید از آن‌ها استفاده نمی‌کنند.
# حداقل فاصله‌های قدیمی بین رخدادها (غیرفعال)
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
    """برای هر یک از دو فهرست مجاز، دقیقاً یک زمان تصادفی در بازه تهران
    تولید می‌کند. seed روزانه باعث می‌شود بعد از restart همان روز تغییر نکند."""
    rng = random.Random("{}|{}|schedule-v{}".format(
        local_date.isoformat(), seed_extra, SCHEDULE_VERSION
    ))
    return {
        job: [rng.randint(start_minute, end_minute)]
        for job, (start_minute, end_minute) in JOB_WINDOWS_MINUTES.items()
    }


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
    return {
        "version": SCHEDULE_VERSION,
        "date": local_date.isoformat(),
        "timezone": tz_name,
        "jobs": jobs,
    }


def get_or_create_today_schedule(
    stored_state: Optional[Dict[str, Any]],
    local_date: date,
    tz_name: str = DEFAULT_TIMEZONE,
    seed_extra: str = "",
) -> Dict[str, Any]:
    """State معتبر همان روز را حفظ می‌کند؛ Stateهای قدیمی را به سیاست
    دوپستی جدید مهاجرت می‌دهد. اگر یکی از فهرست‌ها امروز قبلاً ارسال شده
    باشد، برای جلوگیری از ارسال تکراری، همان فهرست در برنامه جدید sent می‌ماند."""
    same_day = (
        isinstance(stored_state, dict)
        and stored_state.get("date") == local_date.isoformat()
    )
    if same_day:
        old_jobs = stored_state.get("jobs") if isinstance(stored_state, dict) else None
        if (
            stored_state.get("version") == SCHEDULE_VERSION
            and isinstance(old_jobs, dict)
            and set(old_jobs) == set(JOB_COUNTS)
            and all(isinstance(old_jobs.get(job), list) and len(old_jobs[job]) == 1 for job in JOB_COUNTS)
        ):
            return stored_state

    fresh = build_schedule_state(local_date, tz_name=tz_name, seed_extra=seed_extra)
    if same_day:
        old_jobs = stored_state.get("jobs", {}) if isinstance(stored_state, dict) else {}
        for job in JOB_COUNTS:
            previous_occurrences = old_jobs.get(job, []) if isinstance(old_jobs, dict) else []
            already_sent = any(
                isinstance(item, dict) and item.get("sent") is True
                for item in previous_occurrences
            ) if isinstance(previous_occurrences, list) else False
            if already_sent:
                fresh["jobs"][job][0]["sent"] = True
    return fresh


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
