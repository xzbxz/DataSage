from __future__ import annotations

import copy
import importlib
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft7Validator


PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
PACKAGE = "runtime_hardening_test_package"
os.environ["HERMES_HOME"] = str(REPO_ROOT)


def _load_package_module(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


schemas = _load_package_module("schemas")
tools = _load_package_module("tools")
runtime_health = _load_package_module("runtime_health")


def _delivery_request(request_id: str = "q1") -> dict:
    return {
        "request_id": request_id,
        "domain": "delivery",
        "mode": "metric",
        "purpose": "runtime hardening test",
        "metric": "delivery_amount",
        "time_range": {"start": "2026-07-01", "end": "2026-08-01"},
    }


class ModelVisibleIdentityBoundaryTests(unittest.TestCase):
    def test_datasage_skills_do_not_teach_readiness_bypass(self) -> None:
        skills_root = REPO_ROOT / "skills" / "datasage"
        markdown_files = sorted(
            path
            for path in skills_root.rglob("*.md")
            if path.name == "SKILL.md" or "references" in path.parts
        )
        self.assertTrue(markdown_files)
        model_visible_text = "\n".join(
            path.read_text(encoding="utf-8") for path in markdown_files
        ).lower()

        prohibited_instructions = (
            "readiness-guard bypass",
            "only the runtime readiness gate",
            "call `datasage_query` directly",
            "tools.datasage_query directly",
            "tools.datasage_query` directly",
            "entry directly via package injection",
            "package-injection probe harness",
            "package injection, bypassing",
            "works even when the identity gate blocks",
            "same package-injection pattern",
            "mirror that in ad-hoc verification scripts",
        )
        for instruction in prohibited_instructions:
            self.assertNotIn(instruction, model_visible_text)

        self.assertFalse(
            (
                skills_root
                / "datasage-query-patterns"
                / "scripts"
                / "live_accuracy_probe.py"
            ).exists()
        )


class AdaptiveEntityResolutionContractTests(unittest.TestCase):
    def test_schema_uses_new_evidence_stop_condition_not_a_fixed_call_cap(self) -> None:
        description = schemas.DATASAGE_ENTITY_RESOLVE["description"].lower()

        self.assertNotIn("at most once", description)
        self.assertNotIn("call once", description)
        self.assertIn("do not repeat the same resolution with unchanged evidence", description)
        self.assertIn("material new information", description)
        self.assertIn("result could change the next action", description)


class CompletePartitionProofTests(unittest.TestCase):
    def test_truncated_complete_partition_uses_same_statement_proof(self) -> None:
        request = {
            **_delivery_request("customer-change"),
            "analysis_intent": "contribution_analysis",
            "evidence_role": "composition",
            "comparison": {"kind": "previous_period"},
            "complete_change_decomposition": {"dimension": "customer"},
        }
        calls: list[str] = []

        def execute(sql, _params, _limit, **_kwargs):
            calls.append(sql)
            if "customer_name" in sql:
                return (
                    [
                        {
                            "customer_no": f"C{index:03}",
                            "customer_name": f"Customer {index:03}",
                            "metric_value": 2,
                            "comparison_value": 1,
                            "delta_value": 1,
                            "change_rate": 1,
                            "__matched_row_count": 1,
                            "__full_partition_metric_value": 202,
                            "__full_partition_comparison_value": 101,
                            "__full_partition_delta_value": 101,
                            "__full_partition_row_count": 101,
                        }
                        for index in range(100)
                    ],
                    True,
                )
            return (
                [
                    {
                        "metric_value": 202,
                        "comparison_value": 101,
                        "delta_value": 101,
                        "change_rate": 1,
                        "__matched_row_count": 101,
                    }
                ],
                False,
            )

        snapshot = mock.MagicMock()
        snapshot.marker = "snapshot_test_group"
        snapshot.execute.side_effect = execute
        snapshot.__enter__.return_value = snapshot
        snapshot.__exit__.return_value = None
        with mock.patch.object(
            tools, "_consistent_snapshot_executor", return_value=snapshot
        ):
            payload = json.loads(
                tools._datasage_query_with_slot({"requests": [request]})
            )

        partition = next(
            result
            for result in payload["results"]
            if result["request_id"] == "customer-change"
        )
        self.assertTrue(partition["truncated"])
        reconciliation = partition["change_reconciliation"]
        self.assertEqual(reconciliation["status"], "reconciled")
        self.assertEqual(
            reconciliation["proof_mode"], "same_statement_window_full_partition"
        )
        self.assertEqual(reconciliation["full_partition_row_count"], 101)
        self.assertEqual(reconciliation["returned_driver_row_count"], 100)
        self.assertEqual(reconciliation["returned_nonzero_driver_count"], 100)
        self.assertEqual(
            reconciliation["nonzero_driver_count_scope"],
            "returned_rows_only",
        )
        self.assertFalse(reconciliation["complete_population_claims_returned"])
        self.assertEqual(reconciliation["unreturned_current"], "2")
        self.assertEqual(reconciliation["unreturned_comparison"], "1")
        self.assertEqual(reconciliation["unreturned_delta"], "1")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sum("customer_name" in sql for sql in calls), 1)
        self.assertIn("COUNT(*) OVER ()", next(sql for sql in calls if "customer_name" in sql))
        self.assertTrue(
            all(
                "structural_contribution" in claim["allowed_relations"]
                for claim in partition["claim_ledger"]
            )
        )
        self.assertTrue(
            all(
                "change_driver" not in claim["allowed_relations"]
                and "causal_driver" not in claim["allowed_relations"]
                and claim["relation_semantics"]["structural_contribution"]
                == "structural_not_causal"
                for claim in partition["claim_ledger"]
            )
        )

    def test_full_partition_proof_is_embedded_in_the_bounded_statement(self) -> None:
        request = {
            **_delivery_request("partition"),
            "comparison": {"kind": "previous_period"},
            "dimensions": ["customer"],
            "decomposition_of_request_id": "overall",
        }
        datasets, semantics = tools._contracts("delivery")
        sql, params, scope = tools._build_metric_query(
            request, datasets, semantics, 100
        )
        self.assertIn("LIMIT %s", sql)
        self.assertIn("COUNT(*) OVER ()", sql)
        self.assertIn("__full_partition_delta_value", sql)
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(
            scope["embedded_complete_partition_proof"]["version"],
            "same-statement-window-partition-proof/v1",
        )
        self.assertNotIn("complete_partition_proof", scope)

    def test_missing_same_snapshot_proof_never_triggers_a_second_query(self) -> None:
        request = {
            **_delivery_request("customer-change"),
            "comparison": {"kind": "previous_period"},
            "complete_change_decomposition": {"dimension": "customer"},
        }
        calls: list[str] = []

        def execute(sql, _params, _limit, **_kwargs):
            calls.append(sql)
            if "customer_name" in sql:
                return (
                    [
                        {
                            "customer_name": f"Customer {index}",
                            "metric_value": 2,
                            "comparison_value": 1,
                            "delta_value": 1,
                            "change_rate": 1,
                            "__matched_row_count": 1,
                        }
                        for index in range(100)
                    ],
                    True,
                )
            return (
                [
                    {
                        "metric_value": 202,
                        "comparison_value": 101,
                        "delta_value": 101,
                        "change_rate": 1,
                        "__matched_row_count": 101,
                    }
                ],
                False,
            )

        snapshot = mock.MagicMock()
        snapshot.marker = "snapshot_test_group"
        snapshot.execute.side_effect = execute
        snapshot.__enter__.return_value = snapshot
        snapshot.__exit__.return_value = None
        with mock.patch.object(
            tools, "_consistent_snapshot_executor", return_value=snapshot
        ):
            payload = json.loads(
                tools._datasage_query_with_slot({"requests": [request]})
            )

        partition = next(
            result
            for result in payload["results"]
            if result["request_id"] == "customer-change"
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            partition["change_reconciliation"]["status"],
            "not_reconciled",
        )
        self.assertEqual(
            partition["change_reconciliation"]["reason_code"],
            "PARTITION_PROOF_UNAVAILABLE",
        )
        self.assertTrue(
            all(
                "structural_contribution" not in claim["allowed_relations"]
                for claim in partition["claim_ledger"]
            )
        )


class SharedSnapshotTransactionTests(unittest.TestCase):
    class _Connection:
        def __init__(
            self,
            *,
            fail_sql: str | None = None,
            fail_first_business: Exception | None = None,
        ) -> None:
            self.commands: list[tuple[str, tuple | None]] = []
            self.rows: list[dict] = []
            self.rollback_count = 0
            self.close_count = 0
            self.fail_sql = fail_sql
            self.fail_first_business = fail_first_business

        def cursor(self):
            return SharedSnapshotTransactionTests._Cursor(self)

        def rollback(self) -> None:
            self.rollback_count += 1

        def close(self) -> None:
            self.close_count += 1

    class _Cursor:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None) -> None:
            normalized_params = tuple(params) if params is not None else None
            self.connection.commands.append((sql, normalized_params))
            if sql == self.connection.fail_sql:
                raise RuntimeError("injected database failure")
            business_statement = sql.lstrip().upper().startswith(
                ("SELECT", "WITH")
            )
            if (
                business_statement
                and self.connection.fail_first_business is not None
            ):
                failure = self.connection.fail_first_business
                self.connection.fail_first_business = None
                raise failure
            if not business_statement:
                return
            if "customer_name" in sql:
                self.connection.rows = [
                    {
                        "customer_name": "Customer A",
                        "metric_value": 75,
                        "comparison_value": 100,
                        "delta_value": -25,
                        "change_rate": -0.25,
                        "__matched_row_count": 1,
                    },
                    {
                        "customer_name": "Customer B",
                        "metric_value": 125,
                        "comparison_value": 200,
                        "delta_value": -75,
                        "change_rate": -0.375,
                        "__matched_row_count": 1,
                    },
                ]
            else:
                self.connection.rows = [
                    {
                        "metric_value": 200,
                        "comparison_value": 300,
                        "delta_value": -100,
                        "change_rate": -1 / 3,
                        "__matched_row_count": 2,
                    }
                ]

        def fetchmany(self, count: int):
            return self.connection.rows[:count]

    @staticmethod
    def _manual_pair() -> list[dict]:
        overall = {
            **_delivery_request("overall"),
            "comparison": {"kind": "previous_period"},
        }
        partition = {
            **_delivery_request("partition"),
            "comparison": {"kind": "previous_period"},
            "dimensions": ["customer"],
            "decomposition_of_request_id": "overall",
        }
        return [overall, partition]

    def _assert_one_consistent_snapshot(self, requests: list[dict]) -> dict:
        connection = self._Connection()
        with mock.patch.object(
            tools, "_connect", return_value=connection
        ) as connect:
            payload = json.loads(
                tools._datasage_query_with_slot({"requests": requests})
            )
        self.assertEqual(connect.call_count, 1)
        sql_commands = [sql for sql, _params in connection.commands]
        self.assertEqual(
            sql_commands.count(
                "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            ),
            1,
        )
        self.assertEqual(
            sql_commands.count(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
            ),
            1,
        )
        self.assertEqual(
            sum(sql.lstrip().upper().startswith("WITH") for sql in sql_commands),
            2,
        )
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.close_count, 1)
        self.assertTrue(
            any(
                "structural_contribution" in claim["allowed_relations"]
                for result in payload["results"]
                for claim in result["claim_ledger"]
            )
        )
        self.assertNotIn("_snapshot_group_marker", json.dumps(payload))
        return payload

    def test_explicit_operation_uses_one_consistent_snapshot_transaction(self) -> None:
        request = {
            **_delivery_request("customer-change"),
            "comparison": {"kind": "previous_period"},
            "complete_change_decomposition": {"dimension": "customer"},
        }
        self._assert_one_consistent_snapshot([request])

    def test_manual_linked_pair_uses_the_same_transaction(self) -> None:
        self._assert_one_consistent_snapshot(self._manual_pair())

    def test_snapshot_setup_failure_always_rolls_back_and_closes(self) -> None:
        connection = self._Connection(
            fail_sql="START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        with mock.patch.object(tools, "_connect", return_value=connection):
            with self.assertRaises(tools.QueryFailure) as caught:
                with tools._consistent_snapshot_executor():
                    self.fail("failed snapshot setup must not enter the body")
        self.assertEqual(caught.exception.code, "QUERY_FAILED")
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.close_count, 1)

    def test_shared_executor_preserves_database_failure_taxonomy(self) -> None:
        cases = (
            (Exception(3024, "maximum statement execution time exceeded"),
             "SERVER_STATEMENT_TIMEOUT", True),
            (TimeoutError("timed out"), "QUERY_TIMEOUT", True),
            (RuntimeError("connection reset"), "QUERY_FAILED", False),
        )
        for error, expected_code, expected_timeout in cases:
            with self.subTest(expected_code=expected_code):
                failure = tools._database_query_failure(error)
                self.assertEqual(failure.code, expected_code)
                self.assertEqual(failure.timeout, expected_timeout)

    def test_failed_statement_poisons_group_without_second_select(self) -> None:
        connection = self._Connection(
            fail_first_business=TimeoutError("timed out")
        )
        with mock.patch.object(tools, "_connect", return_value=connection):
            payload = json.loads(
                tools._datasage_query_with_slot(
                    {"requests": self._manual_pair()}
                )
            )
        business_commands = [
            sql
            for sql, _params in connection.commands
            if sql.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        self.assertEqual(len(business_commands), 1)
        self.assertEqual(
            [result["error"]["code"] for result in payload["results"]],
            ["QUERY_TIMEOUT", "QUERY_TIMEOUT"],
        )
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.close_count, 1)


class RetryContractTests(unittest.TestCase):
    def test_transient_capacity_failure_has_one_bounded_retry(self) -> None:
        metadata = tools._retry_metadata(
            tools.QueryFailure("QUERY_CONCURRENCY_LIMIT", "busy")
        )
        self.assertTrue(metadata["retryable"])
        self.assertEqual(metadata["max_retry_attempts"], 1)
        self.assertGreaterEqual(metadata["retry_after_seconds"], 1)

    def test_database_unavailable_has_cache_aware_backoff(self) -> None:
        with mock.patch.object(
            runtime_health,
            "query_readiness_status",
            return_value={
                "ready": False,
                "reason_code": "DATABASE_CONNECTION_FAILED",
                "missing_names": [],
            },
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [_delivery_request()]}
                )
            )
        error = payload["results"][0]["error"]
        self.assertEqual(error["code"], "DATABASE_UNAVAILABLE")
        self.assertTrue(error["retryable"])
        self.assertEqual(error["max_retry_attempts"], 1)
        self.assertGreaterEqual(error["retry_after_seconds"], 10)

    def test_permanent_readiness_failures_are_not_retryable(self) -> None:
        permanent_codes = (
            "DATABASE_CONFIGURATION_MISSING",
            "DATABASE_GRANTS_UNVERIFIED",
            "DATABASE_SECURITY_EVIDENCE_MISSING",
            "DATABASE_TLS_REQUIRED",
            "DATABASE_TLS_CERTIFICATE_INVALID",
            "DATABASE_TLS_IDENTITY_INVALID",
            "HERMES_IDENTITY_MISMATCH",
        )
        for internal_code in permanent_codes:
            with self.subTest(internal_code=internal_code), mock.patch.object(
                runtime_health,
                "query_readiness_status",
                return_value={
                    "ready": False,
                    "reason_code": internal_code,
                    "missing_names": [],
                },
            ):
                payload = json.loads(
                    tools.runtime_guarded_datasage_query(
                        {"requests": [_delivery_request()]}
                    )
                )
            error = payload["results"][0]["error"]
            expected_public = (
                "DATABASE_CONFIGURATION_MISSING"
                if internal_code == "DATABASE_CONFIGURATION_MISSING"
                else "HERMES_IDENTITY_UNVERIFIED"
                if internal_code.startswith("HERMES_IDENTITY_")
                else "DATABASE_UNAVAILABLE"
            )
            self.assertEqual(error["code"], expected_public)
            self.assertFalse(error["retryable"])
            self.assertEqual(error["max_retry_attempts"], 0)
            self.assertNotIn("retry_after_seconds", error)

    def test_invalid_input_remains_non_retryable(self) -> None:
        metadata = tools._retry_metadata(
            tools.QueryFailure("INVALID_INPUT", "bad request")
        )
        self.assertFalse(metadata["retryable"])
        self.assertEqual(metadata["max_retry_attempts"], 0)
        self.assertNotIn("retry_after_seconds", metadata)


class SchemaRuntimeAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft7Validator(schemas.DATASAGE_QUERY["parameters"])

    def test_model_visible_descriptions_expose_evidence_subsumption_without_fixed_routing(self) -> None:
        properties = schemas.REQUEST["properties"]
        comparison = properties["comparison"]["description"]
        decomposition = properties["complete_change_decomposition"]["description"]

        self.assertIn(
            "already returns current, prior-period, absolute change, and change rate",
            comparison,
        )
        self.assertIn(
            "do not add explicit current/prior requests solely to duplicate them",
            comparison,
        )
        self.assertIn("Other metrics or scopes remain selectable", comparison)
        self.assertIn(
            "already includes the same-scope overall comparison",
            decomposition,
        )
        self.assertIn(
            "defaults its comparison semantics to previous_period",
            decomposition,
        )
        self.assertIn("When comparison is omitted", decomposition)
        self.assertNotIn("must include comparison exactly", decomposition)
        self.assertIn(
            "do not add an identical overall request solely to duplicate it",
            decomposition,
        )
        self.assertIn("Other evidence remains selectable", decomposition)
        combined = f"{comparison} {decomposition}".lower()
        for forbidden in (
            "fixed number of",
            "always query",
            "must query customer",
            "must query department",
            "choose one intent",
        ):
            self.assertNotIn(forbidden, combined)

    def test_schema_rejects_timestamp_boundaries_runtime_does_not_accept(self) -> None:
        request = _delivery_request()
        request["time_range"] = {
            "start": "2026-07-01T00:00:00",
            "end": "2026-08-01T00:00:00",
        }
        self.assertFalse(self.validator.is_valid({"requests": [request]}))

    def test_snapshot_comparison_requires_months_in_schema(self) -> None:
        request = {
            "request_id": "q1",
            "domain": "receivable",
            "mode": "metric",
            "purpose": "runtime hardening test",
            "metric": "current_debt_amount",
            "comparison": {"kind": "snapshot_months_before"},
        }
        self.assertFalse(self.validator.is_valid({"requests": [request]}))

    def test_previous_period_rejects_irrelevant_months_in_schema_and_runtime(self) -> None:
        request = _delivery_request()
        request["comparison"] = {"kind": "previous_period", "months": 2}
        self.assertFalse(self.validator.is_valid({"requests": [request]}))
        datasets, semantics = tools._contracts("delivery")
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._build_metric_query(request, datasets, semantics, 100)
        self.assertEqual(caught.exception.code, "INVALID_PLAN")

    def test_filter_count_limit_matches_schema_and_runtime(self) -> None:
        request = _delivery_request()
        request["metric_filters"] = {
            f"filter_{index}": "value" for index in range(13)
        }
        self.assertFalse(self.validator.is_valid({"requests": [request]}))
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request(request)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_comparison_shape_is_rejected_before_readiness_for_both_states(self) -> None:
        invalid_comparisons = (
            {"kind": "snapshot_months_before"},
            {"kind": "snapshot_months_before", "months": True},
            {"kind": "previous_period", "months": 2},
        )
        for ready in (False, True):
            for comparison in invalid_comparisons:
                request = _delivery_request()
                request["comparison"] = comparison
                with (
                    self.subTest(ready=ready, comparison=comparison),
                    mock.patch.object(
                        runtime_health,
                        "query_readiness_status",
                        return_value={
                            "ready": ready,
                            "reason_code": None
                            if ready
                            else "DATABASE_CONNECTION_FAILED",
                        },
                    ),
                    mock.patch.object(tools, "datasage_query") as execute,
                ):
                    payload = json.loads(
                        tools.runtime_guarded_datasage_query(
                            {"requests": [request]}
                        )
                    )
                self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
                execute.assert_not_called()

    def test_complete_decomposition_defaults_previous_period_without_mutating_input(self) -> None:
        for period_field, period_value in (
            (
                "time_range",
                {"start": "2026-07-01", "end": "2026-08-01"},
            ),
            ("calendar_month", "2026-07"),
        ):
            request = _delivery_request("decomposition")
            request.pop("time_range", None)
            request[period_field] = period_value
            request["complete_change_decomposition"] = {"dimension": "customer"}
            original = copy.deepcopy(request)

            with self.subTest(period_field=period_field):
                self.assertTrue(self.validator.is_valid({"requests": [request]}))
                normalized = tools._validate_request(request)
                expanded, partitions = tools._expand_complete_change_decompositions(
                    [request]
                )

                self.assertEqual(
                    normalized["comparison"],
                    {"kind": "previous_period"},
                )
                self.assertEqual(
                    [item["comparison"] for item in expanded],
                    [
                        {"kind": "previous_period"},
                        {"kind": "previous_period"},
                    ],
                )
                self.assertEqual(request, original)
                self.assertEqual(len(partitions), 1)

    def test_complete_decomposition_still_rejects_other_comparison_and_missing_period(self) -> None:
        other_comparison = _delivery_request("other-comparison")
        other_comparison.pop("time_range")
        other_comparison["comparison"] = {
            "kind": "snapshot_months_before",
            "months": 1,
        }
        other_comparison["complete_change_decomposition"] = {
            "dimension": "customer"
        }
        missing_period = _delivery_request("missing-period")
        missing_period.pop("time_range")
        missing_period["complete_change_decomposition"] = {"dimension": "customer"}

        for request in (other_comparison, missing_period):
            with self.subTest(request_id=request["request_id"]):
                self.assertFalse(self.validator.is_valid({"requests": [request]}))
                with self.assertRaises(tools.QueryFailure) as caught:
                    tools._validate_request(request)
                self.assertEqual(caught.exception.code, "INVALID_INPUT")


class CalendarMonthContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft7Validator(schemas.DATASAGE_QUERY["parameters"])

    @staticmethod
    def _calendar_request(value: object) -> dict:
        request = _delivery_request()
        request.pop("time_range")
        request["calendar_month"] = value
        return request

    def test_schema_and_runtime_expand_a_calendar_month_without_mutating_input(self) -> None:
        request = self._calendar_request("2026-07")
        self.assertTrue(self.validator.is_valid({"requests": [request]}))

        normalized = tools._validate_request(request)

        self.assertNotIn("time_range", request)
        self.assertEqual(request["calendar_month"], "2026-07")
        self.assertNotIn("calendar_month", normalized)
        self.assertEqual(
            normalized["time_range"],
            {"start": "2026-07-01", "end": "2026-08-01"},
        )

    def test_leap_february_and_december_cross_year_are_deterministic(self) -> None:
        cases = (
            ("2024-02", {"start": "2024-02-01", "end": "2024-03-01"}),
            ("2026-12", {"start": "2026-12-01", "end": "2027-01-01"}),
        )
        for calendar_month, expected in cases:
            with self.subTest(calendar_month=calendar_month):
                normalized = tools._validate_request(
                    self._calendar_request(calendar_month)
                )
                self.assertEqual(normalized["time_range"], expected)

    def test_bool_non_string_and_illegal_year_month_fail_closed(self) -> None:
        invalid_values = (
            True,
            202607,
            {"year": 2026, "month": 7},
            "2026-00",
            "2026-13",
            "0000-01",
            "9999-12",
            "2026-7",
        )
        for value in invalid_values:
            request = self._calendar_request(value)
            with self.subTest(calendar_month=value):
                self.assertFalse(self.validator.is_valid({"requests": [request]}))
                with self.assertRaises(tools.QueryFailure) as caught:
                    tools._validate_request(request)
                self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_calendar_month_is_mutually_exclusive_with_time_range(self) -> None:
        request = _delivery_request()
        request["calendar_month"] = "2026-07"
        self.assertFalse(self.validator.is_valid({"requests": [request]}))
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request(request)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

        snapshot_request = self._calendar_request("2026-07")
        snapshot_request["comparison"] = {
            "kind": "snapshot_months_before",
            "months": 1,
        }
        self.assertFalse(
            self.validator.is_valid({"requests": [snapshot_request]})
        )
        with self.assertRaises(tools.QueryFailure) as snapshot_caught:
            tools._validate_request(snapshot_request)
        self.assertEqual(snapshot_caught.exception.code, "INVALID_INPUT")

    def test_previous_period_accepts_calendar_month_and_public_evidence_is_half_open(self) -> None:
        request = self._calendar_request("2024-02")
        request["comparison"] = {"kind": "previous_period"}
        self.assertTrue(self.validator.is_valid({"requests": [request]}))

        normalized = tools._validate_request(request)
        datasets, semantics = tools._contracts("delivery")
        sql, params, scope = tools._build_metric_query(
            normalized, datasets, semantics, 100
        )

        self.assertEqual(sql.count("%s"), len(params))
        self.assertIn("2024-02-01", params)
        self.assertIn("2024-03-01", params)
        self.assertEqual(
            tools._public_time_range(scope["time_range"]),
            {
                "current": {
                    "start": "2024-02-01",
                    "end": "2024-03-01",
                    "source": "explicit",
                },
                "comparison": {
                    "start": "2024-01-01",
                    "end": "2024-02-01",
                    "source": "explicit",
                },
            },
        )

    def test_existing_explicit_time_range_remains_compatible(self) -> None:
        for time_range in (
            {"start": "2026-07-01", "end": "2026-08-01"},
            {"start": "2026-07", "end": "2026-08"},
        ):
            request = {**_delivery_request(), "time_range": time_range}
            with self.subTest(time_range=time_range):
                self.assertTrue(
                    self.validator.is_valid({"requests": [request]})
                )
                normalized = tools._validate_request(request)
                self.assertEqual(normalized, request)


class MetricContextWireTests(unittest.TestCase):
    @staticmethod
    def _private_result(
        request_id: str,
        *,
        metric_ref: str = "metric_delivery",
        label: str = "净出库金额",
        definition: str | None = "所选期间毛出库金额扣除同期有效退货金额后的净流量。",
        unit: str | None = "人民币元",
        unit_policy: str | None = None,
        currency_policy: str | None = None,
        answer_note: str | None = None,
    ) -> dict:
        return {
            "request_id": request_id,
            "status": "success",
            "data_state": "rows",
            "business_metric_ref": metric_ref,
            "business_metric_label": label,
            "business_metric_definition": definition,
            "business_metric_unit": unit,
            "business_metric_unit_policy": unit_policy,
            "business_metric_currency_policy": currency_policy,
            "business_metric_answer_note": answer_note,
            "business_dimension_labels": [],
            "scope_fingerprint": "scope_test",
            "projection_fingerprint": "projection_test",
            "claim_ledger": [],
            "disclosure_contract_version": "metric-disclosure-ledger/v1",
            "disclosure_ledger": [],
            "disclosure_ledger_seal": "sha256_test",
            "allowed_reasoning_topics": [],
            "change_reconciliation": None,
            "row_count": 1,
            "truncated": False,
            "applied_time_range": {"source": "current_snapshot"},
            "error": None,
        }

    def test_contexts_are_deduplicated_without_copying_meaning_into_results(self) -> None:
        results = [self._private_result("july"), self._private_result("june")]

        contexts = tools._model_wire_metric_contexts(results)

        self.assertEqual(
            contexts,
            [
                {
                    "business_metric_ref": "metric_delivery",
                    "label": "净出库金额",
                    "definition": "所选期间毛出库金额扣除同期有效退货金额后的净流量。",
                    "unit": "人民币元",
                }
            ],
        )
        public_result = tools._model_wire_result(results[0])
        self.assertNotIn("definition", public_result)
        self.assertNotIn("business_metric_definition", public_result)
        self.assertNotIn("answer_note", public_result)

    def test_nonempty_policy_and_answer_note_are_preserved_once(self) -> None:
        result = self._private_result(
            "original",
            unit_policy="必须按单位分组或限定单一单位。",
            currency_policy="必须按币种分组或限定单一币种，且不同币种不得直接相加。",
            answer_note="结果允许为负数。",
        )

        context = tools._model_wire_metric_contexts([result])[0]

        self.assertEqual(context["unit_policy"], result["business_metric_unit_policy"])
        self.assertEqual(
            context["currency_policy"], result["business_metric_currency_policy"]
        )
        self.assertEqual(context["answer_note"], result["business_metric_answer_note"])

    def test_real_business_string_unit_policy_is_safely_projected(self) -> None:
        datasets, semantics = tools._contracts("target")

        context = tools._business_metric_context(
            {"metric": "delivery_target_completion"},
            semantics,
            datasets,
        )

        self.assertEqual(
            context["business_metric_unit_policy"],
            "目标、实际与缺口为人民币元；完成率为比例（可为负，可超过1，不做截断）",
        )

    def test_completion_rate_policy_allows_over_100_percent_and_negative(self) -> None:
        """完成率语义声明必须允许超额(>1)与负实际(<0)，不得再声明为0-1。"""
        datasets, semantics = tools._contracts("target")

        for metric_code in ("delivery_target_completion", "receipt_target_completion"):
            metric = semantics["metrics"][metric_code]
            unit_policy = metric["unit_policy"]
            self.assertNotIn("0至1", unit_policy)
            self.assertIn("可为负", unit_policy)
            self.assertIn("可超过1", unit_policy)
            self.assertIn("不做截断", unit_policy)

    def test_target_completion_sql_has_no_clamp_on_rate(self) -> None:
        """生成的完成率 SQL 必须是实际/目标直接相除，不得夹取到0-1。"""
        aq = _load_package_module("analytical_queries")

        datasets, semantics = tools._contracts("target")
        metric = semantics["metrics"]["delivery_target_completion"]
        request = {
            "request_id": "rate-no-clamp",
            "domain": "target",
            "mode": "metric",
            "purpose": "verify completion rate not clamped",
            "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail",
            "calendar_month": "2026-08",
        }
        sql, _params, _scope = aq._target_completion_query(
            request, metric, datasets, 10
        )
        self.assertNotIn("LEAST(", sql.upper())
        self.assertNotIn("GREATEST(", sql.upper())
        self.assertNotIn("BETWEEN 0 AND 1", sql.upper())
        self.assertIn("AS metric_value", sql)
        self.assertIn("AS completion_rate", sql)

    def test_physical_policy_and_internal_policy_token_are_not_projected(self) -> None:
        datasets = {
            "datasets": {
                "vk_dwd.delivery_detail": {
                    "allowed_columns": ["secret_measure"],
                }
            }
        }
        context = tools._business_metric_context(
            {"metric": "delivery_amount"},
            {
                "metrics": {
                    "delivery_amount": {
                        "business_definition": "安全业务定义。",
                        "unit": "人民币元",
                        "unit_policy": {
                            "business_rule": "必须依据 secret_measure 汇总。"
                        },
                        "currency_policy": "internalpolicy",
                    }
                }
            },
            datasets,
        )

        self.assertIsNone(context["business_metric_unit_policy"])
        self.assertIsNone(context["business_metric_currency_policy"])

    def test_conflicting_context_for_one_ref_fails_closed(self) -> None:
        results = [
            self._private_result("july"),
            self._private_result("june", definition="冲突定义。"),
        ]

        with self.assertRaises(tools.QueryFailure) as caught:
            tools._model_wire_metric_contexts(results)

        self.assertEqual(caught.exception.code, "CONTRACT_UNAVAILABLE")

    def test_physical_semantic_text_is_not_projected(self) -> None:
        datasets = {
            "datasets": {
                "vk_dwd.delivery_detail": {
                    "allowed_columns": ["secret_measure"],
                }
            }
        }
        context = tools._business_metric_context(
            {"metric": "delivery_amount"},
            {
                "metrics": {
                    "delivery_amount": {
                        "business_definition": "汇总 secret_measure 得到净出库金额。",
                        "unit": "人民币元",
                    }
                }
            },
            datasets,
        )
        result = self._private_result(
            "physical",
            definition=context["business_metric_definition"],
            unit=context["business_metric_unit"],
        )

        encoded = json.dumps(
            tools._model_wire_metric_contexts([result]), ensure_ascii=False
        )

        self.assertNotIn("secret_measure", encoded)
        self.assertNotIn("vk_dwd", encoded)

    def test_query_payload_exposes_top_level_contexts(self) -> None:
        requests = [_delivery_request("july"), _delivery_request("june")]

        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(
                tools,
                "_run_one",
                side_effect=lambda request, **_kwargs: self._private_result(
                    request["request_id"]
                ),
            ),
        ):
            payload = json.loads(tools._datasage_query_with_slot({"requests": requests}))

        self.assertEqual(len(payload["metric_contexts"]), 1)
        self.assertEqual(
            payload["metric_contexts"][0]["business_metric_ref"],
            "metric_delivery",
        )
        self.assertNotIn("business_metric_definition", payload["results"][0])

    def test_query_payload_conflict_fails_closed_with_empty_contexts(self) -> None:
        requests = [_delivery_request("july"), _delivery_request("june")]

        def run_one(request, **_kwargs):
            definition = "定义甲。" if request["request_id"] == "july" else "定义乙。"
            return self._private_result(
                request["request_id"],
                definition=definition,
            )

        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(tools, "_run_one", side_effect=run_one),
        ):
            payload = json.loads(tools._datasage_query_with_slot({"requests": requests}))

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "CONTRACT_UNAVAILABLE")
        self.assertEqual(payload["metric_contexts"], [])
        self.assertEqual(payload["results"], [])

    def test_invalid_query_payload_has_empty_contexts(self) -> None:
        payload = json.loads(tools._datasage_query_with_slot({"requests": []}))

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["metric_contexts"], [])


class BoundedBatchParallelismTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._ACTIVE_QUERY_CALLS = 0

    def tearDown(self) -> None:
        tools._ACTIVE_QUERY_CALLS = 0

    @staticmethod
    def _result(request_id: str) -> dict:
        return {
            "request_id": request_id,
            "status": "success",
            "data_state": "rows",
            "business_metric_ref": "metric_test",
            "business_metric_label": "Test Metric",
            "business_dimension_labels": [],
            "scope_fingerprint": "scope_test",
            "projection_fingerprint": "projection_test",
            "claim_ledger": [],
            "disclosure_contract_version": "metric-disclosure-ledger/v1",
            "disclosure_ledger": [],
            "disclosure_ledger_seal": "sha256_test",
            "allowed_reasoning_topics": [],
            "change_reconciliation": None,
            "row_count": 1,
            "truncated": False,
            "applied_time_range": {"source": "current_snapshot"},
            "error": None,
        }

    def test_independent_filter_free_batch_uses_available_global_slots(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def run_one(request, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return self._result(request["request_id"])

        requests = [_delivery_request(f"q{index}") for index in range(4)]
        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(tools, "_run_one", side_effect=run_one),
        ):
            payload = json.loads(tools.datasage_query({"requests": requests}))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(
            [item["request_id"] for item in payload["results"]],
            [item["request_id"] for item in requests],
        )
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 4)
        self.assertEqual(tools._ACTIVE_QUERY_CALLS, 0)

    def test_existing_global_load_forces_single_worker(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def run_one(request, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return self._result(request["request_id"])

        tools._ACTIVE_QUERY_CALLS = 3
        requests = [_delivery_request(f"q{index}") for index in range(3)]
        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(tools, "_run_one", side_effect=run_one),
        ):
            payload = json.loads(tools.datasage_query({"requests": requests}))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(max_active, 1)
        self.assertEqual(tools._ACTIVE_QUERY_CALLS, 3)


class DisplayIdentityTests(unittest.TestCase):
    def test_long_values_with_shared_prefix_remain_distinguishable(self) -> None:
        prefix = "X" * 79
        left = tools._safe_display_value(prefix + "A" * 30)
        right = tools._safe_display_value(prefix + "B" * 30)

        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertNotEqual(left, right)
        self.assertLessEqual(len(left), 80)
        self.assertLessEqual(len(right), 80)

    def test_hash_suffix_never_exceeds_16_17_or_80_character_limit(self) -> None:
        prefix = "Y" * 100
        for limit in (16, 17, 80):
            with self.subTest(limit=limit), mock.patch.object(
                tools, "_bounded_int", return_value=limit
            ):
                left = tools._safe_display_value(prefix + "A")
                right = tools._safe_display_value(prefix + "B")
            self.assertEqual(len(left), limit)
            self.assertEqual(len(right), limit)
            self.assertNotEqual(left, right)


class GovernedCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._ACTIVE_QUERY_CALLS = 0
        self.validator = Draft7Validator(schemas.DATASAGE_QUERY["parameters"])

    def tearDown(self) -> None:
        tools._ACTIVE_QUERY_CALLS = 0

    @staticmethod
    def _scalar_result(
        request_id: str,
        value: str,
        *,
        unit: str = "CNY",
        metric_ref: str = "delivery_amount",
        period: tuple[str, str] = ("2026-07-01", "2026-08-01"),
        filters: dict | None = None,
        share_dimensions: list[str] | None = None,
    ) -> dict:
        start, end = period
        return {
            **BoundedBatchParallelismTests._result(request_id),
            "_calculation_scope": {
                "version": "governed-calculation-scope/v1",
                "metric_basis_fingerprint": f"basis_{metric_ref}",
                "filter_scope": dict(filters or {}),
                "share_partition_dimensions": (
                    ["customer"]
                    if share_dimensions is None
                    else list(share_dimensions)
                ),
            },
            "business_metric_ref": metric_ref,
            "applied_time_range": {
                "start": start,
                "end": end,
                "source": "explicit",
            },
            "claim_ledger": [
                {
                    "claim_id": "claim_unsealed_0",
                    "claim_seal": "unsealed",
                    "request_id": request_id,
                    "metric_ref": metric_ref,
                    "metric_label": request_id,
                    "dimensions": [],
                    "scope_entities": [],
                    "period": {
                        "start": start,
                        "end": end,
                        "source": "explicit",
                    },
                    "scope_fingerprint": f"scope_{request_id}",
                    "projection_fingerprint": f"projection_{request_id}",
                    "unit": unit,
                    "currency": "CNY" if unit == "CNY" else None,
                    "facts": {"metric_value": value},
                    "states": {},
                    "source_truncated": False,
                    "allowed_relations": ["observation"],
                }
            ],
        }

    def _execute(
        self,
        calculations,
        values=("10.5", "3.2"),
        *,
        left_kwargs: dict | None = None,
        right_kwargs: dict | None = None,
    ) -> dict:
        requests = [_delivery_request("left"), _delivery_request("right")]

        def run_one(request, **_kwargs):
            if request["request_id"] == "left":
                return self._scalar_result(
                    "left", values[0], **dict(left_kwargs or {})
                )
            options = {"period": ("2026-06-01", "2026-07-01")}
            options.update(dict(right_kwargs or {}))
            return self._scalar_result(
                "right",
                values[1],
                **options,
            )

        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(
                tools,
                "_run_one",
                side_effect=run_one,
            ),
        ):
            return json.loads(
                tools.datasage_query(
                    {"requests": requests, "calculations": calculations}
                )
            )

    def test_schema_allows_only_named_governed_operations(self) -> None:
        args = {
            "requests": [_delivery_request("left"), _delivery_request("right")],
            "calculations": [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
        }
        self.assertTrue(self.validator.is_valid(args))
        args["calculations"][0]["formula"] = "left - right"
        self.assertFalse(self.validator.is_valid(args))

    def test_difference_is_deterministic_derived_observation(self) -> None:
        payload = self._execute(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ]
        )
        calculation = payload["calculations"][0]
        self.assertEqual(calculation["status"], "success")
        self.assertEqual(calculation["value"], "7.3")
        self.assertEqual(calculation["unit"], "CNY")
        self.assertEqual(calculation["allowed_relations"], ["derived_observation"])
        self.assertEqual(
            calculation["relation_semantics"]["derived_observation"],
            "arithmetic_not_registered_metric_or_causal_evidence",
        )
        self.assertEqual(
            [item["request_id"] for item in calculation["operands"]],
            ["left", "right"],
        )
        self.assertEqual(
            calculation["scope_compatibility"]["period_relation"],
            "different",
        )
        self.assertTrue(
            all(item["claim_id"].startswith("claim_") for item in calculation["operands"])
        )
        self.assertTrue(
            all(
                item["scope_fingerprint"].startswith("scope_")
                and item["projection_fingerprint"].startswith("projection_")
                and item["metric_basis_fingerprint"].startswith("basis_")
                and item["filter_fingerprint"].startswith("filter_")
                for item in calculation["operands"]
            )
        )

    def test_ratio_division_by_zero_fails_closed(self) -> None:
        payload = self._execute(
            [
                {
                    "calculation_id": "ratio",
                    "operation": "ratio",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            values=("10", "0"),
        )
        calculation = payload["calculations"][0]
        self.assertEqual(calculation["status"], "failed")
        self.assertEqual(calculation["error"]["code"], "CALCULATION_DIVISION_BY_ZERO")
        self.assertFalse(calculation["error"]["retryable"])
        self.assertEqual(payload["status"], "partial")

    def test_share_returns_a_proportion_not_a_metric(self) -> None:
        payload = self._execute(
            [
                {
                    "calculation_id": "share",
                    "operation": "share",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            values=("25", "100"),
            left_kwargs={
                "period": ("2026-07-01", "2026-08-01"),
                "filters": {"customer": ["C001"]},
            },
            right_kwargs={
                "period": ("2026-07-01", "2026-08-01"),
                "filters": {},
            },
        )
        calculation = payload["calculations"][0]
        self.assertEqual(calculation["value"], "0.25")
        self.assertEqual(calculation["unit"], "proportion")
        self.assertIn("NOT_A_REGISTERED_METRIC", calculation["limitations"])
        self.assertTrue(
            calculation["scope_compatibility"][
                "numerator_strict_subset_of_denominator"
            ]
        )

    def test_difference_rejects_different_metric_filters(self) -> None:
        left = self._scalar_result(
            "left", "10", filters={"customer": ["C001"]}
        )
        right = self._scalar_result(
            "right",
            "2",
            period=("2026-06-01", "2026-07-01"),
            filters={},
        )
        tools._seal_claim_ids([left, right])
        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        self.assertEqual(
            calculation["error"]["code"],
            "CALCULATION_SCOPE_MISMATCH",
        )

    def test_unknown_scope_contract_never_claims_compatibility(self) -> None:
        left = self._scalar_result("left", "10")
        right = self._scalar_result("right", "2")
        right.pop("_calculation_scope")
        tools._seal_claim_ids([left, right])
        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        self.assertEqual(
            calculation["error"]["code"],
            "CALCULATION_SCOPE_UNVERIFIED",
        )

    def test_share_requires_a_proven_strict_subset(self) -> None:
        left = self._scalar_result("left", "100", filters={})
        right = self._scalar_result(
            "right", "25", filters={"customer": ["C001"]}
        )
        tools._seal_claim_ids([left, right])
        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "share",
                    "operation": "share",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        self.assertEqual(
            calculation["error"]["code"],
            "CALCULATION_SUBSET_NOT_PROVEN",
        )

    def test_share_requires_registered_additive_partition_dimension(self) -> None:
        left = self._scalar_result(
            "left",
            "25",
            filters={"customer": ["C001"]},
            share_dimensions=[],
        )
        right = self._scalar_result(
            "right",
            "100",
            filters={},
            share_dimensions=[],
        )
        tools._seal_claim_ids([left, right])
        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "share",
                    "operation": "share",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        self.assertEqual(
            calculation["error"]["code"],
            "CALCULATION_SUBSET_NOT_PROVEN",
        )

    def test_share_rejects_negative_or_above_total_values(self) -> None:
        calculation = {
            "calculation_id": "share",
            "operation": "share",
            "left_request_id": "left",
            "right_request_id": "right",
        }
        for numerator, denominator in (("-1", "100"), ("125", "100")):
            left = self._scalar_result(
                "left",
                numerator,
                filters={"customer": ["C001"]},
            )
            right = self._scalar_result(
                "right",
                denominator,
                filters={},
            )
            tools._seal_claim_ids([left, right])
            with self.subTest(numerator=numerator, denominator=denominator):
                result = tools._build_governed_calculations(
                    [calculation], [left, right]
                )[0]
            self.assertEqual(
                result["error"]["code"],
                "CALCULATION_SHARE_OUT_OF_RANGE",
            )

    def test_incompatible_units_fail_closed(self) -> None:
        requests = [_delivery_request("left"), _delivery_request("right")]
        calculation = {
            "calculation_id": "ratio",
            "operation": "ratio",
            "left_request_id": "left",
            "right_request_id": "right",
        }
        with (
            mock.patch.object(
                tools,
                "_prepare_one",
                side_effect=lambda request, **_kwargs: {
                    "request": request,
                    "semantics": {},
                },
            ),
            mock.patch.object(
                tools,
                "_run_one",
                side_effect=lambda request, **_kwargs: self._scalar_result(
                    request["request_id"],
                    "10" if request["request_id"] == "left" else "2",
                    unit="CNY" if request["request_id"] == "left" else "count",
                    period=("2026-07-01", "2026-08-01")
                    if request["request_id"] == "left"
                    else ("2026-06-01", "2026-07-01"),
                ),
            ),
        ):
            payload = json.loads(
                tools.datasage_query(
                    {"requests": requests, "calculations": [calculation]}
                )
            )
        self.assertEqual(
            payload["calculations"][0]["error"]["code"],
            "CALCULATION_UNIT_MISMATCH",
        )


if __name__ == "__main__":
    unittest.main()
