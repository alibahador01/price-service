import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import football_images as fi  # noqa: E402


class TestLicenseGate(unittest.TestCase):
    def test_exact_true_string_confirms(self):
        self.assertTrue(fi.is_license_confirmed("true"))
        self.assertTrue(fi.is_license_confirmed("True"))
        self.assertTrue(fi.is_license_confirmed("TRUE"))

    def test_anything_else_is_not_confirmed(self):
        self.assertFalse(fi.is_license_confirmed(None))
        self.assertFalse(fi.is_license_confirmed(""))
        self.assertFalse(fi.is_license_confirmed("false"))
        self.assertFalse(fi.is_license_confirmed("1"))
        self.assertFalse(fi.is_license_confirmed("yes"))
        self.assertFalse(fi.is_license_confirmed("ture"))  # تایپو هم رد می‌شود


class TestResolveMatchImage(unittest.TestCase):
    def test_no_license_always_unavailable_even_with_valid_ids(self):
        result = fi.resolve_match_image(
            provider="bzzoiro", home_team_id="10", away_team_id="11",
            license_confirmed=False, star_player_id="99",
        )
        self.assertEqual(result["status"], "image_unavailable")
        self.assertEqual(result["reason"], "license_not_confirmed")
        self.assertIsNone(result["url"])

    def test_star_player_takes_priority_over_team_logo(self):
        result = fi.resolve_match_image(
            provider="bzzoiro", home_team_id="10", away_team_id="11",
            license_confirmed=True, star_player_id="99",
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["image_type"], "player")
        self.assertIn("/img/player/99/", result["url"])

    def test_falls_back_to_home_team_logo(self):
        result = fi.resolve_match_image(
            provider="bzzoiro", home_team_id="10", away_team_id="11",
            license_confirmed=True, star_player_id=None,
        )
        self.assertEqual(result["image_type"], "team")
        self.assertIn("/img/team/10/", result["url"])

    def test_falls_back_to_away_team_if_home_missing(self):
        result = fi.resolve_match_image(
            provider="bzzoiro", home_team_id=None, away_team_id="11",
            license_confirmed=True,
        )
        self.assertIn("/img/team/11/", result["url"])

    def test_no_valid_ids_is_unavailable_not_placeholder(self):
        result = fi.resolve_match_image(
            provider="football_data", home_team_id=None, away_team_id=None,
            license_confirmed=True,
        )
        self.assertEqual(result["status"], "image_unavailable")
        self.assertEqual(result["reason"], "no_valid_id")
        self.assertIsNone(result["url"])


class TestValidateImageResponse(unittest.TestCase):
    def test_404_not_found(self):
        result = fi.validate_image_response(404, "image/png", 1000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")

    def test_204_invalid_id(self):
        result = fi.validate_image_response(204, None, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_id")

    def test_valid_image_passes(self):
        result = fi.validate_image_response(200, "image/jpeg", 50000)
        self.assertTrue(result["ok"])

    def test_wrong_content_type_rejected(self):
        result = fi.validate_image_response(200, "text/html", 500)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_content_type")

    def test_too_large_rejected(self):
        result = fi.validate_image_response(200, "image/png", fi.MAX_IMAGE_BYTES + 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "too_large")

    def test_empty_rejected(self):
        result = fi.validate_image_response(200, "image/png", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty")

    def test_unexpected_status_rejected(self):
        result = fi.validate_image_response(500, "image/png", 100)
        self.assertFalse(result["ok"])
        self.assertIn("unexpected_status", result["reason"])


if __name__ == "__main__":
    unittest.main()
