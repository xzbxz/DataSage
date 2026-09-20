"""E03 pure SQL helper split and builder seam checks."""

from __future__ import annotations

from datetime import date
import importlib
import os
from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
# Keep this test runnable on its own with the candidate Profile as its home.
os.environ["HERMES_HOME"] = str(ROOT)


def _package(label: str) -> str:
    name = f"datasage_query_builders_{label}_{uuid.uuid4().hex}"
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / "plugins" / "datasage-query")]
    sys.modules[name] = package
    return name


def _tools(label: str):
    package = _package(label)
    return importlib.import_module(f"{package}.tools")


class QueryBuilderSplitTests(unittest.TestCase):
    def test_query_sql_is_execution_free_and_tools_names_remain_aliases(self) -> None:
        package = _package("pure")
        query_sql = importlib.import_module(f"{package}.query_sql")
        self.assertNotIn(f"{package}.tools", sys.modules)

        tools = importlib.import_module(f"{package}.tools")
        self.assertIs(tools._filter_clause, query_sql._filter_clause)
        self.assertIs(tools._metric_aggregation_sql, query_sql._metric_aggregation_sql)
        self.assertIs(tools._integrity_columns, query_sql._integrity_columns)
        self.assertEqual(tools._INTERNAL_MATCH_COUNT, query_sql._INTERNAL_MATCH_COUNT)
        self.assertIn(tools._INTERNAL_MATCH_COUNT, tools._INTERNAL_RESULT_FIELDS)

    def test_migrated_helpers_keep_sql_and_error_contracts(self) -> None:
        tools = _tools("helpers")
        dataset = {"allowed_columns": ["amount", "status"], "forbidden_columns": []}
        datasets = {"defaults": {"blocked_columns": []}}
        params: list[object] = []
        self.assertEqual(
            "`f`.`status` IN (%s, %s)",
            tools._filter_clause(
                "status", {"op": "in", "value": ["open", "closed"]}, params, alias="f"
            ),
        )
        self.assertEqual(["open", "closed"], params)
        self.assertEqual(
            "COALESCE(SUM(`f`.`amount`), 0)",
            tools._metric_aggregation_sql(
                {"aggregation": "sum", "measure": "amount"},
                dataset,
                datasets,
                "f",
            ),
        )
        self.assertEqual(
            [
                "CASE WHEN m > 0 THEN NULL ELSE SUM(`f`.`amount`) END AS metric_value",
                "m AS missing_value_count",
                "k AS known_value_count",
                "CASE WHEN (m) + (k) > 0 THEN 1.0 * (k) / ((m) + (k)) ELSE NULL END AS value_coverage_rate",
                "CASE WHEN (m) + (k) = 0 THEN 'missing' WHEN m = 0 THEN 'complete' WHEN k = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
            ],
            tools._integrity_columns("SUM(`f`.`amount`)", "m", "k"),
        )
        with self.assertRaises(tools.QueryFailure) as invalid_filter:
            tools._filter_clause("status", {"op": "in", "value": []}, [], alias="f")
        self.assertEqual("INVALID_PLAN", invalid_filter.exception.code)
        with self.assertRaises(tools.QueryFailure) as blocked:
            tools._approved_column("amount", {"amount"}, {"amount"})
        self.assertEqual("COLUMN_NOT_ALLOWED", blocked.exception.code)

    def test_tools_builder_patch_seams_still_reach_core_builder(self) -> None:
        tools = _tools("patch_seams")
        query_builders = importlib.import_module(f"{tools.__package__}.query_builders")
        raw_request = {
            "request_id": "ratio-seam",
            "domain": "delivery",
            "mode": "metric",
            "metric": "return_amount_rate",
            "dimensions": [],
            "calendar_month": "2026-07",
            "comparison": {"kind": "previous_period"},
        }
        request = tools._validate_request(raw_request)
        datasets, semantics = tools._contracts("delivery")
        request = tools._validate_metric_contract(request, semantics)
        metric = semantics["metrics"][request["metric"]]
        with mock.patch.object(
            query_builders,
            "_build_metric_core",
            wraps=query_builders._build_metric_core,
        ) as core:
            tools._build_ratio_metric_core(
                request,
                metric,
                datasets,
                semantics,
                observed_on=date(2026, 8, 25),
            )
        self.assertEqual(2, core.call_count)


if __name__ == "__main__":
    unittest.main()
