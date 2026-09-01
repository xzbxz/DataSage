from __future__ import annotations

import json
import importlib
import os
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
sys.path.insert(0, str(PROFILE_ROOT / "tests"))

import test_business_contracts as tb  # noqa: E402

entitlements = importlib.import_module(f"{tb.TEST_PACKAGE}.entitlements")


def _request(domain: str, metric: str, **overrides):
    value = {
        "request_id": f"remediation_{metric}",
        "domain": domain,
        "metric": metric,
        "detail_receipt": tb.BusinessContractTests._metric_detail_receipt(domain, metric),
    }
    value.update(overrides)
    return value


def _build(request):
    normalized = tb.tools._validate_request(request)
    datasets, semantics = tb.tools._contracts(str(normalized["domain"]))
    normalized = tb.tools._validate_metric_detail_gate(normalized, semantics)
    return tb.tools._build_metric_query(
        normalized,
        datasets,
        semantics,
        tb.tools._metric_query_limit(normalized),
        observed_on=date(2026, 8, 18),
    )


class ToolsCoreRemediationTests(unittest.TestCase):
    def test_multiple_currencies_require_grouping(self):
        with self.assertRaises(tb.tools.QueryFailure) as caught:
            _build(
                _request(
                    "inventory",
                    "current_inventory_amount_original",
                    metric_filters={"currency": ["CNY", "USD"]},
                )
            )
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", caught.exception.code)

    def test_multiple_units_require_grouping(self):
        with self.assertRaises(tb.tools.QueryFailure) as caught:
            _build(
                _request(
                    "inventory",
                    "current_inventory_quantity",
                    metric_filters={"unit": ["m", "kg"]},
                )
            )
        self.assertEqual("UNIT_SCOPE_REQUIRED", caught.exception.code)

    def test_snapshot_scalar_rejects_multiple_months(self):
        with self.assertRaises(tb.tools.QueryFailure) as caught:
            _build(
                _request(
                    "receivable",
                    "current_debt_amount",
                    time_range={"start": "2026-07-01", "end": "2026-09-01"},
                )
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_month_metric_rejects_day_bucket(self):
        with self.assertRaises(tb.tools.QueryFailure) as caught:
            _build(
                _request(
                    "inventory",
                    "month_end_inventory_cost_rmb",
                    time_range={"start": "2026-08-01", "end": "2026-09-01"},
                    time_bucket="day",
                )
            )
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_sum_product_many_coalesces_every_addend(self):
        datasets, semantics = tb.tools._contracts("receivable")
        metric = semantics["metrics"]["aging_over_90_amount"]
        dataset = datasets["datasets"][metric["table"]]
        sql = tb.tools._metric_aggregation_sql(
            metric,
            dataset,
            datasets,
            "f",
        )
        self.assertEqual(len(metric["measure_columns"]), sql.count("COALESCE(`f`."))

    def test_model_claim_keeps_currency_and_analytical_components(self):
        rows = [
            {
                "metric_value": "100",
                "currency_no": "USD",
                "settlement_band": "0-30",
                "sample_bill_count": 5,
                "net_delivery_amount_rmb": "100",
                "net_receipt_amount_rmb": "80",
                "receipt_coverage": "0.8",
            }
        ]
        claim = tb.tools._claim_ledger(
            "claim_projection",
            "metric_projection",
            "projection",
            None,
            [{"label": "币种", "fields": ["currency_no"]}],
            {"start": "2026-08-01", "end": "2026-09-01"},
            "scope",
            "projection",
            False,
            rows,
        )[0]
        self.assertEqual([{"label": "币种", "value": "USD"}], claim["dimensions"])
        for field in (
            "settlement_band",
            "sample_bill_count",
            "net_delivery_amount_rmb",
            "net_receipt_amount_rmb",
            "receipt_coverage",
        ):
            self.assertIn(field, claim["facts"])

    def test_display_projection_removes_format_controls(self):
        rendered = tb.tools._safe_display_value("A\u202eB\u2066C\u200bD")
        self.assertIsNotNone(rendered)
        self.assertFalse(any(ord(char) in {0x202E, 0x2066, 0x200B} for char in rendered))

    def test_coarse_denial_happens_before_business_validation(self):
        with (
            mock.patch.object(entitlements, "coarse_authorized", return_value=False),
            mock.patch.object(tb.tools, "_validate_query_dispatch") as validate,
        ):
            payload = json.loads(
                tb.tools.entitlement_guarded_datasage_query(
                    {"requests": [{"domain": "delivery", "metric": "unknown"}]}
                )
            )
        self.assertEqual("DATA_ENTITLEMENT_DENIED", payload["error"]["code"])
        validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
