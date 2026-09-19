"""Offline tests for the bounded slow-task continuation policy."""

from __future__ import annotations

import unittest
import importlib

import test_business_contracts as base

slow_task_retry = importlib.import_module(base.TEST_PACKAGE + ".slow_task_retry")


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class Progress:
    def __init__(self, **statuses: str) -> None:
        self.data = {"components": {key: {"status": value} for key, value in statuses.items()}}


class ComponentFailure(Exception):
    def __init__(self) -> None:
        super().__init__("DELIVERY_COMPONENT_FAILED")


class UploadFailure(Exception):
    def __init__(self, *, no_send_confirmed: bool = False) -> None:
        super().__init__("UPLOAD_FAILED")
        self.no_send_confirmed = no_send_confirmed


class SlowTaskRetryTests(unittest.TestCase):
    def budget(self, *, max_retries: int = 2, max_elapsed_seconds: float = 120):
        clock = FakeClock()
        return slow_task_retry.RetryBudget(
            max_retries=max_retries,
            max_elapsed_seconds=max_elapsed_seconds,
            clock=clock.now,
            sleep=clock.sleep,
        ), clock

    def test_failed_component_retries_with_5_then_15_seconds(self):
        budget, clock = self.budget()
        attempts = []

        def action():
            attempts.append(1)
            if len(attempts) < 3:
                raise ComponentFailure()
            return "completed"

        self.assertEqual("completed", budget.run(action, Progress(text="provider_accepted", file="failed")))
        self.assertEqual(3, len(attempts))
        self.assertEqual([5.0, 15.0], clock.sleeps)
        self.assertEqual(20.0, clock.value)

    def test_budget_is_shared_across_component_actions(self):
        budget, clock = self.budget()
        attempts = []

        def failing():
            attempts.append("failed-component")
            raise ComponentFailure()

        with self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
            budget.run(failing, Progress(file="failed"))
        self.assertEqual(3, len(attempts))
        with self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
            budget.run(failing, Progress(file="failed"))
        self.assertEqual(4, len(attempts))
        self.assertEqual([5.0, 15.0], clock.sleeps)

    def test_unknown_inflight_unverified_and_not_delivered_never_retry(self):
        for state in ("unknown", "in_flight", "unverified_success", "not_delivered", "unrecognized_legacy_ok"):
            budget, clock = self.budget()
            attempts = []

            def action():
                attempts.append(1)
                raise ComponentFailure()

            with self.subTest(state=state), self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
                budget.run(action, Progress(text=state, file="failed"))
            self.assertEqual(1, len(attempts))
            self.assertEqual([], clock.sleeps)

    def test_timeout_and_preparation_errors_never_retry(self):
        for error in (TimeoutError("timeout"), ValueError("CONFIG_INVALID"), RuntimeError("SCHEDULE_DISABLED")):
            budget, clock = self.budget()
            attempts = []

            def action(error=error):
                attempts.append(1)
                raise error

            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                budget.run(action, Progress(file="failed"))
            self.assertEqual(1, len(attempts))
            self.assertEqual([], clock.sleeps)

    def test_upload_failure_never_retries_from_dynamic_marker(self):
        budget, clock = self.budget(max_retries=1)
        attempts = []

        def action():
            attempts.append(1)
            raise UploadFailure(no_send_confirmed=True)

        with self.assertRaisesRegex(UploadFailure, "UPLOAD_FAILED"):
            budget.run(action, Progress(file="failed"))
        self.assertEqual([1], attempts)
        self.assertEqual([], clock.sleeps)

    def test_component_failure_without_explicit_failed_record_never_retries(self):
        for progress in (Progress(), {"components": {"file": {}}}, {"components": {"file": "failed"}}):
            budget, clock = self.budget()
            attempts = []

            def action():
                attempts.append(1)
                raise ComponentFailure()

            with self.subTest(progress=progress), self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
                budget.run(action, progress)
            self.assertEqual([1], attempts)
            self.assertEqual([], clock.sleeps)

    def test_elapsed_budget_exhaustion_reraises_original_error(self):
        budget, clock = self.budget(max_elapsed_seconds=10)
        attempts = []

        def action():
            attempts.append(1)
            raise ComponentFailure()

        with self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
            budget.run(action, Progress(file="failed"))
        self.assertEqual(2, len(attempts))
        self.assertEqual([5.0], clock.sleeps)

    def test_sleep_waking_past_deadline_does_not_retry(self):
        clock = FakeClock()
        budget = slow_task_retry.RetryBudget(
            max_retries=2,
            max_elapsed_seconds=10,
            clock=clock.now,
            sleep=lambda seconds: (clock.sleeps.append(seconds), setattr(clock, "value", 11.0)),
        )
        attempts = []

        def action():
            attempts.append(1)
            raise ComponentFailure()

        with self.assertRaisesRegex(ComponentFailure, "DELIVERY_COMPONENT_FAILED"):
            budget.run(action, Progress(file="failed"))
        self.assertEqual([1], attempts)
        self.assertEqual([5.0], clock.sleeps)

    def test_failed_text_with_accepted_text_is_safe_for_file_closure(self):
        budget, _clock = self.budget(max_retries=1)
        calls = []

        def file_only_action():
            calls.append("file")
            if len(calls) == 1:
                raise ComponentFailure()
            return "file-complete"

        self.assertEqual(
            "file-complete",
            budget.run(file_only_action, Progress(text="provider_accepted", file="failed")),
        )
        self.assertEqual(["file", "file"], calls)

    def test_no_state_queue_or_notification_side_effects(self):
        budget, clock = self.budget()
        state = {"writes": 0, "notifications": 0}

        def action():
            raise ComponentFailure()

        with self.assertRaises(ComponentFailure):
            budget.run(action, Progress(file="failed"))
        self.assertEqual(0, state["writes"])
        self.assertEqual(0, state["notifications"])
        self.assertEqual([5.0, 15.0], clock.sleeps)


if __name__ == "__main__":
    unittest.main()
