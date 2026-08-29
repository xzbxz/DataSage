#!/usr/bin/env python3
"""Collect commit-bound, raw DataSage non-database performance evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROFILE_ROOT = Path(__file__).resolve().parents[1]
PROFILE_GIT_ROOT = PROFILE_ROOT.parent
HERMES_ROOT = PROFILE_ROOT.parents[1] / "hermes-agent"
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_PATH = PROFILE_ROOT / "tests" / "fixtures" / "performance_non_db_contract.json"
PRODUCER_PATH = PROFILE_ROOT / "tests" / "run_performance_evidence.py"
TEST_PATH = PROFILE_ROOT / "tests" / "test_performance_evidence.py"
SOUL_PATH = PROFILE_ROOT / "SOUL.md"
REPORT_SCHEMA = "datasage-performance-evidence/v1"
CONTRACT_SCHEMA = "datasage-performance-non-db-contract/v1"
PINNED_HERMES_VERSION = "0.20.5"
PINNED_HERMES_COMMIT = "fcbd1076a93841fa88855acce810e342a5b78101"
DENIED_CODE = "DATA_ENTITLEMENT_DENIED"
DENIED_RESULT = {
    "status": "failed",
    "error": {
        "code": DENIED_CODE,
        "message": "当前请求未获授权，业务查询未执行。",
        "retryable": False,
    },
}
EXPECTED_TOOLS = ("datasage_catalog", "datasage_entity_resolve", "datasage_query")
USAGE_FIELDS = (
    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens", "api_calls",
)


class EvidenceError(RuntimeError):
    """The evidence cannot be collected or attributed safely."""


def _reject_constant(value: str) -> None:
    raise EvidenceError(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise EvidenceError(f"duplicate JSON property: {key}")
        payload[key] = value
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"cannot read contract {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("performance contract must be a JSON object")
    return payload


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: object) -> str:
    return _sha256_bytes(_canonical_json_bytes(payload))


def _is_hex_digest(value: Any, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_contract(contract: Mapping[str, Any]) -> None:
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise EvidenceError("unsupported performance contract schema")
    if contract.get("subject") != {"name": "datasage-canary-next", "version": "0.15.0-rc8"}:
        raise EvidenceError("contract subject drifted from the reviewed profile")
    if contract.get("host") != {
        "hermes_version": PINNED_HERMES_VERSION,
        "hermes_git_commit": PINNED_HERMES_COMMIT,
    }:
        raise EvidenceError("contract host pin drifted from Hermes 0.20.5 source")
    if contract.get("provider") != "deepseek" or contract.get("model") != "deepseek-v4-flash":
        raise EvidenceError("contract provider/model drifted from the reviewed target")
    names = (
        "sample_plan", "execution", "safety_budget", "measurement",
        "accounting", "acceptance", "pricing_snapshot",
    )
    controls = {name: contract.get(name) for name in names}
    if not all(isinstance(value, dict) for value in controls.values()):
        raise EvidenceError("contract control blocks must be JSON objects")
    plan, execution = controls["sample_plan"], controls["execution"]
    budget, measurement = controls["safety_budget"], controls["measurement"]
    accounting, acceptance = controls["accounting"], controls["acceptance"]
    pricing = controls["pricing_snapshot"]
    cases = contract.get("cases")
    if not isinstance(cases, list) or len(cases) != 3 or not all(isinstance(case, dict) for case in cases):
        raise EvidenceError("contract must define exactly three measured cases")
    if tuple(case.get("expected_tool") for case in cases) != EXPECTED_TOOLS:
        raise EvidenceError("contract cases must cover exactly the three DataSage tools in order")
    case_ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(set(case_ids)) != 3:
        raise EvidenceError("contract case ids must be unique non-empty strings")
    for case in cases:
        if not isinstance(case.get("expected_arguments"), dict):
            raise EvidenceError("every case must pin expected_arguments as an object")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise EvidenceError("every case must pin a non-empty prompt")
    fixed = (
        (plan.get("warmup_runs"), 1, "warmup_runs"),
        (plan.get("measured_runs_per_case"), 3, "measured_runs_per_case"),
        (plan.get("measured_sample_count"), 9, "measured_sample_count"),
        (execution.get("timeout_seconds"), 120, "timeout_seconds"),
        (execution.get("max_iterations_per_run"), 2, "max_iterations_per_run"),
        (execution.get("max_output_tokens_per_call"), 2048, "max_output_tokens_per_call"),
        (budget.get("max_successful_llm_calls"), 20, "max_successful_llm_calls"),
        (budget.get("max_total_peak_estimated_cost_usd"), "2.30", "cost budget"),
        (measurement.get("clock"), "time.perf_counter_ns", "clock"),
        (measurement.get("p50_algorithm"), "statistics.median", "p50 algorithm"),
        (
            measurement.get("p90_algorithm"),
            "statistics.quantiles(n=10,method='inclusive')[8]",
            "p90 algorithm",
        ),
        (
            measurement.get("raw_samples_hash_algorithm"),
            "sha256(canonical_json({warmup,samples}))",
            "raw samples hash algorithm",
        ),
        (acceptance.get("expected_tool_call_rule"), "exactly_one_and_equal", "tool call rule"),
        (acceptance.get("required_final_response"), DENIED_CODE, "final response"),
        (acceptance.get("database_runtime_entered"), False, "database runtime result"),
        (acceptance.get("required_api_calls_per_run"), 2, "api calls"),
        (acceptance.get("max_duration_ns"), 120_000_000_000, "duration"),
    )
    for actual, expected, label in fixed:
        if actual != expected:
            raise EvidenceError(f"contract {label} must remain {expected!r}")
    if _canonical_json_bytes(acceptance.get("required_tool_result")) != _canonical_json_bytes(
        DENIED_RESULT
    ):
        raise EvidenceError("contract tool result must remain the exact entitlement denial object")
    if plan.get("warmup_case_id") != cases[0]["id"]:
        raise EvidenceError("warmup must use the catalog denial case")
    if budget.get("budget_semantics") != (
        "warmup_plus_measured_cumulative_peak_pricing_test_safety_not_business_sla"
    ):
        raise EvidenceError("cost budget must cover warmup plus measured samples")
    if acceptance.get("threshold_basis") != "test_execution_safety_not_business_sla":
        raise EvidenceError("latency bound must remain a test safety bound")
    if not _is_hex_digest(acceptance.get("expected_tool_schema_sha256"), 64):
        raise EvidenceError("contract must pin the reviewed public tool schema hash")
    if accounting.get("usage_source") != "Hermes AIAgent.run_conversation result totals":
        raise EvidenceError("usage must come from official Hermes result totals")
    if accounting.get("total_tokens_equation") != (
        "input_tokens + cache_read_tokens + cache_write_tokens + output_tokens"
    ):
        raise EvidenceError("canonical total token equation drifted")
    if accounting.get("reasoning_tokens_semantics") != "subset_of_output_tokens":
        raise EvidenceError("reasoning token semantics drifted")
    if pricing.get("billing_period") != "peak" or pricing.get("model") != contract.get("model"):
        raise EvidenceError("pricing snapshot must remain peak and model-specific")
    if pricing.get("unit_tokens") != 1_000_000:
        raise EvidenceError("pricing unit must remain one million tokens")


def _git(root: Path, *args: str) -> str:
    canonical_root = root.resolve().as_posix()
    command = ["git", "-c", f"safe.directory={canonical_root}", "-C", canonical_root, *args]
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise EvidenceError(f"git attribution failed for {root}: {detail}")
    return completed.stdout.strip()


def _git_blob(root: Path, commit: str, relative_path: str) -> bytes:
    canonical_root = root.resolve().as_posix()
    command = [
        "git", "-c", f"safe.directory={canonical_root}", "-C", canonical_root,
        "show", f"{commit}:{relative_path}",
    ]
    completed = subprocess.run(command, check=False, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceError(
            f"commit-bound source is unavailable ({commit}:{relative_path}): {detail}"
        )
    return completed.stdout


def _bind_profile_environment() -> None:
    expected = PROFILE_ROOT.resolve()
    existing = os.environ.get("HERMES_HOME")
    if existing:
        try:
            observed = Path(existing).expanduser().resolve()
        except OSError as exc:
            raise EvidenceError(f"cannot resolve existing HERMES_HOME: {exc}") from exc
        if observed != expected:
            raise EvidenceError(f"HERMES_HOME already targets a different profile: {observed}")
    else:
        os.environ["HERMES_HOME"] = str(expected)
    if not HERMES_ROOT.is_dir():
        raise EvidenceError(f"pinned Hermes source root is missing: {HERMES_ROOT}")
    host = HERMES_ROOT.resolve()
    sys.path[:] = [item for item in sys.path if Path(item or os.curdir).resolve() != host]
    sys.path.insert(0, str(host))


def _assert_host_attribution(*, require_clean: bool) -> None:
    commit = _git(HERMES_ROOT, "rev-parse", "HEAD")
    if commit != PINNED_HERMES_COMMIT:
        raise EvidenceError(f"Hermes host commit is not pinned: {commit}")
    if require_clean and _git(HERMES_ROOT, "status", "--porcelain=v1", "--untracked-files=all"):
        raise EvidenceError("Hermes host Git subject is dirty")


def _assert_clean_worktrees() -> None:
    _assert_host_attribution(require_clean=True)
    for source in (CONTRACT_PATH, PRODUCER_PATH, TEST_PATH, SOUL_PATH):
        relative = source.relative_to(PROFILE_GIT_ROOT).as_posix()
        _git(PROFILE_GIT_ROOT, "ls-files", "--error-unmatch", "--", relative)
    profile_relative = PROFILE_ROOT.relative_to(PROFILE_GIT_ROOT).as_posix()
    dirty = _git(
        PROFILE_GIT_ROOT, "status", "--porcelain=v1", "--untracked-files=all",
        "--", profile_relative,
    )
    if dirty:
        raise EvidenceError("target profile is dirty; evidence source must be committed")


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise EvidenceError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host_version() -> str:
    try:
        return importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        try:
            import tomllib
            payload = tomllib.loads((HERMES_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
            return str(payload["project"]["version"])
        except (KeyError, OSError, ValueError) as exc:
            raise EvidenceError(f"cannot determine Hermes version: {exc}") from exc


def _load_identity(contract: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    receipt_module = _load_module(
        PROFILE_ROOT / "build_release_receipt.py", "datasage_performance_subject_receipt",
    )
    receipt = receipt_module.build_receipt()
    contract_subject = contract["subject"]
    if receipt.get("name") != contract_subject["name"]:
        raise EvidenceError("distribution name differs from the performance contract")
    if receipt.get("version") != contract_subject["version"]:
        raise EvidenceError("distribution version differs from the performance contract")
    content_hash = receipt.get("content_sha256")
    if not _is_hex_digest(content_hash, 64):
        raise EvidenceError("distribution content hash is invalid")
    profile_commit = _git(PROFILE_GIT_ROOT, "rev-parse", "HEAD")
    host_commit, host_version = _git(HERMES_ROOT, "rev-parse", "HEAD"), _host_version()
    if host_commit != contract["host"]["hermes_git_commit"]:
        raise EvidenceError("Hermes commit differs from the contract")
    if host_version != contract["host"]["hermes_version"]:
        raise EvidenceError("Hermes version differs from the contract")
    return {
        "subject": {
            "name": receipt["name"], "version": receipt["version"],
            "content_sha256": content_hash, "profile_git_commit": profile_commit,
        },
        "host": {"hermes_version": host_version, "hermes_git_commit": host_commit},
    }


def _commit_source_material(
    identity: Mapping[str, Mapping[str, str]], contract_path: Path,
) -> dict[str, Any]:
    commit = identity["subject"]["profile_git_commit"]

    def blob(path: Path) -> bytes:
        return _git_blob(
            PROFILE_GIT_ROOT, commit, path.relative_to(PROFILE_GIT_ROOT).as_posix(),
        )

    contract_blob, producer_blob, soul_blob = blob(contract_path), blob(PRODUCER_PATH), blob(SOUL_PATH)
    try:
        system_prompt = soul_blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"commit-bound SOUL.md is not UTF-8: {exc}") from exc
    return {
        "contract_sha256": _sha256_bytes(contract_blob),
        "producer_sha256": _sha256_bytes(producer_blob),
        "system_prompt": {
            "path": "SOUL.md", "sha256": _sha256_bytes(soul_blob), "content": system_prompt,
        },
    }


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _verified_import(name: str, root: Path = HERMES_ROOT) -> Any:
    module = importlib.import_module(name)
    module_file = getattr(module, "__file__", None)
    if not module_file or not _path_is_within(Path(module_file), root):
        raise EvidenceError(f"module {name} was not imported from pinned root {root}")
    return module


def _function_graph(root: Callable[..., Any]) -> Iterator[Callable[..., Any]]:
    pending: list[Any] = [root]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        if not inspect.isfunction(candidate) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        yield candidate
        wrapped = getattr(candidate, "__wrapped__", None)
        if inspect.isfunction(wrapped):
            pending.append(wrapped)
        for cell in candidate.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if inspect.isfunction(value):
                pending.append(value)


def _verify_handler_provenance(entries: Mapping[str, Any]) -> None:
    required = {
        "datasage_catalog": "datasage_catalog",
        "datasage_entity_resolve": "datasage_entity_resolve",
        "datasage_query": "entitlement_guarded_datasage_query",
    }
    for tool_name, target_name in required.items():
        functions = list(_function_graph(entries[tool_name].handler))
        if target_name not in {function.__name__ for function in functions}:
            raise EvidenceError(f"{tool_name} handler closure no longer contains {target_name}")
        for function in functions:
            source = inspect.getsourcefile(function)
            if not source or not _path_is_within(Path(source), PLUGIN_ROOT):
                raise EvidenceError(f"{tool_name} handler escapes the target plugin: {source}")


def _isolated_test_plugin_discovery(
    registry_module: Any,
) -> tuple[Any, dict[str, Any]]:
    """Probe the real plugin package without adopting host global state."""

    registry = registry_module.registry
    scope = registry.current_scope_key()
    registry_before = {
        name: registry.snapshot_registration(name, scope=scope)
        for name in EXPECTED_TOOLS
    }
    unique = hashlib.sha256(
        f"{id(registry)}:{time.perf_counter_ns()}".encode("ascii")
    ).hexdigest()[:16]
    package_name = f"datasage_performance_preflight_{unique}"
    probe_module = _load_module(
        PROFILE_ROOT / "tests" / "plugin_registration_probe.py",
        f"datasage_registration_probe_{unique}",
    )
    try:
        _plugin, registration = probe_module.probe_registration(
            PLUGIN_ROOT,
            package_name=package_name,
        )
        by_name = {tool["name"]: tool for tool in registration.tools}
        if set(by_name) != set(EXPECTED_TOOLS):
            raise EvidenceError("datasage-query toolset exposes an unexpected tool surface")
        entries = {
            name: SimpleNamespace(
                name=name,
                toolset=by_name[name]["toolset"],
                schema=by_name[name]["schema"],
                handler=by_name[name]["handler"],
            )
            for name in EXPECTED_TOOLS
        }
        _verify_handler_provenance(entries)
    finally:
        for name in [
            name
            for name in sys.modules
            if name == package_name or name.startswith(f"{package_name}.")
        ]:
            sys.modules.pop(name, None)
    for name, previous in registry_before.items():
        if registry.snapshot_registration(name, scope=scope) is not previous:
            raise EvidenceError(f"isolated registration probe polluted registry slot {name}")
    return registry, entries


def _prepare_host_boundary(
    *,
    load_credentials: bool,
    suppress_home_initialization: bool = False,
    import_agent_module: bool = True,
    isolated_test_state: bool = False,
) -> dict[str, Any]:
    """Bind and inspect the real host/plugin without constructing an agent."""
    _bind_profile_environment()
    _assert_host_attribution(require_clean=True)
    env_loader = _verified_import("hermes_cli.env_loader")
    if load_credentials:
        env_loader.load_hermes_dotenv(hermes_home=PROFILE_ROOT, project_env=HERMES_ROOT / ".env")
    config_module = _verified_import("hermes_cli.config")
    plugins_module = _verified_import("hermes_cli.plugins")
    registry_module = _verified_import("tools.registry")
    if isolated_test_state and (load_credentials or import_agent_module):
        raise EvidenceError("isolated test discovery cannot load credentials or AIAgent")
    original_initializer = config_module.ensure_hermes_home
    if suppress_home_initialization:
        # Unit preflight runs under a read-only profile. Only neutralize the
        # directory-creation side effect; config parsing and plugin decisions
        # remain the real Hermes implementation.
        config_module.ensure_hermes_home = lambda: None
    try:
        if isolated_test_state:
            registry, entries = _isolated_test_plugin_discovery(
                registry_module,
            )
        else:
            plugins_module.discover_plugins()
            registry = registry_module.registry
            entries = {}
            for tool_name in EXPECTED_TOOLS:
                entry = registry.get_entry(tool_name)
                if entry is None or entry.toolset != "datasage-query":
                    raise EvidenceError(f"target plugin did not register {tool_name}")
                entries[tool_name] = entry
            if set(registry.get_tool_names_for_toolset("datasage-query")) != set(EXPECTED_TOOLS):
                raise EvidenceError("datasage-query toolset exposes an unexpected tool surface")
            _verify_handler_provenance(entries)
    finally:
        config_module.ensure_hermes_home = original_initializer
    run_agent_spec = importlib.util.find_spec("run_agent")
    run_agent_origin = getattr(run_agent_spec, "origin", None)
    if not run_agent_origin or not _path_is_within(Path(run_agent_origin), HERMES_ROOT):
        raise EvidenceError("run_agent module spec is not owned by the pinned Hermes root")
    run_agent_module = _verified_import("run_agent") if import_agent_module else None
    return {
        "config": config_module, "plugins": plugins_module, "run_agent": run_agent_module,
        "registry": registry, "entries": entries,
        "module_paths": {
            "config": str(Path(config_module.__file__).resolve()),
            "plugins": str(Path(plugins_module.__file__).resolve()),
            "run_agent": str(Path(run_agent_origin).resolve()),
        },
    }


@contextmanager
def _database_tripwire(handler: Callable[..., Any]) -> Iterator[dict[str, bool]]:
    state = {"entered": False}
    replacements: list[tuple[Any, str, Any]] = []
    functions = list(_function_graph(handler))

    def tripwire(*_args: Any, **_kwargs: Any) -> Any:
        state["entered"] = True
        raise EvidenceError("database runtime tripwire activated")

    modules: dict[int, Any] = {}
    for function in functions:
        module = function.__globals__.get("db_runtime")
        if module is not None:
            modules[id(module)] = module
        if function.__name__ == "entitlement_guarded_datasage_query":
            runtime_handler = function.__globals__.get("runtime_guarded_datasage_query")
            if callable(runtime_handler):
                replacements.append((function.__globals__, "runtime_guarded_datasage_query", runtime_handler))
                function.__globals__["runtime_guarded_datasage_query"] = tripwire
    for module in modules.values():
        for attribute in ("load_pymysql", "connect", "execute"):
            original = getattr(module, attribute, None)
            if callable(original):
                replacements.append((module, attribute, original))
                setattr(module, attribute, tripwire)
    try:
        yield state
    finally:
        for owner, attribute, original in reversed(replacements):
            if isinstance(owner, dict):
                owner[attribute] = original
            else:
                setattr(owner, attribute, original)


def _configured_model(config: Mapping[str, Any]) -> tuple[str, str]:
    model_config = config.get("model")
    if not isinstance(model_config, dict):
        raise EvidenceError("config.model must be an object")
    provider = str(model_config.get("provider") or "").strip()
    model = model_config.get("default") or model_config.get("model") or ""
    if isinstance(model, dict):
        config_module = _verified_import("hermes_cli.config")
        model, _overrides = config_module.split_model_config_default(model)
    return provider, str(model).strip()


def _tool_schema_hash(tools: Any) -> str:
    if not isinstance(tools, list):
        raise EvidenceError("AIAgent.tools is not a list")
    try:
        ordered = sorted(tools, key=lambda item: item["function"]["name"])
        names = tuple(item["function"]["name"] for item in ordered)
    except (KeyError, TypeError) as exc:
        raise EvidenceError(f"AIAgent.tools contains an invalid public schema: {exc}") from exc
    if names != EXPECTED_TOOLS:
        raise EvidenceError(f"AIAgent public tools are not the exact reviewed set: {names}")
    return _sha256_json(ordered)


def _build_official_boundary(
    contract: Mapping[str, Any], system_prompt: str,
) -> Callable[[Mapping[str, Any]], tuple[dict[str, Any], bool, str]]:
    context = _prepare_host_boundary(load_credentials=True)
    config = context["config"].load_config()
    configured_provider, configured_model = _configured_model(config)
    if (configured_provider, configured_model) != (contract["provider"], contract["model"]):
        raise EvidenceError("active profile provider/model differs from the contract")
    runtime_module = _verified_import("hermes_cli.runtime_provider")
    runtime = runtime_module.resolve_runtime_provider(
        requested=configured_provider, target_model=configured_model,
    )
    if runtime.get("provider") != configured_provider:
        raise EvidenceError("Hermes resolved a different provider")
    agent_class = context["run_agent"].AIAgent

    def execute(case: Mapping[str, Any]) -> tuple[dict[str, Any], bool, str]:
        entry = context["entries"][str(case["expected_tool"])]
        agent = None
        with _database_tripwire(entry.handler) as database_state:
            try:
                agent = agent_class(
                    api_key=runtime.get("api_key"), base_url=runtime.get("base_url"),
                    provider=runtime.get("provider"), requested_provider=runtime.get("requested_provider"),
                    api_mode=runtime.get("api_mode"), credential_pool=runtime.get("credential_pool"),
                    model=configured_model, enabled_toolsets=["datasage-query"],
                    max_iterations=int(contract["execution"]["max_iterations_per_run"]),
                    max_tokens=int(contract["execution"]["max_output_tokens_per_call"]),
                    run_budget_seconds=float(contract["execution"]["timeout_seconds"]),
                    quiet_mode=True, platform="cli", session_db=None, fallback_model=None,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    checkpoints_enabled=False,
                )
                if set(agent.valid_tool_names) != set(EXPECTED_TOOLS):
                    raise EvidenceError(
                        f"AIAgent valid_tool_names expanded or contracted: {sorted(agent.valid_tool_names)}"
                    )
                schema_hash = _tool_schema_hash(agent.tools)
                if schema_hash != contract["acceptance"]["expected_tool_schema_sha256"]:
                    raise EvidenceError("actual AIAgent tool schema differs from the contract pin")
                result = agent.run_conversation(str(case["prompt"]), system_message=system_prompt)
                if not isinstance(result, dict):
                    raise EvidenceError("AIAgent.run_conversation did not return an object")
            finally:
                if agent is not None:
                    try:
                        agent.shutdown_memory_provider(getattr(agent, "_session_messages", []))
                    except Exception:
                        pass
                    agent.close()
        return result, database_state["entered"], schema_hash

    return execute


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _observed_tool_calls(messages: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        raise EvidenceError("Hermes result messages must be a list")
    for message in messages:
        if _field(message, "role") != "assistant":
            continue
        tool_calls = _field(message, "tool_calls", [])
        if tool_calls is None:
            continue
        if not isinstance(tool_calls, list):
            raise EvidenceError("assistant tool_calls must be a list")
        for call in tool_calls:
            function = _field(call, "function", {})
            name, arguments = _field(function, "name"), _field(function, "arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(
                        arguments, object_pairs_hook=_strict_object, parse_constant=_reject_constant,
                    )
                except ValueError as exc:
                    raise EvidenceError(f"tool arguments are not strict JSON: {exc}") from exc
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise EvidenceError("tool call must contain a name and parsed object arguments")
            calls.append({"name": name, "arguments": arguments})
    return calls


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        return "".join(_content_text(item) for item in content)
    return ""


def _tool_result(messages: Any) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        raise EvidenceError("Hermes result messages must be a list")
    for message in messages:
        if _field(message, "role") != "tool":
            continue
        try:
            payload = json.loads(
                _content_text(_field(message, "content")), object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except ValueError as exc:
            raise EvidenceError(f"tool result is not strict JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise EvidenceError("tool result must be a JSON object")
        results.append(payload)
    if len(results) != 1:
        raise EvidenceError(f"expected exactly one parsed tool result, observed {len(results)}")
    return results[0]


def _official_usage(result: Mapping[str, Any]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for field in USAGE_FIELDS:
        value = result.get(field)
        if type(value) is not int or value < 0:
            raise EvidenceError(f"Hermes result has invalid {field}")
        usage[field] = value
    expected_total = (
        usage["input_tokens"] + usage["cache_read_tokens"]
        + usage["cache_write_tokens"] + usage["output_tokens"]
    )
    if usage["total_tokens"] != expected_total:
        raise EvidenceError("Hermes usage totals violate the canonical token equation")
    if usage["reasoning_tokens"] > usage["output_tokens"]:
        raise EvidenceError("reasoning tokens must be a subset of output tokens")
    if usage["cache_write_tokens"] != 0:
        raise EvidenceError("cache-write usage cannot be bounded by the fixed pricing snapshot")
    return usage


def _peak_cost(usage: Mapping[str, int], pricing: Mapping[str, Any]) -> Decimal:
    unit = Decimal(int(pricing["unit_tokens"]))
    return (
        Decimal(usage["input_tokens"]) * Decimal(str(pricing["input_cache_miss_per_million"]))
        + Decimal(usage["cache_read_tokens"]) * Decimal(str(pricing["input_cache_hit_per_million"]))
        + Decimal(usage["output_tokens"]) * Decimal(str(pricing["output_per_million"]))
    ) / unit


def _validate_observation(
    sample: Mapping[str, Any], case: Mapping[str, Any], contract: Mapping[str, Any],
) -> None:
    expected_call = [{"name": case["expected_tool"], "arguments": case["expected_arguments"]}]
    if _canonical_json_bytes(sample["observed_tool_calls"]) != _canonical_json_bytes(expected_call):
        raise EvidenceError(f"{case['id']} did not make the exact contracted tool call")
    if _canonical_json_bytes(sample["tool_result"]) != _canonical_json_bytes(
        contract["acceptance"]["required_tool_result"]
    ):
        raise EvidenceError(f"{case['id']} did not return the contracted entitlement denial")
    if sample["final_response"].strip() != DENIED_CODE:
        raise EvidenceError(f"{case['id']} final response was not the exact denial code")
    if sample["database_runtime_entered"] is not False:
        raise EvidenceError(f"{case['id']} reached the database runtime")
    if sample["usage"]["api_calls"] != contract["acceptance"]["required_api_calls_per_run"]:
        raise EvidenceError(f"{case['id']} did not use exactly two official API calls")
    if sample["duration_ns"] > contract["acceptance"]["max_duration_ns"]:
        raise EvidenceError(f"{case['id']} exceeded the test execution timeout")


def _observe_run(
    case: Mapping[str, Any], contract: Mapping[str, Any],
    run_boundary: Callable[[Mapping[str, Any]], tuple[dict[str, Any], bool, str]],
    clock_ns: Callable[[], int], run_index: int,
) -> tuple[dict[str, Any], Decimal, str]:
    started = clock_ns()
    result, database_entered, schema_hash = run_boundary(case)
    duration_ns = clock_ns() - started
    if type(duration_ns) is not int or duration_ns < 0:
        raise EvidenceError("monotonic clock returned an invalid duration")
    if result.get("provider") != contract["provider"] or result.get("model") != contract["model"]:
        raise EvidenceError("Hermes result provider/model differs from the contract")
    final_response = result.get("final_response")
    if not isinstance(final_response, str):
        raise EvidenceError("Hermes result final_response must be a string")
    sample = {
        "case_id": str(case["id"]), "run_index": run_index, "duration_ns": duration_ns,
        "expected_tool": str(case["expected_tool"]),
        "observed_tool_calls": _observed_tool_calls(result.get("messages")),
        "tool_result": _tool_result(result.get("messages")),
        "database_runtime_entered": bool(database_entered),
        "final_response": final_response, "usage": _official_usage(result),
    }
    _validate_observation(sample, case, contract)
    return sample, _peak_cost(sample["usage"], contract["pricing_snapshot"]), schema_hash


def collect_evidence(
    *, contract_path: Path = CONTRACT_PATH,
    environment_binder: Callable[[], None] = _bind_profile_environment,
    clean_checker: Callable[[], None] = _assert_clean_worktrees,
    identity_loader: Callable[[Mapping[str, Any]], dict[str, dict[str, str]]] = _load_identity,
    source_loader: Callable[[Mapping[str, Mapping[str, str]], Path], dict[str, Any]] = _commit_source_material,
    boundary_factory: Callable[
        [Mapping[str, Any], str], Callable[[Mapping[str, Any]], tuple[dict[str, Any], bool, str]],
    ] = _build_official_boundary,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Collect only fully conforming raw observations; never emit a verdict."""
    environment_binder()
    clean_checker()
    contract = _read_json(contract_path)
    _validate_contract(contract)
    identity = identity_loader(contract)
    sources = source_loader(identity, contract_path)
    system_prompt = sources.get("system_prompt")
    if not isinstance(system_prompt, dict) or not isinstance(system_prompt.get("content"), str):
        raise EvidenceError("source loader did not return commit-bound SOUL content")
    for name in ("contract_sha256", "producer_sha256"):
        if not _is_hex_digest(sources.get(name), 64):
            raise EvidenceError(f"source loader returned invalid {name}")
    if system_prompt.get("path") != "SOUL.md" or not _is_hex_digest(system_prompt.get("sha256"), 64):
        raise EvidenceError("source loader returned invalid SOUL identity")
    run_boundary = boundary_factory(contract, system_prompt["content"])
    max_calls = int(contract["safety_budget"]["max_successful_llm_calls"])
    max_cost = Decimal(str(contract["safety_budget"]["max_total_peak_estimated_cost_usd"]))
    cumulative_calls, cumulative_cost = 0, Decimal("0")
    schema_hash: str | None = None

    def account(sample: Mapping[str, Any], cost: Decimal, observed_schema_hash: str) -> None:
        nonlocal cumulative_calls, cumulative_cost, schema_hash
        if observed_schema_hash != contract["acceptance"]["expected_tool_schema_sha256"]:
            raise EvidenceError("observed tool schema differs from the contract pin")
        if schema_hash is None:
            schema_hash = observed_schema_hash
        elif schema_hash != observed_schema_hash:
            raise EvidenceError("tool schema changed during evidence collection")
        cumulative_calls += sample["usage"]["api_calls"]
        cumulative_cost += cost
        if cumulative_calls > max_calls:
            raise EvidenceError("successful LLM calls exceeded the cumulative safety budget")
        if cumulative_cost > max_cost:
            raise EvidenceError("warmup plus measured peak cost exceeded the cumulative safety budget")

    warmup_id = contract["sample_plan"]["warmup_case_id"]
    warmup_case = next(case for case in contract["cases"] if case["id"] == warmup_id)
    warmup, warmup_cost, warmup_schema = _observe_run(
        warmup_case, contract, run_boundary, clock_ns, 0,
    )
    account(warmup, warmup_cost, warmup_schema)
    samples: list[dict[str, Any]] = []
    measured_runs = int(contract["sample_plan"]["measured_runs_per_case"])
    for case in contract["cases"]:
        for run_index in range(1, measured_runs + 1):
            sample, sample_cost, observed_schema = _observe_run(
                case, contract, run_boundary, clock_ns, run_index,
            )
            account(sample, sample_cost, observed_schema)
            samples.append(sample)
    if len(samples) != contract["sample_plan"]["measured_sample_count"]:
        raise EvidenceError("measured sample count differs from the contract")
    if schema_hash is None:
        raise EvidenceError("tool schema was not observed")
    return {
        "schema": REPORT_SCHEMA, "subject": identity["subject"], "host": identity["host"],
        "provider": contract["provider"], "model": contract["model"],
        "contract": {
            "path": contract_path.relative_to(PROFILE_ROOT).as_posix(),
            "sha256": sources["contract_sha256"],
        },
        "producer": {
            "path": PRODUCER_PATH.relative_to(PROFILE_ROOT).as_posix(),
            "sha256": sources["producer_sha256"],
        },
        "system_prompt": {"path": system_prompt["path"], "sha256": system_prompt["sha256"]},
        "tool_schema_sha256": schema_hash,
        "pricing_snapshot_sha256": _sha256_json(contract["pricing_snapshot"]),
        "warmup": warmup, "samples": samples,
    }


def _expected_output_path(profile_git_commit: str) -> Path:
    return PROFILE_ROOT / "pending" / "evidence" / f"performance-{profile_git_commit}.json"


def _write_report(output: Path, report: Mapping[str, Any]) -> None:
    expected = _expected_output_path(str(report["subject"]["profile_git_commit"]))
    if output.resolve() != expected.resolve():
        raise EvidenceError(f"output must be {expected}")
    if output.exists():
        raise EvidenceError(f"refusing to overwrite existing evidence: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True,
        help="pending/evidence/performance-<profile_git_commit>.json",
    )
    args = parser.parse_args(argv)
    try:
        report = collect_evidence()
        _write_report(args.output, report)
    except EvidenceError as exc:
        parser.exit(1, f"performance evidence collection refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
