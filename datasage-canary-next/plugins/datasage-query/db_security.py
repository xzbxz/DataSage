"""Fail-closed transport and server-authorization checks for MySQL."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any
import uuid

from .settings import get_secret

from . import contract_store, settings


class DatabaseSecurityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_READ_ONLY_PRIVILEGES = {"SELECT", "SHOW VIEW", "USAGE"}
_ALLOWED_TLS_PROTOCOLS = {"TLSv1.2", "TLSv1.3"}
MYSQL_SOURCE_IDENTITY_STATEMENT = (
    "SELECT @@server_uuid AS server_uuid, @@server_id AS server_id, "
    "@@hostname AS server_hostname, @@port AS server_port, "
    "DATABASE() AS database_name, CURRENT_USER() AS authenticated_user"
)
_SOURCE_IDENTITY_DOMAIN = b"datasage-query-source-identity/v1\x00"
_SOURCE_COMMITMENT_DOMAIN = b"datasage-query-source-commitment/v1\x00"
_SOURCE_SECURITY_DOMAIN = b"datasage-query-source-evidence/v1\x00"
_SOURCE_GRANT_POLICIES = {
    "strict_object_read_only",
    "user_accepted_canary_existing_account",
    "user_accepted_canary_privileged_account",
}
_SOURCE_IDENTITY_CONFIGURED_FIELDS = ("host", "port", "database", "user")
_SOURCE_IDENTITY_OBSERVED_FIELDS = (
    "server_uuid",
    "server_id",
    "server_hostname",
    "server_port",
    "database_name",
    "authenticated_user",
)
_SOURCE_EVIDENCE_LEGACY_FIELDS = frozenset(
    {
        "schema",
        "identity_sha256",
        "connection_verified",
        "transport_mode",
        "transport_policy_verified",
        "grant_policy",
        "grants_verified",
        "read_only",
        "source_commitment_sha256",
        "security_evidence_sha256",
    }
)
_SOURCE_EVIDENCE_IDENTITY_FIELDS = frozenset(
    {
        "configured_port",
        "observed_server_port",
        "canary_source_port_mismatch_exception",
    }
)
_DATABASE_SECURITY_BOOL_SETTINGS = (
    "production_mode",
    "require_tls",
    "canary_accept_existing_account",
    "canary_allow_privileged_account",
    "canary_allow_source_port_mismatch",
)
_CANARY_SOURCE_PORT_PAIR_PATTERN = re.compile(r"^(\d{1,5}):(\d{1,5})$")


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise DatabaseSecurityError(
            "DATABASE_PATH_UNAVAILABLE",
            "受保护路径不可用。",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _approved_path_roots(profile_root: Path | None = None) -> tuple[Path, ...]:
    """Return operator-approved roots for profile-owned security material."""

    roots: list[Path] = []
    configured = settings.get_list("mysql_approved_path_roots")
    for raw in configured:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise DatabaseSecurityError(
                "DATABASE_PATH_ROOT_INVALID",
                "数据库安全路径根必须是绝对路径。",
            )
        roots.append(candidate)
    active = (
        Path(profile_root).expanduser()
        if profile_root is not None
        else contract_store.profile_root()
    )
    if not active.is_absolute():
        raise DatabaseSecurityError(
            "DATABASE_PATH_ROOT_INVALID",
            "当前 Profile 路径必须是绝对路径。",
        )
    roots.insert(0, active)
    return tuple(roots)


def _safe_path_in_approved_root(
    value: str | Path,
    *,
    profile_root: Path | None = None,
    require_file: bool = True,
) -> Path:
    """Resolve a path only after checking every lexical component."""

    raw_path = Path(value).expanduser()
    if not raw_path.is_absolute():
        raise DatabaseSecurityError(
            "DATABASE_PATH_OUTSIDE_APPROVED_ROOT",
            "数据库安全路径必须是绝对路径。",
        )
    lexical = raw_path.absolute()
    for raw_root in _approved_path_roots(profile_root):
        root = raw_root.absolute()
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            continue
        current = root
        try:
            if _is_reparse(current):
                raise DatabaseSecurityError(
                    "DATABASE_PATH_REPARSE_FORBIDDEN",
                    "数据库安全路径根不能是符号链接或 reparse point。",
                )
            for part in relative.parts:
                current = current / part
                if _is_reparse(current):
                    raise DatabaseSecurityError(
                        "DATABASE_PATH_REPARSE_FORBIDDEN",
                        "数据库安全路径不能穿过符号链接或 reparse point。",
                    )
            resolved_root = root.resolve(strict=True)
            resolved = lexical.resolve(strict=True)
        except DatabaseSecurityError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise DatabaseSecurityError(
                "DATABASE_PATH_UNAVAILABLE",
                "数据库安全路径不可用。",
            ) from exc
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise DatabaseSecurityError(
                "DATABASE_PATH_OUTSIDE_APPROVED_ROOT",
                "数据库安全路径超出批准根目录。",
            ) from exc
        if require_file and not resolved.is_file():
            raise DatabaseSecurityError(
                "DATABASE_PATH_UNAVAILABLE",
                "数据库安全文件不存在或不可读。",
            )
        return resolved
    raise DatabaseSecurityError(
        "DATABASE_PATH_OUTSIDE_APPROVED_ROOT",
        "数据库安全路径超出批准根目录。",
    )


def _server_uuid_allowlist() -> tuple[set[str], bool]:
    """Return a validated optional UUID allowlist and whether it was set."""

    configured = settings.get_list("mysql_allowed_server_uuids")
    if not configured:
        # Accept the descriptive alias during migration, while the manifest's
        # canonical key remains mysql_allowed_server_uuids.
        configured = settings.get_list("mysql_server_uuid_allowlist")
    if not configured:
        return set(), False
    result: set[str] = set()
    for raw in configured:
        try:
            parsed = uuid.UUID(raw.strip())
        except (AttributeError, ValueError) as exc:
            raise DatabaseSecurityError(
                "DATABASE_SERVER_UUID_ALLOWLIST_INVALID",
                "数据库 server UUID allowlist 无效。",
            ) from exc
        if not _mysql_server_uuid_has_sufficient_entropy(parsed):
            raise DatabaseSecurityError(
                "DATABASE_SERVER_UUID_ALLOWLIST_INVALID",
                "数据库 server UUID allowlist 无效。",
            )
        result.add(str(parsed).casefold())
    return result, True


def _database_security_policy() -> dict[str, bool]:
    """Load the complete Hermes-provided database policy or fail closed."""

    missing_value = object()
    configured = {
        name: settings.get(name, missing_value)
        for name in _DATABASE_SECURITY_BOOL_SETTINGS
    }
    missing = [
        name
        for name in _DATABASE_SECURITY_BOOL_SETTINGS
        if configured[name] is missing_value
    ]
    if missing:
        raise DatabaseSecurityError(
            "DATABASE_SECURITY_SETTING_MISSING",
            "数据库安全设置缺失。",
        )
    invalid = [
        name
        for name in _DATABASE_SECURITY_BOOL_SETTINGS
        if not isinstance(configured.get(name), bool)
    ]
    if invalid:
        raise DatabaseSecurityError(
            "DATABASE_SECURITY_SETTING_INVALID",
            "数据库安全设置必须使用布尔值。",
        )
    policy = {
        name: configured[name] for name in _DATABASE_SECURITY_BOOL_SETTINGS
    }
    if policy["canary_allow_privileged_account"] and (
        policy["production_mode"]
        or not policy["canary_accept_existing_account"]
    ):
        raise DatabaseSecurityError(
            "DATABASE_CANARY_PRIVILEGED_ACCOUNT_FORBIDDEN",
            "临时 Canary 高权限账号例外仅允许在非生产且启用现有账号例外时使用。",
        )
    if policy["canary_allow_source_port_mismatch"] and policy["production_mode"]:
        raise DatabaseSecurityError(
            "DATABASE_CANARY_SOURCE_PORT_MISMATCH_FORBIDDEN",
            "临时 Canary 数据库端口例外仅允许在非生产模式使用。",
        )
    if policy["production_mode"] and policy["canary_accept_existing_account"]:
        raise DatabaseSecurityError(
            "DATABASE_CANARY_ACCOUNT_ACCEPTANCE_FORBIDDEN",
            "生产模式禁止 Canary 现有账号例外。",
        )
    return policy


def _canary_allowed_source_port_pairs() -> frozenset[tuple[int, int]]:
    """Return the validated, explicit configured-to-observed port pairs."""

    pairs: set[tuple[int, int]] = set()
    for raw in settings.get_list("canary_allowed_source_port_pairs"):
        match = _CANARY_SOURCE_PORT_PAIR_PATTERN.fullmatch(raw.strip())
        if match is None:
            raise DatabaseSecurityError(
                "DATABASE_CANARY_SOURCE_PORT_PAIR_INVALID",
                "Canary 数据库端口对配置无效。",
            )
        configured_port, observed_port = (int(value) for value in match.groups())
        if not (
            1 <= configured_port <= 65535
            and 1 <= observed_port <= 65535
            and configured_port != observed_port
        ):
            raise DatabaseSecurityError(
                "DATABASE_CANARY_SOURCE_PORT_PAIR_INVALID",
                "Canary 数据库端口对配置无效。",
            )
        pairs.add((configured_port, observed_port))
    return frozenset(pairs)


def canary_existing_account_accepted(
    *,
    profile_root: Path | None = None,
) -> bool:
    """Honor only the complete Git-tracked Profile security policy."""

    del profile_root
    return _database_security_policy()["canary_accept_existing_account"]


def canary_privileged_account_allowed(
    *,
    profile_root: Path | None = None,
) -> bool:
    """Return the explicitly gated temporary privileged-account exception."""

    del profile_root
    return _database_security_policy()["canary_allow_privileged_account"]


def canary_source_port_mismatch_allowed(
    *,
    profile_root: Path | None = None,
) -> bool:
    """Return the explicitly gated temporary source-port exception."""

    del profile_root
    return _database_security_policy()["canary_allow_source_port_mismatch"]


def mysql_tls_policy(
    *,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    """Resolve transport only from the complete Git-tracked Profile policy."""

    del profile_root
    policy = _database_security_policy()
    production_mode = policy["production_mode"]
    tls_required = production_mode or policy["require_tls"]
    return {
        "production_mode": production_mode,
        "tls_required": tls_required,
        "tls_configured": bool(
            (get_secret("DATA_QUERY_MYSQL_SSL_CA", "") or "").strip()
        ),
    }


def mysql_tls_kwargs(
    *,
    policy: Mapping[str, Any] | None = None,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    """Return public PyMySQL arguments for certificate and identity checks."""
    if policy is None:
        policy = mysql_tls_policy(profile_root=profile_root)
    if (
        not isinstance(policy, Mapping)
        or not isinstance(policy.get("tls_required"), bool)
        or not isinstance(policy.get("tls_configured"), bool)
    ):
        raise DatabaseSecurityError(
            "DATABASE_TRANSPORT_POLICY_INVALID",
            "数据库 TLS policy snapshot 无效。",
        )
    policy = MappingProxyType(dict(policy))
    ca_value = (get_secret("DATA_QUERY_MYSQL_SSL_CA", "") or "").strip()
    if not ca_value:
        if policy["tls_required"]:
            raise DatabaseSecurityError(
                "DATABASE_TLS_CONFIGURATION_MISSING",
                "当前模式要求数据库 TLS，必须配置 DATA_QUERY_MYSQL_SSL_CA。",
            )
        if policy["tls_configured"]:
            raise DatabaseSecurityError(
                "DATABASE_TRANSPORT_POLICY_CHANGED",
                "数据库 TLS policy snapshot 与当前配置不一致。",
            )
        return {"ssl_disabled": True}
    if not policy["tls_configured"]:
        raise DatabaseSecurityError(
            "DATABASE_TRANSPORT_POLICY_CHANGED",
            "数据库 TLS policy snapshot 与当前配置不一致。",
        )
    try:
        ca_path = _safe_path_in_approved_root(
            ca_value,
            profile_root=profile_root,
            require_file=True,
        )
    except DatabaseSecurityError as exc:
        if exc.code in {
            "DATABASE_PATH_UNAVAILABLE",
            "DATABASE_PATH_OUTSIDE_APPROVED_ROOT",
            "DATABASE_PATH_REPARSE_FORBIDDEN",
        }:
            raise DatabaseSecurityError(
                "DATABASE_TLS_CA_UNAVAILABLE",
                "数据库 TLS CA 文件不存在、不可读或不在批准根目录。",
            ) from exc
        raise
    return {
        "ssl_ca": str(ca_path),
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }


def _status_value(row: Any) -> str:
    if isinstance(row, dict):
        value = row.get("Value") or row.get("value")
        if value is None:
            values = list(row.values())
            value = values[1] if len(values) >= 2 else None
    elif isinstance(row, (tuple, list)) and len(row) >= 2:
        value = row[1]
    else:
        value = None
    return str(value or "").strip()


def _complete_status_value(cursor: Any) -> str:
    """Fully consume unbuffered SHOW STATUS results before the next query."""
    rows = cursor.fetchall()
    return _status_value(rows[0] if rows else None)


def _canonical_hash(domain: bytes, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _source_evidence_hash(value: dict[str, Any]) -> str:
    return _canonical_hash(
        _SOURCE_SECURITY_DOMAIN,
        {key: item for key, item in value.items() if key != "security_evidence_sha256"},
    )


def _mysql_server_uuid_has_sufficient_entropy(value: uuid.UUID) -> bool:
    """Reject synthetic/trivial UUIDs without requiring a UUID version/variant."""

    raw = value.hex
    bit_count = value.int.bit_count()
    if bit_count < 24 or bit_count > 104 or len(set(raw)) < 4:
        return False
    return not any(
        raw == raw[:period] * (len(raw) // period)
        for period in (1, 2, 4, 8, 16)
    )


def _source_evidence_identity_is_valid(evidence: Mapping[str, Any]) -> bool:
    """Validate optional configured/observed identity retention in evidence."""

    present = set(evidence)
    if present == _SOURCE_EVIDENCE_LEGACY_FIELDS:
        return True
    if present != _SOURCE_EVIDENCE_LEGACY_FIELDS | _SOURCE_EVIDENCE_IDENTITY_FIELDS:
        return False
    configured_port_value = evidence.get("configured_port")
    observed_port_value = evidence.get("observed_server_port")
    mismatch_exception = evidence.get("canary_source_port_mismatch_exception")
    if (
        not isinstance(mismatch_exception, bool)
    ):
        return False
    try:
        configured_port = int(str(configured_port_value).strip())
        observed_port = int(str(observed_port_value).strip())
    except (TypeError, ValueError):
        return False
    if not 1 <= configured_port <= 65535 or not 1 <= observed_port <= 65535:
        return False
    return mismatch_exception is (configured_port != observed_port)


def validate_mysql_source_evidence(
    evidence: Any,
    *,
    require_read_only: bool,
) -> dict[str, Any]:
    """Validate a non-secret reference derived from one verified connection."""

    if (
        not isinstance(evidence, dict)
        or not _source_evidence_identity_is_valid(evidence)
        or evidence.get("schema") != "datasage-query-source-evidence/v1"
        or re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("identity_sha256") or "")) is None
        or evidence.get("connection_verified") is not True
        or evidence.get("transport_mode") not in {"tls", "plaintext"}
        or evidence.get("transport_policy_verified") is not True
        or evidence.get("grant_policy") not in _SOURCE_GRANT_POLICIES
        or evidence.get("grants_verified") is not True
        or re.fullmatch(
            r"[0-9a-f]{64}", str(evidence.get("source_commitment_sha256") or "")
        ) is None
        or not isinstance(evidence.get("read_only"), bool)
        or re.fullmatch(
            r"[0-9a-f]{64}", str(evidence.get("security_evidence_sha256") or "")
        ) is None
        or evidence["security_evidence_sha256"] != _source_evidence_hash(evidence)
        or (require_read_only and evidence["read_only"] is not True)
    ):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_EVIDENCE_INVALID",
            "Database source evidence is invalid.",
        )
    return dict(evidence)


def verify_mysql_source_identity(
    connection: Any,
    *,
    tls_evidence: dict[str, Any],
    grant_evidence: dict[str, Any],
    configured_identity: dict[str, Any],
) -> dict[str, Any]:
    """Bind configured and server-issued identity without exposing raw values."""

    with connection.cursor() as cursor:
        cursor.execute(MYSQL_SOURCE_IDENTITY_STATEMENT)
        rows = cursor.fetchall()
    row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
    fields = _SOURCE_IDENTITY_OBSERVED_FIELDS
    if row is None or any(not str(row.get(field) or "").strip() for field in fields):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Database source identity is unavailable.",
        )
    try:
        parsed_uuid = uuid.UUID(str(row["server_uuid"]).strip())
    except (ValueError, AttributeError) as exc:
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Database source identity is invalid.",
        ) from exc
    if not _mysql_server_uuid_has_sufficient_entropy(parsed_uuid):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Database source identity is invalid.",
        )
    security_policy = _database_security_policy()
    allowed_server_uuids, allowlist_configured = _server_uuid_allowlist()
    observed_uuid = str(parsed_uuid).casefold()
    if security_policy["production_mode"] and not allowlist_configured:
        raise DatabaseSecurityError(
            "DATABASE_SERVER_UUID_ALLOWLIST_REQUIRED",
            "生产模式必须配置数据库 server UUID allowlist。",
        )
    if allowed_server_uuids and observed_uuid not in allowed_server_uuids:
        raise DatabaseSecurityError(
            "DATABASE_SERVER_UUID_NOT_ALLOWED",
            "数据库 server identity 不在批准 allowlist 中。",
        )
    configured_fields = ("host", "port", "database", "user")
    if any(not str(configured_identity.get(field) or "").strip() for field in configured_fields):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Configured database identity is incomplete.",
        )
    try:
        configured_port = int(str(configured_identity["port"]).strip())
        observed_port = int(str(row["server_port"]).strip())
    except (TypeError, ValueError) as exc:
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Database source port identity is invalid.",
        ) from exc
    if not 1 <= configured_port <= 65535 or not 1 <= observed_port <= 65535:
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Database source port identity is invalid.",
        )
    canary_port_mismatch_exception = False
    if observed_port != configured_port:
        if not (
            security_policy["canary_allow_source_port_mismatch"]
            and not security_policy["production_mode"]
            and (configured_port, observed_port)
            in _canary_allowed_source_port_pairs()
        ):
            raise DatabaseSecurityError(
                "DATABASE_SOURCE_IDENTITY_MISMATCH",
                "数据库 server port 与配置不一致。",
            )
        canary_port_mismatch_exception = True

    def _mysql_account_name(value: Any) -> str:
        text = str(value or "").strip()
        # CURRENT_USER() is normally returned as user@host.  Only the account
        # name is compared with the configured login; the host part is a
        # server-issued authentication detail and is retained in the digest.
        if "@" in text:
            text = text.split("@", 1)[0]
        return text.strip("`'\" ").casefold()

    configured_database = str(configured_identity["database"]).strip().casefold()
    observed_database = str(row["database_name"]).strip().casefold()
    configured_user = str(configured_identity["user"]).strip().casefold()
    observed_user = _mysql_account_name(row["authenticated_user"])
    if observed_database != configured_database:
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_MISMATCH",
            "数据库 schema 与配置不一致。",
        )
    if observed_user != configured_user:
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_MISMATCH",
            "数据库认证用户与配置不一致。",
        )
    transport_mode = tls_evidence.get("transport_mode")
    transport_verified = bool(
        transport_mode == "tls"
        and tls_evidence.get("tls_verified") is True
        and tls_evidence.get("transport_encrypted") is True
        or transport_mode == "plaintext"
        and tls_evidence.get("tls_required") is not True
        and tls_evidence.get("tls_configured") is False
        and tls_evidence.get("insecure_transport_allowed") is True
    )
    grant_policy = grant_evidence.get("grant_policy")
    if (
        not transport_verified
        or grant_evidence.get("grants_verified") is not True
        or grant_policy not in _SOURCE_GRANT_POLICIES
    ):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_EVIDENCE_INVALID",
            "Database source security evidence is incomplete.",
        )
    identity_tuple = {
        "configured": {field: str(configured_identity[field]) for field in configured_fields},
        "observed": {field: str(row[field]) for field in fields},
    }
    evidence = {
        "schema": "datasage-query-source-evidence/v1",
        "identity_sha256": _canonical_hash(_SOURCE_IDENTITY_DOMAIN, identity_tuple),
        "configured_port": configured_port,
        "observed_server_port": observed_port,
        "canary_source_port_mismatch_exception": canary_port_mismatch_exception,
        "connection_verified": True,
        "transport_mode": transport_mode,
        "transport_policy_verified": True,
        "grant_policy": str(grant_policy),
        "grants_verified": True,
        "read_only": False,
        "source_commitment_sha256": _canonical_hash(
            _SOURCE_COMMITMENT_DOMAIN,
            {
                "identity": identity_tuple,
                "transport": tls_evidence,
                "grant": grant_evidence,
                "canary_source_port_mismatch_exception": canary_port_mismatch_exception,
                "read_only": False,
            },
        ),
    }
    connection._datasage_source_commitment_preimage = {
        "identity": identity_tuple,
        "transport": dict(tls_evidence),
        "grant": dict(grant_evidence),
        "canary_source_port_mismatch_exception": canary_port_mismatch_exception,
    }
    evidence["security_evidence_sha256"] = _source_evidence_hash(evidence)
    connection._datasage_source_evidence = dict(evidence)
    return dict(evidence)


def confirm_mysql_read_only_transaction(connection: Any) -> dict[str, Any]:
    """Mark the connection reference read-only only after START succeeds."""

    evidence = validate_mysql_source_evidence(
        getattr(connection, "_datasage_source_evidence", None),
        require_read_only=False,
    )
    evidence["read_only"] = True
    preimage = getattr(connection, "_datasage_source_commitment_preimage", None)
    if not isinstance(preimage, dict):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_EVIDENCE_INVALID",
            "Database source evidence is invalid.",
        )
    evidence["source_commitment_sha256"] = _canonical_hash(
        _SOURCE_COMMITMENT_DOMAIN,
        {**preimage, "read_only": True},
    )
    evidence["security_evidence_sha256"] = _source_evidence_hash(evidence)
    connection._datasage_source_evidence = dict(evidence)
    return validate_mysql_source_evidence(evidence, require_read_only=True)


def verify_mysql_tls(
    connection: Any,
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove TLS using one immutable policy snapshot."""
    if policy is None:
        policy = mysql_tls_policy()
    if (
        not isinstance(policy, Mapping)
        or not isinstance(policy.get("tls_required"), bool)
        or not isinstance(policy.get("tls_configured"), bool)
    ):
        raise DatabaseSecurityError(
            "DATABASE_TRANSPORT_POLICY_INVALID",
            "数据库 TLS policy snapshot 无效。",
        )
    policy = MappingProxyType(dict(policy))
    with connection.cursor() as cursor:
        cursor.execute("SHOW STATUS LIKE 'Ssl_cipher'")
        cipher = _complete_status_value(cursor)
        cursor.execute("SHOW STATUS LIKE 'Ssl_version'")
        protocol = _complete_status_value(cursor)
    if not policy["tls_configured"]:
        if cipher or protocol:
            raise DatabaseSecurityError(
                "DATABASE_TRANSPORT_POLICY_MISMATCH",
                "连接传输状态与显式明文策略不一致。",
            )
        if policy["tls_required"]:
            raise DatabaseSecurityError(
                "DATABASE_TLS_CONFIGURATION_MISSING",
                "当前模式要求数据库 TLS，拒绝明文数据库连接。",
            )
        return {
            "tls_required": policy["tls_required"],
            "tls_configured": False,
            "tls_verified": False,
            "transport_mode": "plaintext",
            "transport_encrypted": False,
            "insecure_transport_allowed": True,
            "tls_cipher": None,
            "tls_protocol": None,
        }
    if not cipher or not protocol:
        raise DatabaseSecurityError(
            "DATABASE_TLS_NEGOTIATION_UNVERIFIED",
            "数据库连接未完成可验证的 TLS 协商。",
        )
    if protocol not in _ALLOWED_TLS_PROTOCOLS:
        raise DatabaseSecurityError(
            "DATABASE_TLS_PROTOCOL_FORBIDDEN",
            "数据库连接未协商到允许的 TLS 1.2 或 TLS 1.3。",
        )
    return {
        "tls_required": policy["tls_required"],
        "tls_configured": True,
        "tls_verified": True,
        "transport_mode": "tls",
        "transport_encrypted": True,
        "insecure_transport_allowed": False,
        "tls_cipher": cipher,
        "tls_protocol": protocol,
    }


def _grant_texts(rows: Any) -> list[str]:
    grants: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            value = next(iter(row.values()), "")
        elif isinstance(row, (tuple, list)) and row:
            value = row[0]
        else:
            value = ""
        text = str(value or "").strip()
        if text:
            grants.append(text)
    return grants


def verify_mysql_read_only_grants(
    connection: Any,
    *,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    """Require server-issued grants to contain only reviewed read privileges."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
        grants = _grant_texts(cursor.fetchall())
    if not grants:
        raise DatabaseSecurityError(
            "DATABASE_GRANTS_UNAVAILABLE",
            "无法取得数据库账号授权证明。",
        )
    policy = _database_security_policy()
    if policy["canary_allow_privileged_account"]:
        observed_privileges: set[str] = set()
        observed_read_only: set[str] = set()
        observed_scopes: set[str] = set()
        select_capable = False
        for grant in grants:
            if re.search(r"\bWITH\s+GRANT\s+OPTION\b", grant, re.IGNORECASE):
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_OPTION_FORBIDDEN",
                    "数据库账号具有转授权能力，已拒绝查询。",
                )
            match = re.match(
                r"^GRANT\s+(.+?)\s+ON\s+"
                r"((?:`[^`]+`|[^.\s]+)\.(?:`[^`]+`|[^\s]+))\s+TO\s+",
                grant,
                re.IGNORECASE,
            )
            if not match:
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_FORMAT_INVALID",
                    "The database grant format could not be audited.",
                )
            privileges = {
                item.strip().upper()
                for item in match.group(1).split(",")
                if item.strip()
            }
            if not privileges:
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_FORMAT_INVALID",
                    "The database grant format could not be audited.",
                )
            select_capable = select_capable or bool(
                privileges & {"SELECT", "ALL", "ALL PRIVILEGES"}
            )
            observed_privileges.update(privileges)
            observed_read_only.update(privileges & _READ_ONLY_PRIVILEGES)
            observed_scopes.add(match.group(2).replace("`", "").casefold())
        if not select_capable:
            raise DatabaseSecurityError(
                "DATABASE_SELECT_PRIVILEGE_MISSING",
                "数据库账号缺少可验证的 SELECT 权限。",
            )
        # ALL/ALL PRIVILEGES semantically include SELECT; retain that required
        # capability in the evidence while exposing every observed privilege.
        observed_read_only.add("SELECT")
        return {
            "grants_verified": True,
            "read_only_privileges": sorted(observed_read_only),
            "observed_privileges": sorted(observed_privileges),
            "grant_scopes": sorted(observed_scopes),
            "grant_policy": "user_accepted_canary_privileged_account",
            "canary_account_exception": True,
            "canary_privileged_account_exception": True,
        }
    if policy["canary_accept_existing_account"]:
        observed_privileges: set[str] = set()
        observed_read_only: set[str] = set()
        observed_scopes: set[str] = set()
        for grant in grants:
            if re.search(r"\bWITH\s+GRANT\s+OPTION\b", grant, re.IGNORECASE):
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_OPTION_FORBIDDEN",
                    "数据库账号具有转授权能力，已拒绝查询。",
                )
            match = re.match(
                r"^GRANT\s+(.+?)\s+ON\s+"
                r"((?:`[^`]+`|[^.\s]+)\.(?:`[^`]+`|[^\s]+))\s+TO\s+",
                grant,
                re.IGNORECASE,
            )
            if not match:
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_FORMAT_INVALID",
                    "The database grant format could not be audited.",
                )
            privileges = {
                item.strip().upper()
                for item in match.group(1).split(",")
                if item.strip()
            }
            if (
                "ALL" in privileges
                or "ALL PRIVILEGES" in privileges
                or "FILE" in privileges
                or not privileges <= _READ_ONLY_PRIVILEGES
            ):
                raise DatabaseSecurityError(
                    "DATABASE_WRITE_PRIVILEGE_FORBIDDEN",
                    "数据库账号包含非只读权限，已拒绝查询。",
                )
            scope = match.group(2).replace("`", "").casefold()
            # Canary may relax the configured object allowlist (for example,
            # permit a reviewed schema.* scope), but a global *.* grant is
            # still outside the approved object boundary.
            if scope == "*.*" and privileges != {"USAGE"}:
                raise DatabaseSecurityError(
                    "DATABASE_GRANT_SCOPE_TOO_BROAD",
                    "数据库只读账号不能使用 global scope。",
                )
            observed_privileges.update(privileges)
            observed_read_only.update(privileges)
            observed_scopes.add(scope)
        if "SELECT" not in observed_read_only:
            raise DatabaseSecurityError(
                "DATABASE_SELECT_PRIVILEGE_MISSING",
                "数据库账号缺少可验证的 SELECT 权限。",
            )
        return {
            "grants_verified": True,
            "read_only_privileges": sorted(observed_read_only),
            "observed_privileges": sorted(observed_privileges),
            "grant_scopes": sorted(observed_scopes),
            "grant_policy": "user_accepted_canary_existing_account",
            "canary_account_exception": True,
            "canary_privileged_account_exception": False,
        }
    allowed_scopes = {
        item.strip().replace("`", "").casefold()
        for item in settings.get_list("mysql_allowed_grant_scopes")
        if item.strip() and item.strip() != ".*"
    }
    if not allowed_scopes:
        raise DatabaseSecurityError(
            "DATABASE_GRANT_SCOPE_UNCONFIGURED",
            "未配置可验证的数据库授权范围。",
        )
    if any(scope.endswith(".*") for scope in allowed_scopes):
        raise DatabaseSecurityError(
            "DATABASE_GRANT_SCOPE_TOO_BROAD",
            "数据库只读账号必须使用显式对象或受控视图授权，不能使用 schema.*。",
        )
    observed: set[str] = set()
    observed_scopes: set[str] = set()
    for grant in grants:
        if re.search(r"\bWITH\s+GRANT\s+OPTION\b", grant, re.IGNORECASE):
            raise DatabaseSecurityError(
                "DATABASE_GRANT_OPTION_FORBIDDEN",
                "数据库账号具有转授权能力，已拒绝查询。",
            )
        match = re.match(
            r"^GRANT\s+(.+?)\s+ON\s+((?:`[^`]+`|[^.\s]+)\.(?:`[^`]+`|[^\s]+))"
            r"\s+TO\s+",
            grant,
            re.IGNORECASE,
        )
        if not match:
            raise DatabaseSecurityError(
                "DATABASE_GRANT_FORMAT_INVALID",
                "数据库账号授权格式无法验证。",
            )
        privileges = {
            item.strip().upper()
            for item in match.group(1).split(",")
            if item.strip()
        }
        if "ALL PRIVILEGES" in privileges or not privileges <= _READ_ONLY_PRIVILEGES:
            raise DatabaseSecurityError(
                "DATABASE_WRITE_PRIVILEGE_FORBIDDEN",
                "数据库账号包含非只读权限，已拒绝查询。",
            )
        scope = match.group(2).replace("`", "").casefold()
        if privileges == {"USAGE"} and scope == "*.*":
            observed.update(privileges)
            continue
        if scope == "*.*" or scope not in allowed_scopes:
            raise DatabaseSecurityError(
                "DATABASE_GRANT_SCOPE_FORBIDDEN",
                "数据库账号授权超出允许的数据对象范围，已拒绝查询。",
            )
        observed.update(privileges)
        observed_scopes.add(scope)
    if "SELECT" not in observed:
        raise DatabaseSecurityError(
            "DATABASE_SELECT_PRIVILEGE_MISSING",
            "数据库账号缺少可验证的 SELECT 权限。",
        )
    return {
        "grants_verified": True,
        "read_only_privileges": sorted(observed),
        "observed_privileges": sorted(observed),
        "grant_scopes": sorted(observed_scopes),
        "grant_policy": "strict_object_read_only",
        "canary_account_exception": False,
        "canary_privileged_account_exception": False,
    }
