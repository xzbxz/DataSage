"""Shared database-driver loading and connection security adapter.

The adapter owns PyMySQL provenance checks and connection establishment.  A
query-executor bridge is deliberately injected by the composition layer so
entity resolution can reuse the governed executor without importing it.
"""

from __future__ import annotations

import _imp
import hashlib
import importlib.util
import logging
import ssl
import stat
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Sequence

from agent.secret_scope import get_secret

from . import contract_store, settings, db_executor
from .db_security import (
    DatabaseSecurityError,
    mysql_tls_kwargs,
    mysql_tls_policy,
    verify_mysql_read_only_grants,
    verify_mysql_source_identity,
    verify_mysql_tls,
)


logger = logging.getLogger(__name__)
_PYMYSQL_ALIAS_PREFIX = "_datasage_pymysql_"
_QUERY_EXECUTOR: Callable[..., tuple[list[dict[str, Any]], bool]] | None = None


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNAVAILABLE",
            "vendored dependency path is unavailable",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_vendor_path(path: Path, approved_root: Path) -> Path:
    """Resolve a vendor path only when every component stays in the root."""

    lexical_root = approved_root.absolute()
    lexical_path = path.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED",
            "vendored dependency escapes the approved plugin root",
        ) from exc
    current = lexical_root
    try:
        if _is_reparse(current):
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNTRUSTED",
                "approved plugin root is a symlink or reparse point",
            )
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise DatabaseRuntimeError(
                    "DEPENDENCY_UNTRUSTED",
                    "vendored dependency traverses a symlink or reparse point",
                )
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_path.resolve(strict=True)
    except DatabaseRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNAVAILABLE",
            "vendored dependency path is unavailable",
        ) from exc
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED",
            "vendored dependency escapes the approved plugin root",
        ) from exc
    return resolved


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


def _pymysql_alias(vendor_root: Path) -> str:
    identity = vendor_root.resolve().as_posix().encode("utf-8")
    return f"{_PYMYSQL_ALIAS_PREFIX}{hashlib.sha256(identity).hexdigest()}"


def _pymysql_modules(alias: str) -> dict[str, Any]:
    prefix = f"{alias}."
    return {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == alias or name.startswith(prefix)
    }


def _clear_pymysql_modules(alias: str) -> None:
    for name in _pymysql_modules(alias):
        sys.modules.pop(name, None)


def _validate_pymysql_family(module: Any, package_root: Path, alias: str):
    origin_text = str(getattr(module, "__file__", "") or "")
    if not origin_text:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED", "PyMySQL module provenance is unavailable"
        )
    origin = _validate_vendor_path(Path(origin_text), package_root)
    try:
        package_paths = tuple(
            _validate_vendor_path(Path(path), package_root)
            for path in module.__path__
        )
    except (AttributeError, TypeError, OSError):
        package_paths = ()
    version = tuple(getattr(module, "VERSION", ())[:3])
    connect_callable = getattr(module, "connect", None)
    cursors = getattr(module, "cursors", None)
    if (
        getattr(module, "__name__", None) != alias
        or origin != (package_root / "__init__.py").resolve()
        or package_paths != (package_root,)
        or version != (1, 2, 0)
        or not callable(connect_callable)
        or cursors is None
        or not hasattr(cursors, "SSDictCursor")
    ):
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED",
            "PyMySQL provenance, version, or module integrity is invalid",
        )
    modules = _pymysql_modules(alias)
    if modules.get(alias) is not module:
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNTRUSTED", "PyMySQL package registration is invalid"
        )
    for name, member in modules.items():
        origin_text = str(getattr(member, "__file__", "") or "")
        try:
            origin = _validate_vendor_path(Path(origin_text), package_root)
        except (OSError, RuntimeError, ValueError):
            origin = None
        if origin is None or not origin.is_relative_to(package_root):
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNTRUSTED",
                f"PyMySQL module provenance is invalid: {name}",
            )
        if getattr(member, "__name__", None) != name:
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNTRUSTED",
                f"PyMySQL module registration is invalid: {name}",
            )
        package_paths = getattr(member, "__path__", None)
        if package_paths is not None:
            if name == alias:
                expected_path = package_root
            else:
                relative_name = name.removeprefix(f"{alias}.")
                expected_path = package_root.joinpath(*relative_name.split("."))
            try:
                resolved_paths = tuple(
                    _validate_vendor_path(Path(path), package_root)
                    for path in package_paths
                )
            except (TypeError, OSError, RuntimeError, ValueError):
                resolved_paths = ()
            if resolved_paths != (expected_path,):
                raise DatabaseRuntimeError(
                    "DEPENDENCY_UNTRUSTED",
                    f"PyMySQL package path is invalid: {name}",
                )
    return module


def load_pymysql():
    module_file = Path(__file__).absolute()
    plugin_root = module_file.parent
    if module_file.exists():
        _validate_vendor_path(module_file, plugin_root)
    # The imported module location is the code-owned approval root.  Do not
    # resolve it before checking lexical components: resolve() would hide a
    # junction/symlink that points the vendor tree outside the plugin.
    _validate_vendor_path(plugin_root, plugin_root)
    vendor_root = _validate_vendor_path(plugin_root / "vendor", plugin_root)
    package_root = _validate_vendor_path(vendor_root / "pymysql", plugin_root)
    if not package_root.is_dir():
        raise DatabaseRuntimeError(
            "DEPENDENCY_UNAVAILABLE", "vendored PyMySQL is unavailable"
        )
    alias = _pymysql_alias(vendor_root)
    _imp.acquire_lock()
    try:
        loaded = sys.modules.get(alias)
        if loaded is not None:
            return _validate_pymysql_family(loaded, package_root, alias)
        if _pymysql_modules(alias):
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNTRUSTED",
                "orphaned profile-specific PyMySQL submodules are registered",
            )
        try:
            spec = importlib.util.spec_from_file_location(
                alias,
                package_root / "__init__.py",
                submodule_search_locations=[str(package_root)],
            )
            if spec is None or spec.loader is None:
                raise DatabaseRuntimeError(
                    "DEPENDENCY_UNAVAILABLE",
                    "vendored PyMySQL import specification is unavailable",
                )
            pymysql = importlib.util.module_from_spec(spec)
            sys.modules[alias] = pymysql
            spec.loader.exec_module(pymysql)
            return _validate_pymysql_family(pymysql, package_root, alias)
        except ImportError as exc:
            _clear_pymysql_modules(alias)
            raise DatabaseRuntimeError(
                "DEPENDENCY_UNAVAILABLE", "vendored PyMySQL could not be loaded"
            ) from exc
        except BaseException:
            _clear_pymysql_modules(alias)
            raise
    finally:
        _imp.release_lock()


def connection_port() -> int:
    raw = (get_secret("DATA_QUERY_MYSQL_PORT", "") or "").strip()
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
        "host": (get_secret("DATA_QUERY_MYSQL_HOST", "") or "").strip(),
        "database": (get_secret("DATA_QUERY_MYSQL_DATABASE", "") or "").strip(),
        "user": (get_secret("DATA_QUERY_MYSQL_USER", "") or "").strip(),
        "password": get_secret("DATA_QUERY_MYSQL_PASSWORD", "") or "",
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
    deadline_at = db_executor.current_deadline()
    if deadline_at is not None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise db_executor.DeadlineExceeded("deadline exceeded before connection setup")
        connect_timeout = min(connect_timeout, remaining)
        query_timeout = min(query_timeout, remaining)
    connection = None
    watchdog = None
    try:
        port = connection_port()
        active_profile_root = contract_store.profile_root()
        try:
            tls_policy = MappingProxyType(
                dict(mysql_tls_policy(profile_root=active_profile_root))
            )
        except DatabaseSecurityError:
            # Keep the legacy injected-test seam usable when the TLS adapter
            # itself is replaced.  The real ``mysql_tls_kwargs`` call below
            # still fails closed if the policy is genuinely unavailable.
            tls_policy = None
            tls_kwargs = mysql_tls_kwargs(profile_root=active_profile_root)
        else:
            tls_kwargs = mysql_tls_kwargs(
                policy=tls_policy,
                profile_root=active_profile_root,
            )
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
            **({"defer_connect": True} if deadline_at is not None else {}),
            **tls_kwargs,
        )
        if deadline_at is not None:
            watchdog = db_executor.SocketDeadline(connection, deadline_at)
            watchdog.check()
            connection.connect()
            watchdog.check()
        tls_evidence = (
            verify_mysql_tls(connection, policy=tls_policy)
            if tls_policy is not None
            else verify_mysql_tls(connection)
        )
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
        if watchdog is not None:
            watchdog.check()
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
        if (watchdog is not None and watchdog.expired.is_set()) or (deadline_at is not None and time.monotonic() >= deadline_at):
            raise db_executor.DeadlineExceeded("deadline exceeded during connection security") from exc
        raise
    finally:
        if watchdog is not None:
            watchdog.stop()
