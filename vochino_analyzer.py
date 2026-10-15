"""Optional production-safe sports and crypto analyzers for the Vochino service.

This module is intentionally isolated from the existing voucher/price loops. It only
reads a caller-supplied STATE snapshot and sends through a caller-supplied function.
"""
from __future__ import annotations

import copy
import datetime as dt
import html
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

try:
    import fcntl  # Unix/Render process-level leader lock
except ImportError:  # pragma: no cover - Render runs on Linux
    fcntl = None

TZ = ZoneInfo("Europe/Istanbul")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_TIMEOUT = (3.05, 8.0)
AI_TIMEOUT_SECONDS = 15
MAX_RETRIES = 2
CACHE_TTL_SECONDS = 25 * 60
ODDS_CACHE_TTL_SECONDS = 20 * 60
AI_DISAGREEMENT_THRESHOLD = float(os.getenv("AI_DISAGREEMENT_THRESHOLD", "10"))

# ESPN identifiers are kept as supplied by the project owner. Availability is checked live.
FOOTBALL_LEAGUES: Sequence[Tuple[str, str, str]] = (
    ("eng.1", "EPL", "انگلیس / Premier League"),
    ("esp.1", "La Liga", "اسپانیا / La Liga"),
    ("ita.1", "Serie A", "ایتالیا / Serie A"),
    ("ger.1", "Bundesliga", "آلمان / Bundesliga"),
    ("fra.1", "Ligue 1", "فرانسه / Ligue 1"),
    ("tur.1", "Süper Lig", "ترکیه / Süper Lig"),
    ("uefa.champions", "UCL", "اروپا / Champions League"),
    ("uefa.europa", "Europa League", "اروپا / Europa League"),
    ("uefa.europa.conf", "Conference League", "اروپا / Conference League"),
    ("eng.fa", "FA Cup", "انگلیس / FA Cup"),
    ("esp.copa_del_rey", "Copa del Rey", "اسپانیا / Copa del Rey"),
    ("ned.1", "Eredivisie", "هلند / Eredivisie"),
    ("por.1", "Primeira Liga", "پرتغال / Primeira Liga"),
    ("bel.1", "Pro League", "بلژیک / Pro League"),
    ("mex.1", "Liga MX", "مکزیک / Liga MX"),
    ("bra.1", "Brasileirão", "برزیل / Brasileirão"),
    ("sco.1", "Scottish Premiership", "اسکاتلند / Scottish Premiership"),
    ("rus.1", "Russian Premier League", "روسیه / Russian Premier League"),
)
LEAGUES: Sequence[Dict[str, str]] = tuple(
    {"sport": "soccer", "id": league_id, "name": label, "code": code}
    for league_id, code, label in FOOTBALL_LEAGUES
) + (
    {"sport": "basketball", "id": "nba", "name": "NBA / آمریکا", "code": "NBA"},
    {"sport": "basketball", "id": "wnba", "name": "WNBA / آمریکا", "code": "WNBA"},
    {"sport": "hockey", "id": "nhl", "name": "NHL / آمریکا-کانادا", "code": "NHL"},
    {"sport": "tennis", "id": "atp", "name": "ATP / تنیس مردان", "code": "ATP"},
    {"sport": "tennis", "id": "wta", "name": "WTA / تنیس زنان", "code": "WTA"},
)

ODDS_SPORT_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "eng.1": ("soccer_epl",),
    "esp.1": ("soccer_spain_la_liga",),
    "ita.1": ("soccer_italy_serie_a",),
    "ger.1": ("soccer_germany_bundesliga",),
    "fra.1": ("soccer_france_ligue_one",),
    "tur.1": ("soccer_turkey_super_league",),
    "uefa.champions": ("soccer_uefa_champs_league",),
    "uefa.europa": ("soccer_uefa_europa_league",),
    "uefa.europa.conf": ("soccer_uefa_europa_conference_league",),
    "ned.1": ("soccer_netherlands_eredivisie",),
    "por.1": ("soccer_portugal_primeira_liga",),
    "bel.1": ("soccer_belgium_first_division_a",),
    "mex.1": ("soccer_mexico_ligamx",),
    "bra.1": ("soccer_brazil_campeonato",),
    "sco.1": ("soccer_scotland_premiership",),
    "rus.1": ("soccer_russia_premier_league",),
    "nba": ("basketball_nba",),
    "wnba": ("basketball_wnba",),
    "nhl": ("icehockey_nhl",),
}


def _log(logger: Callable[[str], None], message: str) -> None:
    try:
        logger("[ANALYZER] " + message[:700])
    except Exception:
        pass


def _now() -> dt.datetime:
    return dt.datetime.now(TZ)


def _date_key(now: Optional[dt.datetime] = None) -> str:
    return (now or _now()).date().isoformat()


def _jalali_date(now: Optional[dt.datetime] = None) -> str:
    now = now or _now()
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=now).strftime("%Y/%m/%d")
    except Exception:
        return now.strftime("%Y-%m-%d")


def _fa_digits(value: Any) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _safe_text(value: Any, max_len: int = 500) -> str:
    if value is None:
        return ""
    return html.escape(str(value).strip()[:max_len], quote=False)


def _json_get(url: str, *, params: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None,
              timeout: Any = ESPN_TIMEOUT, retries: int = MAX_RETRIES) -> Any:
    """Bounded GET with short retries; never logs query strings or credentials."""
    request_headers = {"User-Agent": "VochinoAnalyzer/1.0", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=request_headers, timeout=timeout)
            if response.status_code == 404:
                return None
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt < retries:
                    time.sleep(min(1.5 * (2 ** attempt), 4.0))
                    continue
                return None
            if response.status_code != 200:
                return None
            try:
                return response.json()
            except (ValueError, TypeError):
                return None
        except requests.RequestException:
            if attempt < retries:
                time.sleep(min(1.0 * (2 ** attempt), 3.0))
    return None


def _parse_time(value: Any) -> Optional[dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(TZ)
    except (ValueError, OverflowError):
        return None


def _normalise_name(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _same_side(a: str, b: str) -> bool:
    na, nb = _normalise_name(a), _normalise_name(b)
    return bool(na and nb and (na == nb or (min(len(na), len(nb)) >= 5 and (na in nb or nb in na))))


def _status(event: Dict[str, Any]) -> str:
    status = event.get("status") or {}
    kind = status.get("type") or {}
    state = str(kind.get("state", "")).lower()
    completed = kind.get("completed") is True
    detail = kind.get("shortDetail") or kind.get("detail") or kind.get("description")
    if completed or state in ("post", "final"):
        return "تمام‌شده" + (f" ({detail})" if detail else "")
    if state in ("in", "live"):
        return "زنده" + (f" ({detail})" if detail else "")
    if state in ("pre", "scheduled"):
        return "شروع‌نشده" + (f" ({detail})" if detail else "")
    return str(detail or "وضعیت اعلام نشده")


def _normalise_event(raw: Dict[str, Any], league: Dict[str, str], received_at: str) -> Optional[Dict[str, Any]]:
    event_id = str(raw.get("id") or "").strip()
    start = _parse_time(raw.get("date"))
    competition = (raw.get("competitions") or [{}])[0]
    competitors = competition.get("competitors") or []
    names: Dict[str, str] = {}
    scores: Dict[str, Optional[str]] = {}
    for competitor in competitors:
        side = str(competitor.get("homeAway") or "").lower()
        team = competitor.get("team") or competitor.get("athlete") or {}
        name = team.get("displayName") or team.get("fullName") or team.get("name") or competitor.get("displayName")
        if not side and len(competitors) == 2:
            side = "home" if not names else "away"
        if side in ("home", "away") and name:
            names[side] = str(name)
            score = competitor.get("score")
            scores[side] = str(score) if score is not None else None
    if not event_id or not start:
        return None
    if not names:
        label = str(raw.get("name") or raw.get("shortName") or "").strip()
        if " vs " in label.lower():
            parts = re.split(r"\s+vs\.?\s+", label, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                names = {"home": parts[0], "away": parts[1]}
    return {
        "id": event_id,
        "sport": league["sport"],
        "league_id": league["id"],
        "league": league["name"],
        "league_code": league["code"],
        "name": str(raw.get("name") or raw.get("shortName") or ""),
        "home": names.get("home", ""),
        "away": names.get("away", ""),
        "home_score": scores.get("home"),
        "away_score": scores.get("away"),
        "start": start.isoformat(),
        "start_dt": start,
        "status": _status({**raw, "status": raw.get("status") or competition.get("status") or {}}),
        "state": str(((raw.get("status") or competition.get("status") or {}).get("type") or {}).get("state", "unknown")).lower(),
        "source": "ESPN",
        "received_at": received_at,
        "url": str(raw.get("links", [{}])[0].get("href", "")) if raw.get("links") else "",
        "raw": raw,
    }


def _bounded_library_call(function: Callable[..., Any], *, timeout: float = 9.0, **kwargs: Any) -> Any:
    """Run an optional data SDK without allowing its network call to block our scheduler."""
    result: List[Any] = []
    finished = threading.Event()

    def invoke() -> None:
        try:
            result.append(function(**kwargs))
        except Exception:
            result.append(None)
        finally:
            finished.set()

    threading.Thread(target=invoke, daemon=True, name="sports-skills-call").start()
    if not finished.wait(timeout):
        return None
    return result[0] if result else None


def _walk_event_lists(payload: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """Extract only clearly event-like rows from the package's documented data envelope."""
    if depth > 6:
        return []
    if isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
        if rows and any(
            any(key in item for key in ("id", "event_id", "game_id", "match_id"))
            and any(key in item for key in ("date", "start", "start_time", "startTime", "commence_time", "gameTimeUtc"))
            for item in rows
        ):
            return rows
        for item in payload[:30]:
            found = _walk_event_lists(item, depth + 1)
            if found:
                return found
        return []
    if isinstance(payload, dict):
        for key in ("events", "games", "matches", "fixtures", "schedule", "scoreboard"):
            if key in payload:
                found = _walk_event_lists(payload[key], depth + 1)
                if found:
                    return found
        for key in ("data", "result", "response", "payload"):
            if key in payload:
                found = _walk_event_lists(payload[key], depth + 1)
                if found:
                    return found
    return []


def _skills_team_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("displayName", "display_name", "fullName", "full_name", "name", "shortName", "abbreviation"):
            if value.get(key):
                return str(value[key])
    return str(value or "").strip()


def _skills_event_to_espn_shape(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Adapt common SDK scoreboard forms without inventing missing fields."""
    if raw.get("competitions") and raw.get("date"):
        return raw
    event_id = raw.get("id") or raw.get("event_id") or raw.get("game_id") or raw.get("match_id")
    start = (raw.get("date") or raw.get("start") or raw.get("start_time") or raw.get("startTime")
             or raw.get("commence_time") or raw.get("gameTimeUtc") or raw.get("start_date"))
    if not event_id or not start:
        return None
    home = raw.get("homeTeam") or raw.get("home_team") or raw.get("home")
    away = raw.get("awayTeam") or raw.get("away_team") or raw.get("away")
    competitors = raw.get("competitors") or raw.get("participants")
    if isinstance(competitors, list):
        sides: Dict[str, Dict[str, Any]] = {}
        for i, item in enumerate(competitors):
            if not isinstance(item, dict):
                continue
            side = str(item.get("homeAway") or item.get("side") or item.get("role") or "").lower()
            if side not in ("home", "away") and len(competitors) == 2:
                side = "home" if i == 0 else "away"
            if side in ("home", "away"):
                sides[side] = item
        home = home or sides.get("home")
        away = away or sides.get("away")
    home_name, away_name = _skills_team_name(home), _skills_team_name(away)
    if not home_name or not away_name:
        participants = raw.get("players") or raw.get("teams")
        if isinstance(participants, list) and len(participants) >= 2:
            home_name = home_name or _skills_team_name(participants[0])
            away_name = away_name or _skills_team_name(participants[1])
    if not home_name or not away_name:
        title = str(raw.get("name") or raw.get("title") or raw.get("shortName") or "")
        parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", title, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            home_name = home_name or parts[0].strip()
            away_name = away_name or parts[1].strip()
    if not home_name or not away_name:
        return None
    status = raw.get("status")
    if isinstance(status, str):
        low = status.lower()
        state = "in" if any(term in low for term in ("live", "in progress", "in_progress")) else (
            "post" if any(term in low for term in ("final", "completed", "finished")) else "pre")
        status = {"type": {"state": state, "completed": state == "post", "detail": status}}
    return {
        "id": str(event_id), "date": str(start), "name": str(raw.get("name") or raw.get("title") or f"{home_name} vs {away_name}"),
        "shortName": str(raw.get("shortName") or raw.get("title") or ""),
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": home_name}, "score": raw.get("home_score", raw.get("homeScore"))},
            {"homeAway": "away", "team": {"displayName": away_name}, "score": raw.get("away_score", raw.get("awayScore"))},
        ], "status": status or {}}],
        "status": status or {}, "links": raw.get("links", []),
    }


def _skills_league_matches(raw: Dict[str, Any], league: Dict[str, str]) -> bool:
    """Soccer daily schedules can include many leagues; require explicit league metadata."""
    if league.get("sport") != "soccer":
        return True
    wanted_by_id = {
        "eng.1": ("premier league", "eng.1", "epl"),
        "esp.1": ("la liga", "esp.1", "laliga"),
        "ita.1": ("serie a", "ita.1", "seriea"),
        "ger.1": ("bundesliga", "ger.1"),
        "fra.1": ("ligue 1", "fra.1", "ligue1"),
        "tur.1": ("super lig", "süper lig", "tur.1"),
        "uefa.champions": ("champions league", "uefa.champions", "ucl"),
        "uefa.europa": ("europa league", "uefa.europa"),
        "uefa.europa.conf": ("conference league", "uefa.europa.conf"),
        "eng.fa": ("fa cup", "eng.fa"),
        "esp.copa_del_rey": ("copa del rey", "esp.copa_del_rey"),
        "ned.1": ("eredivisie", "ned.1"), "por.1": ("primeira liga", "por.1"),
        "bel.1": ("pro league", "bel.1"), "mex.1": ("liga mx", "mex.1"),
        "bra.1": ("brasileirao", "brasileirão", "bra.1"),
        "sco.1": ("scottish premiership", "sco.1"),
        "rus.1": ("russian premier league", "rus.1"),
    }
    refs: List[str] = []
    for key in ("league", "competition", "tournament", "league_name", "competition_name", "slug",
                "league_id", "competition_id", "leagueSlug", "competitionSlug", "competitionId", "leagueId"):
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("slug") or value.get("id")
        if value is not None:
            refs.append(re.sub(r"[^a-z0-9]+", "", str(value).lower()))
    competitions = raw.get("competitions")
    if isinstance(competitions, list):
        for item in competitions[:5]:
            if isinstance(item, dict):
                for key in ("league", "competition", "tournament"):
                    value = item.get(key)
                    if isinstance(value, dict):
                        value = value.get("name") or value.get("slug") or value.get("id")
                    if value is not None:
                        refs.append(re.sub(r"[^a-z0-9]+", "", str(value).lower()))
    wanted = [re.sub(r"[^a-z0-9]+", "", name.lower()) for name in wanted_by_id.get(league["id"], (league["name"], league["id"]))]
    return any(ref == option or (len(option) >= 6 and option in ref) for ref in refs for option in wanted)


class SportsDataProvider:
    def __init__(self, logger: Callable[[str], None]):
        self.logger = logger
        self._events: List[Dict[str, Any]] = []
        self._received_at: Optional[float] = None
        self._successful_leagues: int = 0
        self._source_counts: Dict[str, int] = {"ESPN": 0, "sports-skills": 0}
        self._failed_leagues: List[str] = []
        self._news_cache: Dict[str, Any] = {}
        self._summary_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._skills_schedule_cache: Dict[str, Any] = {}
        self._skills_unavailable = False
        self._lock = threading.RLock()

    def _sports_skills_scoreboard(self, league: Dict[str, str], now: dt.datetime,
                                  received_at: str) -> Tuple[List[Dict[str, Any]], bool]:
        """Best-effort SDK fallback, used only when the matching ESPN scoreboard fails."""
        if self._skills_unavailable:
            return [], False
        try:
            from sports_skills import football, nba, nhl, tennis, wnba
            sport, league_id = league["sport"], league["id"]
            soccer_cache_key = now.strftime("%Y-%m-%d") if sport == "soccer" else None
            if sport == "soccer":
                function = getattr(football, "get_daily_schedule", None)
                kwargs = {"date": now.strftime("%Y-%m-%d")}
            elif sport == "basketball" and league_id == "nba":
                function = getattr(nba, "get_scoreboard", None)
                kwargs = {"date": now.strftime("%Y-%m-%d")}
            elif sport == "basketball" and league_id == "wnba":
                function = getattr(wnba, "get_scoreboard", None)
                kwargs = {"date": now.strftime("%Y-%m-%d")}
            elif sport == "hockey":
                function = getattr(nhl, "get_scoreboard", None)
                kwargs = {"date": now.strftime("%Y-%m-%d")}
            elif sport == "tennis":
                function = getattr(tennis, "get_scoreboard", None)
                kwargs = {"tour": league_id, "date": now.strftime("%Y-%m-%d")}
            else:
                return [], False
            if not callable(function):
                return [], False
            if soccer_cache_key is not None:
                with self._lock:
                    cached = soccer_cache_key in self._skills_schedule_cache
                    payload = self._skills_schedule_cache.get(soccer_cache_key)
                if not cached:
                    payload = _bounded_library_call(function, timeout=9.0, **kwargs)
                    with self._lock:
                        self._skills_schedule_cache[soccer_cache_key] = payload
            else:
                payload = _bounded_library_call(function, timeout=9.0, **kwargs)
            if not isinstance(payload, dict):
                return [], False
            # The SDK uses {data: {events: [...]}}. Soccer's daily schedule may include
            # multiple competitions, so require explicit and matching league metadata.
            rows = _walk_event_lists(payload)
            normalized: List[Dict[str, Any]] = []
            for raw in rows:
                if not _skills_league_matches(raw, league):
                    continue
                adapted = _skills_event_to_espn_shape(raw)
                if not adapted:
                    continue
                event = _normalise_event(adapted, league, received_at)
                if event and event["start_dt"].date() == now.date():
                    event["source"] = "sports-skills"
                    event["source_received_at"] = received_at
                    normalized.append(event)
            # An empty, valid array still means the provider answered; it does not prove
            # there are no matches overall, which is retained in failure/source metadata.
            status = payload.get("status")
            valid_envelope = isinstance(payload.get("data"), (dict, list)) or isinstance(payload.get("events"), list) or status is True
            if valid_envelope:
                return normalized, True
            return [], False
        except ImportError as exc:
            self._skills_unavailable = True
            _log(self.logger, f"sports-skills unavailable ({type(exc).__name__}); ESPN-only fallback remains active")
            return [], False
        except Exception as exc:
            _log(self.logger, f"sports-skills scoreboard fallback failed for {league['code']} ({type(exc).__name__})")
            return [], False

    def refresh_today(self, now: Optional[dt.datetime] = None) -> Tuple[List[Dict[str, Any]], int, List[str]]:
        now = now or _now()
        date_string = now.strftime("%Y%m%d")
        events: List[Dict[str, Any]] = []
        failures: List[str] = []
        success_count = 0
        valid_count = 0
        received_at = dt.datetime.now(dt.timezone.utc).isoformat()
        skills_count = 0
        for league in LEAGUES:
            url = f"{ESPN_BASE}/{league['sport']}/{league['id']}/scoreboard"
            data = _json_get(url, params={"dates": date_string})
            if isinstance(data, dict) and isinstance(data.get("events"), list):
                success_count += 1
                valid_count += 1
                for raw in data.get("events", []):
                    if isinstance(raw, dict):
                        event = _normalise_event(raw, league, received_at)
                        if event and event["start_dt"].date() == now.date():
                            events.append(event)
                continue
            fallback_events, fallback_ok = self._sports_skills_scoreboard(league, now, received_at)
            if fallback_ok:
                skills_count += 1
                valid_count += 1
                events.extend(fallback_events)
            else:
                failures.append(f"{league['code']}: ESPN and sports-skills unavailable/invalid")
        # Deduplicate by event id and keep source timestamp as provenance.
        unique: Dict[str, Dict[str, Any]] = {}
        for event in events:
            unique.setdefault(event["id"], event)
        events = sorted(unique.values(), key=lambda item: item["start_dt"])
        with self._lock:
            self._events = events
            self._received_at = time.time()
            self._successful_leagues = valid_count
            self._source_counts = {"ESPN": success_count, "sports-skills": skills_count}
            self._failed_leagues = failures
        _log(self.logger, f"Scoreboard refresh: {len(events)} event(s); ESPN valid={success_count}, sports-skills fallback={skills_count}, unresolved={len(failures)}")
        return copy.deepcopy(events), valid_count, failures

    def snapshot_events(self) -> Tuple[List[Dict[str, Any]], int, List[str], bool]:
        with self._lock:
            stale = self._received_at is None or (time.time() - self._received_at > CACHE_TTL_SECONDS)
            return copy.deepcopy(self._events), self._successful_leagues, list(self._failed_leagues), stale

    def summary(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = event["id"]
        with self._lock:
            cached = self._summary_cache.get(event_id)
            if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
                return copy.deepcopy(cached[1])
        url = f"{ESPN_BASE}/{event['sport']}/{event['league_id']}/summary"
        data = _json_get(url, params={"event": event_id})
        source = "ESPN summary"
        if not isinstance(data, dict):
            data = self._sports_skills_summary(event)
            source = "sports-skills summary fallback"
        if not isinstance(data, dict):
            return {"available": False, "source": source, "received_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        allowed = ("boxscore", "statistics", "injuries", "rosters", "leaders", "odds", "pickcenter", "lastFiveGames", "seasonseries", "news", "header", "standings", "event", "game", "summary", "stats", "teams", "players", "scoreboard")
        candidate = data.get("data") if isinstance(data.get("data"), dict) else data
        reduced = {key: candidate[key] for key in allowed if key in candidate}
        if not reduced and source == "ESPN summary":
            fallback = self._sports_skills_summary(event)
            if isinstance(fallback, dict) and fallback:
                candidate = fallback.get("data") if isinstance(fallback.get("data"), dict) else fallback
                reduced = {key: candidate[key] for key in allowed if key in candidate}
                if not reduced:
                    reduced = candidate
                source = "sports-skills summary fallback"
        if not reduced and candidate:
            # Keep a bounded payload from the optional SDK rather than silently
            # interpreting an unknown response shape as valid stats.
            reduced = candidate if source != "ESPN summary" else {}
        result = {
            "available": bool(reduced),
            "data": _trim_json(reduced, max_depth=6, max_items=150),
            "source": source,
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        with self._lock:
            self._summary_cache[event_id] = (time.time(), result)
        return copy.deepcopy(result)

    def _sports_skills_summary(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Load game details only for a missing ESPN summary; ESPN remains authoritative."""
        if self._skills_unavailable:
            return None
        try:
            from sports_skills import football, nba, nhl, wnba
            if event.get("sport") == "soccer":
                function = getattr(football, "get_event_summary", None)
            elif event.get("sport") == "basketball" and event.get("league_id") == "nba":
                function = getattr(nba, "get_game_summary", None)
            elif event.get("sport") == "basketball" and event.get("league_id") == "wnba":
                function = getattr(wnba, "get_game_summary", None)
            elif event.get("sport") == "hockey":
                function = getattr(nhl, "get_game_summary", None)
            else:
                return None
            if not callable(function):
                return None
            payload = _bounded_library_call(function, timeout=9.0, event_id=str(event["id"]))
            if not isinstance(payload, dict):
                return None
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if not isinstance(data, dict) or not data:
                return None
            # Check explicit ids when available; do not attach a different event's summary.
            candidate_id = data.get("id") or data.get("event_id") or data.get("game_id")
            nested_event = data.get("event") if isinstance(data.get("event"), dict) else {}
            candidate_id = candidate_id or nested_event.get("id") or nested_event.get("event_id")
            if candidate_id and str(candidate_id) != str(event["id"]):
                return None
            return data
        except ImportError as exc:
            self._skills_unavailable = True
            _log(self.logger, f"sports-skills unavailable for summaries ({type(exc).__name__})")
            return None
        except Exception as exc:
            _log(self.logger, f"sports-skills summary fallback failed for event {event.get('id')} ({type(exc).__name__})")
            return None

    def news(self) -> List[Dict[str, str]]:
        """Small, bounded news pass; failures never impact scoreboard refresh."""
        news_leagues = [
            {"sport": "soccer", "id": "eng.1", "name": "EPL"},
            {"sport": "basketball", "id": "nba", "name": "NBA"},
            {"sport": "hockey", "id": "nhl", "name": "NHL"},
        ]
        found: List[Dict[str, str]] = []
        for league in news_leagues:
            cache_key = f"{league['sport']}/{league['id']}"
            cached = self._news_cache.get(cache_key)
            if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
                found.extend(cached[1])
                continue
            url = f"{ESPN_BASE}/{league['sport']}/{league['id']}/news"
            data = _json_get(url, params={"limit": 4})
            items: List[Dict[str, str]] = []
            articles = data.get("articles", []) if isinstance(data, dict) else []
            if isinstance(articles, list):
                for article in articles[:4]:
                    if not isinstance(article, dict):
                        continue
                    headline = article.get("headline") or article.get("title")
                    links = article.get("links") or {}
                    web = links.get("web") or {}
                    href = web.get("href") or article.get("link") or ""
                    published = article.get("published") or article.get("lastModified") or ""
                    if headline:
                        items.append({"league": league["name"], "headline": str(headline)[:240], "url": str(href)[:500], "published": str(published)[:50]})
            self._news_cache[cache_key] = (time.time(), items)
            found.extend(items)
        return found[:8]


def _trim_json(value: Any, max_depth: int = 6, max_items: int = 150, _depth: int = 0) -> Any:
    if _depth >= max_depth:
        return "[depth-limited]"
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                output["_truncated"] = True
                break
            output[str(key)[:100]] = _trim_json(item, max_depth, max_items, _depth + 1)
        return output
    if isinstance(value, list):
        return [_trim_json(item, max_depth, max_items, _depth + 1) for item in value[:max_items]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)[:500] if isinstance(value, str) else value
    return str(value)[:200]


def _flatten_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Extract evidence only from keys actually returned by ESPN."""
    if not summary.get("available"):
        return {}
    data = summary.get("data") or {}
    wanted = {"lastFiveGames", "injuries", "leaders", "boxscore", "statistics", "standings", "seasonseries", "rosters"}
    result: Dict[str, Any] = {}
    def walk(obj: Any, path: str = "", depth: int = 0) -> None:
        if depth > 5 or len(result) >= 80:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                child = f"{path}.{key}" if path else str(key)
                if key in wanted and value not in (None, {}, [], ""):
                    result[child] = _trim_json(value, max_depth=4, max_items=35)
                if isinstance(value, (dict, list)):
                    walk(value, child, depth + 1)
        elif isinstance(obj, list):
            for i, value in enumerate(obj[:25]):
                if isinstance(value, (dict, list)):
                    walk(value, f"{path}[{i}]", depth + 1)
    walk(data)
    return result


class OddsProvider:
    def __init__(self, logger: Callable[[str], None]):
        self.logger = logger
        self.key = os.getenv("THE_ODDS_API_KEY", "").strip()
        self._sports: Optional[List[Dict[str, Any]]] = None
        self._sports_at = 0.0
        self._odds_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = threading.RLock()

    def _active_sports(self) -> List[Dict[str, Any]]:
        with self._lock:
            ttl = 12 * 60 * 60 if self._sports else 5 * 60
            if self._sports is not None and time.time() - self._sports_at < ttl:
                return copy.deepcopy(self._sports)
        if not self.key:
            return []
        # Sports endpoint does not consume usage credits, per current v4 docs.
        data = _json_get("https://api.the-odds-api.com/v4/sports", params={"apiKey": self.key}, timeout=(3.05, 7), retries=1)
        if not isinstance(data, list):
            with self._lock:
                self._sports = []
                self._sports_at = time.time()
            _log(self.logger, "The Odds API sport catalogue unavailable; retry suppressed for five minutes")
            return []
        valid = [item for item in data if isinstance(item, dict) and item.get("active") is True and item.get("key")]
        with self._lock:
            self._sports = valid
            self._sports_at = time.time()
        return copy.deepcopy(valid)

    def _sport_key(self, event: Dict[str, Any]) -> Optional[str]:
        active = self._active_sports()
        by_key = {item.get("key"): item for item in active}
        for candidate in ODDS_SPORT_CANDIDATES.get(event["league_id"], ()):
            if candidate in by_key:
                return candidate
        if event["sport"] == "tennis":
            prefix = "tennis_atp_" if event["league_id"] == "atp" else "tennis_wta_"
            candidates = sorted([item["key"] for item in active if str(item.get("key", "")).startswith(prefix)])
            # Limit quota exposure. The event matcher below still validates tournament, players and time.
            return candidates[0] if len(candidates) == 1 else None
        return None

    def odds_for_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.key:
            return []
        sport_key = self._sport_key(event)
        if not sport_key:
            return []
        with self._lock:
            cached = self._odds_cache.get(sport_key)
            if cached and time.time() - cached[0] < ODDS_CACHE_TTL_SECONDS:
                rows = copy.deepcopy(cached[1])
            else:
                rows = []
                params = {"apiKey": self.key, "regions": os.getenv("THE_ODDS_API_REGIONS", "eu,uk"),
                          "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"}
                try:
                    response = requests.get(
                        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                        params=params, headers={"User-Agent": "VochinoAnalyzer/1.0", "Accept": "application/json"},
                        timeout=(3.05, 8.0),
                    )
                    if response.status_code == 200:
                        parsed = response.json()
                        if isinstance(parsed, list):
                            rows = [item for item in parsed if isinstance(item, dict)]
                    # Never retry quota or authorization errors automatically.
                except (requests.RequestException, ValueError, TypeError):
                    rows = []
                self._odds_cache[sport_key] = (time.time(), rows)
        return self._match_event_odds(event, rows)

    def _match_event_odds(self, event: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        start = event["start_dt"]
        matched: List[Dict[str, Any]] = []
        for row in rows:
            if not (_same_side(event.get("home", ""), str(row.get("home_team", ""))) and
                    _same_side(event.get("away", ""), str(row.get("away_team", "")))):
                continue
            row_start = _parse_time(row.get("commence_time"))
            if not row_start or abs((row_start - start).total_seconds()) > 2 * 60 * 60:
                continue
            books = []
            for bookmaker in row.get("bookmakers", []) if isinstance(row.get("bookmakers"), list) else []:
                updated = _parse_time(bookmaker.get("last_update"))
                # Ignore snapshots older than 2 hours; upcoming-market odds need freshness.
                if not updated or abs((_now() - updated).total_seconds()) > 2 * 60 * 60:
                    continue
                for market in bookmaker.get("markets", []) if isinstance(bookmaker.get("markets"), list) else []:
                    if market.get("key") != "h2h":
                        continue
                    outcomes = []
                    for outcome in market.get("outcomes", []) if isinstance(market.get("outcomes"), list) else []:
                        try:
                            price = float(outcome.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if price > 1.0 and price < 1000:
                            outcomes.append({"name": str(outcome.get("name", "")), "price": price})
                    if len(outcomes) >= 2:
                        books.append({"bookmaker": str(bookmaker.get("title") or bookmaker.get("key") or "unknown"),
                                      "updated_at": updated.isoformat() if updated else str(bookmaker.get("last_update") or "unknown"),
                                      "outcomes": outcomes})
            if books:
                matched.append({"source": "The Odds API", "event_id": str(row.get("id", "")),
                                "commence_time": row_start.isoformat(), "bookmakers": books,
                                "received_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        return matched[:1]


def _betbetter_signal(event: Dict[str, Any], logger: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """Optional corroborating model signal. fairOdds is never treated as bookmaker odds."""
    enabled = os.getenv("BETBETTER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    league_code = event.get("league_id")
    mapping = {
        "eng.1": ("epl", "premier_league"), "esp.1": ("laliga", "la_liga"),
        "ita.1": ("seriea", "serie_a"), "ger.1": ("bundesliga",), "fra.1": ("ligue1",),
        "nba": ("nba",), "wnba": ("wnba",), "nhl": ("nhl",), "atp": ("atp",), "wta": ("wta",),
    }
    try:
        import betbetter  # optional; package documentation exposes get_picks(sport)
        getter = getattr(betbetter, "get_picks", None)
        if not callable(getter):
            return None
        for code in mapping.get(league_code, ()):
            result_box: List[Any] = []
            done = threading.Event()
            def worker() -> None:
                try:
                    result_box.append(getter(code))
                except Exception:
                    result_box.append(None)
                finally:
                    done.set()
            threading.Thread(target=worker, daemon=True, name="betbetter-read").start()
            if not done.wait(6.0):
                _log(logger, "Bet Better lookup timed out; signal ignored")
                return None
            feed = result_box[0] if result_box else None
            picks = feed.get("picks", []) if isinstance(feed, dict) else []
            for pick in picks if isinstance(picks, list) else []:
                if not isinstance(pick, dict):
                    continue
                game = str(pick.get("game", ""))
                if not (_same_side(event.get("home", ""), game) and _same_side(event.get("away", ""), game)):
                    # Game strings can have the order home-away or away-home.
                    both = _normalise_name(event.get("home", "")) in _normalise_name(game) and _normalise_name(event.get("away", "")) in _normalise_name(game)
                    if not both:
                        continue
                pick_start = _parse_time(pick.get("gameTimeUtc"))
                if pick_start and abs((pick_start - event["start_dt"]).total_seconds()) > 4 * 60 * 60:
                    continue
                probability = pick.get("modelProbabilityPct")
                try:
                    probability = float(probability)
                    if not 0 < probability < 100:
                        probability = None
                except (TypeError, ValueError):
                    probability = None
                return {"source": "Bet Better", "game": game,
                        "market": str(pick.get("market", "")), "selection": str(pick.get("selection", "")),
                        "model_probability_pct": probability,
                        "fair_odds": pick.get("fairOdds"), "published_at": pick.get("updatedAt") or pick.get("publishedAt")}
    except Exception as exc:
        _log(logger, f"Bet Better signal unavailable ({type(exc).__name__}); ESPN remains primary")
    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(candidate[start:end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


def _analysis_prompt(event: Dict[str, Any], summary: Dict[str, Any], odds: List[Dict[str, Any]], signal: Optional[Dict[str, Any]]) -> str:
    factual = {
        "event": {k: event.get(k) for k in ("id", "sport", "league", "home", "away", "start", "status", "home_score", "away_score")},
        "summary": summary.get("data", {}) if summary.get("available") else None,
        "summary_source_received_at": summary.get("received_at"),
        "odds": odds,
        "betbetter_model_signal_not_bookmaker_odds": signal,
    }
    return (
        "Analyze only the supplied JSON data. Do not browse or use memory. Never invent injuries, form, stats, odds, or probability. "
        "The supplied Bet Better fair odds are model fair odds, NOT bookmaker prices. Real bookmaker odds are only in the odds array. "
        "Return ONE JSON object with keys selection, probability_pct, rationale, evidence_fields, insufficient_data. "
        "selection must exactly match one outcome name from a bookmaker h2h odds list or be null. "
        "probability_pct must be a conservative model estimate 0-100, or null when data does not support an estimate. "
        "evidence_fields must list exact keys/paths present in summary; otherwise empty. Set insufficient_data=true if at least two relevant, independent factual signals are absent. "
        "Do not claim certainty. Keep rationale under 450 characters and in Persian.\nDATA_JSON:\n" + json.dumps(factual, ensure_ascii=False, separators=(",", ":"))[:24000]
    )


def _call_gemini(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-4-argon").strip() or "gemini-4-argon"
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=AI_TIMEOUT_SECONDS * 1000))
        response = client.models.generate_content(model=model, contents=prompt)
        return _extract_json_object(getattr(response, "text", "") or "")
    except Exception as exc:
        _log(logger or (lambda _message: None), f"Gemini request failed ({type(exc).__name__})")
        return None


def _call_groq(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
    try:
        from groq import Groq
        client = Groq(api_key=key, timeout=AI_TIMEOUT_SECONDS, max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": "You are an independent evidence auditor. Return only valid JSON."},
                      {"role": "user", "content": prompt}],
            temperature=0.1,
            max_completion_tokens=800,
        )
        text = response.choices[0].message.content if response.choices else ""
        return _extract_json_object(text or "")
    except Exception as exc:
        _log(logger or (lambda _message: None), f"Groq request failed ({type(exc).__name__})")
        return None


def _call_openrouter(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1:free").strip() or "deepseek/deepseek-r1:free"
    try:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "X-Title": "Vochino Sports Analyzer"}
        referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json={"model": model, "messages": [
                {"role": "system", "content": "Independently re-evaluate evidence; return one JSON object only."},
                {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 900},
            timeout=AI_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            _log(logger or (lambda _message: None), f"OpenRouter HTTP {response.status_code}; DeepSeek result unavailable")
            return None
        data = response.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _extract_json_object(text)
    except (requests.RequestException, ValueError, TypeError, IndexError, KeyError) as exc:
        _log(logger or (lambda _message: None), f"OpenRouter request failed ({type(exc).__name__})")
        return None


def _valid_ai_answer(answer: Optional[Dict[str, Any]], valid_outcomes: Sequence[str], available_paths: Sequence[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(answer, dict) or answer.get("insufficient_data") is True:
        return None
    selection = str(answer.get("selection") or "").strip()
    match = next((outcome for outcome in valid_outcomes if _same_side(selection, outcome)), None)
    if not match:
        return None
    try:
        probability = float(answer.get("probability_pct"))
        if not 1 <= probability <= 99:
            return None
    except (TypeError, ValueError):
        return None
    evidence = answer.get("evidence_fields")
    if not isinstance(evidence, list):
        return None
    matched_evidence = []
    for item in evidence:
        candidate = str(item)
        if any(candidate == path or candidate in path or path in candidate for path in available_paths):
            matched_evidence.append(candidate[:120])
    evidence_groups = {item.split(".", 1)[0].split("[", 1)[0] for item in matched_evidence}
    if len(set(matched_evidence)) < 2 or len(evidence_groups) < 2:
        return None
    rationale = str(answer.get("rationale") or "").strip()[:450]
    return {"selection": match, "probability_pct": probability, "rationale": rationale, "evidence_fields": list(dict.fromkeys(matched_evidence))}


def _best_market(odds: List[Dict[str, Any]]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    if not odds:
        return [], None
    books = odds[0].get("bookmakers", [])
    if not books:
        return [], None
    # Prefer the most recently updated bookmaker where freshness is established.
    books = sorted(books, key=lambda b: str(b.get("updated_at", "")), reverse=True)
    chosen = books[0]
    outcomes = chosen.get("outcomes", [])
    return [str(item.get("name", "")) for item in outcomes if item.get("name")], chosen


def _american_to_decimal(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return None
    result = 1.0 + (number / 100.0 if number > 0 else 100.0 / abs(number))
    return result if 1.0 < result < 1000.0 else None


def _espn_odds_for_event(event: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept ESPN odds only when actual outcome prices can be parsed; never use model fair odds."""
    if not summary.get("available"):
        return []
    data = summary.get("data") or {}
    candidates: List[Dict[str, Any]] = []
    def collect(obj: Any, depth: int = 0) -> None:
        if depth > 5 or len(candidates) > 40:
            return
        if isinstance(obj, dict):
            if any(key in obj for key in ("homeTeamOdds", "awayTeamOdds", "drawOdds", "outcomes")):
                candidates.append(obj)
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    collect(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj[:30]:
                if isinstance(value, (dict, list)):
                    collect(value, depth + 1)
    collect(data.get("odds"))
    collect(data.get("pickcenter"))
    for row in candidates:
        outcomes: List[Dict[str, Any]] = []
        if isinstance(row.get("outcomes"), list):
            for item in row["outcomes"]:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("label")
                price = item.get("decimalOdds") or item.get("decimal") or item.get("price")
                if price is None:
                    price = _american_to_decimal(item.get("odds") or item.get("moneyLine"))
                try:
                    price_float = float(price)
                except (TypeError, ValueError):
                    continue
                if name and 1 < price_float < 1000:
                    outcomes.append({"name": str(name), "price": price_float})
        if not outcomes:
            for side, name in (("homeTeamOdds", event.get("home", "")), ("awayTeamOdds", event.get("away", ""))):
                side_odds = row.get(side)
                if not isinstance(side_odds, dict):
                    continue
                price = side_odds.get("decimalOdds") or side_odds.get("decimal")
                if price is None:
                    price = _american_to_decimal(side_odds.get("moneyLine") or side_odds.get("moneyline"))
                try:
                    price_float = float(price)
                except (TypeError, ValueError):
                    continue
                if name and 1 < price_float < 1000:
                    outcomes.append({"name": str(name), "price": price_float})
            draw = row.get("drawOdds")
            if isinstance(draw, dict):
                draw_name = draw.get("name") or "Draw"
                draw_price = draw.get("decimalOdds") or draw.get("decimal")
                if draw_price is None:
                    draw_price = _american_to_decimal(draw.get("moneyLine") or draw.get("moneyline"))
                try:
                    draw_float = float(draw_price)
                except (TypeError, ValueError):
                    draw_float = None
                if draw_float and 1 < draw_float < 1000:
                    outcomes.append({"name": str(draw_name), "price": draw_float})
        # Never treat two-outcome prices as a football 1X2 market.
        if event.get("sport") == "soccer" and len(outcomes) < 3:
            continue
        # Events with missing sides cannot be reliably joined to a market.
        if len(outcomes) < 2:
            continue
        provider = row.get("provider") or row.get("bookmaker") or {}
        if isinstance(provider, dict):
            book_name = provider.get("name") or provider.get("displayName") or provider.get("key") or "ESPN odds provider"
        else:
            book_name = str(provider or "ESPN odds provider")
        odds_updated = row.get("lastUpdated") or row.get("last_update") or row.get("updatedAt") or row.get("lastUpdate")
        return [{"source": "ESPN", "event_id": event["id"], "commence_time": event["start"],
                 "bookmakers": [{"bookmaker": str(book_name), "updated_at": str(odds_updated) if odds_updated else "زمان به‌روزرسانی در پاسخ ESPN اعلام نشده", "outcomes": outcomes}],
                 "received_at": summary.get("received_at") or dt.datetime.now(dt.timezone.utc).isoformat()}]
    return []


def _consensus_analysis(event: Dict[str, Any], summary: Dict[str, Any], odds: List[Dict[str, Any]], logger: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    paths = list(_flatten_summary(summary).keys())
    # Odds alone or simple recent form alone is not enough for a recommendation.
    if len(paths) < 2 or not odds:
        return None
    outcomes, chosen_market = _best_market(odds)
    if len(outcomes) < 2 or not chosen_market:
        return None
    signal = _betbetter_signal(event, logger)
    prompt = _analysis_prompt(event, summary, odds, signal)
    gemini = _call_gemini(prompt, logger)
    groq = _call_groq(prompt, logger)
    if not gemini or not groq:
        _log(logger, f"AI analysis unavailable for event {event['id']}: both Gemini and Groq are required")
        return None
    g = _valid_ai_answer(gemini, outcomes, paths)
    r = _valid_ai_answer(groq, outcomes, paths)
    if not g or not r:
        return None
    same = _normalise_name(g["selection"]) == _normalise_name(r["selection"])
    probability_gap = abs(g["probability_pct"] - r["probability_pct"])
    if not same or probability_gap >= AI_DISAGREEMENT_THRESHOLD:
        deepseek = _call_openrouter(prompt, logger)
        d = _valid_ai_answer(deepseek, outcomes, paths)
        if not d:
            _log(logger, f"AI disagreement and no valid DeepSeek decision for event {event['id']}")
            return None
        # Third engine must agree with one of the independent first two and be close on probability.
        matches_g = _normalise_name(d["selection"]) == _normalise_name(g["selection"]) and abs(d["probability_pct"] - g["probability_pct"]) < AI_DISAGREEMENT_THRESHOLD
        matches_r = _normalise_name(d["selection"]) == _normalise_name(r["selection"]) and abs(d["probability_pct"] - r["probability_pct"]) < AI_DISAGREEMENT_THRESHOLD
        if matches_g:
            chosen = g
        elif matches_r:
            chosen = r
        else:
            return None
    else:
        chosen = {"selection": g["selection"],
                  "probability_pct": (g["probability_pct"] + r["probability_pct"]) / 2.0,
                  "rationale": g["rationale"] if len(g["rationale"]) >= len(r["rationale"]) else r["rationale"],
                  "evidence_fields": sorted(set(g["evidence_fields"] + r["evidence_fields"]))}
    # Compare estimated probability with a no-vig implied probability from one bookmaker.
    selected_price = None
    implied_probs = []
    for outcome in chosen_market.get("outcomes", []):
        try:
            price = float(outcome["price"])
            if price <= 1:
                continue
            implied_probs.append((outcome.get("name"), 1.0 / price))
            if _same_side(chosen["selection"], outcome.get("name", "")):
                selected_price = price
        except (TypeError, ValueError):
            continue
    total = sum(probability for _, probability in implied_probs)
    if not selected_price or total <= 0:
        return None
    no_vig = next((probability / total for name, probability in implied_probs if _same_side(chosen["selection"], str(name))), None)
    if no_vig is None or chosen["probability_pct"] / 100.0 < no_vig + 0.03:
        return None
    return {**chosen, "bookmaker": chosen_market.get("bookmaker"), "odds": selected_price,
            "odds_updated_at": chosen_market.get("updated_at"), "no_vig_probability": no_vig,
            "betbetter_signal": signal, "odds_source": odds[0].get("source", "unknown"),
            "source": "Gemini/Groq consensus"}


def _event_summary_line(event: Dict[str, Any]) -> str:
    start = event["start_dt"].strftime("%H:%M")
    teams = " - ".join(x for x in (event.get("home"), event.get("away")) if x)
    if not teams:
        teams = event.get("name") or "نام رویداد در منبع کامل نیست"
    score = ""
    if event.get("home_score") is not None or event.get("away_score") is not None:
        score = f" | {event.get('home_score', '—')} : {event.get('away_score', '—')}"
    return f"• {_safe_text(teams, 120)} | {_safe_text(event['league'], 80)} | {_fa_digits(start)} | {_safe_text(event['status'], 100)}{score}"


def _daily_brief(provider: SportsDataProvider, now: dt.datetime, snapshot: Optional[Dict[str, Any]] = None) -> str:
    events, successes, failures, stale = provider.snapshot_events()
    today_events = [event for event in events if event["start_dt"].date() == now.date()]
    upcoming = [event for event in today_events if event.get("state") not in ("post", "final", "in", "live") and event["start_dt"] >= now - dt.timedelta(hours=5)]
    live = [event for event in today_events if event.get("state") in ("in", "live")]
    completed = [event for event in today_events if event.get("state") in ("post", "final")]
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫ 〰╮", "📰 اخبار ورزشی روز", "╰〰💲 𓆩𓆩📊𓆪𓆪 💲〰╯",
             f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} استانبول", ""]
    if successes == 0:
        lines.append("⚠️ برنامه مسابقات از ESPN دریافت نشد؛ بنابراین درباره نبود مسابقه نتیجه‌گیری نمی‌شود.")
    else:
        if upcoming:
            lines.append("🏆 مسابقات باقی‌مانده امروز")
            lines.extend(_event_summary_line(event) for event in upcoming[:7])
        elif not stale:
            lines.append("📭 در پاسخ موفق لیگ‌های بررسی‌شده، مسابقه باقی‌مانده‌ای برای امروز ثبت نشده است؛ پوشش ESPN کامل یا تضمین‌شده نیست.")
        if live:
            lines.extend(["", "🔴 مسابقات زنده"])
            lines.extend(_event_summary_line(event) for event in live[:3])
        if completed:
            lines.extend(["", "✅ مسابقات تمام‌شده"])
            lines.extend(_event_summary_line(event) for event in completed[:3])
    news = provider.news()
    if news:
        lines.extend(["", "📌 خبرهای منتشرشده از ESPN"])
        for item in news[:3]:
            headline = _safe_text(item.get("headline"), 220)
            url = _safe_text(item.get("url"), 350)
            label = _safe_text(item.get("league"), 50)
            lines.append(f"• [{label}] {headline}" + (f"\n  {url}" if url else ""))
    if failures:
        lines.extend(["", f"ℹ️ {len(failures)} لیگ/endpoint در این نوبت پاسخ معتبر نداد؛ جزئیات در لاگ سرویس است."])
    prices = snapshot.get("ali1377", {}) if isinstance(snapshot, dict) and isinstance(snapshot.get("ali1377"), dict) else {}
    voucher_prices = []
    for key, label in (("u_buy", "خرید U"), ("u_sell", "فروش U"), ("premium_buy", "خرید Premium"), ("premium_sell", "فروش Premium")):
        value = prices.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            voucher_prices.append(f"{label}: {_fa_digits(format(value, ',.0f'))} تومان")
    if voucher_prices:
        lines.extend(["", "💰 قیمت‌های ووچر از snapshot داخلی STATE", " | ".join(voucher_prices)])
    else:
        lines.extend(["", "💰 قیمت ووچر در STATE فعلاً در دسترس نیست."])
    source_counts = getattr(provider, "_source_counts", {})
    sources_used = []
    if source_counts.get("ESPN", 0):
        sources_used.append("ESPN")
    if source_counts.get("sports-skills", 0):
        sources_used.append("sports-skills (fallback)")
    if not sources_used:
        sources_used.append("منابع بررسی‌شده؛ دریافت موفق تأیید نشد")
    lines.extend(["", "📚 منابع برنامه: " + "، ".join(sources_used) + " | ساعت‌ها به وقت Europe/Istanbul", "⚠️ این پیام برنامه و خبر است، نه پیشنهاد شرط‌بندی."])
    return "\n".join(lines)[:3900]


def _crypto_report(snapshot: Dict[str, Any], now: dt.datetime) -> str:
    ali = snapshot.get("ali1377") if isinstance(snapshot.get("ali1377"), dict) else {}
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    poster = meta.get("poster") if isinstance(meta.get("poster"), dict) else {}
    prices = poster.get("swapwallet_prices") if isinstance(poster.get("swapwallet_prices"), dict) else {}
    stamp = poster.get("swapwallet_last_success") or "زمان دقیق این snapshot مشخص نیست"
    age_hours: Optional[float] = None
    try:
        parsed_stamp = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed_stamp.tzinfo is None:
            parsed_stamp = parsed_stamp.replace(tzinfo=dt.timezone.utc)
        age_hours = (now.astimezone(dt.timezone.utc) - parsed_stamp.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
    except (ValueError, TypeError, OverflowError):
        pass
    current_labels = {
        "USDT/IRT": "تتر/تومان (منبع SwapWallet؛ مقدار خام من STATE)",
        "TRX/IRT": "ترون/تومان", "TON/IRT": "تون/تومان", "BTC/IRT": "بیت‌کوین/تومان",
        "ETH/IRT": "اتریوم/تومان", "FET/IRT": "FET/تومان", "PAXG/IRT": "PAXG/تومان", "XAUT/IRT": "XAUT/تومان",
    }
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫ 〰╮", "📊 تحلیل بازار کریپتو", "╰〰💲 𓆩𓆩📊𓆪𓆪 💲〰╯",
             f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} استانبول", "", "💰 قیمت‌های موجود در snapshot سرویس"]
    count = 0
    for key, label in current_labels.items():
        value = prices.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            lines.append(f"• {label}: {_fa_digits(f'{value:,.8f}'.rstrip('0').rstrip('.'))}")
            count += 1
    for key, label in (("tether_usd", "USDT/USD"), ("utopia_usd", "Utopia/USD")):
        value = ali.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            lines.append(f"• {label}: {_fa_digits(f'{value:.8f}'.rstrip('0').rstrip('.'))}")
            count += 1
    if not count:
        lines.append("در STATE هیچ قیمت معتبر قابل‌نمایشی پیدا نشد.")
    if age_hours is None:
        lines.append(f"🕒 زمان آخرین دریافت بازار: {_safe_text(stamp, 80)}")
    else:
        lines.append(f"🕒 زمان آخرین دریافت بازار: {_safe_text(stamp, 80)} (حدود {_fa_digits(f'{max(0, age_hours):.1f}')} ساعت قبل)")
        if age_hours > 24:
            lines.append("⚠️ داده قیمت بیش از ۲۴ ساعت عمر دارد؛ برای تصمیم فعلی مناسب نیست.")
    lines.extend(["", "🧠 جمع‌بندی", "در STATE تاریخچه معتبر قیمت برای محاسبه تغییر روزانه/هفتگی پیدا نشد؛ بنابراین روند صعودی/نزولی یا درصد تغییر ساخته نمی‌شود.",
                  "📌 نتیجه: صرفاً نمایش snapshot؛ پیشنهاد قطعی خرید/فروش صادر نمی‌شود.",
                  "📚 منبع: STATE داخلی سرویس و snapshot ذخیره‌شده SwapWallet؛ بدون درخواست قیمت جدید."])
    return "\n".join(lines)[:3900]


class AnalyzerLedger:
    """Small atomic ledger for idempotency; does not touch the app's price JSON."""
    def __init__(self, path: str, logger: Callable[[str], None]):
        self.path = Path(path)
        self.logger = logger
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = {"version": 1, "sent": {}, "attempts": {}, "qualified_picks": {}}
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                self.data.update(value)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            _log(self.logger, f"ledger read failed ({type(exc).__name__}); starting empty ledger")

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".analyzer-ledger-", suffix=".tmp", dir=str(self.path.parent), text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise

    def already_sent(self, key: str) -> bool:
        with self.lock:
            return key in self.data.setdefault("sent", {})

    def record_sent(self, key: str, record: Dict[str, Any]) -> None:
        with self.lock:
            self.data.setdefault("sent", {})[key] = {**record, "sent_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            self.save()

    def attempt_recently(self, event_id: str, now: dt.datetime, minutes: int = 15) -> bool:
        with self.lock:
            stamp = self.data.setdefault("attempts", {}).get(event_id)
            if not stamp:
                return False
            try:
                then = dt.datetime.fromisoformat(stamp)
                if then.tzinfo is None:
                    then = then.replace(tzinfo=dt.timezone.utc)
                return (now.astimezone(dt.timezone.utc) - then.astimezone(dt.timezone.utc)).total_seconds() < minutes * 60
            except (ValueError, TypeError):
                return False

    def record_attempt(self, event_id: str, now: dt.datetime) -> None:
        with self.lock:
            self.data.setdefault("attempts", {})[event_id] = now.astimezone(dt.timezone.utc).isoformat()
            self.save()

    def add_qualified_pick(self, day: str, item: Dict[str, Any]) -> None:
        with self.lock:
            picks = self.data.setdefault("qualified_picks", {}).setdefault(day, [])
            if not any(existing.get("event_id") == item.get("event_id") for existing in picks):
                picks.append(item)
                self.save()

    def qualified_picks(self, day: str) -> List[Dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.data.setdefault("qualified_picks", {}).get(day, []))


def _single_pick_message(event: Dict[str, Any], analysis: Dict[str, Any], now: dt.datetime) -> str:
    start = event["start_dt"].strftime("%H:%M")
    return "\n".join([
        "╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫ 〰╮", "تحلیل ورزشی امروز", "╰〰💲 𓆩𓆩📊𓆪𓆪 💲〰╯",
        f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))}",
        f"🏟 {_safe_text(event.get('home') or event.get('name'), 100)} 🆚 {_safe_text(event.get('away'), 100)}",
        f"🏆 {_safe_text(event.get('league'), 100)} | شروع {_fa_digits(start)} استانبول",
        "🎯 بازار: برنده مسابقه / h2h",
        f"📊 برآورد مدل: {_fa_digits(format(analysis['probability_pct'], '.1f'))}٪ (برآورد مدل، نه احتمال تضمین‌شده)",
        f"🧾 انتخاب: {_safe_text(analysis['selection'], 100)}",
        f"💹 ضریب واقعی: {_fa_digits(format(analysis['odds'], '.2f'))} | {_safe_text(analysis.get('bookmaker'), 80)}",
        f"🕒 آخرین به‌روزرسانی ضریب: {_safe_text(analysis.get('odds_updated_at'), 100)}",
        f"📋 نکات: {_safe_text(analysis.get('rationale'), 450) or 'جزئیات تحلیلی کافی برای توضیح متنی موجود نبود.'}",
        f"📚 منابع: {_safe_text(analysis.get('odds_source', 'منبع ضرایب نامشخص'), 80)}، ESPN، Gemini و Groq{', Bet Better (سیگنال مستقل مدل)' if analysis.get('betbetter_signal') else ''}",
        "⚠️ داده‌ها و تخمین مدل ممکن است خطا داشته باشند؛ هیچ نتیجه‌ای تضمین‌شده نیست.",
    ])[:3900]


def _maybe_single_analyses(provider: SportsDataProvider, odds_provider: OddsProvider,
                           ledger: AnalyzerLedger, send: Callable[[str], bool], logger: Callable[[str], None],
                           now: dt.datetime) -> None:
    if not (17 <= now.hour < 23):
        return
    sent_today = sum(1 for key, value in ledger.data.setdefault("sent", {}).items()
                     if key.startswith(now.date().isoformat() + ":single:") and isinstance(value, dict))
    if sent_today >= 5:
        return
    events, _, _, stale = provider.snapshot_events()
    if stale:
        return
    for event in events:
        if sent_today >= 5:
            break
        if event["start_dt"].date() != now.date() or event.get("state") not in ("pre", "scheduled", "unknown"):
            continue
        minutes_to_start = (event["start_dt"] - now).total_seconds() / 60.0
        if not 38 <= minutes_to_start <= 78:
            continue
        if ledger.already_sent(f"{now.date().isoformat()}:single:{event['id']}"):
            continue
        if ledger.attempt_recently(event["id"], now):
            continue
        ledger.record_attempt(event["id"], now)
        try:
            summary = provider.summary(event)
            odds = _espn_odds_for_event(event, summary)
            if not odds:
                odds = odds_provider.odds_for_event(event)
            analysis = _consensus_analysis(event, summary, odds, logger)
            if not analysis:
                continue
            message = _single_pick_message(event, analysis, now)
            if send(message):
                key = f"{now.date().isoformat()}:single:{event['id']}"
                record = {"event_id": event["id"], "selection": analysis["selection"], "odds": analysis["odds"],
                          "bookmaker": analysis["bookmaker"], "start": event["start"], "league": event["league"]}
                ledger.record_sent(key, record)
                ledger.add_qualified_pick(now.date().isoformat(), record)
                sent_today += 1
                _log(logger, f"single analysis sent for event {event['id']}")
        except Exception as exc:
            _log(logger, f"single analysis failed for event {event['id']} ({type(exc).__name__})")


def _maybe_daily_mix(ledger: AnalyzerLedger, send: Callable[[str], bool], logger: Callable[[str], None], now: dt.datetime) -> None:
    if not (20 <= now.hour < 23):
        return
    key = f"{now.date().isoformat()}:mix:daily"
    if ledger.already_sent(key) or ledger.attempt_recently(key, now):
        return
    picks = ledger.qualified_picks(now.date().isoformat())
    # A mix is never forced. No pick from model fair odds is accepted; records only hold bookmaker h2h odds.
    unique = []
    seen_events = set()
    for item in picks:
        try:
            price = float(item.get("odds"))
        except (TypeError, ValueError):
            continue
        if price <= 1.0 or not item.get("bookmaker") or not item.get("event_id") or item["event_id"] in seen_events:
            continue
        seen_events.add(item["event_id"])
        unique.append({**item, "odds": price})
    if not 4 <= len(unique) <= 7:
        return
    total = 1.0
    for item in unique:
        total *= item["odds"]
    if total < 2.0:
        return
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐 ➫ 〰╮", "🎯 میکس روزانه", "╰〰💲 𓆩𓆩🎯𓆪𓆪 💲〰╯", f"📅 {_fa_digits(_jalali_date(now))}", ""]
    for index, item in enumerate(unique, 1):
        odds_text = _fa_digits(format(item["odds"], ".2f"))
        lines.append(f"{_fa_digits(index)}. {_safe_text(item.get('league'), 80)} | {_safe_text(item.get('selection'), 100)} | ضریب {odds_text} | {_safe_text(item.get('bookmaker'), 80)}")
    lines.extend(["", f"🎟 ضریب کل محاسبه‌شده: {_fa_digits(f'{total:.2f}')} ",
                  "📚 ضرایب از بازار واقعی ثبت‌شده‌اند؛ انتخاب‌ها و رویدادها تضمین‌شده نیستند."])
    ledger.record_attempt(key, now)
    if send("\n".join(lines)[:3900]):
        ledger.record_sent(key, {"count": len(unique), "total_odds": round(total, 4), "event_ids": sorted(seen_events)})
        _log(logger, "daily mix sent")


def _crypto_due(now: dt.datetime) -> Optional[str]:
    # One fixed run in each requested window, local to Europe/Istanbul.
    if now.hour == 12 and now.minute >= 30:
        return "midday"
    if now.hour == 0 and now.minute >= 30:
        return "overnight"
    return None


def _sports_daily_due(now: dt.datetime) -> bool:
    return now.hour >= 16


def _in_silent_period(now: dt.datetime) -> bool:
    return 1 <= now.hour < 10


def _acquire_process_lock(path: Path, shutdown: threading.Event, logger: Callable[[str], None]) -> Optional[Any]:
    if fcntl is None:
        _log(logger, "fcntl lock unavailable; analyzer scheduler disabled for safety")
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        handle.flush()
        return handle
    except BlockingIOError:
        _log(logger, "another worker holds analyzer lease; this worker will not schedule analyzers")
    except OSError as exc:
        _log(logger, f"analyzer lock unavailable ({type(exc).__name__}); scheduler disabled for safety")
    return None


def _worker(state_snapshot: Callable[[], Dict[str, Any]], send: Callable[[str], bool], shutdown: threading.Event,
            logger: Callable[[str], None], state_file: str) -> None:
    ledger = AnalyzerLedger(state_file, logger)
    provider = SportsDataProvider(logger)
    odds_provider = OddsProvider(logger)
    sports_enabled = os.getenv("SPORTS_ANALYZER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    crypto_enabled = os.getenv("CRYPTO_ANALYZER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    last_refresh = 0.0
    while not shutdown.is_set():
        now = _now()
        try:
            if not _in_silent_period(now):
                date_key = _date_key(now)
                if sports_enabled:
                    if last_refresh == 0.0 or time.time() - last_refresh >= 45 * 60:
                        provider.refresh_today(now)
                        last_refresh = time.time()
                    brief_key = f"{date_key}:brief:daily"
                    if _sports_daily_due(now) and not ledger.already_sent(brief_key) and not ledger.attempt_recently(brief_key, now):
                        # If all endpoints fail, still send an honest diagnostic brief, not invented fixtures.
                        ledger.record_attempt(brief_key, now)
                        try:
                            price_snapshot = state_snapshot()
                        except Exception:
                            price_snapshot = {}
                        message = _daily_brief(provider, now, price_snapshot)
                        if send(message):
                            ledger.record_sent(brief_key, {"kind": "sports_daily_brief"})
                    _maybe_single_analyses(provider, odds_provider, ledger, send, logger, now)
                    _maybe_daily_mix(ledger, send, logger, now)
                if crypto_enabled:
                    due = _crypto_due(now)
                    if due:
                        crypto_key = f"{date_key}:crypto:{due}"
                        if not ledger.already_sent(crypto_key) and not ledger.attempt_recently(crypto_key, now):
                            ledger.record_attempt(crypto_key, now)
                            try:
                                snapshot = state_snapshot()
                                message = _crypto_report(snapshot if isinstance(snapshot, dict) else {}, now)
                                if send(message):
                                    ledger.record_sent(crypto_key, {"kind": "crypto_snapshot", "slot": due})
                            except Exception as exc:
                                _log(logger, f"crypto snapshot/report failed ({type(exc).__name__})")
        except Exception as exc:
            _log(logger, f"scheduler iteration failed ({type(exc).__name__})")
        shutdown.wait(30.0)


def start_analyzer_threads(*, state_snapshot: Callable[[], Dict[str, Any]], send: Callable[[str], bool],
                           shutdown: threading.Event, logger: Callable[[str], None], state_file: str) -> bool:
    """Start at most one analyzer scheduler per process, guarded by a shared-filesystem lease."""
    enabled = os.getenv("ANALYZER_SCHEDULER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        _log(logger, "scheduler disabled; set ANALYZER_SCHEDULER_ENABLED=true to enable after configuration")
        return False
    if not os.getenv("SENDER_BOT_TOKEN", "").strip() or not os.getenv("CHANNEL_ID", "").strip():
        _log(logger, "scheduler disabled because SENDER_BOT_TOKEN or CHANNEL_ID is missing")
        return False
    lease_path = Path(state_file + ".lock")
    lease = _acquire_process_lock(lease_path, shutdown, logger)
    if lease is None:
        return False
    def target() -> None:
        try:
            _worker(state_snapshot, send, shutdown, logger, state_file)
        finally:
            try:
                if fcntl:
                    fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
                lease.close()
            except Exception:
                pass
    thread = threading.Thread(target=target, daemon=True, name="vochino-analyzers")
    thread.start()
    _log(logger, "sports/crypto analyzer thread started")
    return True
