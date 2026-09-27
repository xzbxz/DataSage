"""Phase 2 detail API schema/runtime compatibility against the candidate plugin."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "datasage_detail_api_schema_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins" / "datasage-query")]
    sys.modules[PACKAGE] = package

schemas = importlib.import_module(f"{PACKAGE}.schemas")
detail_pages = importlib.import_module(f"{PACKAGE}.detail_pages")


def _request(**overrides):
    value = {
        "request_id": "detail-q",
        "domain": "target",
        "metric": "delivery_target_completion",
        "attribution_mode": "transaction_detail",
        "dimensions": ["department"],
        "analysis": {
            "group_filters": [
                {"field": "completion_rate", "op": "lt", "value": "0.8"}
            ]
        },
        "detail": {"id": "target_department_groups"},
    }
    value.update(overrides)
    return value


class Phase2DetailSchemaCandidateTests(unittest.TestCase):
    def setUp(self):
        self.validator = jsonschema.Draft7Validator(schemas.REQUEST)

    def test_positive_shape_schema_and_runtime_are_aligned(self):
        request = _request(detail={"id": "target_department_groups", "limit": 25})
        self.assertFalse(list(self.validator.iter_errors(request)))
        normalized = detail_pages.normalize_detail(request["detail"])
        self.assertEqual({"id": "target_department_groups", "limit": 25}, normalized)
        shaped = detail_pages.validate_request_detail_shape(request)
        self.assertEqual(normalized, shaped["detail"])

    def test_default_is_idempotent_and_cursor_has_no_padding(self):
        detail = {"id": "target_department_groups"}
        normalized = detail_pages.normalize_detail(detail)
        self.assertEqual(normalized, detail_pages.normalize_detail(normalized))
        padded = _request(detail={"id": "target_department_groups", "cursor": "abc="})
        self.assertTrue(list(self.validator.iter_errors(padded)))
        with self.assertRaises(detail_pages.DetailPagesError):
            detail_pages.normalize_detail(padded["detail"])

    def test_detail_requires_analysis_and_rejects_conflicting_top_level_fields(self):
        for field, value in (
            ("limit", 1),
            ("comparison", {"kind": "previous_period"}),
            ("time_bucket", "month"),
            ("complete_change_decomposition", {"dimension": "department"}),
            ("complete_target_gap_decomposition", {"dimension": "department"}),
        ):
            with self.subTest(field=field):
                request = _request(**{field: value})
                self.assertTrue(list(self.validator.iter_errors(request)))
                with self.assertRaises(detail_pages.DetailPagesError):
                    detail_pages.validate_request_detail_shape(request)
        missing_analysis = _request()
        missing_analysis.pop("analysis")
        self.assertTrue(list(self.validator.iter_errors(missing_analysis)))
        with self.assertRaises(detail_pages.DetailPagesError):
            detail_pages.validate_request_detail_shape(missing_analysis)


if __name__ == "__main__":
    unittest.main()
