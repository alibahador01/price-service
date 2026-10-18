"""Optional production-safe sports and crypto analyzers for the Vochino service.

This module is intentionally isolated from the existing voucher/price loops. It only
reads a caller-supplied STATE snapshot and sends through a caller-supplied function.
"""
from __future__ import annotations

import copy
import datetime as dt
import html
import hashlib
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

TZ = ZoneInfo("Asia/Tehran")
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
    if value in (None, "", 0, "0") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            epoch = float(value)
            if epoch >= 1_000_000_000_000:  # milliseconds since Unix epoch
                epoch /= 1000.0
            if epoch >= 1_000_000_000:
                return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(TZ)
        except (ValueError, OverflowError, OSError):
            return None
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{10,13}", text):
        try:
            epoch = float(text)
            if epoch >= 1_000_000_000_000:
                epoch /= 1000.0
            return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(TZ)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
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
             or raw.get("commence_time") or raw.get("gameTimeUtc") or raw.get("start_date")
             or raw.get("scheduled_at") or raw.get("scheduledStart") or raw.get("scheduledStartTime"))
    if start is None and raw.get("startTimestamp") is not None:
        try:
            start = dt.datetime.fromtimestamp(float(raw["startTimestamp"]), tz=dt.timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            start = None
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
        # Live Tennis API uses players.p1 / players.p2, not a list of competitors.
        if isinstance(participants, dict):
            home_name = home_name or _skills_team_name(participants.get("p1") or participants.get("player1"))
            away_name = away_name or _skills_team_name(participants.get("p2") or participants.get("player2"))
        elif isinstance(participants, list) and len(participants) >= 2:
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


def _bounded_json_dumps(value: Any, max_chars: int = 36000) -> str:
    """Serialize valid bounded JSON; never truncate a JSON string mid-document."""
    candidate = _trim_json(value, max_depth=7, max_items=70)

    def dump(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    text = dump(candidate)
    if len(text) <= max_chars:
        return text

    if not isinstance(candidate, dict):
        return dump(_trim_json(candidate, max_depth=4, max_items=20))

    # Preserve the decision-critical event, five-game records, stat-group names,
    # and real bookmaker inventory while progressively reducing optional details.
    markets_key = "available_real_bookmaker_markets"
    markets = candidate.get(markets_key, [])
    if isinstance(markets, list):
        compact_markets = []
        for market in markets[:40]:
            if not isinstance(market, dict):
                continue
            compact_markets.append({
                "market_id": market.get("market_id"), "market": market.get("market"),
                "market_key": market.get("market_key"), "bookmaker": market.get("bookmaker"),
                "updated_at": market.get("updated_at"), "source": market.get("source"),
                "event_id": market.get("event_id"),
                "outcomes": (market.get("outcomes", [])[:8] if isinstance(market.get("outcomes"), list) else []),
            })
        candidate[markets_key] = compact_markets
    if isinstance(candidate.get("data"), dict):
        candidate["data"] = _trim_json(candidate["data"], max_depth=5, max_items=35)
    if isinstance(candidate.get("verified_availability_data"), (dict, list)):
        candidate["verified_availability_data"] = _trim_json(candidate["verified_availability_data"], max_depth=4, max_items=20)
    if isinstance(candidate.get("recent_form_exactly_completed_only"), dict):
        form = candidate["recent_form_exactly_completed_only"]
        candidate["recent_form_exactly_completed_only"] = {
            side: [
                {k: row.get(k) for k in ("id", "date", "home", "away", "home_score", "away_score", "status") if k in row}
                for row in form.get(side, [])[:5] if isinstance(row, dict)
            ] if isinstance(form.get(side), list) else []
            for side in ("home", "away")
        }
        if isinstance(form.get("stats"), dict):
            candidate["recent_form_exactly_completed_only"]["stats"] = _trim_json(form["stats"], max_depth=4, max_items=24)
    text = dump(candidate)
    if len(text) <= max_chars:
        return text

    # The final compact shape intentionally omits bulky raw payloads, not evidence.
    compact = {
        "event": candidate.get("event"),
        "recent_form_exactly_completed_only": candidate.get("recent_form_exactly_completed_only"),
        "sport_specific_metric_groups_present": candidate.get("sport_specific_metric_groups_present"),
        "verified_availability_data": candidate.get("verified_availability_data"),
        markets_key: candidate.get(markets_key, [])[:18] if isinstance(candidate.get(markets_key), list) else [],
        "data": _trim_json(candidate.get("data", {}), max_depth=4, max_items=18),
    }
    text = dump(compact)
    if len(text) <= max_chars:
        return text
    # Each provider value was already bounded; raise a deliberate small-payload
    # error rather than sending malformed JSON or silently dropping its closing brace.
    raise ValueError("verified model payload exceeds configured size limit")


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


def _odds_get_json(url: str, keys: Sequence[str], params: Dict[str, Any], logger: Callable[[str], None]) -> Any:
    """Use the spare API key only on authentication failures, never to bypass a quota."""
    if not keys:
        return None
    for index, key in enumerate(keys):
        query = dict(params)
        query["apiKey"] = key
        try:
            response = requests.get(url, params=query,
                                    headers={"User-Agent": "VochinoAnalyzer/2.0", "Accept": "application/json"},
                                    timeout=(3.05, 7.0))
            if response.status_code in (401, 403) and index + 1 < len(keys):
                continue
            if response.status_code != 200:
                if response.status_code in (401, 403, 429):
                    _log(logger, f"The Odds API returned HTTP {response.status_code}; quota/auth limit respected")
                return None
            try:
                return response.json()
            except (ValueError, TypeError):
                return None
        except requests.RequestException as exc:
            _log(logger, f"The Odds API request failed ({type(exc).__name__})")
            return None
    return None


class OddsProvider:
    def __init__(self, logger: Callable[[str], None]):
        self.logger = logger
        self.keys = _env_values("THE_ODDS_API_KEY", "THE_ODDS_API_KEY_2", "ODDS_API_KEY", "ODDS_API_KEY_2")
        self.key = self.keys[0] if self.keys else ""
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
        data = _odds_get_json("https://api.the-odds-api.com/v4/sports", self.keys, {}, self.logger)
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
                params = {"regions": os.getenv("THE_ODDS_API_REGIONS", "eu,uk"),
                          "markets": ("h2h" if event.get("sport") == "tennis" else "h2h,spreads,totals"),
                          "oddsFormat": "decimal", "dateFormat": "iso"}
                parsed = _odds_get_json(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds", self.keys, params, self.logger)
                if isinstance(parsed, list):
                    rows = [item for item in parsed if isinstance(item, dict)]
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
                    market_key = str(market.get("key") or "").strip().lower()
                    if not market_key:
                        continue
                    outcomes = []
                    for outcome in market.get("outcomes", []) if isinstance(market.get("outcomes"), list) else []:
                        try:
                            price = float(outcome.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if price > 1.0 and price < 1000:
                            point = outcome.get("point")
                            name = str(outcome.get("name", "")).strip()
                            if point is not None and market_key in ("totals", "spreads", "alternate_totals", "team_totals"):
                                name = f"{name} {point}".strip()
                            outcomes.append({"name": name, "price": price, "point": point})
                    if len(outcomes) >= 2:
                        books.append({"market": market_key, "market_name": market_key.replace("_", " ").title(),
                                      "bookmaker": str(bookmaker.get("title") or bookmaker.get("key") or "unknown"),
                                      "updated_at": updated.isoformat() if updated else str(bookmaker.get("last_update") or "unknown"),
                                      "outcomes": outcomes})
            if books:
                matched.append({"source": "The Odds API", "event_id": str(row.get("id", "")),
                                "commence_time": row_start.isoformat(), "bookmakers": books,
                                "received_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        return matched[:1]


# --- Additional source adapters and evidence guards (2026-10 production plan) ---

TEAM_NAME_FA = {
    "real madrid": "رئال مادرید", "barcelona": "بارسلونا", "fc barcelona": "بارسلونا",
    "arsenal": "آرسنال", "chelsea": "چلسی", "manchester city": "منچسترسیتی",
    "man city": "منچسترسیتی", "manchester united": "منچستریونایتد", "man united": "منچستریونایتد",
    "liverpool": "لیورپول", "tottenham hotspur": "تاتنهام", "tottenham": "تاتنهام",
    "newcastle united": "نیوکاسل", "aston villa": "استون ویلا", "bayern munich": "بایرن مونیخ",
    "borussia dortmund": "بوروسیا دورتموند", "bayer leverkusen": "بایرلورکوزن", "rb leipzig": "لایپزیگ",
    "paris saint-germain": "پاری‌سن‌ژرمن", "paris saint germain": "پاری‌سن‌ژرمن", "psg": "پاری‌سن‌ژرمن",
    "juventus": "یوونتوس", "inter milan": "اینتر", "internazionale": "اینتر", "ac milan": "آث میلان",
    "napoli": "ناپولی", "roma": "رم", "atletico madrid": "اتلتیکومادرید", "atletico de madrid": "اتلتیکومادرید",
    "sevilla": "سویا", "athletic club": "اتلتیک بیلبائو", "ajax": "آژاکس", "benfica": "بنفیکا",
    "celtic": "سلتیک", "porto": "پورتو", "sporting cp": "اسپورتینگ", "bournemouth": "بورنموث",
    "afc bournemouth": "بورنموث", "sturm graz": "اشتروم گراتس", "olympiacos": "المپیاکوس",
    "marseille": "مارسی", "celta vigo": "سلتاویگو", "leverkusen": "بایرلورکوزن",
    "los angeles lakers": "لس‌آنجلس لیکرز", "la lakers": "لس‌آنجلس لیکرز", "boston celtics": "بوستون سلتیکس",
    "golden state warriors": "گلدن استیت وریرز", "new york knicks": "نیویورک نیکس", "denver nuggets": "دنور ناگتس",
    "toronto maple leafs": "تورنتو میپل لیفز", "montreal canadiens": "مونترال کانادینز",
    "boston bruins": "بوستون بروینز", "edmonton oilers": "ادمونتون اویلرز", "florida panthers": "فلوریدا پنترز",
    "novak djokovic": "نواک جوکوویچ", "djokovic": "جوکوویچ", "carlos alcaraz": "کارلوس آلکاراز",
    "alcaraz": "آلکاراز", "jannik sinner": "یانیک سینر", "iga swiatek": "ایگا اشویانتک",
    "coco gauff": "کوکو گاف", "aryna sabalenka": "آرینا سابالنکا", "rafael nadal": "رافائل نادال",
}
LEAGUE_NAME_FA = {
    "EPL": "لیگ برتر انگلیس", "La Liga": "لالیگا", "Serie A": "سری آ ایتالیا",
    "Bundesliga": "بوندسلیگا", "Ligue 1": "لیگ یک فرانسه", "UCL": "لیگ قهرمانان اروپا",
    "Europa League": "لیگ اروپا", "Conference League": "لیگ کنفرانس اروپا", "NBA": "ان‌بی‌ای",
    "WNBA": "لیگ بسکتبال زنان آمریکا", "NHL": "لیگ هاکی NHL", "ATP": "تور تنیس مردان ATP", "WTA": "تور تنیس زنان WTA",
}


def _env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _env_values(*names: str) -> List[str]:
    result = []
    for name in names:
        value = os.getenv(name, "").strip()
        if value and value not in result:
            result.append(value)
    return result


def _api_json(method: str, url: str, *, keys: Optional[Sequence[str]] = None,
              header_factory: Optional[Callable[[str], Dict[str, str]]] = None,
              params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None,
              timeout: Tuple[float, float] = (3.05, 7.0)) -> Any:
    """Bounded JSON request. Rotate a backup key only for auth failures, never 429/quota errors."""
    candidates = list(keys or [""])
    if not candidates:
        return None
    for index, key in enumerate(candidates):
        if header_factory and not key:
            continue
        headers = {"User-Agent": "VochinoAnalyzer/2.0", "Accept": "application/json"}
        if header_factory and key:
            headers.update(header_factory(key))
        try:
            response = requests.request(method.upper(), url, headers=headers, params=params,
                                        json=body, timeout=timeout)
            if response.status_code in (401, 403) and index + 1 < len(candidates):
                continue
            if response.status_code == 429 or response.status_code in (402, 404):
                return None
            if response.status_code >= 500 or response.status_code != 200:
                return None
            try:
                return response.json()
            except (ValueError, TypeError):
                return None
        except requests.RequestException:
            return None
    return None


def _walk_dicts(payload: Any, depth: int = 0, max_depth: int = 7) -> Iterable[Dict[str, Any]]:
    if depth > max_depth:
        return
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from _walk_dicts(value, depth + 1, max_depth)
    elif isinstance(payload, list):
        for item in payload[:700]:
            if isinstance(item, (dict, list)):
                yield from _walk_dicts(item, depth + 1, max_depth)


def _rows_from_payload(payload: Any, keys: Sequence[str] = ("results", "data", "events", "matches", "games", "items", "fixtures", "odds")) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for child in keys:
                nested = value.get(child)
                if isinstance(nested, list):
                    return [x for x in nested if isinstance(x, dict)]
    return []


def _side_name(row: Dict[str, Any], side: str) -> str:
    names = ("home", "home_team", "homeTeam", "homeTeamName", "home_name", "player1", "player_1", "participant1") if side == "home" else (
        "away", "away_team", "awayTeam", "awayTeamName", "away_name", "player2", "player_2", "participant2")
    for key in names:
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("displayName") or value.get("fullName") or value.get("shortName") or value.get("abbreviation")
        if value not in (None, ""):
            return str(value).strip()
    for container_key in ("teams", "participants", "competitors", "players"):
        values = row.get(container_key)
        if isinstance(values, list) and len(values) >= 2:
            item = values[0 if side == "home" else 1]
            if isinstance(item, dict):
                return str(item.get("name") or item.get("displayName") or item.get("fullName") or item.get("abbreviation") or "").strip()
            return str(item).strip()
    return ""


def _row_start(row: Dict[str, Any]) -> Optional[dt.datetime]:
    for key in ("date", "commence_time", "start_time", "startTime", "start_date", "gameDate", "start_at", "scheduled_at", "matchDate", "datetime", "scheduledStart", "scheduledStartTime"):
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("date") or value.get("start")
        parsed = _parse_time(value)
        if parsed:
            return parsed
    timestamp = row.get("startTimestamp")
    if timestamp is not None:
        try:
            return dt.datetime.fromtimestamp(float(timestamp), tz=dt.timezone.utc).astimezone(TZ)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return None


def _row_is_final(row: Dict[str, Any]) -> bool:
    status = row.get("status") or row.get("gameState") or row.get("game_state") or row.get("state") or row.get("match_status") or ""
    if isinstance(status, dict):
        status = status.get("name") or status.get("state") or status.get("description") or status.get("shortDetail") or ""
    text = re.sub(r"[^a-z]+", " ", str(status).lower()).strip()
    # “Postponed” must never be confused with ESPN's completed state “post”.
    if "postponed" in text or "postponement" in text or "cancelled" in text or "canceled" in text or "abandoned" in text:
        return False
    final_states = {"final", "finished", "complete", "completed", "full time", "ft", "post", "after extra time", "after penalties"}
    return text in final_states or row.get("completed") is True


def _row_scores(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    def val(*keys: str) -> Optional[float]:
        for key in keys:
            v = row.get(key)
            if isinstance(v, dict):
                v = v.get("value") or v.get("score") or v.get("displayValue")
            try:
                f = float(str(v).replace(",", ""))
                if f >= 0:
                    return f
            except (TypeError, ValueError):
                pass
        return None
    return val("home_score", "homeScore", "home_points", "homeScoreValue", "home_result"), val("away_score", "awayScore", "away_points", "awayScoreValue", "away_result")


def _persian_fallback_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "نامشخص"
    normalized = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    for key in sorted(TEAM_NAME_FA, key=len, reverse=True):
        if normalized == key:
            return TEAM_NAME_FA[key]
    # Transliterate a fallback instead of exposing an unformatted English team name.
    text = raw
    substitutions = [
        (r"sch", "ش"), (r"sh", "ش"), (r"ch", "چ"), (r"zh", "ژ"), (r"kh", "خ"),
        (r"gh", "غ"), (r"ph", "ف"), (r"th", "ث"), (r"ck", "ک"), (r"qu", "ک"),
        (r"oo", "و"), (r"ou", "و"), (r"ow", "او"), (r"ee", "ی"), (r"ea", "ی"),
        (r"ai", "ی"), (r"ay", "ی"), (r"ei", "ی"), (r"ie", "ی"), (r"y", "ی"),
        (r"a", "ا"), (r"b", "ب"), (r"c", "ک"), (r"d", "د"), (r"e", "ِ"),
        (r"f", "ف"), (r"g", "گ"), (r"h", "ه"), (r"i", "ی"), (r"j", "ج"),
        (r"k", "ک"), (r"l", "ل"), (r"m", "م"), (r"n", "ن"), (r"o", "و"),
        (r"p", "پ"), (r"q", "ق"), (r"r", "ر"), (r"s", "س"), (r"t", "ت"),
        (r"u", "و"), (r"v", "و"), (r"w", "و"), (r"x", "کس"), (r"z", "ز"),
    ]
    out = text.lower()
    for pattern, repl in substitutions:
        out = re.sub(pattern, repl, out)
    out = re.sub(r"[^\u0600-\u06FF0-9۰-۹\s'’.-]", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out or "نامشخص"


def _fa_team(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    if normalized in TEAM_NAME_FA:
        return TEAM_NAME_FA[normalized]
    for key, translation in TEAM_NAME_FA.items():
        if normalized and (normalized.endswith(" " + key) or normalized.startswith(key + " ")):
            return translation
    return _persian_fallback_name(raw)


def _fa_league(value: Any) -> str:
    raw = str(value or "").strip()
    return LEAGUE_NAME_FA.get(raw, _persian_fallback_name(raw) if raw else "رقابت ورزشی")


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _extract_market_rows(payload: Any, *, source: str, expected_event: Optional[Dict[str, Any]] = None,
                         require_event_match: bool = False) -> List[Dict[str, Any]]:
    """Normalize fresh real bookmaker rows without mixing alternate lines or player props."""
    if payload is None:
        return []
    output: List[Dict[str, Any]] = []
    seen = set()
    source_norm = re.sub(r"[^a-z0-9]+", "", str(source).lower())
    prop_line_source = source_norm == "propline"
    core_whole_event_markets = {
        "h2h", "moneyline", "1x2", "winner", "match_winner", "matchwinner", "btts",
        "both_teams_to_score", "double_chance", "draw_no_bet", "dnb", "odd_even",
        "correct_score", "half_time_full_time", "ht_ft", "first_goal", "last_goal",
    }
    game_line_markets = {
        "spreads", "spread", "totals", "total", "over_under", "team_totals", "team_total",
        "alternate_totals", "alternate_spreads", "total_corners", "corners", "total_cards",
        "cards", "player_props", "game_total", "total_points", "total_goals", "sets_total",
        "games_total", "total_games",
    }

    def _name_part(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("name") or value.get("displayName") or value.get("fullName") or value.get("title") or value.get("id")
        return str(value or "").strip()

    def _number(value: Any) -> Optional[float]:
        if isinstance(value, dict):
            value = value.get("decimal") or value.get("decimal_odds") or value.get("decimalOdds") or value.get("american") or value.get("value")
        try:
            return float(str(value).replace("+", "").strip())
        except (TypeError, ValueError):
            return None

    def _parse_outcome(out: Dict[str, Any], market_key: str) -> Optional[Dict[str, Any]]:
        # Decimal prices win when both formats are present. PropLine's documented `price`
        # field is American odds; convert it before any implied-probability calculation.
        explicit_decimal = next((out.get(key) for key in (
            "price_decimal", "decimal_price", "decimal_odds", "decimalOdds", "decimalPrice", "priceDecimal",
            "odds_decimal", "oddsDecimal"
        ) if out.get(key) not in (None, "")), None)
        raw_price = explicit_decimal
        price_format = "decimal"
        if raw_price is None:
            raw_price = out.get("price")
            if raw_price is None:
                raw_price = out.get("odds") or out.get("odds_value") or out.get("value")
            if isinstance(raw_price, dict):
                if any(raw_price.get(k) not in (None, "") for k in ("decimal", "decimal_odds", "decimalOdds")):
                    raw_price = raw_price.get("decimal") or raw_price.get("decimal_odds") or raw_price.get("decimalOdds")
                else:
                    raw_price = raw_price.get("american") or raw_price.get("value")
                    price_format = "american" if prop_line_source else "decimal"
            elif prop_line_source:
                price_format = str(out.get("price_format") or out.get("odds_format") or "auto").lower()
        price = _number(raw_price)
        if price is None:
            return None
        if explicit_decimal is None and prop_line_source and (price_format in ("american", "us", "american_odds") or price < 0 or price >= 100):
            # PropLine's standard `price` is American odds (e.g., -110/+100), while
            # alternate source payloads may expose a decimal field explicitly.
            if price == 0:
                return None
            price = 1.0 + (price / 100.0 if price > 0 else 100.0 / abs(price))
        elif price_format in ("american", "us", "american_odds"):
            if price == 0:
                return None
            price = 1.0 + (price / 100.0 if price > 0 else 100.0 / abs(price))
        if not (1.0001 < price < 1000):
            return None

        name = out.get("name") or out.get("label") or out.get("selection") or out.get("side") or out.get("outcome") or out.get("display_name")
        if isinstance(name, dict):
            name = name.get("name") or name.get("label") or name.get("displayName")
        if not name:
            return None
        point = out.get("point") if out.get("point") is not None else out.get("line")
        subject = (out.get("player_name") or out.get("athlete_name") or out.get("player") or out.get("athlete") or
                   out.get("participant") or out.get("description") or out.get("team_name") or out.get("team"))
        subject = _name_part(subject)
        return {"raw_name": str(name).strip()[:120], "price": float(price), "point": point,
                "subject": subject[:100], "raw": out}

    def add(market_key: str, market_name: str, bookmaker: str, updated: Any, outcomes: Any, event_id: Any = None) -> None:
        if not isinstance(outcomes, list):
            return
        mkey = str(market_key or "unknown").strip().lower().replace(" ", "_")
        bname = str(bookmaker or "ناشناس").strip()
        stamp = _parse_time(updated)
        # Never invent a timestamp for bookmaker odds.
        if not stamp or abs((_now() - stamp).total_seconds()) > 6 * 60 * 60:
            return
        parsed = []
        for out in outcomes:
            if isinstance(out, dict):
                item = _parse_outcome(out, mkey)
                if item:
                    parsed.append(item)
        if len(parsed) < 2:
            return

        # Split alternate lines and player markets before normalizing. Over and Under at
        # different lines, or two different athletes in one API array, must never be mixed.
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        is_whole_event = mkey in core_whole_event_markets
        is_player_prop = prop_line_source and not is_whole_event and mkey not in game_line_markets
        if is_whole_event:
            groups[("event", "")] = parsed
        else:
            for item in parsed:
                point = item.get("point")
                subject = item.get("subject", "")
                if is_player_prop:
                    # Some feeds label a player's prop in description, others put player in the market label.
                    grouping = (subject.lower(), str(point if point is not None else ""))
                elif mkey in ("spreads", "spread", "alternate_spreads"):
                    try:
                        line_key = str(abs(float(point))) if point is not None else ""
                    except (TypeError, ValueError):
                        line_key = str(point or "")
                    # Spread side labels differ between Home/Away (or team names); pair
                    # opposite sides only when the absolute handicap line matches.
                    grouping = ("event", line_key)
                elif point is not None or subject:
                    grouping = (subject.lower(), str(point if point is not None else ""))
                else:
                    grouping = ("event", "")
                groups.setdefault(grouping, []).append(item)

        for (subject, line_key), items in groups.items():
            # Player prop markets generally need both Over and Under; market lines need at
            # least two matched outcomes. Do not pair unrelated one-way selections.
            if len(items) < 2:
                continue
            unique_outcomes: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for item in items:
                label = item["raw_name"]
                point = item.get("point")
                if point is not None and str(point) not in label and mkey in game_line_markets | {"player_points", "player_rebounds", "player_assists", "player_aces", "player_games", "player_shots"}:
                    label = f"{label} {point}".strip()
                signature = (label.lower(), str(point if point is not None else ""))
                unique_outcomes[signature] = {"name": label[:160], "price": item["price"], "point": point,
                                              "description": item.get("subject", "")}
            normalized = list(unique_outcomes.values())
            if len(normalized) < 2:
                continue
            # Include line/athlete in the market ID so all 300-500 real market lines survive dedupe.
            descriptor = (subject + "|" + line_key + "|" + str(market_name or mkey)).encode("utf-8", "ignore")
            suffix = hashlib.sha1(descriptor).hexdigest()[:10]
            display_market = str(market_name or mkey.replace("_", " ").title())[:100]
            raw_subject = next((x.get("subject") for x in items if x.get("subject")), "")
            if raw_subject and is_player_prop:
                display_market = f"{display_market} — {raw_subject}"[:140]
            elif raw_subject and mkey not in core_whole_event_markets:
                display_market = f"{display_market} — {raw_subject}"[:140]
            if line_key:
                display_market = f"{display_market} ({line_key})"[:150]
            event_ref = event_id or (expected_event or {}).get("id")
            entry = {"market_id": f"{source}:{event_ref}:{mkey}:{bname}:{suffix}",
                     "market": display_market, "market_key": mkey, "bookmaker": bname,
                     "updated_at": stamp.isoformat(), "source": source, "event_id": str(event_ref or ""),
                     "outcomes": normalized}
            sig = (entry["market_id"], tuple((x["name"], round(float(x["price"]), 4)) for x in normalized))
            if sig not in seen:
                seen.add(sig)
                output.append(entry)

    # Common odds schema: event -> books -> markets -> outcomes.
    for obj in _walk_dicts(payload):
        event_id = obj.get("id") or obj.get("event_id") or obj.get("fixture_id")
        home = _side_name(obj, "home")
        away = _side_name(obj, "away")
        if expected_event and require_event_match and not (home and away):
            continue
        if expected_event and home and away and not (
            (_same_side(expected_event.get("home", ""), home) and _same_side(expected_event.get("away", ""), away)) or
            (_same_side(expected_event.get("home", ""), away) and _same_side(expected_event.get("away", ""), home))
        ):
            continue
        bookmakers = obj.get("bookmakers") or obj.get("books") or obj.get("sportsbooks")
        if isinstance(bookmakers, dict):
            book_iter = [{**book, "_book_key": k} for k, book in bookmakers.items() if isinstance(book, dict)]
        else:
            book_iter = _as_list(bookmakers)
        for book in book_iter:
            if not isinstance(book, dict):
                continue
            bname = book.get("title") or book.get("name") or book.get("displayName") or book.get("key") or book.get("_book_key") or book.get("bookmaker") or source
            updated = book.get("last_update") or book.get("last_update_at") or book.get("updated_at") or book.get("last_change_at") or book.get("recorded_at") or obj.get("last_update") or obj.get("updated_at") or ""
            market_items = book.get("markets") or book.get("odds") or book.get("market_data")
            if isinstance(market_items, dict):
                market_iter = [{**value, "_market_key": key} for key, value in market_items.items() if isinstance(value, dict)]
            else:
                market_iter = _as_list(market_items)
            for market_obj in market_iter:
                if not isinstance(market_obj, dict):
                    continue
                mk = market_obj.get("key") or market_obj.get("market_key") or market_obj.get("market") or market_obj.get("type") or market_obj.get("_market_key")
                outcomes = market_obj.get("outcomes") or market_obj.get("selections") or market_obj.get("lines")
                add(str(mk or "unknown"), str(market_obj.get("title") or market_obj.get("name") or mk or "بازار"), str(bname),
                    market_obj.get("last_update") or market_obj.get("updated_at") or updated, outcomes, event_id)

        if (obj.get("market_key") or obj.get("market")) and (obj.get("price") or obj.get("decimal_odds") or obj.get("price_decimal")) and (obj.get("bookmaker") or obj.get("bookmaker_name")):
            mk = str(obj.get("market_key") or obj.get("market"))
            add(mk, str(obj.get("market_name") or mk), str(obj.get("bookmaker") or obj.get("bookmaker_name")),
                obj.get("updated_at") or obj.get("recorded_at") or "",
                [{k: obj.get(k) for k in ("name", "outcome", "selection", "price", "price_decimal", "decimal_odds", "point", "line", "description", "player", "player_name", "team")}], event_id)

    # Schema without an event/book wrapper (e.g. an API's normalized market rows).
    if not output and not require_event_match:
        for obj in _walk_dicts(payload):
            outcomes = obj.get("outcomes") or obj.get("selections")
            if not isinstance(outcomes, list):
                continue
            market = obj.get("market_key") or obj.get("key") or obj.get("market") or obj.get("marketName")
            book = obj.get("bookmaker") or obj.get("bookmaker_name") or obj.get("book") or source
            add(str(market or "unknown"), str(obj.get("market_name") or obj.get("title") or market or "بازار"),
                str(book), obj.get("updated_at") or obj.get("last_update") or "", outcomes,
                obj.get("event_id") or obj.get("id"))
    return output[:600]


class DataCompletionProvider:
    """Source-specific completion. It never creates missing stats or markets."""
    def __init__(self, logger: Callable[[str], None]):
        self.logger = logger
        self.bsd_keys = _env_values("BZZOIRO_API_KEY", "BSD_API_KEY", "BZZOIRO_KEY")
        self.tennis_keys = _env_values("LIVE_TENNIS_API_KEY", "LIVETENNIS_API_KEY", "LIVE_TENNIS_API_KEY_2", "LIVETENNIS_API_KEY_2", "TENNIS_API_KEY", "TENNIS_API_KEY_2")
        self.puck_keys = _env_values("PUCKAPI_API_KEY", "PUCKAPI_KEY", "PUCK_API_KEY", "PUCKAPI_API_KEY_2", "PUCKAPI_KEY_2", "PUCK_API_KEY_2")
        self.prop_keys = _env_values("PROPLINE_API_KEY", "PROP_LINE_API_KEY", "PROPLINE_KEY", "PROP_LINE_KEY", "PROPLINE_API_KEY_2", "PROP_LINE_API_KEY_2", "PROPLINE_KEY_2", "PROP_LINE_KEY_2")
        self.openodds_keys = _env_values("OPENODDSAPI_API_KEY", "OPEN_ODDS_API_KEY", "OPENODDS_API_KEY", "OPENODDSAPI_API_KEY_2", "OPEN_ODDS_API_KEY_2", "OPENODDS_API_KEY_2")
        self.tavily_keys = _env_values("TAVILY_API_KEY", "TAVILY_API_KEY_2")
        self.lock = threading.RLock()
        self.cache: Dict[str, Tuple[float, Any]] = {}
        self.puck_detail_day: Optional[str] = None
        self.puck_detail_used = False
        self.bsd_hist_stats_ids: set = set()
        self.prop_history_day = _date_key()
        self.prop_history_stats_calls = 0

    def _cached(self, key: str, ttl: int, fn: Callable[[], Any]) -> Any:
        with self.lock:
            item = self.cache.get(key)
            if item and time.time() - item[0] < ttl:
                return copy.deepcopy(item[1])
        value = fn()
        if value is not None:
            with self.lock:
                self.cache[key] = (time.time(), copy.deepcopy(value))
        return copy.deepcopy(value)

    def _bsd_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.bsd_keys:
            return None
        return _api_json("GET", "https://sports.bzzoiro.com/api/v2/" + path.lstrip("/"), keys=self.bsd_keys,
                         header_factory=lambda key: {"Authorization": "Token " + key}, params=params)

    def _bsd_tennis_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.bsd_keys:
            return None
        return _api_json("GET", "https://sports.bzzoiro.com/tennis/api/v2/" + path.lstrip("/"), keys=self.bsd_keys,
                         header_factory=lambda key: {"Authorization": "Token " + key}, params=params)

    def _prop_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.prop_keys:
            return None
        return _api_json("GET", "https://api.prop-line.com/v1/" + path.lstrip("/"), keys=self.prop_keys,
                         header_factory=lambda key: {"X-API-Key": key}, params=params)

    def _puck_post(self, path: str, body: Dict[str, Any]) -> Any:
        if not self.puck_keys:
            return None
        return _api_json("POST", "https://mcp.puckapi.com/v1/" + path.lstrip("/"), keys=self.puck_keys,
                         header_factory=lambda key: {"x-api-key": key, "Content-Type": "application/json"}, body=body)

    def _key_market(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self._cached("markets:" + str(event.get("id")), 20 * 60, lambda: self._markets_uncached(event)) or []

    def _prop_sport_key(self, event: Dict[str, Any]) -> Optional[str]:
        league_id = event.get("league_id")
        soccer_map = {
            "eng.1": "soccer_epl", "esp.1": "soccer_spain_la_liga", "ita.1": "soccer_italy_serie_a",
            "ger.1": "soccer_germany_bundesliga", "fra.1": "soccer_france_ligue_one",
            "uefa.champions": "soccer_uefa_champs_league", "uefa.europa": "soccer_uefa_europa_league",
            "uefa.europa.conf": "soccer_uefa_europa_conference_league", "ned.1": "soccer_netherlands_eredivisie",
            "por.1": "soccer_portugal_primeira_liga", "bel.1": "soccer_belgium_first_division_a",
            "tur.1": "soccer_turkey_super_league", "sco.1": "soccer_scotland_premiership", "rus.1": "soccer_russia_premier_league",
            "mex.1": "soccer_mexico_ligamx", "bra.1": "soccer_brazil_campeonato", "eng.fa": "soccer_fa_cup",
            "esp.copa_del_rey": "soccer_spain_copa_del_rey",
        }
        if event.get("sport") == "soccer":
            return soccer_map.get(league_id)
        if league_id in ("nba", "wnba"):
            return "basketball_" + league_id
        if event.get("sport") == "hockey":
            return "hockey_nhl"
        if event.get("sport") == "tennis":
            return "tennis"
        return None

    def _markets_uncached(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        sport_key = self._prop_sport_key(event)
        if not self.prop_keys or not sport_key:
            return []
        events_payload = self._cached("prop-events:" + sport_key, 15 * 60,
                                      lambda: self._prop_get(f"sports/{sport_key}/events", {"limit": 200}))
        rows = _rows_from_payload(events_payload, ("events", "data", "results", "items"))
        matched = None
        for row in rows:
            home = _side_name(row, "home")
            away = _side_name(row, "away")
            start = _row_start(row)
            if not (home and away and start):
                continue
            teams_match = ((_same_side(event.get("home", ""), home) and _same_side(event.get("away", ""), away)) or
                           (_same_side(event.get("home", ""), away) and _same_side(event.get("away", ""), home)))
            if teams_match and abs((start - event["start_dt"]).total_seconds()) <= 3 * 60 * 60:
                matched = row
                break
        if not matched:
            return []
        pid = matched.get("id") or matched.get("event_id") or matched.get("eventId")
        if not pid:
            return []
        m_payload = self._prop_get(f"sports/{sport_key}/events/{pid}/markets")
        market_rows = _rows_from_payload(m_payload, ("markets", "data", "results", "items"))
        available_keys = []
        for row in market_rows:
            key = row.get("key") or row.get("market_key") or row.get("market") or row.get("id")
            if key:
                key = str(key).strip()
                if key not in available_keys:
                    available_keys.append(key)
        collected: List[Dict[str, Any]] = []
        if available_keys:
            # Scan up to 500 source-advertised market types, in bounded groups. Never invent
            # catalog items; the upstream list is the exact set supported for this event.
            key_cap = available_keys[:500]
            if len(available_keys) > len(key_cap):
                self.logger(f"PropLine catalog exposes {len(available_keys)} keys; internal safety cap is 500")
            for batch_start in range(0, len(key_cap), 25):
                batch = key_cap[batch_start:batch_start + 25]
                batch_key = ",".join(batch)
                cache_key = f"prop-odds:{sport_key}:{pid}:{hashlib.sha1(batch_key.encode()).hexdigest()[:12]}"
                payload = self._cached(cache_key, 15 * 60, lambda batch_key=batch_key: self._prop_get(
                    f"sports/{sport_key}/events/{pid}/odds", {"markets": batch_key, "max_age": 3600}))
                if payload is not None:
                    collected.extend(_extract_market_rows(payload, source="PropLine", expected_event=event))
                # Source documents both a 5-request/sec rate and burst constraints.
                if batch_start + 25 < len(key_cap):
                    time.sleep(0.22)
        else:
            # No market catalogue returned: ask only for documented core markets as a fallback.
            basic = "h2h,spreads,totals" if event.get("sport") != "tennis" else "h2h"
            payload = self._cached(f"prop-odds-core:{sport_key}:{pid}", 15 * 60, lambda: self._prop_get(
                f"sports/{sport_key}/events/{pid}/odds", {"markets": basic, "max_age": 3600}))
            collected.extend(_extract_market_rows(payload, source="PropLine", expected_event=event))
        return collected[:500]

    def _bsd_event_search(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sport = event.get("sport")
        if sport not in ("soccer", "tennis") or not self.bsd_keys:
            return None
        start = event["start_dt"]
        date_from = (start.date() - dt.timedelta(days=1)).isoformat()
        date_to = (start.date() + dt.timedelta(days=1)).isoformat()
        if sport == "soccer":
            payload = self._cached(f"bsd-events:{date_from}:{date_to}", 6 * 60 * 60,
                                   lambda: self._bsd_get("events/", {"date_from": date_from, "date_to": date_to, "limit": 200, "offset": 0}))
        else:
            payload = self._cached(f"bsd-tennis:{date_from}:{date_to}", 6 * 60 * 60,
                                   lambda: self._bsd_tennis_get("matches/", {"date_from": date_from, "date_to": date_to, "status": "finished", "limit": 200, "offset": 0}))
        rows = _rows_from_payload(payload, ("results", "events", "matches", "data", "items"))
        for row in rows:
            home, away = _side_name(row, "home"), _side_name(row, "away")
            rstart = _row_start(row)
            teams_match = ((_same_side(event.get("home", ""), home) and _same_side(event.get("away", ""), away)) or
                           (_same_side(event.get("home", ""), away) and _same_side(event.get("away", ""), home)))
            if teams_match and rstart and abs((rstart - start).total_seconds()) <= 4 * 60 * 60:
                return row
        return None

    def _bsd_history(self, team_id: Any, event: Dict[str, Any], current_bsd_id: str, deep: bool) -> List[Dict[str, Any]]:
        if not team_id:
            return []
        team_id = str(team_id)
        start_date = (event["start_dt"].date() - dt.timedelta(days=100)).isoformat()
        end_date = (event["start_dt"].date() - dt.timedelta(days=1)).isoformat()
        payload = self._cached(f"bsd-history:{team_id}:{end_date}", 12 * 60 * 60,
                               lambda: self._bsd_get("events/", {"team_id": team_id, "date_from": start_date,
                                                                    "date_to": end_date, "status": "finished", "limit": 50}))
        rows = _rows_from_payload(payload, ("results", "events", "data", "items"))
        matched = []
        for row in rows:
            rid = str(row.get("id") or row.get("event_id") or "")
            d = _row_start(row)
            if not rid or rid == current_bsd_id or not d or d >= event["start_dt"] or not _row_is_final(row):
                continue
            h, a = _row_scores(row)
            if h is None or a is None:
                continue
            matched.append({"id": rid, "date": d.isoformat(), "home": _side_name(row, "home"), "away": _side_name(row, "away"),
                            "home_score": h, "away_score": a, "status": "finished"})
        matched.sort(key=lambda x: x["date"], reverse=True)
        result = matched[:5]
        if deep:
            for game in result:
                rid = game["id"]
                if rid in self.bsd_hist_stats_ids:
                    continue
                stats = self._cached(f"bsd-stats:{rid}", 24 * 60 * 60, lambda rid=rid: self._bsd_get(f"events/{rid}/stats/"))
                self.bsd_hist_stats_ids.add(rid)
                if stats:
                    game["stats"] = _trim_json(stats, max_depth=5, max_items=50)
        return result

    def upcoming_tennis_events(self, now: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
        """Adapt only real upcoming matches from the Live Tennis API feed."""
        now = now or _now()
        if not self.tennis_keys:
            return []
        payload = self._cached("live-tennis:upcoming:" + _date_key(now), 20 * 60,
                               lambda: _api_json("GET", "https://api.livetennisapi.com/api/public/v1/matches",
                                                 keys=self.tennis_keys, header_factory=lambda key: {"X-API-Key": key},
                                                 params={"status": "upcoming", "limit": 100}))
        output: List[Dict[str, Any]] = []
        received = dt.datetime.now(dt.timezone.utc).isoformat()
        for row in _rows_from_payload(payload, ("matches", "events", "data", "results", "items"))[:120]:
            if not isinstance(row, dict):
                continue
            start = _row_start(row)
            if not start or start.astimezone(TZ).date() != now.astimezone(TZ).date() or start < now - dt.timedelta(minutes=10):
                continue
            tournament = row.get("tournament")
            if isinstance(tournament, dict):
                tournament = tournament.get("name") or tournament.get("title") or ""
            tour = str(row.get("tour") or row.get("tour_name") or row.get("category") or row.get("circuit") or tournament or "").lower()
            lid = "wta" if "wta" in tour or "women" in tour or "female" in tour else "atp"
            league = next((item for item in LEAGUES if item["id"] == lid), None)
            if league is None:
                continue
            normalized_raw = _skills_event_to_espn_shape(row)
            if not normalized_raw:
                continue
            ev = _normalise_event(normalized_raw, league, received)
            if not ev or not ev.get("home") or not ev.get("away"):
                continue
            ev["source"] = "Live Tennis API"
            ev["id"] = "livetennis:" + str(ev["id"])
            ev["state"] = "pre"
            ev["status"] = "شروع‌نشده"
            output.append(ev)
        return output

    def _prop_recent_form(self, event: Dict[str, Any], *, deep: bool = False) -> Dict[str, Any]:
        """Use the documented score archive as a fallback; all historical rows must be final and pre-kickoff."""
        sport_key = self._prop_sport_key(event)
        if not sport_key or not self.prop_keys:
            return {"home": [], "away": []}
        score_payload = self._cached(
            f"prop-scores:{sport_key}:45d", 8 * 60 * 60,
            lambda: self._prop_get(f"sports/{sport_key}/scores", {"days_from": 45}))
        rows = _rows_from_payload(score_payload, ("scores", "events", "results", "data", "games", "items"))
        sides: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}
        target_start = event.get("start_dt")
        if not isinstance(target_start, dt.datetime):
            return sides
        for row in rows:
            start = _row_start(row)
            if not start or start >= target_start or not _row_is_final(row):
                continue
            hname, aname = _side_name(row, "home"), _side_name(row, "away")
            hs, ass = _row_scores(row)
            if not hname or not aname or hs is None or ass is None:
                continue
            item = {"id": str(row.get("id") or row.get("event_id") or ""), "date": start.isoformat(),
                    "home": hname, "away": aname, "home_score": hs, "away_score": ass, "status": "finished"}
            if _same_side(event.get("home", ""), hname) or _same_side(event.get("home", ""), aname):
                sides["home"].append(item)
            if _same_side(event.get("away", ""), hname) or _same_side(event.get("away", ""), aname):
                sides["away"].append(item)
        for side in sides:
            sides[side].sort(key=lambda x: str(x.get("date", "")), reverse=True)
            sides[side] = sides[side][:5]
        if deep:
            if self.prop_history_day != _date_key():
                self.prop_history_day = _date_key()
                self.prop_history_stats_calls = 0
            unique: Dict[str, Dict[str, Any]] = {}
            for side in ("home", "away"):
                for row in sides[side]:
                    if row.get("id"):
                        unique[row["id"]] = row
            # Hard daily cap protects API quota; absent stats remain absent and never become zeroes.
            for event_id, row in list(unique.items())[:10]:
                if self.prop_history_stats_calls >= 24:
                    break
                stats_payload = self._cached(
                    f"prop-boxscore:{sport_key}:{event_id}", 36 * 60 * 60,
                    lambda eid=event_id: self._prop_get(f"sports/{sport_key}/events/{eid}/stats"))
                if stats_payload not in (None, {}, []):
                    row["boxscore_statistics"] = _trim_json(stats_payload, max_depth=5, max_items=120)
                    self.prop_history_stats_calls += 1
        return sides

    def enrich(self, event: Dict[str, Any], summary: Dict[str, Any], *, deep: bool = False) -> Dict[str, Any]:
        """Collect only fields that the provider returns; missing fields remain missing."""
        result: Dict[str, Any] = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sources": []}
        base = summary.get("data") if isinstance(summary.get("data"), dict) else {}
        if base:
            result["espn_summary"] = base
            result["sources"].append("ESPN")
        if event.get("sport") == "soccer" and self.bsd_keys:
            bsd_event = self._bsd_event_search(event)
            if bsd_event:
                bid = str(bsd_event.get("id") or bsd_event.get("event_id") or "")
                result["football_match"] = _trim_json(bsd_event, max_depth=5, max_items=80)
                result["sources"].append("Bzzoiro")
                if deep:
                    detail_calls = {
                        "match_detail": lambda: self._bsd_get(f"events/{bid}/") if bid else None,
                        "stats": lambda: self._bsd_get(f"events/{bid}/stats/") if bid else None,
                        "lineups": lambda: self._bsd_get(f"events/{bid}/lineups/") if bid else None,
                        "incidents": lambda: self._bsd_get(f"events/{bid}/incidents/") if bid else None,
                        "availability": lambda: self._bsd_get(f"events/{bid}/availability/") if bid else None,
                        "odds": lambda: self._bsd_get(f"events/{bid}/odds/comparison/") if bid else None,
                        "head_to_head": lambda: self._bsd_get(f"events/{bid}/h2h/") if bid else None,
                    }
                    for name, callback in detail_calls.items():
                        payload = self._cached(f"bsd-detail:{bid}:{name}", 6 * 60 * 60, callback) if bid else None
                        if payload not in (None, {}, []):
                            result[name] = _trim_json(payload, max_depth=6, max_items=100)
                    detail = result.get("match_detail") or bsd_event
                    home_team_id = None; away_team_id = None
                    for blob in _walk_dicts(detail):
                        h = blob.get("home_team") or blob.get("homeTeam") or blob.get("home")
                        a = blob.get("away_team") or blob.get("awayTeam") or blob.get("away")
                        if isinstance(h, dict) and h.get("id") is not None: home_team_id = h.get("id")
                        if isinstance(a, dict) and a.get("id") is not None: away_team_id = a.get("id")
                    h_id = home_team_id or bsd_event.get("home_team_id") or bsd_event.get("homeTeamId")
                    a_id = away_team_id or bsd_event.get("away_team_id") or bsd_event.get("awayTeamId")
                    for side, team_id in (("home", h_id), ("away", a_id)):
                        form = self._bsd_history(team_id, event, bid, True)
                        if form:
                            result.setdefault("recent_form", {})[side] = form
        # Tennis: keep the free Live Tennis endpoint to today's tournament/score data;
        # historical form is only attached if another returned source actually supplies it.
        if event.get("sport") == "tennis" and self.tennis_keys:
            current = self._cached("live-tennis:upcoming:" + _date_key(), 20 * 60, lambda: _api_json(
                "GET", "https://api.livetennisapi.com/api/public/v1/matches", keys=self.tennis_keys,
                header_factory=lambda key: {"X-API-Key": key}, params={"status": "upcoming", "limit": 100}))
            if current:
                result["tennis_live_feed"] = _trim_json(current, max_depth=5, max_items=80)
                result["sources"].append("Live Tennis API")
            # Bzzoiro's separate tennis endpoint may require a paid sports add-on.
            # Do not spend requests there; use the Live Tennis key for the schedule.
            # A pick still requires five verified completed matches from an actual data feed.
        # NHL recent form is a single cached batch query, 5 credits/day at most.
        if event.get("sport") == "hockey" and self.puck_keys:
            date_from = (event["start_dt"].date() - dt.timedelta(days=50)).isoformat()
            date_to = (event["start_dt"].date() - dt.timedelta(days=1)).isoformat()
            puck = self._cached("puck-games:" + _date_key(), 20 * 60 * 60, lambda: self._puck_post(
                "get_games", {"date_from": date_from, "date_to": date_to, "game_state": "FINAL", "limit": 500}))
            rows = _rows_from_payload(puck, ("games", "data", "results"))
            forms: Dict[str, List[Dict[str, Any]]] = {"home": [], "away": []}
            hname, aname = event.get("home", ""), event.get("away", "")
            for row in rows:
                start = _row_start(row)
                if not start or start >= event["start_dt"] or not _row_is_final(row):
                    continue
                h, a = _side_name(row, "home"), _side_name(row, "away")
                hs, ass = _row_scores(row)
                if hs is None or ass is None:
                    continue
                item = {"date": start.isoformat(), "home": h, "away": a, "home_score": hs, "away_score": ass, "status": "finished"}
                if _same_side(hname, h) or _same_side(hname, a): forms["home"].append(item)
                if _same_side(aname, h) or _same_side(aname, a): forms["away"].append(item)
            for side in forms:
                forms[side].sort(key=lambda x: x["date"], reverse=True)
                forms[side] = forms[side][:5]
            if forms["home"] and forms["away"]:
                result["recent_form"] = forms
                result["sources"].append("PuckAPI")
        # For tennis, OpenOddsAPI contributes only real market prices, never player history.
        return result

    def markets_for_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        markets: List[Dict[str, Any]] = []
        # Broad real-market lookup through PropLine; only markets actually returned are kept.
        markets.extend(self._key_market(event))
        if event.get("sport") == "soccer" and self.bsd_keys:
            bsd_event = self._bsd_event_search(event)
            if bsd_event:
                bid = bsd_event.get("id") or bsd_event.get("event_id")
                if bid:
                    p = self._cached(f"bsd-market:{bid}", 20 * 60, lambda: self._bsd_get(f"events/{bid}/odds/comparison/"))
                    markets.extend(_extract_market_rows(p, source="Bzzoiro", expected_event=event))
        if event.get("sport") == "tennis" and self.openodds_keys:
            p = self._cached("openodds-tennis:" + _date_key(), 15 * 60, lambda: _api_json(
                "GET", "https://openoddsapi.com/v1/sports/tennis/odds", keys=self.openodds_keys,
                header_factory=lambda key: {"X-API-Key": key},
                params={"region": "eu", "odds_format": "decimal", "markets": "moneyline"}))
            # OpenOddsAPI returns tennis events under `bouts`; each book holds `outcomes`
            # directly (unlike the usual event/book/market schema). Normalize only exact matchup rows.
            if isinstance(p, dict) and isinstance(p.get("bouts"), list):
                normalized_events = []
                for bout in p["bouts"][:250]:
                    if not isinstance(bout, dict):
                        continue
                    home = _side_name(bout, "home")
                    away = _side_name(bout, "away")
                    start = _row_start(bout)
                    same_teams = home and away and ((
                        _same_side(event.get("home", ""), home) and _same_side(event.get("away", ""), away)) or (
                        _same_side(event.get("home", ""), away) and _same_side(event.get("away", ""), home)))
                    if not same_teams or not start or abs((start - event["start_dt"]).total_seconds()) > 3 * 60 * 60:
                        continue
                    books = bout.get("books") or bout.get("bookmakers") or []
                    normalized_books = []
                    if isinstance(books, dict):
                        books = [dict(v, key=k) for k, v in books.items() if isinstance(v, dict)]
                    for book in books if isinstance(books, list) else []:
                        if not isinstance(book, dict):
                            continue
                        outcomes = book.get("outcomes") or book.get("selections") or []
                        normalized_books.append({"key": book.get("key") or book.get("name") or book.get("title") or book.get("bookmaker") or book.get("book") or book.get("source") or "OpenOdds book",
                            "title": book.get("title") or book.get("name") or book.get("key") or book.get("bookmaker") or book.get("book") or book.get("source") or "OpenOdds book",
                            "last_update": book.get("last_update") or bout.get("last_update"),
                            "markets": [{"key": "h2h", "title": "Moneyline", "outcomes": outcomes}]})
                    normalized_events.append({"id": bout.get("id") or bout.get("event_id") or "openodds-tennis",
                        "home_team": home, "away_team": away, "commence_time": start.isoformat(),
                        "bookmakers": normalized_books})
                markets.extend(_extract_market_rows(normalized_events, source="OpenOddsAPI", expected_event=event, require_event_match=True))
            else:
                markets.extend(_extract_market_rows(p, source="OpenOddsAPI", expected_event=event, require_event_match=True))
        # Deduplicate by source/market/book and keep only actual outcomes.
        unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in markets:
            key = (str(row.get("market_id") or ""), str(row.get("bookmaker") or ""))
            if key not in unique or str(row.get("updated_at", "")) > str(unique[key].get("updated_at", "")):
                unique[key] = row
        return list(unique.values())[:500]

    def search_news(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        if not self.tavily_keys:
            return []
        payload = _api_json("POST", "https://api.tavily.com/search", keys=self.tavily_keys,
                            header_factory=lambda key: {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                            body={"query": query, "topic": "news", "search_depth": "basic", "max_results": max_results,
                                   "include_answer": False, "include_raw_content": False, "include_published_date": True},
                            timeout=(3.05, 9.0))
        items = _rows_from_payload(payload, ("results", "data", "items"))
        result = []
        for row in items:
            title = row.get("title") or row.get("headline")
            url = row.get("url") or row.get("link")
            description = row.get("content") or row.get("description") or row.get("snippet")
            if title and url and str(url).startswith(("https://", "http://")):
                result.append({"headline": str(title)[:240], "url": str(url)[:500], "summary": str(description or "")[:500],
                               "published": str(row.get("published_date") or row.get("published") or "")[:80]})
        return result[:max_results]

    def extra_market_context(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Get verified pre-game injury/news context only when Tavily is configured."""
        if not self.tavily_keys:
            return {}
        query = f'latest verified injuries suspensions lineup {event.get("home")} {event.get("away")} {event.get("league")}'
        return {"query_results": self.search_news(query, max_results=4)}


# Keep this name map bounded; unknown names are transliterated rather than left in English.
def _format_recent_form(form: Any) -> str:
    if not isinstance(form, list) or not form:
        return "آمار پنج بازی اخیر به‌طور کامل تأیید نشد."
    tokens = []
    for row in form[:5]:
        if not isinstance(row, dict):
            continue
        try:
            hs, aw = float(row.get("home_score")), float(row.get("away_score"))
        except (TypeError, ValueError):
            continue
        date = _parse_time(row.get("date"))
        date_text = _fa_digits(date.strftime("%m/%d")) if date else ""
        home, away = _fa_team(row.get("home")), _fa_team(row.get("away"))
        tokens.append(f"{date_text} {home} { _fa_digits(int(hs)) }–{ _fa_digits(int(aw)) } {away}")
    return "؛ ".join(tokens) if tokens else "نتایج کافی در دسترس نیست."


def _model_day_key() -> str:
    return _date_key(_now())


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


def _gemini_model_candidates() -> List[str]:
    """Never silently switch models; fallback requires an explicit opt-in."""
    default_model = "gemini-3.5-flash-lite"
    configured_model = os.getenv("GEMINI_MODEL", default_model).strip() or default_model
    models = [configured_model]
    allow_fallback = os.getenv("GEMINI_ALLOW_MODEL_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
    fallback_model = os.getenv("GEMINI_FREE_FALLBACK_MODEL", "").strip()
    if allow_fallback and fallback_model and fallback_model not in models:
        models.append(fallback_model)
    return models


def _call_gemini(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    keys = _env_values("GEMINI_API_KEY", "GEMINI_API_KEY_2")
    if not keys:
        return None
    # Model IDs already set in Render remain authoritative. No implicit premium/paid fallback.
    models = _gemini_model_candidates()
    for model_index, model in enumerate(models):
        for key_index, key in enumerate(keys):
            try:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=AI_TIMEOUT_SECONDS * 1000))
                response = client.models.generate_content(model=model, contents=prompt)
                parsed = _extract_json_object(getattr(response, "text", "") or "")
                if parsed:
                    return parsed
                # A syntactically invalid model answer is not retried through a second key.
                return None
            except Exception as exc:
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                msg = str(exc).lower()
                auth_error = code in (401, 403) or "api key not valid" in msg or "invalid api key" in msg or "unauthorized" in msg
                model_error = "model not found" in msg or "not supported for generatecontent" in msg or "unknown model" in msg
                if auth_error and key_index + 1 < len(keys):
                    continue
                if model_error and model_index + 1 < len(models):
                    break
                _log(logger or (lambda _message: None), f"Gemini request failed ({type(exc).__name__})")
                return None
    return None


def _call_groq(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    keys = _env_values("GROQ_API_KEY", "GROQ_API_KEY_2")
    if not keys:
        return None
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
    for index, key in enumerate(keys):
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
            code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            msg = str(exc).lower()
            auth_error = code in (401, 403) or "invalid api key" in msg or "authenticationerror" in type(exc).__name__.lower()
            if auth_error and index + 1 < len(keys):
                continue
            _log(logger or (lambda _message: None), f"Groq request failed ({type(exc).__name__})")
            return None
    return None


def _call_openrouter(prompt: str, logger: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    keys = _env_values("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2")
    if not keys:
        return None
    model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash-0731:free").strip() or "deepseek/deepseek-v4-flash-0731:free"
    for index, key in enumerate(keys):
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "Vochino Sports Analyzer"}
            referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
            if referer:
                headers["HTTP-Referer"] = referer
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers,
                                     json={"model": model, "messages": [
                                         {"role": "system", "content": "Independently re-evaluate evidence; return one JSON object only."},
                                         {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 900},
                                     timeout=AI_TIMEOUT_SECONDS)
            if response.status_code in (401, 403) and index + 1 < len(keys):
                continue
            if response.status_code != 200:
                _log(logger or (lambda _message: None), f"OpenRouter HTTP {response.status_code}; model result unavailable")
                return None
            data = response.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return _extract_json_object(text)
        except (requests.RequestException, ValueError, TypeError, IndexError, KeyError) as exc:
            _log(logger or (lambda _message: None), f"OpenRouter request failed ({type(exc).__name__})")
            return None
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


def _espn_recent_form(provider: SportsDataProvider, event: Dict[str, Any], *, deep: bool = False) -> Dict[str, Any]:
    """Get the last five completed meetings per side from ESPN history; no current/future results."""
    league_key = (event.get("sport"), event.get("league_id"))
    # ESPN scoreboard archives are not a reliable ATP/WTA form source. Avoid up to
    # 31 fruitless requests; tennis is eligible only when a real supplemental source
    # actually returns five completed matches per player.
    if event.get("sport") == "tennis":
        return {"home": [], "away": [], "stats": {}}
    now = _now()
    if not hasattr(provider, "_history_scoreboards"):
        provider._history_scoreboards = {}
        provider._history_detail_cache = {}
        provider._history_detail_day = _date_key(now)
        provider._history_detail_calls = 0
    if provider._history_detail_day != _date_key(now):
        provider._history_detail_day = _date_key(now)
        provider._history_detail_calls = 0
    league = next((x for x in LEAGUES if x["sport"] == league_key[0] and x["id"] == league_key[1]), None)
    if not league:
        return {"home": [], "away": [], "stats": {}}
    home_results: List[Dict[str, Any]] = []
    away_results: List[Dict[str, Any]] = []
    # Most clubs/players will yield five results in 2–3 weeks. Keep a hard cap to protect Render.
    for day_offset in range(1, 32):
        day = (event["start_dt"].date() - dt.timedelta(days=day_offset))
        key = f"{league['sport']}:{league['id']}:{day.isoformat()}"
        cached = provider._history_scoreboards.get(key)
        if cached is None or time.time() - cached[0] > 18 * 60 * 60:
            url = f"{ESPN_BASE}/{league['sport']}/{league['id']}/scoreboard"
            data = _json_get(url, params={"dates": day.strftime("%Y%m%d")}, timeout=(3.05, 7.0), retries=1)
            items = []
            if isinstance(data, dict) and isinstance(data.get("events"), list):
                received = dt.datetime.now(dt.timezone.utc).isoformat()
                for raw in data["events"]:
                    if isinstance(raw, dict):
                        e = _normalise_event(raw, league, received)
                        if e and e.get("start_dt") and e["start_dt"] < event["start_dt"] and e.get("state") in ("post", "final"):
                            items.append(e)
            provider._history_scoreboards[key] = (time.time(), items)
        else:
            items = cached[1]
        for past in items:
            if _same_side(event.get("home", ""), past.get("home", "")) or _same_side(event.get("home", ""), past.get("away", "")):
                if not any(x.get("id") == past.get("id") for x in home_results):
                    home_results.append(past)
            if _same_side(event.get("away", ""), past.get("home", "")) or _same_side(event.get("away", ""), past.get("away", "")):
                if not any(x.get("id") == past.get("id") for x in away_results):
                    away_results.append(past)
        home_results.sort(key=lambda x: x["start_dt"], reverse=True)
        away_results.sort(key=lambda x: x["start_dt"], reverse=True)
        if len(home_results) >= 5 and len(away_results) >= 5:
            break
    def pack(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for row in items[:5]:
            try:
                hs = float(row.get("home_score"))
                ass = float(row.get("away_score"))
            except (TypeError, ValueError):
                continue
            result.append({"id": str(row.get("id") or ""), "date": row["start_dt"].isoformat(), "home": row.get("home"),
                           "away": row.get("away"), "home_score": hs, "away_score": ass, "status": "finished"})
        return result
    out: Dict[str, Any] = {"home": pack(home_results), "away": pack(away_results), "stats": {}}
    if deep:
        # Fetch detailed box scores only for recent games and cache them for a day; this is capped per process-day.
        unique_games: Dict[str, Dict[str, Any]] = {}
        for item in home_results[:5] + away_results[:5]:
            if item.get("id"):
                unique_games[str(item["id"])] = item
        for game_id, game in list(unique_games.items())[:10]:
            if game_id in provider._history_detail_cache:
                out["stats"][game_id] = copy.deepcopy(provider._history_detail_cache[game_id])
                continue
            if provider._history_detail_calls >= 50:
                break
            provider._history_detail_calls += 1
            payload = _json_get(f"{ESPN_BASE}/{league['sport']}/{league['id']}/summary",
                                params={"event": game_id}, timeout=(3.05, 7.0), retries=1)
            detail = {}
            if isinstance(payload, dict):
                for key in ("boxscore", "statistics", "leaders", "injuries", "plays", "rosters", "header", "odds"):
                    if payload.get(key) not in (None, {}, [], ""):
                        detail[key] = _trim_json(payload[key], max_depth=5, max_items=80)
            provider._history_detail_cache[game_id] = detail
            out["stats"][game_id] = copy.deepcopy(detail)
    return out


def _market_inventory(event: Dict[str, Any], summary: Dict[str, Any], odds_provider: OddsProvider,
                      completion: DataCompletionProvider) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    # Normalize ESPN's actual outcome-price objects directly; they are already grouped by market/book.
    for feed in _espn_odds_for_event(event, summary):
        for book in feed.get("bookmakers", []) if isinstance(feed.get("bookmakers"), list) else []:
            outcomes = book.get("outcomes") if isinstance(book.get("outcomes"), list) else []
            if len(outcomes) < 2:
                continue
            market_key = str(book.get("market") or "h2h")
            result.append({"market_id": f"ESPN:{event.get('id')}:{market_key}:{book.get('bookmaker','')}",
                           "market": book.get("market_name") or market_key.replace("_", " ").title(),
                           "market_key": market_key, "bookmaker": book.get("bookmaker") or "نامشخص",
                           "updated_at": book.get("updated_at"),
                           "source": "ESPN", "event_id": str(event.get("id")), "outcomes": outcomes})
    # Legacy OddsProvider schema -> one row per real bookmaker-market pair.
    try:
        legacy = odds_provider.odds_for_event(event)
    except Exception:
        legacy = []
    for feed in legacy or []:
        for book in feed.get("bookmakers", []) if isinstance(feed, dict) else []:
            if not isinstance(book, dict):
                continue
            outcomes = book.get("outcomes") if isinstance(book.get("outcomes"), list) else []
            if len(outcomes) < 2:
                continue
            result.append({"market_id": f"{feed.get('source','Odds')}:{event.get('id')}:{book.get('market','h2h')}:{book.get('bookmaker','')}",
                           "market": book.get("market_name") or str(book.get("market") or "h2h").replace("_", " ").title(),
                           "market_key": book.get("market") or "h2h", "bookmaker": book.get("bookmaker") or "ناشناس",
                           "updated_at": book.get("updated_at"),
                           "source": feed.get("source", "odds provider"), "event_id": str(event.get("id")), "outcomes": outcomes})
    try:
        result.extend(completion.markets_for_event(event))
    except Exception as exc:
        _log(completion.logger, f"supplemental market lookup failed for event {event.get('id')} ({type(exc).__name__})")
    unique: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    now = _now()
    for row in result:
        if not isinstance(row, dict) or not isinstance(row.get("outcomes"), list) or len(row["outcomes"]) < 2:
            continue
        updated = _parse_time(row.get("updated_at"))
        if not updated or abs((now - updated).total_seconds()) > 6 * 60 * 60:
            continue
        key = (str(row.get("market_id") or ""), str(row.get("market_key") or ""), str(row.get("bookmaker") or ""), str(row.get("market") or ""))
        unique[key] = row
    return list(unique.values())[:500]


def _match_metric_groups(data: Any, sport: str) -> List[str]:
    # Count only explicit numeric/stat fields returned by providers, not model-created descriptions.
    groups: set = set()
    mapping = {
        "soccer": {"shot": "shots", "corner": "corners", "possession": "possession", "xg": "xg", "expectedgoals": "xg",
                   "goal": "goals", "pass": "passes", "foul": "fouls", "card": "cards", "tackle": "tackles", "attack": "attacks", "save": "saves"},
        "basketball": {"point": "points", "rebound": "rebounds", "assist": "assists", "turnover": "turnovers", "fieldgoal": "shooting",
                       "threepoint": "shooting", "block": "blocks", "steal": "steals", "pace": "pace", "freethrow": "shooting"},
        "hockey": {"shot": "shots", "save": "saves", "goal": "goals", "faceoff": "faceoffs", "hit": "hits", "blockedshot": "blocks",
                   "powerplay": "special_teams", "penalty": "penalties", "xg": "xg"},
        "tennis": {"ace": "serve", "doublefault": "serve", "dblfault": "serve", "firstserve": "serve", "secondserve": "serve",
                   "breakpoint": "break_points", "breakpts": "break_points", "breakpt": "break_points", "rank": "ranking",
                   "servicetotal": "serve", "returnpoints": "return", "returnpoint": "return", "matchwin": "form",
                   "winner": "winners", "totalgames": "games", "gameswon": "games", "gamesw": "games",
                   "setswon": "sets", "setsw": "sets", "matcheswon": "match_wins", "matchesw": "match_wins",
                   "tiebreak": "tie_breaks"},
    }.get(sport, {})
    for obj in _walk_dicts(data):
        stat_type = obj.get("stat_type") or obj.get("statType") or obj.get("stat_name")
        stat_value = obj.get("stat_value") if obj.get("stat_value") is not None else obj.get("statValue")
        if isinstance(stat_type, str) and stat_value is not None:
            try:
                float(str(stat_value).replace("%", "").replace(",", "").strip())
                stat_norm = re.sub(r"[^a-z0-9]+", "", stat_type.lower())
                for fragment, group in mapping.items():
                    if fragment in stat_norm:
                        groups.add(group)
            except (TypeError, ValueError):
                pass
        for key, value in obj.items():
            norm = re.sub(r"[^a-z0-9]+", "", str(key).lower())
            if isinstance(value, dict):
                # Stat providers often use {displayValue, value} or {name, value}.
                value = value.get("value") if value.get("value") is not None else value.get("displayValue")
            if not isinstance(value, (int, float, str)) or isinstance(value, bool):
                continue
            try:
                float(str(value).replace("%", "").replace(",", "").strip())
            except ValueError:
                continue
            for fragment, group in mapping.items():
                if fragment in norm:
                    groups.add(group)
    return sorted(groups)


def _has_five_recent(form: Any) -> bool:
    if not isinstance(form, dict):
        return False
    now_utc = _now().astimezone(dt.timezone.utc)
    for side in ("home", "away"):
        rows = form.get(side)
        if not isinstance(rows, list) or len(rows) < 5:
            return False
        valid = 0
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not _row_is_final(row):
                continue
            try:
                hs, aws = float(row.get("home_score")), float(row.get("away_score"))
            except (TypeError, ValueError):
                continue
            stamp = _parse_time(row.get("date"))
            if stamp is None or stamp.astimezone(dt.timezone.utc) >= now_utc:
                continue
            sig = str(row.get("id") or (stamp.isoformat(), row.get("home"), row.get("away"), hs, aws))
            if sig in seen:
                continue
            seen.add(sig); valid += 1
        if valid < 5:
            return False
    return True


def _market_label_fa(market: Any, sport: str) -> str:
    raw_text = str(market or "بازار").strip()
    base = raw_text
    detail = ""
    if " — " in base:
        base, detail = base.split(" — ", 1)
    line_match = re.match(r"^(.*?)(?:\s*\(([^()]*)\))?$", detail or base)
    if detail:
        detail_name = (line_match.group(1) or "").strip()
        detail_line = (line_match.group(2) or "").strip()
    else:
        # Basic market titles such as "Totals (2.5)" carry a line but no player.
        base_match = re.match(r"^(.*?)(?:\s*\(([^()]*)\))?$", base)
        base = (base_match.group(1) or base).strip()
        detail_line = (base_match.group(2) or "").strip()
        detail_name = ""
    text = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    labels = {
        "h2h": "برندهٔ مسابقه", "moneyline": "برندهٔ مسابقه", "winner": "برندهٔ مسابقه", "match_winner": "برندهٔ مسابقه",
        "spreads": "هندیکپ", "spread": "هندیکپ", "totals": "مجموع گل/امتیاز", "total": "مجموع گل/امتیاز",
        "over_under": "مجموع گل/امتیاز", "btts": "هر دو تیم گل می‌زنند", "both_teams_to_score": "هر دو تیم گل می‌زنند",
        "double_chance": "شانس دوگانه", "draw_no_bet": "مساوی = بازگشت وجه", "team_totals": "مجموع گل/امتیاز تیم",
        "total_corners": "مجموع کرنرها", "corners": "کرنرها", "total_shots": "مجموع شوت‌ها", "shots": "شوت‌ها",
        "alternate_totals": "مجموع جایگزین", "team_totals": "مجموع گل/امتیاز تیم", "player_props": "بازار بازیکن",
        "player_points": "امتیاز بازیکن", "player_rebounds": "ریباند بازیکن", "player_assists": "پاس گل بازیکن",
        "player_goals": "گل بازیکن", "player_shots": "شوت بازیکن", "player_saves": "مهارهای دروازه‌بان",
        "player_aces": "آس بازیکن", "games_total": "مجموع گیم‌ها", "sets_total": "مجموع ست‌ها",
        "total_games": "مجموع گیم‌ها", "game_total": "مجموع گیم‌ها", "player_total_points": "امتیاز بازیکن",
    }
    label = labels.get(text, _persian_fallback_name(base))
    if detail_name:
        # Use a known club/player name map where possible; otherwise transliterate for a Persian-first channel.
        label += " — " + _fa_team(detail_name)
    if detail_line:
        label += " (" + _fa_digits(detail_line) + ")"
    return label


def _selection_fa(selection: Any, event: Dict[str, Any]) -> str:
    text = str(selection or "").strip()
    norm = text.lower().strip()
    if _same_side(text, event.get("home", "")) or norm in ("home", "home team"):
        return _fa_team(event.get("home"))
    if _same_side(text, event.get("away", "")) or norm in ("away", "away team"):
        return _fa_team(event.get("away"))
    if norm in ("draw", "tie"):
        return "مساوی"
    if norm in ("yes", "true"):
        return "بله"
    if norm in ("no", "false"):
        return "خیر"
    m = re.match(r"^(over|under)\s*([0-9]+(?:\.[0-9]+)?)$", norm)
    if m:
        return ("بیش از " if m.group(1) == "over" else "کمتر از ") + _fa_digits(m.group(2))
    return _fa_team(text)


def _validate_deep_answer(answer: Optional[Dict[str, Any]], markets: Sequence[Dict[str, Any]], metric_groups: Sequence[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(answer, dict) or answer.get("insufficient_data") is True:
        return None
    market_id = str(answer.get("market_id") or "").strip()
    market = next((m for m in markets if str(m.get("market_id")) == market_id), None)
    if not market:
        return None
    selection = str(answer.get("selection") or "").strip()
    outcome = next((x for x in market.get("outcomes", []) if _normalise_name(selection) == _normalise_name(str(x.get("name") or ""))), None)
    if not outcome:
        return None
    try:
        probability = float(answer.get("probability_pct"))
    except (TypeError, ValueError):
        return None
    if not 5 <= probability <= 95:
        return None
    evidence = answer.get("evidence_fields")
    if not isinstance(evidence, list):
        return None
    evidence = [str(x)[:120] for x in evidence if isinstance(x, (str, int, float))]
    # Evidence must identify supplied data paths and include two independent types of evidence.
    data_paths = set()
    for item in evidence:
        if item in metric_groups or any(group in item.lower() for group in metric_groups):
            data_paths.add(item)
    if len(metric_groups) < 3 or len(evidence) < 3 or len(data_paths) < 2:
        return None
    rationale = str(answer.get("rationale") or "").strip()[:500]
    risks = str(answer.get("main_risk") or answer.get("risk") or "").strip()[:250]
    form_summary = str(answer.get("form_summary") or "").strip()[:320]
    market_reasoning = str(answer.get("market_reasoning") or "").strip()[:320]
    comparisons = answer.get("key_comparisons") if isinstance(answer.get("key_comparisons"), list) else []
    comparisons = [str(item).strip()[:220] for item in comparisons if isinstance(item, str) and re.search(r"[\u0600-\u06FF]", item)][:3]
    if not re.search(r"[\u0600-\u06FF]", form_summary):
        form_summary = ""
    if not re.search(r"[\u0600-\u06FF]", market_reasoning):
        market_reasoning = ""
    return {"market_id": market_id, "selection": outcome["name"], "probability_pct": probability,
            "rationale": rationale, "form_summary": form_summary, "key_comparisons": comparisons,
            "market_reasoning": market_reasoning, "risk": risks, "evidence_fields": evidence, "market_row": market}


def _select_markets_for_ai(markets: Sequence[Dict[str, Any]], limit: int = 36) -> List[Dict[str, Any]]:
    """Use a token-bounded, diverse shortlist from the full real market inventory.

    All provider-returned market rows are gathered and normalized first; this function
    only builds the compact analysis surface sent to the language models. It never invents odds.
    """
    priority = {
        "h2h": 0, "moneyline": 0, "1x2": 0, "match_winner": 0, "winner": 0,
        "spreads": 1, "spread": 1, "totals": 2, "total": 2, "over_under": 2,
        "btts": 3, "both_teams_to_score": 3, "double_chance": 4, "draw_no_bet": 4,
        "team_totals": 5, "total_corners": 6, "corners": 6, "cards": 7,
    }
    now = _now()
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for row in markets:
        if not isinstance(row, dict) or len(row.get("outcomes", [])) < 2:
            continue
        stamp = _parse_time(row.get("updated_at"))
        if not stamp or abs((now - stamp).total_seconds()) > 3 * 60 * 60:
            continue
        prices = []
        for outcome in row.get("outcomes", []):
            try:
                price = float(outcome.get("price"))
            except (TypeError, ValueError, AttributeError):
                continue
            if price > 1:
                prices.append(price)
        if len(prices) < 2:
            continue
        key = str(row.get("market_key") or "unknown").lower()
        by_key.setdefault(key, []).append(row)
    for key, rows in by_key.items():
        rows.sort(key=lambda row: (priority.get(key, 20), str(row.get("market", "")).lower(),
                                   -( _parse_time(row.get("updated_at")).timestamp() if _parse_time(row.get("updated_at")) else 0),
                                   str(row.get("bookmaker", ""))))
    ordered_keys = sorted(by_key, key=lambda key: (priority.get(key, 20), key))
    # Round-robin across market types so hundreds of player props do not crowd out all
    # match totals, spreads and other sport-specific markets. Up to four rows per key.
    selected: List[Dict[str, Any]] = []
    max_per_key = 4
    depth = 0
    while len(selected) < limit:
        added = False
        for key in ordered_keys:
            rows = by_key[key]
            if depth < min(max_per_key, len(rows)):
                selected.append(rows[depth])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        depth += 1
    return selected


def _deep_consensus_analysis(event: Dict[str, Any], summary: Dict[str, Any], completion_data: Dict[str, Any],
                             markets: List[Dict[str, Any]], logger: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    if not markets:
        _log(logger, f"event {event.get('id')}: no real, fresh bookmaker market returned")
        return None
    base = summary.get("data") if isinstance(summary.get("data"), dict) else {}
    merged = {"espn": base, "supplemental": completion_data}
    form = completion_data.get("recent_form") if isinstance(completion_data, dict) else None
    if not _has_five_recent(form):
        _log(logger, f"event {event.get('id')}: five completed recent games for both sides not verified; pick skipped")
        return None
    groups = _match_metric_groups(merged, event.get("sport", ""))
    # The model may not replace missing sport-specific statistics with prose.
    if len(groups) < 3:
        _log(logger, f"event {event.get('id')}: insufficient distinct sport-specific stat groups; pick skipped")
        return None
    # Scan all provider-returned market rows internally, then expose a compact but diverse
    # market set to every model; validation below uses the exact same set to prevent ghost picks.
    markets = _select_markets_for_ai(markets, limit=36)
    if not markets:
        _log(logger, f"event {event.get('id')}: no fresh, paired markets survived analysis shortlist")
        return None
    injuries = completion_data.get("availability") or completion_data.get("injuries")
    injury_leads = completion_data.get("injury_context")
    # BetBetter is only an independent model signal; its fairOdds are never bookmaker prices.
    betbetter_signal = _betbetter_signal(event, logger)
    factual = {
        "event": {"id": str(event.get("id")), "sport": event.get("sport"), "league": event.get("league_code") or event.get("league"),
                  "home": event.get("home"), "away": event.get("away"), "start_local": event.get("start_dt").isoformat(), "status": event.get("status")},
        "recent_form_exactly_completed_only": form,
        "data": merged,
        "sport_specific_metric_groups_present": groups,
        "verified_availability_data": injuries,
        "unverified_web_leads_require_primary_confirmation": injury_leads,
        "betbetter_independent_model_signal_not_bookmaker_odds": betbetter_signal,
        "available_real_bookmaker_markets": markets,
    }
    prompt = (
        "You are the lead sports analyst. Only use the attached JSON; never browse from memory, invent a stat, injury, lineup, historical result, bookmaker, market, odds or probability. "
        "The input contains the latest verified real event data and only actual markets returned by bookmaker data services. The event is eligible only because each side has at least five completed previous games and at least three sport-specific stat groups. "
        "Analyze both sides point-by-point: the five recent matches, scores/form, relevant available stats, head-to-head if real, verified injuries/suspensions, venue, rest schedule, and exact available markets. Search snippets marked unverified are leads only and must not be presented as confirmed injuries; never claim fields that are absent. "
        "Treat betbetter_independent_model_signal_not_bookmaker_odds only as corroborating model opinion: compare it against the primary analysis and flag disagreements internally; never use its fairOdds as bookmaker odds or as sole proof. "
        "Choose ONE market AND outcome only from available_real_bookmaker_markets. Do not use a model fair-price as bookmaker odds. Compare probabilities to the market's implied probability and identify the biggest risk. Reject when no robust edge. "
        "Return JSON only with: market_id (exact existing ID), selection (exact one outcome name in that market), probability_pct (a cautious estimated probability, 5-95, not certainty), rationale (Persian), form_summary (one concise Persian summary of the last-five trend, do NOT list the individual old scores), key_comparisons (0-3 concise Persian comparisons using only numeric stats actually present, with team/player labels and values when available), market_reasoning (one concise Persian sentence explaining the selected real market and its de-vigged price context without inventing extra odds), main_risk (Persian), evidence_fields (3 or more exact data paths that exist), insufficient_data (boolean), edge_supported (boolean). "
        "Never post old match results as a list; summarize only meaningful trends. If a comparison cannot be calculated from available fields, omit it. Set insufficient_data=true or edge_supported=false if support is weak, the evidence is contradictory or the market is stale. No betting certainty and no fabricated confidence. Rationale must be concise Persian.\nDATA_JSON:\n" +
        _bounded_json_dumps(factual, max_chars=36000)
    )
    # First two independent reviews are always run. The third engine is a resolver,
    # not a redundant third call on every routine case.
    gemini_raw = _call_gemini(prompt, logger)
    groq_raw = _call_groq(prompt, logger)
    gemini = _validate_deep_answer(gemini_raw, markets, groups)
    groq = _validate_deep_answer(groq_raw, markets, groups)
    if (not gemini_raw or not groq_raw or not gemini or not groq or
            gemini_raw.get("edge_supported") is not True or groq_raw.get("edge_supported") is not True):
        _log(logger, f"event {event.get('id')}: Gemini/Groq evidence validation failed; pick skipped")
        return None

    def vote_key(item: Dict[str, Any]) -> Tuple[str, str]:
        return (str(item.get("market_id")), _normalise_name(str(item.get("selection", ""))))

    def same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        return vote_key(a) == vote_key(b)

    chosen_group: List[Dict[str, Any]] = []
    same_pick = same(gemini, groq)
    probability_gap = abs(gemini["probability_pct"] - groq["probability_pct"])
    if same_pick and probability_gap <= AI_DISAGREEMENT_THRESHOLD:
        # Good alignment: save a third model call and average the two grounded estimates.
        chosen_group = [gemini, groq]
    else:
        deepseek_raw = _call_openrouter(prompt, logger)
        deepseek = _validate_deep_answer(deepseek_raw, markets, groups)
        if not deepseek_raw or deepseek_raw.get("edge_supported") is not True or not deepseek:
            _log(logger, f"event {event.get('id')}: disagreement resolver unavailable/invalid; pick skipped")
            return None
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for item in (gemini, groq, deepseek):
            grouped.setdefault(vote_key(item), []).append(item)
        winner = max(grouped.values(), key=len)
        if len(winner) < 2:
            _log(logger, f"event {event.get('id')}: no 2-of-3 agreement on exact market and selection; pick skipped")
            return None
        # Even a selection majority is rejected if its probability estimates remain materially split.
        probabilities = [item["probability_pct"] for item in winner]
        if max(probabilities) - min(probabilities) > AI_DISAGREEMENT_THRESHOLD:
            _log(logger, f"event {event.get('id')}: probability disagreement unresolved; pick skipped")
            return None
        chosen_group = winner

    chosen = dict(chosen_group[0])
    chosen["probability_pct"] = sum(item["probability_pct"] for item in chosen_group) / len(chosen_group)
    market = next((m for m in markets if m["market_id"] == chosen["market_id"]), None)
    outcome = next((x for x in (market or {}).get("outcomes", []) if x.get("name") == chosen["selection"]), None)
    if not market or not outcome:
        return None
    if isinstance(betbetter_signal, dict) and betbetter_signal.get("selection"):
        signal_selection = str(betbetter_signal.get("selection") or "").strip()
        selected_norm = _normalise_name(chosen.get("selection", ""))
        signal_norm = _normalise_name(signal_selection)
        if signal_norm and signal_norm not in selected_norm and selected_norm not in signal_norm:
            _log(logger, f"event {event.get('id')}: BetBetter disagrees with consensus selection; independent signal retained for audit only")
    # Conservative value gate: compare with de-vigged outcomes in the selected exact market.
    probabilities = []
    for row in market.get("outcomes", []):
        try:
            price = float(row.get("price"))
            if price > 1:
                probabilities.append((row.get("name"), 1 / price))
        except (TypeError, ValueError):
            pass
    margin_total = sum(x[1] for x in probabilities)
    market_implied = next((prob / margin_total for name, prob in probabilities if name == chosen["selection"]), None) if margin_total else None
    if market_implied is None or chosen["probability_pct"] / 100 < market_implied + 0.025:
        _log(logger, f"event {event.get('id')}: estimated edge below safety threshold; pick skipped")
        return None
    try:
        fresh_at = _parse_time(market.get("updated_at"))
        if not fresh_at or ( _now() - fresh_at).total_seconds() > 3 * 60 * 60:
            return None
        price = float(outcome["price"])
        if price <= 1:
            return None
    except (TypeError, ValueError):
        return None
    return {"market_id": market["market_id"], "market": market.get("market"), "market_key": market.get("market_key"),
            "selection": chosen["selection"], "probability_pct": max(1, min(95, chosen["probability_pct"])),
            "rationale": chosen.get("rationale", ""), "form_summary": chosen.get("form_summary", ""),
            "key_comparisons": chosen.get("key_comparisons", []), "market_reasoning": chosen.get("market_reasoning", ""),
            "risk": chosen.get("risk", ""), "evidence_fields": chosen.get("evidence_fields", []),
            "bookmaker": market.get("bookmaker"), "odds": price, "odds_updated_at": market.get("updated_at"),
            "market_implied_probability": market_implied, "odds_source": market.get("source"),
            "model_votes": len(chosen_group), "sport_metric_groups": groups, "recent_form": form,
            "availability": injuries, "betbetter_signal": betbetter_signal}


def _known_injuries(data: Any) -> List[str]:
    """Render only structured player availability; never expose raw JSON/search snippets publicly."""
    if not isinstance(data, (dict, list)):
        return []
    found: List[str] = []
    status_map = {
        "out": "غایب", "injured": "مصدوم", "injury": "مصدوم", "doubtful": "حضور بعید",
        "questionable": "حضور نامشخص", "day to day": "ارزیابی روزانه", "ir": "فهرست مصدومان",
        "injured reserve": "فهرست مصدومان", "suspended": "محروم", "suspension": "محروم",
        "probable": "احتمال حضور", "available": "آمادهٔ بازی", "active": "قابل استفاده",
    }
    for obj in _walk_dicts(data):
        player = obj.get("athlete") or obj.get("player") or obj.get("person") or obj.get("participant")
        if isinstance(player, dict):
            name = player.get("displayName") or player.get("fullName") or player.get("name")
        else:
            name = player if isinstance(player, str) else obj.get("player_name") or obj.get("athlete_name")
        raw_status = obj.get("status") or obj.get("type") or obj.get("availability") or obj.get("designation")
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("name") or raw_status.get("description") or raw_status.get("type")
        raw_reason = obj.get("details") or obj.get("detail") or obj.get("reason") or obj.get("injury") or obj.get("description")
        status_key = re.sub(r"[^a-z]+", " ", str(raw_status or "").lower()).strip()
        reason_text = str(raw_reason or "").strip()
        is_availability_record = bool(name and (status_key in status_map or raw_reason))
        if not is_availability_record:
            continue
        name_fa = _fa_team(name)
        status_fa = status_map.get(status_key)
        if status_fa is None and status_key:
            # Unknown English status is intentionally not shown as a factual Persian claim.
            continue
        reason_fa = ""
        if reason_text and re.search(r"[\u0600-\u06FF]", reason_text):
            reason_fa = "، " + reason_text[:100]
        descriptor = status_fa or ("وضعیت اعلام‌شده" if reason_fa else "")
        if descriptor:
            line = f"{name_fa}: {descriptor}{reason_fa}"
            if line not in found:
                found.append(line)
        if len(found) >= 6:
            break
    return found


def _event_priority(event: Dict[str, Any]) -> float:
    league = str(event.get("league_id") or "")
    sport = str(event.get("sport") or "")
    league_score = {
        "uefa.champions": 100, "eng.1": 98, "esp.1": 95, "ita.1": 91, "ger.1": 90, "fra.1": 88,
        "uefa.europa": 87, "nba": 90, "nhl": 86, "atp": 82, "wta": 80, "uefa.europa.conf": 78,
        "wnba": 72, "tur.1": 67, "ned.1": 68, "por.1": 67, "eng.fa": 70,
    }.get(league, 55 if sport in ("soccer", "basketball", "hockey", "tennis") else 0)
    known_teams = " ".join((event.get("home", ""), event.get("away", ""))).lower()
    fame_hits = sum(1 for name in TEAM_NAME_FA if len(name) > 5 and name in known_teams)
    start = event.get("start_dt") or _now()
    hours = max(0.0, (start - _now()).total_seconds() / 3600.0)
    # The day shortlist favors known competitions and prominent teams, not odds or AI confidence.
    return league_score + min(12, fame_hits * 5) + (4 if 3 <= hours <= 9 else 0)


def _shortlist_message(events: Sequence[Dict[str, Any]], summaries: Dict[str, Dict[str, Any]], forms: Dict[str, Dict[str, Any]], now: dt.datetime) -> str:
    """Public heads-up only. Historical form, stats and injury research remain internal."""
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐⁰¹ ➫ 〰╮", "         ⫹⫺ مسابقات منتخب امروز", "╰〰💲 𓆩𓆩📰𓆪𓆪 💲〰╯", "",
             f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} تهران", "",
             "✦ این مسابقه‌ها برای بررسی عمیق داخلی انتخاب شده‌اند.",
             "تحلیل نهایی نزدیک شروع، فقط در صورت تأیید داده‌ها و بازار واقعی منتشر می‌شود.", "〰️〰️〰️〰️〰️〰️〰️〰️"]
    for idx, event in enumerate(events[:6], 1):
        sport_icon = {"soccer": "⚽", "basketball": "🏀", "hockey": "🏒", "tennis": "🎾"}.get(event.get("sport"), "🏆")
        comp = _fa_league(event.get("league_code") or event.get("league"))
        start = event.get("start_dt")
        if not isinstance(start, dt.datetime):
            continue
        start = start.astimezone(TZ)
        lines.extend([f"{_fa_digits(idx)}. {sport_icon} {_fa_team(event.get('home') or event.get('name'))} 🆚 {_fa_team(event.get('away'))}",
                      f"🏆 {_safe_text(comp, 90)} | 📅 {_fa_digits(_jalali_date(start))} | 🕐 {_fa_digits(start.strftime('%H:%M'))} تهران",
                      "✦ بررسی تخصصی پیش از شروع؛ انتشار فقط با شواهد کافی.", "〰️〰️〰️〰️〰️〰️〰️〰️"])
    lines.extend([_safe_text(_promo_line(now, 10), 300), "⚠️ انتخاب بازی به معنی تضمین انتشار شرط نیست."])
    return "\n".join(lines)[:3900]


def _form_wdl(items: Sequence[Dict[str, Any]], team: str) -> str:
    pieces = []
    for row in list(items)[:5]:
        try:
            home_score, away_score = float(row["home_score"]), float(row["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        is_home = _same_side(team, str(row.get("home", "")))
        own, other = (home_score, away_score) if is_home else (away_score, home_score)
        pieces.append("ب" if own > other else ("م" if own == other else "خ"))
    return "–".join(pieces) if pieces else "اطلاعات کافی نیست"


def _daily_brief(provider: SportsDataProvider, now: dt.datetime, snapshot: Optional[Dict[str, Any]] = None) -> str:
    # Kept as a compatibility helper, but its public format no longer exposes provider names or raw links.
    events, successes, failures, stale = provider.snapshot_events()
    future = [e for e in events if e.get("state") in ("pre", "scheduled", "unknown") and e["start_dt"] >= now]
    selected = sorted(future, key=_event_priority, reverse=True)[:6]
    return _shortlist_message(selected, {}, {}, now) if selected else ""


def _crypto_market_stats(now: dt.datetime) -> Dict[str, Any]:
    """Get actual 24h changes in one ticker call, then 7-day trend per requested asset; cache four hours."""
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "TRX": "TRXUSDT", "TON": "TONUSDT", "FET": "FETUSDT", "PAXG": "PAXGUSDT", "XAUT": "XAUTUSDT"}
    cache = globals().setdefault("_CRYPTO_CACHE", {})
    lock = globals().setdefault("_CRYPTO_CACHE_LOCK", threading.RLock())
    with lock:
        needed = {key: symbol for key, symbol in symbols.items() if not (symbol in cache and time.time() - cache[symbol][0] < 4 * 60 * 60)}
        output = {key: copy.deepcopy(cache[symbol][1]) for key, symbol in symbols.items() if symbol in cache and time.time() - cache[symbol][0] < 4 * 60 * 60}
    if not needed:
        return output
    ticker_rows = _api_json("GET", "https://data-api.binance.vision/api/v3/ticker/24hr", timeout=(2.0, 4.0))
    ticker_by_symbol = {str(row.get("symbol")): row for row in ticker_rows if isinstance(row, dict)} if isinstance(ticker_rows, list) else {}
    # A single symbol endpoint fallback is bounded to only assets missing in the all-symbol response.
    for ticker, symbol in needed.items():
        ticker_row = ticker_by_symbol.get(symbol)
        if ticker_row is None:
            ticker_row = _api_json("GET", "https://data-api.binance.vision/api/v3/ticker/24hr", params={"symbol": symbol}, timeout=(2.0, 4.0))
        if not isinstance(ticker_row, dict):
            continue
        try:
            price = float(ticker_row.get("lastPrice")); change_24h = float(ticker_row.get("priceChangePercent"))
            if price <= 0: continue
        except (TypeError, ValueError):
            continue
        kline = _api_json("GET", "https://data-api.binance.vision/api/v3/klines", params={"symbol": symbol, "interval": "1d", "limit": 9}, timeout=(2.0, 4.0))
        closes=[]; ranges=[]
        if isinstance(kline, list):
            for candle in kline:
                try:
                    if isinstance(candle, list) and len(candle)>5:
                        op, high, low, close = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
                        closes.append(close)
                        if op > 0: ranges.append((high-low)/op*100.0)
                except (TypeError, ValueError):
                    continue
        change_7d = (closes[-1] / closes[-8] - 1) * 100 if len(closes) >= 8 and closes[-8] > 0 else None
        item = {"symbol": symbol, "price_usdt": price, "change_24h_pct": change_24h, "change_7d_pct": change_7d,
                "avg_daily_range_pct": sum(ranges[-7:]) / len(ranges[-7:]) if ranges else None,
                "candles": len(closes), "as_of": now.isoformat()}
        output[ticker] = item
        with lock: cache[symbol] = (time.time(), copy.deepcopy(item))
    return output


def _crypto_report(snapshot: Dict[str, Any], now: dt.datetime, *, deep: bool = True, completion: Optional[DataCompletionProvider] = None) -> str:
    """Persian crypto report from actual app snapshot plus public price-history metrics."""
    ali = snapshot.get("ali1377") if isinstance(snapshot.get("ali1377"), dict) else {}
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    poster = meta.get("poster") if isinstance(meta.get("poster"), dict) else {}
    prices = poster.get("swapwallet_prices") if isinstance(poster.get("swapwallet_prices"), dict) else {}
    labels = {"USDT/IRT": ("💵", "تتر"), "TRX/IRT": ("🪙", "ترون"), "TON/IRT": ("💎", "تون"),
              "BTC/IRT": ("🟠", "بیت‌کوین"), "ETH/IRT": ("🔷", "اتریوم"), "FET/IRT": ("🤖", "فِت"),
              "PAXG/IRT": ("🟨", "طلای دیجیتال"), "XAUT/IRT": ("🟡", "تتر گلد")}
    trends = _crypto_market_stats(now)
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐⁰¹ ➫ 〰╮", "        ⫹⫺ تحلیل بازار دیجیتال", "╰〰💲 𓆩𓆩📊𓆪𓆪 💲〰╯",
             f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} تهران", "", "💰 قیمت‌های آنلاین"]
    shown = 0
    for key, (emoji, label) in labels.items():
        value = prices.get(key)
        try:
            number = float(value)
            if not (0 < number < 1e18):
                continue
        except (TypeError, ValueError):
            continue
        ticker = key.split("/")[0]
        ticker = {"USDT": "USDT", "TRX": "TRX", "TON": "TON", "BTC": "BTC", "ETH": "ETH", "FET": "FET", "PAXG": "PAXG", "XAUT": "XAUT"}.get(ticker, ticker)
        stat = trends.get(ticker)
        suffix = ""
        if stat:
            suffix = f" | ۲۴ساعت {_fa_digits(format(float(stat['change_24h_pct']), '+.2f'))}٪"
            if stat.get("change_7d_pct") is not None:
                suffix += f" | ۷روز {_fa_digits(format(float(stat['change_7d_pct']), '+.2f'))}٪"
        lines.append(f"{emoji} {label} ➫ {_fa_digits(format(number, ',.0f'))} تومان{suffix}")
        shown += 1
    try:
        utopia_usd = float(ali.get("utopia_usd"))
        tether_irt = float(prices.get("USDT/IRT"))
        if utopia_usd > 0 and tether_irt > 0:
            lines.append(f"✨ یوتوپیا ➫ {_fa_digits(format(utopia_usd * tether_irt, ',.0f'))} تومان")
            shown += 1
    except (TypeError, ValueError):
        pass
    if shown == 0:
        return ""
    valid_changes = [v["change_24h_pct"] for v in trends.values() if isinstance(v.get("change_24h_pct"), (int, float))]
    up_count = sum(1 for x in valid_changes if x > 0)
    market_data = {"assets": trends, "up_assets": up_count, "assets_with_24h_data": len(valid_changes), "as_of": now.isoformat()}
    lines.extend(["", "〰️〰️〰️〰️〰️〰️〰️〰️", "🧠 جمع‌بندی بازار"])
    if len(valid_changes) >= 3:
        average = sum(valid_changes) / len(valid_changes)
        lines.append(f"📊 { _fa_digits(up_count) } از { _fa_digits(len(valid_changes)) } داراییِ دارای داده، در ۲۴ ساعت مثبت بوده‌اند.")
        lines.append("✦ حرکت کوتاه‌مدت: " + ("مثبت" if average > 0.6 else "منفی" if average < -0.6 else "ترکیبی/کم‌شتاب"))
    if deep and len(valid_changes) >= 3 and all(_env_values(*keys) for keys in (("GEMINI_API_KEY", "GEMINI_API_KEY_2"), ("GROQ_API_KEY", "GROQ_API_KEY_2"))):
        crypto_prompt = ("Analyze only the verified numeric crypto market and candle data in JSON. Do not invent prices, support/resistance, news, percentages or recommendations. Return JSON only: market_state (bullish/neutral/bearish), risk_level (low/medium/high), key_observations (array of 2 factual observations), main_risks (array), action (monitor/wait/avoid_chasing), confidence_pct (number 5-85). Do not promise returns.\nDATA:\n" + json.dumps(market_data, ensure_ascii=False)[:10000])
        gem = _call_gemini(crypto_prompt)
        groq = _call_groq(crypto_prompt)
        allowed_states = {"bullish", "neutral", "bearish"}
        allowed_actions = {"monitor", "wait", "avoid_chasing"}
        def valid_crypto_vote(vote: Any) -> bool:
            return isinstance(vote, dict) and vote.get("market_state") in allowed_states and vote.get("action") in allowed_actions
        votes: List[Dict[str, Any]] = [vote for vote in (gem, groq) if valid_crypto_vote(vote)]
        def crypto_key(vote: Dict[str, Any]) -> Tuple[str, str]:
            return str(vote.get("market_state")), str(vote.get("action"))
        if len(votes) == 2 and crypto_key(votes[0]) != crypto_key(votes[1]):
            # DeepSeek/OpenRouter is called only as a disagreement resolver.
            third = _call_openrouter(crypto_prompt)
            if valid_crypto_vote(third):
                votes.append(third)
        groups_by_decision: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for vote in votes:
            groups_by_decision.setdefault(crypto_key(vote), []).append(vote)
        winning_votes = max(groups_by_decision.values(), key=len) if groups_by_decision else []
        if len(winning_votes) >= 2:
            state, action = crypto_key(winning_votes[0])
            labels_state = {"bullish": "تمایل مثبت", "neutral": "خنثی/انتظار", "bearish": "تمایل منفی"}
            labels_risk = {"low": "پایین", "medium": "متوسط", "high": "بالا"}
            # Prefer Gemini's factual wording when it agrees; otherwise use the first agreeing engine.
            presenter = gem if valid_crypto_vote(gem) and crypto_key(gem) == (state, action) else winning_votes[0]
            risk_values = [v.get("risk_level") for v in winning_votes if v.get("risk_level") in {"low", "medium", "high"}]
            risk_label = labels_risk.get(max(set(risk_values), key=risk_values.count) if risk_values else "", "بررسی بیشتر")
            lines.append(f"🧭 ارزیابی مستقل: {labels_state[state]} | ریسک: {risk_label}")
            for observation in (presenter.get("key_observations") if isinstance(presenter.get("key_observations"), list) else [])[:2]:
                if isinstance(observation, str) and re.search(r"[؀-ۿ]", observation):
                    lines.append("✦ " + _safe_text(observation, 250))
            for risk in (presenter.get("main_risks") if isinstance(presenter.get("main_risks"), list) else [])[:1]:
                if isinstance(risk, str) and re.search(r"[؀-ۿ]", risk):
                    lines.append("⚠️ ریسک مهم: " + _safe_text(risk, 220))
            actions_map = {"monitor": "زیرنظرگرفتن بازار", "wait": "انتظار برای تأیید روند", "avoid_chasing": "پرهیز از تعقیب حرکت تند"}
            lines.append("📌 رویکرد: " + actions_map.get(action, "بررسی مجدد پیش از تصمیم"))
        else:
            lines.append("📌 سه بررسی به جمع‌بندی همسو نرسیدند؛ پیشنهاد جهت‌دار منتشر نمی‌شود.")
    else:
        lines.append("📌 جمع‌بندی بر پایهٔ تغییرات قابل‌اندازه‌گیری است؛ سیگنال عمیق فقط با دادهٔ کافی و توافق مستقل منتشر می‌شود.")
    lines.extend(["", "〰️〰️〰️〰️〰️〰️〰️〰️", _safe_text(_promo_line(now, 21), 280)])
    return "\n".join(lines)[:3900]


def _translate_news(items: List[Dict[str, str]], logger: Callable[[str], None]) -> List[Dict[str, str]]:
    if not items:
        return []
    payload = [{"title": x.get("headline", ""), "summary": x.get("summary", ""), "date": x.get("published", ""), "url": x.get("url", "")} for x in items[:6]]
    prompt = ("Translate these recent sports news items into natural, concise Persian. Do not add facts or infer details; if the summary contains little information, translate only the headline. "
              "Preserve people's names as conventional Persian transliterations. Return JSON only: {items:[{url,title_fa,summary_fa,date}]} and preserve each exact URL. Never output a raw source brand in prose unless it is part of the actual headline.\nDATA:\n" + json.dumps(payload, ensure_ascii=False)[:9000])
    translated = _call_gemini(prompt, logger)
    rows = translated.get("items") if isinstance(translated, dict) else None
    if not isinstance(rows, list):
        return []
    by_url = {str(x.get("url")): x for x in items}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if url not in by_url or not url.startswith(("https://", "http://")):
            continue
        title = str(row.get("title_fa") or "").strip()
        summary = str(row.get("summary_fa") or "").strip()
        # Do not leak untranslated English headlines to the Persian public channel.
        if not title or not re.search(r"[\u0600-\u06FF]", title):
            continue
        original = by_url[url]
        out.append({"title": title[:220], "summary": summary[:360] if re.search(r"[\u0600-\u06FF]", summary) else "",
                    "url": url, "published": str(row.get("date") or original.get("published") or "")[:80]})
    return out[:5]


def _sports_news_message(provider: SportsDataProvider, completion: DataCompletionProvider, now: dt.datetime) -> str:
    combined: List[Dict[str, str]] = []
    combined.extend(provider.news()[:5])
    tavily = completion.search_news(f"latest important football basketball tennis ice hockey news injury transfer results {now.date().isoformat()}", max_results=5)
    combined.extend(tavily)
    dedup: Dict[str, Dict[str, str]] = {}
    for item in combined:
        title = str(item.get("headline") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        sig = re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", title.lower())
        dedup.setdefault(sig, item)
    translated = _translate_news(list(dedup.values())[:6], lambda _message: None)
    if not translated:
        return ""  # Skip rather than publish raw English, diagnostics or fabricated news.
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐⁰¹ ➫ 〰╮", "         ⫹⫺ مهم‌ترین خبرهای ورزش", "╰〰💲 𓆩𓆩📰𓆪𓆪 💲〰╯", "",
             f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} تهران", ""]
    for idx, item in enumerate(translated[:4], 1):
        title = _safe_text(item["title"], 220)
        summary = _safe_text(item.get("summary", ""), 360)
        url = html.escape(item["url"], quote=True)
        lines.append(f"{_fa_digits(idx)}. 📰 <b>{title}</b>")
        if summary:
            lines.append(summary)
        lines.append(f'<a href="{url}">➭ ورود به خبر اصلی</a>')
        lines.append("〰️〰️〰️〰️〰️〰️〰️〰️")
    lines.extend(["💎 Vochino⁰¹ | خبرهای منتخب با خلاصهٔ فارسی", "〰️〰️〰️〰️〰️〰️〰️〰️", _safe_text(_promo_line(now, 13), 280)])
    return "\n".join(lines)[:3900]


def _translate_market_news(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not items:
        return []
    payload = [{"title": x.get("headline", ""), "summary": x.get("summary", ""), "date": x.get("published", ""), "url": x.get("url", "")} for x in items[:5]]
    prompt = ("Translate these current digital-asset market news items into concise natural Persian. Never add information, prices, predictions, or inferred facts. Preserve exact URLs. Return JSON only: {items:[{url,title_fa,summary_fa,date}]}.\nDATA:\n" + json.dumps(payload, ensure_ascii=False)[:7000])
    translated = _call_gemini(prompt)
    rows = translated.get("items") if isinstance(translated, dict) else None
    source_by_url = {str(x.get("url")): x for x in items}
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        title = str(row.get("title_fa") or "").strip()
        summary = str(row.get("summary_fa") or "").strip()
        if url not in source_by_url or not url.startswith("https://") or not re.search(r"[؀-ۿ]", title):
            continue
        out.append({"title": title[:200], "summary": summary[:280] if re.search(r"[؀-ۿ]", summary) else "", "url": url})
    return out[:3]


def _crypto_news_message(snapshot: Dict[str, Any], now: dt.datetime, completion: DataCompletionProvider) -> str:
    base = _crypto_report(snapshot, now, deep=False, completion=None)
    if not base:
        return ""
    items = completion.search_news("latest important cryptocurrency market news bitcoin ethereum crypto market", max_results=4)
    translated = _translate_market_news(items)
    if translated:
        # Strip the analysis footer and append a fresh rotating promo after the news.
        divider = "〰️〰️〰️〰️〰️〰️〰️〰️"
        if "\n" + divider in base:
            base = base.rsplit("\n" + divider, 1)[0]
        lines = [base.rstrip(), "", divider, "📰 مهم‌ترین خبرهای بازار دیجیتال"]
        for item in translated:
            lines.append("• <b>" + _safe_text(item["title"], 200) + "</b>")
            if item.get("summary"):
                lines.append(_safe_text(item["summary"], 280))
            lines.append('<a href="' + html.escape(item["url"], quote=True) + '">➭ ورود به خبر اصلی</a>')
        lines.extend(["", "〰️〰️〰️〰️〰️〰️〰️〰️", _safe_text(_promo_line(now, 19), 280)])
    else:
        # Quietly omit the news section when no translated, verifiable item is available.
        return base
    return "\n".join(lines)[:3900]


def _promo_line(now: dt.datetime, index: int = 0) -> str:
    """Rotate concise, factual calls-to-action; avoid operational guarantees we cannot verify."""
    messages = [
        "💎 نرخ آنلاین را بررسی کن و بعد تصمیم بگیر.\n🎟 خرید و سفارش از ربات VOCHINO⁰¹.",
        "🔷 قیمت پیش از ثبت سفارش قابل بررسی است.\n➭ ورود به ربات از دکمهٔ پایین پیام.",
        "💸 اول نرخ را ببین؛ بعد انتخاب کن.\n🎫 جزئیات خرید و فروش در ربات.",
        "✦ مسیر سفارش ساده و مستقیم است.\n💎 برای مشاهدهٔ گزینه‌ها وارد ربات شو.",
        "🎟 نرخ ووچر را بررسی کن؛ سپس سفارش بده.\n🔷 VOCHINO⁰¹ — ورود از دکمهٔ پایین.",
        "💎 انتخاب آگاهانه با دیدن نرخ شروع می‌شود.\n🎫 اطلاعات سفارش در ربات.",
        "🔹 برای قیمت روز، ربات را باز کن.\n🎟 خرید و فروش را از مسیر مشخص سفارش پیگیری کن.",
        "✦ نرخ را مقایسه کن و مطابق نیازت انتخاب کن.\n💎 ورود به ربات از دکمهٔ زیر پیام.",
        "🧾 پیش از ثبت، جزئیات سفارش را بررسی کن.\n🎟 شروع سفارش در ربات VOCHINO⁰¹.",
        "💠 نرخ روز ووچر در دسترس بررسی است.\n➭ برای خرید وارد ربات شو.",
        "🎯 مسابقه‌های منتخب؛ تحلیل مبتنی بر داده.\n💎 برای نرخ ووچر، ورود به ربات VOCHINO⁰¹ را بزن.",
        "🔍 قبل از سفارش، مبلغ و نرخ را بررسی کن.\n🎟 مسیر خرید از ربات در دسترس است.",
        "💎 ساده انتخاب کن؛ روشن سفارش بده.\n➭ نرخ و گزینه‌ها را در ربات ببین.",
        "📌 برای خرید، جزئیات سفارش را همان‌جا مرور کن.\n🎫 ورود به VOCHINO⁰¹ از دکمهٔ پایین.",
        "🔷 نرخ به‌روز را ببین و با آگاهی تصمیم بگیر.\n💎 خرید ووچر از ربات.",
        "🛒 گزینه‌های خرید و فروش را در ربات بررسی کن.\n🎟 دسترسی از دکمهٔ ورود زیر پیام.",
        "✦ اطلاعات روشن، انتخاب سنجیده‌تر.\n💎 نرخ ووچر را در ربات بررسی کن.",
        "🎟 برای مشاهدهٔ نرخ و ثبت سفارش، وارد ربات شو.\n🔹 پشتیبانی از دکمهٔ جداگانهٔ همین پیام.",
        "📈 تحلیل ورزشی؛ اول شواهد، بعد پیشنهاد.\n💎 برای سفارش ووچر، ربات در دسترس است.",
        "🔔 خبر و تحلیل منتخب، بدون شلوغی اضافه.\n🎟 نرخ خرید و فروش را در ربات ببین.",
        "💠 انتخاب ووچر را با بررسی نرخ آغاز کن.\n➭ ثبت سفارش از ربات VOCHINO⁰¹.",
        "🧭 قبل از تصمیم، شرایط سفارش را مرور کن.\n💎 ورود به ربات از دکمهٔ پایین.",
        "⚽ تحلیل منتخب امروز؛ داده‌محور و محتاطانه.\n🎟 نرخ ووچر از مسیر ربات قابل بررسی است.",
        "🤝 برای پیگیری سفارش از مسیر رسمی استفاده کن.\n💎 ورود و پشتیبانی ربات VOCHINO⁰¹ از دکمه‌های پایین.",
    ]
    slot = (now.timetuple().tm_yday * 7 + now.hour + index) % len(messages)
    return messages[slot]


def _single_pick_message(event: Dict[str, Any], analysis: Dict[str, Any], now: dt.datetime) -> str:
    sport_icon = {"soccer": "⚽", "basketball": "🏀", "hockey": "🏒", "tennis": "🎾"}.get(event.get("sport"), "🏆")
    home, away = _fa_team(event.get("home") or event.get("name")), _fa_team(event.get("away"))
    comp = _fa_league(event.get("league_code") or event.get("league"))
    market = _market_label_fa(analysis.get("market"), event.get("sport", ""))
    selection = _selection_fa(analysis.get("selection"), event)
    # Event timestamps may arrive in UTC; the public channel always shows Tehran time.
    start = event["start_dt"].astimezone(TZ).strftime("%H:%M")
    percent = max(1, min(95, int(round(float(analysis.get("probability_pct", 0))))))
    filled = round(percent / 10)
    bar = "▰" * filled + "▱" * (10-filled)
    event_start = event["start_dt"].astimezone(TZ)
    lines = ["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐⁰¹ ➫ 〰╮", "      تکی منتخب | تحلیل ۳۶۰ درجه", f"╰〰💲 𓆩𓆩{sport_icon}𓆪𓆪 💲〰╯", "",
             f"📅 {_fa_digits(_jalali_date(event_start))} | زمان انتشار {_fa_digits(now.strftime('%H:%M'))} تهران", "",
             f"{sport_icon} <b>{_safe_text(home, 100)} 🆚 {_safe_text(away, 100)}</b>",
             f"🏆 {_safe_text(comp, 90)} | 🕐 شروع {_fa_digits(_jalali_date(event_start))} — {_fa_digits(start)} تهران", "",
             "🎯 <b>بازار:</b> " + _safe_text(market, 140),
             "➭ <b>پیشنهاد:</b> " + _safe_text(selection, 130),
             f"💹 <b>ضریب واقعی:</b> {_fa_digits(format(float(analysis['odds']), '.2f'))} | {_safe_text(analysis.get('bookmaker') or 'بازار معتبر', 80)}",
             f"📊 برآورد محتاطانهٔ مدل: {_fa_digits(percent)}٪", f"💱 {bar} {_fa_digits(percent)}٪", ""]
    metric_group_names = {"shots": "شوت", "corners": "کرنر", "possession": "مالکیت", "xg": "گل موردانتظار", "goals": "گل", "passes": "پاس", "fouls": "خطا",
                          "cards": "کارت", "points": "امتیاز", "rebounds": "ریباند", "assists": "پاس گل", "turnovers": "از دست‌دادن توپ",
                          "saves": "مهار", "faceoffs": "فیس‌آف", "serve": "سرویس", "break_points": "بریک‌پوینت", "return": "ریترن"}
    groups = [metric_group_names.get(str(x), str(x)) for x in analysis.get("sport_metric_groups", [])]
    if groups:
        lines.append("📋 شاخص‌های بررسی‌شده: " + _safe_text("، ".join(groups[:6]), 160))
    form_summary = str(analysis.get("form_summary") or "").strip()
    if form_summary and re.search(r"[\u0600-\u06FF]", form_summary):
        lines.append("📚 <b>روند اخیر:</b> " + _safe_text(form_summary, 320))
    comparisons = analysis.get("key_comparisons") if isinstance(analysis.get("key_comparisons"), list) else []
    if comparisons:
        lines.append("⚖️ <b>مقایسهٔ آماری:</b>")
        for comparison in comparisons[:3]:
            if isinstance(comparison, str) and re.search(r"[\u0600-\u06FF]", comparison):
                lines.append("➭ " + _safe_text(comparison, 220))
    market_reasoning = str(analysis.get("market_reasoning") or "").strip()
    if market_reasoning and re.search(r"[\u0600-\u06FF]", market_reasoning):
        lines.append("💡 <b>منطق انتخاب بازار:</b> " + _safe_text(market_reasoning, 300))
    market_implied = analysis.get("market_implied_probability")
    try:
        market_implied = float(market_implied)
        if 0 < market_implied < 1:
            gap = percent - market_implied * 100
            lines.append(f"📈 برتری تخمینی نسبت به احتمال منصفانهٔ بازار: {_fa_digits(format(gap, '+.1f'))} واحد درصد")
    except (TypeError, ValueError):
        pass
    stamp = _parse_time(analysis.get("odds_updated_at"))
    if stamp:
        lines.append(f"🕒 آخرین به‌روزرسانی ضریب: {_fa_digits(stamp.astimezone(TZ).strftime('%H:%M'))} تهران")
    risk = str(analysis.get("risk") or "").strip()
    if risk and re.search(r"[\u0600-\u06FF]", risk):
        lines.append("⚠️ ریسک اصلی: " + _safe_text(risk, 240))
    rationale = str(analysis.get("rationale") or "").strip()
    if rationale and re.search(r"[\u0600-\u06FF]", rationale):
        lines.append("🧠 <b>جمع‌بندی:</b> " + _safe_text(rationale, 460))
    injuries = _known_injuries(analysis.get("availability"))
    if injuries:
        lines.append("🩹 وضعیت بازیکنان: " + _safe_text("؛ ".join(injuries[:2]), 240))
    lines.extend(["", "〰️〰️〰️〰️〰️〰️〰️〰️", _safe_text(_promo_line(now, 2), 250), "⚠️ برآورد تحلیلی است و نتیجهٔ مسابقه تضمین‌شده نیست."])
    return "\n".join(lines)[:3900]


def _build_mix(qualified: Sequence[Dict[str, Any]], now: dt.datetime, mode: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    picks=[]; seen=set()
    for item in qualified:
        if item.get("event_id") in seen: continue
        try: odd=float(item.get("odds")); prob=float(item.get("probability_pct"))
        except (TypeError,ValueError): continue
        if odd<=1 or not 1<prob<96 or not item.get("market_id") or not item.get("bookmaker") or not item.get("event_id"): continue
        seen.add(item["event_id"]); picks.append({**item,"odds":odd,"probability_pct":prob})
    if len(picks)<3: return None
    if mode=="balanced":
        picks.sort(key=lambda p:(-p["probability_pct"],p["odds"]))
        low,high=2.8,4.8; name="میکس متعادل"; sub="ترکیب کوتاه با تأکید بر شواهد و انتخاب‌های محتاطانه‌تر."
    else:
        picks.sort(key=lambda p:(-(p["probability_pct"]-float(p.get("market_implied_probability",0))*100),-p["probability_pct"]))
        low,high=3.2,6.0; name="میکس ارزش"; sub="ترکیب انتخاب‌های دارای شواهد و ارزش نسبی بازار؛ نه تعقیب ضریب بالا."
    chosen=[]; total=1.0
    for item in picks:
        if len(chosen)>=4: break
        proposed=total*item["odds"]
        if proposed<=high:
            chosen.append(item); total=proposed
    if len(chosen)<3 or not low<=total<=high: return None
    combined=1.0
    for item in chosen: combined*=item["probability_pct"]/100.0
    percent=round(combined*100)
    lines=["╭〰 ➫ 𝑽𝒐𝒄𝒉𝒊𝒏𝒐⁰¹ ➫ 〰╮",f"      🎯 {name}","╰〰💲 𓆩𓆩🎟𓆪𓆪 💲〰╯","",
           f"📅 {_fa_digits(_jalali_date(now))} | ⏰ {_fa_digits(now.strftime('%H:%M'))} تهران",sub,"〰️〰️〰️〰️〰️〰️〰️〰️"]
    for idx,item in enumerate(chosen,1):
        icon={"soccer":"⚽","tennis":"🎾","hockey":"🏒","basketball":"🏀"}.get(item.get("sport"),"🏆")
        start_text = ""
        try:
            start_dt = _parse_time(item.get("start"))
            if start_dt:
                start_text = f"\n🕐 شروع: {_fa_digits(_jalali_date(start_dt))} — {_fa_digits(start_dt.astimezone(TZ).strftime('%H:%M'))} تهران"
        except Exception:
            start_text = ""
        lines.extend([f"{_fa_digits(idx)}. {icon} {_fa_team(item.get('home'))} 🆚 {_fa_team(item.get('away'))}",
                      "🎯 "+_safe_text(_market_label_fa(item.get("market"),item.get("sport","")),80)+": "+_safe_text(_selection_fa(item.get("selection"),{"home":item.get("home"),"away":item.get("away")}),100),
                      f"💹 ضریب واقعی: {_fa_digits(format(item['odds'],'.2f'))}" + start_text,"〰️〰️〰️〰️〰️〰️〰️〰️"])
    fill=round(percent/10)
    lines.extend([f"🎟 <b>ضریب کل واقعی:</b> {_fa_digits(format(total,'.2f'))}",f"📊 برآورد ترکیبی مدل: {_fa_digits(percent)}٪",
                  "💱 "+"▰"*fill+"▱"*(10-fill)+f" {_fa_digits(percent)}٪","〰️〰️〰️〰️〰️〰️〰️〰️",
                  _safe_text(_promo_line(now,4 if mode=="value" else 1),260),"⚠️ احتمال ترکیبی تخمینی و مبتنی بر فرض استقلال تقریبی انتخاب‌هاست؛ برد یا سود تضمین‌شده نیست."])
    record={"kind":mode,"count":len(chosen),"total_odds":round(total,4),"estimated_probability_pct":percent,"event_ids":[p.get("event_id") for p in chosen]}
    return "\n".join(lines)[:3900],record


def _event_summary_line(event: Dict[str, Any]) -> str:
    start = event["start_dt"].strftime("%H:%M")
    teams = " - ".join(x for x in (event.get("home"), event.get("away")) if x)
    if not teams:
        teams = event.get("name") or "نام رویداد در منبع کامل نیست"
    score = ""
    if event.get("home_score") is not None or event.get("away_score") is not None:
        score = f" | {event.get('home_score', '—')} : {event.get('away_score', '—')}"
    return f"• {_safe_text(teams, 120)} | {_safe_text(event['league'], 80)} | {_fa_digits(start)} | {_safe_text(event['status'], 100)}{score}"


def _daily_brief_legacy(provider: SportsDataProvider, now: dt.datetime, snapshot: Optional[Dict[str, Any]] = None) -> str:
    return _daily_brief(provider, now, snapshot)


def _crypto_report_legacy(snapshot: Dict[str, Any], now: dt.datetime) -> str:
    return _crypto_report(snapshot, now, deep=False, completion=None)


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


def _single_pick_message_legacy(event: Dict[str, Any], analysis: Dict[str, Any], now: dt.datetime) -> str:
    return _single_pick_message(event, analysis, now)


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
    """Tehran-local crypto schedule: deep analysis 14:30; price/news digest 21:00."""
    local = now.astimezone(TZ)
    if local.hour == 14 and 30 <= local.minute < 37:
        return "deep"
    if local.hour == 21 and 0 <= local.minute < 7:
        return "news"
    return None


def _sports_daily_due(now: dt.datetime) -> bool:
    return now.hour >= 16


def _in_silent_period(now: dt.datetime) -> bool:
    return 1 <= now.hour < 12


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


def _save_evidence(path: str, day: str, event: Dict[str, Any], evidence: Dict[str, Any], logger: Callable[[str], None]) -> None:
    """Store bounded, de-identified event evidence outside the voucher STATE file."""
    target = Path(path + ".evidence.json")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {"version": 1, "events": {}}
        try:
            with target.open("r", encoding="utf-8") as fh:
                prior = json.load(fh)
            if isinstance(prior, dict) and isinstance(prior.get("events"), dict):
                data = prior
        except (FileNotFoundError, ValueError, OSError):
            pass
        key = day + ":" + str(event.get("id"))
        data["events"][key] = {"stored_at": _now().isoformat(), "event": {
            "id": str(event.get("id")), "sport": event.get("sport"), "league_id": event.get("league_id"),
            "home": event.get("home"), "away": event.get("away"), "start": event.get("start_dt").isoformat() if event.get("start_dt") else None},
            "evidence": _trim_json(evidence, max_depth=6, max_items=60)}
        # Keep the latest 40 events only to bound the file; secrets are never persisted.
        if len(data["events"]) > 40:
            ordered = sorted(data["events"].items(), key=lambda kv: str(kv[1].get("stored_at", "")), reverse=True)
            data["events"] = dict(ordered[:40])
        fd, tmp = tempfile.mkstemp(prefix=".vochino-evidence-", suffix=".tmp", dir=str(target.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, target)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
    except Exception as exc:
        _log(logger, f"evidence snapshot not persisted ({type(exc).__name__})")


def _merge_recent_forms(primary: Any, fallback: Any) -> Dict[str, Any]:
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    merged: Dict[str, Any] = {}
    for side in ("home", "away"):
        one = primary.get(side) if isinstance(primary.get(side), list) else []
        two = fallback.get(side) if isinstance(fallback.get(side), list) else []
        seen = set(); rows = []
        for row in one + two:
            if not isinstance(row, dict) or not _row_is_final(row):
                continue
            try:
                hs, aws = float(row.get("home_score")), float(row.get("away_score"))
            except (TypeError, ValueError):
                continue
            stamp = _parse_time(row.get("date"))
            if stamp is None or stamp >= _now():
                continue
            sig = str(row.get("id") or (stamp.isoformat(), row.get("home"), row.get("away"), hs, aws))
            if sig in seen:
                continue
            seen.add(sig)
            clean = dict(row); clean["home_score"] = hs; clean["away_score"] = aws; clean["status"] = "finished"
            rows.append(clean)
        rows.sort(key=lambda row: str(row.get("date", "")), reverse=True)
        merged[side] = rows[:5]
    stats = {}
    for block in (primary.get("stats"), fallback.get("stats")):
        if isinstance(block, dict): stats.update(block)
    if stats: merged["stats"] = stats
    return merged


def _event_evidence_stats(event: Dict[str, Any], summary: Dict[str, Any], form: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    return {"event": {"id": event.get("id"), "sport": event.get("sport"), "league": event.get("league"),
                       "home": event.get("home"), "away": event.get("away"), "start": event.get("start_dt").isoformat() if event.get("start_dt") else None},
            "summary": summary.get("data") if isinstance(summary, dict) else {}, "recent_form": form,
            "supplemental": extra}


def _single_spacing_ready(ledger: "AnalyzerLedger", event: Dict[str, Any], now: dt.datetime) -> bool:
    """Limit distinct single-pick posts to one per 20 minutes; same-kickoff events may be 2 minutes apart."""
    sent = ledger.data.setdefault("sent", {})
    rows = [row for row in sent.values() if isinstance(row, dict) and row.get("kind") == "single" and row.get("sent_at")]
    if not rows:
        return True
    latest = max(rows, key=lambda row: str(row.get("sent_at", "")))
    stamp = _parse_time(latest.get("sent_at"))
    if stamp is None:
        return True
    previous_start = _parse_time(latest.get("start"))
    current_start = event.get("start_dt")
    same_kickoff = bool(previous_start and current_start and previous_start.replace(second=0, microsecond=0) == current_start.astimezone(TZ).replace(second=0, microsecond=0))
    minimum_gap = 120 if same_kickoff else 20 * 60
    elapsed = (now.astimezone(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)).total_seconds()
    return elapsed >= minimum_gap


def _worker(state_snapshot: Callable[[], Dict[str, Any]], send: Callable[[str], bool], shutdown: threading.Event,
            logger: Callable[[str], None], state_file: str) -> None:
    ledger = AnalyzerLedger(state_file, logger)
    provider = SportsDataProvider(logger)
    odds_provider = OddsProvider(logger)
    completion = DataCompletionProvider(logger)
    sports_enabled = os.getenv("SPORTS_ANALYZER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    crypto_enabled = os.getenv("CRYPTO_ANALYZER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    last_refresh = 0.0
    last_refresh_day = ""

    def slot_due(now: dt.datetime, hour: int, minute: int = 0, window: int = 7) -> bool:
        return now.hour == hour and minute <= now.minute < minute + window

    def refresh_events(now: dt.datetime, force: bool = False) -> List[Dict[str, Any]]:
        nonlocal last_refresh, last_refresh_day
        today = _date_key(now)
        if force or not last_refresh or today != last_refresh_day or time.time() - last_refresh >= 4 * 60 * 60:
            provider.refresh_today(now)
            last_refresh = time.time(); last_refresh_day = today
        events, _, _, stale = provider.snapshot_events()
        if stale and not force:
            provider.refresh_today(now)
            last_refresh = time.time(); last_refresh_day = today
            events, _, _, _ = provider.snapshot_events()
        # Live Tennis adds schedule rows that are absent from ESPN. It never replaces ESPN rows.
        try:
            extra_tennis = completion.upcoming_tennis_events(now)
        except Exception as exc:
            _log(logger, f"tennis schedule fallback failed ({type(exc).__name__})")
            extra_tennis = []
        by_id = {str(item.get("id")): item for item in events}
        for item in extra_tennis:
            sig = (_normalise_name(item.get("home")), _normalise_name(item.get("away")))
            existing = next((e for e in by_id.values() if e.get("sport") == "tennis" and
                             { _normalise_name(e.get("home")), _normalise_name(e.get("away")) } == set(sig) and
                             abs((e["start_dt"] - item["start_dt"]).total_seconds()) <= 3 * 60 * 60), None)
            if not existing:
                by_id[str(item["id"])] = item
        merged = sorted(by_id.values(), key=lambda item: item.get("start_dt") or now)
        with provider._lock:
            provider._events = copy.deepcopy(merged)
            provider._received_at = time.time()
        return merged

    def prepare_event(event: Dict[str, Any], deep: bool) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        summary = provider.summary(event)
        extra = completion.enrich(event, summary, deep=deep)
        # Historical detail is only retrieved for selected matches, with per-process caps/caches.
        history = _espn_recent_form(provider, event, deep=deep) if deep else {"home": [], "away": [], "stats": {}}
        extra["recent_form"] = _merge_recent_forms(extra.get("recent_form"), history)
        if deep and not _has_five_recent(extra.get("recent_form")):
            try:
                supplemental_form = completion._prop_recent_form(event, deep=True)
                extra["recent_form"] = _merge_recent_forms(extra.get("recent_form"), supplemental_form)
                if _has_five_recent(extra.get("recent_form")):
                    extra.setdefault("sources", []).append("PropLine recent scores/box scores")
            except Exception as exc:
                _log(logger, f"supplemental history failed for {event.get('id')} ({type(exc).__name__})")
        extra["historical_stats"] = history.get("stats", {})
        # Use a bounded, trusted-source search for injuries and line-up reports only on analysis candidates.
        if deep:
            context = completion.extra_market_context(event)
            results = context.get("query_results") if isinstance(context, dict) else []
            if results:
                # Keep raw search snippets as untrusted evidence; models must not treat them as verified injuries.
                extra["injury_context"] = [{"headline": x.get("headline"), "summary": x.get("summary"),
                                             "published": x.get("published"), "url": x.get("url")} for x in results[:4]]
        _save_evidence(state_file, _date_key(_now()), event, _event_evidence_stats(event, summary, extra.get("recent_form", {}), extra), logger)
        return summary, extra, history

    def send_once(key: str, message: str, record: Dict[str, Any]) -> bool:
        if not message or ledger.already_sent(key):
            return False
        if not send(message):
            return False
        ledger.record_sent(key, record)
        return True

    _log(logger, "scheduler policy: Tehran timezone, public quiet period 01:00-12:00, bounded API/AI calls")
    while not shutdown.is_set():
        now = _now()
        try:
            events = refresh_events(now)
            if not _in_silent_period(now):
                day = _date_key(now)

                # Sport news: two compact Persian news digests, links hidden behind an inline phrase.
                if sports_enabled and (slot_due(now, 13, 0) or slot_due(now, 18, 30)):
                    slot = "midday" if now.hour == 13 else "evening"
                    key = f"{day}:sports-news:{slot}"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 30):
                        ledger.record_attempt(key, now)
                        try:
                            msg = _sports_news_message(provider, completion, now)
                            if send_once(key, msg, {"kind": "sports_news", "slot": slot}):
                                _log(logger, f"sports news sent ({slot})")
                        except Exception as exc:
                            _log(logger, f"sports news pass failed ({type(exc).__name__})")

                # At 16:00 select 4-6 notable events, with no forced picks at this stage.
                if sports_enabled and slot_due(now, 16, 0):
                    key = f"{day}:shortlist"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 20):
                        future = [e for e in events if e.get("start_dt") and e["start_dt"].astimezone(TZ).date() == now.date()
                                  and e["start_dt"] > now and e.get("state") not in ("post", "final", "in", "live")]
                        candidates = sorted(future, key=_event_priority, reverse=True)[:6]
                        summaries: Dict[str, Dict[str, Any]] = {}; forms: Dict[str, Dict[str, Any]] = {}
                        if candidates:
                            for event in candidates:
                                try:
                                    summary, extra, history = prepare_event(event, False)
                                    summaries[str(event["id"])] = summary
                                    forms[str(event["id"])] = extra.get("recent_form", {})
                                except Exception as exc:
                                    _log(logger, f"shortlist enrichment failed for {event.get('id')} ({type(exc).__name__})")
                            # save candidates for a stable downstream schedule even after refresh.
                            ledger.data.setdefault("shortlists", {})[day] = [{"id": str(e["id"]), "start": e["start_dt"].isoformat(),
                                                                               "sport": e.get("sport"), "league_id": e.get("league_id"),
                                                                               "home": e.get("home"), "away": e.get("away")} for e in candidates]
                            ledger.save()
                            msg = _shortlist_message(candidates, summaries, forms, now)
                            if send_once(key, msg, {"kind": "shortlist", "count": len(candidates)}):
                                _log(logger, f"shortlist sent ({len(candidates)} event(s))")

                # Analyze only shortlisted matches about 40-70 minutes before kickoff.
                if sports_enabled:
                    shortlist_rows = ledger.data.setdefault("shortlists", {}).get(day, [])
                    shortlist_ids = {str(row.get("id")) for row in shortlist_rows if isinstance(row, dict)}
                    event_map = {str(e.get("id")): e for e in events}
                    sent_today = sum(1 for k in ledger.data.setdefault("sent", {}) if k.startswith(day + ":single:") )
                    ready = [event_map[eid] for eid in shortlist_ids if eid in event_map]
                    ready.sort(key=lambda e: e.get("start_dt") or now)
                    due_candidates = []
                    for event in ready:
                        if sent_today >= 5:
                            break
                        start = event.get("start_dt")
                        if not start or start.astimezone(TZ).date() != now.date() or event.get("state") in ("post", "final", "in", "live"):
                            continue
                        mins = (start - now).total_seconds() / 60.0
                        if 38 <= mins <= 72 and not ledger.already_sent(f"{day}:single:{event['id']}") and not ledger.attempt_recently(str(event['id']), now, 25):
                            due_candidates.append(event)
                    for index, event in enumerate(due_candidates[:5]):
                        if index and due_candidates[index-1]["start_dt"].replace(second=0,microsecond=0) == event["start_dt"].replace(second=0,microsecond=0):
                            if shutdown.wait(120): break
                        key = f"{day}:single:{event['id']}"
                        if ledger.already_sent(key) or ledger.attempt_recently(str(event['id']), now, 25):
                            continue
                        if not _single_spacing_ready(ledger, event, now):
                            continue
                        ledger.record_attempt(str(event["id"]), now)
                        try:
                            summary, extra, history = prepare_event(event, True)
                            markets = _market_inventory(event, summary, odds_provider, completion)
                            analysis = _deep_consensus_analysis(event, summary, extra, markets, logger)
                            if not analysis:
                                continue
                            # Only structured provider availability is published as an injury fact.
                            # Tavily snippets remain internal leads and are never labeled confirmed.
                            analysis["availability"] = extra.get("availability") or extra.get("injuries")
                            message = _single_pick_message(event, analysis, _now())
                            record = {"event_id": str(event["id"]), "sport": event.get("sport"), "home": event.get("home"),
                                      "away": event.get("away"), "league": event.get("league"), "start": event["start_dt"].isoformat(),
                                      "market_id": analysis.get("market_id"), "market": analysis.get("market"), "sport_metric_groups": analysis.get("sport_metric_groups"),
                                      "selection": analysis.get("selection"), "odds": analysis.get("odds"), "probability_pct": analysis.get("probability_pct"),
                                      "market_implied_probability": analysis.get("market_implied_probability"), "bookmaker": analysis.get("bookmaker"),
                                      "rationale": analysis.get("rationale"), "risk": analysis.get("risk")}
                            if send_once(key, message, {"kind": "single", **record}):
                                ledger.add_qualified_pick(day, record)
                                sent_today += 1
                                _log(logger, f"single analysis sent for event {event['id']}")
                        except Exception as exc:
                            _log(logger, f"deep analysis failed for event {event.get('id')} ({type(exc).__name__})")

                # Two intentionally different daily mixes; a mix is not forced without >=3 qualified legs.
                if sports_enabled and slot_due(now, 20, 30):
                    key = f"{day}:mix:balanced"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 30):
                        ledger.record_attempt(key, now)
                        mixed = _build_mix(ledger.qualified_picks(day), now, "balanced")
                        if mixed and send_once(key, mixed[0], mixed[1]):
                            _log(logger, "balanced mix sent")
                if sports_enabled and slot_due(now, 22, 20):
                    key = f"{day}:mix:value"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 30):
                        ledger.record_attempt(key, now)
                        mixed = _build_mix(ledger.qualified_picks(day), now, "value")
                        if mixed and send_once(key, mixed[0], mixed[1]):
                            _log(logger, "value mix sent")

                # Crypto: one deeper evidence-based market report and one price/news digest.
                if crypto_enabled and slot_due(now, 14, 30):
                    key = f"{day}:crypto:deep"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 30):
                        ledger.record_attempt(key, now)
                        try:
                            snapshot = state_snapshot()
                            msg = _crypto_report(snapshot if isinstance(snapshot, dict) else {}, now, deep=True, completion=completion)
                            if send_once(key, msg, {"kind": "crypto_deep"}):
                                _log(logger, "deep crypto report sent")
                        except Exception as exc:
                            _log(logger, f"crypto deep report failed ({type(exc).__name__})")
                if crypto_enabled and slot_due(now, 21, 0):
                    key = f"{day}:crypto:news"
                    if not ledger.already_sent(key) and not ledger.attempt_recently(key, now, 30):
                        ledger.record_attempt(key, now)
                        try:
                            snapshot = state_snapshot()
                            msg = _crypto_news_message(snapshot if isinstance(snapshot, dict) else {}, now, completion)
                            if send_once(key, msg, {"kind": "crypto_news"}):
                                _log(logger, "crypto market report/news sent")
                        except Exception as exc:
                            _log(logger, f"crypto market report/news failed ({type(exc).__name__})")
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
