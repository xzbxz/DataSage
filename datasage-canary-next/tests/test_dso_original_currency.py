"""Offline original-currency formal DSO checks through the public handler.

The shared RemainingCaseTests seam uses only an in-memory SQLite snapshot.  No
real database, profile, model, or network transport is involved.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

import test_remediation_remaining_cases as public


class DsoOriginalCurrencyTests(unittest.TestCase):
    START = "2026-02-01"
    END = "2026-04-01"

    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        for table, columns in {
            "vk_dw.customer_debt_bymonth_dw": {
                "debt_amount": "REAL",
                "currency_no": "TEXT",
                "customer_id": "TEXT",
                "customer_name": "TEXT",
                "customer_dept": "TEXT",
            },
            "vk_dwd.sale_bill_goods_detail_dwd": {
                "delivery_amount": "REAL",
                "currency_no": "TEXT",
                "customer_id": "TEXT",
                "customer_name": "TEXT",
            },
        }.items():
            existing = {
                row[1]
                for row in self.h.conn.execute(f"PRAGMA {table.split('.')[0]}.table_info({table.split('.')[1]})")
            }
            for column, sql_type in columns.items():
                if column not in existing:
                    self.h.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                    )

    def _clear(self):
        self.h.conn.execute("DELETE FROM vk_dw.customer_debt_bymonth_dw")
        self.h.conn.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd")

    def _seed(
        self,
        debts=(100, 120, 140),
        deliveries=(130, 170),
        *,
        debt_currency="USD",
        delivery_currency="USD",
        customer_id="C1",
        customer_name="Synthetic customer",
        missing_debt_month=None,
        debt_amounts_rmb=None,
        delivery_amounts_rmb=None,
    ):
        self._clear()
        debt_months = ["2026-01", "2026-02", "2026-03"]
        debt_rows = []
        for index, (month, amount) in enumerate(zip(debt_months, debts)):
            if month == missing_debt_month:
                continue
            rmb = (
                debt_amounts_rmb[index]
                if debt_amounts_rmb is not None
                else amount
            )
            debt_rows.append(
                (amount, rmb, month, "n", debt_currency, customer_id, customer_name, "D1")
            )
        self.h.insert(
            "vk_dw.customer_debt_bymonth_dw",
            "debt_amount,debt_amount_rmb,bill_date,is_inner_cus,currency_no,customer_id,customer_name,customer_dept",
            debt_rows,
        )
        delivery_rows = []
        for index, (month, amount) in enumerate(zip(["2026-02", "2026-03"], deliveries)):
            rmb = (
                delivery_amounts_rmb[index]
                if delivery_amounts_rmb is not None
                else amount
            )
            delivery_rows.append(
                (amount, rmb, 6, "n", f"{month}-15", delivery_currency, customer_id, customer_name)
            )
        self.h.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount,delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,currency_no,customer_id,customer_name",
            delivery_rows,
        )

    def _request(self, metric, *, request_id="dso-original", dimensions=None, basis="original"):
        return public.metric(
            metric,
            "receivable",
            request_id=request_id,
            month=None,
            currency_basis=basis,
            dimensions=list(dimensions or []),
            time_range={"start": self.START, "end": self.END},
        )

    def _payload_result(self, request):
        payload = self.h.query(request)
        self.assertIn(payload.get("status"), {"success", "partial"}, payload)
        result = next(item for item in payload["results"] if item["request_id"] == request["request_id"])
        return payload, result

    def _assert_verified(
        self,
        payload,
        result,
        expected_average,
        expected_delivery,
        expected_scope=None,
    ):
        self.assertEqual("rows", result["data_state"], result)
        row = result["rows"][0]
        facts = row["facts"]
        attestation = facts["calculation_attestation"]
        self.assertEqual("verified", attestation["status"])
        self.assertEqual(
            "formal-receivable-turnover-original-calculation-attestation/v1",
            attestation["contract_version"],
        )
        self.assertEqual("original", attestation["currency_basis"])
        self.assertEqual(expected_scope or {"mode": "grouped"}, result["currency_scope"])
        self.assertEqual(expected_average, Decimal(str(facts["average_net_debt_original"])))
        self.assertEqual(expected_delivery, Decimal(str(facts["delivery_amount_original"])))
        self.assertEqual(Decimal("23.6"), Decimal(str(facts["metric_value"])))
        self.assertEqual(3, facts["snapshot_month_count"])
        self.assertEqual(2, facts["effective_month_count"])
        self.assertEqual(
            "原币金额（按币种分别计量）",
            attestation["component_units"]["average_net_debt_original"],
        )
        self.assertEqual(
            attestation["component_units"],
            row["fact_units"],
        )
        self.assertTrue(self.h.sql_trace)
        sql = "\n".join(trace["sql"] for trace in self.h.sql_trace)
        self.assertIn("debt_amount", sql)
        self.assertIn("delivery_amount", sql)
        self.assertIn("currency_no", sql)

    def test_original_single_currency_hand_calculation_and_attestation(self):
        self._seed()
        payload, result = self._payload_result(
            self._request(
                "formal_receivable_turnover_days_original",
                dimensions=["currency"],
            )
        )
        # (100/2 + 120 + 140/2) / 2 = 120; 120 * 59 / 300 = 23.6.
        self._assert_verified(
            payload,
            result,
            Decimal("120"),
            Decimal("300"),
        )

    def test_original_customer_scope_adds_currency_and_keeps_one_business_dimension(self):
        self._seed()
        payload, result = self._payload_result(
            self._request(
                "formal_receivable_turnover_days_original",
                request_id="dso-original-customer",
                dimensions=["customer"],
            )
        )
        self._assert_verified(payload, result, Decimal("120"), Decimal("300"))
        self.assertEqual(1, len(result["rows"]))

    def test_original_dso_catalog_uses_business_language_and_two_dimension_contract(self):
        semantics = public.plugin.contracts.execution_contracts("receivable")[1]
        metric = semantics["metrics"]["formal_receivable_turnover_days_original"]
        human_text = json.dumps(
            {
                "label": metric["label"],
                "business_definition": metric["business_definition"],
                "answer_contract": metric["answer_contract"],
                "disclosures": metric["disclosures"],
                "answer_note": metric["answer_note"],
                "result_fact_units": metric["result_fact_units"],
            },
            ensure_ascii=False,
        )
        self.assertIn("交易币种", human_text)
        self.assertIn("N+1", human_text)
        self.assertIn("完整自然月", human_text)
        self.assertNotIn("currency_no", human_text)
        self.assertEqual(2, metric["max_group_dimensions"])
        self.assertEqual("currency_no", metric["currency_policy"]["column"])
        self.assertEqual(
            "原币金额（按币种分别计量）",
            metric["result_fact_units"]["average_net_debt_original"],
        )

    def test_exact_original_currency_scope_rejects_multi_filter_without_group(self):
        self._seed()
        request = public.metric(
            "formal_receivable_turnover_days_original",
            "receivable",
            request_id="dso-original-multi-filter-reject",
            month=None,
            dimensions=[],
            metric_filters={"currency": ["USD", "EUR"]},
            time_range={"start": self.START, "end": self.END},
        )
        payload = self.h.query(request)
        self.assertEqual("failed", payload["status"], payload)
        self.assertEqual(
            "CURRENCY_SCOPE_REQUIRED",
            payload["results"][0]["error"]["code"],
        )
        self.assertFalse(self.h.sql_trace)

    def test_exact_original_single_filter_and_grouped_multi_filter_remain_valid(self):
        self._seed()
        single = public.metric(
            "formal_receivable_turnover_days_original",
            "receivable",
            request_id="dso-original-single-filter",
            month=None,
            dimensions=[],
            metric_filters={"currency": "USD"},
            time_range={"start": self.START, "end": self.END},
        )
        payload, result = self._payload_result(single)
        self._assert_verified(
            payload,
            result,
            Decimal("120"),
            Decimal("300"),
            {"mode": "filtered", "value": "USD"},
        )

        self._seed()
        grouped = public.metric(
            "formal_receivable_turnover_days_original",
            "receivable",
            request_id="dso-original-grouped-filter",
            month=None,
            dimensions=["currency"],
            metric_filters={"currency": ["USD", "EUR"]},
            time_range={"start": self.START, "end": self.END},
        )
        payload, result = self._payload_result(grouped)
        self._assert_verified(payload, result, Decimal("120"), Decimal("300"))

    def test_original_negative_average_remains_signed(self):
        self._seed(debts=(-20, -10, 0), deliveries=(100, 100))
        payload, result = self._payload_result(
            self._request(
                "formal_receivable_turnover_days_original",
                request_id="dso-original-negative",
                dimensions=["currency"],
            )
        )
        row = result["rows"][0]
        self.assertEqual(Decimal("-10"), Decimal(str(row["facts"]["average_net_debt_original"])))
        self.assertEqual(Decimal("-2.95"), Decimal(str(row["facts"]["metric_value"])))
        self.assertEqual("verified", row["facts"]["calculation_attestation"]["status"])

    def test_original_zero_and_negative_gross_delivery_are_undefined(self):
        for value in (0, -100):
            with self.subTest(delivery=value):
                self._seed(deliveries=(value, value))
                payload = self.h.query(
                    self._request(
                        "formal_receivable_turnover_days_original",
                        request_id=f"dso-original-denom-{value}",
                        dimensions=["currency"],
                    )
                )
                result = payload["results"][0]
                self.assertEqual("undefined", result["data_state"], result)
                row = result["rows"][0]
                self.assertIsNone(row["facts"].get("metric_value"))
                attestation = row["facts"]["calculation_attestation"]
                self.assertEqual("undefined", attestation["status"])
                self.assertFalse(attestation["guards"]["gross_delivery_denominator_positive"])

    def test_original_missing_n_plus_one_snapshot_is_undefined(self):
        self._seed(missing_debt_month="2026-02")
        payload = self.h.query(
            self._request(
                "formal_receivable_turnover_days_original",
                request_id="dso-original-missing-snapshot",
                dimensions=["currency"],
            )
        )
        result = payload["results"][0]
        self.assertEqual("undefined", result["data_state"], result)
        row = result["rows"][0]
        self.assertIsNone(row["facts"].get("metric_value"))
        self.assertFalse(
            row["facts"]["calculation_attestation"]["guards"]["complete_month_end_snapshots"]
        )

    def test_auto_probe_counts_debt_only_and_delivery_only_groups(self):
        self._seed(
            debt_currency="USD",
            delivery_currency="EUR",
            debt_amounts_rmb=(100, 120, 140),
            delivery_amounts_rmb=(130, 170),
        )
        payload, result = self._payload_result(
            self._request(
                "formal_receivable_turnover_days",
                request_id="dso-auto-two-sided",
                basis="auto",
            )
        )
        expected_ref = "metric_" + sha256(
            "receivable\0formal_receivable_turnover_days".encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(expected_ref, result["business_metric_ref"])
        self.assertTrue(any("currency_scope_keys" in trace["sql"] for trace in self.h.sql_trace))
        probe_rows = self.h.sql_trace[0].get("database_rows") or []
        self.assertEqual(2, probe_rows[0]["currency_count"])
        self.assertEqual(0, probe_rows[0]["unknown_currency_groups"])

    def test_auto_unknown_currency_fails_without_amount(self):
        self._seed(debt_currency=None)
        payload = self.h.query(
            self._request(
                "formal_receivable_turnover_days",
                request_id="dso-auto-unknown",
                basis="auto",
            )
        )
        self.assertEqual("failed", payload["status"], payload)
        self.assertEqual(
            "CURRENCY_SCOPE_UNKNOWN",
            payload["results"][0]["error"]["code"],
        )
        self.assertEqual(1, len(self.h.sql_trace))
        self.assertEqual(1, self.h.sql_trace[0]["database_rows"][0]["unknown_currency_groups"])

    def test_explicit_original_unknown_group_hides_average_and_denominator(self):
        self._seed(debt_currency=None, delivery_currency=None)
        payload = self.h.query(
            self._request(
                "formal_receivable_turnover_days_original",
                request_id="dso-original-unknown-group",
                dimensions=["currency"],
            )
        )
        result = payload["results"][0]
        self.assertEqual("undefined", result["data_state"], result)
        facts = result["rows"][0]["facts"]
        self.assertIsNone(facts.get("average_net_debt_original"))
        self.assertIsNone(facts.get("delivery_amount_original"))
        self.assertIsNone(facts.get("metric_value"))

    def test_original_attestation_scope_units_basis_and_value_tampering_fail_closed(self):
        self._seed()
        original = public.plugin.tools._formal_dso_calculation_attestation
        for field in ("currency_basis", "currency_scope", "component_units", "component_values"):
            with self.subTest(field=field):
                self._clear()
                self._seed()

                def corrupt(*args, _field=field, **kwargs):
                    attestation = original(*args, **kwargs)
                    if _field == "currency_basis":
                        attestation["currency_basis"] = "rmb"
                    elif _field == "currency_scope":
                        attestation["currency_scope"] = {"mode": "filtered", "value": "EUR"}
                    elif _field == "component_units":
                        attestation["component_units"] = {
                            **attestation["component_units"],
                            "delivery_amount_original": "人民币元",
                        }
                    else:
                        attestation["component_values"] = {
                            **attestation["component_values"],
                            "delivery_amount_original": "999",
                        }
                    return attestation

                with patch.object(public.plugin.tools, "_formal_dso_calculation_attestation", side_effect=corrupt):
                    payload = self.h.query(
                        self._request(
                            "formal_receivable_turnover_days_original",
                            request_id=f"dso-original-tamper-{field}",
                            dimensions=["currency"],
                        )
                    )
                result = payload["results"][0]
                self.assertNotEqual("rows", result.get("data_state"), result)
                rendered = json.dumps(result, ensure_ascii=False)
                self.assertNotIn('"metric_value": 23.6', rendered)


if __name__ == "__main__":
    unittest.main()
