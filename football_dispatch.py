"""
football_dispatch.py
لایه هماهنگ‌کننده فوتبال: واکشی از provider فعال (با circuit breaker)،
reconcile کردن State روز، و ساخت payload ارسال هر مسابقه (عکس+کپشن).
شبکه از بیرون تزریق می‌شود تا کاملاً بدون اینترنت تست شود.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import football_client
import football_config
import football_fixtures
import football_images
import message_templates


def fetch_active_provider_events(
    *,
    provider_state: Dict[str, Any],
    bzzoiro_api_key: str,
    football_data_api_key: str,
    fetch_bzzoiro_fn: Callable[..., List[Dict[str, Any]]],
    fetch_football_data_fn: Callable[..., List[Dict[str, Any]]],
    date_from: str,
    date_to: str,
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """بخش ۷.۳/۷.۸: Bzzoiro همیشه منبع اصلی است؛ وقتی در حالت fallback
    هستیم هم هر چرخه یک probe سبک به Bzzoiro می‌زنیم تا امکان بازگشت
    کنترل‌شده باشد. دو منبع هرگز در یک چرخه merge نمی‌شوند."""
    active = provider_state.get("active_provider", "bzzoiro")

    def try_bzzoiro() -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any], Optional[str]]:
        try:
            raw = fetch_bzzoiro_fn(date_from=date_from, date_to=date_to, api_key=bzzoiro_api_key)
            events = [e for e in (football_client.normalize_bzzoiro_event(r) for r in raw) if e]
            new_state = football_client.record_bzzoiro_attempt(provider_state, success=True, now=now)
            return events, new_state, None
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)[:150]
            new_state = football_client.record_bzzoiro_attempt(
                provider_state, success=False, reason=reason, now=now
            )
            return None, new_state, reason

    def try_football_data(state_for_result: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            raw2 = fetch_football_data_fn(date_from=date_from, date_to=date_to, api_key=football_data_api_key)
            events2 = [e for e in (football_client.normalize_football_data_match(r) for r in raw2) if e]
            return events2, state_for_result
        except Exception as exc:  # noqa: BLE001
            print("[FOOTBALL] fallback fetch also failed: {}".format(str(exc)[:200]), flush=True)
            return [], state_for_result

    if active == "bzzoiro":
        events, new_state, _reason = try_bzzoiro()
        if events is not None:
            return events, new_state
        return try_football_data(new_state)

    # در حال fallback: یک probe به Bzzoiro
    events, new_state, _reason = try_bzzoiro()
    if events is not None and new_state.get("active_provider") == "bzzoiro":
        return events, new_state  # دوباره سالم شد و این چرخه کامل به منبع اصلی برگشت
    return try_football_data(new_state)


def refresh_fixtures_cycle(
    *,
    provider_state: Dict[str, Any],
    fixtures_state: List[Dict[str, Any]],
    bzzoiro_api_key: str,
    football_data_api_key: str,
    fetch_bzzoiro_fn: Callable[..., List[Dict[str, Any]]],
    fetch_football_data_fn: Callable[..., List[Dict[str, Any]]],
    local_date: Any,
    tz: Any,
    now: datetime,
    date_from: str,
    date_to: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """یک چرخه کامل refresh: واکشی + reconcile. خروجی: (new_fixtures_state,
    new_provider_state, actions)."""
    events, new_provider_state = fetch_active_provider_events(
        provider_state=provider_state,
        bzzoiro_api_key=bzzoiro_api_key, football_data_api_key=football_data_api_key,
        fetch_bzzoiro_fn=fetch_bzzoiro_fn, fetch_football_data_fn=fetch_football_data_fn,
        date_from=date_from, date_to=date_to, now=now,
    )
    new_fixtures_state, actions = football_fixtures.reconcile_fixtures(
        fixtures_state, events, local_date=local_date, tz=tz, now=now,
    )
    return new_fixtures_state, new_provider_state, actions


def build_match_send_payload(
    entry: Dict[str, Any],
    *,
    license_confirmed: bool,
    tz: Any,
    date_fa_fn: Optional[Callable[[datetime], str]] = None,
    time_fa_fn: Optional[Callable[[datetime], str]] = None,
) -> Dict[str, Any]:
    """کپشن (بخش ۷.۹، قالب تک‌بازی) + نتیجه resolve عکس (بخش ۷.۵) را
    می‌سازد. ارسال واقعی (sendPhoto/sendMessage) بیرون از این فایل است."""
    kickoff_utc = datetime.fromisoformat(entry["kickoff_utc"])
    local_kickoff = kickoff_utc.astimezone(tz)
    league_fa = football_config.canonical_league_fa(entry["league_name"]) or entry["league_name"]
    date_fa = date_fa_fn(local_kickoff) if date_fa_fn else local_kickoff.strftime("%Y-%m-%d")
    time_fa = time_fa_fn(local_kickoff) if time_fa_fn else local_kickoff.strftime("%H:%M")

    caption = message_templates.render_football_message(
        league_fa, entry["home_team"], entry["away_team"], time_fa, date_fa,
    )
    image = football_images.resolve_match_image(
        provider=entry["provider"],
        home_team_id=entry.get("home_team_id"),
        away_team_id=entry.get("away_team_id"),
        license_confirmed=license_confirmed,
        star_player_id=None,  # شناسایی خودکار بازیکن ستاره در این نسخه پیاده نشده؛ فقط لوگوی تیم
    )
    return {"caption": caption, "image": image}
