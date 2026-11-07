import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import promo_messages as pm  # noqa: E402


class TestPromoBankIntegrity(unittest.TestCase):
    def test_exactly_30_texts(self):
        self.assertEqual(len(pm.PROMO_BANK), 30)

    def test_no_duplicate_texts(self):
        self.assertEqual(len(set(pm.PROMO_BANK)), 30)

    def test_no_empty_text(self):
        for text in pm.PROMO_BANK:
            self.assertTrue(text.strip())

    def test_brand_marker_present_in_most_texts(self):
        # سند الزام نکرده همه ۳۰ متن اسم برند را تحت‌اللفظی بیاورند (تنوع لحن)،
        # ولی اکثریت قاطع باید داشته باشند؛ این فقط یک چک سلامت کلی است.
        # برند طبق درخواست کاربر از Vochino⁰¹ به Vochino-Hub⁰¹ تغییر کرد.
        count = sum(1 for t in pm.PROMO_BANK if "Vochino-Hub\u2070\u00b9" in t)
        self.assertGreaterEqual(count, 20)

    def test_forbidden_claims_absent(self):
        forbidden = ["تضمین سود", "بدون ریسک", "کارمزد صفر", "تضمین اصالت"]
        for i, text in enumerate(pm.PROMO_BANK):
            for phrase in forbidden:
                self.assertNotIn(phrase, text, "entry {} contains forbidden phrase {}".format(i, phrase))

    def test_no_literal_start_command(self):
        for text in pm.PROMO_BANK:
            self.assertNotIn("/start", text)

    def test_first_and_last_entries_exact(self):
        # برند طبق درخواست کاربر از Vochino⁰¹ به Vochino-Hub⁰¹ تغییر کرد؛
        # بقیه متن عیناً همان متن اصلی سند است.
        self.assertEqual(
            pm.PROMO_BANK[0],
            "🜲 Vochino-Hub\u2070\u00b9 | خرید و فروش مطمئن ووچر\n"
            "تحویل خودکار کد، معمولاً در کمتر از ۳ دقیقه؛ با نرخ آنلاین و کارمزد منصفانه.\n"
            "ورود از دکمه زیر.",
        )
        self.assertEqual(
            pm.PROMO_BANK[-1],
            "🜲 Vochino-Hub\u2070\u00b9 | تبادل U، Hot و Premium Voucher\n"
            "خرید و فروش خودکار، پشتیبانی آنلاین و نرخ به\u200cروز.\n"
            "ورود از دکمه قرمز زیر.",
        )


class TestRotation(unittest.TestCase):
    def test_full_cycle_visits_all_30_without_repeat(self):
        state = dict(pm.DEFAULT_PROMO_STATE)
        seen = []
        for _ in range(30):
            nxt = pm.peek_next_promo(state)
            seen.append(nxt["index"])
            state = pm.commit_promo_index(state, nxt["index"])
        self.assertEqual(sorted(seen), list(range(30)))
        self.assertEqual(len(set(seen)), 30)

    def test_cycle_wraps_to_start_after_30(self):
        state = dict(pm.DEFAULT_PROMO_STATE)
        for _ in range(30):
            nxt = pm.peek_next_promo(state)
            state = pm.commit_promo_index(state, nxt["index"])
        self.assertEqual(state["index"], 0)

    def test_peek_does_not_mutate_state(self):
        state = {"index": 5}
        pm.peek_next_promo(state)
        self.assertEqual(state["index"], 5)

    def test_failed_send_does_not_advance_index(self):
        # اگر commit صدا زده نشود (یعنی ارسال ناموفق بود)، index ثابت می‌ماند.
        state = {"index": 7}
        nxt = pm.peek_next_promo(state)
        self.assertEqual(nxt["index"], 7)
        again = pm.peek_next_promo(state)
        self.assertEqual(again["index"], 7)

    def test_commit_advances_only_on_explicit_call(self):
        state = {"index": 7}
        nxt = pm.peek_next_promo(state)
        new_state = pm.commit_promo_index(state, nxt["index"])
        self.assertEqual(new_state["index"], 8)
        self.assertEqual(state["index"], 7)  # state اصلی دست‌نخورده می‌ماند (immutable pattern)

    def test_out_of_range_index_recovers_gracefully(self):
        state = {"index": 999}
        nxt = pm.peek_next_promo(state)
        self.assertTrue(0 <= nxt["index"] < 30)


class TestFooter(unittest.TestCase):
    def test_footer_appended_after_blank_line(self):
        result = pm.append_promo_footer("قیمت‌های امروز", "متن تبلیغ")
        self.assertEqual(result, "قیمت‌های امروز\n\nمتن تبلیغ")

    def test_footer_does_not_alter_main_text_content(self):
        main = "خط اول\nخط دوم"
        result = pm.append_promo_footer(main, "تبلیغ")
        self.assertTrue(result.startswith(main))


if __name__ == "__main__":
    unittest.main()
