"""Static and bounded live readiness checks for the query tool."""

from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any

from .db_security import (
    DatabaseSecurityError,
    canary_existing_account_accepted,
    mysql_tls_kwargs,
    mysql_tls_policy,
)
from . import settings
logger = logging.getLogger(__name__)
_LIVE_LOCK = threading.Lock()
_LIVE_CACHE: tuple[float, str, dict[str, Any]] | None = None
_LIVE_SUCCESS_TTL_SECONDS = 30.0
_LIVE_FAILURE_TTL_SECONDS = 10.0

_REQUIRED_DATABASE_ENV = (
    "DATA_QUERY_MYSQL_HOST",
    "DATA_QUERY_MYSQL_DATABASE",
    "DATA_QUERY_MYSQL_USER",
    "DATA_QUERY_MYSQL_PASSWORD",
)


class _ProfileRootFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _profile_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNAVAILABLE") from exc
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def runtime_identity_status(*, profile_root: Path | None = None) -> dict[str, Any]:
    """Verify only that the DataSage profile root path is safe.

    Hermes owns active-profile and runtime selection. Historical release
    manifests, imported-Hermes paths, and private launcher layouts are not
    DataSage runtime prerequisites. This bounded check cannot prove a Git
    commit/tree binding and reports that limitation explicitly. The database
    transport/grant/account gates below remain fail-closed.
    """

    git_binding = {
        "available": False,
        "commit": None,
        "tree": None,
        "reason_code": "GIT_BINDING_UNAVAILABLE",
    }

    candidate = _profile_root() if profile_root is None else Path(profile_root)
    try:
        root = candidate.resolve(strict=True)
        if not root.is_dir() or _is_reparse(candidate):
            raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNSAFE")
    except (OSError, _ProfileRootFailure) as exc:
        reason = (
            exc.code
            if isinstance(exc, _ProfileRootFailure)
            else "DATASAGE_PROFILE_ROOT_INVALID"
        )
        return {
            "ready": False,
            "state": "profile_path_integrity",
            "reason_code": reason,
            "path_integrity_verified": False,
            "git_binding": git_binding,
            "identity_override": False,
            "runtime": "hermes_managed",
        }

    return {
        "ready": True,
        "state": "profile_path_integrity",
        "reason_code": None,
        "path_integrity_verified": True,
        "git_binding": git_binding,
        "identity_override": False,
        "runtime": "hermes_managed",
    }


def _live_cache_key() -> str:
    """Invalidate live evidence whenever connection policy changes."""
    connection_names = (
        "DATA_QUERY_MYSQL_HOST",
        "DATA_QUERY_MYSQL_PORT",
        "DATA_QUERY_MYSQL_DATABASE",
        "DATA_QUERY_MYSQL_USER",
        "DATA_QUERY_MYSQL_PASSWORD",
        "DATA_QUERY_MYSQL_SSL_CA",
    )
    payload = json.dumps(
        {
            "connection": {
                name: os.environ.get(name, "")
                for name in connection_names
            },
            "policy": {
                "production_mode": settings.get_bool(
                    "production_mode",
                    False,
                ),
                "canary_accept_existing_account": settings.get_bool(
                    "canary_accept_existing_account",
                    False,
                ),
                "require_tls": settings.get_bool("require_tls", False),
                "mysql_allowed_grant_scopes": settings.get_list(
                    "mysql_allowed_grant_scopes"
                ),
                "mysql_health_connect_timeout_seconds": settings.get_int(
                    "mysql_health_connect_timeout_seconds",
                    5,
                    1,
                    60,
                ),
                "mysql_grant_audit_timeout_seconds": settings.get_int(
                    "mysql_grant_audit_timeout_seconds",
                    10,
                    1,
                    60,
                ),
            },
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def database_configuration_status() -> dict[str, Any]:
    """Return non-secret evidence explaining whether query can be advertised."""
    try:
        existing_account_accepted = canary_existing_account_accepted()
    except DatabaseSecurityError as exc:
        return {
            "ready": False,
            "reason_code": exc.code,
            "missing_names": [],
        }
    missing = [
        name
        for name in _REQUIRED_DATABASE_ENV
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        return {
            "ready": False,
            "reason_code": "DATABASE_CONFIGURATION_MISSING",
            "missing_names": missing,
        }
    try:
        tls = mysql_tls_kwargs()
        tls_policy = mysql_tls_policy()
    except DatabaseSecurityError as exc:
        return {
            "ready": False,
            "reason_code": exc.code,
            "missing_names": [],
        }
    try:
        from . import tools

        module = tools._load_pymysql()
    except Exception:
        return {
            "ready": False,
            "reason_code": "DEPENDENCY_UNAVAILABLE",
            "missing_names": [],
        }
    return {
        "ready": True,
        "reason_code": None,
        "missing_names": [],
        "production_mode": tls_policy["production_mode"],
        "tls_required": tls_policy["tls_required"],
        "tls_configured": tls_policy["tls_configured"],
        "transport_policy": (
            "verified_tls" if tls_policy["tls_configured"]
            else "plaintext_nonproduction"
        ),
        "transport_observed": "not_checked",
        "tls_verified": None,
        "tls_ca_readable": (
            Path(tls["ssl_ca"]).is_file() if "ssl_ca" in tls else False
        ),
        "pymysql_version": ".".join(str(value) for value in module.VERSION[:3]),
        "grant_scope_count": len(
            settings.get_list("mysql_allowed_grant_scopes")
        ),
        "grant_policy": (
            "user_accepted_canary_existing_account"
            if existing_account_accepted
            else "strict_object_read_only"
        ),
    }


def query_readiness_status() -> dict[str, Any]:
    """Return the complete runtime gate used immediately before query work."""

    identity = runtime_identity_status()
    if not identity["ready"]:
        return {
            "ready": False,
            "reason_code": identity["reason_code"],
            "missing_names": [],
            "identity": identity,
        }
    status = database_configuration_status()
    if status["ready"]:
        status = live_database_security_status()
    return {**status, "identity": identity}


def live_database_security_status() -> dict[str, Any]:
    """TTL-cached live connection/grant proof with truthful transport status."""
    global _LIVE_CACHE
    now = time.monotonic()
    cache_key = _live_cache_key()
    cached = _LIVE_CACHE
    if cached is not None and cache_key == cached[1] and now < cached[0]:
        return dict(cached[2])
    with _LIVE_LOCK:
        now = time.monotonic()
        cache_key = _live_cache_key()
        cached = _LIVE_CACHE
        if cached is not None and cache_key == cached[1] and now < cached[0]:
            return dict(cached[2])
        try:
            from . import tools

            connection = tools._connect(
                connect_timeout_seconds=settings.get_int(
                    "mysql_health_connect_timeout_seconds",
                    5,
                    1,
                    60,
                ),
                read_timeout_seconds=settings.get_int(
                    "mysql_grant_audit_timeout_seconds",
                    10,
                    1,
                    60,
                ),
            )
        except tools.QueryFailure as exc:
            status = {
                "ready": False,
                "reason_code": exc.code,
                "missing_names": [],
            }
        except Exception:
            status = {
                "ready": False,
                "reason_code": "DATABASE_CONNECTION_FAILED",
                "missing_names": [],
            }
        else:
            evidence = getattr(connection, "_datasage_security_evidence", None)
            try:
                connection.close()
            except Exception:
                logger.warning(
                    "datasage_health_connection_close_failed",
                    exc_info=True,
                )
            if (
                not isinstance(evidence, dict)
                or evidence.get("live_connection_verified") is not True
                or evidence.get("grants_verified") is not True
                or evidence.get("transport_mode") not in {"tls", "plaintext"}
            ):
                status = {
                    "ready": False,
                    "reason_code": "DATABASE_SECURITY_EVIDENCE_MISSING",
                    "missing_names": [],
                }
            else:
                status = {
                    "ready": True,
                    "reason_code": None,
                    "missing_names": [],
                    **evidence,
                }
        ttl = (
            _LIVE_SUCCESS_TTL_SECONDS
            if status["ready"]
            else _LIVE_FAILURE_TTL_SECONDS
        )
        _LIVE_CACHE = (time.monotonic() + ttl, cache_key, dict(status))
        return status
