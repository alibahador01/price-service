import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard as kb  # noqa: E402


class TestBuildChannelKeyboard(unittest.TestCase):
    def test_exactly_two_buttons_in_one_row(self):
        result = kb.build_channel_keyboard("https://t.me/Vochino_bh01_bot")
        rows = result["inline_keyboard"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 2)

    def test_login_app_url_appends_app_path(self):
        result = kb.build_channel_keyboard("https://t.me/Vochino_bh01_bot")
        first = result["inline_keyboard"][0][0]
        self.assertEqual(first["text"], kb.LOGIN_APP_BUTTON_TEXT)
        self.assertEqual(first["url"], "https://t.me/Vochino_bh01_bot/app")

    def test_enter_bot_url_is_base_link(self):
        result = kb.build_channel_keyboard("https://t.me/Vochino_bh01_bot")
        second = result["inline_keyboard"][0][1]
        self.assertEqual(second["text"], kb.ENTER_BOT_BUTTON_TEXT)
        self.assertEqual(second["url"], "https://t.me/Vochino_bh01_bot")

    def test_both_buttons_styled_danger(self):
        result = kb.build_channel_keyboard("https://t.me/Vochino_bh01_bot")
        for button in result["inline_keyboard"][0]:
            self.assertEqual(button["style"], "danger")

    def test_trailing_slash_in_base_url_handled(self):
        result = kb.build_channel_keyboard("https://t.me/Vochino_bh01_bot/")
        first = result["inline_keyboard"][0][0]
        self.assertEqual(first["url"], "https://t.me/Vochino_bh01_bot/app")

    def test_empty_base_url_does_not_crash(self):
        result = kb.build_channel_keyboard("")
        self.assertEqual(result["inline_keyboard"][0][0]["url"], "")
        self.assertEqual(result["inline_keyboard"][0][1]["url"], "")


if __name__ == "__main__":
    unittest.main()
