"""External DB-backed currency contract regressions.

The file is kept outside the candidate worktree.  The isolated runner must
set ``CURRENCY_TEST_SOURCE_ROOT`` to the extracted/merged profile source and
run this module with its existing clean environment, guard, and SQLite test
seam.  With no environment override, ``parents[1]`` is treated as the
profile root; this deliberately avoids falling back to a developer checkout.

The tests do not open a real database, load credentials, register a plugin,
start a service, call a model, or send anything.  The numeric case uses the
existing ``RemainingCaseTests`` in the selected source tree, whose connection
and network tripwires are already synthetic and offline.
"""

from __future__ import annotations

from datetime import date
import importlib
import os
from pathlib import Path
import sys
import types
import unittest


DEFAULT_SOURCE = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("CURRENCY_TEST_SOURCE_ROOT", str(DEFAULT_SOURCE))).resolve()
PLUGIN_ROOT = SOURCE / "plugins" / "datasage-query"
TESTS_ROOT = SOURCE / "tests"
if not PLUGIN_ROOT.is_dir():
    raise RuntimeError(
        "CURRENCY_TEST_SOURCE_ROOT must point at a profile root containing "
        "plugins/datasage-query; the external test never guesses a checkout"
    )

PACKAGE = "currency_db_verified_contracts_tests"
_package = types.ModuleType(PACKAGE)
_package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, _package)
tools = importlib.import_module(f"{PACKAGE}.tools")
contracts = importlib.import_module(f"{PACKAGE}.contracts")
currency_basis = importlib.import_module(f"{PACKAGE}.currency_basis")
errors = importlib.import_module(f"{PACKAGE}.query_errors")

OBSERVED_ON = date(2026, 9, 26)

# This is the complete 13-row planned-variant register from the independent
# DB verification inventory: target 8, inventory 2, formal DSO 1, pattern 2.
# Keep the list literal so a missing new variant fails loudly instead of being
# hidden by a dynamic loop or a skip.
REQUIRED_VARIANTS = (
    ("target", "delivery_allocated_target_amount", "delivery_allocated_target_amount_original"),
    ("target", "receipt_allocated_target_amount", "receipt_allocated_target_amount_original"),
    ("target", "delivery_target_amount", "delivery_target_amount_original"),
    ("target", "receipt_target_amount", "receipt_target_amount_original"),
    ("target", "allocated_net_delivery_amount", "allocated_net_delivery_amount_original"),
    ("target", "allocated_net_receipt_amount", "allocated_net_receipt_amount_original"),
    ("target", "delivery_target_completion", "delivery_target_completion_original"),
    ("target", "receipt_target_completion", "receipt_target_completion_original"),
    ("inventory", "turnover_net_delivery_rmb", "turnover_net_delivery_original"),
    ("inventory", "inventory_turnover_days", "inventory_turnover_days_original"),
    ("receivable", "formal_receivable_turnover_days", "formal_receivable_turnover_days_original"),
    ("pattern_matching", "linked_delivery_amount_rmb", "linked_delivery_amount"),
    ("pattern_matching", "person_attributed_delivery_amount_rmb", "person_attributed_delivery_amount"),
)


def _semantics(domain: str):
    return contracts.execution_contracts(domain)[1]


def _compile(request: dict):
    """Compile through the selected source's DB-free validator/builder seam."""
    prepared, datasets, semantics = tools._validate_request_plan_without_entities(
        request, observed_on=OBSERVED_ON
    )
    sql, params, scope = tools._build_metric_query(
        prepared, datasets, semantics, 100, observed_on=OBSERVED_ON
    )
    return prepared, sql, params, scope


def _public_tests_module():
    """Load the selected source's existing offline SQLite harness on demand."""
    if str(TESTS_ROOT) not in sys.path:
        sys.path.insert(0, str(TESTS_ROOT))
    return importlib.import_module("test_remediation_remaining_cases")


class CurrencyDbVerifiedContractTests(unittest.TestCase):
    @staticmethod
    def _ensure_column(harness, table, column, sql_type):
        columns = {
            row[1]
            for row in harness.conn.execute(f"PRAGMA vk_dwd.table_info({table})")
        }
        if column not in columns:
            harness.conn.execute(
                f"ALTER TABLE vk_dwd.{table} ADD COLUMN {column} {sql_type}"
            )

    def _new_sqlite_harness(self):
        public = _public_tests_module()
        harness = public.RemainingCaseTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        for table, column, sql_type in (
            ("receive_bill_detail_dwd", "detail_receive_amount", "REAL"),
            ("receive_bill_detail_dwd", "currency_no", "TEXT"),
            ("receive_return_bill_detail_dwd", "detail_return_amount", "REAL"),
            ("receive_return_bill_detail_dwd", "currency_no", "TEXT"),
        ):
            self._ensure_column(harness, table, column, sql_type)
        return public, harness

    def _new_pattern_harness(self):
        if str(TESTS_ROOT) not in sys.path:
            sys.path.insert(0, str(TESTS_ROOT))
        pattern_tests = importlib.import_module("test_pattern_matching")
        harness = pattern_tests.PatternTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return pattern_tests, harness

    def _new_target_harness(self):
        public, harness = self._new_sqlite_harness()
        # The base fixture intentionally contains only the old target tables.
        # Add the proposed original fields and the split-coverage source tables
        # in this private SQLite connection; production schemas are untouched.
        for table, column, sql_type in (
            ("receive_bill_detail_dwd", "detail_id", "TEXT"),
            ("receive_bill_detail_dwd", "is_inner_cus", "TEXT"),
            ("receive_bill_detail_dwd", "currency_no", "TEXT"),
            ("receive_bill_detail_dwd", "final_sales_id", "TEXT"),
            ("receive_bill_detail_dwd", "customer_id", "TEXT"),
            ("receive_bill_detail_dwd", "customer_dept", "TEXT"),
            ("receive_bill_detail_dwd", "org_name", "TEXT"),
            ("receive_return_bill_detail_dwd", "detail_id", "TEXT"),
            ("receive_return_bill_detail_dwd", "is_inner_cus", "TEXT"),
            ("receive_return_bill_detail_dwd", "currency_no", "TEXT"),
            ("receive_return_bill_detail_dwd", "final_sales_id", "TEXT"),
            ("receive_return_bill_detail_dwd", "customer_id", "TEXT"),
            ("receive_return_bill_detail_dwd", "customer_dept", "TEXT"),
            ("receive_return_bill_detail_dwd", "org_name", "TEXT"),
        ):
            self._ensure_column(harness, table, column, sql_type)
        harness.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_target_split_dwd(
                target_id TEXT, plan_receive_time TEXT,
                plan_receive_amount REAL, plan_receive_rmb REAL,
                currency_no TEXT, sales_id TEXT, sales_name TEXT,
                customer_id TEXT, customer_name TEXT, customer_dept TEXT,
                org_name TEXT
            );
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_bill_split_dwd(
                detail_id TEXT, bill_time TEXT,
                detail_receive_amount REAL, detail_receive_rmb REAL,
                bill_status TEXT, currency_no TEXT, sales_id TEXT,
                sales_name TEXT, customer_id TEXT, customer_name TEXT,
                customer_dept TEXT, org_name TEXT
            );
            CREATE TABLE IF NOT EXISTS vk_dwd.receive_return_bill_split_dwd(
                detail_id TEXT, bill_time TEXT,
                detail_return_amount REAL, detail_return_rmb REAL,
                bill_status TEXT, currency_no TEXT, sales_id TEXT,
                sales_name TEXT, customer_id TEXT, customer_name TEXT,
                customer_dept TEXT, org_name TEXT
            );
            """
        )
        return public, harness

    @staticmethod
    def _clear_target_tables(harness):
        for table in (
            "receive_target_split_dwd",
            "receive_bill_split_dwd",
            "receive_return_bill_split_dwd",
            "receive_bill_detail_dwd",
            "receive_return_bill_detail_dwd",
        ):
            harness.conn.execute(f"DELETE FROM vk_dwd.{table}")

    @staticmethod
    def _target_query(public, harness, *, basis, request_id):
        request = public.metric(
            "receipt_target_completion",
            "target",
            request_id=request_id,
            month="2026-08",
            currency_basis=basis,
            attribution_mode="salesperson_allocation",
            dimensions=["currency"],
            metric_filters={},
        )
        payload = harness.query(request)
        return payload, harness.result(payload, request_id)

    @staticmethod
    def _scope_state(row):
        states = row.get("states") or {}
        value = states.get("source_scope_state")
        if value is None:
            raise AssertionError(f"source_scope_state was not published in row.states: {row}")
        return value

    @staticmethod
    def _seed_target_case(harness, *, internal_gap):
        CurrencyDbVerifiedContractTests._clear_target_tables(harness)
        if not internal_gap:
            harness.insert(
                "vk_dwd.receive_target_split_dwd",
                "target_id,plan_receive_time,plan_receive_amount,plan_receive_rmb,currency_no",
                [("t-ext", "2026-08-01", 100, 700, "VND")],
            )
            harness.insert(
                "vk_dwd.receive_bill_detail_dwd",
                "detail_id,detail_receive_amount,detail_receive_rmb,bill_status,bill_time,is_inner_cus,currency_no,final_sales_id",
                [("b-ext", 50, 350, "C", "2026-08-15", "n", "VND", "S1")],
            )
            harness.insert(
                "vk_dwd.receive_bill_split_dwd",
                "detail_id,bill_time,detail_receive_amount,detail_receive_rmb,bill_status,currency_no",
                [("b-ext", "2026-08-15", 50, 350, "C", "VND")],
            )
            return

        # The split ledger observes 2; a separate internal base row of 5 is
        # deliberately absent from that ledger.  The result may retain the
        # split target/actual facts, but the completion comparison is not
        # authorized for this incomplete source range.
        harness.insert(
            "vk_dwd.receive_target_split_dwd",
            "target_id,plan_receive_time,plan_receive_amount,plan_receive_rmb,currency_no",
            [("t-int", "2026-08-01", 100, 700, "VND")],
        )
        harness.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_id,detail_receive_amount,detail_receive_rmb,bill_status,bill_time,is_inner_cus,currency_no,final_sales_id",
            [
                ("b-int-covered", 2, 14, "C", "2026-08-15", "y", "VND", "S1"),
                ("b-int-missing", 5, 35, "C", "2026-08-15", "y", "VND", "S1"),
            ],
        )
        harness.insert(
            "vk_dwd.receive_bill_split_dwd",
            "detail_id,bill_time,detail_receive_amount,detail_receive_rmb,bill_status,currency_no",
            [("b-int-covered", "2026-08-15", 2, 14, "C", "VND")],
        )

    @staticmethod
    def _seed_receipts(harness, rows):
        harness.conn.execute("DELETE FROM vk_dwd.receive_bill_detail_dwd")
        harness.conn.execute("DELETE FROM vk_dwd.receive_return_bill_detail_dwd")
        harness.insert(
            "vk_dwd.receive_bill_detail_dwd",
            "detail_receive_amount,detail_deal_amount,exchange_rate,currency_no,bill_status,bill_time",
            [
                (amount, amount, rate, currency, "C", "2026-08-15")
                for amount, rate, currency in rows
            ],
        )

    @staticmethod
    def _metric_value(public, result):
        facts = public.facts(result)
        if not facts:
            raise AssertionError(f"numeric fixture returned no fact rows: {result}")
        return facts[0].get("metric_value")

    def test_01_all_thirteen_planned_variants_are_registered_with_units(self):
        self.assertEqual(13, len(REQUIRED_VARIANTS))
        seen = set()
        for domain, rmb, original in REQUIRED_VARIANTS:
            with self.subTest(domain=domain, rmb=rmb, original=original):
                pair = (domain, rmb, original)
                self.assertNotIn(pair, seen)
                seen.add(pair)
                semantics = _semantics(domain)
                self.assertIn(
                    {"rmb": rmb, "original": original},
                    semantics.get("currency_basis_pairs", []),
                )
                metrics = semantics.get("metrics") or {}
                for code in (rmb, original):
                    definition = metrics.get(code)
                    self.assertIsInstance(definition, dict, code)
                    unit = definition.get("unit") or definition.get("unit_policy")
                    self.assertIsInstance(unit, str, code)
                    self.assertTrue(unit.strip(), code)
                original_policy = metrics[original].get("currency_policy") or {}
                original_unit = str(
                    metrics[original].get("unit")
                    or metrics[original].get("unit_policy")
                    or ""
                )
                self.assertTrue(
                    original_policy.get("mode") == "original_currency"
                    or "原币" in original_unit,
                    f"{domain}/{original} lacks an explicit original-currency contract",
                )

    def test_02_auto_with_vnd_filter_preflights_rmb_then_resolves_original(self):
        request = {
            "request_id": "auto-vnd",
            "domain": "receipt",
            "mode": "metric",
            "metric": "actual_receipt_amount",
            "currency_basis": "auto",
            "dimensions": [],
            "metric_filters": {"currency": "VND"},
            "calendar_month": "2026-08",
            "limit": 100,
        }
        prepared, _datasets, _semantics = tools._validate_request_plan_without_entities(
            request, observed_on=OBSERVED_ON
        )
        # Auto starts at the RMB counterpart so the pre-probe request is
        # contract-valid even before scope proves whether one currency exists.
        self.assertEqual("actual_receipt_amount", prepared["metric"])
        plan = prepared["_currency_basis_plan"]
        self.assertEqual("auto", plan["requested_basis"])
        self.assertEqual("rmb", plan["resolved_basis"])
        self.assertTrue(plan["requires_probe"])
        self.assertEqual("actual_receipt_amount_original", plan["counterparts"]["original"])
        self.assertEqual("VND", prepared["metric_filters"]["currency"])

        resolved = currency_basis.resolve_probe(
            prepared,
            [{"currency_count": 1, "single_currency": "VND", "unknown_currency_groups": 0}],
            False,
        )
        self.assertEqual("actual_receipt_amount_original", resolved["metric"])
        self.assertFalse(resolved["_currency_basis_plan"]["requires_probe"])
        self.assertEqual("VND", resolved["metric_filters"]["currency"])

    def test_03_auto_selection_matches_hand_calculated_sqlite_amounts(self):
        public, harness = self._new_sqlite_harness()

        # Cross-currency auto must use the governed RMB fields: 10*7 + 20*8.
        self._seed_receipts(harness, [(10, 7, "USD"), (20, 8, "EUR")])
        mixed_request = public.metric(
            "actual_receipt_amount", "receipt", month="2026-08",
            currency_basis="auto", request_id="mixed",
        )
        mixed_payload = harness.query(mixed_request)
        mixed = harness.result(mixed_payload, "mixed")
        self.assertEqual(230, self._metric_value(public, mixed))
        self.assertIn(
            "人民币",
            next(
                item["unit"]
                for item in mixed_payload["metric_contexts"]
                if item["business_metric_ref"] == mixed["business_metric_ref"]
            ),
        )

        # One currency must preserve original units: 10 + 20, without FX.
        self._seed_receipts(harness, [(10, 7, "VND"), (20, 7, "VND")])
        single_request = public.metric(
            "actual_receipt_amount", "receipt", month="2026-08",
            currency_basis="auto", request_id="single",
        )
        single_payload = harness.query(single_request)
        single = harness.result(single_payload, "single")
        self.assertEqual(30, self._metric_value(public, single))
        self.assertIn(
            "原币",
            next(
                item["unit"]
                for item in single_payload["metric_contexts"]
                if item["business_metric_ref"] == single["business_metric_ref"]
            ),
        )
        self.assertGreaterEqual(len(harness.sql_trace), 2)

    def test_04_probe_scope_is_complete_and_has_no_final_limit(self):
        request = {
            "request_id": "probe-shape",
            "domain": "receipt",
            "mode": "metric",
            "metric": "actual_receipt_amount",
            "currency_basis": "auto",
            "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "limit": 100,
        }
        prepared, datasets, semantics = tools._validate_request_plan_without_entities(
            request, observed_on=OBSERVED_ON
        )
        probe_sql, probe_params = currency_basis.build_probe(
            prepared, datasets, semantics, tools._build_metric_query,
            observed_on=OBSERVED_ON,
        )
        self.assertIn("currency_count", probe_sql)
        self.assertIn("__matched_row_count", probe_sql)
        self.assertNotIn("LIMIT %s", probe_sql.upper())
        self.assertIn("2026-08-01", [str(value) for value in probe_params])
        self.assertIn("2026-09-01", [str(value) for value in probe_params])

    def test_05_unknown_currency_fails_closed_without_comparable_amount(self):
        request = {
            "request_id": "unknown-currency",
            "domain": "receipt",
            "mode": "metric",
            "metric": "actual_receipt_amount",
            "currency_basis": "auto",
            "dimensions": [],
            "metric_filters": {},
            "calendar_month": "2026-08",
            "limit": 100,
        }
        prepared, _datasets, _semantics = tools._validate_request_plan_without_entities(
            request, observed_on=OBSERVED_ON
        )
        with self.assertRaises(errors.QueryFailure) as caught:
            currency_basis.resolve_probe(
                prepared,
                [{"currency_count": 1, "single_currency": "VND", "unknown_currency_groups": 1}],
                False,
            )
        self.assertEqual("CURRENCY_SCOPE_UNKNOWN", caught.exception.code)

    def test_06_explicit_original_grouping_keeps_currency_dimension_and_unit(self):
        prepared, sql, _params, scope = _compile({
            "request_id": "grouped-original",
            "domain": "receipt",
            "mode": "metric",
            "metric": "actual_receipt_amount_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "calendar_month": "2026-08",
            "limit": 100,
        })
        self.assertEqual("actual_receipt_amount_original", prepared["metric"])
        self.assertIn("currency_no", sql)
        self.assertIn("currency_no", scope.get("dimension_outputs", []))
        self.assertIn("原币", _semantics("receipt")["metrics"][prepared["metric"]]["unit"])

    def test_07_inventory_original_scope_exposes_unknown_currency_counters(self):
        _prepared, sql, _params, _scope = _compile({
            "request_id": "inventory-original",
            "domain": "inventory",
            "mode": "metric",
            "metric": "current_inventory_amount_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "inventory_scope": "total",
            "limit": 100,
        })
        self.assertIn("currency_missing_rows", sql)
        self.assertIn("unclassified_source_row_count", sql)
        self.assertIn("currency_no", sql)

    def test_08_inventory_turnover_original_keeps_global_rmb_readiness(self):
        semantics = _semantics("inventory")
        expected_pair = {
            "rmb": "inventory_turnover_days",
            "original": "inventory_turnover_days_original",
        }
        self.assertIn(expected_pair, semantics.get("currency_basis_pairs", []))
        self.assertIn("inventory_turnover_days_original", semantics.get("metrics", {}))
        _prepared, sql, _params, _scope = _compile({
            "request_id": "turnover-original",
            "domain": "inventory",
            "mode": "metric",
            "metric": "inventory_turnover_days_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "time_range": {"start": "2025-09-01", "end": "2026-09-01"},
            "limit": 100,
        })
        readiness = sql[sql.index("accounted_months"):sql.index("monthly_data")]
        self.assertIn("cost_amount_rmb", readiness)
        self.assertNotIn("currency_no", readiness)
        self.assertIn("cost_amount", sql)
        self.assertIn("pur_delivery_amount", sql)

    def test_09_inventory_turnover_preserves_signed_and_zero_denominator_states(self):
        semantics = _semantics("inventory")
        self.assertIn(
            {"rmb": "inventory_turnover_days", "original": "inventory_turnover_days_original"},
            semantics.get("currency_basis_pairs", []),
        )
        _prepared, sql, _params, _scope = _compile({
            "request_id": "turnover-signed",
            "domain": "inventory",
            "mode": "metric",
            "metric": "inventory_turnover_days_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "time_range": {"start": "2025-09-01", "end": "2026-09-01"},
            "limit": 100,
        })
        upper = sql.upper()
        self.assertIn("= 0", upper)
        self.assertIn("THEN NULL", upper)
        self.assertNotIn("ABS(", upper)

    def test_10_formal_dso_original_uses_gross_delivery_and_currency_key(self):
        semantics = _semantics("receivable")
        self.assertIn(
            {"rmb": "formal_receivable_turnover_days", "original": "formal_receivable_turnover_days_original"},
            semantics.get("currency_basis_pairs", []),
        )
        self.assertIn("formal_receivable_turnover_days_original", semantics.get("metrics", {}))
        _prepared, sql, _params, _scope = _compile({
            "request_id": "dso-original",
            "domain": "receivable",
            "mode": "metric",
            "metric": "formal_receivable_turnover_days_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "time_range": {"start": "2025-09-01", "end": "2026-09-01"},
            "limit": 100,
        })
        self.assertIn("debt_amount", sql)
        self.assertIn("delivery_amount", sql)
        self.assertIn("currency_no", sql)
        self.assertNotIn("pur_delivery", sql)
        self.assertIn("> 0", sql)

    def test_11_formal_dso_original_tracks_missing_zero_negative_protection(self):
        semantics = _semantics("receivable")
        self.assertIn(
            {"rmb": "formal_receivable_turnover_days", "original": "formal_receivable_turnover_days_original"},
            semantics.get("currency_basis_pairs", []),
        )
        _prepared, sql, _params, _scope = _compile({
            "request_id": "dso-states",
            "domain": "receivable",
            "mode": "metric",
            "metric": "formal_receivable_turnover_days_original",
            "dimensions": ["currency"],
            "metric_filters": {},
            "time_range": {"start": "2025-09-01", "end": "2026-09-01"},
            "limit": 100,
        })
        self.assertIn("debt_null_count", sql)
        self.assertIn("delivery_null_count", sql)
        self.assertIn("missing_value_count", sql)
        self.assertIn("metric_data_state", sql)
        self.assertNotIn("ABS(", sql.upper())

    def test_12_target_original_variant_compiles_with_currency_scope(self):
        semantics = _semantics("target")
        self.assertIn(
            {"rmb": "delivery_target_amount", "original": "delivery_target_amount_original"},
            semantics.get("currency_basis_pairs", []),
        )
        prepared, sql, _params, scope = _compile({
            "request_id": "target-original",
            "domain": "target",
            "mode": "metric",
            "metric": "delivery_target_amount_original",
            "attribution_mode": "transaction_detail",
            "dimensions": ["currency"],
            "metric_filters": {},
            "calendar_month": "2026-08",
            "limit": 100,
        })
        self.assertEqual("delivery_target_amount_original", prepared["metric"])
        self.assertIn("currency_no", sql)
        self.assertIn("currency_no", scope.get("dimension_outputs", []))
        self.assertIn("detail_target_amount", sql)

    def test_13_receipt_target_split_scope_numeric_rmb_and_original(self):
        # External scope: target 100 and split actual 50 are comparable in
        # both bases.  The RMB columns use a non-unit FX relation (7x).
        for basis, target_field, actual_field, gap_field, expected in (
            ("rmb", "target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb", (700, 350, 350)),
            ("original", "target_amount_original", "actual_amount_original", "gap_amount_original", (100, 50, 50)),
        ):
            with self.subTest(scope="external", basis=basis):
                public, harness = self._new_target_harness()
                self._seed_target_case(harness, internal_gap=False)
                _payload, result = self._target_query(
                    public, harness, basis=basis, request_id=f"external-{basis}"
                )
                row = result["rows"][0]
                facts = row["facts"]
                self.assertEqual(expected[0], facts[target_field])
                self.assertEqual(expected[1], facts[actual_field])
                self.assertEqual(expected[2], facts[gap_field])
                self.assertEqual(0.5, facts["completion_rate"])

        # Internal source coverage: split observations remain visible (target
        # 100 / split actual 2), while the unrepresented base row of 5 makes
        # the comparison incomplete.  The same guard applies to RMB/original.
        for basis, target_field, actual_field in (
            ("rmb", "target_amount_rmb", "actual_amount_rmb"),
            ("original", "target_amount_original", "actual_amount_original"),
        ):
            with self.subTest(scope="internal_gap", basis=basis):
                public, harness = self._new_target_harness()
                self._seed_target_case(harness, internal_gap=True)
                _payload, result = self._target_query(
                    public, harness, basis=basis, request_id=f"internal-{basis}"
                )
                row = result["rows"][0]
                facts = row["facts"]
                expected_target, expected_actual = (700, 14) if basis == "rmb" else (100, 2)
                self.assertEqual(expected_target, facts[target_field])
                self.assertEqual(expected_actual, facts[actual_field])
                self.assertIsNone(facts["completion_rate"])
                self.assertIsNone(facts.get("gap_amount_rmb"))
                self.assertIsNone(facts.get("gap_amount_original"))
                self.assertEqual("source_range_incomplete", self._scope_state(row))

    def test_14_pattern_rmb_mixed_currency_distinct_details_are_hand_summed(self):
        _pattern_tests, harness = self._new_pattern_harness()

        # Equal original amounts with different FX produce distinct stored
        # RMB values.  Detail 1 is linked by two task rows but must be counted
        # once by the stable-detail RMB metric: 650 + 870 = 1520.
        harness.sale(1, amount=100, currency="USD", amount_rmb=650)
        harness.sale(2, amount=100, currency="EUR", amount_rmb=870)
        harness.row(task=1, execute=11, detail=1, amount=100, currency="USD")
        harness.row(task=2, execute=12, detail=1, amount=100, currency="USD")
        harness.row(task=3, execute=13, detail=2, amount=100, currency="EUR")

        result = harness.result(harness.run_pattern("linked_delivery_amount_rmb"))
        facts = result["rows"][0]["facts"]
        self.assertEqual(1520, facts["metric_value"])
        self.assertEqual(1520, facts["known_subset_value"])
        self.assertEqual(0, facts["missing_value_count"])
        self.assertNotEqual(650, 870)

    def test_15_pattern_rmb_missing_zero_and_negative_are_distinct(self):
        # ``PatternTests.sale`` treats None as "use original".  Every case
        # therefore supplies an explicit RMB value; the missing case then
        # sets the stored field to SQL NULL deliberately.
        cases = (
            ("missing", 50, "JPY", 123, None, 0, 1),
            ("zero", 0, "USD", 0, 0, 0, 0),
            ("negative", -20, "EUR", -130, -130, -130, 0),
        )
        for label, original, currency, stored_rmb, expected, expected_known, missing in cases:
            with self.subTest(case=label):
                _pattern_tests, harness = self._new_pattern_harness()
                harness.sale(1, amount=original, currency=currency, amount_rmb=stored_rmb)
                if label == "missing":
                    harness.conn.execute(
                        "UPDATE vk_dwd.sale_bill_goods_detail_dwd "
                        "SET delivery_amount_rmb = NULL WHERE goods_detail_id = 1"
                    )
                harness.row(detail=1, amount=original, currency=currency)
                result = harness.result(harness.run_pattern("linked_delivery_amount_rmb"))
                facts = result["rows"][0]["facts"]
                self.assertEqual(expected, facts["metric_value"])
                self.assertEqual(expected_known, facts["known_subset_value"])
                self.assertEqual(missing, facts["missing_value_count"])


if __name__ == "__main__":
    unittest.main()
