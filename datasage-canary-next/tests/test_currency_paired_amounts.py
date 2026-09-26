"""Offline original-currency regressions for delivery/receipt comparison.

The existing remaining-cases proxy owns the in-memory SQLite database, MySQL
syntax adapters, network tripwires, and raw-to-model evidence path. Expected
amounts below are hand-calculated from synthetic currency rows.
"""

from __future__ import annotations

import importlib
import unittest


public = importlib.import_module("test_remediation_remaining_cases")


class OriginalCurrencyPairedAmountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd "
            "ADD COLUMN delivery_amount REAL"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.sale_bill_goods_detail_dwd "
            "ADD COLUMN currency_no TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.delivery_return_detail_dwd "
            "ADD COLUMN return_amount REAL"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.delivery_return_detail_dwd "
            "ADD COLUMN currency_no TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receive_bill_detail_dwd "
            "ADD COLUMN detail_receive_amount REAL"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receive_bill_detail_dwd "
            "ADD COLUMN currency_no TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receive_return_bill_detail_dwd "
            "ADD COLUMN detail_return_amount REAL"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receive_return_bill_detail_dwd "
            "ADD COLUMN currency_no TEXT"
        )

    def _insert_currency(self, currency: str, *, delivery: float, delivery_return: float, receipt: float, refund: float) -> None:
        self.h.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount,delivery_amount_rmb,currency_no,bill_status,is_inner_cus,delivery_time",
            [(delivery, delivery, currency, 6, "n", "2026-08-15")],
        )
        self.h.insert(
            "vk_dwd.delivery_return_detail_dwd",
            "return_amount,return_amount_rmb,currency_no,status,complnt_type,channel_type,is_inner_cus,statement_time",
            [(delivery_return, delivery_return, currency, 4, 1, 1, "n", "2026-08-16")],
        )
        self.h.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_receive_amount,detail_receive_rmb,currency_no,bill_status,bill_time",
            [(receipt, receipt, currency, "C", "2026-08-17")],
        )
        self.h.insert(
            "vk_dwd.receive_return_bill_detail_dwd",
            "detail_return_amount,detail_return_rmb,currency_no,bill_status,bill_time",
            [(refund, refund, currency, "C", "2026-08-18")],
        )

    @staticmethod
    def _currency(row: dict) -> str | None:
        for dimension in row.get("dimensions", []):
            if dimension.get("label") == "币种":
                return dimension.get("value")
        return None

    def _query(self, *, dimensions=None, filters=None) -> tuple[dict, list[dict]]:
        response = self.h.query(
            public.metric(
                "delivery_receipt_comparison_original",
                "receipt",
                month=None,
                dimensions=dimensions or [],
                metric_filters=filters or {},
            )
        )
        result = self.h.result(response)
        return result, list(self.h.sql_trace[-1]["database_rows"])

    def test_grouped_currencies_keep_original_amounts_and_coverage(self) -> None:
        self._insert_currency(
            "USD", delivery=100, delivery_return=10, receipt=60, refund=5
        )
        self._insert_currency(
            "EUR", delivery=200, delivery_return=20, receipt=100, refund=10
        )

        result, raw_rows = self._query(dimensions=["currency"])
        self.assertEqual("success", result["status"])
        self.assertEqual({"USD", "EUR"}, {row["currency_no"] for row in raw_rows})
        self.assertEqual({"USD", "EUR"}, {self._currency(row) for row in result["rows"]})
        facts_by_currency = {
            self._currency(row): row["facts"] for row in result["rows"]
        }
        self.assertEqual(35, facts_by_currency["USD"]["metric_value"])
        self.assertEqual(90, facts_by_currency["USD"]["net_delivery_amount_original"])
        self.assertEqual(55, facts_by_currency["USD"]["net_receipt_amount_original"])
        self.assertAlmostEqual(55 / 90, facts_by_currency["USD"]["receipt_coverage"])
        self.assertEqual(90, facts_by_currency["EUR"]["metric_value"])
        self.assertEqual(180, facts_by_currency["EUR"]["net_delivery_amount_original"])
        self.assertEqual(90, facts_by_currency["EUR"]["net_receipt_amount_original"])
        self.assertAlmostEqual(0.5, facts_by_currency["EUR"]["receipt_coverage"])
        self.assertIn("currency_no", self.h.sql_trace[-1]["sqlite_sql"])
        self.assertIn("delivery_amount", self.h.sql_trace[-1]["sqlite_sql"])
        self.assertIn("detail_receive_amount", self.h.sql_trace[-1]["sqlite_sql"])

    def test_unknown_currency_groups_are_missing_but_known_group_survives(self) -> None:
        self._insert_currency(
            "USD", delivery=100, delivery_return=10, receipt=60, refund=5
        )
        for currency in (None, " "):
            self.h.insert(
                "vk_dwd.sale_bill_goods_detail_dwd",
                "delivery_amount,delivery_amount_rmb,currency_no,bill_status,is_inner_cus,delivery_time",
                [(999, 999, currency, 6, "n", "2026-08-15")],
            )
            self.h.insert(
                "vk_dwd.delivery_return_detail_dwd",
                "return_amount,return_amount_rmb,currency_no,status,complnt_type,channel_type,is_inner_cus,statement_time",
                [(99, 99, currency, 4, 1, 1, "n", "2026-08-16")],
            )
            self.h.insert(
                "vk_dwd.receive_bill_detail_dwd",
                "detail_receive_amount,detail_receive_rmb,currency_no,bill_status,bill_time",
                [(777, 777, currency, "C", "2026-08-17")],
            )
            self.h.insert(
                "vk_dwd.receive_return_bill_detail_dwd",
                "detail_return_amount,detail_return_rmb,currency_no,bill_status,bill_time",
                [(77, 77, currency, "C", "2026-08-18")],
            )

        result, raw_rows = self._query(dimensions=["currency"])
        self.assertEqual({"USD", None, " "}, {row["currency_no"] for row in raw_rows})
        rows_by_currency = {
            self._currency(row): row for row in result["rows"] if self._currency(row) != "未知币种"
        }
        self.assertEqual(35, rows_by_currency["USD"]["facts"]["metric_value"])
        self.assertEqual(90, rows_by_currency["USD"]["facts"]["net_delivery_amount_original"])
        self.assertEqual(55, rows_by_currency["USD"]["facts"]["net_receipt_amount_original"])
        unknown_rows = [row for row in result["rows"] if self._currency(row) == "未知币种"]
        self.assertEqual(2, len(unknown_rows))
        for row in unknown_rows:
            facts = row["facts"]
            self.assertIsNone(facts["metric_value"])
            self.assertIsNone(facts["net_delivery_amount_original"])
            self.assertIsNone(facts["net_receipt_amount_original"])
            self.assertIsNone(facts["receipt_coverage"])
            self.assertEqual("missing", facts["metric_data_state"])
        self.assertIn("TRIM", self.h.sql_trace[-1]["sqlite_sql"])

    def test_single_currency_filter_allows_scalar_original_comparison(self) -> None:
        self._insert_currency(
            "USD", delivery=100, delivery_return=10, receipt=60, refund=5
        )
        self._insert_currency(
            "EUR", delivery=200, delivery_return=20, receipt=100, refund=10
        )

        result, raw_rows = self._query(filters={"currency": "USD"})
        self.assertEqual(1, len(raw_rows))
        facts = result["rows"][0]["facts"]
        self.assertEqual(35, facts["metric_value"])
        self.assertEqual(90, facts["net_delivery_amount_original"])
        self.assertEqual(55, facts["net_receipt_amount_original"])
        self.assertAlmostEqual(55 / 90, facts["receipt_coverage"])
        self.assertEqual({"mode": "filtered", "value": "USD"}, result["currency_scope"])
        self.assertEqual("原币金额（按币种分别计量）", result["rows"][0]["unit"])
        self.assertEqual(
            {
                "metric_value": "原币金额（按币种分别计量）",
                "net_delivery_amount_original": "原币金额（按币种分别计量）",
                "net_receipt_amount_original": "原币金额（按币种分别计量）",
                "receipt_coverage": "比例",
            },
            result["rows"][0]["fact_units"],
        )

    def test_original_comparison_requires_currency_scope_and_rejects_multi_filter(self) -> None:
        self._insert_currency(
            "USD", delivery=100, delivery_return=10, receipt=60, refund=5
        )
        before = len(self.h.sql_trace)
        unscoped = self.h.query(
            public.metric(
                "delivery_receipt_comparison_original",
                "receipt",
                month=None,
                dimensions=[],
                metric_filters={},
            )
        )
        self.assertEqual("failed", unscoped["status"])
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", unscoped["results"][0]["error"]["code"])
        self.assertEqual(before, len(self.h.sql_trace))

        before = len(self.h.sql_trace)
        multi = self.h.query(
            public.metric(
                "delivery_receipt_comparison_original",
                "receipt",
                month=None,
                dimensions=[],
                metric_filters={"currency": ["USD", "EUR"]},
            )
        )
        self.assertEqual("failed", multi["status"])
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", multi["results"][0]["error"]["code"])
        self.assertEqual(before, len(self.h.sql_trace))

    def test_original_fact_units_and_rmb_metric_surface_stay_distinct(self) -> None:
        self._insert_currency(
            "USD", delivery=100, delivery_return=10, receipt=60, refund=5
        )
        result, _raw_rows = self._query(filters={"currency": "USD"})
        facts = result["rows"][0]["facts"]
        self.assertIn("net_delivery_amount_original", facts)
        self.assertIn("net_receipt_amount_original", facts)
        self.assertNotIn("net_delivery_amount_rmb", facts)
        self.assertNotIn("net_receipt_amount_rmb", facts)
        self.assertEqual("complete", facts.get("metric_data_state"))
        self.assertFalse(result.get("truncated"))


if __name__ == "__main__":
    unittest.main()
