"""DataSage read-only execution adapter.

The low-level executor and runtime own connection, TLS, deadline, and close
semantics. This module only preserves the existing DataSage error/evidence
adapter and snapshot wrapper.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from . import db_executor, db_runtime, settings
from .db_security import (
    DatabaseSecurityError,
    confirm_mysql_read_only_transaction,
)
from .query_errors import QueryFailure

# Reuse the existing settings owner without adding another wrapper contract.
_bounded_int = settings.get_int

def _connect(
    *,
    connect_timeout_seconds: int | None = None,
    read_timeout_seconds: int | None = None,
    timeout_seconds: int | None = None,
):
    try:
        return db_runtime.connect(
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            timeout_seconds=timeout_seconds,
        )
    except db_runtime.DatabaseRuntimeError as exc:
        messages = {
            "CONFIGURATION_MISSING": "数据库连接配置不完整。",
            "INVALID_INPUT": "数据库连接配置无效。",
            "DEPENDENCY_UNAVAILABLE": "查询组件缺少随制品分发的 PyMySQL，当前未连接数据库。",
            "DEPENDENCY_UNTRUSTED": "PyMySQL 来源、版本或模块完整性与制品清单不一致。",
            "DATABASE_TLS_CERTIFICATE_INVALID": "数据库 TLS 证书或服务端身份验证失败。",
            "DATABASE_TLS_IDENTITY_INVALID": "数据库 TLS 证书或服务端身份验证失败。",
            "DATABASE_TLS_NEGOTIATION_FAILED": "数据库服务端未完成强制 TLS 协商。",
        }
        raise QueryFailure(
            exc.code,
            messages.get(exc.code, exc.message),
            stage=exc.stage,
        ) from exc

def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _bounded_int("max_cell_chars", 2000, 100, 20000):
            raise QueryFailure("OUTPUT_TOO_LARGE", "查询结果包含超长文本，已停止向模型传递。")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "查询结果包含非有限数值，已停止生成公开证据。",
                stage="result_validation",
            )
        return value
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "查询结果包含非有限数值，已停止生成公开证据。",
                stage="result_validation",
            )
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return _json_value(value.decode("utf-8", errors="replace"))
    return _json_value(str(value))

def _execute_with_source(
    sql: str,
    params: Sequence[Any],
    limit: int,
    *,
    deadline_at: float | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    executor = db_executor.ReadOnlyDbExecutor(
        mode="single_statement",
        connection_factory=_connect,
        confirm_read_only_transaction=confirm_mysql_read_only_transaction,
        query_timeout_seconds=_bounded_int(
            "mysql_query_timeout_seconds", 30, 1, 300
        ),
        deadline_at=deadline_at,
        row_mapper=lambda row: {
            str(key): _json_value(value) for key, value in row.items()
        },
    )
    try:
        result = executor.execute(sql, params, limit)
        return result.rows, result.truncated, result.source_evidence_ref
    except DatabaseSecurityError as exc:
        raise QueryFailure(
            "DATABASE_IDENTITY_CHANGED",
            "Database source identity changed during query execution.",
            stage="database_security",
            source_evidence_ref=executor.source_evidence_ref,
        ) from exc
    except db_executor.DeadlineExceeded as exc:
        raise QueryFailure(
            "BATCH_DEADLINE_EXCEEDED",
            "本次批量查询已达到总时限。",
            timeout=True,
            source_evidence_ref=executor.source_evidence_ref,
        ) from exc
    except QueryFailure as failure:
        if executor.source_evidence_ref is not None:
            failure.source_evidence_ref = executor.source_evidence_ref
        raise
    except Exception as exc:
        mapped = _database_query_failure(exc)
        failure = QueryFailure(
            mapped.code,
            "查询超时。" if mapped.timeout else "数据库查询失败。",
            timeout=mapped.timeout,
        )
        failure.source_evidence_ref = executor.source_evidence_ref
        raise failure from exc

def _database_query_failure(exc: Exception) -> QueryFailure:
    """Map driver failures without changing the public retry taxonomy."""

    text = str(exc).lower()
    error_code = exc.args[0] if getattr(exc, "args", ()) else None
    server_timeout = (
        error_code == 3024
        or "maximum statement execution time exceeded" in text
    )
    timeout = (
        server_timeout
        or isinstance(exc, TimeoutError)
        or "timeout" in text
        or "timed out" in text
    )
    return QueryFailure(
        (
            "SERVER_STATEMENT_TIMEOUT"
            if server_timeout
            else "QUERY_TIMEOUT"
            if timeout
            else "QUERY_FAILED"
        ),
        "Query timed out." if timeout else "Database query failed.",
        timeout=timeout,
    )

class _ConsistentSnapshotExecutor:
    """Execute a linked evidence group on one read-only consistent snapshot."""

    def __init__(self, *, deadline_at: float | None = None) -> None:
        self.deadline_at = deadline_at
        self._executor: db_executor.ReadOnlyDbExecutor | None = None
        self.marker: str | None = None
        self.closed = False
        self.poisoned_failure: QueryFailure | None = None
        self.source_evidence_ref: dict[str, Any] | None = None

    def __enter__(self) -> "_ConsistentSnapshotExecutor":
        try:
            self._executor = db_executor.ReadOnlyDbExecutor(
                mode="consistent_snapshot",
                connection_factory=_connect,
                confirm_read_only_transaction=confirm_mysql_read_only_transaction,
                query_timeout_seconds=_bounded_int(
                    "mysql_query_timeout_seconds", 30, 1, 300
                ),
                deadline_at=self.deadline_at,
                row_mapper=lambda row: {
                    str(key): _json_value(value) for key, value in row.items()
                },
            )
            self._executor.__enter__()
            self.marker = self._executor.marker
            self.source_evidence_ref = self._executor.source_evidence_ref
            return self
        except DatabaseSecurityError as exc:
            self.close()
            raise QueryFailure(
                "DATABASE_IDENTITY_CHANGED",
                "Database source identity changed during query execution.",
                stage="database_security",
                source_evidence_ref=self.source_evidence_ref,
            ) from exc
        except db_executor.DeadlineExceeded as exc:
            self.close()
            raise QueryFailure(
                "BATCH_DEADLINE_EXCEEDED",
                "The batch query deadline has been exceeded.",
                timeout=True,
                source_evidence_ref=self.source_evidence_ref,
            ) from exc
        except QueryFailure as failure:
            self.close()
            raise
        except Exception as exc:
            failure = _database_query_failure(exc)
            self.close()
            raise failure from exc

    def execute(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        try:
            if self.poisoned_failure is not None:
                poisoned = self.poisoned_failure
                raise QueryFailure(
                    poisoned.code,
                    poisoned.message,
                    timeout=poisoned.timeout,
                    stage=poisoned.stage,
                    retryable=poisoned.retryable,
                    source_evidence_ref=self.source_evidence_ref,
                )
            if (
                self._executor is None
                or self.closed
                or self.marker is None
                or self.source_evidence_ref is None
            ):
                raise QueryFailure(
                    "QUERY_FAILED",
                    "Database query failed.",
                )
            result = self._executor.execute(
                sql,
                params,
                limit,
                deadline_at=deadline_at,
            )
            return result.rows, result.truncated, result.source_evidence_ref
        except db_executor.DeadlineExceeded as exc:
            failure = QueryFailure(
                "BATCH_DEADLINE_EXCEEDED",
                "The batch query deadline has been exceeded.",
                timeout=True,
                source_evidence_ref=self.source_evidence_ref,
            )
            self._remember_failure(failure)
            raise failure from exc
        except QueryFailure as failure:
            self._remember_failure(failure)
            raise
        except Exception as exc:
            failure = _database_query_failure(exc)
            self._remember_failure(failure)
            raise failure from exc

    def _remember_failure(self, failure: QueryFailure) -> None:
        if self.source_evidence_ref is not None:
            failure.source_evidence_ref = dict(self.source_evidence_ref)
        if self.poisoned_failure is None:
            self.poisoned_failure = QueryFailure(
                failure.code,
                failure.message,
                timeout=failure.timeout,
                stage=failure.stage,
                retryable=failure.retryable,
                source_evidence_ref=failure.source_evidence_ref,
            )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._executor is not None:
            self._executor.close()

    def __exit__(self, *_args: Any) -> None:
        self.close()

def _consistent_snapshot_executor(
    *, deadline_at: float | None = None
) -> _ConsistentSnapshotExecutor:
    return _ConsistentSnapshotExecutor(deadline_at=deadline_at)
