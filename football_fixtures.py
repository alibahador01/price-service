"""
football_fixtures.py
انتخاب روزانه حداکثر ۱۰ مسابقه (بخش ۷.۴) + زمان‌بندی هر مسابقه دقیقاً ۹۰
دقیقه پیش از kickoff، تشخیص missed_window، و هماهنگی با تغییر/لغو مسابقه
(بخش ۷.۶/۷.۷). همه چیز خالص پایتون؛ شبکه در football_client.py است.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

import football_client
import football_config

FIXTURE_STATUS_PENDING = "pending"
FIXTURE_STATUS_SENT = "sent"
FIXTURE_STATUS_MISSED_WINDOW = "missed_window"


def local_date_of(kickoff_utc: datetime, tz) -> Any:
    return kickoff_utc.astimezone(tz).date()


def compute_scheduled_send_at(kickoff_utc: datetime) -> datetime:
    return kickoff_utc - timedelta(minutes=football_config.SEND_BEFORE_KICKOFF_MINUTES)


def select_daily_fixtures(
    events: List[Dict[str, Any]],
    *,
    local_date,
    tz,
    max_matches: int = football_config.MAX_MATCHES_PER_LOCAL_DAY,
) -> List[Dict[str, Any]]:
    """طبق بخش ۷.۴: فقط allowlist، فقط همان روز محلی، بدون تکرار، حداکثر
    ده مورد با اولویت تیم‌هر دو/یکی منتخب سپس اولویت لیگ سپس نزدیک‌ترین
    زمان شروع. اگر کمتر از ۱۰ واجد شرایط بود، همان تعداد کمتر برمی‌گردد —
    هرگز برای پرکردن سهمیه، بازی نامرتبط اضافه نمی‌شود."""
    seen_fp = set()
    candidates: List[Dict[str, Any]] = []
    for e in events:
        if not e.get("schedulable"):
            continue
        if not football_config.is_allowed_league(e.get("league_name", "")):
            continue
        if not (
            football_config.is_selected_team(e.get("home_team", ""))
            or football_config.is_selected_team(e.get("away_team", ""))
        ):
            continue
        if local_date_of(e["kickoff_utc"], tz) != local_date:
            continue
        fp = football_client.dedupe_fingerprint(e)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        candidates.append(e)

    candidates.sort(
        key=lambda e: (
            football_config.selection_priority(e["league_name"], e["home_team"], e["away_team"]),
            e["kickoff_utc"],
        )
    )
    return candidates[:max_matches]


def identity_key(provider: str, event_id: str) -> str:
    """کلید هویت برای تطبیق «همین مسابقه» بین دو واکشی متوالی از همان
    provider — بر خلاف dedupe_fingerprint (که عمداً شامل kickoff است و
    برای جلوگیری از ارسال دوباره به‌کار می‌رود)، این کلید با تغییر kickoff
    عوض نمی‌شود، چون این دقیقاً همان سناریویی است که باید edit تشخیص داده
    شود، نه cancel+new."""
    return "{}:{}".format(provider, event_id)


def build_fixture_entry(event: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """اگر مسابقه بعد از زمان هدف کشف شد، همان لحظه missed_window می‌شود
    تا یادآور دیرهنگام به‌جای زمان‌بندی دقیق منتشر نشود (بخش ۷.۶)."""
    scheduled = compute_scheduled_send_at(event["kickoff_utc"])
    status = FIXTURE_STATUS_MISSED_WINDOW if now > scheduled else FIXTURE_STATUS_PENDING
    return {
        "identity_key": identity_key(event["provider"], event["event_id"]),
        "fingerprint": football_client.dedupe_fingerprint(event),
        "provider": event["provider"],
        "event_id": event["event_id"],
        "league_name": event["league_name"],
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "home_team_id": event.get("home_team_id"),
        "away_team_id": event.get("away_team_id"),
        "kickoff_utc": event["kickoff_utc"].isoformat(),
        "scheduled_send_at_utc": scheduled.isoformat(),
        "status": status,
        "actual_sent_at_utc": None,
        "chat_message_id": None,
    }


def due_fixture_sends(fixtures_state: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    due = []
    for entry in fixtures_state:
        if entry.get("status") != FIXTURE_STATUS_PENDING:
            continue
        scheduled = datetime.fromisoformat(entry["scheduled_send_at_utc"])
        if scheduled <= now:
            due.append(entry)
    due.sort(key=lambda e: e["scheduled_send_at_utc"])
    return due


def mark_sent(entry: Dict[str, Any], now: datetime, chat_message_id: Any = None) -> Dict[str, Any]:
    updated = dict(entry)
    updated["status"] = FIXTURE_STATUS_SENT
    updated["actual_sent_at_utc"] = now.isoformat()
    updated["chat_message_id"] = chat_message_id
    return updated


def reconcile_fixtures(
    existing_state: List[Dict[str, Any]],
    fresh_events: List[Dict[str, Any]],
    *,
    local_date,
    tz,
    now: datetime,
    max_matches: int = football_config.MAX_MATCHES_PER_LOCAL_DAY,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """State روز را با داده تازه merge می‌کند. خروجی: (new_state, actions).
    actions = [{"type": "edit"|"cancel", "entry": {...}}]
    - مسابقه‌ای که دیگر در انتخاب امروز نیست و قبلاً ارسال شده -> cancel.
    - مسابقه‌ای که kickoff آن تغییر کرد و قبلاً ارسال شده -> edit.
    - مسابقه جدید -> وارد state می‌شود (pending یا missed_window)."""
    selected = select_daily_fixtures(fresh_events, local_date=local_date, tz=tz, max_matches=max_matches)
    fresh_by_key = {identity_key(e["provider"], e["event_id"]): e for e in selected}
    existing_by_key = {e["identity_key"]: e for e in existing_state}

    new_state: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    for key, entry in existing_by_key.items():
        fresh = fresh_by_key.pop(key, None)
        if fresh is None:
            if entry.get("status") == FIXTURE_STATUS_SENT:
                actions.append({"type": "cancel", "entry": entry})
            # اگر pending/missed_window بود و دیگر واجد شرایط نیست، فقط حذف می‌شود
            continue

        new_kickoff_iso = fresh["kickoff_utc"].isoformat()
        if entry.get("kickoff_utc") != new_kickoff_iso:
            new_scheduled = compute_scheduled_send_at(fresh["kickoff_utc"])
            updated = dict(entry)
            updated["kickoff_utc"] = new_kickoff_iso
            updated["scheduled_send_at_utc"] = new_scheduled.isoformat()
            updated["fingerprint"] = football_client.dedupe_fingerprint(fresh)
            if entry.get("status") == FIXTURE_STATUS_SENT:
                actions.append({"type": "edit", "entry": updated})
            elif entry.get("status") in (FIXTURE_STATUS_PENDING, FIXTURE_STATUS_MISSED_WINDOW):
                updated["status"] = (
                    FIXTURE_STATUS_MISSED_WINDOW if now > new_scheduled else FIXTURE_STATUS_PENDING
                )
            new_state.append(updated)
        else:
            new_state.append(entry)

    for key, fresh in fresh_by_key.items():
        new_state.append(build_fixture_entry(fresh, now))

    return new_state, actions
