import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import football_client as fcli  # noqa: E402


class TestNormalizeBzzoiro(unittest.TestCase):
    def test_valid_event_normalized(self):
        raw = {
            "id": 555, "league": {"name": "Premier League"},
            "home_team": {"id": 10, "name": "Liverpool"},
            "away_team": {"id": 11, "name": "Arsenal"},
            "kickoff_utc": "2026-11-05T19:00:00Z", "status": "upcoming",
        }
        event = fcli.normalize_bzzoiro_event(raw)
        self.assertEqual(event["provider"], "bzzoiro")
        self.assertEqual(event["event_id"], "555")
        self.assertEqual(event["home_team_id"], "10")
        self.assertTrue(event["schedulable"])
        self.assertEqual(event["kickoff_utc"].tzinfo, timezone.utc)

    def test_non_upcoming_status_not_schedulable(self):
        raw = {
            "id": 1, "league": {"name": "La Liga"},
            "home_team": {"id": 1, "name": "A"}, "away_team": {"id": 2, "name": "B"},
            "kickoff_utc": "2026-11-05T19:00:00Z", "status": "postponed",
        }
        event = fcli.normalize_bzzoiro_event(raw)
        self.assertFalse(event["schedulable"])

    def test_missing_required_field_returns_none(self):
        raw = {"id": 1, "league": {"name": "La Liga"}}
        self.assertIsNone(fcli.normalize_bzzoiro_event(raw))

    def test_invalid_kickoff_returns_none(self):
        raw = {
            "id": 1, "league": {"name": "La Liga"},
            "home_team": {"id": 1, "name": "A"}, "away_team": {"id": 2, "name": "B"},
            "kickoff_utc": "not-a-date", "status": "upcoming",
        }
        self.assertIsNone(fcli.normalize_bzzoiro_event(raw))


class TestNormalizeFootballData(unittest.TestCase):
    def test_valid_match_normalized(self):
        raw = {
            "id": 999, "competition": {"name": "Bundesliga"},
            "homeTeam": {"name": "Bayern München"}, "awayTeam": {"name": "Borussia Dortmund"},
            "utcDate": "2026-11-06T18:30:00Z", "status": "SCHEDULED",
        }
        event = fcli.normalize_football_data_match(raw)
        self.assertEqual(event["provider"], "football_data")
        self.assertIsNone(event["home_team_id"])  # هرگز با Bzzoiro ID یکی فرض نشود
        self.assertTrue(event["schedulable"])

    def test_in_play_not_schedulable(self):
        raw = {
            "id": 1, "competition": {"name": "Serie A"},
            "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"},
            "utcDate": "2026-11-06T18:30:00Z", "status": "IN_PLAY",
        }
        event = fcli.normalize_football_data_match(raw)
        self.assertFalse(event["schedulable"])


class TestDedupeFingerprint(unittest.TestCase):
    def test_same_match_different_providers_same_fingerprint(self):
        e1 = {"home_team": "Liverpool", "away_team": "Arsenal",
              "kickoff_utc": datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)}
        e2 = {"home_team": "liverpool", "away_team": "ARSENAL",
              "kickoff_utc": datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)}
        self.assertEqual(fcli.dedupe_fingerprint(e1), fcli.dedupe_fingerprint(e2))

    def test_different_kickoff_different_fingerprint(self):
        e1 = {"home_team": "Liverpool", "away_team": "Arsenal",
              "kickoff_utc": datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)}
        e2 = {"home_team": "Liverpool", "away_team": "Arsenal",
              "kickoff_utc": datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)}
        self.assertNotEqual(fcli.dedupe_fingerprint(e1), fcli.dedupe_fingerprint(e2))


class TestCircuitBreaker(unittest.TestCase):
    def test_starts_on_bzzoiro(self):
        state = fcli.initial_provider_state()
        self.assertEqual(state["active_provider"], "bzzoiro")

    def test_failover_after_threshold_failures(self):
        state = fcli.initial_provider_state()
        for _ in range(fcli.BZZOIRO_FAILURE_THRESHOLD):
            state = fcli.record_bzzoiro_attempt(state, success=False, reason="timeout")
        self.assertEqual(state["active_provider"], "football_data")
        self.assertEqual(state["last_failover_reason"], "timeout")

    def test_no_failover_below_threshold(self):
        state = fcli.initial_provider_state()
        state = fcli.record_bzzoiro_attempt(state, success=False, reason="timeout")
        self.assertEqual(state["active_provider"], "bzzoiro")

    def test_recovery_after_consecutive_successes(self):
        state = fcli.initial_provider_state()
        for _ in range(fcli.BZZOIRO_FAILURE_THRESHOLD):
            state = fcli.record_bzzoiro_attempt(state, success=False)
        self.assertEqual(state["active_provider"], "football_data")
        for _ in range(fcli.BZZOIRO_RECOVERY_SUCCESSES):
            state = fcli.record_bzzoiro_attempt(state, success=True)
        self.assertEqual(state["active_provider"], "bzzoiro")

    def test_single_success_during_fallback_does_not_recover_yet(self):
        state = fcli.initial_provider_state()
        for _ in range(fcli.BZZOIRO_FAILURE_THRESHOLD):
            state = fcli.record_bzzoiro_attempt(state, success=False)
        state = fcli.record_bzzoiro_attempt(state, success=True)
        self.assertEqual(state["active_provider"], "football_data")

    def test_failure_resets_success_streak(self):
        state = fcli.initial_provider_state()
        state = fcli.record_bzzoiro_attempt(state, success=True)
        state = fcli.record_bzzoiro_attempt(state, success=False)
        self.assertEqual(state["consecutive_bzzoiro_successes"], 0)


class TestFetchMocked(unittest.TestCase):
    @mock.patch("football_client.request_with_retry")
    def test_fetch_bzzoiro_events_single_page(self, mock_request):
        mock_response = mock.Mock()
        mock_response.json.return_value = {"results": [{"id": 1}, {"id": 2}], "next": None}
        mock_request.return_value = mock_response
        events = fcli.fetch_bzzoiro_events(date_from="2026-11-01", date_to="2026-11-08", api_key="x")
        self.assertEqual(len(events), 2)

    @mock.patch("football_client.request_with_retry")
    def test_fetch_bzzoiro_events_follows_pagination(self, mock_request):
        page1 = mock.Mock()
        page1.json.return_value = {"results": [{"id": 1}], "next": "yes"}
        page2 = mock.Mock()
        page2.json.return_value = {"results": [{"id": 2}], "next": None}
        mock_request.side_effect = [page1, page2]
        events = fcli.fetch_bzzoiro_events(date_from="2026-11-01", date_to="2026-11-08", api_key="x")
        self.assertEqual(len(events), 2)
        self.assertEqual(mock_request.call_count, 2)

    @mock.patch("football_client.request_with_retry")
    def test_fetch_football_data_matches(self, mock_request):
        mock_response = mock.Mock()
        mock_response.json.return_value = {"matches": [{"id": 1}]}
        mock_request.return_value = mock_response
        matches = fcli.fetch_football_data_matches(date_from="2026-11-01", date_to="2026-11-08", api_key="x")
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
