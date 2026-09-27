"""Currency-basis boundaries for finite analysis requests.

The fixture reuses the existing public in-memory harness setup. Pure seam
tests use a copied receipt contract; the current-debt and open-receivable
cases run through the production receivable contracts and public handler,
without opening a real database or running a model.
"""

from __future__ import annotations

from copy import deepcopy
import importlib
import unittest

import test_remediation_remaining_cases as public
from test_currency_metric_pairs import CurrencyMetricPairTests


BASIS = importlib.import_module(f"{public.plugin.__name__}.currency_basis")
ERRORS = importlib.import_module(f"{public.plugin.__name__}.query_errors")


class AnalysisCurrencyBasisTests(unittest.TestCase):
    def setUp(self):
        self.harness = public.RemainingCaseTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    @staticmethod
    def _ensure_column(harness, schema, table, column, sql_type):
        columns = {
            row[1]
            for row in harness.conn.execute(f"PRAGMA {schema}.table_info({table})")
        }
        if column not in columns:
            harness.conn.execute(
                f"ALTER TABLE {schema}.{table} ADD COLUMN {column} {sql_type}"
            )

    def _receivable_schema(self):
        CurrencyMetricPairTests._receivable_schema(self, self.harness)
        for column in ("customer_no", "customer_name", "customer_dept"):
            self._ensure_column(
                self.harness,
                "vk_dw",
                "customer_debt_bymonth_dw",
                column,
                "TEXT",
            )
        self._ensure_column(
            self.harness,
            "vk_dwd",
            "receivable_bill_detail_dwd",
            "customer_dept",
            "TEXT",
        )

    def _seed_current_debt(self, *, include_eur=True):
        self._receivable_schema()
        debt_rows = [
            (20000, "2026-08", "n", 2000, "USD", "A", "A", "甲", "ORG"),
        ]
        if include_eur:
            debt_rows.append(
                (30000, "2026-08", "n", 3000, "EUR", "B", "B", "乙", "ORG")
            )
        self.harness.insert(
            "vk_dw.customer_debt_bymonth_dw",
            "debt_amount_rmb,bill_date,is_inner_cus,debt_amount,currency_no,customer_id,customer_no,customer_name,org_name",
            debt_rows,
        )
        self.harness.insert(
            "vk_dwd.receivable_bill_detail_dwd",
            "detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no,customer_dept",
            [
                (1000, 1, "C", "2026-07-01", "n", "A", "A", "甲", "ORG", "USD", "D"),
                (1000, 1, "C", "2026-07-01", "n", "B", "B", "乙", "ORG", "EUR", "D"),
            ],
        )
        self.harness.insert(
            "vk_dwd.customer_credit_dwd",
            "customer_id,org_name,currency_no,credit_days",
            [("A", "ORG", "USD", 30), ("B", "ORG", "EUR", 30)],
        )

    def _semantics(self, *, original_analysis: bool = True):
        semantics = deepcopy(
            public.plugin.contracts.execution_contracts("receipt")[1]
        )
        metrics = semantics["metrics"]
        field = {
            "label": "分析金额",
            "stage": "group",
            "unit": "人民币元",
            "source_scope": "analysis_parent",
            "unknown_policy": "retain_unknown",
            "operation": "value",
        }
        for code in ("actual_receipt_amount", "actual_receipt_amount_original"):
            metrics[code]["analysis_fields"] = {
                "row": {},
                "group": {"metric_value": dict(field)},
            }
            metrics[code]["analysis_supported_combinations"] = [
                {
                    "row_fields": [],
                    "group_fields": ["metric_value"],
                    "stages": ["group"],
                }
            ]
        metrics["actual_receipt_amount_original"]["analysis_fields"]["group"][
            "metric_value"
        ]["unit"] = "原币金额（按币种分别计量）"
        metrics["actual_receipt_amount_original"]["analysis_fields"]["group"][
            "metric_value"
        ]["currency"] = "original"
        if not original_analysis:
            metrics["actual_receipt_amount_original"].pop("analysis_fields", None)
            metrics["actual_receipt_amount_original"].pop(
                "analysis_supported_combinations", None
            )
        return semantics

    @staticmethod
    def _request(request_id="analysis-currency"):
        return public.metric(
            "actual_receipt_amount",
            "receipt",
            request_id=request_id,
            currency_basis="auto",
            analysis={
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": "10000"}
                ]
            },
        )

    def test_private_probe_removes_analysis_but_formal_request_keeps_it(self):
        semantics = self._semantics()
        request = self._request()
        prepared = BASIS.prepare_request(request, semantics)
        self.assertIn("analysis", prepared)
        captured = {}

        def builder(probe, _datasets, _semantics, _limit, *, observed_on=None):
            captured.update(probe)
            return "SELECT 1 AS __matched_row_count", [], {
                "dimension_outputs": ["currency_no"]
            }

        BASIS.build_probe(prepared, {}, semantics, builder)
        self.assertNotIn("analysis", captured)
        self.assertEqual("actual_receipt_amount_original", captured["metric"])
        self.assertEqual(["currency"], captured["dimensions"])

    def test_auto_without_analysis_keeps_legacy_plan_shape(self):
        request = public.metric(
            "actual_receipt_amount",
            "receipt",
            request_id="legacy-currency",
            currency_basis="auto",
        )
        prepared = BASIS.prepare_request(request, self._semantics())
        self.assertNotIn(
            "analysis_validations", prepared["_currency_basis_plan"]
        )

    def test_auto_multi_currency_selects_rmb_and_keeps_analysis_slice(self):
        prepared = BASIS.prepare_request(self._request(), self._semantics())
        resolved = BASIS.resolve_probe(
            prepared,
            [{"currency_count": 2, "single_currency": None, "unknown_currency_groups": 0}],
            False,
        )
        self.assertEqual("actual_receipt_amount", resolved["metric"])
        self.assertEqual("10000", resolved["analysis"]["group_filters"][0]["value"])
        self.assertEqual("rmb", resolved["_currency_basis_plan"]["resolved_basis"])

    def test_current_debt_auto_multi_currency_uses_real_public_path(self):
        self._seed_current_debt(include_eur=True)
        request = public.metric(
            "current_debt_amount",
            "receivable",
            request_id="current-debt-mixed",
            month=None,
            dimensions=["customer"],
            currency_basis="auto",
            analysis={
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": "10000"}
                ]
            },
        )
        payload = self.harness.query(request)
        result = self.harness.result(payload, "current-debt-mixed")
        self.assertEqual("success", result["status"])
        self.assertTrue(result.get("business_metric_ref"))
        self.assertIn("analysis_context", result)
        self.assertTrue(any("currency_count" in item["sql"] for item in self.harness.sql_trace))
        self.assertIn("analysis_match_state", self.harness.sql_trace[-1]["sql"])

    def test_auto_single_currency_rejects_unregistered_original_analysis(self):
        prepared = BASIS.prepare_request(
            self._request(), self._semantics(original_analysis=False)
        )
        self.assertEqual("actual_receipt_amount", prepared["metric"])
        with self.assertRaises(ERRORS.QueryFailure) as caught:
            BASIS.resolve_probe(
                prepared,
                [{"currency_count": 1, "single_currency": "USD", "unknown_currency_groups": 0}],
                False,
            )
        self.assertEqual("ANALYSIS_UNSUPPORTED", caught.exception.code)

    def test_auto_unknown_currency_fails_before_basis_selection(self):
        prepared = BASIS.prepare_request(self._request(), self._semantics())
        with self.assertRaises(ERRORS.QueryFailure) as caught:
            BASIS.resolve_probe(
                prepared,
                [{"currency_count": 1, "single_currency": "USD", "unknown_currency_groups": 1}],
                False,
            )
        self.assertEqual("CURRENCY_SCOPE_UNKNOWN", caught.exception.code)

    def test_current_debt_auto_single_currency_rejects_original_analysis_on_real_contract(self):
        self._seed_current_debt(include_eur=False)
        request = public.metric(
            "current_debt_amount",
            "receivable",
            request_id="current-debt-single",
            month=None,
            dimensions=["customer"],
            currency_basis="auto",
            analysis={
                "group_filters": [
                    {"field": "metric_value", "op": "gte", "value": "10000"}
                ]
            },
        )
        payload = self.harness.query(request)
        result = payload["results"][0]
        self.assertEqual("failed", result["status"])
        self.assertEqual("ANALYSIS_UNSUPPORTED", result["error"]["code"])

    def test_open_receivable_row_analysis_auto_is_explicitly_rejected(self):
        request = public.metric(
            "open_receivable_amount",
            "receivable",
            request_id="open-row-auto",
            month=None,
            dimensions=["customer"],
            currency_basis="auto",
            analysis={
                "row_filters": [
                    {"field": "overdue_days", "op": "gt", "value": 30}
                ]
            },
        )
        payload = self.harness.query(request)
        result = payload["results"][0]
        self.assertEqual("failed", result["status"])
        self.assertEqual(
            "ANALYSIS_UNSUPPORTED_COMBINATION", result["error"]["code"]
        )
        self.assertFalse(self.harness.sql_trace)


if __name__ == "__main__":
    unittest.main()
