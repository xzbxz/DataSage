"""Independent hand-calculated coverage for original-currency metric pairs.

Uses the existing isolated SQLite/snapshot test seam. No production data or
transport is used; expected amounts are independent synthetic arithmetic.
"""
from __future__ import annotations

from decimal import Decimal
import re
import unittest

import test_remediation_remaining_cases as public


RECEIVABLE_ORIGINAL_VARIANTS = (
    ("positive_debt_amount_original", "positive_debt_amount"),
    ("debt_balance_trend_original", "debt_balance_trend"),
    ("aging_0_30_amount_original", "aging_0_30_amount"),
    ("aging_31_60_amount_original", "aging_31_60_amount"),
    ("aging_61_90_amount_original", "aging_61_90_amount"),
    ("aging_91_120_amount_original", "aging_91_120_amount"),
    ("aging_121_150_amount_original", "aging_121_150_amount"),
    ("aging_151_180_amount_original", "aging_151_180_amount"),
    ("aging_181_240_amount_original", "aging_181_240_amount"),
    ("aging_241_300_amount_original", "aging_241_300_amount"),
    ("aging_301_360_amount_original", "aging_301_360_amount"),
    ("aging_over_360_amount_original", "aging_over_360_amount"),
    ("aging_over_30_amount_original", "aging_over_30_amount"),
    ("aging_over_60_amount_original", "aging_over_60_amount"),
    ("aging_over_90_amount_original", "aging_over_90_amount"),
    ("aging_over_120_amount_original", "aging_over_120_amount"),
    ("aging_over_150_amount_original", "aging_over_150_amount"),
    ("aging_over_180_amount_original", "aging_over_180_amount"),
    ("aging_over_240_amount_original", "aging_over_240_amount"),
    ("aging_over_300_amount_original", "aging_over_300_amount"),
    ("uncredited_positive_debt_amount_original", "uncredited_positive_debt_amount"),
)

AGING_VARIANTS = (
    ("aging_0_30_amount_original", "aging_0_30_amount", 1),
    ("aging_31_60_amount_original", "aging_31_60_amount", 2),
    ("aging_61_90_amount_original", "aging_61_90_amount", 3),
    ("aging_91_120_amount_original", "aging_91_120_amount", 4),
    ("aging_121_150_amount_original", "aging_121_150_amount", 5),
    ("aging_151_180_amount_original", "aging_151_180_amount", 6),
    ("aging_181_240_amount_original", "aging_181_240_amount", 7),
    ("aging_241_300_amount_original", "aging_241_300_amount", 8),
    ("aging_301_360_amount_original", "aging_301_360_amount", 9),
    ("aging_over_360_amount_original", "aging_over_360_amount", 10),
    ("aging_over_30_amount_original", "aging_over_30_amount", sum(range(2, 11))),
    ("aging_over_60_amount_original", "aging_over_60_amount", sum(range(3, 11))),
    ("aging_over_90_amount_original", "aging_over_90_amount", sum(range(4, 11))),
    ("aging_over_120_amount_original", "aging_over_120_amount", sum(range(5, 11))),
    ("aging_over_150_amount_original", "aging_over_150_amount", sum(range(6, 11))),
    ("aging_over_180_amount_original", "aging_over_180_amount", sum(range(7, 11))),
    ("aging_over_240_amount_original", "aging_over_240_amount", sum(range(8, 11))),
    ("aging_over_300_amount_original", "aging_over_300_amount", sum(range(9, 11))),
)

INVENTORY_PAIRS = (
    {
        "rmb": "current_inventory_amount_rmb",
        "original": "current_inventory_amount_original",
        "table": "vk_dw.inventory_barcode_detail_dw",
        "original_field": "ddp_amount",
        "rmb_field": "ddp_amount_rmb",
        "time_policy": "current_snapshot",
    },
    {
        "rmb": "month_end_inventory_cost_rmb",
        "original": "month_end_inventory_cost_original",
        "table": "vk_dwd.inventory_cost_dwd",
        "original_field": "cost_amount",
        "rmb_field": "cost_amount_rmb",
        "time_policy": "latest_snapshot",
    },
    {
        "rmb": "month_end_inventory_ddp_rmb",
        "original": "month_end_inventory_ddp_original",
        "table": "vk_dwd.inventory_cost_dwd",
        "original_field": "ddp_amount",
        "rmb_field": "ddp_amount_rmb",
        "time_policy": "latest_snapshot",
    },
)

# These are deliberate fixture differences from RemainingCaseTests.DDL.  They
# are synthetic only and document what the draft adds when copied to tests/.
FIXTURE_DIFFERENCES = {
    "receivable": (
        "Adds debt_amount/currency_no to the existing debt snapshot, creates "
        "the aging snapshot with original buckets plus exchange_rate, adds "
        "customer master rows, and adds credit id for the missing-credit join. "
        "Values are hand-authored original 1..10 with FX 7 and RMB 7x; no "
        "production schema or data is read."
    ),
    "inventory": (
        "Adds original amount, RMB amount, currency_no and minimal snapshot "
        "columns to the existing isolated inventory tables. Each pair uses "
        "original 1, RMB 7, then a second synthetic currency for the auto "
        "cross-currency RMB path."
    ),
}


class CurrencyMetricPairTests(unittest.TestCase):
    def _new_harness(self):
        harness = public.RemainingCaseTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

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

    def _receivable_schema(self, harness):
        for column, sql_type in (
            ("debt_amount", "REAL"),
            ("currency_no", "TEXT"),
            ("customer_id", "TEXT"),
            ("org_name", "TEXT"),
        ):
            self._ensure_column(
                harness, "vk_dw", "customer_debt_bymonth_dw", column, sql_type
            )
        self._ensure_column(harness, "vk_dwd", "customer_credit_dwd", "id", "TEXT")
        harness.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS vk_dwd.customer_aging_dwd(
                bill_date TEXT, customer_id TEXT, customer_no TEXT,
                customer_name TEXT, customer_dept TEXT, customer_region TEXT,
                sales_id TEXT, sales_name TEXT, org_name TEXT, currency_no TEXT,
                exchange_rate REAL, debt_amount REAL, is_inner_cus TEXT,
                is_ccbs_cus TEXT, is_ha_cus TEXT,
                "0_30_debt" REAL, "31_60_debt" REAL, "61_90_debt" REAL,
                "91_120_debt" REAL, "121_150_debt" REAL, "151_180_debt" REAL,
                "181_240_debt" REAL, "241_300_debt" REAL,
                "301_360_debt" REAL, "361_debt" REAL
            )
            '''
        )
        for customer_id, customer_name in (
            ("USD-CUSTOMER", "USD Customer"),
            ("EUR-CUSTOMER", "EUR Customer"),
        ):
            harness.insert(
                "vk_dwd.customer_dwd",
                "customer_id,customer_no,customer_name,is_delete,is_void",
                [(customer_id, customer_id, customer_name, "n", "n")],
            )

    def _inventory_schema(self, harness):
        for column, sql_type in (
            ("ddp_amount", "REAL"),
            ("currency_no", "TEXT"),
            ("goods_num", "REAL"),
            ("piece_num", "REAL"),
            ("unit", "TEXT"),
            ("goods_id", "TEXT"),
            ("whse_id", "TEXT"),
            ("whse_dept", "TEXT"),
        ):
            self._ensure_column(
                harness, "vk_dw", "inventory_barcode_detail_dw", column, sql_type
            )
        for column, sql_type in (
            ("cost_amount", "REAL"),
            ("ddp_amount", "REAL"),
            ("ddp_amount_rmb", "REAL"),
            ("currency_no", "TEXT"),
            ("goods_num", "REAL"),
            ("piece_num", "REAL"),
            ("unit", "TEXT"),
            ("goods_id", "TEXT"),
            ("whse_id", "TEXT"),
            ("whse_dept", "TEXT"),
            ("org_id", "TEXT"),
            ("org_name", "TEXT"),
        ):
            self._ensure_column(
                harness, "vk_dwd", "inventory_cost_dwd", column, sql_type
            )

    @staticmethod
    def _request(domain, metric_id, basis, *, request_id="r", currency=None,
                 month=None, dimensions=None, time_range=None, time_bucket=None):
        extra = {"currency_basis": basis, "dimensions": dimensions or []}
        if currency is not None:
            extra["metric_filters"] = {"currency": currency}
        if time_range is not None:
            extra["time_range"] = time_range
        if time_bucket is not None:
            extra["time_bucket"] = time_bucket
        return public.metric(
            metric_id,
            domain,
            request_id=request_id,
            month=month,
            **extra,
        )

    @staticmethod
    def _non_null_values(result):
        return [
            row.get("facts", {}).get("metric_value")
            for row in result.get("rows", [])
            if row.get("facts", {}).get("metric_value") is not None
        ]

    def _result(self, harness, request):
        payload = harness.query(request)
        result = harness.result(payload, request["request_id"])
        if not hasattr(self, "_public_payloads"):
            self._public_payloads = {}
        self._public_payloads[id(result)] = payload
        return result

    def _assert_basis(self, result, basis, metric_id):
        expected = "人民币" if basis == "rmb" else "原币"
        payload = self._public_payloads[id(result)]
        context = next(c for c in payload["metric_contexts"] if c["business_metric_ref"] == result["business_metric_ref"])
        self.assertIn(expected, context["business_metric_unit"], result)
        selection = next(
            (
                item
                for item in payload.get("disclosures", [])
                if item.get("disclosure_id") == "currency.basis.selection" and result["request_id"] in item.get("request_ids", [])
            ),
            None,
        )
        self.assertIsNotNone(selection, metric_id)
        self.assertIn("currency.basis.selection", result.get("disclosure_refs", []))

    @staticmethod
    def _decimal(value):
        return Decimal(str(value))

    def _seed_debt(self, harness, *, include_eur=False):
        rows = [
            (7, 1, "2026-08", "n", "USD-CUSTOMER", "O", "USD"),
        ]
        if include_eur:
            rows.append((7, -1, "2026-08", "n", "EUR-CUSTOMER", "O", "EUR"))
        harness.insert(
            "vk_dw.customer_debt_bymonth_dw",
            "debt_amount_rmb,debt_amount,bill_date,is_inner_cus,customer_id,org_name,currency_no",
            rows,
        )

    def _seed_trend(self, harness, *, include_eur=False):
        rows = [
            (7, 1, "2026-07", "n", "USD-CUSTOMER", "O", "USD"),
            (14, 2, "2026-08", "n", "USD-CUSTOMER", "O", "USD"),
        ]
        if include_eur:
            rows.extend(
                [
                    (21, 3, "2026-07", "n", "EUR-CUSTOMER", "O", "EUR"),
                    (28, 4, "2026-08", "n", "EUR-CUSTOMER", "O", "EUR"),
                ]
            )
        harness.insert(
            "vk_dw.customer_debt_bymonth_dw",
            "debt_amount_rmb,debt_amount,bill_date,is_inner_cus,customer_id,org_name,currency_no",
            rows,
        )

    def _seed_aging(self, harness, *, include_eur=False):
        buckets = list(range(1, 11))
        columns = [
            "bill_date", "customer_id", "customer_no", "customer_name",
            "org_name", "currency_no", "exchange_rate", "debt_amount",
            "is_inner_cus", "is_ccbs_cus", "is_ha_cus",
            "0_30_debt", "31_60_debt", "61_90_debt", "91_120_debt",
            "121_150_debt", "151_180_debt", "181_240_debt",
            "241_300_debt", "301_360_debt", "361_debt",
        ]
        rows = [
            (
                "2026-08", "USD-CUSTOMER", "USD-CUSTOMER", "USD Customer",
                "O", "USD", 7, sum(buckets), "n", "n", "n", *buckets,
            )
        ]
        if include_eur:
            rows.append(
                (
                    "2026-08", "EUR-CUSTOMER", "EUR-CUSTOMER", "EUR Customer",
                    "O", "EUR", 7, sum(buckets), "n", "n", "n", *buckets,
                )
            )
        harness.insert("vk_dwd.customer_aging_dwd", ",".join(columns), rows)

    def test_receivable_original_pairs_auto_explicit_filtered_and_cross_currency(self):
        for original_metric, rmb_metric in (
            ("positive_debt_amount_original", "positive_debt_amount"),
            ("uncredited_positive_debt_amount_original", "uncredited_positive_debt_amount"),
        ):
            with self.subTest(metric=original_metric):
                harness = self._new_harness()
                self._receivable_schema(harness)
                self._seed_debt(harness)

                auto = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "auto", month=None),
                )
                self._assert_basis(auto, "original", original_metric)
                self.assertEqual(Decimal("1"), self._decimal(self._non_null_values(auto)[0]))

                explicit_rmb = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "rmb", month=None),
                )
                self._assert_basis(explicit_rmb, "rmb", rmb_metric)
                self.assertEqual(Decimal("7"), self._decimal(self._non_null_values(explicit_rmb)[0]))

                filtered = self._result(
                    harness,
                    self._request(
                        "receivable", rmb_metric, "original", month=None,
                        currency="USD", request_id="filtered",
                    ),
                )
                self._assert_basis(filtered, "original", original_metric)
                self.assertEqual(Decimal("1"), self._decimal(self._non_null_values(filtered)[0]))

                harness.conn.execute("DELETE FROM vk_dw.customer_debt_bymonth_dw")
                self._seed_debt(harness, include_eur=True)
                cross = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "auto", month=None, request_id="cross"),
                )
                self._assert_basis(cross, "rmb", rmb_metric)
                self.assertEqual(Decimal("14"), sum(map(self._decimal, self._non_null_values(cross))))

                negative = self._result(
                    harness,
                    self._request(
                        "receivable", rmb_metric, "original", month=None,
                        currency="EUR", request_id="negative",
                    ),
                )
                self._assert_basis(negative, "original", original_metric)
                self.assertEqual(Decimal("-1"), self._decimal(self._non_null_values(negative)[0]))

    def test_debt_balance_trend_original_keeps_months_and_basis(self):
        harness = self._new_harness()
        self._receivable_schema(harness)
        self._seed_trend(harness)
        window = {"start": "2026-07-01", "end": "2026-09-01"}

        with self.subTest(metric="debt_balance_trend_original"):
            auto = self._result(
                harness,
                self._request(
                    "receivable", "debt_balance_trend", "auto", month=None,
                    time_range=window, time_bucket="month",
                ),
            )
            self._assert_basis(auto, "original", "debt_balance_trend_original")
            self.assertEqual(Decimal("3"), sum(map(self._decimal, self._non_null_values(auto))))
            self.assertIn("2026-07", str(auto))
            self.assertIn("2026-08", str(auto))

            explicit_rmb = self._result(
                harness,
                self._request(
                    "receivable", "debt_balance_trend", "rmb", month=None,
                    time_range=window, time_bucket="month", request_id="rmb",
                ),
            )
            self._assert_basis(explicit_rmb, "rmb", "debt_balance_trend")
            self.assertEqual(Decimal("21"), sum(map(self._decimal, self._non_null_values(explicit_rmb))))

            filtered = self._result(
                harness,
                self._request(
                    "receivable", "debt_balance_trend", "original", month=None,
                    time_range=window, time_bucket="month", currency="USD", request_id="filtered",
                ),
            )
            self._assert_basis(filtered, "original", "debt_balance_trend_original")
            self.assertEqual(Decimal("3"), sum(map(self._decimal, self._non_null_values(filtered))))

            harness.conn.execute("DELETE FROM vk_dw.customer_debt_bymonth_dw")
            self._seed_trend(harness, include_eur=True)
            cross = self._result(
                harness,
                self._request(
                    "receivable", "debt_balance_trend", "auto", month=None,
                    time_range=window, time_bucket="month", request_id="cross",
                ),
            )
            self._assert_basis(cross, "rmb", "debt_balance_trend")
            self.assertEqual(Decimal("70"), sum(map(self._decimal, self._non_null_values(cross))))
            self.assertIn("2026-07", str(cross))
            self.assertIn("2026-08", str(cross))

    def test_aging_original_variants_hand_calculated_and_fx7(self):
        harness = self._new_harness()
        self._receivable_schema(harness)
        self._seed_aging(harness)
        expected = {
            metric: Decimal(str(value))
            for metric, _rmb, value in AGING_VARIANTS
        }
        for original_metric, rmb_metric, expected_original in AGING_VARIANTS:
            with self.subTest(metric=original_metric):
                auto = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "auto", month=None),
                )
                self._assert_basis(auto, "original", original_metric)
                self.assertEqual(expected[original_metric], self._decimal(self._non_null_values(auto)[0]))

                explicit_rmb = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "rmb", month=None, request_id="rmb"),
                )
                self._assert_basis(explicit_rmb, "rmb", rmb_metric)
                self.assertEqual(
                    expected[original_metric] * 7,
                    self._decimal(self._non_null_values(explicit_rmb)[0]),
                )

                filtered = self._result(
                    harness,
                    self._request(
                        "receivable", rmb_metric, "original", month=None,
                        currency="USD", request_id="filtered",
                    ),
                )
                self._assert_basis(filtered, "original", original_metric)
                self.assertEqual(expected[original_metric], self._decimal(self._non_null_values(filtered)[0]))

        harness.conn.execute("DELETE FROM vk_dwd.customer_aging_dwd")
        self._seed_aging(harness, include_eur=True)
        for original_metric, rmb_metric, expected_original in AGING_VARIANTS:
            with self.subTest(metric=original_metric, scope="two_currency_auto"):
                cross = self._result(
                    harness,
                    self._request("receivable", rmb_metric, "auto", month=None, request_id="cross"),
                )
                self._assert_basis(cross, "rmb", rmb_metric)
                self.assertEqual(
                    Decimal(str(expected_original)) * 14,
                    sum(map(self._decimal, self._non_null_values(cross))),
                )

    def test_inventory_currency_basis_pairs_scalar_and_snapshot_qualifiers(self):
        harness = self._new_harness()
        self._inventory_schema(harness)
        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,ddp_amount_rmb,currency_no,status,goods_num,piece_num,unit",
            [(1, 7, "USD", 1, 1, 1, "Pcs")],
        )
        harness.insert(
            "vk_dwd.inventory_cost_dwd",
            "cost_amount,cost_amount_rmb,ddp_amount,ddp_amount_rmb,currency_no,bill_date,goods_num,piece_num,unit",
            [(1, 7, 1, 7, "USD", "2026-08", 1, 1, "Pcs")],
        )

        for pair in INVENTORY_PAIRS:
            with self.subTest(metric=pair["original"], time_policy=pair["time_policy"]):
                auto = self._result(
                    harness,
                    self._request("inventory", pair["rmb"], "auto", month=None),
                )
                self._assert_basis(auto, "original", pair["original"])
                self.assertEqual(Decimal("1"), self._decimal(self._non_null_values(auto)[0]))

                explicit_rmb = self._result(
                    harness,
                    self._request("inventory", pair["rmb"], "rmb", month=None, request_id="rmb"),
                )
                self._assert_basis(explicit_rmb, "rmb", pair["rmb"])
                self.assertEqual(Decimal("7"), self._decimal(self._non_null_values(explicit_rmb)[0]))

                filtered = self._result(
                    harness,
                    self._request(
                        "inventory", pair["rmb"], "original", month=None,
                        currency="USD", request_id="filtered",
                    ),
                )
                self._assert_basis(filtered, "original", pair["original"])
                self.assertEqual(Decimal("1"), self._decimal(self._non_null_values(filtered)[0]))
                if pair["time_policy"] == "latest_snapshot":
                    self.assertIn("2026-08", str(filtered))

        harness.insert(
            "vk_dw.inventory_barcode_detail_dw",
            "ddp_amount,ddp_amount_rmb,currency_no,status,goods_num,piece_num,unit",
            [(1, 7, "EUR", 1, 1, 1, "Pcs")],
        )
        harness.insert(
            "vk_dwd.inventory_cost_dwd",
            "cost_amount,cost_amount_rmb,ddp_amount,ddp_amount_rmb,currency_no,bill_date,goods_num,piece_num,unit",
            [(1, 7, 1, 7, "EUR", "2026-08", 1, 1, "Pcs")],
        )
        for pair in INVENTORY_PAIRS:
            with self.subTest(metric=pair["original"], scope="two_currency_auto"):
                cross = self._result(
                    harness,
                    self._request("inventory", pair["rmb"], "auto", month=None, request_id="cross"),
                )
                self._assert_basis(cross, "rmb", pair["rmb"])
                self.assertEqual(Decimal("14"), sum(map(self._decimal, self._non_null_values(cross))))


    def test_return_rate_preserves_recorded_conversion_timing(self):
        harness = self._new_harness()
        for table, amount in (("sale_bill_goods_detail_dwd", "delivery_amount"),
                              ("delivery_return_detail_dwd", "return_amount")):
            self._ensure_column(harness, "vk_dwd", table, amount, "REAL")
            self._ensure_column(harness, "vk_dwd", table, "currency_no", "TEXT")
        # Recorded RMB amounts deliberately use different conversion values at
        # delivery and return time: original rate 20/100, recorded RMB 280/700.
        harness.insert("vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount,delivery_amount_rmb,currency_no,delivery_time,bill_status,is_inner_cus",
            [(100, 700, "USD", "2026-08-05", 6, "n")])
        harness.insert("vk_dwd.delivery_return_detail_dwd",
            "return_amount,return_amount_rmb,currency_no,statement_time,status,complnt_type,channel_type,is_inner_cus",
            [(20, 280, "USD", "2026-08-20", 4, 1, 1, "n")])
        original = self._result(harness, self._request("delivery", "return_amount_rate", "auto", month="2026-08"))
        self.assertAlmostEqual(.2, float(self._non_null_values(original)[0]))
        self.assertEqual("USD", original["rows"][0]["currency"])
        rmb = self._result(harness, self._request("delivery", "return_amount_rate", "rmb", month="2026-08"))
        self.assertAlmostEqual(.4, float(self._non_null_values(rmb)[0]))
        harness.insert("vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount,delivery_amount_rmb,currency_no,delivery_time,bill_status,is_inner_cus",
            [(50, 400, "EUR", "2026-08-05", 6, "n")])
        harness.insert("vk_dwd.delivery_return_detail_dwd",
            "return_amount,return_amount_rmb,currency_no,statement_time,status,complnt_type,channel_type,is_inner_cus",
            [(10, 80, "EUR", "2026-08-20", 4, 1, 1, "n")])
        combined = self._result(harness, self._request("delivery", "return_amount_rate", "auto", month="2026-08"))
        self.assertAlmostEqual(360 / 1100, float(self._non_null_values(combined)[0]))
        filtered = self._result(harness, self._request("delivery", "return_amount_rate", "auto", month="2026-08", currency="USD"))
        self.assertAlmostEqual(.2, float(self._non_null_values(filtered)[0]))


if __name__ == "__main__":
    unittest.main()
