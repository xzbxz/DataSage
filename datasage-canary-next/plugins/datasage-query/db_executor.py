"""Narrow read-only database execution lifecycle shared by query modes.

This module deliberately does not own public error codes, connection settings,
source-identity policy, or business-value serialization.  Those policies stay
at the composition boundary and are supplied as dependencies.  Driver and
security exceptions are allowed to cross this boundary unchanged.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any, Callable, Literal, Mapping, NamedTuple, Protocol, Sequence


logger = logging.getLogger(__name__)

ExecutionMode = Literal["single_statement", "consistent_snapshot"]


class ConnectionFactory(Protocol):
    def __call__(
        self,
        *,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
    ) -> Any: ...


class DeadlineExceeded(TimeoutError):
    """Internal deadline signal for the caller to map into its public taxonomy."""


class StatementResult(NamedTuple):
    rows: list[dict[str, Any]]
    truncated: bool
    source_evidence_ref: dict[str, Any]


class ReadOnlyDbExecutor:
    """Run one statement or a linked group in a read-only MySQL transaction.

    ``single_statement`` opens and closes around exactly one ``execute`` call.
    ``consistent_snapshot`` must be used as a context manager and reuses one
    repeatable-read snapshot until the context exits.
    """

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        connection_factory: ConnectionFactory,
        confirm_read_only_transaction: Callable[[Any], Mapping[str, Any]],
        query_timeout_seconds: int,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        row_mapper: Callable[[Mapping[str, Any]], dict[str, Any]] = dict,
    ) -> None:
        if mode not in {"single_statement", "consistent_snapshot"}:
            raise ValueError(f"unsupported database execution mode: {mode!r}")
        if (
            isinstance(query_timeout_seconds, bool)
            or not isinstance(query_timeout_seconds, int)
            or query_timeout_seconds < 1
        ):
            raise ValueError("query_timeout_seconds must be a positive integer")

        self.mode = mode
        self.deadline_at = deadline_at
        self._connection_factory = connection_factory
        self._confirm_read_only_transaction = confirm_read_only_transaction
        self._query_timeout_seconds = query_timeout_seconds
        self._clock = clock
        self._row_mapper = row_mapper

        self._connection: Any = None
        self._opened = False
        self._closed = False
        self._poisoned_error: Exception | None = None
        self._source_evidence_ref: dict[str, Any] | None = None
        self.marker: str | None = None

    @property
    def source_evidence_ref(self) -> dict[str, Any] | None:
        if self._source_evidence_ref is None:
            return None
        return dict(self._source_evidence_ref)

    @property
    def closed(self) -> bool:
        return self._closed

    def _remaining(self, deadline_at: float | None) -> float | None:
        if deadline_at is None:
            return None
        return deadline_at - self._clock()

    def _timeout_budget(self, remaining: float | None) -> int:
        if remaining is None:
            return self._query_timeout_seconds
        return min(self._query_timeout_seconds, max(1, math.floor(remaining)))

    def _open(self, *, deadline_at: float | None) -> None:
        if self._opened or self._closed:
            raise RuntimeError("database executor cannot be opened in its current state")

        try:
            remaining = self._remaining(deadline_at)
            if remaining is not None and remaining <= 0:
                raise DeadlineExceeded(
                    "database execution deadline exceeded before connect"
                )
            connect_budget = self._timeout_budget(remaining)
            self._connection = self._connection_factory(
                connect_timeout_seconds=connect_budget,
                read_timeout_seconds=connect_budget,
            )
            remaining = self._remaining(deadline_at)
            if remaining is not None and remaining <= 1:
                raise DeadlineExceeded(
                    "database execution deadline exceeded after connect"
                )

            with self._connection.cursor() as cursor:
                cursor.execute("SET SESSION time_zone = '+08:00'")
                if self.mode == "single_statement":
                    query_timeout = self._timeout_budget(remaining)
                    cursor.execute(
                        "SET SESSION MAX_EXECUTION_TIME = %s",
                        (query_timeout * 1000,),
                    )
                    cursor.execute("START TRANSACTION READ ONLY")
                else:
                    cursor.execute(
                        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                    cursor.execute(
                        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
                    )

            evidence = self._confirm_read_only_transaction(self._connection)
            self._source_evidence_ref = dict(evidence)
            if self.mode == "consistent_snapshot":
                self.marker = f"snapshot_group_{uuid.uuid4().hex}"
            self._opened = True
        except Exception:
            self.close()
            raise

    def execute(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None = None,
    ) -> StatementResult:
        effective_deadline = (
            deadline_at if deadline_at is not None else self.deadline_at
        )
        if self.mode == "single_statement":
            self._open(deadline_at=effective_deadline)
            try:
                return self._execute_open_statement(
                    sql,
                    params,
                    limit,
                    deadline_at=effective_deadline,
                    set_statement_timeout=False,
                )
            finally:
                self.close()

        if self._poisoned_error is not None:
            raise self._poisoned_error
        if not self._opened or self._closed:
            raise RuntimeError(
                "consistent_snapshot executor must be entered before execution"
            )
        try:
            return self._execute_open_statement(
                sql,
                params,
                limit,
                deadline_at=effective_deadline,
                set_statement_timeout=True,
            )
        except Exception as exc:
            self._poisoned_error = exc
            raise

    def _execute_open_statement(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None,
        set_statement_timeout: bool,
    ) -> StatementResult:
        remaining = self._remaining(deadline_at)
        if remaining is not None and remaining <= 0:
            raise DeadlineExceeded("database execution deadline exceeded before statement")
        query_timeout = self._timeout_budget(remaining)

        with self._connection.cursor() as cursor:
            if set_statement_timeout:
                cursor.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s",
                    (query_timeout * 1000,),
                )
            cursor.execute(sql, tuple(params))
            raw_rows = cursor.fetchmany(limit + 1)

        if deadline_at is not None and self._clock() > deadline_at:
            raise DeadlineExceeded("database execution deadline exceeded after statement")
        if self._source_evidence_ref is None:
            raise RuntimeError("read-only source evidence is unavailable")

        truncated = len(raw_rows) > limit
        rows = [self._row_mapper(row) for row in raw_rows[:limit]]
        return StatementResult(rows, truncated, dict(self._source_evidence_ref))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        if connection is None:
            return
        try:
            connection.rollback()
        except Exception as exc:
            logger.warning(
                "datasage_db_executor rollback_failed mode=%s error_type=%s",
                self.mode,
                type(exc).__name__,
            )
        try:
            connection.close()
        except Exception as exc:
            logger.warning(
                "datasage_db_executor connection_close_failed mode=%s error_type=%s",
                self.mode,
                type(exc).__name__,
            )

    def __enter__(self) -> "ReadOnlyDbExecutor":
        if self.mode != "consistent_snapshot":
            raise RuntimeError(
                "context management is only supported for consistent_snapshot mode"
            )
        self._open(deadline_at=self.deadline_at)
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
