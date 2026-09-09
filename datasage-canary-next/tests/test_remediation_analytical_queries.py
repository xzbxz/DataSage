from __future__ import annotations

import copy
from datetime import date
import importlib
import os
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

TEST_PACKAGE = "datasage_query_remediation_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

analytical_queries = importlib.import_module(f"{TEST_PACKAGE}.analytical_queries")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class AnalyticalQueryRemediationTests(unittest.TestCase):
    @staticmethod
    def _metric(domain: str, code: str) -> tuple[dict, dict]:
        datasets, semantics = contracts.execution_contracts(domain)
        return datasets, semantics["metrics"][code]

    def test_inventory_turnover_rejects_current_or_future_explicit_month(self) -> None:
        request = {
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"}
        }
        with self.assertRaises(analytical_queries.AnalysisQueryError) as caught:
            analytical_queries._inventory_turnover_period(
                request,
                12,
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

        datasets, semantics = contracts.execution_contracts("inventory")
        metric = semantics["metrics"]["inventory_turnover_days"]
        with self.assertRaises(analytical_queries.AnalysisQueryError) as dispatched:
            analytical_queries.build_analytical_metric_query(
                {**request, "metric": "inventory_turnover_days"},
                metric,
                datasets,
                semantics,
                10,
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("INVALID_PLAN", dispatched.exception.code)

    def test_formal_dso_rejects_window_ending_in_current_month(self) -> None:
        datasets, metric = self._metric(
            "customer_risk", "formal_receivable_turnover_days"
        )
        request = {
            "metric": "formal_receivable_turnover_days",
            "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2025-09-01", "end": "2026-09-01"},
        }
        with self.assertRaises(analytical_queries.AnalysisQueryError) as caught:
            analytical_queries._formal_dso_query(
                request,
                metric,
                datasets,
                10,
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_inventory_coverage_is_per_group_and_default_is_only_a_candidate(self) -> None:
        datasets, semantics = contracts.execution_contracts("inventory")
        metric = semantics["metrics"]["inventory_turnover_days"]
        request = {"dimensions": [], "metric_filters": {}}
        sql, params, _scope = analytical_queries._inventory_turnover_query(
            request,
            metric,
            datasets,
            semantics,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertNotIn("global_months AS", sql)
        self.assertIn("entity_snapshot_present", sql)
        self.assertIn("cost_missing_value_count", sql)
        latest_start = sql.index("latest_available AS")
        latest_end = sql.index("bounds AS", latest_start)
        latest_sql = sql[latest_start:latest_end]
        self.assertIn("`bill_date` < %s", latest_sql)
        self.assertIn("<> 0", latest_sql)
        self.assertNotIn("`ddp_amount_rmb`", latest_sql)
        self.assertEqual("2026-08", params[0])

    def test_target_completion_caps_actual_at_observation_day(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        metric = semantics["metrics"]["delivery_target_completion"]
        request = {
            "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail",
            "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
        }
        sql, params, _scope = analytical_queries._target_completion_query(
            request,
            metric,
            datasets,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertIn("__actual_null_count", sql)
        self.assertEqual(2, params.count("2026-08-19"))
        self.assertNotIn("2026-09-01", params)
        self.assertIn("OR COALESCE(a.__actual_null_count, 0) > 0", sql)

    def test_target_actual_null_is_incomplete_not_reported_zero(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        metric = semantics["metrics"]["delivery_target_completion"]
        request = {
            "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail",
            "dimensions": [],
            "metric_filters": {},
            "calendar_month": "2026-08",
        }
        sql, _params, _scope = analytical_queries._target_completion_query(
            request,
            metric,
            datasets,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertIn("WHEN COALESCE(a.__actual_null_count, 0) > 0 THEN 'incomplete'", sql)
        self.assertIn(
            "OR COALESCE(a.__actual_null_count, 0) > 0 THEN NULL ELSE COALESCE(a.actual_amount_rmb, 0)",
            sql,
        )

    def test_salesperson_receipt_target_rejects_half_month_target_window(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        metric = semantics["metrics"]["receipt_target_completion"]
        request = {
            "metric": "receipt_target_completion",
            "attribution_mode": "salesperson_allocation",
            "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2026-07-15", "end": "2026-08-15"},
        }
        with self.assertRaises(analytical_queries.AnalysisQueryError) as caught:
            analytical_queries._target_completion_query(
                request,
                metric,
                datasets,
                10,
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_date_backed_receipt_target_keeps_date_bounds_and_month_key(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        request = {
            "request_id": "receipt_target_month",
            "domain": "target",
            "mode": "metric",
            "metric": "receipt_target_completion",
            "attribution_mode": "salesperson_allocation",
            "dimensions": ["salesperson", "customer"],
            "metric_filters": {},
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "time_bucket": "month",
        }
        normalized = tools._validate_request(request)
        normalized = tools._validate_metric_contract(normalized, semantics)
        sql, params, _scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["2026-08-01", "2026-09-01"], params[:2])
        self.assertIn(
            "GROUP BY DATE_FORMAT(`t`.`plan_receive_time`, '%%Y-%%m'), `t`.`sales_id`, `t`.`customer_id`",
            sql,
        )
        self.assertNotIn("GROUP BY `t`.`plan_receive_time`, `t`.`sales_id`, `t`.`customer_id`", sql)

    def test_delivery_year_month_target_keeps_month_value_format(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        metric = semantics["metrics"]["delivery_target_completion"]
        request = {
            "metric": "delivery_target_completion",
            "attribution_mode": "salesperson_allocation",
            "dimensions": ["salesperson", "customer"],
            "metric_filters": {},
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "time_bucket": "month",
        }
        sql, params, _scope = analytical_queries._target_completion_query(
            request,
            metric,
            datasets,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["n", "2026-08", "2026-09"], params[:3])
        self.assertIn(
            "GROUP BY `t`.`year_month`, `t`.`sales_id`, `t`.`customer_id`",
            sql,
        )

    def test_receipt_target_amounts_reject_half_month_target_window(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        cases = (
            ("receipt_allocated_target_amount", "salesperson_allocation"),
            ("receipt_target_amount", "transaction_detail"),
        )
        for metric_code, attribution_mode in cases:
            with self.subTest(metric=metric_code):
                request = {
                    "domain": "target",
                    "metric": metric_code,
                    "attribution_mode": attribution_mode,
                    "dimensions": [],
                    "metric_filters": {},
                    "time_range": {
                        "start": "2026-07-15",
                        "end": "2026-08-15",
                    },
                }
                with self.assertRaises(tools.QueryFailure) as caught:
                    tools._build_metric_query(
                        request,
                        datasets,
                        semantics,
                        10,
                        observed_on=date(2026, 8, 18),
                    )
                self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_analytical_approved_rejects_forbidden_allowed_column(self) -> None:
        with self.assertRaises(analytical_queries.AnalysisQueryError) as caught:
            analytical_queries._approved(
                "secret",
                {"allowed_columns": ["secret"], "forbidden_columns": ["secret"]},
            )
        self.assertEqual("COLUMN_NOT_ALLOWED", caught.exception.code)

    def test_allocated_metric_sql_uses_declared_split_source(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        metric = copy.deepcopy(semantics["metrics"]["allocated_net_delivery_amount"])
        request = {
            "metric": "allocated_net_delivery_amount",
            "attribution_mode": "salesperson_allocation",
            "dimensions": [],
            "metric_filters": {},
        }
        sql, _params, scope = analytical_queries.build_analytical_metric_query(
            request,
            metric,
            datasets,
            semantics,
            10,
            observed_on=date(2026, 8, 18),
        )
        self.assertTrue(sql.strip())
        self.assertEqual("available", metric["availability"]["status"])
        self.assertEqual("salesperson_allocation", metric["required_attribution_mode"])
        source_metric = semantics["metrics"][metric["source_completion_metric"]]
        source_path = source_metric["paths"][metric["source_path"]]
        self.assertEqual("salesperson_allocation", source_path["ledger"])
        self.assertEqual(
            [
                "vk_dwd.sale_bill_split_dwd",
                "vk_dwd.delivery_return_detail_dwd",
            ],
            scope["source_datasets"],
        )
        self.assertIn("`vk_dwd`.`sale_bill_split_dwd`", sql)
        self.assertIn("`vk_dwd`.`delivery_return_detail_dwd`", sql)
        self.assertNotIn("`vk_dwd`.`sale_bill_goods_detail_dwd`", sql)
        self.assertNotIn("`vk_dwd`.`delivery_target_detail_dwd`", sql)

    def test_salesperson_completion_paths_compile_with_dimension_and_filter(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
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
            ],
        }
        for metric_code, source_datasets in expected_sources.items():
            with self.subTest(metric=metric_code):
                metric = semantics["metrics"][metric_code]
                path = metric["paths"]["salesperson_allocation"]
                self.assertEqual("available", path["availability"]["status"])
                self.assertEqual("salesperson_allocation", path["ledger"])
                self.assertIn("salesperson", path["allowed_dimensions"])
                self.assertIn("salesperson", path["dimension_mappings"])
                sql, _params, scope = analytical_queries.build_analytical_metric_query(
                    {
                        "metric": metric_code,
                        "attribution_mode": "salesperson_allocation",
                        "dimensions": ["salesperson"],
                        "metric_filters": {"salesperson": "synthetic-salesperson"},
                        "calendar_month": "2026-08",
                    },
                    metric,
                    datasets,
                    semantics,
                    10,
                    observed_on=date(2026, 8, 18),
                )
                self.assertTrue(sql.strip())
                self.assertEqual(source_datasets, scope["source_datasets"])
                self.assertIn("`sales_id`", sql)
                self.assertIn("`sales_name`", sql)
                self.assertNotIn("`vk_dwd`.`sale_bill_goods_detail_dwd`", sql)
                self.assertNotIn("`vk_dwd`.`receive_bill_detail_dwd`", sql)

    def test_salesperson_customer_completion_uses_id_bound_customer_and_split_sources(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        bindings = {
            "salesperson": {
                "entity_type": "salesperson",
                "value_field": "canonical_id",
                "identity_columns": ["sales_id"],
                "filter_values": ["sales-1"],
            },
            "customer": {
                "entity_type": "customer",
                "value_field": "canonical_id",
                "identity_columns": ["customer_id"],
                "filter_values": ["customer-1"],
            },
        }
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
            ],
        }
        for metric_code, source_datasets in expected_sources.items():
            with self.subTest(metric=metric_code):
                metric = semantics["metrics"][metric_code]
                sql, params, scope = analytical_queries.build_analytical_metric_query(
                    {
                        "metric": metric_code,
                        "attribution_mode": "salesperson_allocation",
                        "dimensions": ["salesperson", "customer"],
                        "metric_filters": {
                            "salesperson": "display-name-is-not-used",
                            "customer": "display-name-is-not-used",
                        },
                        "_entity_bindings": bindings,
                        "calendar_month": "2026-08",
                    },
                    metric,
                    datasets,
                    semantics,
                    10,
                    observed_on=date(2026, 8, 18),
                )
                self.assertTrue(sql.strip())
                self.assertEqual(source_datasets, scope["source_datasets"])
                self.assertIn("`sales_id`", sql)
                self.assertIn("`customer_id`", sql)
                self.assertIn("`sales_name`", sql)
                self.assertIn("`customer_name`", sql)
                self.assertIn("sales-1", params)
                self.assertIn("customer-1", params)
                self.assertNotIn("display-name-is-not-used", params)
                self.assertNotIn("`vk_dwd`.`sale_bill_goods_detail_dwd`", sql)
                self.assertNotIn("`vk_dwd`.`delivery_target_detail_dwd`", sql)
                self.assertNotIn("`vk_dwd`.`receive_bill_detail_dwd`", sql)
                self.assertNotIn("`vk_dwd`.`receive_target_dwd`", sql)

    def test_salesperson_customer_completion_keeps_zero_missing_future_state_guards(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        for metric_code in ("delivery_target_completion", "receipt_target_completion"):
            with self.subTest(metric=metric_code):
                metric = semantics["metrics"][metric_code]
                request = {
                    "metric": metric_code,
                    "attribution_mode": "salesperson_allocation",
                    "dimensions": ["salesperson", "customer"],
                    "metric_filters": {},
                    "calendar_month": "2099-01",
                }
                sql, _params, _scope = analytical_queries._target_completion_query(
                    request,
                    metric,
                    datasets,
                    10,
                    observed_on=date(2026, 8, 18),
                )
                self.assertIn("__actual_null_count", sql)
                self.assertIn("target_data_state", sql)
                self.assertIn("not_set_for_future", sql)
                self.assertIn("WHEN COALESCE(a.__actual_null_count, 0) > 0", sql)

    def test_allocated_path_availability_is_checked_independently(self) -> None:
        datasets, semantics = contracts.execution_contracts("target")
        mutated_semantics = copy.deepcopy(semantics)
        path = mutated_semantics["metrics"]["delivery_target_completion"]["paths"][
            "salesperson_allocation"
        ]
        path["availability"] = {
            "status": "blocked",
            "error_code": "SYNTHETIC_PATH_BLOCKED",
            "message": "synthetic path gate",
        }
        metric = copy.deepcopy(
            mutated_semantics["metrics"]["allocated_net_delivery_amount"]
        )
        metric.pop("availability", None)
        request = {
            "metric": "allocated_net_delivery_amount",
            "attribution_mode": "salesperson_allocation",
            "dimensions": [],
            "metric_filters": {},
        }
        with self.assertRaises(analytical_queries.AnalysisQueryError) as caught:
            analytical_queries._allocated_amount_query(
                request,
                metric,
                datasets,
                mutated_semantics,
                10,
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("SYNTHETIC_PATH_BLOCKED", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
