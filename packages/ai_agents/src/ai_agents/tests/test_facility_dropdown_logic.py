import unittest

from ai_agents.services.focused_business_service import (
    _exact_field_selectors,
    _other_field_exclusion_selectors,
    _selection_changed,
    _snapshot_meaningful,
)


class FacilityDropdownLogicTests(unittest.TestCase):
    def test_business_partner_uses_stable_selectors(self):
        selectors = _exact_field_selectors([r"business\s*partner", r"partner"])
        self.assertIn("#facility-business-partner", selectors)
        self.assertIn("[name='businessPartnerId']", selectors)

    def test_exclusion_selectors_omit_the_field_itself(self):
        selectors = _other_field_exclusion_selectors([r"business\s*partner", r"partner"])
        self.assertNotIn("#facility-business-partner", selectors)
        self.assertNotIn("[name='businessPartnerId']", selectors)

    def test_exclusion_selectors_include_every_sibling_field(self):
        # This is the exact contamination pattern observed in production:
        # Business Partner's option discovery picking up Currency's default
        # value, Product's placeholder, and Start Date's label because none of
        # them were excluded from the scan.
        selectors = _other_field_exclusion_selectors([r"business\s*partner", r"partner"])
        self.assertIn("#facility-currency", selectors)
        self.assertIn("#facility-product-id", selectors)
        self.assertIn("#facility-start-date", selectors)

    def test_exclusion_selectors_with_no_own_field_exclude_nothing(self):
        # Used for arbitrary/unknown required fields where we don't know which
        # group the field belongs to - every known control should still be
        # excluded so it can't be mistaken for the unknown field's options.
        selectors = _other_field_exclusion_selectors(None)
        self.assertIn("#facility-business-partner", selectors)
        self.assertIn("#facility-currency", selectors)

    def test_placeholder_is_not_selected_evidence(self):
        self.assertFalse(
            _snapshot_meaningful(
                {
                    "value": "",
                    "text": "Select Business Partner â–¾",
                }
            )
        )

    def test_preselected_company_is_meaningful(self):
        self.assertTrue(
            _snapshot_meaningful(
                {
                    "value": "6000",
                    "text": "6000 - TCL â–¾",
                }
            )
        )

    def test_custom_option_text_change_confirms_selection(self):
        before = {"value": "", "text": "Select Business Partner â–¾"}
        after = {"value": "21", "text": "BP3002 - Bank of Baroda â–¾"}
        self.assertTrue(
            _selection_changed(before, after, "BP3002 - Bank of Baroda")
        )

    def test_unchanged_placeholder_is_not_selection(self):
        before = {"value": "", "text": "Select Business Partner â–¾"}
        after = {"value": "", "text": "Select Business Partner â–¾"}
        self.assertFalse(_selection_changed(before, after))


if __name__ == "__main__":
    unittest.main()

