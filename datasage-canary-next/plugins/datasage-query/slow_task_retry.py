"""Small, local-only retry policy for an already prepared slow-task component.

This module deliberately does not call ``deliver_components``, create cron
jobs, write Progress, or rebuild/freeze business material.  The caller passes
an action closure that already selects the failed component and its existing
delivery path.  A single RetryBudget is shared by all component closures in
one slow-task invocation.
"""

from __future__ import annotations

import time
from typing import Any, Callable


_BLOCKING_PROGRESS_STATES = frozenset({"unknown", "in_flight", "unverified_success", "not_delivered"})
_COMPONENT_FAILURE = "DELIVERY_COMPONENT_FAILED"


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    text = str(error or "")
    return text.split(":", 1)[0].strip()


def _progress_states(progress: Any) -> list[str]:
    """Read progress without mutating it; unknown shapes fail closed."""
    if progress is None:
        return ["unknown"]
    data = getattr(progress, "data", None)
    if isinstance(data, dict) and isinstance(data.get("components"), dict):
        return [
            str(item.get("status") or "unknown") if isinstance(item, dict) else "unknown"
            for item in data["components"].values()
        ]
    components = getattr(progress, "components", None)
    if isinstance(components, dict):
        return [
            str(item.get("status") or "unknown") if isinstance(item, dict) else "unknown"
            for item in components.values()
        ]
    if isinstance(progress, dict) and isinstance(progress.get("components"), dict):
        return [
            str(item.get("status") or "unknown") if isinstance(item, dict) else "unknown"
            for item in progress["components"].values()
        ]
    return ["unknown"]


def _safe_component_failure(error: Exception, progress: Any) -> bool:
    code = _error_code(error)
    if code == _COMPONENT_FAILURE:
        states = _progress_states(progress)
        return "failed" in states and all(state in {"failed","provider_accepted","committed","not_attempted"} for state in states)
    return False


class RetryBudget:
    """At-most-two continuation retries with bounded elapsed time.

    ``clock`` and ``sleep`` are injectable only for offline tests.  The
    default policy waits 5 seconds before retry one and 15 seconds before
    retry two.  The budget is shared by every ``run`` call made on this object.
    """

    def __init__(
        self,
        max_retries: int = 2,
        max_elapsed_seconds: float = 120,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if max_elapsed_seconds < 0:
            raise ValueError("max_elapsed_seconds must be non-negative")
        self.max_retries = max_retries
        self.max_elapsed_seconds = float(max_elapsed_seconds)
        self.clock = clock
        self.sleep = sleep
        self.started_at = clock()
        self.retries_used = 0
        self.attempts = 0

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, float(self.clock() - self.started_at))

    def _delay_for_retry(self) -> float:
        return (5.0, 15.0)[min(self.retries_used, 1)]

    def run(self, action: Callable[[], Any], progress: Any) -> Any:
        """Run an existing component action under the shared continuation budget."""
        if not callable(action):
            raise TypeError("action must be callable")
        while True:
            self.attempts += 1
            try:
                return action()
            except Exception as error:
                if not _safe_component_failure(error, progress):
                    raise
                if self.retries_used >= self.max_retries:
                    raise
                delay = self._delay_for_retry()
                if self.elapsed_seconds + delay > self.max_elapsed_seconds:
                    raise
                self.sleep(delay)
                if self.elapsed_seconds > self.max_elapsed_seconds:
                    raise
                self.retries_used += 1


__all__ = ["RetryBudget"]
