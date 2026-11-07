"""
gemini_client.py
کلاینت Gemini — تحلیل‌گر اصلی (بخش ۴.۱، ۴.۴، ۴.۵ سند). از SDK رسمی
`google-genai` استفاده می‌کند.

⚠️ هشدار صادقانه: این فایل در محیطی بدون دسترسی اینترنت نوشته شده است؛
امضای دقیق متدهای SDK (به‌خصوص نام پارامتر Thinking Level برای
gemini-3.8-flash) تست زنده نشده. پیش از Deploy، طبق بخش ۱۶ سند، حتماً با
مستندات فعلی https://ai.google.dev/gemini-api/docs تطبیق داده شود. اگر نام
پارامتر عوض شده باشد، این تابع Exception می‌دهد و crypto_intelligence طبق
Guard لایه ۵ آن چرخه را skip می‌کند؛ سرویس قیمت متاثر نمی‌شود.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

GEMINI_MODEL_DEFAULT = "gemini-3.8-flash"
GEMINI_THINKING_LEVEL_DEFAULT = "high"  # طبق بخش ۴.۱: minimal ممنوع است


class GeminiUnavailable(Exception):
    """هر خطای ارتباطی/پارسی Gemini؛ بالادست باید fallback به Groq یا skip کند."""


def call_gemini_structured(
    system_prompt: str,
    user_payload: Dict[str, Any],
    *,
    api_key: str,
    model: Optional[str] = None,
    thinking_level: str = GEMINI_THINKING_LEVEL_DEFAULT,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """یک خروجی JSON ساختاریافته برمی‌گرداند یا GeminiUnavailable raise
    می‌کند. فراخوان (crypto_intelligence) مسئول Guardها و تصمیم fallback
    است؛ این تابع فقط ارتباط با API را انجام می‌دهد."""
    model = model or os.environ.get("GEMINI_MODEL", GEMINI_MODEL_DEFAULT)
    if not api_key:
        raise GeminiUnavailable("GEMINI_API_KEY is missing")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiUnavailable("google-genai package not installed: {}".format(exc))

    try:
        client = genai.Client(api_key=api_key)
        # TODO(verify-before-deploy): نام و ساختار دقیق ThinkingConfig را با
        # نسخه فعلی SDK تطبیق بده؛ ممکن است thinking_level یا نام دیگری باشد.
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        )
        response = client.models.generate_content(
            model=model,
            contents=json.dumps(user_payload, ensure_ascii=False),
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - هر خطای SDK باید به fallback برسد
        raise GeminiUnavailable("Gemini call failed: {}".format(str(exc)[:300]))

    text = getattr(response, "text", None)
    if not text:
        raise GeminiUnavailable("empty response text from Gemini")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GeminiUnavailable("Gemini returned invalid JSON: {}".format(exc))
