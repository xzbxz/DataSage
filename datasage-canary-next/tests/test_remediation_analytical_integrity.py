"""Offline public-handler regressions for custom analytical NULL coverage.

These tests use the existing in-memory SQLite public harness. They never use a
live database, network, model, gateway, or credentials. Expected states are
hand-defined from the synthetic rows and are not generated from YAML.
"""

import unittest

import test_remediation_remaining_cases as public


class AnalyticalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_paired_amounts_preserve_null_and_valid_side(self):
        self.h.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept",
            [(100, 6, "n", "2026-08-15", "HCM")],
        )
        self.h.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_receive_rmb,detail_deal_amount,exchange_rate,bill_status,bill_time",
            [(50, 50, 1, "C", "2026-08-15"), (None, 10, 1, "C", "2026-08-16")],
        )
        response = self.h.query(
            public.metric("delivery_receipt_comparison", "customer_risk", month=None)
        )
        result = self.h.result(response)
        facts = result["rows"][0]["facts"]
        self.assertEqual("incomplete", result["data_state"])
        self.assertIsNone(facts["metric_value"])
        self.assertEqual(100, facts["net_delivery_amount_rmb"])
        self.assertIsNone(facts["net_receipt_amount_rmb"])
        self.assertGreater(facts["missing_value_count"], 0)
        self.assertEqual("incomplete", result["rows"][0]["states"]["metric_data_state"])

    def test_paired_amounts_do_not_turn_unmatched_side_into_zero(self):
        self.h.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept",
            [(100, 6, "n", "2026-08-15", "HCM")],
        )
        response = self.h.query(
            public.metric("delivery_receipt_comparison", "customer_risk", month=None)
        )
        result = self.h.result(response)
        facts = result["rows"][0]["facts"]
        self.assertEqual("incomplete", result["data_state"])
        self.assertIsNone(facts["metric_value"])
        self.assertEqual(100, facts["net_delivery_amount_rmb"])
        self.assertIsNone(facts["net_receipt_amount_rmb"])
        self.assertEqual(0, facts["missing_value_count"])
        self.assertIsNone(facts["value_coverage_rate"])

    def test_settlement_missing_bill_time_is_incomplete(self):
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd "
            "ADD COLUMN verification_complete_time TEXT"
        )
        self.h.insert(
            "vk_dwd.receivable_bill_detail_dwd",
            "detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no,bill_id,verification_complete_time",
            [
                (0, 1, "C", "2026-08-01", "n", "a", "a", "A", "O", "CNY", "bill-a", "2026-08-11"),
                (0, 1, "C", None, "n", "b", "b", "B", "O", "CNY", "bill-b", "2026-08-12"),
                (0, 1, "C", None, "n", "b", "b", "B", "O", "CNY", "bill-b", "2026-08-12"),
            ],
        )
        response = self.h.query(
            public.metric("average_settlement_days", "customer_risk", month=None)
        )
        result = self.h.result(response)
        facts = result["rows"][0]["facts"]
        self.assertEqual("incomplete", result["data_state"])
        self.assertIsNone(facts["metric_value"])
        self.assertEqual(1, facts["sample_bill_count"])
        self.assertEqual(10, facts["known_subset_value"])
        self.assertGreater(facts["missing_value_count"], 0)

    def test_settlement_missing_date_on_open_bill_is_excluded_not_poisoning(self):
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd "
            "ADD COLUMN verification_complete_time TEXT"
        )
        self.h.insert(
            "vk_dwd.receivable_bill_detail_dwd",
            "detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no,bill_id,verification_complete_time",
            [
                (0, 1, "C", "2026-08-01", "n", "a", "a", "A", "O", "CNY", "bill-a", "2026-08-11"),
                (100, 1, "C", None, "n", "b", "b", "B", "O", "CNY", "bill-open", "2026-08-12"),
            ],
        )
        response = self.h.query(
            public.metric("average_settlement_days", "customer_risk", month=None)
        )
        result = self.h.result(response)
        facts = result["rows"][0]["facts"]
        self.assertEqual("rows", result["data_state"])
        self.assertEqual(10, facts["metric_value"])
        self.assertEqual(0, facts["missing_value_count"])
        self.assertEqual(1, facts["excluded_open_balance_bill_count"])

    def test_settlement_empty_and_real_zero_states_remain_distinct(self):
        self.h.conn.execute("ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT")
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd "
            "ADD COLUMN verification_complete_time TEXT"
        )
        empty = self.h.query(
            public.metric("average_settlement_days", "customer_risk", month=None)
        )
        self.assertEqual("empty", self.h.result(empty)["data_state"])
        self.h.insert(
            "vk_dwd.receivable_bill_detail_dwd",
            "detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no,bill_id,verification_complete_time",
            [(0, 1, "C", "2026-08-01", "n", "a", "a", "A", "O", "CNY", "bill-zero", "2026-08-01")],
        )
        zero = self.h.query(
            public.metric("average_settlement_days", "customer_risk", month=None)
        )
        zero_result = self.h.result(zero)
        self.assertEqual("zero", zero_result["data_state"])
        self.assertEqual(0, zero_result["rows"][0]["facts"]["metric_value"])

    def _seed_formal_dso(self, *, debt_null=False, delivery_null=False):
        months = [
            "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
            "2026-06", "2026-07", "2026-08",
        ]
        debt_rows = [(100, month, "n") for month in months]
        if debt_null:
            debt_rows.append((None, "2026-08", "n"))
        self.h.insert(
            "vk_dw.customer_debt_bymonth_dw",
            "debt_amount_rmb,bill_date,is_inner_cus",
            debt_rows,
        )
        delivery_rows = [(100, 6, "n", f"{month}-15", "HCM") for month in months[1:]]
        if delivery_null:
            delivery_rows.append((None, 6, "n", "2026-08-16", "HCM"))
        self.h.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept",
            delivery_rows,
        )

    def _formal_dso_result(self):
        response = self.h.query(
            public.metric("formal_receivable_turnover_days", "customer_risk", month=None)
        )
        return self.h.result(response)

    def test_formal_dso_normal_control_is_complete(self):
        self._seed_formal_dso()
        result = self._formal_dso_result()
        facts = result["rows"][0]["facts"]
        self.assertEqual("rows", result["data_state"])
        self.assertAlmostEqual(30.4166666667, facts["metric_value"], places=8)
        self.assertEqual(100, facts["average_net_debt_rmb"])
        self.assertEqual(1200, self.h.sql_trace[-1]["database_rows"][0]["delivery_amount_rmb"])
        self.assertEqual(0, facts["missing_value_count"])
        self.assertEqual(1, facts["value_coverage_rate"])
        self.assertEqual("verified", facts["calculation_attestation"]["status"])

    def test_formal_dso_debt_null_keeps_valid_denominator_not_partial_ratio(self):
        self._seed_formal_dso(debt_null=True)
        result = self._formal_dso_result()
        facts = result["rows"][0]["facts"]
        self.assertIn(result["data_state"], {"incomplete", "undefined"})
        self.assertIsNone(facts.get("metric_value"))
        self.assertIsNone(facts["average_net_debt_rmb"])
        self.assertEqual(1200, self.h.sql_trace[-1]["database_rows"][0]["delivery_amount_rmb"])
        self.assertNotIn("known_subset_value", facts)
        self.assertNotEqual("verified", facts["calculation_attestation"]["status"])

    def test_formal_dso_delivery_null_keeps_valid_average_not_partial_ratio(self):
        self._seed_formal_dso(delivery_null=True)
        result = self._formal_dso_result()
        facts = result["rows"][0]["facts"]
        self.assertIn(result["data_state"], {"incomplete", "undefined"})
        self.assertIsNone(facts.get("metric_value"))
        self.assertEqual(100, facts["average_net_debt_rmb"])
        self.assertIsNone(self.h.sql_trace[-1]["database_rows"][0]["delivery_amount_rmb"])
        self.assertNotIn("known_subset_value", facts)
        self.assertNotEqual("verified", facts["calculation_attestation"]["status"])

    def test_formal_dso_all_inputs_missing_is_undefined(self):
        self._seed_formal_dso(debt_null=True, delivery_null=True)
        # Replace every known amount with NULL while retaining the source rows.
        self.h.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET debt_amount_rmb = NULL")
        self.h.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb = NULL")
        result = self._formal_dso_result()
        facts = result["rows"][0]["facts"]
        self.assertEqual("undefined", result["data_state"])
        self.assertIsNone(facts.get("metric_value"))
        self.assertIsNone(facts["average_net_debt_rmb"])
        self.assertIsNone(self.h.sql_trace[-1]["database_rows"][0]["delivery_amount_rmb"])
        self.assertNotIn("known_subset_value", facts)
        self.assertEqual(0, facts["known_value_count"])
        self.assertNotEqual("verified", facts["calculation_attestation"]["status"])

    def test_formal_dso_missing_debt_or_denominator_cannot_be_verified(self):
        self._seed_formal_dso(debt_null=True, delivery_null=True)
        result = self._formal_dso_result()
        facts = result["rows"][0]["facts"]
        self.assertIn(result["data_state"], {"incomplete", "undefined"})
        self.assertIsNone(facts.get("metric_value"))
        attestation = facts.get("calculation_attestation") or {}
        self.assertNotEqual("verified", attestation.get("status"))
        self.assertGreater(facts.get("missing_value_count", 0), 0)


    def test_paired_all_null_and_real_zero_have_distinct_public_states(self):
        for value,state in ((None,"undefined"),(0,"zero")):
            with self.subTest(value=value):
                self.h.conn.execute("DELETE FROM vk_dwd.sale_bill_goods_detail_dwd")
                self.h.conn.execute("DELETE FROM vk_dwd.receive_bill_detail_dwd")
                self.h.insert("vk_dwd.sale_bill_goods_detail_dwd","delivery_amount_rmb,bill_status,is_inner_cus,delivery_time",[(value,6,"n","2026-08-15")])
                self.h.insert("vk_dwd.receive_bill_detail_dwd","detail_receive_rmb,bill_status,bill_time",[(value,"C","2026-08-15")])
                result=self.h.result(self.h.query(public.metric("delivery_receipt_comparison","customer_risk",month=None)))
                self.assertEqual(state,result["data_state"])
                self.assertEqual(value,result["rows"][0]["facts"]["metric_value"])
                self.assertEqual(2 if value is None else 0,result["rows"][0]["facts"]["missing_value_count"])

    def test_settlement_distribution_without_eligible_sample_does_not_invent_zero(self):
        self.h.conn.execute("ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT")
        self.h.conn.execute("ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN verification_complete_time TEXT")
        self.h.conn.create_function("FIELD",-1,lambda value,*items: items.index(value)+1 if value in items else 0)
        self.h.insert("vk_dwd.receivable_bill_detail_dwd", "detail_unsettled_amount,bill_status,bill_time,is_inner_cus,bill_id,verification_complete_time",[(5,"C","2026-08-01","n","synthetic_open","2026-08-11")])
        result=self.h.result(self.h.query(public.metric("settlement_days_distribution","customer_risk",month=None)))
        self.assertEqual("undefined",result["data_state"])
        self.assertIsNone(result["rows"][0]["facts"]["metric_value"])
        self.assertIsNone(result["rows"][0]["facts"]["known_subset_value"])
        self.assertEqual(0,result["rows"][0]["facts"]["sample_bill_count"])


    def test_settlement_unknown_balance_is_not_assumed_closed(self):
        self.h.conn.execute("ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT")
        self.h.conn.execute("ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN verification_complete_time TEXT")
        self.h.insert("vk_dwd.receivable_bill_detail_dwd","detail_unsettled_amount,bill_status,bill_time,is_inner_cus,bill_id,verification_complete_time",[(0,"C","2026-08-01","n","known","2026-08-11"),(None,"C","2026-08-01","n","unknown","2026-08-21")])
        result=self.h.result(self.h.query(public.metric("average_settlement_days","customer_risk",month=None)))
        facts=result["rows"][0]["facts"]
        self.assertEqual("incomplete",result["data_state"])
        self.assertIsNone(facts["metric_value"])
        self.assertEqual(10,facts["known_subset_value"])
        self.assertEqual(1,facts["missing_value_count"])
        self.assertEqual(1,facts["sample_bill_count"])


    def test_formal_dso_missing_snapshot_month_is_not_a_verified_average(self):
        self._seed_formal_dso()
        self.h.conn.execute("DELETE FROM vk_dw.customer_debt_bymonth_dw WHERE bill_date='2026-02'")
        result=self._formal_dso_result();facts=result["rows"][0]["facts"]
        self.assertIsNone(facts.get("metric_value"))
        self.assertIsNone(facts["average_net_debt_rmb"])
        self.assertEqual(12,facts["snapshot_month_count"])
        self.assertNotEqual("verified",facts["calculation_attestation"]["status"])


if __name__ == "__main__":
    unittest.main()
