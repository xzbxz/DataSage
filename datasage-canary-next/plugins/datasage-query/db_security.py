"""Fail-closed transport and server-authorization checks for MySQL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any
import uuid

from agent.secret_scope import get_secret

from . import settings


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
}
_DATABASE_SECURITY_BOOL_SETTINGS = (
    "production_mode",
    "require_tls",
    "canary_accept_existing_account",
)


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
    if policy["production_mode"] and policy["canary_accept_existing_account"]:
        raise DatabaseSecurityError(
            "DATABASE_CANARY_ACCOUNT_ACCEPTANCE_FORBIDDEN",
            "生产模式禁止 Canary 现有账号例外。",
        )
    return policy


def canary_existing_account_accepted(
    *,
    profile_root: Path | None = None,
) -> bool:
    """Honor only the complete Git-tracked Profile security policy."""

    del profile_root
    return _database_security_policy()["canary_accept_existing_account"]


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


def mysql_tls_kwargs() -> dict[str, Any]:
    """Return public PyMySQL arguments for certificate and identity checks."""
    policy = mysql_tls_policy()
    ca_value = (get_secret("DATA_QUERY_MYSQL_SSL_CA", "") or "").strip()
    if not ca_value:
        if policy["tls_required"]:
            raise DatabaseSecurityError(
                "DATABASE_TLS_CONFIGURATION_MISSING",
                "当前模式要求数据库 TLS，必须配置 DATA_QUERY_MYSQL_SSL_CA。",
            )
        return {"ssl_disabled": True}
    ca_path = Path(ca_value).expanduser().resolve()
    if not ca_path.is_file():
        raise DatabaseSecurityError(
            "DATABASE_TLS_CA_UNAVAILABLE",
            "数据库 TLS CA 文件不存在或不可读。",
        )
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


def validate_mysql_source_evidence(
    evidence: Any,
    *,
    require_read_only: bool,
) -> dict[str, Any]:
    """Validate a non-secret reference derived from one verified connection."""

    required = {
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
    if (
        not isinstance(evidence, dict)
        or set(evidence) != required
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
    fields = (
        "server_uuid",
        "server_id",
        "server_hostname",
        "server_port",
        "database_name",
        "authenticated_user",
    )
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
    configured_fields = ("host", "port", "database", "user")
    if any(not str(configured_identity.get(field) or "").strip() for field in configured_fields):
        raise DatabaseSecurityError(
            "DATABASE_SOURCE_IDENTITY_INVALID",
            "Configured database identity is incomplete.",
        )
    transport_mode = tls_evidence.get("transport_mode")
    transport_verified = bool(
        transport_mode == "tls"
        and tls_evidence.get("tls_verified") is True
        and tls_evidence.get("transport_encrypted") is True
        or transport_mode == "plaintext"
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
                "read_only": False,
            },
        ),
    }
    connection._datasage_source_commitment_preimage = {
        "identity": identity_tuple,
        "transport": dict(tls_evidence),
        "grant": dict(grant_evidence),
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


def verify_mysql_tls(connection: Any) -> dict[str, Any]:
    """Prove configured TLS, or explicitly report permitted plaintext use."""
    policy = mysql_tls_policy()
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
    if canary_existing_account_accepted(profile_root=profile_root):
        observed_privileges: set[str] = set()
        observed_scopes: set[str] = set()
        for grant in grants:
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
            observed_privileges.update(
                item.strip().upper()
                for item in match.group(1).split(",")
                if item.strip()
            )
            observed_scopes.add(
                match.group(2).replace("`", "").casefold()
            )
        return {
            "grants_verified": True,
            "read_only_privileges": [],
            "observed_privileges": sorted(observed_privileges),
            "grant_scopes": sorted(observed_scopes),
            "grant_policy": "user_accepted_canary_existing_account",
            "canary_account_exception": True,
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
    }
