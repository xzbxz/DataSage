"""R23: a deadline, a timer or a socket abort must not outlive its request.

The executor already bounds connection/statement budgets, closes on failure and releases
leased slots (covered in test_db_executor.py and the slot tests).  What is checked here is
the other half of the acceptance: a fresh request must not inherit the previous request's
deadline, and a completed request must leave no timer thread behind while an expired one
aborts its socket immediately and reports a timeout rather than a success or an empty value.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import threading
import time
import types
import unittest

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_r23_execution_tests"

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)
db_executor = importlib.import_module(f"{PACKAGE}.db_executor")


class _FakeSocket:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self, how) -> None:  # noqa: ARG002 - mirrors the socket API
        self.shutdown_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakeConnection:
    def __init__(self) -> None:
        self._sock = _FakeSocket()


class ExecutionBudgetIsolationTests(unittest.TestCase):
    def test_a_new_request_does_not_inherit_the_previous_deadline(self) -> None:
        self.assertIsNone(db_executor.current_deadline())

        with self.assertRaises(db_executor.DeadlineExceeded):
            with db_executor.deadline_scope(time.monotonic() - 1):
                deadline = db_executor.SocketDeadline(
                    _FakeConnection(), db_executor.current_deadline()
                )
                deadline.check()
        self.assertIsNone(
            db_executor.current_deadline(),
            "a timed-out request must leave no deadline for the next one",
        )

        outer = time.monotonic() + 60
        inner = time.monotonic() + 30
        with db_executor.deadline_scope(outer) as outer_effective:
            self.assertEqual(outer, outer_effective)
            with db_executor.deadline_scope(inner) as inner_effective:
                self.assertEqual(
                    inner, inner_effective, "a nested call keeps the tighter deadline"
                )
            self.assertEqual(outer, db_executor.current_deadline())
        self.assertIsNone(db_executor.current_deadline())

        with db_executor.deadline_scope(None) as inherited_none:
            self.assertIsNone(inherited_none)

    def test_a_completed_request_leaves_no_timer_thread_behind(self) -> None:
        connection = _FakeConnection()
        before = {thread.ident for thread in threading.enumerate()}
        deadline = db_executor.SocketDeadline(connection, time.monotonic() + 30)
        self.assertIsNotNone(deadline.timer)
        self.assertFalse(deadline.expired.is_set())

        deadline.stop()

        self.assertFalse(
            deadline.timer.is_alive(),
            "the deadline timer must not outlive the request",
        )
        after = {thread.ident for thread in threading.enumerate()}
        self.assertEqual(before, after, "no thread may outlive the request")
        self.assertEqual(0, connection._sock.shutdown_calls)
        self.assertEqual(0, connection._sock.close_calls)

    def test_an_expired_deadline_aborts_and_reports_a_timeout(self) -> None:
        connection = _FakeConnection()
        deadline = db_executor.SocketDeadline(connection, time.monotonic() - 1)

        self.assertTrue(deadline.expired.is_set())
        self.assertIsNone(deadline.timer, "an expired deadline needs no timer thread")
        self.assertEqual(1, connection._sock.shutdown_calls)
        self.assertEqual(1, connection._sock.close_calls)
        with self.assertRaises(db_executor.DeadlineExceeded):
            deadline.check()

    def test_a_timeout_is_never_a_success_or_an_empty_value(self) -> None:
        """An expired deadline raises the typed signal instead of yielding rows."""

        with db_executor.deadline_scope(time.monotonic() - 1) as scope:
            self.assertLess(scope, time.monotonic())
        with db_executor.deadline_scope(time.monotonic() - 1):
            connection = _FakeConnection()
            deadline = db_executor.SocketDeadline(
                connection, db_executor.current_deadline()
            )
            with self.assertRaises(db_executor.DeadlineExceeded) as caught:
                deadline.check()
        self.assertIn("deadline", str(caught.exception).lower())
        # The expired constructor already aborted once; check() aborts again on the way
        # out (the socket calls are swallowed and safe to repeat).
        self.assertGreaterEqual(connection._sock.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
