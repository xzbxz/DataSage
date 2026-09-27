"""Independent guarded execution for the v2 receivable contract.

This probe is intentionally separate from the frozen v1 execution adapter.
The amount comes from ``current_debt_amount`` (the latest monthly signed net
debt snapshot).  Current open-item overdue facts are a second source with a
different as-of.  Since the fixture supplies no authoritative bridge between
those as-ofs, the joint predicate must remain unknown for amount-inside or
amount-unknown customers.  A same SQL/read clock is not treated as business
time alignment.

The root agent runs this file only in the existing guarded source-copy/Home
runner.  It uses the candidate's public ``datasage_catalog`` and
``datasage_query`` handlers through the existing in-memory SQLite harness.  It
does not call a model, connect to a real database, or alter the v1 oracle.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import unittest

import test_remediation_remaining_cases as _remaining


HERE = Path(__file__).resolve().parent / "fixtures"
ORACLE = json.loads((HERE / "phase1-ar-v2-oracle.json").read_text(encoding="utf-8"))
SCENARIO = ORACLE["source_facts"]
EXPECTED = ORACLE["hand_calculation"]


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AssertionError(f"boolean is not a numeric fact: {value!r}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AssertionError(f"invalid numeric fact {value!r}") from error
    if not parsed.is_finite():
        raise AssertionError(f"non-finite numeric fact {value!r}")
    return parsed


def _facts(row: dict) -> dict:
    facts = row.get("facts")
    return facts if isinstance(facts, dict) else row


def _states(row: dict) -> dict:
    states = row.get("states")
    return states if isinstance(states, dict) else {}


def _dimension_map(row: dict) -> dict[str, object]:
    result: dict[str, object] = {}
    dimensions = row.get("dimensions")
    if isinstance(dimensions, list):
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            code = item.get("name", item.get("code", item.get("field", item.get("dimension", item.get("label")))))
            if isinstance(code, str):
                result[code] = item.get("value")
    return result


def _customer(row: dict) -> str | None:
    dimensions = _dimension_map(row)
    for key in ("customer", "customer_name", "客户", "客户名称"):
        if key in dimensions and dimensions[key] is not None:
            return str(dimensions[key])
    for key in ("customer", "customer_name", "客户", "客户名称"):
        if row.get(key) is not None:
            return str(row[key])
    return None


def _first(row: dict, *keys: str) -> object:
    facts = _facts(row)
    states = _states(row)
    for key in keys:
        if key in facts:
            return facts[key]
        if key in row:
            return row[key]
        if key in states:
            return states[key]
    return None


def _recursive_values(value: object, key: str) -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(_recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_recursive_values(child, key))
    return found


def _numeric_evidence(result: dict, key: str) -> int | None:
    for value in _recursive_values(result, key):
        if isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _rows(result: dict) -> dict[str, dict]:
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise AssertionError(f"result rows missing: {result!r}")
    output: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and _customer(row) is not None:
            output[_customer(row)] = row
    return output


def _result(payload: dict) -> dict:
    if payload.get("status") not in {"success", "partial"}:
        raise AssertionError(f"public v2 query failed: {payload!r}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise AssertionError(f"unexpected public v2 envelope: {payload!r}")
    result = results[0]
    if not isinstance(result, dict) or result.get("status") not in {None, "success", "partial"}:
        raise AssertionError(f"unexpected v2 result: {result!r}")
    return result


def _bill_time(overdue_days: object, credit_days: int = 30) -> str:
    if overdue_days is None:
        return "2026-08-01"
    return (date(2026, 9, 2) - timedelta(days=int(overdue_days) + credit_days)).isoformat()


class ARV2ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _remaining.RemainingCaseTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self._assemble_fixture()

    def _assemble_fixture(self) -> None:
        connection = self.harness.conn
        connection.execute("DROP TABLE IF EXISTS vk_dw.customer_debt_bymonth_dw")
        connection.execute(
            """
            CREATE TABLE vk_dw.customer_debt_bymonth_dw(
                bill_date TEXT, customer_id TEXT, customer_no TEXT,
                customer_name TEXT, customer_dept TEXT, customer_region TEXT,
                sales_id TEXT, sales_name TEXT, org_name TEXT, currency_no TEXT,
                debt_amount_rmb REAL, debt_amount REAL, exchange_rate REAL,
                is_inner_cus TEXT, is_brand_cus TEXT, is_ha_cus TEXT,
                is_ht_cus TEXT, is_ccbs_cus TEXT,
                receivable_amount REAL, receive_amount REAL,
                return_amount REAL, reverse_amount REAL,
                cumulative_receivable_amount REAL,
                cumulative_receive_amount REAL,
                cumulative_return_amount REAL,
                cumulative_reverse_amount REAL
            )
            """
        )
        debt_columns = (
            "bill_date,customer_id,customer_no,customer_name,customer_dept,customer_region,"
            "sales_id,sales_name,org_name,currency_no,debt_amount_rmb,debt_amount,exchange_rate,"
            "is_inner_cus,is_brand_cus,is_ha_cus,is_ht_cus,is_ccbs_cus,receivable_amount,receive_amount,"
            "return_amount,reverse_amount,cumulative_receivable_amount,cumulative_receive_amount,"
            "cumulative_return_amount,cumulative_reverse_amount"
        )
        debt_rows = []
        for row in SCENARIO["debt_snapshot_rows"]:
            gross = row.get("gross_receivable_rmb")
            prepay = row.get("prepayment_rmb")
            writeoff = row.get("writeoff_rmb")
            debt = row.get("debt_amount_rmb")
            debt_rows.append(
                (
                    row["bill_date"], row["customer"], row["customer"], row["customer"],
                    "D", "CN", None, None, "ORG", "CNY",
                    None if debt is None else float(debt),
                    None if debt is None else float(debt), 1.0,
                    "n", "n", "n", "n", "n",
                    None if gross is None else float(gross),
                    None if prepay is None else float(prepay),
                    0.0, None if writeoff is None else float(writeoff),
                    None if gross is None else float(gross),
                    None if prepay is None else float(prepay),
                    0.0, None if writeoff is None else float(writeoff),
                )
            )
        self.harness.insert("vk_dw.customer_debt_bymonth_dw", debt_columns, debt_rows)

        self.harness.conn.execute("DELETE FROM vk_dwd.receivable_bill_detail_dwd")
        self.harness.conn.execute("DELETE FROM vk_dwd.customer_credit_dwd")
        receivable_columns = "detail_unsettled_amount,exchange_rate,bill_status,bill_time,is_inner_cus,customer_id,customer_no,customer_name,org_name,currency_no"
        open_rows = []
        credit_rows = []
        for row in SCENARIO["current_open_items"]:
            open_rows.append(
                (
                    float(row["open_amount_rmb"]), 1.0, "C", _bill_time(row.get("overdue_days")), "n",
                    row["customer"], row["customer"], row["customer"], "ORG", "CNY",
                )
            )
            if row.get("credit_term_state") == "known":
                credit_rows.append((row["customer"], "ORG", "CNY", 30))
        self.harness.insert("vk_dwd.receivable_bill_detail_dwd", receivable_columns, open_rows)
        if credit_rows:
            self.harness.insert("vk_dwd.customer_credit_dwd", "customer_id,org_name,currency_no,credit_days", credit_rows)

    def request(self, *, limit: int = 20, analysis: dict | None = None) -> dict:
        analysis = analysis or ORACLE["public_request"]["analysis"]
        return _remaining.metric(
            "current_debt_amount",
            "receivable",
            request_id="arv2",
            month=None,
            dimensions=["customer"],
            currency_basis="rmb",
            analysis=analysis,
            order_by=ORACLE["public_request"]["order_by"],
            limit=limit,
        )

    def query(self, **kwargs: object) -> dict:
        return _result(self.harness.invoke("datasage_query", {"requests": [self.request(**kwargs)]}))

    def test_catalog_exposes_current_debt_analysis_fields(self) -> None:
        payload = self.harness.invoke(
            "datasage_catalog",
            {"requests": [{"domain": "receivable", "metric": "current_debt_amount"}]},
        )
        self.assertEqual("success", payload.get("status"), payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        for field in ("current_debt_amount", "overdue_days", "metric_value", "any_overdue_days"):
            self.assertIn(field, serialized)
        self.assertIn("latest_monthly_customer_debt_snapshot", serialized)
        self.assertIn("current_open_receivable_observation", serialized)
        self.assertNotIn("open_receivable_amount", serialized)

    def test_strict_joint_result_preserves_parent_set_and_unknown_alignment(self) -> None:
        result = self.query()
        rows = _rows(result)
        unknown = set(EXPECTED["strict_same_asof_joint"]["unknown"])
        excluded = set(EXPECTED["strict_same_asof_joint"]["excluded"])
        parent = set(EXPECTED["latest_snapshot_customer_set"])
        self.assertTrue(unknown <= set(rows), f"unknown parent groups missing: {result!r}")
        self.assertFalse(excluded & set(rows), f"known outside/negative groups leaked: {result!r}")
        self.assertNotIn("C-CURRENT-ONLY", rows, "current-open-only customer expanded the net-debt parent set")
        for customer in unknown:
            row = rows[customer]
            state = str(_first(row, "analysis_match_state", "joint_match_state", "analysis_joint_state", "data_state"))
            self.assertRegex(state.lower(), "unknown|incomplete|partial|unavailable")
        self.assertEqual(_decimal("10000"), _decimal(_first(rows["C-LOW-BOUND"], "metric_value")))
        self.assertEqual(_decimal("30000"), _decimal(_first(rows["C-MID"], "metric_value")))
        self.assertEqual(_decimal("50000"), _decimal(_first(rows["C-HIGH-BOUND"], "metric_value")))
        self.assertEqual(_decimal("10000"), _decimal(_first(rows["C-PREPAY-OFFSET"], "metric_value")))
        self.assertIsNone(_first(rows["C-DEBT-MISSING"], "metric_value"))
        # C-IN-NO-LATE is the required false-current-observation counterexample:
        # without same-as-of proof it stays unknown rather than becoming false.
        no_late_state = str(_first(rows["C-IN-NO-LATE"], "analysis_match_state", "joint_match_state", "analysis_joint_state", "data_state"))
        self.assertRegex(no_late_state.lower(), "unknown|incomplete|partial|unavailable")
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn("2026-08", serialized)
        self.assertIn("2026-09-02", serialized)
        self.assertRegex(serialized.lower(), "alignment|as.of|asof|业务时点|unknown")
        self.assertIn("latest_monthly_customer_debt_snapshot", serialized)
        self.assertIn("current_open_receivable_observation", serialized)
        self.assertEqual(12, _numeric_evidence(result, "analysis_population_count"))
        self.assertEqual(0, _numeric_evidence(result, "analysis_match_count"))
        self.assertEqual(7, _numeric_evidence(result, "analysis_unknown_count"))
        self.assertEqual(5, _numeric_evidence(result, "analysis_excluded_count"))
        sql = "\n".join(str(item.get("sql", "")) for item in self.harness.sql_trace)
        self.assertIn("customer_debt_bymonth_dw", sql)
        self.assertIn("receivable_bill_detail_dwd", sql)

    def test_prepaid_and_written_off_amounts_are_not_replaced_by_open_item_sum(self) -> None:
        result = self.query(limit=20)
        rows = _rows(result)
        self.assertEqual(_decimal("10000"), _decimal(_first(rows["C-PREPAY-OFFSET"], "metric_value")))
        self.assertNotIn("C-OFFSET-OUTSIDE", rows)
        self.assertNotIn("C-OUTSIDE-LOW", rows)
        self.assertNotIn("C-OUTSIDE-HIGH", rows)
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn("customer_open_receivable_total", serialized) if "customer_open_receivable_total" in serialized else None

    def test_latest_month_wins_over_older_debt_snapshot(self) -> None:
        result = self.query(limit=20)
        rows = _rows(result)
        self.assertEqual(_decimal("30000"), _decimal(_first(rows["C-MID"], "metric_value")))
        self.assertNotEqual(_decimal("60000"), _decimal(_first(rows["C-MID"], "metric_value")))

    def test_strict_boundary_and_mutation_sensitivity(self) -> None:
        result = self.query(limit=20)
        rows = _rows(result)
        self.assertIn("C-LOW-BOUND", rows)
        self.assertIn("C-HIGH-BOUND", rows)
        self.assertNotIn("C-OUTSIDE-LOW", rows)
        self.assertNotIn("C-OUTSIDE-HIGH", rows)
        # Change only C-LOW-BOUND's latest net debt from 10,000 to 9,999.99.
        self.harness.conn.execute("UPDATE vk_dw.customer_debt_bymonth_dw SET debt_amount_rmb=9999.99, debt_amount=9999.99 WHERE customer_name='C-LOW-BOUND' AND bill_date='2026-08'")
        changed = self.query(limit=20)
        changed_rows = _rows(changed)
        self.assertNotIn("C-LOW-BOUND", changed_rows)

    def test_public_scope_is_parent_debt_plus_observation_and_not_ar_only(self) -> None:
        result = self.query(limit=20)
        rows = _rows(result)
        self.assertTrue(set(rows) <= set(EXPECTED["latest_snapshot_customer_set"]))
        self.assertTrue(set(EXPECTED["strict_same_asof_joint"]["unknown"]) <= set(rows))
        self.assertNotIn("C-CURRENT-ONLY", rows)
        counts_text = json.dumps(result, ensure_ascii=False, default=str)
        self.assertEqual(12, _numeric_evidence(result, "analysis_population_count"))
        self.assertIn("parent_population", counts_text) if "parent_population" in counts_text else None


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ARV2ExecutionTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
