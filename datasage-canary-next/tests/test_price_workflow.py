"""Small protocol tests for explicit empty adapter fields."""

import importlib
import unittest

import test_business_contracts as base


workflow = importlib.import_module(base.TEST_PACKAGE + ".price_workflow")


class PriceWorkflowProtocolTests(unittest.TestCase):
    def test_explicit_empty_fields_do_not_fall_back_to_context(self):
        context = {
            "after_reference": [{"id": 1}],
            "changes": [{"goods_no": "old"}],
            "document": {"event_counts": {"old": 1}},
        }
        self.assertEqual([], workflow._field({"after_reference": []}, "after_reference", context["after_reference"]))
        self.assertEqual([], workflow._field({"changes": []}, "changes", context["changes"]))
        self.assertEqual({}, workflow._field({"document": {}}, "document", context["document"]))

    def test_missing_fields_still_use_context_defaults(self):
        context = {"after_reference": [{"id": 1}], "changes": [{"goods_no": "old"}]}
        self.assertEqual(context["after_reference"], workflow._field({}, "after_reference", context["after_reference"]))
        self.assertEqual(context["changes"], workflow._field({}, "changes", context["changes"]))


if __name__ == "__main__":
    unittest.main()
