"""Static readiness checks for the query tool."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Any

from agent.secret_scope import get_secret
from .db_security import (
    DatabaseSecurityError,
    canary_existing_account_accepted,
    mysql_tls_kwargs,
    mysql_tls_policy,
)
from . import contract_store, db_runtime, settings

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
    """Verify only the active profile path and registration-time snapshot."""

    snapshot = contract_store.contract_snapshot_status()
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
            "active_profile_path": str(active_root),
            "contract_snapshot_loaded": snapshot.get("loaded") is True,
        }

    if snapshot.get("loaded") is not True:
        reason_code = (
            "CONTRACT_SNAPSHOT_ROOT_CHANGED"
            if snapshot.get("drifted") is True
            else "CONTRACT_SNAPSHOT_UNAVAILABLE"
        )
        return {
            "ready": False,
            "state": "contract_snapshot_integrity",
            "reason_code": reason_code,
            "path_integrity_verified": True,
            "active_profile_path": str(active_root),
            "contract_snapshot_loaded": False,
        }

    return {
        "ready": True,
        "state": "profile_path_integrity",
        "reason_code": None,
        "path_integrity_verified": True,
        "active_profile_path": str(active_root),
        "contract_snapshot_loaded": True,
    }


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
    """Return static readiness; the real query connection owns live checks."""

    identity = runtime_identity_status()
    if not identity["ready"]:
        return {
            "ready": False,
            "reason_code": identity["reason_code"],
            "missing_names": [],
            "identity": identity,
        }
    status = database_configuration_status()
    return {**status, "identity": identity}
