"""Offline currency-basis policy tests.

These tests use the existing RemainingCaseTests SQLite/snapshot seam.  They
exercise the real request preparation, probe SQL, final SQL, result projection,
and calculation guard; no expected scope is mocked into the handler.
"""
from __future__ import annotations

from hashlib import sha256
import importlib
import json
import unittest

import test_remediation_remaining_cases as public


BASIS = importlib.import_module(f"{public.plugin.__name__}.currency_basis")
QUERY_ERRORS = importlib.import_module(f"{public.plugin.__name__}.query_errors")


class CurrencyBasisPolicyTests(unittest.TestCase):
    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self._ensure_column("vk_dwd", "receive_bill_detail_dwd", "currency_no")
        self._ensure_column("vk_dwd", "receive_bill_detail_dwd", "detail_receive_amount")
        self._ensure_column("vk_dwd", "receive_return_bill_detail_dwd", "currency_no")
        self._ensure_column("vk_dwd", "receive_return_bill_detail_dwd", "exchange_rate")
        self._ensure_column("vk_dwd", "receive_return_bill_detail_dwd", "detail_deal_amount")
        self._ensure_column("vk_dwd", "receive_return_bill_detail_dwd", "detail_return_amount")
        self._ensure_column("vk_dw", "inventory_barcode_detail_dw", "ddp_amount")
        self._ensure_column("vk_dw", "inventory_barcode_detail_dw", "currency_no")

    def _ensure_column(self, schema, table, column):
        columns = {
            row[1]
            for row in self.h.conn.execute(f"PRAGMA {schema}.table_info({table})")
        }
        if column not in columns:
            self.h.conn.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN {column} TEXT")

    def _clear_receipts(self):
        self.h.conn.execute("DELETE FROM vk_dwd.receive_bill_detail_dwd")
        self.h.conn.execute("DELETE FROM vk_dwd.receive_return_bill_detail_dwd")

    def _with_days(self, rows):
        normalized = []
        for row in rows:
            if len(row) == 3:
                amount, rate, currency = row
                day = "2026-08-15"
            else:
                amount, rate, currency, day = row
            normalized.append((amount, rate, currency, day))
        return normalized

    def _receipts(self, rows):
        self._clear_receipts()
        self.h.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_receive_amount,detail_deal_amount,exchange_rate,currency_no,bill_status,bill_time",
            [
                (amount, amount, rate, currency, "C", day)
                for amount, rate, currency, day in self._with_days(rows)
            ],
        )

    def _refunds(self, rows):
        self.h.insert(
            "vk_dwd.receive_return_bill_detail_dwd",
            "detail_deal_amount,detail_return_amount,exchange_rate,currency_no,bill_status,bill_time",
            [
                (amount, amount, rate, currency, "C", day)
                for amount, rate, currency, day in self._with_days(rows)
            ],
        )

    def _semantics(self, domain):
        return public.plugin.contracts.execution_contracts(domain)[1]

    def _request(self, metric_code, *, basis="auto", request_id="r", **extra):
        return public.metric(
            metric_code,
            "receipt",
            request_id=request_id,
            currency_basis=basis,
            **extra,
        )

    def _result(self, request):
        payload = self.h.query(request)
        return self.h.result(payload, request["request_id"])

    def _payload_result(self, request):
        payload = self.h.query(request)
        return payload, self.h.result(payload, request["request_id"])

    def _fixed_metric_ref(self, domain, metric_code):
        return "metric_" + sha256(
            f"{domain}\0{metric_code}".encode("utf-8")
        ).hexdigest()[:16]

    def _assert_public_basis(self, payload, result, basis, metric_code):
        expected_unit = "人民币" if basis == "rmb" else "原币"
        contexts = payload.get("metric_contexts") or []
        context = next(
            item for item in contexts
            if item.get("business_metric_ref") == result["business_metric_ref"]
        )
        self.assertEqual(
            self._fixed_metric_ref("receipt", metric_code),
            result["business_metric_ref"],
            result,
        )
        self.assertIn(expected_unit, context["unit"], payload)
        self.assertIn("business_metric_ref", context)
        disclosures = payload.get("disclosures") or []
        selection = next(
            (
                item for item in disclosures
                if item.get("disclosure_id") == "currency.basis.selection"
                and result["request_id"] in (item.get("request_ids") or [])
            ),
            None,
        )
        self.assertIsNotNone(selection, payload)
        self.assertIn("currency.basis.selection", result.get("disclosure_refs", []))
        self.assertTrue(selection.get("text"), payload)
        if basis == "original":
            self.assertTrue(
                context.get("currency_policy") or "原币" in selection["text"],
                payload,
            )

    def _assert_receipt_evidence(self, payload, request_id):
        bundle = payload.get("evidence_bundle") or {}
        self.assertEqual(1, (bundle.get("coverage") or {}).get("request_count"))
        item = next(
            item for item in bundle.get("items", [])
            if item.get("request_id") == request_id
        )
        self.assertEqual("success", item.get("status"))

    def test_exact_metric_without_currency_basis_is_unchanged(self):
        request = public.metric(
            "actual_receipt_amount_original",
            "receipt",
            request_id="exact",
            metric_filters={"currency": "USD"},
        )
        prepared = BASIS.prepare_request(request, self._semantics("receipt"))
        self.assertEqual(request, prepared)

    def test_contract_pairs_and_rmb_only_sources_are_explicit(self):
        semantics = self._semantics("receipt")
        pairs = semantics["currency_basis_pairs"]
        self.assertIn(
            {"rmb": "actual_receipt_amount", "original": "actual_receipt_amount_original"},
            pairs,
        )
        capability = BASIS.capability("actual_receipt_amount", semantics)
        self.assertEqual({"auto", "rmb", "original"}, set(capability["supported"]))
        self.assertEqual(
            {
                "rmb": "actual_receipt_amount",
                "original": "actual_receipt_amount_original",
            },
            capability["counterparts"],
        )
        for domain, code in (
            ("profit", "customer_month_gross_profit"),
            ("target", "delivery_target_completion"),
        ):
            info = BASIS.capability(code, self._semantics(domain))
            self.assertIn("auto", info["supported"])
            self.assertIn("rmb", info["supported"])
            self.assertNotIn("original", info["supported"])
            with self.assertRaises(QUERY_ERRORS.QueryFailure) as caught:
                BASIS.prepare_request(
                    {"domain": domain, "metric": code, "currency_basis": "original"},
                    self._semantics(domain),
                )
            self.assertEqual("CURRENCY_BASIS_UNAVAILABLE", caught.exception.code)

    def test_explicit_rmb_uses_actual_metric_sql_and_row_conversion(self):
        self._receipts([(100, 1, "CNY"), (10, 7, "USD")])
        payload, result = self._payload_result(
            self._request("actual_receipt_amount", basis="rmb")
        )
        self.assertEqual(170, public.facts(result)[0]["metric_value"])
        self._assert_public_basis(payload, result, "rmb", "actual_receipt_amount")
        self._assert_receipt_evidence(payload, "r")
        sql = "\n".join(trace["sql"] for trace in self.h.sql_trace)
        self.assertIn("exchange_rate", sql)
        self.assertGreaterEqual(len(self.h.sql_trace), 1)

    def test_auto_single_currency_probes_then_filters_original(self):
        self._receipts([(10, 7, "USD"), (20, 7, "USD")])
        payload, result = self._payload_result(self._request("actual_receipt_amount"))
        self.assertEqual(30, public.facts(result)[0]["metric_value"])
        self._assert_public_basis(payload, result, "original", "actual_receipt_amount_original")
        self._assert_receipt_evidence(payload, "r")
        self.assertEqual(
            {"mode": "filtered", "value": "USD"},
            result["currency_scope"],
        )
        explicit_payload, explicit = self._payload_result(
            self._request(
                "actual_receipt_amount",
                basis="original",
                request_id="explicit",
                metric_filters={"currency": "USD"},
            )
        )
        for field in (
            "business_metric_ref",
            "business_metric_unit",
            "currency_scope",
        ):
            if field == "business_metric_unit":
                auto_value = next(item["unit"] for item in payload["metric_contexts"] if item["business_metric_ref"] == result["business_metric_ref"])
                explicit_value = next(item["unit"] for item in explicit_payload["metric_contexts"] if item["business_metric_ref"] == explicit["business_metric_ref"])
            else:
                auto_value, explicit_value = result[field], explicit[field]
            self.assertEqual(auto_value, explicit_value, field)
        auto_text = next(item["text"] for item in payload["disclosures"] if item["disclosure_id"] == "currency.basis.selection" and "r" in item.get("request_ids", []))
        explicit_text = next(item["text"] for item in explicit_payload["disclosures"] if item["disclosure_id"] == "currency.basis.selection" and "explicit" in item.get("request_ids", []))
        self.assertIn("一种币种", auto_text)
        self.assertIn("明确选择", explicit_text)
        sql = "\n".join(trace["sql"] for trace in self.h.sql_trace)
        self.assertIn("currency_count", sql)
        self.assertGreaterEqual(len(self.h.sql_trace), 2)

    def test_auto_single_currency_preserves_raw_whitespace_for_final_filter(self):
        self._receipts([(10, 7, " USD ")])
        payload, result = self._payload_result(self._request("actual_receipt_amount"))
        self.assertEqual(10, public.facts(result)[0]["metric_value"])
        self._assert_public_basis(payload, result, "original", "actual_receipt_amount_original")
        self.assertEqual("USD", result["currency_scope"]["value"].strip())
        self.assertTrue(
            any(" USD " in [str(value) for value in trace["params"]] for trace in self.h.sql_trace)
        )

    def test_auto_cross_currency_probes_then_uses_rmb(self):
        self._receipts([(10, 7, "USD"), (20, 8, "EUR")])
        payload, result = self._payload_result(self._request("actual_receipt_amount"))
        self.assertEqual(230, public.facts(result)[0]["metric_value"])
        self._assert_public_basis(payload, result, "rmb", "actual_receipt_amount")
        self.assertIn("人民币", next(item["unit"] for item in payload["metric_contexts"] if item["business_metric_ref"] == result["business_metric_ref"]))
        self.assertTrue(
            any("currency_count" in trace["sql"] for trace in self.h.sql_trace)
        )
        self.assertGreaterEqual(len(self.h.sql_trace), 2)

    def test_auto_comparison_across_usd_and_eur_periods_uses_rmb(self):
        self._receipts(
            [
                (10, 7, "USD", "2026-08-15"),
                (20, 8, "EUR", "2026-07-15"),
            ]
        )
        request = self._request(
            "actual_receipt_amount",
            month="2026-08",
            comparison={"kind": "previous_period"},
        )
        payload, result = self._payload_result(request)
        self._assert_public_basis(payload, result, "rmb", "actual_receipt_amount")
        self.assertEqual(70, public.facts(result)[0]["metric_value"])
        self.assertEqual(160, public.facts(result)[0]["comparison_value"])
        self.assertTrue(any("currency_count" in t["sql"] for t in self.h.sql_trace))

    def test_auto_unknown_currency_fails_without_a_fabricated_amount(self):
        self._receipts([(10, 7, None)])
        payload = self.h.query(self._request("actual_receipt_amount"))
        self.assertEqual("failed", payload["status"], payload)
        self.assertEqual("CURRENCY_SCOPE_UNKNOWN", payload["results"][0]["error"]["code"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("币种", text)
        self.assertIn("人民币", text)
        self.assertEqual(1, len(self.h.sql_trace), self.h.sql_trace)

    def test_explicit_original_groups_and_filter_restores_one_currency(self):
        self._receipts([(10, 7, "USD"), (20, 8, "EUR")])
        grouped_payload, grouped = self._payload_result(
            self._request("actual_receipt_amount", basis="original")
        )
        self._assert_public_basis(grouped_payload, grouped, "original", "actual_receipt_amount_original")
        values = {
            row["dimensions"][0]["value"]: row["facts"]["metric_value"]
            for row in grouped["rows"]
        }
        self.assertEqual({"USD": 10, "EUR": 20}, values)
        self.assertNotEqual(30, public.facts(grouped)[0]["metric_value"])

        filtered_payload, filtered = self._payload_result(
            self._request(
                "actual_receipt_amount",
                basis="original",
                metric_filters={"currency": "USD"},
                request_id="usd",
            )
        )
        self.assertEqual(10, public.facts(filtered)[0]["metric_value"])
        self.assertEqual(
            {"mode": "filtered", "value": "USD"},
            filtered["currency_scope"],
        )

    def test_negative_refund_stays_signed_in_original_net(self):
        self._receipts([(100, 1, "USD")])
        self._refunds([(30, 1, "USD"), (-5, 1, "USD")])
        payload, result = self._payload_result(
            self._request(
                "net_receipt_amount",
                basis="original",
                metric_filters={"currency": "USD"},
            )
        )
        self.assertEqual(75, public.facts(result)[0]["metric_value"])
        self._assert_public_basis(payload, result, "original", "net_receipt_amount_original")
        self.assertTrue(self.h.sql_trace)

    def test_empty_currency_is_not_treated_as_a_known_currency(self):
        self._receipts([(10, 7, "")])
        payload = self.h.query(self._request("actual_receipt_amount"))
        self.assertEqual("failed", payload["status"], payload)
        self.assertEqual("CURRENCY_SCOPE_UNKNOWN", payload["results"][0]["error"]["code"])
        self.assertIn("币种", json.dumps(payload, ensure_ascii=False))

    def test_empty_actual_source_remains_empty_after_auto_probe(self):
        self._clear_receipts()
        result = self._result(self._request("actual_receipt_amount"))
        self.assertEqual("empty", result["data_state"])
        self.assertFalse(result.get("rows"))
        self.assertGreaterEqual(len(self.h.sql_trace), 2)

    def test_cross_currency_ratio_fails_with_rmb_requery_hint(self):
        self._receipts([(10, 7, "USD"), (20, 8, "EUR")])
        left = self._request(
            "actual_receipt_amount",
            basis="original",
            request_id="usd",
            metric_filters={"currency": "USD"},
        )
        right = self._request(
            "actual_receipt_amount",
            basis="original",
            request_id="eur",
            metric_filters={"currency": "EUR"},
        )
        payload = self.h.invoke(
            "datasage_query",
            {
                "requests": [left, right],
                "calculations": [
                    {
                        "calculation_id": "cross_currency_ratio",
                        "operation": "ratio",
                        "left_request_id": "usd",
                        "right_request_id": "eur",
                    }
                ],
            },
        )
        calculation = payload["calculations"][0]
        self.assertEqual("failed", calculation["status"], payload)
        self.assertEqual("CALCULATION_CURRENCY_MISMATCH", calculation["error"]["code"])
        hint = json.dumps(calculation, ensure_ascii=False)
        self.assertRegex(hint, "人民币|RMB")
        self.assertRegex(hint, "重查|重新查询|re-query")

    def test_snapshot_probe_and_final_query_use_the_same_offline_seam(self):
        self._receipts([(10, 7, "USD")])
        result = self._result(self._request("actual_receipt_amount"))
        self.assertEqual(10, public.facts(result)[0]["metric_value"])
        self.assertGreaterEqual(len(self.h.sql_trace), 2)
        self.assertTrue(all("sqlite_sql" in trace for trace in self.h.sql_trace))
        self.assertTrue(all("database_rows" in trace for trace in self.h.sql_trace))
        self.assertTrue(self.h.calls)

    def test_failure_after_selection_identifies_the_original_metric(self):
        self._receipts([(10, 7, "USD")])
        execute = self.h.execute
        def fail_selected(sql, params, limit, *, deadline_at=None):
            if "currency_count" in sql:
                return execute(sql, params, limit, deadline_at=deadline_at)
            raise QUERY_ERRORS.QueryFailure("QUERY_FAILED", "Synthetic selected-query failure")
        with patch.object(self.h, "execute", side_effect=fail_selected):
            payload = self.h.query(self._request("actual_receipt_amount"))
        result = payload["results"][0]
        self.assertEqual("failed", result["status"])
        self.assertEqual("QUERY_FAILED", result["error"]["code"])
        self.assertEqual(self._fixed_metric_ref("receipt", "actual_receipt_amount_original"), result["business_metric_ref"])
        self.assertEqual(1, len(self.h.sql_trace))

    def test_pending_metric_keeps_its_gate_with_currency_selection(self):
        payload = self.h.query(public.metric("receivable_quantity", "receivable", currency_basis="auto"))
        self.assertEqual("SEMANTIC_UNIT_RECONCILIATION_REQUIRED", payload["results"][0]["error"]["code"])
        self.assertFalse(self.h.sql_trace)

    def test_invalid_basis_types_never_reach_sql(self):
        for value in (None, [], {}, True, "USD"):
            with self.subTest(value=value):
                payload = self.h.query(self._request("actual_receipt_amount", basis=value))
                self.assertEqual("INVALID_INPUT", payload["results"][0]["error"]["code"])
        self.assertFalse(self.h.sql_trace)


if __name__ == "__main__":
    unittest.main()
