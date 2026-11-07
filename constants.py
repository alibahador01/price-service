"""
constants.py
چند رشتهٔ ثابت با گلیف‌های خاص (سوپراسکریپت ⁰¹، نماد 🜲) که در چند فایل
تکرار می‌شوند. برای جلوگیری از اشتباه تایپی در کاراکترهای یونیکد، همه‌جا از
همین ثابت‌ها import می‌شود، نه تایپ مستقیم.

به‌روزرسانی برند: طبق درخواست صریح کاربر، نام از Vochino⁰¹ به Vochino-Hub⁰¹
تغییر کرد؛ سوپراسکریپت ⁰¹ بدون تغییر باقی ماند. معادل فارسی «ووچینو-هاب»
انتخاب مستقیم من (ترجمه آشکار Hub) است، نه چیزی که کاربر عیناً نوشته باشد؛
در صورت نیاز به شکل دیگری، قابل اصلاح است.
"""
from __future__ import annotations

# ⁰ = U+2070 (SUPERSCRIPT ZERO), ¹ = U+00B9 (SUPERSCRIPT ONE)
BRAND_SUPERSCRIPT = "\u2070\u00b9"
FA_BRAND_NAME = "ووچینو-هاب" + BRAND_SUPERSCRIPT       # ووچینو-هاب⁰¹
EN_BRAND_NAME = "Vochino-Hub" + BRAND_SUPERSCRIPT       # Vochino-Hub⁰¹

RED_MARKER = "\U0001F732"  # 🜲
