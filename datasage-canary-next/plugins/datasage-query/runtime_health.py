"""Static and bounded live readiness checks for the query tool."""

from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
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
_IDENTITY_FIELDS = ("commit", "tag", "tree_oid", "uv_lock_sha256")
_ATTESTATION_VERSION = "datasage-runtime-attestation/v1"
_ATTESTATION_TIMEOUT_SECONDS = 60
_REPLAY_CONTEXT = (
    "replay",
    "datasage-trusted-replay",
    "datasage-ephemeral-replay",
)

_REQUIRED_DATABASE_ENV = (
    "DATA_QUERY_MYSQL_HOST",
    "DATA_QUERY_MYSQL_DATABASE",
    "DATA_QUERY_MYSQL_USER",
    "DATA_QUERY_MYSQL_PASSWORD",
)


class _IdentityFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _profile_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _production_identity_required(root: Path) -> bool:
    return settings.get_bool("production_mode", False) or (
        root / ".production-release"
    ).is_file()


def _identity_fingerprint(identity: dict[str, Any] | None) -> str | None:
    if identity is None:
        return None
    bounded = {field: identity.get(field) for field in _IDENTITY_FIELDS}
    payload = json.dumps(
        bounded,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _imported_hermes_origin() -> Path:
    module = sys.modules.get("hermes_cli")
    if module is None:
        raise _IdentityFailure("HERMES_IDENTITY_MODULE_NOT_IMPORTED")
    spec = getattr(module, "__spec__", None)
    raw_origin = getattr(spec, "origin", None) if spec is not None else None
    if not raw_origin or raw_origin in {"built-in", "frozen"}:
        raw_origin = getattr(module, "__file__", None)
    if not isinstance(raw_origin, str) or not raw_origin:
        raise _IdentityFailure("HERMES_IDENTITY_ORIGIN_UNAVAILABLE")
    origin = Path(raw_origin)
    if not origin.is_absolute() or not origin.is_file():
        raise _IdentityFailure("HERMES_IDENTITY_ORIGIN_INVALID")
    return origin


def _dsrt_runtime_root_from_origin(origin: Path) -> Path:
    try:
        resolved = origin.resolve(strict=True)
        checkout = resolved.parents[1]
        checkout_parent = resolved.parents[2]
        state = resolved.parents[3]
        runtime = resolved.parents[4]
    except (IndexError, OSError) as exc:
        raise _IdentityFailure("DSRT_RUNTIME_PATH_UNAVAILABLE") from exc
    if (
        resolved.name != "__init__.py"
        or resolved.parent.name != "hermes_cli"
        or checkout_parent.name != "hermes-checkouts"
        or state.name != "state"
        or runtime.name != "runtime"
        or checkout.parent != checkout_parent
        or checkout_parent.parent != state
        or state.parent != runtime
    ):
        raise _IdentityFailure("DSRT_RUNTIME_PATH_INVALID")
    for candidate in (resolved, resolved.parent, checkout, checkout_parent, state, runtime):
        if _is_reparse(candidate):
            raise _IdentityFailure("DSRT_RUNTIME_PATH_UNSAFE")
    return runtime


def _native_replay_context() -> bool:
    """Accept replay mode only from Hermes' engaged request ContextVars."""

    try:
        from gateway import session_context

        if not session_context.session_context_engaged():
            return False
        values = []
        for name in (
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_SOURCE",
            "HERMES_SESSION_USER_ID",
        ):
            variable = session_context._VAR_MAP.get(name)
            value = variable.get() if variable is not None else session_context._UNSET
            if value is session_context._UNSET:
                return False
            values.append(str(value).strip())
    except (AttributeError, ImportError, RuntimeError):
        return False
    return (values[0].lower(), values[1], values[2]) == _REPLAY_CONTEXT


def _external_dsrt_attestation(profile_root: Path) -> dict[str, Any]:
    try:
        origin = _imported_hermes_origin()
        runtime_root = _dsrt_runtime_root_from_origin(origin)
    except _IdentityFailure as exc:
        return {"ready": False, "reason_code": exc.code}
    replay_context = _native_replay_context()
    control = runtime_root.parent / "control" / (
        "evaluation/ephemeral_replay_launcher.py"
        if replay_context
        else "runtime_gateway_bootstrap.py"
    )
    backup_root = runtime_root.parent / "backups"
    if _is_reparse(control) or not control.is_file():
        return {"ready": False, "reason_code": "DSRT_CONTROL_PACKAGE_UNAVAILABLE"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    environment.update({
        "DATASAGE_REQUIRE_CONTROL_MANIFEST": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    })
    if replay_context:
        replay_root = runtime_root.parent.parent
        command = [
            sys.executable, "-I", "-B", str(control),
            "--profile-root", str(profile_root),
            "--command-config", str(replay_root / "REPLAY_COMMAND_CONFIG.json"),
            "--hermes-origin", str(origin),
            "--attest-existing",
        ]
    else:
        environment.update({
            "DATASAGE_HERMES_ORIGIN": str(origin),
            "DATASAGE_GATEWAY_PID": str(os.getpid()),
        })
        command = [
            sys.executable, "-I", "-B", str(control),
            "--root", str(runtime_root),
            "--runtime-home", str(profile_root),
            "--backup-root", str(backup_root),
            "attest",
        ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_ATTESTATION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=environment,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"ready": False, "reason_code": "DSRT_ATTESTATION_UNAVAILABLE"}
    if (
        not isinstance(value, dict)
        or value.get("version") != _ATTESTATION_VERSION
        or not isinstance(value.get("ready"), bool)
        or (
            value.get("ready")
            and set(value)
            != {
                "version",
                "ready",
                "reason_code",
                "release_unit_id",
                "profile_artifact_id",
                "profile_payload_sha256",
                "hermes",
                "control",
                "runtime_home",
                "hermes_origin",
            }
        )
        or (
            not value.get("ready")
            and set(value) != {"version", "ready", "reason_code"}
        )
        or (
            value["ready"]
            and (
                completed.returncode != 0
                or value.get("reason_code") is not None
                or not isinstance(value.get("release_unit_id"), str)
                or not value["release_unit_id"]
                or not isinstance(value.get("profile_artifact_id"), str)
                or not value["profile_artifact_id"]
                or not isinstance(value.get("profile_payload_sha256"), str)
                or len(value["profile_payload_sha256"]) != 64
                or not isinstance(value.get("runtime_home"), str)
                or not value["runtime_home"]
                or not isinstance(value.get("hermes_origin"), str)
                or not value["hermes_origin"]
                or not isinstance(value.get("hermes"), dict)
                or set(value["hermes"]) != set(_IDENTITY_FIELDS)
                or not isinstance(value.get("control"), dict)
                or set(value["control"])
                != {"commit", "tag", "tree_oid", "probe_sha256"}
            )
        )
        or (
            not value["ready"]
            and (
                completed.returncode == 0
                or not isinstance(value.get("reason_code"), str)
                or not value["reason_code"]
            )
        )
    ):
        return {"ready": False, "reason_code": "DSRT_ATTESTATION_INVALID"}
    return value


def runtime_identity_status(*, profile_root: Path | None = None) -> dict[str, Any]:
    """Verify the active profile through the external DSRT proof chain."""
    root = _profile_root() if profile_root is None else profile_root.resolve()
    release_path = root / ".release" / "RELEASE.json"
    if not release_path.is_file():
        try:
            _dsrt_runtime_root_from_origin(_imported_hermes_origin())
        except _IdentityFailure:
            dsrt_backed = False
        else:
            dsrt_backed = True
        if not dsrt_backed and _production_identity_required(root):
            return {
                "ready": False,
                "state": "installed",
                "reason_code": "HERMES_IDENTITY_RELEASE_MISSING",
                "identity_override": False,
                "expected_fingerprint": None,
                "actual_fingerprint": None,
            }
        if not dsrt_backed:
            return {
                "ready": True,
                "state": "source",
                "reason_code": None,
                "identity_override": False,
                "expected_fingerprint": None,
                "actual_fingerprint": None,
            }
    attestation = _external_dsrt_attestation(root)
    hermes = attestation.get("hermes")
    identity = hermes if isinstance(hermes, dict) else None
    payload = attestation.get("profile_payload_sha256")
    return {
        "ready": bool(attestation.get("ready")),
        "state": "installed",
        "reason_code": attestation.get("reason_code"),
        "identity_override": False,
        "expected_fingerprint": _identity_fingerprint(identity),
        "actual_fingerprint": _identity_fingerprint(identity),
        "payload_expected": payload,
        "payload_actual": payload if attestation.get("ready") else None,
        "release_unit_id": attestation.get("release_unit_id"),
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


def record_startup_health() -> dict[str, Any]:
    """Record static readiness only; plugin registration has no network I/O."""
    identity = runtime_identity_status()
    database = database_configuration_status()
    status = {
        **database,
        "ready": bool(identity["ready"] and database["ready"]),
        "reason_code": (
            identity["reason_code"]
            if not identity["ready"]
            else database["reason_code"]
        ),
        "identity": identity,
    }
    root = _profile_root()
    release_path = root / ".release" / "RELEASE.json"
    release: dict[str, Any] = {}
    if release_path.is_file():
        try:
            parsed = json.loads(release_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                release = parsed
        except (OSError, ValueError):
            release = {}
    logger.info(
        "datasage_startup_health component=database_query "
        "version=%s artifact_id=%s payload_sha256=%s ready=%s "
        "reason_code=%s missing_names=%s tls_ca_readable=%s "
        "pymysql_version=%s grant_scope_count=%s "
        "identity_state=%s identity_override=%s "
        "identity_reason_code=%s identity_expected=%s identity_actual=%s",
        release.get("distribution_version", "source"),
        release.get("artifact_id", "unreleased"),
        release.get("payload_sha256", "unreleased"),
        status["ready"],
        status["reason_code"],
        ",".join(status["missing_names"]),
        status.get("tls_ca_readable"),
        status.get("pymysql_version"),
        status.get("grant_scope_count"),
        identity["state"],
        identity["identity_override"],
        identity["reason_code"],
        identity["expected_fingerprint"],
        identity["actual_fingerprint"],
    )
    return status
