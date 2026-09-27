"""Static contract coverage for the finite public analysis language."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "analysis_request_contract_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins" / "datasage-query")]
sys.modules.setdefault(PACKAGE, package)

analysis_contract = importlib.import_module(f"{PACKAGE}.analysis_contract")
request_contract = importlib.import_module(f"{PACKAGE}.request_contract")
wire = importlib.import_module(f"{PACKAGE}.wire")


def _metric(**overrides):
    value = {
        "analysis_fields": {
            "row": {
                "price_to_ddp_ratio": {
                    "stage": "row",
                    "unit": "比例",
                    "operation": "slice_outbound_gross",
                }
            },
            "group": {
                "metric_value": {
                    "stage": "group",
                    "unit": "人民币元",
                    "currency": "RMB",
                    "operation": "value",
                },
                "any_overdue_days": {
                    "stage": "group",
                    "unit": "自然日",
                    "operation": "exists",
                },
            },
        },
        "analysis_supported_combinations": [
            {
                "row_fields": ["price_to_ddp_ratio"],
                "group_fields": ["metric_value", "any_overdue_days"],
                "stages": ["row", "group"],
            }
        ],
    }
    value.update(overrides)
    return value


class AnalysisRequestContractTests(unittest.TestCase):
    def test_shape_uses_and_and_finite_decimal_values(self):
        normalized = analysis_contract.normalize_analysis(
            {
                "row_filters": [
                    {"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}
                ],
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": 10000},
                    {"field": "any_overdue_days", "op": "gt", "value": 30},
                ],
            }
        )
        self.assertEqual("0.5", normalized["row_filters"][0]["value"])
        group_values = {
            item["field"]: item["value"] for item in normalized["group_filters"]
        }
        self.assertEqual("10000", group_values["metric_value"])
        equivalent = [
            analysis_contract.normalize_analysis(
                {"row_filters": [{"field": "f", "op": "eq", "value": value}]}
            )["row_filters"][0]["value"]
            for value in (0.5, "0.50", "+.500")
        ]
        self.assertEqual(["0.5", "0.5", "0.5"], equivalent)

    def test_and_condition_order_is_canonical_and_empty_stage_is_allowed(self):
        first = analysis_contract.normalize_analysis(
            {
                "row_filters": [
                    {"field": "z", "op": "gte", "value": "0.50"},
                    {"field": "a", "op": "eq", "value": "+.500"},
                ],
                "group_filters": [],
            }
        )
        second = analysis_contract.normalize_analysis(
            {
                "row_filters": [
                    {"field": "a", "op": "eq", "value": "0.5"},
                    {"field": "z", "op": "gte", "value": 0.5},
                ]
            }
        )
        self.assertEqual(first, second)
        with self.assertRaises(analysis_contract.AnalysisContractError):
            analysis_contract.normalize_analysis(
                {"row_filters": [], "group_filters": []}
            )

    def test_bool_nonfinite_exponent_and_unknown_operator_are_rejected(self):
        for value in (True, float("nan"), float("inf"), "1e3", "9" * 65):
            with self.subTest(value=value):
                with self.assertRaises(analysis_contract.AnalysisContractError):
                    analysis_contract.normalize_analysis(
                        {"row_filters": [{"field": "f", "op": "eq", "value": value}]}
                    )
        with self.assertRaises(analysis_contract.AnalysisContractError) as caught:
            analysis_contract.normalize_analysis(
                {"row_filters": [{"field": "f", "op": "ne", "value": 1}]}
            )
        self.assertEqual("INVALID_INPUT", caught.exception.code)

    def test_each_stage_and_total_filter_limits_are_bounded(self):
        make = lambda count: [
            {"field": f"f{index}", "op": "eq", "value": index}
            for index in range(count)
        ]
        for key, count in (
            ("row_filters", analysis_contract.ANALYSIS_MAX_ROW_FILTERS + 1),
            ("group_filters", analysis_contract.ANALYSIS_MAX_GROUP_FILTERS + 1),
        ):
            with self.subTest(key=key):
                with self.assertRaises(analysis_contract.AnalysisContractError):
                    analysis_contract.normalize_analysis({key: make(count)})
        with self.assertRaises(analysis_contract.AnalysisContractError):
            analysis_contract.normalize_analysis(
                {"row_filters": make(5), "group_filters": make(4)}
            )

    def test_metric_registration_rejects_unsupported_field_and_combination(self):
        valid = analysis_contract.validate_analysis_for_metric(
            {
                "row_filters": [
                    {"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}
                ],
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": 10000},
                    {"field": "any_overdue_days", "op": "gt", "value": 30},
                ],
            },
            _metric(),
            metric_code="probe_metric",
        )
        self.assertEqual("0.5", valid["row_filters"][0]["value"])
        group_only = analysis_contract.validate_analysis_for_metric(
            {
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": 10000}
                ]
            },
            _metric(),
            metric_code="probe_metric",
        )
        self.assertEqual("10000", group_only["group_filters"][0]["value"])
        split_combinations = _metric(
            analysis_supported_combinations=[
                {
                    "row_fields": ["price_to_ddp_ratio"],
                    "group_fields": [],
                    "stages": ["row"],
                },
                {
                    "row_fields": [],
                    "group_fields": ["metric_value", "any_overdue_days"],
                    "stages": ["group"],
                },
            ]
        )
        with self.assertRaises(analysis_contract.AnalysisContractError) as cross:
            analysis_contract.validate_analysis_for_metric(
                {
                    "row_filters": [
                        {"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}
                    ],
                    "group_filters": [
                        {"field": "metric_value", "op": "gte", "value": 10000}
                    ],
                },
                split_combinations,
                metric_code="probe_metric",
            )
        self.assertEqual("ANALYSIS_UNSUPPORTED_COMBINATION", cross.exception.code)
        with self.assertRaises(analysis_contract.AnalysisContractError) as caught:
            analysis_contract.validate_analysis_for_metric(
                {
                    "group_filters": [
                        {"field": "unknown", "op": "eq", "value": 1}
                    ]
                },
                _metric(),
                metric_code="probe_metric",
            )
        self.assertEqual("ANALYSIS_UNSUPPORTED", caught.exception.code)

    def test_calculation_fact_aliases_are_optional_and_defaulted(self):
        envelope = request_contract.validate_query_envelope(
            {
                "requests": [
                    {"request_id": "q1", "domain": "inventory", "metric": "m"},
                    {"request_id": "q2", "domain": "inventory", "metric": "m"},
                ],
                "calculations": [
                    {
                        "calculation_id": "c1",
                        "operation": "difference",
                        "left_request_id": "q1",
                        "right_request_id": "q2",
                    }
                ],
            }
        )
        self.assertEqual("metric_value", envelope.calculations[0]["left_field"])
        self.assertEqual("metric_value", envelope.calculations[0]["right_field"])

    def test_wire_compaction_preserves_analysis_context(self):
        context = {
            "conditions": [{"stage": "group", "field": "metric_value"}],
            "scope_fingerprint": "sha256_test",
            "unknown_count": 1,
        }
        payload = {
            "status": "success",
            "results": [
                {
                    "request_id": "q1",
                    "claim_ledger": [],
                    "analysis_context": context,
                }
            ],
        }
        compacted = wire.compact_query_payload(payload)
        self.assertEqual(context, compacted["results"][0]["analysis_context"])


if __name__ == "__main__":
    unittest.main()
