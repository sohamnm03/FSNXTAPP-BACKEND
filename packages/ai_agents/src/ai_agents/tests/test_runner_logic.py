import unittest

from ai_agents.services.claude_service import judge
from ai_agents.services.modal_service import _modal_score
from ai_agents.services.ui_consistency_service import (
    _clustered_colour_count,
    _heading_hierarchy_ok,
)


class ValidationJudgeTests(unittest.TestCase):
    def test_blocked_invalid_value_is_pass(self):
        result = judge(
            "Invalid data must be blocked",
            {
                "submit_was_blocked": True,
                "url_changed": False,
                "entered_data_cleared": None,
            },
        )
        self.assertEqual(result["status"], "pass")

    def test_progressed_invalid_value_is_fail(self):
        result = judge(
            "Invalid data must be blocked",
            {
                "submit_was_blocked": False,
                "url_changed": True,
                "entered_data_cleared": None,
            },
        )
        self.assertEqual(result["status"], "fail")

    def test_data_loss_is_fail_even_when_blocked(self):
        result = judge(
            "Invalid data must be blocked without losing work",
            {
                "submit_was_blocked": True,
                "url_changed": False,
                "entered_data_cleared": ["Facility Name"],
            },
        )
        self.assertEqual(result["status"], "fail")

    def test_ambiguous_outcome_is_inconclusive_without_llm(self):
        result = judge(
            "Invalid data must be blocked",
            {
                "submit_was_blocked": None,
                "url_changed": False,
                "review_reached": False,
                "entered_data_cleared": None,
            },
        )
        self.assertEqual(result["status"], "inconclusive")


class ModalDetectionTests(unittest.TestCase):
    def test_real_dialog_scores_above_small_open_dropdown(self):
        dialog = {
            "visible": True,
            "role": "dialog",
            "ariaModal": "true",
            "tag": "div",
            "cls": "facility-detail-modal",
            "dataState": "",
            "area": 420000,
            "viewportArea": 1296000,
            "position": "fixed",
            "zIndex": 1000,
            "hasDialogAncestor": False,
        }
        dropdown = {
            "visible": True,
            "role": "",
            "ariaModal": "",
            "tag": "button",
            "cls": "select-trigger",
            "dataState": "open",
            "area": 1800,
            "viewportArea": 1296000,
            "position": "relative",
            "zIndex": 0,
            "hasDialogAncestor": False,
        }
        self.assertGreater(_modal_score(dialog), _modal_score(dropdown))
        self.assertLessEqual(_modal_score(dropdown), 0)


class UiMetricTests(unittest.TestCase):
    def test_heading_hierarchy_uses_levels_not_dom_order(self):
        headings = [
            {"level": 4, "size": 14},
            {"level": 2, "size": 22},
            {"level": 3, "size": 18},
        ]
        self.assertTrue(_heading_hierarchy_ok(headings))

    def test_visually_similar_colours_are_clustered(self):
        colours = [
            "rgb(30, 40, 50)",
            "rgb(32, 42, 51)",
            "rgb(200, 210, 220)",
        ]
        self.assertEqual(_clustered_colour_count(colours), 2)


if __name__ == "__main__":
    unittest.main()

