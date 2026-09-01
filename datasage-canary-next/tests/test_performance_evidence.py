from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PROFILE_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROFILE_ROOT / "tests" / "run_performance_evidence.py"
CONTRACT_PATH = PROFILE_ROOT / "tests" / "fixtures" / "performance_non_db_contract.json"


def _load_runner():
    spec = importlib.util.spec_from_file_location("datasage_performance_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import performance evidence runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class IncrementingClock:
    def __init__(self, step: int = 100_000_000) -> None:
        self.value = 0
        self.step = step

    def __call__(self) -> int:
        current = self.value
        self.value += self.step
        return current


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _fake_identity(_contract_payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        "subject": {
            "name": "datasage-canary-next",
            "version": "0.15.0-rc9",
            "content_sha256": "a" * 64,
            "profile_git_commit": "b" * 40,
        },
        "host": {
            "hermes_version": "0.20.5",
            "hermes_git_commit": runner.PINNED_HERMES_COMMIT,
        },
    }


def _fake_sources(_identity, _contract_path) -> dict[str, Any]:
    return {
        "contract_sha256": "d" * 64,
        "producer_sha256": "e" * 64,
        "system_prompt": {
            "path": "SOUL.md",
            "sha256": "f" * 64,
            "content": "tracked test system prompt",
        },
    }


def _fake_result(
    case: dict[str, Any],
    *,
    arguments: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    input_tokens: int = 100,
    total_tokens: int | None = None,
    final_response: str = "Error: DATA_ENTITLEMENT_DENIED",
) -> dict[str, Any]:
    tool_call = types.SimpleNamespace(
        function=types.SimpleNamespace(
            name=case["expected_tool"],
            arguments=json.dumps(
                arguments if arguments is not None else case["expected_arguments"],
                ensure_ascii=False,
            ),
        ),
    )
    denial = tool_result if tool_result is not None else {
        "status": "failed",
        "error": {
            "code": "DATA_ENTITLEMENT_DENIED",
            "message": "当前请求未获授权，业务查询未执行。",
            "retryable": False,
        },
    }
    cache_read_tokens, output_tokens = 10, 20
    canonical_total = input_tokens + cache_read_tokens + output_tokens
    return {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "messages": [
            types.SimpleNamespace(role="assistant", tool_calls=[tool_call]),
            {"role": "tool", "content": json.dumps(denial, ensure_ascii=False)},
            {"role": "assistant", "content": final_response},
        ],
        "final_response": final_response,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": 5,
        "total_tokens": canonical_total if total_tokens is None else total_tokens,
        "api_calls": 2,
    }


def _fake_factory(observed: list[tuple[str, str]] | None = None, **result_overrides):
    schema_hash = _contract()["acceptance"]["expected_tool_schema_sha256"]

    def factory(_contract_payload, system_prompt):
        def boundary(case):
            if observed is not None:
                observed.append((case["id"], system_prompt))
            return _fake_result(case, **result_overrides), False, schema_hash

        return boundary

    return factory


def _collect(**overrides):
    arguments = {
        "contract_path": CONTRACT_PATH,
        "environment_binder": lambda: None,
        "clean_checker": lambda: None,
        "identity_loader": _fake_identity,
        "source_loader": _fake_sources,
        "boundary_factory": _fake_factory(),
        "clock_ns": IncrementingClock(),
    }
    arguments.update(overrides)
    return runner.collect_evidence(**arguments)


class PerformanceEvidenceTests(unittest.TestCase):
    def test_fake_official_boundary_produces_commit_bound_raw_schema(self) -> None:
        observed: list[tuple[str, str]] = []
        report = _collect(boundary_factory=_fake_factory(observed))
        self.assertEqual(
            set(report),
            {
                "schema", "subject", "host", "provider", "model", "contract",
                "producer", "system_prompt", "tool_schema_sha256",
                "pricing_snapshot_sha256", "warmup", "samples",
            },
        )
        self.assertEqual(report["schema"], "datasage-performance-evidence/v1")
        self.assertEqual(report["subject"]["name"], "datasage-canary-next")
        self.assertEqual(report["host"]["hermes_git_commit"], runner.PINNED_HERMES_COMMIT)
        self.assertEqual(report["contract"], {
            "path": "tests/fixtures/performance_non_db_contract.json", "sha256": "d" * 64,
        })
        self.assertEqual(report["producer"], {
            "path": "tests/run_performance_evidence.py", "sha256": "e" * 64,
        })
        self.assertEqual(report["system_prompt"], {"path": "SOUL.md", "sha256": "f" * 64})
        self.assertEqual(
            report["tool_schema_sha256"],
            _contract()["acceptance"]["expected_tool_schema_sha256"],
        )
        self.assertEqual(len(report["samples"]), 9)
        self.assertEqual(len(observed), 10)
        self.assertTrue(all(prompt == "tracked test system prompt" for _case, prompt in observed))
        self.assertEqual(report["warmup"]["run_index"], 0)
        self.assertEqual(report["warmup"]["case_id"], "catalog_denied_before_database")
        expected_tools = (
            ["datasage_catalog"] * 3
            + ["datasage_entity_resolve"] * 3
            + ["datasage_query"] * 3
        )
        self.assertEqual([sample["expected_tool"] for sample in report["samples"]], expected_tools)
        self.assertEqual(
            [sample["run_index"] for sample in report["samples"]],
            [1, 2, 3, 1, 2, 3, 1, 2, 3],
        )
        for sample in [report["warmup"], *report["samples"]]:
            self.assertEqual(
                set(sample),
                {
                    "case_id", "run_index", "duration_ns", "expected_tool",
                    "observed_tool_calls", "tool_result", "database_runtime_entered",
                    "final_response", "usage",
                },
            )
            self.assertEqual(sample["duration_ns"], 100_000_000)
            self.assertEqual(len(sample["observed_tool_calls"]), 1)
            self.assertIsInstance(sample["observed_tool_calls"][0]["arguments"], dict)
            self.assertEqual(sample["tool_result"]["error"]["code"], "DATA_ENTITLEMENT_DENIED")
            self.assertIs(sample["database_runtime_entered"], False)
            self.assertEqual(sample["final_response"], "Error: DATA_ENTITLEMENT_DENIED")
            self.assertEqual(set(sample["usage"]), set(runner.USAGE_FIELDS))
        encoded = json.dumps(report, ensure_ascii=False)
        for forbidden in ('"passed"', '"eligible"', '"p50"', '"p90"', '"cost_usd"'):
            self.assertNotIn(forbidden, encoded)

    def test_wrong_hermes_home_fails_before_clean_check_or_model(self) -> None:
        events: list[str] = []

        def clean_checker():
            events.append("clean")

        def boundary_factory(_contract_payload, _system_prompt):
            events.append("model")
            raise AssertionError("model boundary must not be built")

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(PROFILE_ROOT.parent / "wrong")}, clear=False):
            with self.assertRaisesRegex(runner.EvidenceError, "different profile"):
                runner.collect_evidence(
                    contract_path=CONTRACT_PATH,
                    clean_checker=clean_checker,
                    identity_loader=_fake_identity,
                    source_loader=_fake_sources,
                    boundary_factory=boundary_factory,
                    clock_ns=IncrementingClock(),
                )
        self.assertEqual(events, [])

    def test_clean_worktree_guard_rejects_a_dirty_profile(self) -> None:
        real_git = runner._git

        def dirty_profile(root, *args):
            if root == runner.PROFILE_GIT_ROOT and args[:2] == (
                "status",
                "--porcelain=v1",
            ):
                return " M datasage-canary-next/tests/run_performance_evidence.py"
            return real_git(root, *args)

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}, clear=False):
            runner._bind_profile_environment()
            with (
                mock.patch.object(runner, "_git", side_effect=dirty_profile),
                self.assertRaisesRegex(runner.EvidenceError, "target profile is dirty"),
            ):
                runner._assert_clean_worktrees()

    def test_invalid_arguments_are_rejected_not_recorded(self) -> None:
        with self.assertRaisesRegex(runner.EvidenceError, "exact contracted tool call"):
            _collect(boundary_factory=_fake_factory(arguments={"forged": True}))

    def test_expected_arguments_comparison_is_json_type_sensitive(self) -> None:
        schema_hash = _contract()["acceptance"]["expected_tool_schema_sha256"]

        def factory(_contract_payload, _system_prompt):
            def boundary(case):
                arguments = dict(case["expected_arguments"])
                if case["id"] == "entity_denied_before_database":
                    arguments["limit"] = True
                return _fake_result(case, arguments=arguments), False, schema_hash

            return boundary

        with self.assertRaisesRegex(runner.EvidenceError, "exact contracted tool call"):
            _collect(boundary_factory=factory)

    def test_tool_result_rejects_additional_success_or_data_fields(self) -> None:
        result = {
            **_contract()["acceptance"]["required_tool_result"],
            "success": False,
            "data": [],
        }
        with self.assertRaisesRegex(runner.EvidenceError, "contracted entitlement denial"):
            _collect(boundary_factory=_fake_factory(tool_result=result))

    def test_required_tool_result_comparison_is_json_type_sensitive(self) -> None:
        contract = _contract()
        contract["acceptance"]["required_tool_result"]["error"]["retryable"] = 0

        with self.assertRaisesRegex(runner.EvidenceError, "exact entitlement denial object"):
            runner._validate_contract(contract)

    def test_final_response_requires_standalone_denial_code(self) -> None:
        report = _collect(
            boundary_factory=_fake_factory(
                final_response="Error: DATA_ENTITLEMENT_DENIED"
            )
        )
        self.assertEqual(
            "Error: DATA_ENTITLEMENT_DENIED",
            report["samples"][0]["final_response"],
        )
        for final_response in (
            "",
            "查询失败",
            "XDATA_ENTITLEMENT_DENIED",
            "xDATA_ENTITLEMENT_DENIED",
            "DATA_ENTITLEMENT_DENIED_DETAIL",
            "DATA_ENTITLEMENT_DENIEDdetail",
        ):
            with self.subTest(final_response=final_response), self.assertRaisesRegex(
                runner.EvidenceError, "standalone denial code"
            ):
                _collect(boundary_factory=_fake_factory(final_response=final_response))

    def test_official_token_total_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(runner.EvidenceError, "canonical token equation"):
            _collect(boundary_factory=_fake_factory(total_tokens=999))

    def test_official_preflight_schema_matches_real_agent_without_network(self) -> None:
        probe = r'''
import importlib.util, json, os, socket, sys, types
from pathlib import Path
from unittest import mock

profile = Path(sys.argv[1]).resolve()
os.environ["HERMES_HOME"] = str(profile)
spec = importlib.util.spec_from_file_location(
    "datasage_performance_agent_schema_probe",
    profile / "tests" / "run_performance_evidence.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
contract = json.loads((profile / "tests" / "fixtures" / "performance_non_db_contract.json").read_text(encoding="utf-8"))
context = module._prepare_host_boundary(
    load_credentials=False,
    suppress_home_initialization=True,
    import_agent_module=True,
)
agent_class = context["run_agent"].AIAgent
hermes_logging = module._verified_import("hermes_logging")
offline_client = types.SimpleNamespace(close=lambda: None)
with (
    mock.patch.object(context["config"], "ensure_hermes_home", lambda: None),
    mock.patch.object(hermes_logging, "setup_logging", lambda *_args, **_kwargs: None),
    mock.patch.object(agent_class, "_create_openai_client", return_value=offline_client),
    mock.patch.object(socket, "create_connection", side_effect=AssertionError("network blocked")),
    mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network blocked")),
):
    agent = agent_class(
        api_key="offline-test-key", base_url="https://api.deepseek.com/v1",
        provider="deepseek", requested_provider="deepseek", api_mode="chat_completions",
        model=contract["model"], enabled_toolsets=["datasage-query"],
        max_iterations=2, max_tokens=2048, run_budget_seconds=120,
        quiet_mode=True, platform="cli", session_db=None, fallback_model=None,
        skip_context_files=True, skip_memory=True, skip_background_review=True,
        checkpoints_enabled=False,
    )
    try:
        assert set(agent.valid_tool_names) == set(module.EXPECTED_TOOLS)
        preflight_hash = module._tool_schema_hash(context["public_tools"])
        agent_hash = module._tool_schema_hash(agent.tools)
        assert preflight_hash == agent_hash
        assert agent_hash == contract["acceptance"]["expected_tool_schema_sha256"]
    finally:
        agent.close()
'''
        environment = dict(os.environ)
        environment["HERMES_HOME"] = str(PROFILE_ROOT)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(PROFILE_ROOT)],
            cwd=PROFILE_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"fresh-process AIAgent schema probe failed:\n{completed.stdout}\n{completed.stderr}",
        )

    def test_warmup_plus_measured_peak_cost_budget_is_cumulative(self) -> None:
        with self.assertRaisesRegex(runner.EvidenceError, "warmup plus measured peak cost"):
            _collect(boundary_factory=_fake_factory(input_tokens=600_000))

    def test_database_tripwire_observes_and_blocks_runtime_entry(self) -> None:
        database_module = types.SimpleNamespace()

        def original_execute(*_args, **_kwargs):
            return "would enter database"

        database_module.execute = original_execute
        namespace: dict[str, Any] = {"db_runtime": database_module}
        exec("def inner(args):\n    return db_runtime.execute(args)\n", namespace)
        inner = namespace["inner"]

        def outer(args):
            return inner(args)

        with runner._database_tripwire(outer) as state:
            with self.assertRaisesRegex(runner.EvidenceError, "tripwire"):
                outer({"request": "test"})
            self.assertIs(state["entered"], True)
        self.assertIs(database_module.execute, original_execute)

    def test_real_no_network_host_plugin_preflight_and_schema_pin(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}, clear=False):
            context = runner._prepare_host_boundary(
                load_credentials=False,
                suppress_home_initialization=True,
                import_agent_module=False,
                isolated_test_state=True,
            )
        self.assertIsNone(context["run_agent"])
        for module_name in ("config", "plugins", "model_tools", "run_agent"):
            module_path = Path(context["module_paths"][module_name]).resolve()
            self.assertTrue(runner._path_is_within(module_path, runner.HERMES_ROOT))
        self.assertEqual(set(context["entries"]), set(runner.EXPECTED_TOOLS))
        runner._verify_handler_provenance(context["entries"])
        self.assertEqual(
            runner._tool_schema_hash(context["public_tools"]),
            _contract()["acceptance"]["expected_tool_schema_sha256"],
        )

    def test_real_release_receipt_identity_matches_contract_without_model(self) -> None:
        contract = _contract()

        identity = runner._load_identity(contract)

        self.assertEqual(identity["subject"]["name"], contract["subject"]["name"])
        self.assertEqual(identity["subject"]["version"], contract["subject"]["version"])
        self.assertRegex(identity["subject"]["content_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            identity["host"]["hermes_git_commit"],
            contract["host"]["hermes_git_commit"],
        )

    def test_preflight_isolates_and_restores_polluted_registry_and_plugin_module(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}, clear=False):
            runner._bind_profile_environment()
            registry_module = runner._verified_import("tools.registry")
            plugins_module = runner._verified_import("hermes_cli.plugins")
            original_registry = registry_module.registry
            polluted_registry = registry_module.ToolRegistry()
            scope = polluted_registry.current_scope_key()

            def stale_handler(_args):
                return "stale"

            for tool_name in runner.EXPECTED_TOOLS:
                polluted_registry.register(
                    name=tool_name,
                    toolset="datasage-query",
                    schema={"name": tool_name, "description": "stale", "parameters": {}},
                    handler=stale_handler,
                    scope=scope,
                )
            stale_module_name = "hermes_plugins.datasage_query"
            prior_module = __import__("sys").modules.get(stale_module_name)
            stale_module = types.ModuleType(stale_module_name)
            stale_module.__file__ = str(PROFILE_ROOT.parent / "wrong" / "__init__.py")
            prior_bare_scope = dict(plugins_module._BARE_MODULE_SCOPE)
            registry_module.registry = polluted_registry
            __import__("sys").modules[stale_module_name] = stale_module
            plugins_module._BARE_MODULE_SCOPE[stale_module_name] = "stale-scope"
            try:
                context = runner._prepare_host_boundary(
                    load_credentials=False,
                    suppress_home_initialization=True,
                    import_agent_module=False,
                    isolated_test_state=True,
                )
                self.assertEqual(set(context["entries"]), set(runner.EXPECTED_TOOLS))
                runner._verify_handler_provenance(context["entries"])
                self.assertIs(registry_module.registry, polluted_registry)
                self.assertIs(__import__("sys").modules[stale_module_name], stale_module)
                self.assertEqual(
                    plugins_module._BARE_MODULE_SCOPE[stale_module_name],
                    "stale-scope",
                )
            finally:
                registry_module.registry = original_registry
                if prior_module is None:
                    __import__("sys").modules.pop(stale_module_name, None)
                else:
                    __import__("sys").modules[stale_module_name] = prior_module
                plugins_module._BARE_MODULE_SCOPE.clear()
                plugins_module._BARE_MODULE_SCOPE.update(prior_bare_scope)

    def test_contract_pins_algorithms_sources_budget_and_arguments(self) -> None:
        contract = _contract()
        runner._validate_contract(contract)
        self.assertEqual(contract["subject"]["name"], "datasage-canary-next")
        self.assertEqual(contract["host"]["hermes_git_commit"], runner.PINNED_HERMES_COMMIT)
        self.assertEqual(contract["sample_plan"]["measured_sample_count"], 9)
        self.assertEqual(contract["measurement"]["clock"], "time.perf_counter_ns")
        self.assertEqual(contract["measurement"]["p50_algorithm"], "statistics.median")
        self.assertEqual(
            contract["measurement"]["p90_algorithm"],
            "statistics.quantiles(n=10,method='inclusive')[8]",
        )
        self.assertEqual(
            contract["measurement"]["raw_samples_hash_algorithm"],
            "sha256(canonical_json({warmup,samples}))",
        )
        self.assertEqual(contract["acceptance"]["required_tool_result"], runner.DENIED_RESULT)
        self.assertEqual(
            contract["acceptance"]["required_final_response_code"], runner.DENIED_CODE
        )
        self.assertEqual(contract["safety_budget"]["max_total_peak_estimated_cost_usd"], "2.30")
        self.assertTrue(all(isinstance(case["expected_arguments"], dict) for case in contract["cases"]))


if __name__ == "__main__":
    unittest.main()
