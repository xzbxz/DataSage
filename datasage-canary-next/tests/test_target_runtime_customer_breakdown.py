from __future__ import annotations

import importlib
import os
from datetime import date
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

TEST_PACKAGE = "datasage_query_target_runtime_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

analytical_queries = importlib.import_module(f"{TEST_PACKAGE}.analytical_queries")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class TargetRuntimeCustomerBreakdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.datasets, cls.semantics = contracts.execution_contracts("target")

    def _request(
        self,
        metric: str,
        *,
        attribution_mode: str = "salesperson_allocation",
        dimensions: list[str] | None = None,
        time_range: dict[str, str] | None = None,
        calendar_month: str | None = None,
        time_bucket: str | None = None,
        complete_target_gap_decomposition: dict[str, str] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "request_id": f"target_runtime_{metric}",
            "domain": "target",
            "mode": "metric",
            "metric": metric,
            "attribution_mode": attribution_mode,
        }
        if dimensions is not None:
            request["dimensions"] = dimensions
        if time_range is not None:
            request["time_range"] = time_range
        if calendar_month is not None:
            request["calendar_month"] = calendar_month
        if time_bucket is not None:
            request["time_bucket"] = time_bucket
        if complete_target_gap_decomposition is not None:
            request["complete_target_gap_decomposition"] = (
                complete_target_gap_decomposition
            )
        return request

    def _build(self, request: dict[str, object]):
        normalized = tools._validate_request(request)
        normalized = tools._validate_metric_contract(normalized, self.semantics)
        return tools._build_metric_query(
            normalized,
            self.datasets,
            self.semantics,
            tools._metric_query_limit(normalized),
            observed_on=date(2026, 9, 4),
        )

    def test_delivery_and_receipt_completion_compile_salesperson_customer(self):
        expected_sources = {
            "delivery_target_completion": [
                "vk_dwd.delivery_target_split_dwd",
                "vk_dwd.sale_bill_split_dwd",
                "vk_dwd.delivery_return_detail_dwd",
            ],
            "receipt_target_completion": [
                "vk_dwd.receive_target_split_dwd",
                "vk_dwd.receive_bill_split_dwd",
                "vk_dwd.receive_return_bill_split_dwd",
                "vk_dwd.receive_bill_detail_dwd",
                "vk_dwd.receive_return_bill_detail_dwd",
            ],
        }
        for metric, source_datasets in expected_sources.items():
            with self.subTest(metric=metric):
                _sql, _params, scope = self._build(
                    self._request(
                        metric,
                        dimensions=["salesperson", "customer"],
                        calendar_month="2026-08",
                    )
                )
                self.assertEqual(source_datasets, scope["source_datasets"])
                self.assertEqual(
                    ["sales_name", "customer_name"],
                    scope["dimension_outputs"],
                )

    def test_allocated_net_inherits_canonical_path_dimensions_before_preflight(self):
        expected_dimensions = {"salesperson", "department", "organization", "customer", "currency"}
        for metric in ("allocated_net_delivery_amount", "allocated_net_receipt_amount"):
            with self.subTest(metric=metric):
                metric_definition = self.semantics["metrics"][metric]
                self.assertNotIn("allowed_dimensions", metric_definition)
                allowed = tools._metric_allowed_dimensions(
                    self._request(metric),
                    metric_definition,
                    self.semantics,
                )
                self.assertEqual(expected_dimensions, allowed)
                _sql, _params, scope = self._build(
                    self._request(
                        metric,
                        dimensions=["salesperson", "customer"],
                        calendar_month="2026-08",
                    )
                )
                self.assertEqual(["sales_name", "customer_name"], scope["dimension_outputs"])

    def test_receipt_split_target_rejects_partial_month(self):
        with self.assertRaises(tools.QueryFailure) as caught:
            self._build(
                self._request(
                    "receipt_target_completion",
                    dimensions=["salesperson"],
                    time_range={"start": "2026-08-15", "end": "2026-09-15"},
                )
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_receipt_target_month_uses_date_bounds_and_date_month_key(self):
        sql, params, _scope = self._build(
            self._request(
                "receipt_target_completion",
                dimensions=["salesperson", "customer"],
                calendar_month="2026-08",
                time_bucket="month",
            )
        )
        self.assertEqual(["2026-08-01", "2026-09-01"], params[:2])
        self.assertIn(
            "GROUP BY DATE_FORMAT(`t`.`plan_receive_time`, '%%Y-%%m'), `t`.`sales_id`, `t`.`customer_id`",
            sql,
        )
        self.assertNotIn(
            "GROUP BY `t`.`plan_receive_time`, `t`.`sales_id`, `t`.`customer_id`",
            sql,
        )

    def test_future_target_keeps_structured_future_state(self):
        sql, _params, _scope = self._build(
            self._request(
                "delivery_target_completion",
                attribution_mode="transaction_detail",
                calendar_month="2099-01",
            )
        )
        self.assertIn("'not_started' IN ('not_started', 'includes_future')", sql)
        self.assertIn("THEN 'not_set_for_future'", sql)

    def test_missing_target_gap_is_null_and_not_claimed_as_numeric_gap(self):
        sql, _params, _scope = self._build(
            self._request(
                "delivery_target_completion",
                attribution_mode="transaction_detail",
                calendar_month="2026-08",
            )
        )
        self.assertIn("OR COALESCE(t.__matched_row_count, 0) = 0", sql)
        self.assertFalse(
            tools._target_status_is_coherent(
                {
                    "target_amount_rmb": "0",
                    "actual_amount_rmb": "100",
                    "gap_amount_rmb": "-100",
                    "completion_rate": None,
                    "metric_value": None,
                },
                {
                    "target_data_state": "missing",
                    "actual_data_state": "reported",
                    "period_state": "completed",
                },
            )
        )

    def test_zero_target_keeps_undefined_completion_with_consistent_gap(self):
        self.assertTrue(
            tools._target_status_is_coherent(
                {
                    "target_amount_rmb": "0",
                    "actual_amount_rmb": "100",
                    "gap_amount_rmb": "-100",
                    "completion_rate": None,
                    "metric_value": None,
                },
                {
                    "target_data_state": "zero",
                    "actual_data_state": "reported",
                    "period_state": "completed",
                },
            )
        )

    def test_no_target_or_actual_rows_preserve_structured_state(self):
        row = {
            "metric_value": None,
            "completion_rate": None,
            "target_amount_rmb": 0,
            "actual_amount_rmb": None,
            "gap_amount_rmb": None,
            "target_data_state": "not_set_for_future",
            "actual_data_state": "not_started",
            "period_state": "not_started",
            tools._INTERNAL_MATCH_COUNT: 0,
        }
        public_rows, data_state = tools._evidence_rows_and_state([row], False)
        self.assertEqual("undefined", data_state)
        self.assertEqual("not_set_for_future", public_rows[0]["target_data_state"])
        self.assertNotIn(tools._INTERNAL_MATCH_COUNT, public_rows[0])

        generic_rows, generic_state = tools._evidence_rows_and_state(
            [{"metric_value": 0, tools._INTERNAL_MATCH_COUNT: 0}], False
        )
        self.assertEqual([], generic_rows)
        self.assertEqual("empty", generic_state)

    def test_target_gap_rejects_time_bucket_before_query_planning(self):
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request(
                self._request(
                    "delivery_target_completion",
                    attribution_mode="transaction_detail",
                    calendar_month="2026-08",
                    time_bucket="month",
                    complete_target_gap_decomposition={"dimension": "customer"},
                )
            )
        self.assertEqual("INVALID_INPUT", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
