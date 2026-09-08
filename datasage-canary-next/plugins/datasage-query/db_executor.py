"""Narrow read-only database execution lifecycle shared by query modes.

This module deliberately does not own public error codes, connection settings,
source-identity policy, or business-value serialization.  Those policies stay
at the composition boundary and are supplied as dependencies.  Driver and
security exceptions are allowed to cross this boundary unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import socket
import threading
import math
import time
import uuid
from typing import Any, Callable, Literal, Mapping, NamedTuple, Protocol, Sequence


logger = logging.getLogger(__name__)

MAX_EXECUTOR_ROWS = 10_000
_FORBIDDEN_SQL_WORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "CALL",
        "CREATE",
        "DELETE",
        "DO",
        "DROP",
        "GRANT",
        "HANDLER",
        "INSERT",
        "LOAD",
        "LOCK",
        "OPTIMIZE",
        "PURGE",
        "RENAME",
        "REPLACE",
        "REVOKE",
        "SET",
        "TRUNCATE",
        "UNINSTALL",
        "UPDATE",
        "XA",
    }
)
_FORBIDDEN_SQL_FUNCTIONS = frozenset(
    {"BENCHMARK", "GET_LOCK", "LOAD_FILE", "RELEASE_LOCK", "SLEEP"}
)


def _sql_words(sql: str) -> list[str]:
    """Tokenize enough SQL to reject comments, multiple statements and verbs."""

    words: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if character == ";" or character == "@":
            raise ValueError("SQL must be one SELECT statement without session variables")
        if character == "#" or sql.startswith("--", index) or sql.startswith("/*", index):
            raise ValueError("SQL comments are not allowed")
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            closed = False
            while index < length:
                current = sql[index]
                if current == "\\" and quote != "`":
                    index += 2
                    continue
                if current == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise ValueError("SQL contains an unterminated quoted value")
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] in {"_", "$"}):
                end += 1
            words.append(sql[index:end].upper())
            index = end
            continue
        if character == ":" and index + 1 < length and sql[index + 1] == "=":
            raise ValueError("SQL user-variable assignment is not allowed")
        index += 1
    return words


def _validate_read_only_statement(sql: Any, limit: Any) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty string")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_EXECUTOR_ROWS
    ):
        raise ValueError(
            f"limit must be an integer between 1 and {MAX_EXECUTOR_ROWS}"
        )
    words = _sql_words(sql)
    if not words or words[0] not in {"SELECT", "WITH"}:
        raise ValueError("only one SELECT or WITH query is allowed")
    if words[0] == "WITH" and "SELECT" not in words:
        raise ValueError("WITH query must contain a SELECT statement")
    if _FORBIDDEN_SQL_WORDS.intersection(words):
        raise ValueError("SQL contains a non-read-only statement")
    if _FORBIDDEN_SQL_FUNCTIONS.intersection(words):
        raise ValueError("SQL contains a side-effect or delay function")
    if "INTO" in words or "OUTFILE" in words or "DUMPFILE" in words:
        raise ValueError("SQL result redirection is not allowed")
    if "PROCEDURE" in words:
        raise ValueError("SQL contains an unsupported execution directive")
    if "FOR" in words and "SHARE" in words:
        raise ValueError("locking SELECT statements are not allowed")
    if "FOR" in words and "UPDATE" in words:
        raise ValueError("locking SELECT statements are not allowed")
    if "LOCK" in words:
        raise ValueError("locking statements are not allowed")

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


_CALL_DEADLINE: ContextVar[float | None] = ContextVar("datasage_call_deadline", default=None)


def current_deadline() -> float | None:
    return _CALL_DEADLINE.get()


@contextmanager
def deadline_scope(deadline_at):
    inherited = current_deadline()
    effective = inherited if deadline_at is None else deadline_at
    if inherited is not None and effective is not None:
        effective = min(inherited, effective)
    token = _CALL_DEADLINE.set(effective)
    try:
        yield effective
    finally:
        _CALL_DEADLINE.reset(token)


class SocketDeadline:
    """Interrupt this connection's blocking I/O; never leave a query future running."""
    def __init__(self, connection, deadline_at, *, clock=None):
        self.connection = connection
        self.deadline_at = deadline_at
        self.clock = clock or time.monotonic
        self.expired = threading.Event()
        self.timer = None
        if deadline_at is not None:
            remaining = deadline_at - self.clock()
            if remaining <= 0:
                self.abort()
            else:
                self.timer = threading.Timer(remaining, self.abort)
                self.timer.daemon = True
                self.timer.start()

    def abort(self):
        self.expired.set()
        sock = getattr(self.connection, "_sock", None)
        if sock is not None:
            # Do not close the buffered file here: its read lock belongs to
            # the query thread. Shutdown wakes that reader so it can unwind.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def check(self):
        if self.deadline_at is not None and (self.expired.is_set() or self.clock() >= self.deadline_at):
            self.abort()
            raise DeadlineExceeded("database execution deadline exceeded")

    def stop(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()


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

        self._watchdog = None
        self._effective_deadline = deadline_at
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
            self._effective_deadline = deadline_at
            with deadline_scope(deadline_at):
                self._connection = self._connection_factory(
                    connect_timeout_seconds=connect_budget,
                    read_timeout_seconds=connect_budget,
                )
            self._watchdog = SocketDeadline(self._connection, deadline_at, clock=self._clock)
            self._watchdog.check()
            remaining = self._remaining(deadline_at)
            if remaining is not None and remaining <= 0:
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
            self._watchdog.check()
            self._source_evidence_ref = dict(evidence)
            if self.mode == "consistent_snapshot":
                self.marker = f"snapshot_group_{uuid.uuid4().hex}"
            self._opened = True
        except Exception as exc:
            expired = (self._watchdog is not None and self._watchdog.expired.is_set()) or (deadline_at is not None and self._clock() >= deadline_at)
            self.close()
            if expired:
                raise DeadlineExceeded("database execution deadline exceeded during connect") from exc
            raise

    def execute(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None = None,
    ) -> StatementResult:
        # Validate before opening a connection so an invalid statement or
        # unbounded fetch cannot cause any database I/O.
        _validate_read_only_statement(sql, limit)
        candidates = [value for value in (deadline_at, self.deadline_at, self._effective_deadline, current_deadline()) if value is not None]
        effective_deadline = min(candidates) if candidates else None
        if (self.mode == "consistent_snapshot" and self._opened and not self._closed
                and self._poisoned_error is None and effective_deadline != self._effective_deadline):
            self._effective_deadline = effective_deadline
            if self._watchdog is not None:
                self._watchdog.stop()
            self._watchdog = SocketDeadline(self._connection, effective_deadline, clock=self._clock)
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
            except Exception as exc:
                if (self._watchdog is not None and self._watchdog.expired.is_set()) or (effective_deadline is not None and self._clock() >= effective_deadline):
                    raise DeadlineExceeded("database execution deadline exceeded during statement") from exc
                raise
            finally:
                self.close()
                if (self._watchdog is not None and self._watchdog.expired.is_set()) or (effective_deadline is not None and self._clock() >= effective_deadline):
                    raise DeadlineExceeded("database execution deadline exceeded during cleanup")

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
            if (self._watchdog is not None and self._watchdog.expired.is_set()) or (effective_deadline is not None and self._clock() >= effective_deadline):
                exc = DeadlineExceeded("database execution deadline exceeded during snapshot")
            self._poisoned_error = exc
            raise exc

    def _execute_open_statement(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None,
        set_statement_timeout: bool,
    ) -> StatementResult:
        _validate_read_only_statement(sql, limit)
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

        if deadline_at is not None and self._clock() >= deadline_at:
            raise DeadlineExceeded("database execution deadline exceeded after statement")
        if self._source_evidence_ref is None:
            raise RuntimeError("read-only source evidence is unavailable")

        truncated = len(raw_rows) > limit
        rows = []
        for row in raw_rows[:limit]:
            if self._watchdog is not None:
                self._watchdog.check()
            rows.append(self._row_mapper(row))
        if self._watchdog is not None:
            self._watchdog.check()
        return StatementResult(rows, truncated, dict(self._source_evidence_ref))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        try:
            if connection is None:
                return
            expired = (self._watchdog is not None and self._watchdog.expired.is_set()) or (self._effective_deadline is not None and self._clock() >= self._effective_deadline)
            if expired and self._watchdog is not None:
                self._watchdog.abort()
            if not expired:
                try:
                    connection.rollback()
                except Exception as exc:
                    logger.warning("datasage_db_executor rollback_failed mode=%s error_type=%s", self.mode, type(exc).__name__)
            try:
                connection.close()
            except Exception as exc:
                logger.warning("datasage_db_executor connection_close_failed mode=%s error_type=%s", self.mode, type(exc).__name__)
        finally:
            if self._watchdog is not None:
                self._watchdog.stop()

    def __enter__(self) -> "ReadOnlyDbExecutor":
        if self.mode != "consistent_snapshot":
            raise RuntimeError(
                "context management is only supported for consistent_snapshot mode"
            )
        self._open(deadline_at=self.deadline_at)
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
        if self._poisoned_error is None and self._effective_deadline is not None and self._clock() >= self._effective_deadline:
            raise DeadlineExceeded("database execution deadline exceeded during cleanup")
