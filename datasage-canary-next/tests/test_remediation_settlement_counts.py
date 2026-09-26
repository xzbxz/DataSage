"""Offline F01 regressions for settlement per-bill evidence counts.

The existing ``test_remediation_remaining_cases`` harness owns the isolated
SQLite database, MySQL-function adapters, socket tripwires, and raw-to-model
projection path. Expected values below are hand-authored from the synthetic
rows; no contract value is used to calculate an oracle.
"""

from __future__ import annotations

import importlib
import unittest


public = importlib.import_module("test_remediation_remaining_cases")


class SettlementCountRemediationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = public.RemainingCaseTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd ADD COLUMN bill_id TEXT"
        )
        self.h.conn.execute(
            "ALTER TABLE vk_dwd.receivable_bill_detail_dwd "
            "ADD COLUMN verification_complete_time TEXT"
        )
        self.h.conn.create_function(
            "FIELD",
            -1,
            lambda value, *items: items.index(value) + 1 if value in items else 0,
        )

    @staticmethod
    def _bill(
        bill_id: str,
        bill_time: str,
        completion_time: str,
        unsettled: float | None,
        customer: str | None = "A",
    ) -> tuple:
        return (
            unsettled,
            1,
            "C",
            bill_time,
            "n",
            customer,
            customer,
            customer,
            "ORG",
            "CNY",
            bill_id,
            completion_time,
        )

    def _insert(self, rows: list[tuple]) -> None:
        self.h.insert(
            "vk_dwd.receivable_bill_detail_dwd",
            "detail_unsettled_amount,exchange_rate,bill_status,bill_time,"
            "is_inner_cus,customer_id,customer_no,customer_name,org_name,"
            "currency_no,bill_id,verification_complete_time",
            rows,
        )

    def _query(
        self,
        metric: str = "average_settlement_days",
        dimensions: list[str] | None = None,
    ) -> tuple[dict, list[dict]]:
        response = self.h.query(
            public.metric(
                metric,
                "receivable",
                month=None,
                dimensions=dimensions or [],
            )
        )
        result = self.h.result(response)
        raw_rows = list(self.h.sql_trace[-1]["database_rows"])
        return result, raw_rows

    def test_T04_one_bill_two_exclusion_reasons_has_one_per_bill_match(self) -> None:
        self._insert(
            [self._bill("both", "2026-08-10", "2026-08-08", 100)]
        )
        result, raw_rows = self._query()

        self.assertEqual(1, raw_rows[0]["__matched_row_count"])
        self.assertEqual(1, raw_rows[0]["excluded_negative_bill_count"])
        self.assertEqual(1, raw_rows[0]["excluded_open_balance_bill_count"])
        self.assertIsNone(raw_rows[0]["metric_value"])
        self.assertEqual("undefined", result["data_state"])
        facts = public.facts(result)[0]
        self.assertIsNone(facts["metric_value"])
        self.assertEqual(0, facts["sample_bill_count"])

    def test_T05_two_bills_with_distinct_reasons_count_two(self) -> None:
        self._insert(
            [
                self._bill("negative", "2026-08-10", "2026-08-08", 0),
                self._bill("open", "2026-08-10", "2026-08-12", 100),
            ]
        )
        result, raw_rows = self._query()

        self.assertEqual(2, raw_rows[0]["__matched_row_count"])
        self.assertEqual(1, raw_rows[0]["excluded_negative_bill_count"])
        self.assertEqual(1, raw_rows[0]["excluded_open_balance_bill_count"])
        self.assertEqual("undefined", result["data_state"])
        self.assertIsNone(public.facts(result)[0]["metric_value"])

    def test_T06_normal_two_day_bill_keeps_public_value_and_count(self) -> None:
        self._insert(
            [self._bill("normal", "2026-08-01", "2026-08-03", 0)]
        )
        result, raw_rows = self._query()

        self.assertEqual(1, raw_rows[0]["__matched_row_count"])
        self.assertEqual(2, raw_rows[0]["metric_value"])
        self.assertEqual(1, raw_rows[0]["sample_bill_count"])
        facts = public.facts(result)[0]
        self.assertEqual(2, facts["metric_value"])
        self.assertEqual(1, facts["sample_bill_count"])
        self.assertEqual("rows", result["data_state"])

    def test_T07_empty_and_all_excluded_states_remain_distinct(self) -> None:
        empty_result, empty_raw = self._query()
        self.assertEqual(0, empty_raw[0]["__matched_row_count"])
        self.assertEqual("empty", empty_result["data_state"])
        self.assertEqual([], empty_result["rows"])

        self._insert(
            [self._bill("excluded", "2026-08-10", "2026-08-08", 100)]
        )
        excluded_result, excluded_raw = self._query()
        self.assertEqual(1, excluded_raw[0]["__matched_row_count"])
        self.assertEqual("undefined", excluded_result["data_state"])
        self.assertIsNone(public.facts(excluded_result)[0]["metric_value"])

    def test_T08_duplicate_details_share_one_per_bill_row(self) -> None:
        self._insert(
            [
                self._bill("duplicate", "2026-08-01", "2026-08-03", 0),
                self._bill("duplicate", "2026-08-01", "2026-08-03", 0),
            ]
        )
        result, raw_rows = self._query()

        self.assertEqual(1, raw_rows[0]["__matched_row_count"])
        self.assertEqual(1, raw_rows[0]["sample_bill_count"])
        self.assertEqual(2, raw_rows[0]["metric_value"])
        self.assertEqual(2, public.facts(result)[0]["metric_value"])

    def test_T09_raw_match_count_stays_internal_through_model_wire(self) -> None:
        self._insert(
            [self._bill("wire", "2026-08-01", "2026-08-03", 0)]
        )
        result, raw_rows = self._query()

        self.assertEqual(1, raw_rows[0]["__matched_row_count"])
        self.assertEqual(result["row_count"], len(result["rows"]))
        self.assertEqual(1, result["row_count"])
        facts = public.facts(result)[0]
        self.assertNotIn("__matched_row_count", facts)
        self.assertEqual(2, facts["metric_value"])

    def test_grouped_maximum_handles_null_dimension_and_per_group_counts(self) -> None:
        self._insert(
            [
                self._bill("a-short", "2026-08-01", "2026-08-03", 0, "A"),
                self._bill("a-long", "2026-07-01", "2026-08-10", 0, "A"),
                self._bill("null-dimension", "2026-06-01", "2026-08-10", 0, None),
            ]
        )
        _result, raw_rows = self._query(
            metric="maximum_settlement_days", dimensions=["customer"]
        )

        by_customer = {row["customer_name"]: row for row in raw_rows}
        self.assertEqual(40, by_customer["A"]["metric_value"])
        self.assertEqual(2, by_customer["A"]["__matched_row_count"])
        self.assertEqual(70, by_customer[None]["metric_value"])
        self.assertEqual(1, by_customer[None]["__matched_row_count"])

    def test_grouped_distribution_preserves_multiple_bands_and_null_group(self) -> None:
        self._insert(
            [
                self._bill("a-short", "2026-08-01", "2026-08-03", 0, "A"),
                self._bill("a-long", "2026-07-01", "2026-08-10", 0, "A"),
                self._bill("null-dimension", "2026-06-01", "2026-08-10", 0, None),
            ]
        )
        _result, raw_rows = self._query(
            metric="settlement_days_distribution", dimensions=["customer"]
        )

        by_key = {
            (row["customer_name"], row["settlement_band"]): row
            for row in raw_rows
        }
        self.assertEqual(3, len(by_key))
        self.assertEqual(2, by_key[("A", "0-30天")]["__matched_row_count"])
        self.assertEqual(2, by_key[("A", "31-60天")]["__matched_row_count"])
        self.assertEqual(1, by_key[(None, "61-90天")]["__matched_row_count"])
        self.assertTrue(all(row["sample_bill_count"] == 1 for row in by_key.values()))


if __name__ == "__main__":
    unittest.main()
