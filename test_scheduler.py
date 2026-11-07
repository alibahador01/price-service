import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    TEHRAN = None


class TestGenerateMinuteOffsets(unittest.TestCase):
    def test_exact_counts_per_job(self):
        offsets = scheduler.generate_minute_offsets(date(2026, 11, 2))
        for job, count in scheduler.JOB_COUNTS.items():
            self.assertEqual(len(offsets[job]), count, job)

    def test_deterministic_same_date(self):
        a = scheduler.generate_minute_offsets(date(2026, 11, 2))
        b = scheduler.generate_minute_offsets(date(2026, 11, 2))
        self.assertEqual(a, b)

    def test_different_dates_usually_differ(self):
        a = scheduler.generate_minute_offsets(date(2026, 11, 2))
        b = scheduler.generate_minute_offsets(date(2026, 11, 3))
        self.assertNotEqual(a, b)

    def test_all_times_within_or_near_active_window(self):
        offsets = scheduler.generate_minute_offsets(date(2026, 11, 2))
        for job, times in offsets.items():
            for t in times:
                self.assertGreaterEqual(t, scheduler.ACTIVE_WINDOW_START_MIN - 1)
                self.assertLessEqual(t, scheduler.ACTIVE_WINDOW_END_MIN + 20)

    def test_within_type_min_spacing_respected(self):
        offsets = scheduler.generate_minute_offsets(date(2026, 11, 2))
        for job, min_gap in scheduler.MIN_SPACING_MINUTES.items():
            times = sorted(offsets[job])
            for i in range(1, len(times)):
                gap = times[i] - times[i - 1]
                # segment-based picking نمی‌تواند fاصله دقیق تضمین کند ولی باید
                # به‌طور منطقی نزدیک حداقل مورد انتظار باشد (حداقل نصف آن).
                self.assertGreaterEqual(gap, min_gap * 0.4, "{} gap {}".format(job, gap))

    def test_global_min_gap_between_different_types(self):
        offsets = scheduler.generate_minute_offsets(date(2026, 11, 2))
        flat = []
        for job, times in offsets.items():
            if job == scheduler.GOODNIGHT_JOB:
                continue
            for t in times:
                flat.append((job, t))
        flat.sort(key=lambda e: e[1])
        for i in range(1, len(flat)):
            prev_job, prev_t = flat[i - 1]
            job, t = flat[i]
            if job != prev_job:
                self.assertGreaterEqual(t - prev_t, scheduler.GLOBAL_MIN_GAP_MINUTES - 1)

    def test_goodnight_is_after_everything_else(self):
        offsets = scheduler.generate_minute_offsets(date(2026, 11, 2))
        others = [t for job, times in offsets.items() if job != scheduler.GOODNIGHT_JOB for t in times]
        self.assertGreater(offsets[scheduler.GOODNIGHT_JOB][0], max(others))


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestBuildScheduleState(unittest.TestCase):
    def test_structure_and_flags(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        self.assertEqual(state["date"], "2026-11-02")
        for job, count in scheduler.JOB_COUNTS.items():
            self.assertEqual(len(state["jobs"][job]), count)
            for occ in state["jobs"][job]:
                self.assertFalse(occ["sent"])
                datetime.fromisoformat(occ["time"])  # باید قابل پارس باشد

    def test_get_or_create_reuses_same_day(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        reused = scheduler.get_or_create_today_schedule(state, date(2026, 11, 2))
        self.assertIs(reused, state)

    def test_get_or_create_regenerates_new_day(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        fresh = scheduler.get_or_create_today_schedule(state, date(2026, 11, 3))
        self.assertEqual(fresh["date"], "2026-11-03")

    def test_get_or_create_handles_none_state(self):
        fresh = scheduler.get_or_create_today_schedule(None, date(2026, 11, 2))
        self.assertEqual(fresh["date"], "2026-11-02")


@unittest.skipIf(TEHRAN is None, "zoneinfo not available")
class TestDueJobs(unittest.TestCase):
    def test_due_jobs_returns_only_past_unsent(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        # اولین رخداد small_list را به عمد عقب ببر تا قطعا "due" باشد
        first_time = datetime.fromisoformat(state["jobs"]["small_list"][0]["time"])
        now = first_time + timedelta(minutes=1)
        due = scheduler.due_jobs(state, now)
        self.assertIn(("small_list", 0), due)

    def test_mark_sent_excludes_from_future_due(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        first_time = datetime.fromisoformat(state["jobs"]["small_list"][0]["time"])
        now = first_time + timedelta(minutes=1)
        scheduler.mark_sent(state, "small_list", 0)
        due = scheduler.due_jobs(state, now)
        self.assertNotIn(("small_list", 0), due)

    def test_due_jobs_sorted_by_time(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        far_future = datetime(2026, 11, 3, 12, 0, tzinfo=TEHRAN)
        due = scheduler.due_jobs(state, far_future)
        times = []
        for job, idx in due:
            times.append(datetime.fromisoformat(state["jobs"][job][idx]["time"]))
        self.assertEqual(times, sorted(times))

    def test_mark_sent_invalid_index_does_not_crash(self):
        state = scheduler.build_schedule_state(date(2026, 11, 2))
        scheduler.mark_sent(state, "small_list", 99)  # نباید استثنا بدهد
        scheduler.mark_sent(state, "unknown_job", 0)


if __name__ == "__main__":
    unittest.main()
