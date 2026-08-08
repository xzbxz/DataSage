"""Offline, network-free health probe embedded in atomic runtime units."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


EXPECTED_TOOLS = {
    "datasage_catalog",
    "datasage_entity_resolve",
    "datasage_query",
}
EXPECTED_HOOKS = {
    "pre_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "transform_llm_output",
}
EXPECTED_MIDDLEWARE = {"llm_request", "tool_request"}
EXPECTED_EXECUTOR_ID = "datasage-atomic-offline-health/v1"
DOMAINS = (
    "delivery",
    "receipt",
    "receivable",
    "target",
    "customer_risk",
    "inventory",
)


def _required_directory(name: str) -> Path:
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"{name} is unavailable")
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise RuntimeError(f"{name} is invalid")
    return path.resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class _RegistrationContext:
    def __init__(self) -> None:
        self.tools: set[str] = set()
        self.hooks: set[str] = set()
        self.middleware: set[str] = set()
        self.handlers: dict[str, Any] = {}

    def register_tool(self, *, name: str, handler: Any, **_kwargs: Any) -> None:
        self.tools.add(name)
        self.handlers[name] = handler

    def register_hook(self, name: str, _callback: Any) -> None:
        self.hooks.add(name)

    def register_middleware(self, name: str, _callback: Any) -> None:
        self.middleware.add(name)


def _load_plugin(profile: Path):
    package = profile / "plugins" / "datasage-query"
    initializer = package / "__init__.py"
    if not initializer.is_file():
        raise RuntimeError("DataSage plugin initializer is missing")
    module_name = "datasage_offline_health_plugin"
    spec = importlib.util.spec_from_file_location(
        module_name,
        initializer,
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("DataSage plugin cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run() -> dict[str, Any]:
    if os.environ.get("DATASAGE_OFFLINE_HEALTH") != "true":
        raise RuntimeError("offline health mode is not active")
    unit_id = os.environ.get("DATASAGE_RELEASE_UNIT_ID")
    if not unit_id:
        raise RuntimeError("release unit identity is unavailable")
    profile = _required_directory("HERMES_HOME")
    hermes = _required_directory("HERMES_AGENT_ROOT")
    forbidden_environment = [
        name
        for name in os.environ
        if (
            name.startswith(("DATA_QUERY_", "WECOM_", "DEEPSEEK_"))
            or name.endswith(("_API_KEY", "_TOKEN", "_SECRET"))
            or name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        )
    ]
    if forbidden_environment:
        raise RuntimeError("sensitive environment reached offline health")

    network_attempted = False
    external_execution_attempted = False

    def enforce_isolation(event: str, _args: tuple[Any, ...]) -> None:
        nonlocal network_attempted, external_execution_attempted
        if event.startswith("socket."):
            network_attempted = True
            raise RuntimeError("network use is forbidden during offline health")
        if (
            event == "subprocess.Popen"
            or event == "os.system"
            or event == "os.startfile"
            or event.startswith("os.spawn")
            or event.startswith("ctypes.dlopen")
        ):
            external_execution_attempted = True
            raise RuntimeError(
                "external execution is forbidden during offline health"
            )

    sys.addaudithook(enforce_isolation)

    sys.path.insert(0, str(hermes))
    import hermes_cli  # type: ignore[import-not-found]

    hermes_origin = Path(str(hermes_cli.__file__)).resolve()
    if not _within(hermes_origin, hermes):
        raise RuntimeError("Hermes import escaped the materialized checkout")

    release_path = profile / ".release" / "RELEASE.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if not isinstance(release, dict) or not release.get("artifact_id"):
        raise RuntimeError("materialized Profile release identity is invalid")

    plugin = _load_plugin(profile)
    context = _RegistrationContext()
    database_attempted = False
    original_connect = plugin.tools._connect
    original_startup_health = plugin.runtime_health.record_startup_health

    def deny_database(*_args: Any, **_kwargs: Any) -> None:
        nonlocal database_attempted
        database_attempted = True
        raise RuntimeError("database use is forbidden during offline health")

    plugin.tools._connect = deny_database
    plugin.runtime_health.record_startup_health = lambda: {
        "ready": False,
        "reason_code": "OFFLINE_HEALTH_NO_DATABASE",
        "missing_names": [],
    }
    try:
        plugin.register(context)
    finally:
        plugin.tools._connect = original_connect
        plugin.runtime_health.record_startup_health = original_startup_health
    if context.tools != EXPECTED_TOOLS:
        raise RuntimeError("DataSage tool registration is incomplete")
    if context.hooks != EXPECTED_HOOKS:
        raise RuntimeError("DataSage hook registration is incomplete")
    if context.middleware != EXPECTED_MIDDLEWARE:
        raise RuntimeError("DataSage middleware registration is incomplete")
    contract_handler = context.handlers.get("datasage_catalog")
    if not callable(contract_handler):
        raise RuntimeError("DataSage catalog handler is unavailable")
    contract = json.loads(
        contract_handler(
            {
                "requests": [
                    {"domain": domain}
                    for domain in DOMAINS
                ],
            }
        )
    )
    if (
        contract.get("status") != "success"
        or len(contract.get("results") or []) != len(DOMAINS)
        or {item.get("domain") for item in contract["results"]} != set(DOMAINS)
    ):
        raise RuntimeError("six-domain DataSage contract is unavailable")
    wecom_loaded = any(
        name.startswith("hermes_cli.platforms.wecom")
        or ".wecom" in name
        for name in sys.modules
    )
    if (
        network_attempted
        or external_execution_attempted
        or database_attempted
        or wecom_loaded
    ):
        raise RuntimeError("offline health isolation boundary was crossed")

    return {
        "status": "healthy",
        "release_unit_id": unit_id,
        "executor_id": EXPECTED_EXECUTOR_ID,
        "profile_artifact_id": release["artifact_id"],
        "checks": {
            "hermes_import": True,
            "plugin_loaded": True,
            "tool_surface": True,
            "six_domain_contract": True,
            "network_used": False,
            "database_used": False,
            "wecom_loaded": False,
        },
    }


def main() -> int:
    try:
        result = run()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "unhealthy",
                    "error_type": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
