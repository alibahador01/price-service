import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import football_fixtures as ff  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    TEHRAN = None


def make_event(home, away, league, kickoff_utc, event_id="1", provider="bzzoiro", schedulable=True):
    return {
        "provider": provider, "event_id": event_id, "league_name": league,
        "home_team": home, "away_team": away,
        "home_team_id": "h{}".format(event_id), "away_team_id": "a{}".format(event_id),
        "kickoff_utc": kickoff_utc, "status": "upcoming", "schedulable": schedulable,
    }


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestSelectDailyFixtures(unittest.TestCase):
    def test_filters_out_non_allowlisted_league(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [make_event("Liverpool", "Arsenal", "Eredivisie", kickoff)]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        self.assertEqual(selected, [])

    def test_filters_out_match_with_no_selected_team(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [make_event("Burnley", "Fulham", "Premier League", kickoff)]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        self.assertEqual(selected, [])

    def test_keeps_match_with_one_selected_team(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [make_event("Liverpool", "Burnley", "Premier League", kickoff)]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        self.assertEqual(len(selected), 1)

    def test_excludes_non_schedulable_status(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [make_event("Liverpool", "Arsenal", "Premier League", kickoff, schedulable=False)]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        self.assertEqual(selected, [])

    def test_caps_at_max_matches(self):
        events = []
        base = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
        for i in range(15):
            kickoff = base + timedelta(minutes=i * 10)
            events.append(make_event("Liverpool", "Arsenal", "Premier League", kickoff, event_id=str(i)))
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN, max_matches=10)
        self.assertEqual(len(selected), 10)

    def test_fewer_than_cap_returns_only_eligible_no_padding(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [make_event("Liverpool", "Arsenal", "Premier League", kickoff)]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN, max_matches=10)
        self.assertEqual(len(selected), 1)

    def test_both_selected_teams_prioritized_over_one(self):
        k1 = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)  # هر دو منتخب، دیرتر
        k2 = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)  # یکی منتخب، زودتر
        events = [
            make_event("Liverpool", "Burnley", "Premier League", k2, event_id="2"),
            make_event("Liverpool", "Arsenal", "Premier League", k1, event_id="1"),
        ]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN, max_matches=1)
        self.assertEqual(selected[0]["event_id"], "1")  # هر دو منتخب برنده می‌شود حتی با زمان دیرتر

    def test_filters_by_local_date_not_utc_date(self):
        # ساعت ۲۳:۰۰ UTC در تهران (UTC+3:30) می‌شود ۰۲:۳۰ روز بعد
        kickoff = datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)
        events = [make_event("Liverpool", "Arsenal", "Premier League", kickoff)]
        selected_wrong_day = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        selected_right_day = ff.select_daily_fixtures(events, local_date=date(2026, 11, 6), tz=TEHRAN)
        self.assertEqual(selected_wrong_day, [])
        self.assertEqual(len(selected_right_day), 1)

    def test_dedupe_identical_fingerprint(self):
        kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        events = [
            make_event("Liverpool", "Arsenal", "Premier League", kickoff, event_id="1"),
            make_event("Liverpool", "Arsenal", "Premier League", kickoff, event_id="2"),  # همان بازی، ID دیگر
        ]
        selected = ff.select_daily_fixtures(events, local_date=date(2026, 11, 5), tz=TEHRAN)
        self.assertEqual(len(selected), 1)


class TestIdentityKey(unittest.TestCase):
    def test_identity_key_invariant_to_kickoff_unlike_fingerprint(self):
        # این دقیقاً باگی بود که در توسعه این فایل کشف و رفع شد: fingerprint
        # عمداً شامل kickoff است (برای dedupe ارسال)، پس نباید برای تطبیق
        # هویت استفاده شود؛ identity_key باید با تغییر kickoff ثابت بماند.
        self.assertEqual(ff.identity_key("bzzoiro", "123"), ff.identity_key("bzzoiro", "123"))

    def test_identity_key_differs_by_provider(self):
        self.assertNotEqual(ff.identity_key("bzzoiro", "123"), ff.identity_key("football_data", "123"))


class TestSchedulingMath(unittest.TestCase):
    def test_scheduled_send_at_is_exactly_90_minutes_before(self):
        kickoff = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        scheduled = ff.compute_scheduled_send_at(kickoff)
        self.assertEqual(kickoff - scheduled, timedelta(minutes=90))

    def test_build_entry_pending_when_before_scheduled_time(self):
        kickoff = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        event = make_event("Liverpool", "Arsenal", "Premier League", kickoff)
        now = kickoff - timedelta(minutes=100)  # قبل از زمان هدف
        entry = ff.build_fixture_entry(event, now)
        self.assertEqual(entry["status"], ff.FIXTURE_STATUS_PENDING)

    def test_build_entry_missed_window_when_discovered_late(self):
        kickoff = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        event = make_event("Liverpool", "Arsenal", "Premier League", kickoff)
        now = kickoff - timedelta(minutes=10)  # خیلی دیر، بعد از پنجره ۹۰ دقیقه‌ای
        entry = ff.build_fixture_entry(event, now)
        self.assertEqual(entry["status"], ff.FIXTURE_STATUS_MISSED_WINDOW)


class TestDueFixtureSends(unittest.TestCase):
    def test_only_pending_and_past_due_returned(self):
        now = datetime(2026, 11, 5, 18, 30, tzinfo=timezone.utc)
        entries = [
            {"status": "pending", "scheduled_send_at_utc": (now - timedelta(minutes=1)).isoformat()},
            {"status": "pending", "scheduled_send_at_utc": (now + timedelta(minutes=10)).isoformat()},
            {"status": "sent", "scheduled_send_at_utc": (now - timedelta(minutes=5)).isoformat()},
        ]
        due = ff.due_fixture_sends(entries, now)
        self.assertEqual(len(due), 1)

    def test_due_sorted_by_time(self):
        now = datetime(2026, 11, 5, 18, 30, tzinfo=timezone.utc)
        entries = [
            {"status": "pending", "scheduled_send_at_utc": (now - timedelta(minutes=1)).isoformat(), "event_id": "late"},
            {"status": "pending", "scheduled_send_at_utc": (now - timedelta(minutes=10)).isoformat(), "event_id": "early"},
        ]
        due = ff.due_fixture_sends(entries, now)
        self.assertEqual(due[0]["event_id"], "early")

    def test_mark_sent_updates_status_and_timestamp(self):
        now = datetime(2026, 11, 5, 18, 30, tzinfo=timezone.utc)
        entry = {"status": "pending", "scheduled_send_at_utc": now.isoformat()}
        updated = ff.mark_sent(entry, now, chat_message_id=555)
        self.assertEqual(updated["status"], "sent")
        self.assertEqual(updated["chat_message_id"], 555)
        self.assertIsNotNone(updated["actual_sent_at_utc"])
        self.assertEqual(entry["status"], "pending")  # ورودی اصلی دست‌نخورده


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestReconcileFixtures(unittest.TestCase):
    def test_new_match_added_as_pending(self):
        now = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
        kickoff = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        fresh = [make_event("Liverpool", "Arsenal", "Premier League", kickoff)]
        new_state, actions = ff.reconcile_fixtures(
            [], fresh, local_date=date(2026, 11, 5), tz=TEHRAN, now=now,
        )
        self.assertEqual(len(new_state), 1)
        self.assertEqual(new_state[0]["status"], "pending")
        self.assertEqual(actions, [])

    def test_sent_match_disappearing_triggers_cancel(self):
        now = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)
        existing = [{
            "identity_key": "bzzoiro:1",
            "kickoff_utc": "2026-11-05T20:00:00+00:00", "status": "sent",
            "home_team": "Liverpool", "away_team": "Arsenal",
        }]
        new_state, actions = ff.reconcile_fixtures(
            existing, [], local_date=date(2026, 11, 5), tz=TEHRAN, now=now,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "cancel")
        self.assertEqual(new_state, [])

    def test_kickoff_change_on_sent_match_triggers_edit(self):
        # هر دو زمان عمداً در همان روز محلی تهران نگه داشته می‌شوند تا تست
        # فقط منطق edit را بسنجد، نه فیلتر روز محلی select_daily_fixtures.
        now = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)
        old_kickoff = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        new_kickoff = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
        existing = [{
            "identity_key": "bzzoiro:1",
            "kickoff_utc": old_kickoff.isoformat(), "status": "sent",
            "home_team": "Liverpool", "away_team": "Arsenal",
            "scheduled_send_at_utc": (old_kickoff - timedelta(minutes=90)).isoformat(),
        }]
        fresh = [make_event("Liverpool", "Arsenal", "Premier League", new_kickoff, event_id="1")]
        new_state, actions = ff.reconcile_fixtures(
            existing, fresh, local_date=date(2026, 11, 5), tz=TEHRAN, now=now,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "edit")
        self.assertEqual(actions[0]["entry"]["kickoff_utc"], new_kickoff.isoformat())

    def test_unchanged_pending_match_stays_as_is(self):
        now = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
        kickoff = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
        existing = [{
            "identity_key": "bzzoiro:1", "kickoff_utc": kickoff.isoformat(), "status": "pending",
            "home_team": "Liverpool", "away_team": "Arsenal",
            "scheduled_send_at_utc": (kickoff - timedelta(minutes=90)).isoformat(),
        }]
        fresh = [make_event("Liverpool", "Arsenal", "Premier League", kickoff, event_id="1")]
        new_state, actions = ff.reconcile_fixtures(
            existing, fresh, local_date=date(2026, 11, 5), tz=TEHRAN, now=now,
        )
        self.assertEqual(actions, [])
        self.assertEqual(len(new_state), 1)
        self.assertEqual(new_state[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
