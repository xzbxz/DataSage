"""M06 independent target-formula and SQL regressions.

This module deliberately keeps the expected arithmetic outside the contract
loader and outside the SQL builder.  ``ast.parse`` is used only to normalize
the four formula expressions structurally; no expression is evaluated or
compiled.

SQLite execution is limited to aggregate target-completion SQL with no
dimensions and no monthly time bucket.  That branch uses backtick-qualified
tables, COALESCE/CASE, and ``%s`` parameters, which are adapted to an attached
SQLite schema and ``?`` placeholders.  MySQL-only operators such as ``<=>``
and ``DATE_FORMAT`` are intentionally outside this execution claim.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from decimal import Decimal
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import types
import unittest

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

TEST_PACKAGE = "datasage_query_remediation_formula_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

analytical_queries = importlib.import_module(f"{TEST_PACKAGE}.analytical_queries")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")


EXPECTED_FORMULAS = {
    "completion_rate": "actual_amount / target_amount",
    "gap": "target_amount - actual_amount",
    "delivery_actual": "gross_delivery_amount - return_amount",
    "receipt_actual": "receipt_amount - refund_amount",
}

DELIVERY_DDL = {
    "delivery_target_detail_dwd": """
        CREATE TABLE `vk_dwd`.`delivery_target_detail_dwd` (
            detail_id TEXT, year_month TEXT, detail_target_rmb REAL,
            is_inner_cus TEXT
        )
    """,
    "sale_bill_goods_detail_dwd": """
        CREATE TABLE `vk_dwd`.`sale_bill_goods_detail_dwd` (
            goods_detail_id TEXT, delivery_time TEXT, delivery_amount_rmb REAL,
            bill_status INTEGER, is_inner_cus TEXT
        )
    """,
    "delivery_return_detail_dwd": """
        CREATE TABLE `vk_dwd`.`delivery_return_detail_dwd` (
            barcode_detail_id TEXT, statement_time TEXT, return_amount_rmb REAL,
            status INTEGER, complnt_type INTEGER, channel_type INTEGER,
            is_inner_cus TEXT
        )
    """,
}

RECEIPT_DDL = {
    "receive_target_dwd": """
        CREATE TABLE `vk_dwd`.`receive_target_dwd` (
            target_id TEXT, plan_receive_time TEXT, plan_receive_rmb REAL
        )
    """,
    "receive_bill_detail_dwd": """
        CREATE TABLE `vk_dwd`.`receive_bill_detail_dwd` (
            detail_id TEXT, bill_time TEXT, detail_receive_rmb REAL,
            bill_status TEXT
        )
    """,
    "receive_return_bill_detail_dwd": """
        CREATE TABLE `vk_dwd`.`receive_return_bill_detail_dwd` (
            detail_id TEXT, bill_time TEXT, detail_return_rmb REAL,
            bill_status TEXT
        )
    """,
}


def _formula_ast(expression: str) -> str:
    """Return a whitespace/parentheses-insensitive AST fingerprint."""

    tree = ast.parse(expression.strip(), mode="eval")
    return ast.dump(tree.body, annotate_fields=True, include_attributes=False)


def _assert_formula_summary(test: unittest.TestCase, semantics: dict) -> None:
    formula = semantics.get("formula")
    test.assertIsInstance(formula, dict)
    test.assertEqual(set(EXPECTED_FORMULAS), set(formula))
    for field, expected in EXPECTED_FORMULAS.items():
        test.assertEqual(
            _formula_ast(expected),
            _formula_ast(str(formula[field])),
            msg=f"formula field {field!r} does not preserve its independent AST",
        )


def _sqlite_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("ATTACH DATABASE ':memory:' AS `vk_dwd`")
    for ddl in (*DELIVERY_DDL.values(), *RECEIPT_DDL.values()):
        connection.execute(ddl)
    return connection


def _insert_delivery(
    connection: sqlite3.Connection,
    *,
    target_rows: list[tuple],
    gross_rows: list[tuple],
    return_rows: list[tuple],
) -> None:
    connection.executemany(
        "INSERT INTO `vk_dwd`.`delivery_target_detail_dwd` "
        "(detail_id, year_month, detail_target_rmb, is_inner_cus) VALUES (?, ?, ?, ?)",
        target_rows,
    )
    connection.executemany(
        "INSERT INTO `vk_dwd`.`sale_bill_goods_detail_dwd` "
        "(goods_detail_id, delivery_time, delivery_amount_rmb, bill_status, is_inner_cus) "
        "VALUES (?, ?, ?, ?, ?)",
        gross_rows,
    )
    connection.executemany(
        "INSERT INTO `vk_dwd`.`delivery_return_detail_dwd` "
        "(barcode_detail_id, statement_time, return_amount_rmb, status, complnt_type, channel_type, is_inner_cus) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        return_rows,
    )
    connection.commit()


def _insert_receipt(
    connection: sqlite3.Connection,
    *,
    target_rows: list[tuple],
    receipt_rows: list[tuple],
    refund_rows: list[tuple],
) -> None:
    connection.executemany(
        "INSERT INTO `vk_dwd`.`receive_target_dwd` "
        "(target_id, plan_receive_time, plan_receive_rmb) VALUES (?, ?, ?)",
        target_rows,
    )
    connection.executemany(
        "INSERT INTO `vk_dwd`.`receive_bill_detail_dwd` "
        "(detail_id, bill_time, detail_receive_rmb, bill_status) VALUES (?, ?, ?, ?)",
        receipt_rows,
    )
    connection.executemany(
        "INSERT INTO `vk_dwd`.`receive_return_bill_detail_dwd` "
        "(detail_id, bill_time, detail_return_rmb, bill_status) VALUES (?, ?, ?, ?)",
        refund_rows,
    )
    connection.commit()


def _sqlite_sql(sql: str) -> str:
    """Adapt only DB-API placeholders; do not rewrite formula or SQL logic."""

    if "<=>" in sql or "DATE_FORMAT" in sql:
        raise AssertionError(
            "this SQLite regression intentionally covers only the aggregate, "
            "MySQL-dialect-compatible target branch"
        )
    return sql.replace("%s", "?")


def _run_builder_query(
    connection: sqlite3.Connection,
    *,
    metric: str,
    observed_on: date,
    time_range: dict[str, str],
) -> dict[str, object]:
    sql, params = _build_target_sql(
        metric=metric,
        observed_on=observed_on,
        time_range=time_range,
    )
    row = connection.execute(_sqlite_sql(sql), params).fetchone()
    if row is None:
        raise AssertionError("target builder returned no aggregate row")
    return dict(row)


def _build_target_sql(
    *,
    metric: str,
    observed_on: date,
    time_range: dict[str, str],
) -> tuple[str, list[object]]:
    datasets, semantics = contracts.execution_contracts("target")
    request = {
        "metric": metric,
        "attribution_mode": "transaction_detail",
        "dimensions": [],
        "metric_filters": {},
        "time_range": time_range,
    }
    sql, params, _scope = analytical_queries._target_completion_query(
        request,
        semantics["metrics"][metric],
        datasets,
        10,
        observed_on=observed_on,
    )
    return sql, params


def _mutate_generated_delivery_sql(sql: str, mutation: str) -> tuple[str, int]:
    """Apply one controlled mutation to the actual generated delivery SQL."""

    ratio = "COALESCE(a.actual_amount_rmb, 0) / COALESCE(t.target_amount_rmb, 0)"
    reverse_ratio = "COALESCE(t.target_amount_rmb, 0) / COALESCE(a.actual_amount_rmb, 0)"
    if mutation == "reverse_ratio":
        mutated = sql.replace(ratio, reverse_ratio)
        return mutated, sql.count(ratio)
    if mutation == "scale_ratio":
        scaled = f"({ratio}) * 100"
        mutated = sql.replace(ratio, scaled)
        return mutated, sql.count(ratio)

    gap = "COALESCE(t.target_amount_rmb, 0) - COALESCE(a.actual_amount_rmb, 0)"
    reverse_gap = "COALESCE(a.actual_amount_rmb, 0) - COALESCE(t.target_amount_rmb, 0)"
    if mutation == "reverse_gap":
        mutated = sql.replace(gap, reverse_gap)
        return mutated, sql.count(gap)

    component = "-(COALESCE(SUM(`c2`.`return_amount_rmb`), 0))"
    if mutation == "add_return":
        mutated = sql.replace(component, "COALESCE(SUM(`c2`.`return_amount_rmb`), 0)")
        return mutated, sql.count(component)
    raise AssertionError(f"unknown SQL mutation: {mutation}")


def _assert_decimal(
    test: unittest.TestCase, actual: object, expected: Decimal | None, *, label: str
) -> None:
    if expected is None:
        test.assertIsNone(actual, label)
    else:
        test.assertIsNotNone(actual, label)
        test.assertAlmostEqual(float(actual), float(expected), places=10, msg=label)


def _independent_oracle(
    *, target: Decimal | None, actual: Decimal | None
) -> dict[str, Decimal | None]:
    completion = (
        None
        if target is None or actual is None or target == 0
        else actual / target
    )
    gap = None if target is None or actual is None else target - actual
    return {"target": target, "actual": actual, "completion": completion, "gap": gap}


class TargetFormulaRemediationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _datasets, cls.semantics = contracts.execution_contracts("target")

    def test_formula_summary_matches_independent_four_field_ast(self) -> None:
        _assert_formula_summary(self, self.semantics)

    def test_formula_summary_accepts_legal_whitespace_and_parentheses(self) -> None:
        mutated = deepcopy(self.semantics)
        mutated["formula"] = {
            "completion_rate": " ( actual_amount ) / ( target_amount ) ",
            "gap": "( target_amount ) - ( actual_amount )",
            "delivery_actual": "( gross_delivery_amount ) - ( return_amount )",
            "receipt_actual": "( receipt_amount ) - ( refund_amount )",
        }
        _assert_formula_summary(self, mutated)

    def test_summary_999_and_formula_mutations_fail_targeted_maintenance_check(self) -> None:
        mutants = {
            "summary_999": {**EXPECTED_FORMULAS, "completion_rate": "999"},
            "reversed_ratio": {
                **EXPECTED_FORMULAS,
                "completion_rate": "target_amount / actual_amount",
            },
            "scaled_ratio": {
                **EXPECTED_FORMULAS,
                "completion_rate": "actual_amount / target_amount * 100",
            },
            "reversed_gap": {
                **EXPECTED_FORMULAS,
                "gap": "actual_amount - target_amount",
            },
            "wrong_delivery_factor": {
                **EXPECTED_FORMULAS,
                "delivery_actual": "gross_delivery_amount + return_amount",
            },
        }
        for label, formula in mutants.items():
            with self.subTest(mutant=label):
                mutated = deepcopy(self.semantics)
                mutated["formula"] = formula
                with self.assertRaises(AssertionError):
                    _assert_formula_summary(self, mutated)

    def test_delivery_builder_sql_matches_independent_hand_oracle(self) -> None:
        connection = _sqlite_connection()
        try:
            _insert_delivery(
                connection,
                target_rows=[("target-1", "2026-08", 100, "n")],
                gross_rows=[("gross-1", "2026-08-10", 150, 6, "n")],
                return_rows=[("return-1", "2026-08-11", 20, 4, 1, 1, "n")],
            )
            row = _run_builder_query(
                connection,
                metric="delivery_target_completion",
                observed_on=date(2026, 9, 20),
                time_range={"start": "2026-08-01", "end": "2026-09-01"},
            )
            oracle = _independent_oracle(
                target=Decimal("100"), actual=Decimal("150") - Decimal("20")
            )
            _assert_decimal(self, row["target_amount_rmb"], oracle["target"], label="delivery target")
            _assert_decimal(self, row["actual_amount_rmb"], oracle["actual"], label="delivery actual")
            _assert_decimal(self, row["gap_amount_rmb"], oracle["gap"], label="delivery gap")
            _assert_decimal(self, row["completion_rate"], oracle["completion"], label="delivery rate")
            self.assertEqual("set", row["target_data_state"])
            self.assertEqual("reported", row["actual_data_state"])
            self.assertEqual("completed", row["period_state"])
        finally:
            connection.close()

    def test_receipt_builder_sql_matches_independent_hand_oracle(self) -> None:
        connection = _sqlite_connection()
        try:
            _insert_receipt(
                connection,
                target_rows=[("target-1", "2026-08-01", 100)],
                receipt_rows=[("receipt-1", "2026-08-10", 150, "6")],
                refund_rows=[("refund-1", "2026-08-11", 20, "6")],
            )
            row = _run_builder_query(
                connection,
                metric="receipt_target_completion",
                observed_on=date(2026, 9, 20),
                time_range={"start": "2026-08-01", "end": "2026-09-01"},
            )
            oracle = _independent_oracle(
                target=Decimal("100"), actual=Decimal("150") - Decimal("20")
            )
            _assert_decimal(self, row["target_amount_rmb"], oracle["target"], label="receipt target")
            _assert_decimal(self, row["actual_amount_rmb"], oracle["actual"], label="receipt actual")
            _assert_decimal(self, row["gap_amount_rmb"], oracle["gap"], label="receipt gap")
            _assert_decimal(self, row["completion_rate"], oracle["completion"], label="receipt rate")
        finally:
            connection.close()

    def test_zero_missing_and_future_states_preserve_independent_boundaries(self) -> None:
        cases = (
            ("zero", [("target-1", "2026-08", 0, "n")], Decimal("0"), None, Decimal("-100"), "zero", "reported", "completed"),
            ("missing", [], None, None, None, "missing", "reported", "completed"),
            ("future", [], None, None, None, "not_set_for_future", "not_started", "not_started"),
        )
        for label, target_rows, target, actual, gap, target_state, actual_state, period_state in cases:
            with self.subTest(case=label):
                connection = _sqlite_connection()
                try:
                    _insert_delivery(
                        connection,
                        target_rows=target_rows,
                        gross_rows=[("gross-1", "2026-08-10", 100, 6, "n")] if label != "future" else [],
                        return_rows=[],
                    )
                    range_value = (
                        {"start": "2099-01-01", "end": "2099-02-01"}
                        if label == "future"
                        else {"start": "2026-08-01", "end": "2026-09-01"}
                    )
                    row = _run_builder_query(
                        connection,
                        metric="delivery_target_completion",
                        observed_on=date(2026, 9, 20),
                        time_range=range_value,
                    )
                    _assert_decimal(self, row["completion_rate"], None, label=f"{label} completion")
                    _assert_decimal(self, row["gap_amount_rmb"], gap, label=f"{label} gap")
                    self.assertEqual(target_state, row["target_data_state"])
                    self.assertEqual(actual_state, row["actual_data_state"])
                    self.assertEqual(period_state, row["period_state"])
                finally:
                    connection.close()

    def test_null_target_or_actual_is_incomplete_without_zero_fallback(self) -> None:
        for null_side in ("target", "actual"):
            with self.subTest(null_side=null_side):
                connection = _sqlite_connection()
                try:
                    _insert_delivery(
                        connection,
                        target_rows=[
                            (
                                "target-null",
                                "2026-08",
                                None if null_side == "target" else 100,
                                "n",
                            )
                        ],
                        gross_rows=[
                            (
                                "gross-null",
                                "2026-08-10",
                                None if null_side == "actual" else 100,
                                6,
                                "n",
                            )
                        ],
                        return_rows=[],
                    )
                    row = _run_builder_query(
                        connection,
                        metric="delivery_target_completion",
                        observed_on=date(2026, 9, 20),
                        time_range={"start": "2026-08-01", "end": "2026-09-01"},
                    )
                    _assert_decimal(self, row["completion_rate"], None, label=f"{null_side} rate")
                    _assert_decimal(self, row["gap_amount_rmb"], None, label=f"{null_side} gap")
                    if null_side == "target":
                        self.assertEqual("incomplete", row["target_data_state"])
                    else:
                        self.assertEqual("incomplete", row["actual_data_state"])
                finally:
                    connection.close()

    def test_unfinished_period_caps_actual_without_clamping_negative_or_over_target_values(self) -> None:
        connection = _sqlite_connection()
        try:
            _insert_delivery(
                connection,
                target_rows=[("target-1", "2026-08", 100, "n")],
                gross_rows=[
                    ("gross-before", "2026-08-10", 150, 6, "n"),
                    ("gross-after", "2026-08-20", 1000, 6, "n"),
                ],
                return_rows=[("return-before", "2026-08-11", 20, 4, 1, 1, "n")],
            )
            row = _run_builder_query(
                connection,
                metric="delivery_target_completion",
                observed_on=date(2026, 8, 18),
                time_range={"start": "2026-08-01", "end": "2026-09-01"},
            )
            oracle = _independent_oracle(target=Decimal("100"), actual=Decimal("130"))
            _assert_decimal(self, row["actual_amount_rmb"], oracle["actual"], label="unfinished actual")
            _assert_decimal(self, row["completion_rate"], oracle["completion"], label="unfinished rate")
            self.assertEqual("in_progress", row["period_state"])

            connection.execute("DELETE FROM `vk_dwd`.`delivery_target_detail_dwd`")
            connection.execute("DELETE FROM `vk_dwd`.`sale_bill_goods_detail_dwd`")
            connection.execute("DELETE FROM `vk_dwd`.`delivery_return_detail_dwd`")
            _insert_delivery(
                connection,
                target_rows=[("target-negative", "2026-08", 100, "n")],
                gross_rows=[("gross-negative", "2026-08-10", -10, 6, "n")],
                return_rows=[("return-negative", "2026-08-11", 5, 4, 1, 1, "n")],
            )
            row = _run_builder_query(
                connection,
                metric="delivery_target_completion",
                observed_on=date(2026, 9, 20),
                time_range={"start": "2026-08-01", "end": "2026-09-01"},
            )
            oracle = _independent_oracle(target=Decimal("100"), actual=Decimal("-15"))
            _assert_decimal(self, row["actual_amount_rmb"], oracle["actual"], label="negative actual")
            _assert_decimal(self, row["completion_rate"], oracle["completion"], label="negative rate")
            _assert_decimal(self, row["gap_amount_rmb"], oracle["gap"], label="negative gap")
        finally:
            connection.close()

    def test_sql_formula_mutations_are_detected_by_independent_oracle(self) -> None:
        connection = _sqlite_connection()
        try:
            _insert_delivery(
                connection,
                target_rows=[("target-mutation", "2026-08", 100, "n")],
                gross_rows=[("gross-mutation", "2026-08-10", 150, 6, "n")],
                return_rows=[("return-mutation", "2026-08-11", 20, 4, 1, 1, "n")],
            )
            sql, params = _build_target_sql(
                metric="delivery_target_completion",
                observed_on=date(2026, 9, 20),
                time_range={"start": "2026-08-01", "end": "2026-09-01"},
            )
            expected = _independent_oracle(target=Decimal("100"), actual=Decimal("130"))
            correct = connection.execute(_sqlite_sql(sql), params).fetchone()
            _assert_decimal(self, correct["completion_rate"], expected["completion"], label="generated SQL rate")
            _assert_decimal(self, correct["gap_amount_rmb"], expected["gap"], label="generated SQL gap")

            mutants = {
                "reverse_ratio": {"completion": Decimal("100") / Decimal("130"), "gap": Decimal("-30")},
                "scale_ratio": {"completion": Decimal("130"), "gap": Decimal("-30")},
                "reverse_gap": {"completion": Decimal("1.3"), "gap": Decimal("30")},
                "add_return": {"completion": Decimal("1.7"), "gap": Decimal("-70")},
            }
            for mutation, mutant_expected in mutants.items():
                with self.subTest(mutation=mutation):
                    mutated_sql, replacements = _mutate_generated_delivery_sql(sql, mutation)
                    self.assertGreater(replacements, 0, mutation)
                    mutated = connection.execute(_sqlite_sql(mutated_sql), params).fetchone()
                    _assert_decimal(
                        self,
                        mutated["completion_rate"],
                        mutant_expected["completion"],
                        label=f"{mutation} rate",
                    )
                    _assert_decimal(
                        self,
                        mutated["gap_amount_rmb"],
                        mutant_expected["gap"],
                        label=f"{mutation} gap",
                    )
                    # A rate-only mutation preserves the gap, and vice versa.
                    # The same two-fact oracle must reject each mutant without
                    # requiring unrelated, correct facts to become incorrect.
                    with self.assertRaises(AssertionError, msg=mutation):
                        _assert_decimal(
                            self, mutated["completion_rate"], expected["completion"],
                            label=f"{mutation} original rate oracle",
                        )
                        _assert_decimal(
                            self, mutated["gap_amount_rmb"], expected["gap"],
                            label=f"{mutation} original gap oracle",
                        )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
