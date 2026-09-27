"""Static detail-page contract tests for the candidate helper.

The helper is loaded from its exact candidate file through a unique importlib module
name.  This test does not prepend the plugin directory to ``sys.path`` and does not
connect to a database, model, host state, session store or service.
"""

from __future__ import annotations

import math
from importlib import util as importlib_util
from pathlib import Path
import unittest


_DETAIL_PAGES_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "datasage-query"
    / "detail_pages.py"
)
_DETAIL_PAGES_SPEC = importlib_util.spec_from_file_location(
    "datasage_detail_pages_candidate_static_test",
    _DETAIL_PAGES_PATH,
)
if _DETAIL_PAGES_SPEC is None or _DETAIL_PAGES_SPEC.loader is None:
    raise ImportError("candidate detail_pages.py spec unavailable")
_DETAIL_PAGES = importlib_util.module_from_spec(_DETAIL_PAGES_SPEC)
_DETAIL_PAGES_SPEC.loader.exec_module(_DETAIL_PAGES)

DetailCursorError = _DETAIL_PAGES.DetailCursorError
DetailReconciliationError = _DETAIL_PAGES.DetailReconciliationError
DetailValidationError = _DETAIL_PAGES.DetailValidationError
encode_cursor = _DETAIL_PAGES.encode_cursor
normalize_detail = _DETAIL_PAGES.normalize_detail
paginate = _DETAIL_PAGES.paginate
validate_metric_detail = _DETAIL_PAGES.validate_metric_detail

INVENTORY_CONTRACT = {
    "id": "inventory_product_groups",
    "requires_analysis": True,
    "required_dimensions": ["product", "unit"],
    "allowed_analysis_fields": ["price_to_ddp_ratio"],
    "summable_fields": ["gross_rolls", "return_rolls"],
    "default_page_size": 25,
    "max_page_size": 50,
    "max_pages": 20,
    "max_collection": 500,
}
RECEIVABLE_CONTRACT = {
    "id": "receivable_customer_groups",
    "requires_analysis": True,
    "required_dimensions": ["customer"],
    "allowed_analysis_fields": ["metric_value"],
    "summable_fields": ["metric_value"],
    "default_page_size": 25,
    "max_page_size": 50,
    "max_pages": 20,
    "max_collection": 500,
}
TARGET_CONTRACT = {
    "id": "target_department_groups",
    "requires_analysis": True,
    "required_dimensions": ["department"],
    "allowed_analysis_fields": ["completion_rate"],
    "summable_fields": ["target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb"],
    "default_page_size": 25,
    "max_page_size": 50,
    "max_pages": 20,
    "max_collection": 500,
}


def _view(*, product="P1", unit="m", gross=None, net=None, states=None, currency="CNY"):
    facts = {}
    if gross is not None:
        facts["gross_rolls"] = gross
    if net is not None:
        facts["net_rolls"] = net
    return {
        "dimensions": {"product": product},
        "facts": facts,
        "states": dict(states or {}),
        "unit": unit,
        "currency": currency,
        "fact_units": {"gross_rolls": "卷", "net_rolls": "卷"},
    }


class NormalizeDetailProposalTests(unittest.TestCase):
    def test_default_page_size_and_closed_cursor_shape(self):
        detail = normalize_detail({"id": "inventory_product_groups"})
        self.assertEqual(25, detail["limit"])
        self.assertEqual(detail, normalize_detail(detail))
        cursor = encode_cursor(
            page=1,
            page_size=25,
            scope_digest="scope_v1_abc",
            data_digest="data_v1_def",
        )
        parsed = normalize_detail({"id": "inventory_product_groups", "cursor": cursor})
        self.assertEqual(cursor, parsed["cursor"])
        self.assertEqual(parsed, normalize_detail(parsed))

    def test_unknown_detail_field_and_malformed_cursor_fail_closed(self):
        with self.assertRaises(DetailValidationError):
            normalize_detail({"id": "inventory_product_groups", "raw": "x"})
        with self.assertRaises(DetailValidationError):
            normalize_detail({"id": "inventory_product_groups", "cursor": None})
        with self.assertRaises(DetailValidationError):
            normalize_detail({"id": "inventory_product_groups", "cursor": "!!!"})


class MetricDetailValidationProposalTests(unittest.TestCase):
    def test_exact_dimensions_and_no_top_level_limit(self):
        metric = {"detail_contract": INVENTORY_CONTRACT}
        request = {
            "metric": "registered_slow_pool_baseline_net_outbound",
            "detail": {"id": "inventory_product_groups"},
            "dimensions": ["product", "unit"],
            "analysis": {"row_filters": [{"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}]},
        }
        self.assertIsNotNone(validate_metric_detail(request, metric))
        with self.assertRaises(DetailValidationError):
            validate_metric_detail({**request, "limit": 2}, metric)
        with self.assertRaises(DetailValidationError):
            validate_metric_detail({**request, "dimensions": ["product"]}, metric)

    def test_receivable_detail_does_not_authorize_open_item_join(self):
        metric = {"detail_contract": RECEIVABLE_CONTRACT}
        request = {
            "metric": "current_debt_amount",
            "detail": {"id": "receivable_customer_groups"},
            "dimensions": ["customer"],
            "analysis": {"group_filters": [{"field": "metric_value", "op": "gte", "value": "0"}]},
        }
        self.assertIsNotNone(validate_metric_detail(request, metric))


class PaginationProposalTests(unittest.TestCase):
    def test_full_and_page_subtotals_are_separate(self):
        views = [
            _view(product="P1", gross="10", net=None),
            _view(product="P2", gross="5", net=None),
        ]
        result = paginate(
            views,
            {"id": "inventory_product_groups", "limit": 1},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            100,
        )
        self.assertEqual("15", result["reconciliation"]["full"]["full_subtotals"]["gross_rolls"])
        self.assertNotIn("net_rolls", result["reconciliation"]["full"]["full_subtotals"])
        self.assertEqual("10", result["reconciliation"]["page"]["full_subtotals"]["gross_rolls"])
        self.assertEqual("not_guaranteed_old_snapshot", result["snapshot_continuity"])

    def test_environment_cap_reduces_effective_page_size_without_expansion(self):
        result = paginate(
            [_view(product="P1", gross="1")],
            {"id": "inventory_product_groups", "limit": 25},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            10,
        )
        self.assertEqual(25, result["requested_page_size"])
        self.assertEqual(10, result["effective_page_size"])

    def test_changed_data_or_scope_rejects_cursor(self):
        first = paginate(
            [_view(gross="1")],
            {"id": "inventory_product_groups", "limit": 1},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            100,
        )
        cursor = first["next_cursor"]
        self.assertIsNone(cursor)
        first = paginate(
            [_view(gross="1"), _view(product="P2", gross="2")],
            {"id": "inventory_product_groups", "limit": 1},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            100,
        )
        with self.assertRaises(DetailCursorError):
            paginate(
                [_view(gross="1"), _view(product="P2", gross="3")],
                {"id": "inventory_product_groups", "limit": 1, "cursor": first["next_cursor"]},
                {"scope_digest": "scope_v1_abc"},
                INVENTORY_CONTRACT,
                100,
            )

    def test_collection_cap_and_page_count_are_bounded(self):
        views = [_view(product=f"P{index}", gross="1") for index in range(4)]
        with self.assertRaises(DetailValidationError):
            paginate(
                views,
                {"id": "inventory_product_groups", "limit": 1},
                {"scope_digest": "scope_v1_abc"},
                {**INVENTORY_CONTRACT, "max_pages": 2},
                100,
            )
        with self.assertRaises(DetailValidationError):
            paginate(
                views,
                {"id": "inventory_product_groups", "limit": 1},
                {"scope_digest": "scope_v1_abc"},
                INVENTORY_CONTRACT,
                3,
            )

    def test_public_float_decimal_and_cross_unit_subtotals(self):
        float_result = paginate(
            [_view(gross=1.1)],
            {"id": "inventory_product_groups"},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            100,
        )
        self.assertEqual("1.1", float_result["reconciliation"]["full"]["full_subtotals"]["gross_rolls"])
        with self.assertRaises(DetailReconciliationError):
            paginate(
                [_view(gross=math.nan)],
                {"id": "inventory_product_groups"},
                {"scope_digest": "scope_v1_abc"},
                INVENTORY_CONTRACT,
                100,
            )
        mixed_fact_units = [_view(product="P1", unit="m", gross="1"), _view(product="P2", unit="y", gross="2")]
        # Source quantity units do not change roll units. Only a conflicting fact
        # unit makes the roll subtotal unknown.
        same_roll_units = paginate(mixed_fact_units, {"id": "inventory_product_groups"},
                                   {"scope_digest": "scope_v1_abc"}, INVENTORY_CONTRACT, 100)
        self.assertEqual("3", same_roll_units["reconciliation"]["full"]["full_subtotals"]["gross_rolls"])
        mixed_fact_units[1]["fact_units"]["gross_rolls"] = "米"
        result = paginate(
            mixed_fact_units,
            {"id": "inventory_product_groups"},
            {"scope_digest": "scope_v1_abc"},
            INVENTORY_CONTRACT,
            100,
        )
        self.assertEqual("unknown", result["reconciliation"]["full"]["status"])
        self.assertIsNone(result["reconciliation"]["full"]["full_subtotals"]["gross_rolls"])

    def test_analysis_match_unknown_does_not_create_matched_total(self):
        contract = {
            **TARGET_CONTRACT,
        }
        views = [
            {
                "dimensions": {"department": "D1"},
                "facts": {"target_amount_rmb": "100", "actual_amount_rmb": "50", "gap_amount_rmb": "50"},
                "states": {"analysis_match_state": "unknown"},
                "unit": "人民币元",
                "currency": "CNY",
                "fact_units": {
                    "target_amount_rmb": "人民币元",
                    "actual_amount_rmb": "人民币元",
                    "gap_amount_rmb": "人民币元",
                },
            }
        ]
        result = paginate(
            views,
            {"id": "target_department_groups"},
            {"scope_digest": "scope_v1_abc"},
            contract,
            100,
        )
        self.assertIsNone(result["reconciliation"]["full"]["matched_subtotals"]["gap_amount_rmb"])
        self.assertIsNone(result["reconciliation"]["full"]["known_matched_subtotals"]["gap_amount_rmb"])


if __name__ == "__main__":
    unittest.main()
