import os
import unittest
from unittest.mock import patch

from ai_agents import config
from ai_agents.services.auth_service import (
    _first_usable,
    normalise_role,
    parse_role_test_users,
    role_capabilities,
)
from ai_agents.services.focused_business_service import DropdownOutcome, _has_meaningful_value
from ai_agents.services.route_service import configured_routes, select_routes


class RoleMatrixTests(unittest.TestCase):
    def test_expected_facility_role_matrix(self):
        self.assertEqual(
            role_capabilities("FOURTHSIGNAL"),
            {"create_facility": True, "approve_facility": False},
        )
        self.assertEqual(
            role_capabilities("MAKER"),
            {"create_facility": True, "approve_facility": False},
        )
        self.assertEqual(
            role_capabilities("CHECKER"),
            {"create_facility": False, "approve_facility": True},
        )

    def test_check_alias_maps_to_checker(self):
        self.assertEqual(normalise_role("check"), "CHECKER")

    def test_role_credentials_allow_colon_in_password(self):
        parsed = parse_role_test_users(
            "FOURTHSIGNAL:fs:pw,MAKER:maker:pw:with:colon,CHECK:check:pw"
        )
        self.assertEqual(parsed[0], ("FOURTHSIGNAL", "fs", "pw"))
        self.assertEqual(parsed[1], ("MAKER", "maker", "pw:with:colon"))
        self.assertEqual(parsed[2], ("CHECKER", "check", "pw"))


class GenericLoginLocatorTests(unittest.TestCase):
    class Candidate:
        def __init__(self, visible=True, enabled=True):
            self.visible = visible
            self.enabled = enabled

        def is_visible(self):
            return self.visible

        def is_enabled(self):
            return self.enabled

    class Matches:
        def __init__(self, candidates):
            self.candidates = candidates

        def count(self):
            return len(self.candidates)

        def nth(self, index):
            return self.candidates[index]

    class Page:
        def __init__(self, matches):
            self.matches = matches

        def locator(self, selector):
            return GenericLoginLocatorTests.Matches(self.matches.get(selector, []))

    def test_first_usable_skips_hidden_and_disabled_candidates(self):
        hidden = self.Candidate(visible=False)
        disabled = self.Candidate(enabled=False)
        usable = self.Candidate()
        page = self.Page({"first": [hidden, disabled], "second": [usable]})
        self.assertIs(_first_usable(page, ("first", "second")), usable)


class RouteRecoveryTests(unittest.TestCase):
    def test_configured_focus_routes_are_added_when_menu_hides_them(self):
        original_focus_only = config.FOCUS_ONLY
        original_focus_routes = config.FOCUS_ROUTES
        original_test_routes = config.TEST_ROUTES
        try:
            config.FOCUS_ONLY = True
            config.FOCUS_ROUTES = "/dashboard,/facility-creation,/facility-approval"
            config.TEST_ROUTES = ""
            routes = configured_routes([], "https://example.com/dashboard")
            paths = {route["path"] for route in routes}
            self.assertEqual(
                paths,
                {"/dashboard", "/facility-creation", "/facility-approval"},
            )
        finally:
            config.FOCUS_ONLY = original_focus_only
            config.FOCUS_ROUTES = original_focus_routes
            config.TEST_ROUTES = original_test_routes


    def test_non_interactive_selection_never_prompts(self):
        routes = [
            {
                "path": "/dashboard",
                "url": "https://example.com/dashboard",
                "name": "Dashboard",
                "source": "configured",
            }
        ]
        original_interactive = config.INTERACTIVE_ROUTE_SELECTION
        original_focus_only = config.FOCUS_ONLY
        try:
            config.INTERACTIVE_ROUTE_SELECTION = False
            config.FOCUS_ONLY = False
            with patch.dict(os.environ, {"TEST_ROUTES": ""}, clear=False),                     patch("builtins.input", side_effect=AssertionError("input called")):
                self.assertEqual(select_routes(routes), routes)
        finally:
            config.INTERACTIVE_ROUTE_SELECTION = original_interactive
            config.FOCUS_ONLY = original_focus_only

    def test_zero_max_pages_selects_every_discovered_route(self):
        routes = [
            {
                "path": f"/page-{index}",
                "url": f"https://example.com/page-{index}",
                "name": f"Page {index}",
                "source": "menu",
            }
            for index in range(20)
        ]
        with (
            patch.object(config, "INTERACTIVE_ROUTE_SELECTION", False),
            patch.object(config, "FOCUS_ONLY", False),
            patch.object(config, "MAX_PAGES", 0),
            patch.dict(os.environ, {"TEST_ROUTES": ""}, clear=False),
        ):
            self.assertEqual(select_routes(routes), routes)


class DropdownEvidenceTests(unittest.TestCase):
    class FakeItem:
        def input_value(self):
            return ""

        def get_attribute(self, name):
            if name == "data-qa-visible-options-loaded":
                return "1"
            return None

        def evaluate(self, _script):
            return ""

    def test_visible_options_do_not_fake_a_selected_value(self):
        self.assertFalse(_has_meaningful_value(self.FakeItem()))

    def test_visible_options_fallback_passes_without_claiming_selection(self):
        outcome = DropdownOutcome(
            passed=True,
            selected=False,
            options_visible=True,
            detail="visible-options fallback accepted",
        )
        self.assertTrue(outcome.passed)
        self.assertFalse(outcome.selected)
        self.assertTrue(outcome.options_visible)

    def test_dropdown_visible_options_fallback_is_enabled_by_default(self):
        self.assertTrue(config.DROPDOWN_VISIBLE_OPTIONS_PASS)


if __name__ == "__main__":
    unittest.main()

