"""Static SQL-contract checks for the phase-one analysis slices.

These tests do not open a database.  The guarded integration runner owns
runtime execution; this file only verifies that the finite planner and SQL
shapes preserve the declared scopes.
"""

from __future__ import annotations

import ast
from datetime import date
import importlib
from pathlib import Path
import sqlite3
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "datasage-query"

PACKAGE = "datasage_analysis_slice_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN)]
    sys.modules[PACKAGE] = package


analysis_queries = importlib.import_module(f"{PACKAGE}.analysis_queries")
contracts = importlib.import_module(f"{PACKAGE}.contracts")
analytical_queries = importlib.import_module(f"{PACKAGE}.analytical_queries")
tools = importlib.import_module(f"{PACKAGE}.tools")


class AnalysisSliceContractTests(unittest.TestCase):
    def test_backend_modules_are_parseable_without_importing_runtime(self):
        for name in ("analysis_queries.py", "analytical_queries.py", "query_builders.py", "query_sql.py"):
            ast.parse((PLUGIN / name).read_text(encoding="utf-8"))

    def test_analysis_contract_registers_only_phase_one_fields(self):
        source = (PLUGIN / "analysis_queries.py").read_text(encoding="utf-8")
        self.assertIn('"price_to_ddp_ratio"', source)
        self.assertIn('"overdue_days"', source)
        self.assertIn('"any_overdue_days"', source)
        self.assertIn('"completion_rate"', source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("text(user", source)

    def test_inventory_slice_preserves_ratio_unknown_and_existing_high_kpi(self):
        source = (PLUGIN / "analytical_queries.py").read_text(encoding="utf-8")
        self.assertIn("analysis_price_state", source)
        self.assertIn("analysis_net_state", source)
        self.assertIn("unknown_return_attribution", source)
        self.assertIn("high_qualifies", source)
        self.assertIn("0.75", (PLUGIN / "contracts" / "inventory-semantics.yaml").read_text(encoding="utf-8"))

    def test_target_slice_counts_before_filter_and_drops_rank_evidence(self):
        source = (PLUGIN / "analytical_queries.py").read_text(encoding="utf-8")
        self.assertIn("analysis_counted", source)
        self.assertIn("analysis_population_count", source)
        self.assertIn("analysis_unknown_count", source)
        self.assertIn('scope["ranking_plan"] = None', source)
        self.assertIn("analysis_match_state` IN ('match','unknown')", source)

    def test_receivable_slice_uses_same_open_detail_and_exists_quantifier(self):
        source = (PLUGIN / "analysis_queries.py").read_text(encoding="utf-8")
        self.assertIn("vk_dw.customer_debt_bymonth_dw", source)
        self.assertIn("unknown_source_time_alignment", source)
        self.assertIn("debt_group", source)
        self.assertIn("debt_latest", source)
        self.assertIn("LEFT JOIN open_group", source)
        self.assertNotIn("UNION SELECT", source)
        self.assertIn("vk_dwd.receivable_bill_detail_dwd", source)
        self.assertIn("vk_dwd.customer_credit_dwd", source)
        self.assertIn("FROM open_rows `of`", source)
        self.assertIn("detail_unsettled_amount", source)
        self.assertIn("analysis_overdue_unknown_count", source)

    @staticmethod
    def _datasets():
        return {
            "datasets": {
                "vk_dw.customer_debt_bymonth_dw": {"allowed_columns": [
                    "bill_date", "customer_id", "customer_no", "customer_name",
                    "customer_dept", "org_name", "currency_no", "debt_amount_rmb",
                    "is_inner_cus",
                ]},
                "vk_dwd.receivable_bill_detail_dwd": {"allowed_columns": [
                    "customer_id", "customer_no", "customer_name", "customer_dept",
                    "org_name", "currency_no", "detail_unsettled_amount",
                    "exchange_rate", "bill_time", "bill_status", "is_inner_cus",
                ]},
                "vk_dwd.customer_credit_dwd": {"allowed_columns": [
                    "customer_id", "org_name", "currency_no", "credit_days",
                ]},
            }
        }

    @staticmethod
    def _analysis_fields():
        return {
            "row": {"overdue_days": {
                "label": "逾期天数", "stage": "row", "unit": "自然日",
                "source_scope": "current_open_receivable_observation",
                "unknown_policy": "retain_unknown", "operation": "exists",
            }},
            "group": {
                "metric_value": {
                    "label": "客户净欠款", "stage": "group", "unit": "人民币元",
                    "currency": "RMB", "source_scope": "latest_monthly_customer_debt_snapshot",
                    "unknown_policy": "retain_unknown", "operation": "value",
                },
                "any_overdue_days": {
                    "label": "存在逾期满足条件的未结项", "stage": "group", "unit": "自然日",
                    "source_scope": "current_open_receivable_observation",
                    "unknown_policy": "retain_unknown", "operation": "exists",
                },
            },
        }

    @staticmethod
    def _metric(code="current_debt_amount"):
        return {
            "table": "vk_dw.customer_debt_bymonth_dw",
            "analysis_fields": AnalysisSliceContractTests._analysis_fields(),
            "analysis_supported_combinations": [{
                "row_fields": ["overdue_days"],
                "group_fields": ["metric_value", "any_overdue_days"],
                "stages": ["row", "group"],
            }],
            "answer_note": "synthetic",
        }

    @staticmethod
    def _sqlite():
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dw")
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute("CREATE TABLE vk_dw.customer_debt_bymonth_dw (bill_date TEXT, customer_id TEXT, customer_no TEXT, customer_name TEXT, customer_dept TEXT, org_name TEXT, currency_no TEXT, debt_amount_rmb REAL, is_inner_cus TEXT)")
        connection.execute("CREATE TABLE vk_dwd.receivable_bill_detail_dwd (customer_id TEXT, customer_no TEXT, customer_name TEXT, customer_dept TEXT, org_name TEXT, currency_no TEXT, detail_unsettled_amount REAL, exchange_rate REAL, bill_time TEXT, bill_status TEXT, is_inner_cus TEXT)")
        connection.execute("CREATE TABLE vk_dwd.customer_credit_dwd (customer_id TEXT, org_name TEXT, currency_no TEXT, credit_days REAL)")
        connection.create_function("CURDATE", 0, lambda: "2026-09-02")
        connection.create_function("DATEDIFF", 2, lambda end, start: (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days if end and start else None)
        connection.create_function("GREATEST", -1, lambda *values: None if any(v is None for v in values) else max(values))
        return connection

    def test_current_debt_interval_compiles_and_executes_with_unknown_alignment(self):
        metric = self._metric()
        request = {
            "metric": "current_debt_amount",
            "domain": "receivable",
            "dimensions": ["customer"],
            "metric_filters": {},
            "analysis": {
                "row_filters": [{"field": "overdue_days", "op": "gt", "value": "30"}],
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": "10000"},
                    {"field": "metric_value", "op": "lte", "value": "50000"},
                    {"field": "any_overdue_days", "op": "gt", "value": "30"},
                ],
            },
        }
        sql, params, _scope = analysis_queries.build_current_debt_analysis_query(
            request, metric, self._datasets(), {"dimensions": {"customer": {"filter_column": "customer_id"}}}, 20,
        )
        connection = self._sqlite()
        self.addCleanup(connection.close)
        connection.executemany("INSERT INTO vk_dw.customer_debt_bymonth_dw VALUES (?,?,?,?,?,?,?,?,?)", [
            ("2026-08", "A", "A", "甲", "D", "ORG", "CNY", 20000, "n"),
            ("2026-08", "B", "B", "乙", "D", "ORG", "CNY", 60000, "n"),
            ("2026-08", "C", "C", "丙", "D", "ORG", "CNY", 12000, "n"),
        ])
        connection.executemany("INSERT INTO vk_dwd.receivable_bill_detail_dwd VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            ("A", "A", "甲", "D", "ORG", "CNY", 1000, 1, "2026-07-01", "C", "n"),
            ("C", "C", "丙", "D", "ORG", "CNY", 1000, 1, "2026-06-01", "C", "n"),
        ])
        connection.executemany("INSERT INTO vk_dwd.customer_credit_dwd VALUES (?,?,?,?)", [("A", "ORG", "CNY", 30), ("C", "ORG", "CNY", 30)])
        rows = list(connection.execute(sql.replace("%s", "?").replace("<=>", "IS"), params))
        self.assertEqual({"A", "C"}, {row["customer_id"] for row in rows})
        self.assertTrue(all(row["analysis_temporal_state"] == "unknown_source_time_alignment" for row in rows))
        self.assertTrue(all(row["analysis_match_state"] == "unknown" for row in rows))
        self.assertTrue(all(row["net_debt_snapshot_month"] == "2026-08" for row in rows))
        self.assertTrue(all(row["open_items_observation_date"] == "2026-09-02" for row in rows))
        self.assertEqual(1, rows[0]["analysis_excluded_count"])

        empty_request = {
            **request,
            "analysis": {"group_filters": [{"field": "metric_value", "op": "gte", "value": "100000"}]},
        }
        empty_sql, empty_params, _ = analysis_queries.build_current_debt_analysis_query(
            empty_request, metric, self._datasets(), {"dimensions": {"customer": {"filter_column": "customer_id"}}}, 20,
        )
        empty_rows = list(connection.execute(empty_sql.replace("%s", "?").replace("<=>", "IS"), empty_params))
        self.assertEqual(1, len(empty_rows))
        self.assertEqual("coverage_only", empty_rows[0]["analysis_match_state"])
        self.assertEqual(0, empty_rows[0]["__matched_row_count"])
        self.assertEqual(3, empty_rows[0]["analysis_population_count"])
        self.assertEqual(3, empty_rows[0]["analysis_excluded_count"])

        open_request = {
            **request,
            "metric": "open_receivable_amount",
            "analysis": {
                "row_filters": [{"field": "overdue_days", "op": "gt", "value": "30"}],
                "group_filters": [{"field": "metric_value", "op": "gte", "value": "1000"}],
            },
        }
        open_metric = {"table": "vk_dwd.receivable_bill_detail_dwd", "analysis_fields": self._analysis_fields(), "analysis_supported_combinations": [{
            "row_fields": ["overdue_days"], "group_fields": ["metric_value", "any_overdue_days"], "stages": ["row", "group"],
        }]}
        open_sql, open_params, _ = analysis_queries.build_open_receivable_analysis_query(
            open_request, open_metric, self._datasets(), {"dimensions": {"customer": {"filter_column": "customer_id"}}}, 20,
        )
        open_rows = list(connection.execute(open_sql.replace("%s", "?").replace("<=>", "IS"), open_params))
        self.assertEqual({"A", "C"}, {row["customer_id"] for row in open_rows})
        self.assertTrue(all(row["analysis_match_state"] == "match" for row in open_rows))

    def test_price_boundary_and_target_interval_use_numeric_execution(self):
        ratio = analysis_queries.ratio_expression("f")
        predicate = f"CASE WHEN f.`ddp_price` > 0 AND f.`deal_price` >= 0 AND f.`deal_price` <= CAST(%s AS DECIMAL(38,12)) * f.`ddp_price` THEN 1 ELSE 0 END"
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE f (deal_price REAL, ddp_price REAL)")
        connection.executemany("INSERT INTO f VALUES (?,?)", [(50, 100), (60, 100), (None, 100), (50, 0)])
        result = connection.execute(f"SELECT SUM({predicate}) FROM f".replace("%s", "?"), ("0.5",)).fetchone()[0]
        self.assertEqual(1, result)

        tri_state = analysis_queries._tri_state_and([
            "completion_rate >= CAST(%s AS DECIMAL(38,12))",
            "completion_rate < CAST(%s AS DECIMAL(38,12))",
        ])
        rows = connection.execute(
            f"WITH vals(completion_rate) AS (VALUES (0.75),(0.8),(NULL)) SELECT {tri_state} AS state FROM vals".replace("%s", "?"),
            ("0", "0.8", "0", "0.8"),
        ).fetchall()
        self.assertEqual(["match", "exclude", "unknown"], [row[0] for row in rows])

    def test_scope_canonicalizes_twelve_decimal_threshold_without_exponent(self):
        request = {
            "metric": "current_debt_amount",
            "domain": "receivable",
            "dimensions": ["customer"],
            "metric_filters": {},
            "analysis": {"group_filters": [{"field": "metric_value", "op": "gte", "value": "0.000000000001"}]},
        }
        sql, _params, scope = analysis_queries.build_current_debt_analysis_query(
            request,
            self._metric(),
            self._datasets(),
            {"dimensions": {"customer": {"filter_column": "customer_id"}}},
            20,
        )
        self.assertIn("CAST(%s AS DECIMAL(38,12))", sql)
        self.assertEqual("0.000000000001", scope["analysis"]["group_filters"][0]["value"])

    def test_target_builder_executes_post_group_completion_filter(self):
        datasets, semantics = contracts.execution_contracts("target")
        request = {
            "metric": "delivery_target_completion",
            "domain": "target",
            "dimensions": ["department"],
            "metric_filters": {},
            "attribution_mode": "transaction_detail",
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "analysis": {"group_filters": [{"field": "completion_rate", "op": "lt", "value": "0.8"}]},
        }
        sql, params, scope = analytical_queries._target_completion_query(
            request, semantics["metrics"][request["metric"]], datasets, 20, observed_on=date(2026, 9, 2)
        )
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS vk_dwd")
        connection.execute("CREATE TABLE vk_dwd.delivery_target_detail_dwd (detail_id TEXT, year_month TEXT, customer_dept TEXT, detail_target_rmb REAL, is_inner_cus TEXT)")
        connection.execute("CREATE TABLE vk_dwd.sale_bill_goods_detail_dwd (goods_detail_id TEXT, delivery_time TEXT, customer_dept TEXT, delivery_amount_rmb REAL, bill_status INTEGER, is_inner_cus TEXT)")
        # The authoritative transaction-detail target path always compiles
        # its governed return component, even when the synthetic case has no
        # returns. Keep the physical return columns needed by that contract.
        connection.execute("CREATE TABLE vk_dwd.delivery_return_detail_dwd (barcode_detail_id TEXT, statement_time TEXT, customer_dept TEXT, return_amount_rmb REAL, status INTEGER, complnt_type INTEGER, channel_type INTEGER, is_inner_cus TEXT)")
        connection.executemany("INSERT INTO vk_dwd.delivery_target_detail_dwd VALUES (?,?,?,?,?)", [("ta", "2026-08", "A", 100, "n"), ("tb", "2026-08", "B", 100, "n"), ("tc", "2026-08", "C", 100, "n"), ("td", "2026-08", "D", 1000, "n"), ("te", "2026-08", "E", -1000, "n"), ("tf", "2026-08", "F", 1000, "n")])
        connection.executemany("INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (?,?,?,?,?,?)", [("ga", "2026-08-10", "A", 50, 6, "n"), ("gb", "2026-08-10", "B", 90, 6, "n"), ("gd", "2026-08-10", "D", 799.9999, 6, "n"), ("ge", "2026-08-10", "E", -799.9999, 6, "n"), ("gf", "2026-08-10", "F", 800, 6, "n")])
        adapted = sql.replace("%s", "?").replace("<=>", "IS")
        rows = list(connection.execute(adapted, params))
        self.assertEqual({"A", "D", "E"}, {row["customer_dept"] for row in rows if row["analysis_match_state"] == "match"})
        self.assertEqual(1, rows[0]["analysis_unknown_count"])
        self.assertEqual(2, rows[0]["analysis_excluded_count"])
        self.assertEqual("target_actual_groups", scope["analysis_count_grain"])

        empty_request = {
            **request,
            "analysis": {"group_filters": [{"field": "completion_rate", "op": "lt", "value": "0"}]},
        }
        empty_sql, empty_params, _ = analytical_queries._target_completion_query(
            empty_request, semantics["metrics"][empty_request["metric"]], datasets, 20, observed_on=date(2026, 9, 2)
        )
        empty_rows = list(connection.execute(empty_sql.replace("%s", "?").replace("<=>", "IS"), empty_params))
        self.assertEqual({"C"}, {row["customer_dept"] for row in empty_rows if row["analysis_match_state"] == "unknown"})

        # Remove the unknown target/actual gap before asserting the true
        # all-excluded coverage sentinel.
        connection.execute("DELETE FROM vk_dwd.delivery_target_detail_dwd")
        connection.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd")
        known_departments = [(name, name, 100, 100) for name in "ABCDEF"]
        connection.executemany("INSERT INTO vk_dwd.delivery_target_detail_dwd VALUES (?,?,?,?,?)", [(f"t{name}", "2026-08", name, target, "n") for name, _key, target, _actual in known_departments])
        connection.executemany("INSERT INTO vk_dwd.sale_bill_goods_detail_dwd VALUES (?,?,?,?,?,?)", [(f"g{name}", "2026-08-10", name, actual, 6, "n") for name, _key, _target, actual in known_departments])
        all_excluded = list(connection.execute(empty_sql.replace("%s", "?").replace("<=>", "IS"), empty_params))
        self.assertEqual(1, len(all_excluded))
        self.assertEqual("coverage_only", all_excluded[0]["analysis_match_state"])
        self.assertEqual(0, all_excluded[0]["__matched_row_count"])
        self.assertEqual(6, all_excluded[0]["analysis_population_count"])
        self.assertEqual(6, all_excluded[0]["analysis_excluded_count"])

        connection.execute("DELETE FROM vk_dwd.delivery_target_detail_dwd")
        connection.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd")
        empty_parent = list(connection.execute(empty_sql.replace("%s", "?").replace("<=>", "IS"), empty_params))
        self.assertEqual(1, len(empty_parent))
        self.assertEqual("coverage_only", empty_parent[0]["analysis_match_state"])
        self.assertEqual(0, empty_parent[0]["analysis_population_count"])
        self.assertEqual(0, empty_parent[0]["analysis_excluded_count"])

    def test_public_builder_allows_inventory_analysis_order_and_legacy_target_order(self):
        inventory_raw = {
            "request_id": "analysis-inventory-order",
            "domain": "inventory",
            "mode": "metric",
            "metric": "registered_slow_pool_baseline_net_outbound",
            "dimensions": ["unit"],
            "metric_filters": {},
            "baseline_week": "2026-W35",
            "analysis": {"row_filters": [{"field": "price_to_ddp_ratio", "op": "lte", "value": "0.5"}]},
            "order_by": {"field": "gross_rolls", "direction": "desc"},
        }
        inventory_request, inventory_datasets, inventory_semantics = tools._validate_request_plan_without_entities(
            inventory_raw, observed_on=date(2026, 9, 2)
        )
        inventory_request = tools._validate_metric_contract(inventory_request, inventory_semantics)
        inventory_sql, _params, _scope = tools._build_metric_query(
            inventory_request, inventory_datasets, inventory_semantics, 20, observed_on=date(2026, 9, 2)
        )
        self.assertIn("gross_rolls", inventory_sql)
        self.assertIn("ORDER BY", inventory_sql)

        target_raw = {
            "request_id": "legacy-target-order",
            "domain": "target",
            "mode": "metric",
            "metric": "delivery_target_completion",
            "dimensions": ["department"],
            "metric_filters": {},
            "attribution_mode": "transaction_detail",
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "order_by": {"field": "completion_rate", "direction": "asc"},
        }
        target_request, target_datasets, target_semantics = tools._validate_request_plan_without_entities(
            target_raw, observed_on=date(2026, 9, 2)
        )
        target_request = tools._validate_metric_contract(target_request, target_semantics)
        target_sql, _params, _scope = tools._build_metric_query(
            target_request, target_datasets, target_semantics, 20, observed_on=date(2026, 9, 2)
        )
        self.assertIn("completion_rate", target_sql)
        self.assertIn("ORDER BY", target_sql)


if __name__ == "__main__":
    unittest.main()
