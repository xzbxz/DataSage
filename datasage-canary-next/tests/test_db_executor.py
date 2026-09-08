"""Offline contract tests for the narrow read-only database executor."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_db_executor_tests"

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)
db_executor = importlib.import_module(f"{PACKAGE}.db_executor")


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        call = (sql, params)
        self.connection.executions.append(call)
        if sql == self.connection.fail_sql:
            raise self.connection.statement_error

    def fetchmany(self, count):
        self.connection.fetch_sizes.append(count)
        if self.connection.row_batches:
            return self.connection.row_batches.pop(0)
        return []


class FakeConnection:
    def __init__(
        self,
        *row_batches,
        fail_sql=None,
        statement_error=None,
        rollback_error=None,
        close_error=None,
    ):
        self.row_batches = list(row_batches)
        self.fail_sql = fail_sql
        self.statement_error = statement_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.executions = []
        self.fetch_sizes = []
        self.cursor_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        self.cursor_count += 1
        return FakeCursor(self)

    def rollback(self):
        self.rollback_count += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class FakeConnectionFactory:
    def __init__(self, connection=None, error=None):
        self.connection = connection
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.connection


class SequenceClock:
    def __init__(self, *values):
        self.values = list(values)
        self.last = values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class ReadOnlyDbExecutorTests(unittest.TestCase):
    def _single(self, connection, **overrides):
        factory = FakeConnectionFactory(connection)
        options = {
            "mode": "single_statement",
            "connection_factory": factory,
            "confirm_read_only_transaction": lambda _connection: {
                "source": "warehouse-a",
                "read_only": True,
            },
            "query_timeout_seconds": 30,
            "clock": lambda: 0.0,
        }
        options.update(overrides)
        return db_executor.ReadOnlyDbExecutor(**options), factory

    def test_single_statement_success_preserves_params_limit_and_setup_order(self):
        connection = FakeConnection(
            [{"amount": 1}, {"amount": 2}, {"amount": 3}]
        )
        executor, factory = self._single(
            connection,
            row_mapper=lambda row: {"amount": str(row["amount"])},
        )

        rows, truncated, source = executor.execute(
            "SELECT amount FROM fact WHERE id = %s", [17], 2
        )

        self.assertEqual([{"amount": "1"}, {"amount": "2"}], rows)
        self.assertTrue(truncated)
        self.assertEqual({"source": "warehouse-a", "read_only": True}, source)
        self.assertEqual(
            [
                ("SET SESSION time_zone = '+08:00'", None),
                ("SET SESSION MAX_EXECUTION_TIME = %s", (30000,)),
                ("START TRANSACTION READ ONLY", None),
                ("SELECT amount FROM fact WHERE id = %s", (17,)),
            ],
            connection.executions,
        )
        self.assertEqual(
            [{"connect_timeout_seconds": 30, "read_timeout_seconds": 30}],
            factory.calls,
        )
        self.assertEqual([3], connection.fetch_sizes)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        self.assertTrue(executor.closed)

    def test_locking_read_forms_are_rejected_before_connect(self):
        connection = FakeConnection([])
        executor, factory = self._single(connection)

        for sql in (
            "SELECT amount FROM fact FOR SHARE",
            "SELECT amount FROM fact LOCK IN SHARE MODE",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    executor.execute(sql, (), 1)

        self.assertEqual([], factory.calls)
        self.assertEqual([], connection.executions)

    def test_ordinary_cte_select_remains_allowed(self):
        connection = FakeConnection([{"value": 1}])
        executor, _factory = self._single(connection)

        rows, truncated, _source = executor.execute(
            "WITH fact AS (SELECT 1 AS value) SELECT value FROM fact",
            (),
            1,
        )

        self.assertEqual([{"value": 1}], rows)
        self.assertFalse(truncated)

    def test_deadline_controls_connection_and_statement_budgets(self):
        connection = FakeConnection([])
        executor, factory = self._single(
            connection,
            deadline_at=12.9,
            clock=SequenceClock(5.1, 5.2, 5.3, 5.4),
        )

        executor.execute("SELECT 1", (), 1)

        self.assertEqual(
            {"connect_timeout_seconds": 7, "read_timeout_seconds": 7},
            factory.calls[0],
        )
        self.assertIn(
            ("SET SESSION MAX_EXECUTION_TIME = %s", (7000,)),
            connection.executions,
        )

    def test_expired_deadline_does_not_connect(self):
        factory = FakeConnectionFactory(FakeConnection([]))
        executor = db_executor.ReadOnlyDbExecutor(
            mode="single_statement",
            connection_factory=factory,
            confirm_read_only_transaction=lambda _connection: {},
            query_timeout_seconds=30,
            deadline_at=10.0,
            clock=lambda: 10.0,
        )

        with self.assertRaises(db_executor.DeadlineExceeded):
            executor.execute("SELECT 1", (), 1)
        self.assertEqual([], factory.calls)
        self.assertTrue(executor.closed)

    def test_connection_failure_is_not_wrapped(self):
        original = OSError("connect failed")
        factory = FakeConnectionFactory(error=original)
        executor = db_executor.ReadOnlyDbExecutor(
            mode="single_statement",
            connection_factory=factory,
            confirm_read_only_transaction=lambda _connection: {},
            query_timeout_seconds=30,
            clock=lambda: 0.0,
        )

        with self.assertRaises(OSError) as raised:
            executor.execute("SELECT 1", (), 1)
        self.assertIs(original, raised.exception)

    def test_statement_failure_is_not_wrapped_and_single_always_closes(self):
        original = ValueError("statement failed")
        connection = FakeConnection(
            [], fail_sql="SELECT broken", statement_error=original
        )
        executor, _factory = self._single(connection)

        with self.assertRaises(ValueError) as raised:
            executor.execute("SELECT broken", ["parameter"], 1)

        self.assertIs(original, raised.exception)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        self.assertTrue(executor.closed)
        self.assertEqual(
            {"source": "warehouse-a", "read_only": True},
            executor.source_evidence_ref,
        )

    def test_close_is_attempted_when_rollback_fails(self):
        connection = FakeConnection([], rollback_error=RuntimeError("rollback"))
        executor, _factory = self._single(connection)

        with self.assertLogs(db_executor.logger, level="WARNING") as captured:
            executor.execute("SELECT 1", (), 1)

        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        self.assertIn(
            "datasage_db_executor rollback_failed mode=single_statement "
            "error_type=RuntimeError",
            captured.output[0],
        )

    def test_snapshot_reuses_one_connection_and_one_source_reference(self):
        connection = FakeConnection([{"value": 1}], [{"value": 2}])
        factory = FakeConnectionFactory(connection)
        evidence = {"source": "warehouse-a", "read_only": True}
        confirmations = []

        def confirm(observed):
            confirmations.append(observed)
            observed.executions.append(("CONFIRM READ ONLY", None))
            return evidence

        executor = db_executor.ReadOnlyDbExecutor(
            mode="consistent_snapshot",
            connection_factory=factory,
            confirm_read_only_transaction=confirm,
            query_timeout_seconds=9,
            clock=lambda: 0.0,
        )

        with executor as opened:
            first = opened.execute("SELECT first", [1], 1)
            second = opened.execute("SELECT second", [2], 1)
            self.assertEqual(first.source_evidence_ref, second.source_evidence_ref)
            self.assertIsNotNone(opened.marker)
            self.assertFalse(opened.closed)

        self.assertEqual(1, len(factory.calls))
        self.assertEqual([connection], confirmations)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)
        self.assertEqual(2, connection.fetch_sizes.count(2))
        self.assertEqual(
            [
                ("SET SESSION time_zone = '+08:00'", None),
                (
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                    None,
                ),
                (
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
                    None,
                ),
                ("CONFIRM READ ONLY", None),
                ("SET SESSION MAX_EXECUTION_TIME = %s", (9000,)),
                ("SELECT first", (1,)),
                ("SET SESSION MAX_EXECUTION_TIME = %s", (9000,)),
                ("SELECT second", (2,)),
            ],
            connection.executions,
        )

    def test_deadlines_after_connect_and_fetch_close_single_statement(self):
        after_connect = FakeConnection([])
        executor, _factory = self._single(
            after_connect,
            deadline_at=5.0,
            clock=SequenceClock(0.0, 5.0),
        )
        with self.assertRaises(db_executor.DeadlineExceeded):
            executor.execute("SELECT 1", (), 1)
        # Expired connections close directly; no new network rollback is started.
        self.assertEqual(0, after_connect.rollback_count)
        self.assertEqual(1, after_connect.close_count)

        after_fetch = FakeConnection([{"value": 1}])
        executor, _factory = self._single(
            after_fetch,
            deadline_at=5.0,
            clock=lambda: 6.0 if after_fetch.fetch_sizes else 0.0,
        )
        with self.assertRaises(db_executor.DeadlineExceeded):
            executor.execute("SELECT 1", (), 1)
        self.assertEqual([2], after_fetch.fetch_sizes)
        self.assertEqual(0, after_fetch.rollback_count)
        self.assertEqual(1, after_fetch.close_count)

    def test_snapshot_deadline_override_poison_keeps_source_and_closes(self):
        connection = FakeConnection([])
        executor = db_executor.ReadOnlyDbExecutor(
            mode="consistent_snapshot",
            connection_factory=FakeConnectionFactory(connection),
            confirm_read_only_transaction=lambda _connection: {"source": "a"},
            query_timeout_seconds=10,
            clock=lambda: 10.0,
        )

        with executor:
            with self.assertRaises(db_executor.DeadlineExceeded) as first:
                executor.execute("SELECT late", (), 1, deadline_at=10.0)
            with self.assertRaises(db_executor.DeadlineExceeded) as second:
                executor.execute("SELECT later", (), 1)
            self.assertIs(first.exception, second.exception)
            self.assertEqual({"source": "a"}, executor.source_evidence_ref)

        self.assertNotIn(("SELECT late", ()), connection.executions)
        self.assertNotIn(("SELECT later", ()), connection.executions)
        self.assertEqual(0, connection.rollback_count)
        self.assertEqual(1, connection.close_count)

    def test_snapshot_setup_failure_closes_connection(self):
        original = RuntimeError("snapshot setup failed")
        connection = FakeConnection(
            [],
            fail_sql="START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            statement_error=original,
        )
        executor = db_executor.ReadOnlyDbExecutor(
            mode="consistent_snapshot",
            connection_factory=FakeConnectionFactory(connection),
            confirm_read_only_transaction=lambda _connection: {"source": "a"},
            query_timeout_seconds=10,
            clock=lambda: 0.0,
        )

        with self.assertRaises(RuntimeError) as caught:
            executor.__enter__()
        self.assertIs(original, caught.exception)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)

    def test_snapshot_statement_failure_poison_preserves_original_exception(self):
        original = RuntimeError("driver rejected statement")
        connection = FakeConnection(
            [], fail_sql="SELECT broken", statement_error=original
        )
        factory = FakeConnectionFactory(connection)
        executor = db_executor.ReadOnlyDbExecutor(
            mode="consistent_snapshot",
            connection_factory=factory,
            confirm_read_only_transaction=lambda _connection: {"source": "a"},
            query_timeout_seconds=10,
            clock=lambda: 0.0,
        )

        with executor:
            with self.assertRaises(RuntimeError) as first:
                executor.execute("SELECT broken", (), 1)
            with self.assertRaises(RuntimeError) as second:
                executor.execute("SELECT later", (), 1)

        self.assertIs(original, first.exception)
        self.assertIs(original, second.exception)
        self.assertNotIn(("SELECT later", ()), connection.executions)

    def test_source_evidence_is_copied_at_each_boundary(self):
        evidence = {"source": "warehouse-a", "read_only": True}
        connection = FakeConnection([{"value": 1}])
        executor, _factory = self._single(
            connection,
            confirm_read_only_transaction=lambda _connection: evidence,
        )

        result = executor.execute("SELECT 1", (), 1)
        evidence["source"] = "mutated"
        result.source_evidence_ref["source"] = "also-mutated"

        self.assertEqual("warehouse-a", executor.source_evidence_ref["source"])

    def test_security_confirmation_failure_is_raw_and_closes_connection(self):
        original = PermissionError("identity changed")
        connection = FakeConnection([])
        executor, _factory = self._single(
            connection,
            confirm_read_only_transaction=lambda _connection: (_ for _ in ()).throw(
                original
            ),
        )

        with self.assertRaises(PermissionError) as raised:
            executor.execute("SELECT 1", (), 1)

        self.assertIs(original, raised.exception)
        self.assertEqual(1, connection.rollback_count)
        self.assertEqual(1, connection.close_count)


if __name__ == "__main__":
    unittest.main()
