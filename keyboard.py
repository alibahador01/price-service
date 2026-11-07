"""
keyboard.py
کیبورد شیشه‌ای زیر پست‌های کانال. طبق بازنگری صریح کاربر (جایگزین نسخه
تک‌دکمه‌ای قبلی که از بخش ۸ سند اولیه گرفته شده بود)، اکنون دو دکمه دارد:
  ۱) «Login App»  -> لینک Mini App ربات (MAIN_BOT_URL + "/app")
  ۲) «Enter Bot»  -> لینک مستقیم خود ربات (MAIN_BOT_URL)
هر دو با style="danger" (رنگ قرمز، در صورت پشتیبانی توسط کلاینت ارسال).
پیام شب‌بخیر طبق قانون قبلی همچنان بدون این کیبورد می‌ماند.
"""
from __future__ import annotations

from typing import Any, Dict

LOGIN_APP_BUTTON_TEXT = '𝑳𝒐𝒈𝒊𝒏 𝑨𝒑𝒑 ➫'
ENTER_BOT_BUTTON_TEXT = '𝑬𝒏𝒕𝒆𝒓 𝑩𝒐𝒕 ➬'


def _normalize_bot_base_url(main_bot_url: str) -> str:
    url = (main_bot_url or "").strip()
    return url.rstrip("/")


def build_channel_keyboard(main_bot_url: str) -> Dict[str, Any]:
    """دو دکمه در یک ردیف؛ هر دو از همان MAIN_BOT_URL موجود مشتق می‌شوند،
    هیچ لینک یا username جدیدی حدس زده نمی‌شود."""
    base = _normalize_bot_base_url(main_bot_url)
    login_app_url = base + "/app" if base else ""
    enter_bot_url = base
    return {
        "inline_keyboard": [[
            {"text": LOGIN_APP_BUTTON_TEXT, "url": login_app_url, "style": "danger"},
            {"text": ENTER_BOT_BUTTON_TEXT, "url": enter_bot_url, "style": "danger"},
        ]]
    }
