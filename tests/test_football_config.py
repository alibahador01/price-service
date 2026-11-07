import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import football_config as fc  # noqa: E402


class TestLeagueMatching(unittest.TestCase):
    def test_known_league_allowed(self):
        self.assertTrue(fc.is_allowed_league("Premier League"))
        self.assertTrue(fc.is_allowed_league("premier league"))  # case-insensitive

    def test_unknown_league_not_allowed(self):
        self.assertFalse(fc.is_allowed_league("Eredivisie"))

    def test_canonical_league_fa_label(self):
        self.assertEqual(fc.canonical_league_fa("La Liga"), "لالیگا اسپانیا")

    def test_unknown_league_fa_is_empty(self):
        self.assertEqual(fc.canonical_league_fa("Eredivisie"), "")


class TestTeamMatching(unittest.TestCase):
    def test_selected_team_direct_name(self):
        self.assertTrue(fc.is_selected_team("Liverpool"))

    def test_selected_team_via_alias(self):
        self.assertTrue(fc.is_selected_team("Man City"))
        self.assertTrue(fc.is_selected_team("PSG"))
        self.assertTrue(fc.is_selected_team("Bayern Munich"))

    def test_non_selected_team(self):
        self.assertFalse(fc.is_selected_team("Burnley"))
        self.assertFalse(fc.is_selected_team("Getafe"))

    def test_canonical_team_name_case_insensitive(self):
        self.assertEqual(fc.canonical_team_name("MAN UTD"), "manchester united")


class TestSelectionPriority(unittest.TestCase):
    def test_both_teams_selected_ranks_highest(self):
        rank_both = fc.selection_priority("Premier League", "Liverpool", "Arsenal")
        rank_one = fc.selection_priority("Premier League", "Liverpool", "Burnley")
        self.assertLess(rank_both, rank_one)

    def test_no_selected_team_ranks_lowest(self):
        rank_none = fc.selection_priority("Premier League", "Burnley", "Fulham")
        rank_one = fc.selection_priority("Premier League", "Liverpool", "Burnley")
        self.assertGreater(rank_none, rank_one)

    def test_league_order_breaks_ties(self):
        rank_ucl = fc.selection_priority("UEFA Champions League", "Liverpool", "Arsenal")
        rank_ligue1 = fc.selection_priority("Ligue 1", "Paris Saint-Germain", "Marseille")
        self.assertLess(rank_ucl, rank_ligue1)


if __name__ == "__main__":
    unittest.main()
