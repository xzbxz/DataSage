"""Static and bounded live readiness checks for the query tool."""

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
import stat
import threading
import time
from typing import Any

from agent.secret_scope import get_secret
from hermes_constants import hermes_home_key

from .db_security import (
    DatabaseSecurityError,
    canary_existing_account_accepted,
    mysql_tls_kwargs,
    mysql_tls_policy,
)
from . import contract_store, db_runtime, settings
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
    return contract_store.profile_root()


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except (OSError, ValueError) as exc:
        raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNAVAILABLE") from exc
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _approved_profile_roots(active_root: Path) -> tuple[Path, ...]:
    configured = settings.get_list("approved_profile_roots")
    roots: list[Path] = []
    for raw in configured:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise _ProfileRootFailure("DATASAGE_APPROVED_ROOT_INVALID")
        roots.append(candidate)
    # The active Hermes profile is always the primary approval root. Optional
    # operator roots support an explicitly managed profile layout without
    # falling back to an arbitrary HERMES_HOME path.
    roots.insert(0, active_root)
    return tuple(roots)


def _validate_profile_path(candidate: Path, active_root: Path) -> Path:
    """Resolve a profile path after checking every lexical component."""

    lexical_candidate = candidate.expanduser().absolute()
    for raw_root in _approved_profile_roots(active_root):
        lexical_root = raw_root.expanduser().absolute()
        try:
            relative = lexical_candidate.relative_to(lexical_root)
        except ValueError:
            continue
        current = lexical_root
        try:
            if _is_reparse(current):
                raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNSAFE")
            for part in relative.parts:
                current = current / part
                if _is_reparse(current):
                    raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNSAFE")
            resolved_root = lexical_root.resolve(strict=True)
            resolved = lexical_candidate.resolve(strict=True)
        except _ProfileRootFailure:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNAVAILABLE") from exc
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNSAFE") from exc
        if not resolved.is_dir():
            raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_UNSAFE")
        return resolved
    raise _ProfileRootFailure("DATASAGE_PROFILE_ROOT_OUTSIDE_APPROVED_ROOT")


def runtime_identity_status(*, profile_root: Path | None = None) -> dict[str, Any]:
    """Verify the profile path and the process-lifetime contract identity.

    Hermes owns active-profile and runtime selection. Historical release
    manifests, imported-Hermes paths, and private launcher layouts are not
    DataSage runtime prerequisites. This bounded check cannot prove a Git
    commit/tree or release-receipt binding and reports that limitation
    explicitly. Candidate identity, live replay, host compaction, stability,
    latency, and cost are verified once before release with
    ``python -B build_release_receipt.py --verify-candidate``; they are not
    recomputed on every business query. The database transport/grant/account
    gates below remain fail-closed.
    """

    git_binding = {
        "available": False,
        "commit": None,
        "tree": None,
        "reason_code": "GIT_BINDING_UNAVAILABLE",
    }
    release_binding = {
        "available": False,
        "enforced_per_query": False,
        "verification_scope": "prestart_release_gate",
        "verification_command": "python -B build_release_receipt.py --verify-candidate",
        "reason_code": "RELEASE_PRESTART_VERIFICATION_REQUIRED",
    }
    contract_snapshot = contract_store.contract_snapshot_status()

    active_root = _profile_root()
    candidate = active_root if profile_root is None else Path(profile_root)
    try:
        _validate_profile_path(candidate, active_root)
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
            "contract_snapshot": contract_snapshot,
            "contract_snapshot_fixed": contract_snapshot.get("fixed") is True,
            "contract_snapshot_enforced_per_read": (
                contract_snapshot.get("enforced_per_read") is True
            ),
            "contract_manifest_digest": contract_snapshot.get("manifest_digest"),
            "contract_snapshot_drift_reason": contract_snapshot.get(
                "drift_reason"
            ),
            "git_binding": git_binding,
            "release_binding": release_binding,
            "identity_override": False,
            "runtime": "hermes_managed",
        }

    snapshot_ready = bool(
        contract_snapshot.get("fixed") is True
        and contract_snapshot.get("drifted") is not True
    )
    if not snapshot_ready:
        reason_code = (
            "CONTRACT_SNAPSHOT_DRIFT"
            if contract_snapshot.get("drifted") is True
            else "CONTRACT_SNAPSHOT_UNAVAILABLE"
        )
        return {
            "ready": False,
            "state": "contract_snapshot_integrity",
            "reason_code": reason_code,
            "path_integrity_verified": True,
            "contract_snapshot": contract_snapshot,
            "contract_snapshot_fixed": contract_snapshot.get("fixed") is True,
            "contract_snapshot_enforced_per_read": (
                contract_snapshot.get("enforced_per_read") is True
            ),
            "contract_manifest_digest": contract_snapshot.get("manifest_digest"),
            "contract_snapshot_drift_reason": contract_snapshot.get(
                "drift_reason"
            ),
            "git_binding": git_binding,
            "release_binding": release_binding,
            "identity_override": False,
            "runtime": "hermes_managed",
        }

    return {
        "ready": True,
        "state": "profile_path_integrity",
        "reason_code": None,
        "path_integrity_verified": True,
        "contract_snapshot": contract_snapshot,
        "contract_snapshot_fixed": True,
        "contract_snapshot_enforced_per_read": True,
        "contract_manifest_digest": contract_snapshot.get("manifest_digest"),
        "contract_snapshot_drift_reason": contract_snapshot.get(
            "drift_reason"
        ),
        "git_binding": git_binding,
        "release_binding": release_binding,
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
            "profile": hermes_home_key(contract_store.profile_root()),
            "connection": {
                name: get_secret(name, "") or ""
                for name in connection_names
            },
            "policy": {
                # Preserve raw security values in the cache key. Strict policy
                # validation happens before query I/O; coercing "false" to
                # False here could otherwise reuse evidence from valid config.
                "production_mode": settings.get("production_mode"),
                "canary_accept_existing_account": settings.get(
                    "canary_accept_existing_account"
                ),
                "require_tls": settings.get("require_tls"),
                "mysql_allowed_grant_scopes": settings.get_list(
                    "mysql_allowed_grant_scopes"
                ),
                "mysql_allowed_server_uuids": settings.get_list(
                    "mysql_allowed_server_uuids"
                ),
                "mysql_server_uuid_allowlist": settings.get_list(
                    "mysql_server_uuid_allowlist"
                ),
                "mysql_approved_path_roots": settings.get_list(
                    "mysql_approved_path_roots"
                ),
                "approved_profile_roots": settings.get_list(
                    "approved_profile_roots"
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
        if not (get_secret(name, "") or "").strip()
    ]
    if missing:
        return {
            "ready": False,
            "reason_code": "DATABASE_CONFIGURATION_MISSING",
            "missing_names": missing,
        }
    try:
        active_profile_root = contract_store.profile_root()
        tls_policy = mysql_tls_policy(profile_root=active_profile_root)
        tls = mysql_tls_kwargs(
            policy=tls_policy,
            profile_root=active_profile_root,
        )
    except DatabaseSecurityError as exc:
        return {
            "ready": False,
            "reason_code": exc.code,
            "missing_names": [],
        }
    try:
        module = db_runtime.load_pymysql()
    except db_runtime.DatabaseRuntimeError:
        return {
            "ready": False,
            "reason_code": "DEPENDENCY_UNAVAILABLE",
            "missing_names": [],
        }
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
            connection = db_runtime.connect(
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
        except db_runtime.DatabaseRuntimeError as exc:
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
