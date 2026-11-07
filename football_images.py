"""
football_images.py
عکس مسابقه و Bzzoiro Image API (بخش ۷.۵). «مجوز حقوقی الزامی»: کانال
Vochino⁰¹ کاربرد تبلیغاتی/تجاری تلقی می‌شود، پس ارسال عکس فقط وقتی مجاز
است که FOOTBALL_IMAGE_LICENSE_CONFIRMED دقیقاً true باشد — پیش‌فرض fail-closed،
هرگز پیش‌فرض true نیست.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

BZZOIRO_IMG_BASE = "https://sports.bzzoiro.com/img"
ALLOWED_IMAGE_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # محافظه‌کارانه زیر سقف واقعی تلگرام


def build_team_image_url(team_id: str) -> str:
    return "{}/team/{}/".format(BZZOIRO_IMG_BASE, team_id)


def build_player_image_url(player_id: str) -> str:
    return "{}/player/{}/".format(BZZOIRO_IMG_BASE, player_id)


def is_license_confirmed(env_value: Optional[str]) -> bool:
    """fail-closed: فقط رشته دقیق "true" (بدون حساسیت به بزرگی حروف) مجوز
    را فعال می‌کند؛ هر مقدار دیگر (خالی، "false"، تایپوی احتمالی) => مجوز نیست."""
    return (env_value or "").strip().lower() == "true"


def resolve_match_image(
    *,
    provider: str,
    home_team_id: Optional[str],
    away_team_id: Optional[str],
    license_confirmed: bool,
    star_player_id: Optional[str] = None,
) -> Dict[str, Any]:
    """طبق بخش ۷.۵: بدون مجوز، هیچ عکسی ارسال نشود. IDهای fallback هرگز
    IDهای Bzzoiro فرض نمی‌شوند — فراخوان مسئول است که این IDها را فقط
    وقتی پاس بدهد که واقعاً از Bzzoiro resolve شده باشند (provider بی‌اثر
    است چون این تابع به ID اعتماد می‌کند، نه به provider به‌تنهایی؛ لایه
    بالادست باید IDهای fallback را None بگذارد)."""
    if not license_confirmed:
        return {"status": "image_unavailable", "reason": "license_not_confirmed", "url": None, "image_type": None}
    if star_player_id:
        return {"status": "ready", "reason": None, "url": build_player_image_url(star_player_id), "image_type": "player"}
    if home_team_id:
        return {"status": "ready", "reason": None, "url": build_team_image_url(home_team_id), "image_type": "team"}
    if away_team_id:
        return {"status": "ready", "reason": None, "url": build_team_image_url(away_team_id), "image_type": "team"}
    return {"status": "image_unavailable", "reason": "no_valid_id", "url": None, "image_type": None}


def validate_image_response(
    status_code: int, content_type: Optional[str], content_length: Optional[int]
) -> Dict[str, Any]:
    """طبق بخش ۷.۵: 404=عکس موجود نیست، 204=ID نامعتبر؛ در هر حالت
    placeholder یا عکس تیم دیگر فرستاده نمی‌شود — فقط image_unavailable."""
    if status_code == 404:
        return {"ok": False, "reason": "not_found"}
    if status_code == 204:
        return {"ok": False, "reason": "invalid_id"}
    if status_code != 200:
        return {"ok": False, "reason": "unexpected_status_{}".format(status_code)}
    if not content_type or not any(content_type.startswith(ct) for ct in ALLOWED_IMAGE_CONTENT_TYPES):
        return {"ok": False, "reason": "invalid_content_type"}
    if content_length is not None and content_length <= 0:
        return {"ok": False, "reason": "empty"}
    if content_length is not None and content_length > MAX_IMAGE_BYTES:
        return {"ok": False, "reason": "too_large"}
    return {"ok": True, "reason": None}
