"""
formatting.py
فرمت‌دهی مشترک اعداد برای پیام‌های جدید (کریپتو، نبض بازار، نهنگ، آربیتراژ).
هم‌راستا با سبک _poster_format_price موجود در render_app.py: ارقام لاتین،
جداکننده هزارگان، برچسب روشن برای داده ناموجود.
"""
from __future__ import annotations

from typing import Optional

PLACEHOLDER = "داده کافی نیست"


def format_price(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return PLACEHOLDER
    try:
        return "{:,.{}f}".format(float(value), decimals)
    except (TypeError, ValueError):
        return PLACEHOLDER


def format_pct(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return PLACEHOLDER
    try:
        sign = "+" if value > 0 else ""
        return "{}{:,.{}f}%".format(sign, float(value), decimals)
    except (TypeError, ValueError):
        return PLACEHOLDER


def format_number(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return PLACEHOLDER
    try:
        return "{:,.{}f}".format(float(value), decimals)
    except (TypeError, ValueError):
        return PLACEHOLDER


def format_usdt(value: Optional[float]) -> str:
    if value is None:
        return PLACEHOLDER
    try:
        return "${:,.0f}".format(float(value))
    except (TypeError, ValueError):
        return PLACEHOLDER


def format_toman(value: Optional[float]) -> str:
    if value is None:
        return PLACEHOLDER
    try:
        return "{:,.0f} تومان".format(float(value))
    except (TypeError, ValueError):
        return PLACEHOLDER


# همان جدول _PERSIAN_DIGITS موجود در render_app.py؛ عمداً مستقل تعریف شده
# تا این ماژول‌های جدید به render_app.py وابسته نباشند (بدون import چرخه‌ای).
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(value: object) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def format_persian_pct(value: Optional[float], decimals: int = 1) -> str:
    """فقط برای خط «رادار نهنگ» داخل تحلیل کریپتو؛ طبق نمونه عیناً بخش ۱۷
    (مثال: «۶۳.۳٪»)، بر خلاف بقیه درصدها که با رقم لاتین و % نمایش می‌شوند."""
    if value is None:
        return PLACEHOLDER
    try:
        return to_persian_digits("{:.{}f}".format(float(value), decimals)) + "٪"
    except (TypeError, ValueError):
        return PLACEHOLDER
