import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import central_scheduler as cs  # noqa: E402
import promo_messages  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    TEHRAN = None


def base_context(**overrides):
    ctx = {
        "tz": TEHRAN, "tz_name": "Asia/Tehran",
        "send_text_message": lambda text, with_keyboard=True: True,
        "build_small_list_message": lambda: "small list text",
        "build_large_list_message": lambda: "big list text",
        "build_goodnight_message": lambda: "شب بخیر",
        "fetch_klines": lambda symbol: [],
        "fetch_agg_trades_window": lambda symbol, s, e: ([], False),
        "call_gemini": lambda *a, **k: {"assets": []},
        "call_groq": lambda *a, **k: {"assets": []},
        "gemini_api_key": "x", "groq_api_key": "y",
        "fetch_fear_greed": lambda: {"value": None, "classification": None, "timestamp_utc": None},
        "fetch_btc_dominance": lambda: None,
        "fetch_24h_changes": lambda: {},
        "format_local_datetime": lambda now: ("امروز", "ساعت"),
        "format_local_time": lambda now: "12:00",
        "load_whale_state": lambda: {},
        "save_whale_state": lambda s: None,
        "get_vochino_usdt_sell_toman": lambda: None,
        "get_cached_tether_usd": lambda: (None, None),
        "get_cached_usd_irt_toman": lambda: (None, None),
        "fetch_bzzoiro_events": lambda **k: [],
        "fetch_football_data_matches": lambda **k: [],
        "bzzoiro_api_key": "x", "football_data_api_key": "y",
        "football_image_license_confirmed": False,
        "send_football_match": lambda image, caption: None,
        "delete_football_message": lambda msg_id: True,
        "edit_football_message_caption": lambda msg_id, caption: True,
        "log": lambda *a, **k: None,
    }
    ctx.update(overrides)
    return ctx


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestRunTickNonFootball(unittest.TestCase):
    def test_due_job_dispatched_and_marked_sent(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        local_date = now.astimezone(TEHRAN).date()
        schedule = scheduler_mod.build_schedule_state(local_date, tz_name="Asia/Tehran")
        schedule["jobs"]["small_list"][0]["time"] = (now - timedelta(minutes=1)).isoformat()
        state = {"daily_schedule": schedule}

        sent_texts = []
        ctx = base_context(send_text_message=lambda text, with_keyboard=True: sent_texts.append(text) or True)
        report = cs.run_tick(ctx, state, now)

        self.assertIn("small_list", report["sent"])
        self.assertTrue(any("small list text" in t for t in sent_texts))
        self.assertTrue(state["daily_schedule"]["jobs"]["small_list"][0]["sent"])

    def test_failed_job_not_marked_sent_and_retried_next_tick(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        local_date = now.astimezone(TEHRAN).date()
        schedule = scheduler_mod.build_schedule_state(local_date, tz_name="Asia/Tehran")
        schedule["jobs"]["small_list"][0]["time"] = (now - timedelta(minutes=1)).isoformat()
        state = {"daily_schedule": schedule}

        ctx = base_context(send_text_message=lambda text, with_keyboard=True: False)
        report = cs.run_tick(ctx, state, now)
        self.assertIn("small_list", report["failed"])
        self.assertFalse(state["daily_schedule"]["jobs"]["small_list"][0]["sent"])

        # tick بعدی هم همان job را due می‌بیند
        later = now + timedelta(seconds=45)
        report2 = cs.run_tick(ctx, state, later)
        self.assertIn("small_list", report2["failed"])

    def test_promo_index_only_commits_on_success(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        local_date = now.astimezone(TEHRAN).date()
        schedule = scheduler_mod.build_schedule_state(local_date, tz_name="Asia/Tehran")
        schedule["jobs"]["small_list"][0]["time"] = (now - timedelta(minutes=1)).isoformat()
        state = {"daily_schedule": schedule, "promo": {"index": 5}}

        ctx = base_context(send_text_message=lambda text, with_keyboard=True: False)
        cs.run_tick(ctx, state, now)
        self.assertEqual(state["promo"]["index"], 5)  # شکست -> index ثابت

        ctx_ok = base_context(send_text_message=lambda text, with_keyboard=True: True)
        cs.run_tick(ctx_ok, state, now)
        self.assertEqual(state["promo"]["index"], 6)  # موفقیت -> index پیش رفت

    def test_one_job_failure_does_not_block_other_due_jobs(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        local_date = now.astimezone(TEHRAN).date()
        schedule = scheduler_mod.build_schedule_state(local_date, tz_name="Asia/Tehran")
        schedule["jobs"]["small_list"][0]["time"] = (now - timedelta(minutes=2)).isoformat()
        schedule["jobs"]["big_list"][0]["time"] = (now - timedelta(minutes=1)).isoformat()
        state = {"daily_schedule": schedule}

        def flaky_send(text, with_keyboard=True):
            return "big list" in text  # small_list می‌خورد شکست، big_list موفق

        ctx = base_context(send_text_message=flaky_send)
        report = cs.run_tick(ctx, state, now)
        self.assertIn("small_list", report["failed"])
        self.assertIn("big_list", report["sent"])

    def test_no_due_jobs_sends_nothing(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        local_date = now.astimezone(TEHRAN).date()
        schedule = scheduler_mod.build_schedule_state(local_date, tz_name="Asia/Tehran")
        for job in schedule["jobs"]:
            for occ in schedule["jobs"][job]:
                occ["time"] = (now + timedelta(hours=5)).isoformat()  # همه در آینده
        state = {"daily_schedule": schedule}

        calls = []
        ctx = base_context(send_text_message=lambda text, with_keyboard=True: calls.append(1) or True)
        cs.run_tick(ctx, state, now)
        self.assertEqual(calls, [])


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestRunTickFootball(unittest.TestCase):
    def test_refresh_only_when_poll_interval_elapsed(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        schedule = scheduler_mod.build_schedule_state(now.astimezone(TEHRAN).date(), tz_name="Asia/Tehran")
        state = {
            "daily_schedule": schedule,
            "football_last_refresh": (now - timedelta(minutes=5)).isoformat(),  # کمتر از ۱۵ دقیقه
        }
        calls = {"n": 0}

        def fetch_bzz(**k):
            calls["n"] += 1
            return []

        ctx = base_context(fetch_bzzoiro_events=fetch_bzz, football_poll_interval_minutes=15)
        cs.run_tick(ctx, state, now)
        self.assertEqual(calls["n"], 0)  # هنوز زود است

        state["football_last_refresh"] = (now - timedelta(minutes=20)).isoformat()
        cs.run_tick(ctx, state, now)
        self.assertEqual(calls["n"], 1)

    def test_due_match_sent_and_marked(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        schedule = scheduler_mod.build_schedule_state(now.astimezone(TEHRAN).date(), tz_name="Asia/Tehran")
        kickoff = now + timedelta(minutes=80)  # ۸۰ دقیقه مانده -> از ۹۰ دقیقه گذشته، due است
        entry = {
            "identity_key": "bzzoiro:1", "fingerprint": "x", "provider": "bzzoiro", "event_id": "1",
            "league_name": "Premier League", "home_team": "Liverpool", "away_team": "Arsenal",
            "home_team_id": "10", "away_team_id": "11",
            "kickoff_utc": kickoff.isoformat(),
            "scheduled_send_at_utc": (kickoff - timedelta(minutes=90)).isoformat(),
            "status": "pending", "actual_sent_at_utc": None, "chat_message_id": None,
        }
        state = {
            "daily_schedule": schedule,
            "football_last_refresh": now.isoformat(),  # رفرش را skip کن، فقط ارسال را تست کن
            "football_fixtures": [entry],
        }
        sent_payloads = []

        def send_match(image, caption):
            sent_payloads.append((image, caption))
            return 999

        ctx = base_context(send_football_match=send_match)
        report = cs.run_tick(ctx, state, now)
        self.assertEqual(len(sent_payloads), 1)
        self.assertIn("bzzoiro:1", report["football_sent"])
        updated_entry = state["football_fixtures"][0]
        self.assertEqual(updated_entry["status"], "sent")
        self.assertEqual(updated_entry["chat_message_id"], 999)

    def test_failed_match_send_does_not_mark_sent(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        schedule = scheduler_mod.build_schedule_state(now.astimezone(TEHRAN).date(), tz_name="Asia/Tehran")
        kickoff = now + timedelta(minutes=80)
        entry = {
            "identity_key": "bzzoiro:1", "fingerprint": "x", "provider": "bzzoiro", "event_id": "1",
            "league_name": "Premier League", "home_team": "Liverpool", "away_team": "Arsenal",
            "home_team_id": "10", "away_team_id": "11",
            "kickoff_utc": kickoff.isoformat(),
            "scheduled_send_at_utc": (kickoff - timedelta(minutes=90)).isoformat(),
            "status": "pending", "actual_sent_at_utc": None, "chat_message_id": None,
        }
        state = {
            "daily_schedule": schedule, "football_last_refresh": now.isoformat(),
            "football_fixtures": [entry],
        }
        ctx = base_context(send_football_match=lambda image, caption: None)  # شکست
        report = cs.run_tick(ctx, state, now)
        self.assertEqual(report["football_sent"], [])
        self.assertEqual(state["football_fixtures"][0]["status"], "pending")

    def test_cancel_action_calls_delete(self):
        now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        schedule = scheduler_mod.build_schedule_state(now.astimezone(TEHRAN).date(), tz_name="Asia/Tehran")
        sent_entry = {
            "identity_key": "bzzoiro:1", "kickoff_utc": "2026-11-02T20:00:00+00:00",
            "status": "sent", "chat_message_id": 555,
            "home_team": "Liverpool", "away_team": "Arsenal",
        }
        state = {
            "daily_schedule": schedule,
            "football_last_refresh": (now - timedelta(minutes=20)).isoformat(),
            "football_fixtures": [sent_entry],
        }
        deleted = []
        ctx = base_context(
            fetch_bzzoiro_events=lambda **k: [],  # دیگر در فهرست نیست -> cancel
            delete_football_message=lambda msg_id: deleted.append(msg_id),
        )
        cs.run_tick(ctx, state, now)
        self.assertEqual(deleted, [555])


if __name__ == "__main__":
    unittest.main()
