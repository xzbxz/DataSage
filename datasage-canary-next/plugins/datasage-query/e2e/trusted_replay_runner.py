#!/usr/bin/env python3
"""Run Hermes CLI with a fixed, request-local DataSage replay identity.

This file is distribution-owned and must be executed from the verified
immutable Profile payload copied into an exclusive replay root. Identity is
never accepted from argv, environment, marker, prompt, or tool arguments.
"""

from __future__ import annotations

import os
import sys
import json
import contextlib
import io
from pathlib import Path

if os.name == "nt":
    import msvcrt


REPLAY_PLATFORM = "replay"
REPLAY_SOURCE = "datasage-trusted-replay"
REPLAY_PRINCIPAL = "datasage-ephemeral-replay"
CREDENTIAL_KEYS = (
    "DEEPSEEK_API_KEY",
    "DATA_QUERY_MYSQL_HOST",
    "DATA_QUERY_MYSQL_PORT",
    "DATA_QUERY_MYSQL_DATABASE",
    "DATA_QUERY_MYSQL_USER",
    "DATA_QUERY_MYSQL_PASSWORD",
)
LIVE_FIXTURE_PREFLIGHT_FLAG = "--live-fixture-preflight"
MAX_EXCHANGE_BYTES = 2_000_000


def _descriptor_from_inherited(raw: str, flags: int) -> int:
    try:
        inherited = int(raw)
    except ValueError as exc:
        raise RuntimeError("live fixture inherited handle is invalid") from exc
    if inherited < 0:
        raise RuntimeError("live fixture inherited handle is invalid")
    if os.name == "nt":
        descriptor = msvcrt.open_osfhandle(inherited, flags)
        try:
            return os.dup(descriptor)
        finally:
            os.close(descriptor)
    return os.dup(inherited)


def _read_exchange_request(raw_handle: str) -> dict[str, object]:
    descriptor = _descriptor_from_inherited(
        raw_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
    )
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, MAX_EXCHANGE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_EXCHANGE_BYTES:
        raise RuntimeError("live fixture exchange is oversized")
    value = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise RuntimeError("live fixture exchange payload is invalid")
    return value


def _write_exchange_response(raw_handle: str, value: dict[str, object]) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    if len(payload) > MAX_EXCHANGE_BYTES:
        raise RuntimeError("live fixture exchange response is oversized")
    descriptor = _descriptor_from_inherited(
        raw_handle, os.O_WRONLY | getattr(os, "O_BINARY", 0)
    )
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _live_fixture_facade(payload: dict[str, object]) -> dict[str, object]:
    """Invoke one native facade with fixed replay ContextVars and model zero."""

    from gateway.session_context import clear_session_vars, set_session_vars

    plugin_root = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
    sys.path.insert(0, str(plugin_root))
    try:
        plugin = __import__(
            "plugins.datasage-query",
            fromlist=["contracts", "tools", "entitlements", "wire"],
        )
    finally:
        try:
            sys.path.remove(str(plugin_root))
        except ValueError:
            pass
    contracts = plugin.contracts
    tools = plugin.tools
    entitlements = plugin.entitlements
    wire = plugin.wire

    tokens = set_session_vars(
        platform=REPLAY_PLATFORM,
        source=REPLAY_SOURCE,
        user_id=REPLAY_PRINCIPAL,
        profile=Path(os.environ["HERMES_HOME"]).name,
        cwd=str(Path.cwd()),
        async_delivery=False,
    )
    try:
        tool_name = payload.get("tool_name")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError("live fixture arguments are invalid")
        raw_handler = (
            contracts.datasage_catalog
            if tool_name == "datasage_catalog"
            else tools.runtime_guarded_datasage_query
            if tool_name == "datasage_query"
            else None
        )
        if raw_handler is None:
            raise RuntimeError("live fixture tool is not allowlisted")
        # Match the registered tool-role path: native ContextVars -> entitlement
        # guard -> official catalog/query facade. No model is constructed.
        handler = entitlements.guard(
            tool_name,
            wire.bounded_json_handler(tool_name, raw_handler),
        )
        return json.loads(handler(arguments))
    finally:
        clear_session_vars(tokens)


def _credential_snapshot() -> dict[str, str]:
    values = {key: os.environ.get(key, "") for key in CREDENTIAL_KEYS}
    if any(not value for value in values.values()):
        values.clear()
        raise RuntimeError("trusted replay credential environment is incomplete")
    if os.environ.get("DATA_QUERY_MYSQL_SSL_CA"):
        values.clear()
        raise RuntimeError("CREDENTIAL_SSL_CA_UNSUPPORTED")
    return values


def _restore_environment(original: dict[str, str | None]) -> None:
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _restore_credentials(values: dict[str, str]) -> None:
    if set(values) != set(CREDENTIAL_KEYS) or any(not value for value in values.values()):
        raise RuntimeError("trusted replay credential snapshot is invalid")
    for key, value in values.items():
        os.environ[key] = value
    if any(os.environ.get(key) != value for key, value in values.items()):
        raise RuntimeError("trusted replay credential environment was not restored")


def _install_env_loader_guard(values: dict[str, str]):
    """Reassert credentials after any later Hermes dotenv reload."""

    from hermes_cli import env_loader

    original = env_loader.load_hermes_dotenv

    def guarded(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            _restore_credentials(values)

    replacements = []
    env_loader.load_hermes_dotenv = guarded
    replacements.append((env_loader, original))
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "load_hermes_dotenv", None) is original:
            setattr(module, "load_hermes_dotenv", guarded)
            replacements.append((module, original))

    def uninstall() -> None:
        for module, prior in reversed(replacements):
            if getattr(module, "load_hermes_dotenv", None) is guarded:
                setattr(module, "load_hermes_dotenv", prior)

    return uninstall


def run(argv: list[str]) -> int:
    home = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
    environment_keys = (*CREDENTIAL_KEYS, "DATA_QUERY_MYSQL_SSL_CA")
    original_environment = {key: os.environ.get(key) for key in environment_keys}
    credentials: dict[str, str] = {}
    uninstall_guard = lambda: None
    try:
        credentials = _credential_snapshot()
        if argv and argv[0] == LIVE_FIXTURE_PREFLIGHT_FLAG:
            if len(argv) != 3:
                raise RuntimeError("live fixture preflight requires inherited handles")
            payload = _read_exchange_request(argv[1])
            # Tool/library diagnostics are intentionally discarded in this
            # mode; only the fixed status enum may cross stdout/stderr.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                result = _live_fixture_facade(payload)
            _write_exchange_response(argv[2], result)
            sys.stdout.write("LIVE_FIXTURE_PREFLIGHT_OK\n")
            return 0
        from gateway.session_context import clear_session_vars, set_session_vars
        from hermes_cli.main import main as hermes_main
        # hermes_cli.main delays importing the real chat CLI until dispatch.
        # Preload it now so cli.py's second override=True dotenv load completes
        # before we restore the attested credential snapshot.
        import cli as _loaded_chat_cli  # noqa: F401

        # Reassert only after both hermes_cli.main and the actual cli module
        # completed their import-time loaders. Guard every later reload too.
        _restore_credentials(credentials)
        uninstall_guard = _install_env_loader_guard(credentials)

        original_argv = sys.argv
        tokens = set_session_vars(
            platform=REPLAY_PLATFORM,
            source=REPLAY_SOURCE,
            user_id=REPLAY_PRINCIPAL,
            profile=home.name,
            cwd=str(Path.cwd()),
            async_delivery=False,
        )
        try:
            sys.argv = ["hermes", *argv]
            result = hermes_main()
            return int(result) if isinstance(result, int) else 0
        finally:
            sys.argv = original_argv
            clear_session_vars(tokens)
    finally:
        uninstall_guard()
        _restore_environment(original_environment)
        credentials.clear()


def main() -> int:
    try:
        return run(sys.argv[1:])
    except BaseException:
        if sys.argv[1:2] == [LIVE_FIXTURE_PREFLIGHT_FLAG]:
            sys.stderr.write("LIVE_FIXTURE_PREFLIGHT_FAILED\n")
            return 70
        raise


if __name__ == "__main__":
    raise SystemExit(main())
