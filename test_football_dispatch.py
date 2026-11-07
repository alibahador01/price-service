import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import football_client  # noqa: E402
import football_dispatch as fd  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    TEHRAN = None


def bzzoiro_raw(event_id="1", kickoff="2026-11-05T15:00:00Z"):
    return {
        "id": event_id, "league": {"name": "Premier League"},
        "home_team": {"id": "h1", "name": "Liverpool"},
        "away_team": {"id": "a1", "name": "Arsenal"},
        "kickoff_utc": kickoff, "status": "upcoming",
    }


def football_data_raw(event_id=1, kickoff="2026-11-05T15:00:00Z"):
    return {
        "id": event_id, "competition": {"name": "Premier League"},
        "homeTeam": {"name": "Liverpool"}, "awayTeam": {"name": "Arsenal"},
        "utcDate": kickoff, "status": "SCHEDULED",
    }


class TestFetchActiveProviderEvents(unittest.TestCase):
    def test_bzzoiro_success_stays_on_bzzoiro(self):
        state = football_client.initial_provider_state()

        def bzz(date_from, date_to, api_key):
            return [bzzoiro_raw()]

        def fd_fn(date_from, date_to, api_key):
            self.fail("fallback should not be called when primary succeeds")

        events, new_state = fd.fetch_active_provider_events(
            provider_state=state, bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            date_from="2026-11-01", date_to="2026-11-08",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(new_state["active_provider"], "bzzoiro")

    def test_bzzoiro_failure_falls_back_same_cycle_after_threshold(self):
        state = football_client.initial_provider_state()
        state["consecutive_bzzoiro_failures"] = football_client.BZZOIRO_FAILURE_THRESHOLD - 1

        def bzz(date_from, date_to, api_key):
            raise RuntimeError("timeout")

        def fd_fn(date_from, date_to, api_key):
            return [football_data_raw()]

        events, new_state = fd.fetch_active_provider_events(
            provider_state=state, bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            date_from="2026-11-01", date_to="2026-11-08",
        )
        self.assertEqual(new_state["active_provider"], "football_data")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["provider"], "football_data")

    def test_both_fail_returns_empty_not_crash(self):
        state = football_client.initial_provider_state()
        state["consecutive_bzzoiro_failures"] = football_client.BZZOIRO_FAILURE_THRESHOLD - 1

        def bzz(date_from, date_to, api_key):
            raise RuntimeError("down")

        def fd_fn(date_from, date_to, api_key):
            raise RuntimeError("also down")

        events, new_state = fd.fetch_active_provider_events(
            provider_state=state, bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            date_from="2026-11-01", date_to="2026-11-08",
        )
        self.assertEqual(events, [])

    def test_recovery_probe_switches_back_mid_cycle(self):
        state = football_client.initial_provider_state()
        state["active_provider"] = "football_data"
        state["consecutive_bzzoiro_successes"] = football_client.BZZOIRO_RECOVERY_SUCCESSES - 1

        def bzz(date_from, date_to, api_key):
            return [bzzoiro_raw()]

        def fd_fn(date_from, date_to, api_key):
            self.fail("should not need fallback once probe recovers")

        events, new_state = fd.fetch_active_provider_events(
            provider_state=state, bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            date_from="2026-11-01", date_to="2026-11-08",
        )
        self.assertEqual(new_state["active_provider"], "bzzoiro")
        self.assertEqual(events[0]["provider"], "bzzoiro")

    def test_never_merges_both_providers_in_one_cycle(self):
        state = football_client.initial_provider_state()

        def bzz(date_from, date_to, api_key):
            return [bzzoiro_raw()]

        def fd_fn(date_from, date_to, api_key):
            return [football_data_raw()]

        events, _ = fd.fetch_active_provider_events(
            provider_state=state, bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            date_from="2026-11-01", date_to="2026-11-08",
        )
        providers = {e["provider"] for e in events}
        self.assertEqual(providers, {"bzzoiro"})  # هرگز هر دو با هم


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestRefreshFixturesCycle(unittest.TestCase):
    def test_full_cycle_produces_pending_fixture(self):
        state = football_client.initial_provider_state()
        now = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)

        def bzz(date_from, date_to, api_key):
            return [bzzoiro_raw()]

        def fd_fn(date_from, date_to, api_key):
            return []

        new_fixtures, new_provider_state, actions = fd.refresh_fixtures_cycle(
            provider_state=state, fixtures_state=[],
            bzzoiro_api_key="x", football_data_api_key="y",
            fetch_bzzoiro_fn=bzz, fetch_football_data_fn=fd_fn,
            local_date=date(2026, 11, 5), tz=TEHRAN, now=now,
            date_from="2026-11-05", date_to="2026-11-12",
        )
        self.assertEqual(len(new_fixtures), 1)
        self.assertEqual(new_fixtures[0]["status"], "pending")
        self.assertEqual(actions, [])


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestBuildMatchSendPayload(unittest.TestCase):
    def make_entry(self):
        kickoff = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
        return {
            "provider": "bzzoiro", "event_id": "1", "league_name": "Premier League",
            "home_team": "Liverpool", "away_team": "Arsenal",
            "home_team_id": "10", "away_team_id": "11",
            "kickoff_utc": kickoff.isoformat(),
        }

    def test_caption_and_image_with_license(self):
        entry = self.make_entry()
        payload = fd.build_match_send_payload(entry, license_confirmed=True, tz=TEHRAN)
        self.assertIn("Liverpool", payload["caption"])
        self.assertIn("Arsenal", payload["caption"])
        self.assertEqual(payload["image"]["status"], "ready")

    def test_image_unavailable_without_license(self):
        entry = self.make_entry()
        payload = fd.build_match_send_payload(entry, license_confirmed=False, tz=TEHRAN)
        self.assertEqual(payload["image"]["status"], "image_unavailable")
        # کپشن همچنان ساخته می‌شود؛ فقط عکس مسدود است
        self.assertIn("Liverpool", payload["caption"])

    def test_uses_canonical_persian_league_name(self):
        entry = self.make_entry()
        payload = fd.build_match_send_payload(entry, license_confirmed=True, tz=TEHRAN)
        self.assertIn("لیگ برتر انگلیس", payload["caption"])


if __name__ == "__main__":
    unittest.main()
