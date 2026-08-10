"""Static and bounded live readiness checks for the query tool."""

from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
import re
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
from .payload_manifest import (
    HASH_ALGORITHM,
    PayloadManifestError,
    aggregate_sha256,
    build_payload_manifest,
)


logger = logging.getLogger(__name__)
_LIVE_LOCK = threading.Lock()
_LIVE_CACHE: tuple[float, str, dict[str, Any]] | None = None
_LIVE_SUCCESS_TTL_SECONDS = 30.0
_LIVE_FAILURE_TTL_SECONDS = 10.0
_IDENTITY_FIELDS = ("commit", "tag", "tree_oid", "uv_lock_sha256")
_MAX_CHECKOUT_PARENT_DEPTH = 8

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


def _checkout_from_origin(origin: Path) -> Path:
    unresolved = origin
    for candidate in (unresolved, *unresolved.parents[:_MAX_CHECKOUT_PARENT_DEPTH]):
        if _is_reparse(candidate):
            raise _IdentityFailure("HERMES_IDENTITY_REPARSE_REJECTED")
    resolved_origin = origin.resolve(strict=True)
    candidate = resolved_origin.parent
    for _ in range(_MAX_CHECKOUT_PARENT_DEPTH):
        marker = candidate / ".git"
        if marker.exists():
            if _is_reparse(candidate) or _is_reparse(marker):
                raise _IdentityFailure("HERMES_IDENTITY_REPARSE_REJECTED")
            checkout = candidate.resolve(strict=True)
            try:
                resolved_origin.relative_to(checkout)
            except ValueError as exc:
                raise _IdentityFailure("HERMES_IDENTITY_PATH_ESCAPE") from exc
            return checkout
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    raise _IdentityFailure("HERMES_IDENTITY_CHECKOUT_NOT_FOUND")


def _git(checkout: Path, *args: str, text: bool = True) -> str | bytes:
    command = [
        "git",
        "-c",
        f"safe.directory={checkout.as_posix()}",
        "-C",
        str(checkout),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _IdentityFailure("HERMES_IDENTITY_GIT_READ_FAILED") from exc
    return completed.stdout


def _actual_hermes_identity(expected_tag: str) -> dict[str, Any]:
    origin = _imported_hermes_origin()
    checkout = _checkout_from_origin(origin)
    commit = str(_git(checkout, "rev-parse", "HEAD")).strip().lower()
    dirty = bool(str(_git(checkout, "status", "--porcelain", "--", ".")).strip())
    tree_oid = str(_git(checkout, "rev-parse", f"{commit}^{{tree}}")).strip().lower()
    exact_tags = str(_git(checkout, "tag", "--points-at", commit)).splitlines()
    uv_lock = _git(checkout, "show", f"{commit}:uv.lock", text=False)
    if (
        not re.fullmatch(r"[0-9a-f]{40,64}", commit)
        or not re.fullmatch(r"[0-9a-f]{40,64}", tree_oid)
        or not isinstance(uv_lock, bytes)
        or not uv_lock
    ):
        raise _IdentityFailure("HERMES_IDENTITY_ACTUAL_INVALID")
    return {
        "commit": commit,
        "tag": (
            expected_tag
            if expected_tag in exact_tags
            else (sorted(exact_tags)[0] if exact_tags else "<no-exact-tag>")
        ),
        "tree_oid": tree_oid,
        "uv_lock_sha256": hashlib.sha256(uv_lock).hexdigest(),
        "_dirty": dirty,
        "_expected_tag_present": expected_tag in exact_tags,
    }


def runtime_identity_status(*, profile_root: Path | None = None) -> dict[str, Any]:
    """Verify installed RELEASE against the checkout that supplied hermes_cli."""
    root = _profile_root() if profile_root is None else profile_root.resolve()
    release_path = root / ".release" / "RELEASE.json"
    production = _production_identity_required(root)
    if not release_path.is_file():
        if production:
            return {
                "ready": False,
                "state": "installed",
                "reason_code": "HERMES_IDENTITY_RELEASE_MISSING",
                "identity_override": False,
                "expected_fingerprint": None,
                "actual_fingerprint": None,
            }
        return {
            "ready": True,
            "state": "source",
            "reason_code": None,
            "identity_override": False,
            "expected_fingerprint": None,
            "actual_fingerprint": None,
        }
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        release = None
    expected = release.get("hermes_source") if isinstance(release, dict) else None
    if not isinstance(expected, dict) or any(
        not isinstance(expected.get(field), str) or not expected[field]
        for field in _IDENTITY_FIELDS
    ):
        return {
            "ready": False,
            "state": "installed",
            "reason_code": "HERMES_IDENTITY_RELEASE_INVALID",
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(
                expected if isinstance(expected, dict) else None
            ),
            "actual_fingerprint": None,
        }
    if (
        not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected["commit"])
        or not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected["tree_oid"])
        or not re.fullmatch(r"[0-9a-fA-F]{64}", expected["uv_lock_sha256"])
        or re.search(r"[\x00-\x20\x7f]", expected["tag"])
    ):
        return {
            "ready": False,
            "state": "installed",
            "reason_code": "HERMES_IDENTITY_RELEASE_INVALID",
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(expected),
            "actual_fingerprint": None,
        }
    normalized_expected = {
        **expected,
        "commit": expected["commit"].lower(),
        "tree_oid": expected["tree_oid"].lower(),
        "uv_lock_sha256": expected["uv_lock_sha256"].lower(),
    }
    expected_files = release.get("payload_files")
    try:
        if (
            release.get("payload_hash_algorithm") != HASH_ALGORITHM
            or not isinstance(expected_files, list)
            or release.get("payload_file_count") != len(expected_files)
            or aggregate_sha256(expected_files) != release.get("payload_sha256")
        ):
            raise PayloadManifestError("invalid release payload manifest")
    except PayloadManifestError:
        return {
            "ready": False,
            "state": "installed",
            "reason_code": "HERMES_IDENTITY_PAYLOAD_MANIFEST_INVALID",
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(normalized_expected),
            "actual_fingerprint": None,
            "payload_expected": release.get("payload_sha256"),
            "payload_actual": None,
        }
    try:
        actual_payload = build_payload_manifest(root)
    except (OSError, PayloadManifestError):
        return {
            "ready": False,
            "state": "installed",
            "reason_code": "HERMES_IDENTITY_PAYLOAD_READ_FAILED",
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(normalized_expected),
            "actual_fingerprint": None,
            "payload_expected": release.get("payload_sha256"),
            "payload_actual": None,
        }
    expected_by_path = {entry["path"]: entry["sha256"] for entry in expected_files}
    actual_by_path = {
        entry["path"]: entry["sha256"] for entry in actual_payload["payload_files"]
    }
    missing = sorted(set(expected_by_path) - set(actual_by_path))
    unexpected = sorted(set(actual_by_path) - set(expected_by_path))
    modified = sorted(
        path
        for path in set(expected_by_path) & set(actual_by_path)
        if expected_by_path[path] != actual_by_path[path]
    )
    if (
        missing
        or unexpected
        or modified
        or actual_payload["payload_sha256"] != release.get("payload_sha256")
    ):
        return {
            "ready": False,
            "state": "installed",
            "reason_code": "HERMES_IDENTITY_PAYLOAD_MISMATCH",
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(normalized_expected),
            "actual_fingerprint": None,
            "payload_expected": release.get("payload_sha256"),
            "payload_actual": actual_payload["payload_sha256"],
            "payload_missing_count": len(missing),
            "payload_unexpected_count": len(unexpected),
            "payload_modified_count": len(modified),
        }
    actual: dict[str, Any] | None = None
    failure_code = "HERMES_IDENTITY_MISMATCH"
    try:
        actual = _actual_hermes_identity(normalized_expected["tag"])
    except _IdentityFailure as exc:
        failure_code = exc.code
    actual_identity = (
        {field: actual.get(field) for field in _IDENTITY_FIELDS}
        if actual is not None
        else None
    )
    if actual is not None and actual.get("_dirty"):
        failure_code = "HERMES_IDENTITY_CHECKOUT_DIRTY"
    elif actual is not None and not actual.get("_expected_tag_present"):
        failure_code = "HERMES_IDENTITY_EXACT_TAG_MISMATCH"
    matched = (
        actual is not None
        and not actual.get("_dirty")
        and actual.get("_expected_tag_present")
        and actual_identity
        == {field: normalized_expected[field] for field in _IDENTITY_FIELDS}
    )
    if matched:
        # Both the profile payload and Hermes checkout identities matched.
        return {
            "ready": True,
            "state": "installed",
            "reason_code": None,
            "identity_override": False,
            "expected_fingerprint": _identity_fingerprint(normalized_expected),
            "actual_fingerprint": _identity_fingerprint(actual_identity),
            "payload_expected": release.get("payload_sha256"),
            "payload_actual": actual_payload["payload_sha256"],
        }
    return {
        "ready": False,
        "state": "installed",
        "reason_code": failure_code,
        "identity_override": False,
        "expected_fingerprint": _identity_fingerprint(normalized_expected),
        "actual_fingerprint": _identity_fingerprint(actual_identity),
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


def database_query_available() -> bool:
    """Compatibility probe; runtime registration no longer hides the tool."""

    status = query_readiness_status()
    if not status["ready"]:
        logger.warning(
            "datasage_query_unavailable reason_code=%s missing_names=%s",
            status["reason_code"],
            ",".join(status["missing_names"]),
        )
    return bool(status["ready"])


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
