"""
ai_guards.py
محافظت چندلایه در برابر توهم و خطای عددی (بخش ۵ سند) + اعتبارسنجی Schema
خروجی مدل (بخش ۴.۴). هیچ‌کدام از این توابع به شبکه نیاز ندارند؛ بنابراین
کاملاً بدون اتصال اینترنت قابل تست‌اند. این لایه‌ها جایگزین قضاوت مدل
زبانی می‌شوند، نه مکمل تزئینی آن.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set

ALLOWED_BIAS = ("bullish", "bearish", "neutral", "insufficient_data")
REQUIRED_ASSET_FIELDS = (
    "symbol", "overall_bias", "bullish_scenario", "bearish_scenario",
    "conclusion", "limitations", "evidence_refs",
)

# ارقام فارسی/عربی و لاتین؛ متن تحلیل مدل نباید هیچ‌کدام را داشته باشد (لایه ۴).
_DIGIT_PATTERN = re.compile(r"[0-9\u06F0-\u06F9\u0660-\u0669]")


# ============================================================
# Layer 3 — Evidence Guard
# ============================================================
def build_evidence_paths(fact_block: Dict[str, Any]) -> Set[str]:
    """مسیرهای مجاز ارجاع (evidence_ref) را فقط از مقادیر غیر-None همان
    Fact Block می‌سازد؛ مدل نمی‌تواند به فیلد خالی یا نماد دیگر ارجاع دهد."""
    symbol = fact_block.get("symbol", "")
    paths: Set[str] = set()
    if fact_block.get("price") is not None:
        paths.add("{}.price".format(symbol))
    if fact_block.get("change_24h_pct") is not None:
        paths.add("{}.change_24h_pct".format(symbol))
    indicators = fact_block.get("indicators") or {}
    for key, value in indicators.items():
        if value is not None:
            paths.add("{}.indicators.{}".format(symbol, key))
    large_trades = fact_block.get("large_trades") or {}
    if large_trades.get("status") == "valid":
        for key in ("buy_notional_usdt", "sell_notional_usdt", "buy_percentage", "sell_percentage"):
            if large_trades.get(key) is not None:
                paths.add("{}.large_trades.{}".format(symbol, key))
    return paths


def evidence_refs_valid(evidence_refs: Sequence[str], allowed_paths: Set[str]) -> List[str]:
    """ارجاع‌های نامعتبر را برمی‌گرداند (خالی = همه معتبرند)."""
    return [ref for ref in evidence_refs if ref not in allowed_paths]


# ============================================================
# Layer 4 — Numeric Guard
# ============================================================
def text_contains_digits(text: str) -> bool:
    return bool(_DIGIT_PATTERN.search(text or ""))


# ============================================================
# JSON Schema Guard (بخش ۴.۴)
# ============================================================
def validate_model_schema(model_output: Dict[str, Any], allowed_symbols: Sequence[str]) -> List[str]:
    errors: List[str] = []
    if not isinstance(model_output, dict):
        return ["model output is not a JSON object"]
    if "assets" not in model_output or not isinstance(model_output["assets"], list):
        return ["missing or invalid 'assets' array"]
    seen_symbols: Set[str] = set()
    for i, asset in enumerate(model_output["assets"]):
        prefix = "assets[{}]".format(i)
        if not isinstance(asset, dict):
            errors.append("{}: not an object".format(prefix))
            continue
        for field in REQUIRED_ASSET_FIELDS:
            if field not in asset:
                errors.append("{}: missing field '{}'".format(prefix, field))
        symbol = asset.get("symbol")
        if symbol not in allowed_symbols:
            errors.append("{}: symbol '{}' not in allowlist".format(prefix, symbol))
        elif symbol in seen_symbols:
            errors.append("{}: duplicate symbol '{}'".format(prefix, symbol))
        else:
            seen_symbols.add(symbol)
        if asset.get("overall_bias") not in ALLOWED_BIAS:
            errors.append("{}: invalid overall_bias '{}'".format(prefix, asset.get("overall_bias")))
        if not isinstance(asset.get("evidence_refs"), list):
            errors.append("{}: evidence_refs must be a list".format(prefix))
    missing_symbols = set(allowed_symbols) - seen_symbols
    if missing_symbols:
        errors.append("missing assets for symbols: {}".format(sorted(missing_symbols)))
    return errors


# ============================================================
# ترکیب کامل Guardها برای یک خروجی مدل
# ============================================================
def guard_model_output(
    fact_blocks_by_symbol: Dict[str, Dict[str, Any]],
    model_output: Dict[str, Any],
) -> List[str]:
    """همه خطاهای schema/evidence/numeric را یک‌جا برمی‌گرداند. لیست خالی
    یعنی خروجی مدل مجاز به رندر شدن است (پیش از Fact Block validate هم باید
    اجرا شده باشد)."""
    allowed_symbols = list(fact_blocks_by_symbol.keys())
    errors = validate_model_schema(model_output, allowed_symbols)
    if errors:
        return errors  # اگر schema خراب است، بررسی عمیق‌تر بی‌معناست

    for asset in model_output.get("assets", []):
        symbol = asset.get("symbol")
        fb = fact_blocks_by_symbol.get(symbol)
        if fb is None:
            errors.append("{}: no matching fact block".format(symbol))
            continue
        allowed_paths = build_evidence_paths(fb)
        invalid_refs = evidence_refs_valid(asset.get("evidence_refs") or [], allowed_paths)
        if invalid_refs:
            errors.append("{}: invalid evidence_refs: {}".format(symbol, invalid_refs))
        if asset.get("overall_bias") != "insufficient_data" and not asset.get("evidence_refs"):
            errors.append("{}: directional bias without any evidence_ref".format(symbol))
        for text_field in ("bullish_scenario", "bearish_scenario", "conclusion"):
            text = asset.get(text_field) or ""
            if text_contains_digits(text):
                errors.append("{}: '{}' contains a free-form number".format(symbol, text_field))
    return errors


# ============================================================
# Layer 5 — Cross-Model Review (نتیجهٔ Groq به‌عنوان بازبین، نه مرجع حقیقت)
# ============================================================
def apply_reviewer_verdict(
    model_output: Dict[str, Any], reviewer_verdict: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    - اگر reviewer verdict == reject → None (کل خروجی رد می‌شود؛ بالادست باید
      یک‌بار اصلاح از Gemini بخواهد یا نوبت را skip کند).
    - اگر تناقض/ادعای بی‌پشتوانه برای یک نماد گزارش شد → bias همان نماد به
      neutral تنزل پیدا می‌کند؛ رأی Groq جای Guard محلی را نمی‌گیرد، فقط
      قاطعیت را کم می‌کند.
    """
    if reviewer_verdict.get("verdict") == "reject":
        return None
    flagged_symbols: Set[str] = set()
    for claim in reviewer_verdict.get("unsupported_claims", []) or []:
        symbol = claim.get("symbol") if isinstance(claim, dict) else None
        if symbol:
            flagged_symbols.add(symbol)
    for item in reviewer_verdict.get("contradictions", []) or []:
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if symbol:
            flagged_symbols.add(symbol)
    if not flagged_symbols:
        return model_output
    downgraded = dict(model_output)
    downgraded["assets"] = []
    for asset in model_output.get("assets", []):
        asset = dict(asset)
        if asset.get("symbol") in flagged_symbols and asset.get("overall_bias") != "insufficient_data":
            asset["overall_bias"] = "neutral"
        downgraded["assets"].append(asset)
    return downgraded
