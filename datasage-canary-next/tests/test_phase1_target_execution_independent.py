"""Standalone guarded public target execution probe.

This probe owns only the frozen TARGET-01 scenario copied into
``phase1-target-oracle.json``.  It imports the existing SQLite harness module
as a module (without importing the old v1 test class or inventory/receivable
probes), assembles explicit NULL coverage rows, and calls the public target
metric API.  It is intended for root's guarded source-copy runner and is not
executed while authored here.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import unittest

import test_remediation_remaining_cases as _remaining


HERE = Path(__file__).resolve().parent / "fixtures"
ORACLE = json.loads((HERE / "phase1-target-oracle.json").read_text(encoding="utf-8"))
SCENARIO = ORACLE["scenario"]


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AssertionError(f"invalid numeric fact {value!r}") from error
    if not result.is_finite():
        raise AssertionError(f"non-finite numeric fact {value!r}")
    return result


def _facts(row: dict) -> dict:
    facts = row.get("facts")
    return facts if isinstance(facts, dict) else row


def _states(row: dict) -> dict:
    states = row.get("states")
    return states if isinstance(states, dict) else {}


_LABELS = {
    "部门": "department",
    "客户部门": "department",
    "department": "department",
    "customer_dept": "department",
}


def _department(row: dict) -> str | None:
    values = row.get("dimensions")
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            key = item.get("name", item.get("code", item.get("field", item.get("dimension", item.get("label")))))
            canonical = _LABELS.get(key, key)
            if canonical == "department":
                return None if item.get("value") is None else str(item["value"])
    for key in ("department", "customer_dept"):
        if row.get(key) is not None:
            return str(row[key])
    return None


def _rows(result: dict) -> dict[str, dict]:
    raw = result.get("rows")
    if not isinstance(raw, list):
        raise AssertionError(f"target rows missing: {result!r}")
    output: dict[str, dict] = {}
    for row in raw:
        if isinstance(row, dict) and _department(row) is not None:
            output[_department(row)] = row
    return output


def _first(row: dict, *names: str) -> object:
    facts = _facts(row)
    states = _states(row)
    for name in names:
        if name in facts:
            return facts[name]
        if name in row:
            return row[name]
        if name in states:
            return states[name]
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
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _result(payload: dict) -> dict:
    if payload.get("status") not in {"success", "partial"}:
        raise AssertionError(f"target public query failed: {payload!r}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise AssertionError(f"unexpected target envelope: {payload!r}")
    result = results[0]
    if not isinstance(result, dict) or result.get("status") not in {None, "success", "partial"}:
        raise AssertionError(f"unexpected target result: {result!r}")
    return result


class TargetPhase1StandaloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _remaining.RemainingCaseTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self._assemble_fixture()

    def _assemble_fixture(self) -> None:
        self.harness.conn.execute("DROP TABLE IF EXISTS vk_dwd.delivery_target_detail_dwd")
        self.harness.conn.execute(
            "CREATE TABLE vk_dwd.delivery_target_detail_dwd(detail_id INTEGER,year_month TEXT,customer_id TEXT,customer_no TEXT,customer_name TEXT,customer_dept TEXT,org_name TEXT,currency_no TEXT,detail_target_rmb REAL,detail_target_amount REAL,is_inner_cus TEXT)"
        )
        target_rows = []
        actual_rows = []
        detail_id = 1
        for row in SCENARIO["source_facts"]:
            department = row["department"]
            # Explicit NULL rows preserve missing target coverage as unknown.
            target = None if row.get("target_rmb") is None else float(row["target_rmb"])
            target_rows.append((detail_id, "2026-09", department, department, department, department, "ORG", "CNY", target, target, "n"))
            detail_id += 1
            # Harness read clock is 2026-09-02; 2026-09-01 actuals are
            # observed and remain in the current-month population.
            actual = None if row.get("actual_rmb") is None else float(row["actual_rmb"])
            actual_rows.append((actual, 6, "n", "2026-09-01", department))
        self.harness.insert(
            "vk_dwd.delivery_target_detail_dwd",
            "detail_id,year_month,customer_id,customer_no,customer_name,customer_dept,org_name,currency_no,detail_target_rmb,detail_target_amount,is_inner_cus",
            target_rows,
        )
        self.harness.insert(
            "vk_dwd.sale_bill_goods_detail_dwd",
            "delivery_amount_rmb,bill_status,is_inner_cus,delivery_time,customer_dept",
            actual_rows,
        )

    def request(self, *, limit: int = 2) -> dict:
        request = _remaining.metric(
            "delivery_target_completion",
            "target",
            request_id="target-standalone",
            month="2026-09",
            dimensions=["department"],
            currency_basis="rmb",
            attribution_mode="transaction_detail",
            analysis={
                "row_filters": list(SCENARIO["request"]["row_filters"]),
                "group_filters": list(SCENARIO["request"]["group_filters"]),
            },
            order_by=dict(SCENARIO["request"]["order_by"]),
            limit=limit,
        )
        return request

    def query_result(self, *, limit: int = 2) -> dict:
        return _result(self.harness.invoke("datasage_query", {"requests": [self.request(limit=limit)]}))

    def test_provenance_and_exact_copy(self) -> None:
        self.assertEqual("26de378bb3ec07f9ae66e52ea5acb37e9983fda7a96508355636d5930e97cc0a", ORACLE["provenance"]["source_sha256"])
        self.assertEqual("scenarios.target_department_completion", ORACLE["provenance"]["source_section"])
        canonical = json.dumps(SCENARIO, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(ORACLE["provenance"]["scenario_sha256"], hashlib.sha256(canonical).hexdigest())

    def test_target_public_rows_boundary_unknown_reconciliation_and_limit(self) -> None:
        result = self.query_result(limit=2)
        rows = _rows(result)
        self.assertEqual(["D-I-LOW", "D-A"], list(rows)[:2], result)
        expected_groups = {row["department"]: row for row in SCENARIO["hand_calculation"]["department_groups"]}
        for department in ["D-I-LOW", "D-A"]:
            row = rows[department]
            expected = expected_groups[department]
            self.assertEqual(_decimal(expected["completion_rate"]), _decimal(_first(row, "completion_rate")), department)
            self.assertEqual(_decimal(expected["target_rmb"]), _decimal(_first(row, "target_amount_rmb")), department)
            self.assertEqual(_decimal(expected["actual_rmb"]), _decimal(_first(row, "actual_amount_rmb")), department)
            self.assertEqual(_decimal(expected["gap_rmb"]), _decimal(_first(row, "gap_amount_rmb")), department)
        self.assertNotIn("D-B-EXACT-80", rows)
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn("completion_rate", serialized)
        self.assertIn("2026-09", serialized)
        self.assertEqual(9, _numeric_evidence(result, "analysis_population_count"))
        self.assertEqual(3, _numeric_evidence(result, "analysis_match_count"))
        self.assertEqual(3, _numeric_evidence(result, "analysis_unknown_count"))
        self.assertEqual(3, _numeric_evidence(result, "analysis_excluded_count"))
        # The limited page need not carry every unknown label. Re-read with a
        # sufficient display limit before asserting the full unknown set and
        # the absence of known exclusions.
        full = self.query_result(limit=20)
        full_serialized = json.dumps(full, ensure_ascii=False, default=str)
        self.assertFalse(full.get("truncated", False), full)
        for department in SCENARIO["hand_calculation"]["unknown_groups_retained"]:
            self.assertIn(department, full_serialized)
        for department in SCENARIO["hand_calculation"]["known_exclusions"]:
            self.assertNotIn(department, full_serialized)

    def test_exact_80_mutation_crosses_strict_boundary(self) -> None:
        base = self.query_result(limit=10)
        self.assertNotIn("D-B-EXACT-80", _rows(base))
        self.harness.conn.execute("UPDATE vk_dwd.sale_bill_goods_detail_dwd SET delivery_amount_rmb=799 WHERE customer_dept='D-B-EXACT-80'")
        changed = self.query_result(limit=10)
        rows = _rows(changed)
        self.assertIn("D-B-EXACT-80", rows)
        self.assertEqual(_decimal("0.799"), _decimal(_first(rows["D-B-EXACT-80"], "completion_rate")))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TargetPhase1StandaloneTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
