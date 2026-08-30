from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
TEST_PACKAGE = "datasage_query_dependency_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contract_store = importlib.import_module(f"{TEST_PACKAGE}.contract_store")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
db_executor = importlib.import_module(f"{TEST_PACKAGE}.db_executor")
db_runtime = importlib.import_module(f"{TEST_PACKAGE}.db_runtime")
entities = importlib.import_module(f"{TEST_PACKAGE}.entities")
runtime_health = importlib.import_module(f"{TEST_PACKAGE}.runtime_health")
receipt_cache = importlib.import_module(f"{TEST_PACKAGE}.receipt_cache")
sql_identifiers = importlib.import_module(f"{TEST_PACKAGE}.sql_identifiers")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class ModuleDependencyTests(unittest.TestCase):
    def test_shared_yaml_cache_is_the_only_yaml_parser_owner(self) -> None:
        contract_store.parse_yaml_cached.cache_clear()
        path = "plugins/datasage-query/contracts/query-policy.yaml"
        via_tools = tools._read_yaml(path)
        via_contracts = contracts._read_yaml(path)
        self.assertIs(via_tools, via_contracts)
        self.assertFalse(hasattr(tools, "_parse_yaml_cached"))
        self.assertFalse(hasattr(contracts, "_parse_yaml_cached"))
        self.assertFalse(hasattr(tools, "_profile_root"))
        self.assertFalse(hasattr(contracts, "_profile_root"))

    def test_identifier_compatibility_facade_preserves_results_and_codes(self) -> None:
        self.assertEqual(
            sql_identifiers.quote_table("vk_dw.sample"),
            tools._quote_table("vk_dw.sample"),
        )
        self.assertEqual("`alias`.`column_1`", tools._qualified_identifier("alias", "column_1"))
        for invalid in ("", "bad-name", "schema.table.extra"):
            with self.subTest(invalid=invalid), self.assertRaises(tools.QueryFailure) as caught:
                tools._quote_table(invalid)
            self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_database_connection_compatibility_facade_delegates(self) -> None:
        sentinel = object()
        with mock.patch.object(db_runtime, "connect", return_value=sentinel) as connect:
            self.assertIs(sentinel, tools._connect(timeout_seconds=3))
        connect.assert_called_once_with(
            connect_timeout_seconds=None,
            read_timeout_seconds=None,
            timeout_seconds=3,
        )

        with mock.patch.object(
            db_runtime,
            "connect",
            side_effect=db_runtime.DatabaseRuntimeError(
                "CONFIGURATION_MISSING", "missing"
            ),
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._connect()
        self.assertEqual("CONFIGURATION_MISSING", caught.exception.code)

    def test_entity_executor_bridge_keeps_existing_monkeypatch_seam(self) -> None:
        with mock.patch.object(tools, "_execute", return_value=([], False)) as execute:
            payload = json.loads(entities.datasage_entity_resolve({"token": "未登记地域"}))
        self.assertEqual("not_found", payload["status"])
        execute.assert_called_once()

    def test_runtime_health_uses_shared_database_adapter(self) -> None:
        class Connection:
            _datasage_security_evidence = {
                "live_connection_verified": True,
                "grants_verified": True,
                "transport_mode": "tls",
            }

            def close(self) -> None:
                return None

        runtime_health._LIVE_CACHE = None
        with mock.patch.object(db_runtime, "connect", return_value=Connection()) as connect:
            status = runtime_health.live_database_security_status()
        self.assertTrue(status["ready"], status)
        connect.assert_called_once()

    def test_receipt_facade_uses_the_signature_aware_cache(self) -> None:
        with mock.patch.object(
            receipt_cache,
            "get_metric_capability_receipt",
            return_value="a" * 64,
        ) as cached:
            receipt = tools._current_metric_detail_receipt(
                "delivery", "delivery_amount"
            )
        self.assertEqual("a" * 64, receipt)
        cached.assert_called_once_with(
            "delivery",
            "delivery_amount",
            builder=tools._build_current_metric_detail_receipt,
        )

        with mock.patch.object(
            receipt_cache,
            "get_metric_capability_receipt",
            side_effect=contract_store.ContractStoreError(
                "CONTRACT_UNAVAILABLE", "missing"
            ),
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._current_metric_detail_receipt("delivery", "delivery_amount")
        self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)
        self.assertEqual("contract_load", caught.exception.stage)

    def test_tools_database_facades_delegate_all_transaction_lifecycle(self) -> None:
        single_source = inspect.getsource(tools._execute_with_source)
        snapshot_source = inspect.getsource(tools._ConsistentSnapshotExecutor)
        combined = single_source + snapshot_source
        self.assertIn("db_executor.ReadOnlyDbExecutor", single_source)
        self.assertIn("db_executor.ReadOnlyDbExecutor", snapshot_source)
        for duplicated_statement in (
            "START TRANSACTION READ ONLY",
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        ):
            self.assertNotIn(duplicated_statement, combined)

    def test_single_statement_facade_preserves_public_error_taxonomy(self) -> None:
        source = {"source_identity": "warehouse-a"}
        cases = (
            (
                db_executor.DeadlineExceeded("late"),
                "BATCH_DEADLINE_EXCEEDED",
                True,
            ),
            (TimeoutError("socket timed out"), "QUERY_TIMEOUT", True),
            (RuntimeError("driver failure"), "QUERY_FAILED", False),
        )
        for error, expected_code, expected_timeout in cases:
            with self.subTest(expected_code=expected_code):
                delegate = mock.Mock()
                delegate.execute.side_effect = error
                delegate.source_evidence_ref = source
                with mock.patch.object(
                    db_executor,
                    "ReadOnlyDbExecutor",
                    return_value=delegate,
                ), self.assertRaises(tools.QueryFailure) as caught:
                    tools._execute_with_source("SELECT 1", (), 1)
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(expected_timeout, caught.exception.timeout)
                self.assertEqual(source, caught.exception.source_evidence_ref)

        delegate = mock.Mock()
        delegate.execute.side_effect = tools.DatabaseSecurityError(
            "SOURCE_IDENTITY_CHANGED", "changed"
        )
        delegate.source_evidence_ref = source
        with mock.patch.object(
            db_executor,
            "ReadOnlyDbExecutor",
            return_value=delegate,
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._execute_with_source("SELECT 1", (), 1)
        self.assertEqual("DATABASE_IDENTITY_CHANGED", caught.exception.code)
        self.assertEqual("database_security", caught.exception.stage)
        self.assertEqual(source, caught.exception.source_evidence_ref)

    def test_snapshot_facade_runs_through_shared_executor_end_to_end(self) -> None:
        class Cursor:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params=None):
                self.connection.executions.append((sql, params))

            def fetchmany(self, count):
                self.connection.fetch_sizes.append(count)
                return [{"metric_value": 7}]

        class Connection:
            def __init__(self):
                self.executions = []
                self.fetch_sizes = []
                self.rollback_count = 0
                self.close_count = 0

            def cursor(self):
                return Cursor(self)

            def rollback(self):
                self.rollback_count += 1

            def close(self):
                self.close_count += 1

        connection = Connection()
        source = {"source_identity": "warehouse-a"}
        with mock.patch.object(
            tools, "_connect", return_value=connection
        ) as connect, mock.patch.object(
            tools,
            "confirm_mysql_read_only_transaction",
            return_value=source,
        ) as confirm:
            with tools._consistent_snapshot_executor() as snapshot:
                rows, truncated, observed_source = snapshot.execute(
                    "SELECT metric_value", (), 2
                )
                self.assertIsNotNone(snapshot.marker)

        self.assertEqual([{"metric_value": 7}], rows)
        self.assertFalse(truncated)
        self.assertEqual(source, observed_source)
        self.assertEqual([3], connection.fetch_sizes)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        connect.assert_called_once()
        confirm.assert_called_once_with(connection)

    def test_snapshot_facade_maps_public_errors_and_reuses_poison(self) -> None:
        source = {"source_identity": "warehouse-a"}
        driver_timeout = RuntimeError(
            3024, "Maximum statement execution time exceeded"
        )
        for raw_error, expected_code in (
            (db_executor.DeadlineExceeded("late"), "BATCH_DEADLINE_EXCEEDED"),
            (driver_timeout, "SERVER_STATEMENT_TIMEOUT"),
        ):
            with self.subTest(expected_code=expected_code):
                delegate = mock.MagicMock()
                delegate.marker = "snapshot_group_test"
                delegate.source_evidence_ref = source
                delegate.execute.side_effect = raw_error
                with mock.patch.object(
                    db_executor,
                    "ReadOnlyDbExecutor",
                    return_value=delegate,
                ):
                    with tools._consistent_snapshot_executor() as snapshot:
                        with self.assertRaises(tools.QueryFailure) as first:
                            snapshot.execute("SELECT broken", (), 1)
                        with self.assertRaises(tools.QueryFailure) as second:
                            snapshot.execute("SELECT later", (), 1)
                self.assertEqual(expected_code, first.exception.code)
                self.assertEqual(expected_code, second.exception.code)
                self.assertTrue(first.exception.timeout)
                self.assertEqual(source, first.exception.source_evidence_ref)
                self.assertEqual(source, second.exception.source_evidence_ref)
                delegate.execute.assert_called_once()
                delegate.close.assert_called_once()

        security_delegate = mock.MagicMock()
        security_delegate.source_evidence_ref = None
        security_delegate.__enter__.side_effect = tools.DatabaseSecurityError(
            "SOURCE_IDENTITY_CHANGED", "changed"
        )
        with mock.patch.object(
            db_executor,
            "ReadOnlyDbExecutor",
            return_value=security_delegate,
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._consistent_snapshot_executor().__enter__()
        self.assertEqual("DATABASE_IDENTITY_CHANGED", caught.exception.code)
        self.assertEqual("database_security", caught.exception.stage)
        security_delegate.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
