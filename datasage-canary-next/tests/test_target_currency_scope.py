"""Offline target-currency and split-ledger coverage regressions.

The existing RemainingCaseTests seam owns the in-memory SQLite database, SQL
adapter, wire projection, and network tripwires.  Expected amounts and scope
states below are hand-calculated from synthetic rows; no source formula is
used to derive the expected values.
"""

from __future__ import annotations

from decimal import Decimal
import importlib
import unittest


public = importlib.import_module("test_remediation_remaining_cases")


TIME_RANGE = {"start": "2026-08-01", "end": "2026-09-01"}


class TargetCurrencyScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self._prepare_receipt_target_schema()

    def _prepare_receipt_target_schema(self) -> None:
        connection = self.h.conn
        alter_columns = {
            "vk_dwd.receive_bill_detail_dwd": (
                "detail_receive_amount REAL",
                "currency_no TEXT",
                "is_inner_cus TEXT",
                "detail_id TEXT",
                "customer_id TEXT",
                "customer_name TEXT",
                "customer_dept TEXT",
                "org_name TEXT",
                "final_sales_id TEXT",
            ),
            "vk_dwd.receive_return_bill_detail_dwd": (
                "detail_return_amount REAL",
                "currency_no TEXT",
                "is_inner_cus TEXT",
                "detail_id TEXT",
                "customer_id TEXT",
                "customer_name TEXT",
                "customer_dept TEXT",
                "org_name TEXT",
                "final_sales_id TEXT",
            ),
        }
        for table, columns in alter_columns.items():
            existing = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA vk_dwd.table_info({table.split('.')[-1]})"
                )
            }
            for definition in columns:
                name = definition.split()[0]
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_target_split_dwd (
                target_id TEXT, plan_receive_time TEXT, sales_id TEXT,
                sales_name TEXT, customer_id TEXT, customer_name TEXT,
                customer_dept TEXT, org_name TEXT, currency_no TEXT,
                exchange_rate REAL, plan_receive_rmb REAL,
                plan_receive_amount REAL, is_inner_cus TEXT
            );
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_bill_split_dwd (
                detail_id TEXT, bill_time TEXT, sales_id TEXT,
                sales_name TEXT, customer_id TEXT, customer_name TEXT,
                customer_dept TEXT, org_name TEXT, currency_no TEXT,
                detail_receive_rmb REAL, detail_receive_amount REAL,
                bill_status TEXT, is_inner_cus TEXT
            );
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_return_bill_split_dwd (
                detail_id TEXT, bill_time TEXT, sales_id TEXT,
                sales_name TEXT, customer_id TEXT, customer_name TEXT,
                customer_dept TEXT, org_name TEXT, currency_no TEXT,
                detail_return_rmb REAL, detail_return_amount REAL,
                bill_status TEXT, is_inner_cus TEXT
            );
            """
        )
        connection.commit()

    def _base_receipt(
        self,
        detail_id: str,
        customer_id: str,
        customer_name: str,
        currency: str | None,
        *,
        internal: str = "n",
    ) -> None:
        self.h.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_id,bill_time,detail_receive_amount,detail_receive_rmb,bill_status,is_inner_cus,currency_no,customer_id,customer_name,customer_dept,org_name,final_sales_id",
            [
                (
                    detail_id,
                    "2026-08-15",
                    60,
                    60,
                    "C",
                    internal,
                    currency,
                    customer_id,
                    customer_name,
                    "D",
                    "O",
                    "BASE-SALES",
                )
            ],
        )

    def _split_receipt(
        self,
        detail_id: str,
        sales_id: str,
        currency: str | None,
        amount: float,
        *,
        customer_id: str,
        customer_name: str,
    ) -> None:
        self.h.insert(
            "vk_dwd.receive_bill_split_dwd",
            "detail_id,bill_time,sales_id,sales_name,customer_id,customer_name,customer_dept,org_name,currency_no,detail_receive_rmb,detail_receive_amount,bill_status,is_inner_cus",
            [
                (
                    detail_id,
                    "2026-08-15",
                    sales_id,
                    sales_id,
                    customer_id,
                    customer_name,
                    "D",
                    "O",
                    currency,
                    amount,
                    amount,
                    "C",
                    "n",
                )
            ],
        )

    def _target(
        self,
        target_id: str,
        sales_id: str,
        currency: str | None,
        amount: float,
        *,
        customer_id: str,
        customer_name: str,
    ) -> None:
        self.h.insert(
            "vk_dwd.receive_target_split_dwd",
            "target_id,plan_receive_time,sales_id,sales_name,customer_id,customer_name,customer_dept,org_name,currency_no,exchange_rate,plan_receive_rmb,plan_receive_amount,is_inner_cus",
            [
                (
                    target_id,
                    "2026-08-01",
                    sales_id,
                    sales_id,
                    customer_id,
                    customer_name,
                    "D",
                    "O",
                    currency,
                    1,
                    amount,
                    amount,
                    "n",
                )
            ],
        )

    def _query(
        self,
        metric: str,
        *,
        dimensions: list[str],
        filters: dict | None = None,
        attribution_mode: str = "salesperson_allocation",
    ) -> dict:
        payload = self.h.query(
            public.metric(
                metric,
                "target",
                month=None,
                dimensions=dimensions,
                metric_filters=filters or {},
                attribution_mode=attribution_mode,
                time_range=TIME_RANGE,
            )
        )
        return self.h.result(payload)

    @staticmethod
    def _field(row: dict, name: str):
        for container_name in ("facts", "states"):
            container = row.get(container_name) or {}
            if name in container:
                return container[name]
        return row.get(name)

    def test_customer_coverage_only_group_survives_and_blocks_completion(self) -> None:
        self._base_receipt("D1", "C1", "External", "USD")
        self._split_receipt("D1", "S1", "USD", 60, customer_id="C1", customer_name="External")
        self._target("T1", "S1", "USD", 100, customer_id="C1", customer_name="External")
        self._base_receipt("D2", "C2", "Internal", "USD", internal="y")

        result = self._query("receipt_target_completion_original", dimensions=["customer", "currency"])
        self.assertEqual("success", result["status"])
        incomplete = [
            row
            for row in result["rows"]
            if self._field(row, "source_scope_unrepresented_internal_count") == 1
        ]
        self.assertEqual(1, len(incomplete), result["rows"])
        row = incomplete[0]
        self.assertEqual("source_range_incomplete", self._field(row, "source_scope_state"))
        self.assertIsNone(row["facts"].get("completion_rate"))
        self.assertIsNone(row["facts"].get("gap_amount_original"))
        self.assertIn(
            "USD",
            {dimension.get("value") for dimension in row["dimensions"]},
        )

    def test_salesperson_coverage_is_unverifiable_without_base_identity_fabrication(self) -> None:
        self._base_receipt("D1", "C1", "External", "USD")
        self._split_receipt("D1", "S1", "USD", 60, customer_id="C1", customer_name="External")
        self._target("T1", "S1", "USD", 100, customer_id="C1", customer_name="External")
        self._base_receipt("D2", "C2", "Internal", "USD", internal="y")

        result = self._query("receipt_target_completion_original", dimensions=["salesperson", "currency"])
        states = {self._field(row, "source_scope_state") for row in result["rows"]}
        self.assertEqual({"source_scope_unverifiable"}, states)
        self.assertTrue(all(any(dimension.get("value") == "USD" for dimension in row["dimensions"])
                            for row in result["rows"]))
        self.assertTrue(all(row["facts"].get("completion_rate") is None for row in result["rows"]))
        self.assertNotIn("BASE-SALES", {row["dimensions"][0].get("value") for row in result["rows"]})

    def test_complete_salesperson_scope_does_not_create_an_unknown_group(self) -> None:
        self._base_receipt("D1", "C1", "External", "USD")
        self._split_receipt("D1", "S1", "USD", 60, customer_id="C1", customer_name="External")
        self._target("T1", "S1", "USD", 100, customer_id="C1", customer_name="External")
        result = self._query("receipt_target_completion_original", dimensions=["salesperson", "currency"])
        self.assertEqual(1, len(result["rows"]))
        row = result["rows"][0]
        self.assertEqual("complete", self._field(row, "source_scope_state"))
        self.assertEqual(Decimal("0.6"), Decimal(str(row["facts"]["completion_rate"])))

    def test_collaborator_rows_do_not_multiply_base_coverage(self) -> None:
        self._base_receipt("D1", "C1", "External", "USD")
        self._split_receipt("D1", "S1", "USD", 30, customer_id="C1", customer_name="External")
        self._split_receipt("D1", "S2", "USD", 30, customer_id="C1", customer_name="External")
        self._target("T1", "S1", "USD", 50, customer_id="C1", customer_name="External")
        self._target("T1", "S2", "USD", 50, customer_id="C1", customer_name="External")

        result = self._query("receipt_target_completion_original", dimensions=["currency"])
        row = result["rows"][0]
        self.assertEqual(1, self._field(row, "source_scope_base_row_count"))
        self.assertEqual(0, self._field(row, "source_scope_unrepresented_internal_count"))
        self.assertEqual("complete", self._field(row, "source_scope_state"))
        self.assertEqual(Decimal("0.6"), Decimal(str(row["facts"]["completion_rate"])))

    def test_different_base_and_split_currency_is_not_covered(self) -> None:
        self._base_receipt("D1", "C1", "External", "USD")
        self._split_receipt("D1", "S1", "EUR", 60, customer_id="C1", customer_name="External")
        self._target("T1", "S1", "EUR", 100, customer_id="C1", customer_name="External")

        result = self._query("receipt_target_completion_original", dimensions=["currency"])
        incomplete = [
            row
            for row in result["rows"]
            if self._field(row, "source_scope_unrepresented_internal_count") == 0
            and self._field(row, "source_scope_state") == "source_range_incomplete"
        ]
        # The base is external, so the mismatch is retained as an unrepresented
        # coverage group only when the source row is internal; EUR remains a
        # normal known-currency group here.
        self.assertEqual([], incomplete)
        self.assertIn("EUR", {dimension.get("value") for row in result["rows"] for dimension in row["dimensions"]})

        self.h.conn.execute(
            "UPDATE vk_dwd.receive_bill_detail_dwd SET is_inner_cus='y' WHERE detail_id='D1'"
        )
        result = self._query("receipt_target_completion_original", dimensions=["currency"])
        self.assertTrue(
            any(
                self._field(row, "source_scope_unrepresented_internal_count") == 1
                and self._field(row, "source_scope_state") == "source_range_incomplete"
                for row in result["rows"]
            )
        )

    def test_original_missing_currency_group_is_null_known_group_survives(self) -> None:
        self._base_receipt("D1", "C1", "Known", "USD")
        self._split_receipt("D1", "S1", "USD", 60, customer_id="C1", customer_name="Known")
        self._target("T1", "S1", "USD", 100, customer_id="C1", customer_name="Known")
        self._base_receipt("D2", "C2", "Unknown", None)
        self._split_receipt("D2", "S2", None, 60, customer_id="C2", customer_name="Unknown")
        self._target("T2", "S2", None, 100, customer_id="C2", customer_name="Unknown")

        result = self._query("receipt_target_completion_original", dimensions=["currency"])
        known = [row for row in result["rows"] if row["dimensions"][0].get("value") == "USD"][0]
        self.assertEqual(Decimal("0.6"), Decimal(str(known["facts"]["completion_rate"])))
        unknown = [row for row in result["rows"] if row["dimensions"][0].get("value") != "USD"]
        self.assertEqual(1, len(unknown))
        self.assertIsNone(unknown[0]["facts"].get("completion_rate"))
        self.assertIsNone(unknown[0]["facts"].get("target_amount_original"))
        self.assertEqual("incomplete", unknown[0]["states"].get("target_data_state"))

    def test_original_scope_and_multi_currency_guard_and_rmb_control(self) -> None:
        self._base_receipt("D1", "C1", "Known", "USD")
        self._split_receipt("D1", "S1", "USD", 60, customer_id="C1", customer_name="Known")
        self._target("T1", "S1", "USD", 100, customer_id="C1", customer_name="Known")

        unscoped = self.h.query(
            public.metric(
                "receipt_target_completion_original",
                "target",
                month=None,
                dimensions=[],
                metric_filters={},
                attribution_mode="salesperson_allocation",
                time_range=TIME_RANGE,
            )
        )
        self.assertEqual("failed", unscoped["status"])
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", unscoped["results"][0]["error"]["code"])

        multi = self.h.query(
            public.metric(
                "allocated_net_receipt_amount_original",
                "target",
                month=None,
                dimensions=[],
                metric_filters={"currency": ["USD", "EUR"]},
                attribution_mode="salesperson_allocation",
                time_range=TIME_RANGE,
            )
        )
        self.assertEqual("failed", multi["status"])
        self.assertEqual("CURRENCY_SCOPE_REQUIRED", multi["results"][0]["error"]["code"])

        rmb = self._query("receipt_target_completion", dimensions=["currency"])
        self.assertEqual(Decimal("0.6"), Decimal(str(rmb["rows"][0]["facts"]["completion_rate"])))
        self.assertEqual(60, rmb["rows"][0]["facts"]["actual_amount_rmb"])


if __name__ == "__main__":
    unittest.main()
