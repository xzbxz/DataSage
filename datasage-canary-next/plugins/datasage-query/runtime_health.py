"""Static readiness checks for the query tool."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
from typing import Any
from types import CodeType
from datetime import datetime, timezone

from agent.secret_scope import get_secret
from .db_security import (
    DatabaseSecurityError,
    _canary_allowed_source_port_pairs,
    canary_existing_account_accepted,
    canary_privileged_account_allowed,
    canary_source_port_mismatch_allowed,
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
        privileged_account_accepted = canary_privileged_account_allowed()
        source_port_mismatch_accepted = canary_source_port_mismatch_allowed()
        allowed_source_port_pair_count = len(_canary_allowed_source_port_pairs())
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
            "user_accepted_canary_privileged_account"
            if privileged_account_accepted
            else "user_accepted_canary_existing_account"
            if existing_account_accepted
            else "strict_object_read_only"
        ),
        "canary_account_exception": existing_account_accepted,
        "canary_privileged_account_exception": privileged_account_accepted,
        "source_port_policy": (
            "user_accepted_canary_port_mismatch"
            if source_port_mismatch_accepted
            else "strict_configured_port_match"
        ),
        "canary_source_port_mismatch_exception": source_port_mismatch_accepted,
        "canary_allowed_source_port_pair_count": allowed_source_port_pair_count,
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


def source_diagnostics() -> dict[str, Any]:
    """Read only non-secret disk identity and this process's pinned contracts.

    A CLI process cannot attest which source an already-running gateway loaded.
    Do not initialize a home, load credentials, connect, or write a status file.
    """
    root = _profile_root()
    source_root = root / "plugins" / "datasage-query"
    relative_paths = ["SOUL.md", "profile.yaml", "plugins/datasage-query/plugin.yaml"]
    relative_paths += [p.relative_to(root).as_posix() for p in source_root.glob("*.py")]
    relative_paths += [p.relative_to(root).as_posix() for p in (source_root / "contracts").iterdir() if p.suffix in {".yaml", ".json"}]
    entries = []
    for relative in sorted(set(relative_paths)):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("DIAGNOSTIC_SOURCE_PATH_INVALID")
        entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    disk_digest = hashlib.sha256(
        "\n".join(f"{name}\0{digest}" for name, digest in entries).encode("utf-8")
    ).hexdigest()
    disk_contract_digest = hashlib.sha256(
        "\n".join(f"{name}\0{digest}" for name, digest in entries if "/contracts/" in name).encode("utf-8")
    ).hexdigest()
    git_head = None
    git_state = "unavailable"
    try:
        environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        command = ["git", "-c", "safe.directory=" + root.parent.as_posix(), "-C", str(root)]
        head = subprocess.run(command + ["rev-parse", "HEAD"], env=environment,
                              capture_output=True, text=True, timeout=5, check=False)
        if head.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head.stdout.strip()):
            git_head = head.stdout.strip()
            dirty = subprocess.run(command + ["status", "--porcelain=v1", "--untracked-files=no", "--", "."],
                                   env=environment, capture_output=True, text=True, timeout=5, check=False)
            git_state = "modified" if dirty.stdout.strip() else "clean" if dirty.returncode == 0 else "unavailable"
    except (OSError, subprocess.SubprocessError):
        pass
    loaded = contract_store.contract_snapshot_status()
    loaded_digest = loaded.get("manifest_digest")
    return {
        "scope": "current_process_and_disk_only",
        "profile_git_head_on_disk": git_head,
        "tracked_worktree_state": git_state,
        "source_file_count": len(entries),
        "disk_source_sha256": disk_digest,
        "disk_contract_files_sha256": disk_contract_digest,
        "current_process_contract_snapshot_loaded": loaded.get("loaded") is True,
        "current_process_contract_snapshot_sha256": loaded_digest,
        "effective_runtime_configuration": "not_observed",
        "running_gateway_loaded_revision": "not_observed",
        "database_connection_attempted": False,
        "credentials_read": False,
    }


def _code_fingerprint(code: CodeType) -> str:
    """Hash semantic code fields, not marshal reference flags or file paths."""
    def canonical(value):
        if isinstance(value, CodeType):
            return {'code': value.co_code.hex(), 'exceptions': value.co_exceptiontable.hex(), 'name': value.co_name,
                    'args': [value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount],
                    'flags': value.co_flags, 'names': value.co_names, 'variables': value.co_varnames,
                    'freevars': value.co_freevars, 'cellvars': value.co_cellvars,
                    'constants': [canonical(item) for item in value.co_consts]}
        if isinstance(value, tuple):return {'tuple': [canonical(item) for item in value]}
        if isinstance(value, frozenset):
            items=[canonical(item) for item in value]
            return {'frozenset': sorted(items,key=lambda item:json.dumps(item,sort_keys=True))}
        if isinstance(value, bytes):return {'bytes': value.hex()}
        if value is None or value is Ellipsis or type(value) in (bool,int,float,complex,str):
            return {'type': type(value).__name__, 'value': repr(value)}
        raise ValueError('unsupported_code_constant')
    content=json.dumps(canonical(code),sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(content).hexdigest()


def record_plugin_initialization(registration_code) -> None:
    """Log this registering process's pinned identity, without runtime actions.

    A gateway claim requires correlating this PID with the host's start event;
    another CLI process emitting this record is not gateway-load evidence.
    """
    logger = logging.getLogger(__name__)
    try:
        record = source_diagnostics()
        record.update(
            event="datasage_plugin_initialized/v2",
            pid=os.getpid(),
            observed_at_utc=datetime.now(timezone.utc).isoformat(),
            registration_code_sha256=_code_fingerprint(registration_code),
            registration_code_hash_basis="canonical-python-code/v1",
        )
        logger.info("DATASAGE_PLUGIN_INITIALIZED %s", json.dumps(record, sort_keys=True))
    except Exception as exc:
        # Diagnostic failures must not disable an otherwise registered plugin.
        # Do not serialize an exception message: it may contain private paths.
        logger.warning("DATASAGE_PLUGIN_IDENTITY_UNAVAILABLE pid=%s error_type=%s", os.getpid(), type(exc).__name__)
