"""
football_client.py
Bzzoiro Sports Data API v2 (منبع اصلی، بخش ۷.۲) و Football-Data.org API v4
(فقط fallback، بخش ۷.۳) + circuit breaker بین دو provider (بخش ۷.۳/۷.۸).

هیچ ادغام بین دو منبع انجام نمی‌شود؛ هر چرخه فقط یک provider مرجع دارد.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from http_retry import HttpCallFailed, request_with_retry

BZZOIRO_BASE_URL = "https://sports.bzzoiro.com/api/v2"
BZZOIRO_EVENTS_PATH = "/events/"
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
FOOTBALL_DATA_MATCHES_PATH = "/matches"

BZZOIRO_SCHEDULABLE_STATUS = "upcoming"
BZZOIRO_TERMINAL_STATUSES = {"live", "finished", "cancelled", "postponed", "unresolved"}

# Football-Data.org statuses -> نرمال‌شده؛ فقط SCHEDULED/TIMED قابل زمان‌بندی‌اند.
FOOTBALL_DATA_SCHEDULABLE_STATUSES = {"SCHEDULED", "TIMED"}

BZZOIRO_FAILURE_THRESHOLD = 2     # این تعداد شکست متوالی -> failover به fallback
BZZOIRO_RECOVERY_SUCCESSES = 2    # این تعداد موفقیت متوالی probe -> بازگشت به Bzzoiro


# ============================================================
# نرمال‌سازی رویداد (قابل تست بدون شبکه)
# ============================================================
def normalize_bzzoiro_event(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        event_id = raw["id"]
        league_name = raw["league"]["name"] if isinstance(raw.get("league"), dict) else raw.get("league_name")
        home_team = raw["home_team"]["name"] if isinstance(raw.get("home_team"), dict) else raw.get("home_team_name")
        away_team = raw["away_team"]["name"] if isinstance(raw.get("away_team"), dict) else raw.get("away_team_name")
        home_team_id = raw["home_team"]["id"] if isinstance(raw.get("home_team"), dict) else raw.get("home_team_id")
        away_team_id = raw["away_team"]["id"] if isinstance(raw.get("away_team"), dict) else raw.get("away_team_id")
        kickoff_raw = raw["kickoff_utc"] if "kickoff_utc" in raw else raw.get("start_time")
        status = (raw.get("status") or "").strip().lower()
        if not (event_id and league_name and home_team and away_team and kickoff_raw):
            return None
        kickoff_utc = _parse_iso_utc(kickoff_raw)
        if kickoff_utc is None:
            return None
        return {
            "provider": "bzzoiro",
            "event_id": str(event_id),
            "league_name": league_name,
            "home_team": home_team,
            "away_team": away_team,
            "home_team_id": str(home_team_id) if home_team_id is not None else None,
            "away_team_id": str(away_team_id) if away_team_id is not None else None,
            "kickoff_utc": kickoff_utc,
            "status": status,
            "schedulable": status == BZZOIRO_SCHEDULABLE_STATUS,
        }
    except (KeyError, TypeError):
        return None


def normalize_football_data_match(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        event_id = raw["id"]
        league_name = (raw.get("competition") or {}).get("name")
        home_team = (raw.get("homeTeam") or {}).get("name")
        away_team = (raw.get("awayTeam") or {}).get("name")
        kickoff_raw = raw.get("utcDate")
        status = (raw.get("status") or "").strip().upper()
        if not (event_id and league_name and home_team and away_team and kickoff_raw):
            return None
        kickoff_utc = _parse_iso_utc(kickoff_raw)
        if kickoff_utc is None:
            return None
        return {
            "provider": "football_data",
            "event_id": str(event_id),
            "league_name": league_name,
            "home_team": home_team,
            "away_team": away_team,
            "home_team_id": None,   # IDهای Bzzoiro هرگز با این منبع یکی فرض نمی‌شوند
            "away_team_id": None,
            "kickoff_utc": kickoff_utc,
            "status": status,
            "schedulable": status in FOOTBALL_DATA_SCHEDULABLE_STATUSES,
        }
    except (KeyError, TypeError):
        return None


def _parse_iso_utc(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def dedupe_fingerprint(event: Dict[str, Any]) -> str:
    """کلید dedupe بر اساس fingerprint تیم‌ها+زمان؛ برای جلوگیری از فرض
    یکی‌بودن event_id بین دو provider متفاوت (بخش ۷.۶)."""
    home = (event.get("home_team") or "").strip().lower()
    away = (event.get("away_team") or "").strip().lower()
    kickoff = event.get("kickoff_utc")
    kickoff_key = kickoff.isoformat() if kickoff else ""
    return "{}|{}|{}".format(home, away, kickoff_key)


# ============================================================
# Circuit breaker بین Bzzoiro و Football-Data.org
# ============================================================
def initial_provider_state() -> Dict[str, Any]:
    return {
        "active_provider": "bzzoiro",
        "consecutive_bzzoiro_failures": 0,
        "consecutive_bzzoiro_successes": 0,
        "last_failover_reason": None,
        "last_failover_at": None,
    }


def record_bzzoiro_attempt(
    state: Dict[str, Any], success: bool, reason: Optional[str] = None, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """نتیجه هر تلاش Bzzoiro (چه وقتی منبع اصلی فعال است، چه به‌عنوان
    probe سلامت وقتی در حال fallback هستیم) را اعمال می‌کند."""
    now = now or datetime.now(timezone.utc)
    new_state = dict(state)
    if success:
        new_state["consecutive_bzzoiro_failures"] = 0
        new_state["consecutive_bzzoiro_successes"] = state.get("consecutive_bzzoiro_successes", 0) + 1
        if (
            state.get("active_provider") == "football_data"
            and new_state["consecutive_bzzoiro_successes"] >= BZZOIRO_RECOVERY_SUCCESSES
        ):
            new_state["active_provider"] = "bzzoiro"
            new_state["last_failover_reason"] = "bzzoiro_recovered"
            new_state["last_failover_at"] = now.isoformat().replace("+00:00", "Z")
    else:
        new_state["consecutive_bzzoiro_successes"] = 0
        new_state["consecutive_bzzoiro_failures"] = state.get("consecutive_bzzoiro_failures", 0) + 1
        if (
            state.get("active_provider") == "bzzoiro"
            and new_state["consecutive_bzzoiro_failures"] >= BZZOIRO_FAILURE_THRESHOLD
        ):
            new_state["active_provider"] = "football_data"
            new_state["last_failover_reason"] = reason or "bzzoiro_failed"
            new_state["last_failover_at"] = now.isoformat().replace("+00:00", "Z")
    return new_state


# ============================================================
# Network fetch
# ============================================================
def fetch_bzzoiro_events(
    *,
    date_from: str,
    date_to: str,
    api_key: str,
    status: str = "upcoming",
    base_url: str = BZZOIRO_BASE_URL,
    timeout: float = 10.0,
    max_attempts: int = 3,
    max_pages: int = 5,
    page_limit: int = 200,
) -> List[Dict[str, Any]]:
    """صفحه‌بندی را دنبال می‌کند تا `next` تمام شود یا به سقف صفحه برسیم."""
    events: List[Dict[str, Any]] = []
    offset = 0
    headers = {"Authorization": "Token {}".format(api_key)}
    for _ in range(max_pages):
        response = request_with_retry(
            "GET", base_url + BZZOIRO_EVENTS_PATH,
            headers=headers,
            params={"date_from": date_from, "date_to": date_to, "status": status,
                    "limit": page_limit, "offset": offset},
            timeout=timeout, max_attempts=max_attempts,
        )
        data = response.json()
        if not isinstance(data, dict) or "results" not in data:
            raise HttpCallFailed("unexpected Bzzoiro events schema")
        page_results = data.get("results") or []
        events.extend(page_results)
        if not data.get("next"):
            break
        offset += page_limit
    return events


def fetch_football_data_matches(
    *,
    date_from: str,
    date_to: str,
    api_key: str,
    base_url: str = FOOTBALL_DATA_BASE_URL,
    timeout: float = 10.0,
    max_attempts: int = 3,
) -> List[Dict[str, Any]]:
    headers = {"X-Auth-Token": api_key}
    response = request_with_retry(
        "GET", base_url + FOOTBALL_DATA_MATCHES_PATH,
        headers=headers, params={"dateFrom": date_from, "dateTo": date_to},
        timeout=timeout, max_attempts=max_attempts,
    )
    data = response.json()
    if not isinstance(data, dict) or "matches" not in data:
        raise HttpCallFailed("unexpected Football-Data.org schema")
    return data.get("matches") or []
