"""
groq_client.py
کلاینت Groq — بازبین مستقل / fallback (بخش ۴.۱، ۴.۶ سند). از SDK رسمی
`groq` استفاده می‌کند. مدل پیش‌فرض `openai/gpt-oss-120b` طبق سند؛
`llama-3.3-70b-versatile` دیگر استفاده نمی‌شود (در سند بازنشسته اعلام شده).

⚠️ مثل gemini_client.py، امضای دقیق متد در این محیط بدون اینترنت تست زنده
نشده؛ پیش از Deploy با https://console.groq.com/docs/models تطبیق بده.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

GROQ_MODEL_DEFAULT = "openai/gpt-oss-120b"


class GroqUnavailable(Exception):
    """هر خطای ارتباطی/پارسی Groq."""


def call_groq_json(
    system_prompt: str,
    user_payload: Dict[str, Any],
    *,
    api_key: str,
    model: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """برای نقش بازبین (بخش ۴.۶) و همچنین fallback تولید خروجی با همان
    Schema (بخش ۴.۳ بند ۸) استفاده می‌شود؛ فراخوان مشخص می‌کند کدام
    system_prompt را بفرستد."""
    model = model or os.environ.get("GROQ_MODEL", GROQ_MODEL_DEFAULT)
    if not api_key:
        raise GroqUnavailable("GROQ_API_KEY is missing")
    try:
        from groq import Groq
    except ImportError as exc:
        raise GroqUnavailable("groq package not installed: {}".format(exc))

    try:
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise GroqUnavailable("Groq call failed: {}".format(str(exc)[:300]))

    try:
        content = completion.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise GroqUnavailable("unexpected Groq response shape: {}".format(exc))
    if not content:
        raise GroqUnavailable("empty response content from Groq")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GroqUnavailable("Groq returned invalid JSON: {}".format(exc))
