"""Shared database-driver loading and connection security adapter.

The adapter owns PyMySQL provenance checks and connection establishment.  A
query-executor bridge is deliberately injected by the composition layer so
entity resolution can reuse the governed executor without importing it.
"""

from __future__ import annotations

import importlib
import logging
import os
import ssl
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from . import settings
from .db_security import (
    DatabaseSecurityError,
    mysql_tls_kwargs,
    verify_mysql_read_only_grants,
    verify_mysql_source_identity,
    verify_mysql_tls,
)


logger = logging.getLogger(__name__)
_PYMYSQL_IMPORT_LOCK = threading.RLock()
_QUERY_EXECUTOR: Callable[..., tuple[list[dict[str, Any]], bool]] | None = None


class DatabaseRuntimeError(Exception):
    def __init__(self, code: str, message: str, *, stage: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage


def install_query_executor(
    executor: Callable[..., tuple[list[dict[str, Any]], bool]],
) -> None:
    """Install the governed executor at the plugin composition boundary."""

    global _QUERY_EXECUTOR
    _QUERY_EXECUTOR = executor


def execute(
    sql: str,
    params: Sequence[Any],
    limit: int,
    *,
    deadline_at: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    executor = _QUERY_EXECUTOR
    if executor is None:
        raise DatabaseRuntimeError(
            "DATABASE_RUNTIME_UNAVAILABLE",
            "governed database query executor is unavailable",
        )
    return executor(sql, params, limit, deadline_at=deadline_at)


def _validate_pymysql_module(module: Any, vendor_root: Path):
    origin_text = str(getattr(module, "__file__", "") or "")
    if not origin_text:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED", "PyMySQL module provenance is unavailable"
        )
    origin = Path(origin_text).resolve()
    version = tuple(getattr(module, "VERSION", ())[:3])
    connect_callable = getattr(module, "connect", None)
    cursors = getattr(module, "cursors", None)
    if (
        not origin.is_relative_to(vendor_root)
        or version != (1, 2, 0)
        or not callable(connect_callable)
        or cursors is None
        or not hasattr(cursors, "SSDictCursor")
    ):
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED",
            "PyMySQL provenance, version, or module integrity is invalid",
        )
    return module


def load_pymysql():
    vendor_root = (Path(__file__).resolve().parent / "vendor").resolve()
    package_root = vendor_root / "pymysql"
    if not package_root.is_dir():
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNAVAILABLE", "vendored PyMySQL is unavailable"
        )
    with _PYMYSQL_IMPORT_LOCK:
        loaded = sys.modules.get("pymysql")
        if loaded is not None:
            return _validate_pymysql_module(loaded, vendor_root)
        sys.path.insert(0, str(vendor_root))
        try:
            pymysql = importlib.import_module("pymysql")
        except ImportError as exc:
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNAVAILABLE", "vendored PyMySQL could not be loaded"
            ) from exc
        finally:
            try:
                sys.path.remove(str(vendor_root))
            except ValueError:
                pass
        return _validate_pymysql_module(pymysql, vendor_root)


def connection_port() -> int:
    raw = os.environ.get("DATA_QUERY_MYSQL_PORT", "").strip()
    if not raw:
        return 3306
    try:
        value = int(raw)
    except ValueError as exc:
        raise DatabaseRuntimeError("INVALID_INPUT", "invalid database port") from exc
    if not 1 <= value <= 65535:
        raise DatabaseRuntimeError("INVALID_INPUT", "database port is out of range")
    return value


def connect(
    *,
    connect_timeout_seconds: int | None = None,
    read_timeout_seconds: int | None = None,
    timeout_seconds: int | None = None,
):
    pymysql = load_pymysql()
    required = {
        "host": os.environ.get("DATA_QUERY_MYSQL_HOST", "").strip(),
        "database": os.environ.get("DATA_QUERY_MYSQL_DATABASE", "").strip(),
        "user": os.environ.get("DATA_QUERY_MYSQL_USER", "").strip(),
        "password": os.environ.get("DATA_QUERY_MYSQL_PASSWORD", ""),
    }
    if not all(required.values()):
        raise DatabaseRuntimeError(
            "CONFIGURATION_MISSING", "database connection configuration is incomplete"
        )
    connect_timeout = settings.get_int("mysql_connect_timeout_seconds", 8, 1, 60)
    query_timeout = settings.get_int("mysql_query_timeout_seconds", 30, 1, 300)
    if timeout_seconds is not None:
        connect_timeout = min(connect_timeout, timeout_seconds)
        query_timeout = min(query_timeout, timeout_seconds)
    if connect_timeout_seconds is not None:
        connect_timeout = min(connect_timeout, connect_timeout_seconds)
    if read_timeout_seconds is not None:
        query_timeout = read_timeout_seconds
    connection = None
    try:
        port = connection_port()
        connection = pymysql.connect(
            host=required["host"],
            port=port,
            database=required["database"],
            user=required["user"],
            password=required["password"],
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=connect_timeout,
            read_timeout=query_timeout,
            write_timeout=query_timeout,
            cursorclass=pymysql.cursors.SSDictCursor,
            **mysql_tls_kwargs(),
        )
        tls_evidence = verify_mysql_tls(connection)
        grant_evidence = verify_mysql_read_only_grants(connection)
        connection._datasage_security_evidence = {
            **tls_evidence,
            **grant_evidence,
            "live_connection_verified": True,
        }
        verify_mysql_source_identity(
            connection,
            tls_evidence=tls_evidence,
            grant_evidence=grant_evidence,
            configured_identity={
                "host": required["host"],
                "port": port,
                "database": required["database"],
                "user": required["user"],
            },
        )
        if tls_evidence["transport_mode"] == "plaintext":
            logger.warning(
                "datasage_database_plaintext_transport production_mode=false "
                "tls_verified=false"
            )
        logger.info(
            "datasage_database_security transport_mode=%s tls_required=%s "
            "tls_configured=%s tls_verified=%s tls_protocol=%s tls_cipher=%s "
            "grants_verified=%s grant_policy=%s observed_privileges=%s",
            tls_evidence["transport_mode"],
            tls_evidence["tls_required"],
            tls_evidence["tls_configured"],
            tls_evidence["tls_verified"],
            tls_evidence["tls_protocol"],
            tls_evidence["tls_cipher"],
            grant_evidence["grants_verified"],
            grant_evidence.get("grant_policy", "strict_object_read_only"),
            ",".join(
                grant_evidence.get(
                    "observed_privileges", grant_evidence["read_only_privileges"]
                )
            ),
        )
        return connection
    except DatabaseSecurityError as exc:
        if connection is not None:
            connection.close()
        raise DatabaseRuntimeError(exc.code, str(exc), stage="database_security") from exc
    except Exception as exc:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if isinstance(exc, DatabaseRuntimeError):
            raise
        if isinstance(exc, ssl.SSLCertVerificationError):
            detail = str(getattr(exc, "verify_message", "") or exc).casefold()
            code = (
                "DATABASE_TLS_IDENTITY_INVALID"
                if "hostname" in detail
                or "ip address mismatch" in detail
                or "doesn't match" in detail
                else "DATABASE_TLS_CERTIFICATE_INVALID"
            )
            raise DatabaseRuntimeError(
                code,
                "database TLS certificate or server identity verification failed",
                stage="database_security",
            ) from exc
        errno = exc.args[0] if getattr(exc, "args", ()) else None
        if errno == 2026:
            raise DatabaseRuntimeError(
                "DATABASE_TLS_NEGOTIATION_FAILED",
                "database server did not complete required TLS negotiation",
                stage="database_security",
            ) from exc
        raise
