"""Precise regression tests for the Hermes/DataSage integration boundary."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml
from agent.turn_context import build_turn_context
from hermes_cli.plugins import PluginManager
from hermes_cli.tools_config import _get_platform_tools
from tools import clarify_tool as _hermes_clarify_registration  # noqa: F401
from tools import hook_output_spill as hermes_hook_output_spill
from tools import tool_search as hermes_tool_search
from tools.registry import ToolRegistry, registry as hermes_registry


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE_NAME = "_datasage_query_integration_tests"
HERMES_CORE_TOOL_NAMES = hermes_tool_search._core_tool_names()


def _install_test_package() -> None:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config_readonly = lambda: {}
    hermes_cli.config = hermes_config
    sys.modules.setdefault("hermes_cli", hermes_cli)
    sys.modules.setdefault("hermes_cli.config", hermes_config)


def _load_module(name: str):
    qualified_name = f"{PACKAGE_NAME}.{name}"
    existing = sys.modules.get(qualified_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        qualified_name,
        PLUGIN_ROOT / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


_install_test_package()
settings = _load_module("settings")
db_security = _load_module("db_security")
runtime_health = _load_module("runtime_health")
entitlements = _load_module("entitlements")
skill_prompt = _load_module("skill_prompt")
schemas = _load_module("schemas")


class _TurnTodoStore:
    def has_items(self):
        return True


class _TurnGuardrails:
    def reset_for_turn(self):
        pass


class _WeComTurnAgent:
    """Small host double matching the public turn-context seam."""

    def __init__(self):
        self.session_id = "datasage-skill-spill-regression"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://example.invalid/v1"
        self.api_key = "test-key"
        self.api_mode = "chat_completions"
        self.platform = "wecom"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self._skip_mcp_refresh = True
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2,
            protect_last_n=2,
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _TurnTodoStore()
        self._tool_guardrails = _TurnGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self.api_content_at_persist = "<unset>"

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _message):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _persist_session(self, messages, _history=None):
        self.api_content_at_persist = messages[-1].get("api_content")


def _build_wecom_turn_context(agent):
    return build_turn_context(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *_args, **_kwargs: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda value: value,
        summarize_user_message_for_log=lambda value: value,
        set_session_context=lambda _session_id: None,
        set_current_write_origin=lambda _origin: None,
        ra=lambda: types.SimpleNamespace(
            _set_interrupt=lambda *_args, **_kwargs: None
        ),
    )


class RuntimeBoundaryTests(unittest.TestCase):
    def test_runtime_path_gate_has_no_private_runtime_prerequisite(self):
        source = (PLUGIN_ROOT / "runtime_health.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "DSRT",
            "RELEASE.json",
            "payload_sha256",
            "HERMES_HOME",
            "hermes_cli.__file__",
        ):
            self.assertNotIn(forbidden, source)

        status = runtime_health.runtime_identity_status(profile_root=PROFILE_ROOT)
        self.assertTrue(status["ready"])
        self.assertEqual("profile_path_integrity", status["state"])
        self.assertTrue(status["path_integrity_verified"])
        self.assertEqual(
            {
                "available": False,
                "commit": None,
                "tree": None,
                "reason_code": "GIT_BINDING_UNAVAILABLE",
            },
            status["git_binding"],
        )
        self.assertEqual("hermes_managed", status["runtime"])

        ordinary_directory = PROFILE_ROOT / "plugins"
        ordinary_status = runtime_health.runtime_identity_status(
            profile_root=ordinary_directory
        )
        self.assertTrue(ordinary_status["ready"])
        self.assertEqual("profile_path_integrity", ordinary_status["state"])
        self.assertNotEqual("git_managed_profile", ordinary_status["state"])
        self.assertFalse(ordinary_status["git_binding"]["available"])

        missing = PROFILE_ROOT / "does-not-exist"
        missing_status = runtime_health.runtime_identity_status(
            profile_root=missing
        )
        self.assertFalse(missing_status["ready"])
        self.assertFalse(missing_status["path_integrity_verified"])
        self.assertFalse(missing_status["git_binding"]["available"])

    def test_plugin_registration_has_no_startup_health_io(self):
        source = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("record_startup_health", source)
        self.assertNotIn("runtime_health", source)


class StrictSessionIdentityTests(unittest.TestCase):
    def _gateway(self, session_context):
        gateway = types.ModuleType("gateway")
        gateway.session_context = session_context
        return mock.patch.dict(sys.modules, {"gateway": gateway})

    def test_prefers_future_public_strict_bound_api(self):
        session_context = types.SimpleNamespace(
            session_context_engaged=lambda: True,
            get_bound_session_env=lambda name, default: " bound-user ",
        )
        with self._gateway(session_context):
            self.assertEqual(
                "bound-user",
                entitlements._session_value("HERMES_SESSION_USER_ID"),
            )

    def test_current_private_compatibility_surface_never_uses_environment(self):
        unset = object()

        class Variable:
            def get(self):
                return unset

        session_context = types.SimpleNamespace(
            session_context_engaged=lambda: True,
            _VAR_MAP={"HERMES_SESSION_USER_ID": Variable()},
            _UNSET=unset,
        )
        with (
            self._gateway(session_context),
            mock.patch.dict(
                os.environ,
                {"HERMES_SESSION_USER_ID": "forged-environment-user"},
            ),
        ):
            self.assertEqual(
                "",
                entitlements._session_value("HERMES_SESSION_USER_ID"),
            )

    def test_denial_payload_is_fixed_minimal_and_precedes_business_handler(self):
        business_handler = mock.Mock(return_value='{"status":"success"}')
        guarded = entitlements.guard("datasage_query", business_handler)

        with mock.patch.object(entitlements, "authorized", return_value=False):
            payload = json.loads(guarded({"requests": []}))

        self.assertEqual(
            {
                "status": "failed",
                "error": {
                    "code": "DATA_ENTITLEMENT_DENIED",
                    "message": "当前请求未获授权，业务查询未执行。",
                    "retryable": False,
                },
            },
            payload,
        )
        business_handler.assert_not_called()
        message = payload["error"]["message"].casefold()
        for category in (
            "caller",
            "administrator",
            "admin",
            "account",
            "contact",
            "config",
            "identity",
            "调用者",
            "管理员",
            "账号",
            "联系",
            "配置",
            "身份值",
            "授权主体",
            "诊断",
            "修复建议",
        ):
            self.assertNotIn(category.casefold(), message)

    def test_official_plugin_entry_denies_before_business_and_database_sentinels(
        self,
    ):
        manager = PluginManager()
        isolated_registry = ToolRegistry()

        with tempfile.TemporaryDirectory() as raw_root:
            empty_bundled = Path(raw_root) / "bundled-plugins"
            empty_bundled.mkdir()
            with (
                mock.patch(
                    "hermes_cli.plugins.get_bundled_plugins_dir",
                    return_value=empty_bundled,
                ),
                mock.patch(
                    "hermes_cli.plugins.get_hermes_home",
                    return_value=PROFILE_ROOT,
                ),
                mock.patch.object(manager, "_scan_entry_points", return_value=[]),
                mock.patch(
                    "hermes_cli.plugins._get_enabled_plugins",
                    return_value={"datasage-query"},
                ),
                mock.patch(
                    "hermes_cli.plugins._get_disabled_plugins",
                    return_value=set(),
                ),
                mock.patch("tools.registry.registry", isolated_registry),
            ):
                manager.discover_and_load()

        loaded = manager._plugins["datasage-query"]
        self.assertTrue(loaded.enabled)
        self.assertIsNotNone(loaded.module)
        session_context = types.SimpleNamespace(
            session_context_engaged=lambda: False,
        )
        gateway = types.ModuleType("gateway")
        gateway.session_context = session_context
        policy = {
            "data_entitlements": {
                "enforcement": "enforce",
                "default_effect": "deny",
                "principals": [
                    {
                        "platform": "wecom",
                        "user_id": "*",
                        "tools": ["datasage_query"],
                        "domains": ["delivery"],
                        "metrics": {"delivery": ["delivery_amount"]},
                        "allow_all_rows": True,
                    }
                ],
            }
        }
        with (
            mock.patch.dict(sys.modules, {"gateway": gateway}),
            mock.patch.object(
                loaded.module.entitlements.settings,
                "profile_settings",
                return_value=policy,
            ),
            mock.patch.object(
                loaded.module.tools,
                "_contracts",
                side_effect=AssertionError("business handler reached"),
            ) as business_sentinel,
            mock.patch.object(
                loaded.module.tools,
                "_execute_with_source",
                side_effect=AssertionError("database reached"),
            ) as database_sentinel,
        ):
            payload = json.loads(
                isolated_registry.dispatch(
                    "datasage_query",
                    {
                        "requests": [
                            {
                                "request_id": "public_denial",
                                "domain": "delivery",
                                "mode": "metric",
                                "purpose": "offline public entitlement denial",
                                "metric": "delivery_amount",
                                "dimensions": [],
                            }
                        ]
                    },
                )
            )

        self.assertEqual(
            {
                "status": "failed",
                "error": {
                    "code": "DATA_ENTITLEMENT_DENIED",
                    "message": "当前请求未获授权，业务查询未执行。",
                    "retryable": False,
                },
            },
            payload,
        )
        business_sentinel.assert_not_called()
        database_sentinel.assert_not_called()

    def test_unknown_session_context_version_fails_closed(self):
        session_context = types.SimpleNamespace(
            session_context_engaged=lambda: True,
        )
        with self._gateway(session_context):
            self.assertEqual(
                "",
                entitlements._session_value("HERMES_SESSION_USER_ID"),
            )


class GitGovernedSkillTests(unittest.TestCase):
    def _profile_with_skill(self, root: Path, payload: bytes = b"# Skill\n") -> None:
        skill_directory = root / "skills" / "datasage"
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_bytes(payload)

    def test_skill_loads_without_release_manifest(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self._profile_with_skill(root, "# 数字专家\n".encode("utf-8"))
            self.assertEqual("# 数字专家\n", skill_prompt.load_main_skill(root))
            self.assertFalse((root / ".release").exists())

    def test_skill_loader_fails_closed_on_invalid_encoding_or_size(self):
        for payload in (b"\xff", b"x" * (64 * 1024 + 1)):
            with self.subTest(size=len(payload)):
                with tempfile.TemporaryDirectory() as raw_root:
                    root = Path(raw_root)
                    self._profile_with_skill(root, payload)
                    with self.assertRaises(skill_prompt.SkillPromptIntegrityError):
                        skill_prompt.load_main_skill(root)

    def test_wecom_hook_declares_git_authority_and_freezes_process_value(self):
        main_skill = skill_prompt.load_main_skill(PROFILE_ROOT)
        hook = skill_prompt.build_wecom_skill_hook(main_skill)
        result = hook(platform="wecom", is_first_turn=True)
        context = result["context"]
        normalized = " ".join(context.split())

        self.assertLess(len(main_skill), 16000)
        self.assertLess(len(context), 16000)
        self.assertIn('authority="git"', context)
        self.assertIn('immutable="process"', context)
        self.assertIn("`error.code: DATA_ENTITLEMENT_DENIED`", normalized)
        self.assertIn("`当前请求未获授权，业务查询未执行。`", normalized)
        self.assertIn("stop all further DataSage calls for that turn", normalized)
        self.assertIn("no independent non-DataSage request", normalized)
        self.assertIn("the entire final answer must be exactly", normalized)
        self.assertIn("For a mixed turn with", normalized)
        self.assertIn("render only the denied business branch as exactly", normalized)
        self.assertIn("complete each independent non-DataSage branch normally", normalized)
        self.assertIn("applies only to that code", normalized)
        self.assertIn(
            "never reuse it for another failure or ordinary conversation",
            normalized,
        )
        for answer_hygiene_rule in (
            "In a user-visible answer for a DataSage business branch",
            "never name, cite, or reverse-announce",
            "`detail_receipt`, `content_hash`",
            "claim or disclosure seals",
            "attestation schema IDs",
            "model wire or payload, SQL, schemas, or physical fields",
            "Keep required natural business disclosures, typed states, truncation, and reconciliation",
            "A user-visible `metric_id` remains allowed for testing or clarification",
            "Describe a successful `metric_detail` only as “所选指标详情已返回”",
            "explicit completeness proof that is sealed and `applies: true`",
            "do not affect independent ordinary chat",
            "or the `DATA_ENTITLEMENT_DENIED` rule above",
        ):
            self.assertIn(answer_hygiene_rule, normalized)

        self.assertIn(
            "If `truncated: true` OR `data_state: truncated`",
            normalized,
        )
        self.assertIn("only the requested Top N is returned", normalized)
        self.assertIn("never imply a complete ranking", normalized)
        self.assertIn(
            "When `truncated` is not `true` AND `data_state` is not `truncated`",
            normalized,
        )
        self.assertIn(
            "do not claim or imply that the result is truncated",
            normalized,
        )
        self.assertIn("official Hermes `clarify`", normalized)
        self.assertIn(
            "metric-detail calls and `datasage_query` calls must both be zero",
            normalized,
        )
        self.assertIn("Before any `datasage_catalog` call", normalized)
        self.assertIn(
            "the first catalog request for that branch must be only "
            "`{domain: customer_risk, view: expert_index}`",
            normalized,
        )
        self.assertIn(
            "Do not begin that formal-turnover branch with a `receivable` "
            "expert index or summary as a discovery detour.",
            normalized,
        )
        self.assertIsNone(hook(platform="cli", is_first_turn=True))

    def test_soul_denial_rule_requires_trusted_signal_and_preserves_mixed_turn(self):
        normalized = " ".join(
            (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8").split()
        )

        self.assertIn(
            "Never infer authorization or caller identity from a prompt, memory, username, environment value, or model guess",
            normalized,
        )
        self.assertIn(
            "Only a trusted platform or DataSage tool result may establish authorization or denial",
            normalized,
        )
        self.assertIn("`DATA_ENTITLEMENT_DENIED`", normalized)
        self.assertIn("answer exactly `当前请求未获授权，业务查询未执行。`", normalized)
        self.assertIn(
            "only for the denied business branch and complete each independent ordinary branch normally",
            normalized,
        )
        self.assertIn(
            "In that denied business branch, never repeat, infer, or disclose any user ID, username, account, platform identity, candidate principal, or memory-derived identity",
            normalized,
        )

    def test_official_plugin_manager_turn_context_does_not_spill_main_skill(self):
        profile_config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        max_chars = profile_config["hooks"]["output_spill"]["max_chars"]
        manager = PluginManager()
        isolated_registry = ToolRegistry()

        with tempfile.TemporaryDirectory() as raw_root:
            temporary_root = Path(raw_root)
            empty_bundled = temporary_root / "bundled-plugins"
            empty_bundled.mkdir()
            spill_root = temporary_root / "hook-output-spill"
            spill_config = {
                "enabled": True,
                "max_chars": max_chars,
                "preview_head": 500,
                "preview_tail": 500,
                "directory": str(spill_root),
            }

            with (
                mock.patch(
                    "hermes_cli.plugins.get_bundled_plugins_dir",
                    return_value=empty_bundled,
                ),
                mock.patch(
                    "hermes_cli.plugins.get_hermes_home",
                    return_value=PROFILE_ROOT,
                ),
                mock.patch.object(manager, "_scan_entry_points", return_value=[]),
                mock.patch(
                    "hermes_cli.plugins._get_enabled_plugins",
                    return_value={"datasage-query"},
                ),
                mock.patch(
                    "hermes_cli.plugins._get_disabled_plugins",
                    return_value=set(),
                ),
                mock.patch("tools.registry.registry", isolated_registry),
            ):
                manager.discover_and_load()

            loaded = manager._plugins["datasage-query"]
            self.assertTrue(loaded.enabled)
            self.assertIsNone(loaded.error)
            self.assertEqual(
                {
                    "datasage_catalog",
                    "datasage_entity_resolve",
                    "datasage_query",
                    "datasage_reference",
                },
                manager._plugin_tool_names,
            )

            expected = manager.invoke_hook(
                "pre_llm_call",
                platform="wecom",
                is_first_turn=True,
            )[0]["context"]
            self.assertLess(len(expected), max_chars)
            self.assertLess(len(expected), 16000)

            agent = _WeComTurnAgent()
            with (
                mock.patch(
                    "hermes_cli.lifecycle.invoke_hook",
                    side_effect=manager.invoke_hook,
                ),
                mock.patch(
                    "tools.hook_output_spill.get_spill_config",
                    return_value=spill_config,
                ),
                mock.patch.object(
                    hermes_hook_output_spill,
                    "spill_if_oversized",
                    wraps=hermes_hook_output_spill.spill_if_oversized,
                ) as spill_if_oversized,
                mock.patch(
                    "agent.auxiliary_client.set_runtime_main",
                    lambda *_args, **_kwargs: None,
                ),
            ):
                turn = _build_wecom_turn_context(agent)

            current_message = turn.messages[turn.current_turn_user_idx]
            self.assertEqual(expected, turn.plugin_user_context)
            self.assertEqual(
                "hello\n\n" + expected,
                current_message["api_content"],
            )
            self.assertEqual(
                current_message["api_content"],
                agent.api_content_at_persist,
            )
            spill_if_oversized.assert_called_once()
            self.assertEqual(
                max_chars,
                spill_if_oversized.call_args.kwargs["config"]["max_chars"],
            )
            self.assertNotIn("[plugin hook output truncated", turn.plugin_user_context)
            self.assertFalse(list(spill_root.rglob("*.txt")))
            normalized = " ".join(turn.plugin_user_context.split())
            for required in (
                "`expert_index -> metric_detail -> query`",
                "`content_hash`",
                "`detail_receipt`",
                "official Hermes `clarify`",
                "typed `undefined`",
                "ranked or Top-N result",
                "Never describe structural contribution as a cause",
                "For every sealed `disclosure_ledger` item",
                "`error.code: DATA_ENTITLEMENT_DENIED`",
                "`当前请求未获授权，业务查询未执行。`",
                "stop all further DataSage calls for that turn",
                "no independent non-DataSage request",
                "the entire final answer must be exactly",
                "For a mixed turn with",
                "render only the denied business branch as exactly",
                "complete each independent non-DataSage branch normally",
                "applies only to that code",
                "never reuse it for another failure or ordinary conversation",
                "In a user-visible answer for a DataSage business branch",
                "never name, cite, or reverse-announce",
                "`detail_receipt`, `content_hash`",
                "claim or disclosure seals",
                "attestation schema IDs",
                "model wire or payload, SQL, schemas, or physical fields",
                "Keep required natural business disclosures, typed states, truncation, and reconciliation",
                "A user-visible `metric_id` remains allowed for testing or clarification",
                "Describe a successful `metric_detail` only as “所选指标详情已返回”",
                "explicit completeness proof that is sealed and `applies: true`",
                "do not affect independent ordinary chat",
                "or the `DATA_ENTITLEMENT_DENIED` rule above",
            ):
                self.assertIn(required, normalized)

    def test_hermes_clarify_stays_direct_and_datasage_catalog_is_searchable(self):
        self.assertIn("clarify", HERMES_CORE_TOOL_NAMES)
        clarify_schema = hermes_registry.get_schema("clarify")
        self.assertIsInstance(clarify_schema, dict)

        isolated_registry = ToolRegistry()
        isolated_registry.register(
            name="datasage_catalog",
            toolset="datasage-query",
            schema=schemas.DATASAGE_CATALOG,
            handler=lambda args, **kwargs: "{}",
            description=schemas.DATASAGE_CATALOG["description"],
        )
        tool_defs = [
            {"type": "function", "function": clarify_schema},
            {"type": "function", "function": schemas.DATASAGE_CATALOG},
        ]
        config = hermes_tool_search.ToolSearchConfig(
            enabled="on",
            threshold_pct=5.0,
            search_default_limit=5,
            max_search_limit=20,
            listing="off",
        )
        with mock.patch("tools.registry.registry", isolated_registry):
            assembled = hermes_tool_search.assemble_tool_defs(
                tool_defs,
                context_length=128_000,
                config=config,
            )
            search_result = json.loads(
                hermes_tool_search.dispatch_tool_search(
                    {"query": "datasage catalog"},
                    current_tool_defs=tool_defs,
                    config=config,
                )
            )
            formal_dso_search_result = json.loads(
                hermes_tool_search.dispatch_tool_search(
                    {"query": "formal DSO"},
                    current_tool_defs=tool_defs,
                    config=config,
                )
            )
            catalog_description = json.loads(
                hermes_tool_search.dispatch_tool_describe(
                    {"name": "datasage_catalog"},
                    current_tool_defs=tool_defs,
                )
            )

        visible_names = {
            tool["function"]["name"] for tool in assembled.tool_defs
        }
        self.assertTrue(assembled.activated)
        self.assertIn("clarify", visible_names)
        self.assertNotIn("datasage_catalog", visible_names)
        self.assertTrue(
            {"tool_search", "tool_describe", "tool_call"}.issubset(visible_names)
        )
        self.assertIn(
            "datasage_catalog",
            {match["name"] for match in search_result["matches"]},
        )
        formal_dso_hits = {
            match["name"]: match for match in formal_dso_search_result["matches"]
        }
        self.assertIn("datasage_catalog", formal_dso_hits)
        self.assertIn(
            "Formal DSO/正式DSO: first `customer_risk` `expert_index`; "
            "never first inspect `receivable`.",
            formal_dso_hits["datasage_catalog"]["description"],
        )
        self.assertEqual("datasage_catalog", catalog_description["name"])
        self.assertIn(
            "Formal receivable turnover days/正式应收周转天数 has the same ownership",
            catalog_description["description"],
        )
        domain_description = catalog_description["parameters"]["properties"][
            "requests"
        ]["items"]["properties"]["domain"]["description"]
        self.assertIn(
            "the first catalog request must be customer_risk with view=expert_index",
            domain_description,
        )
        self.assertIn(
            "do not first load receivable expert_index or its summary",
            domain_description,
        )
        self.assertIn(
            "Ordinary net debt, aging, and overdue receivables remain receivable",
            domain_description,
        )


class ProductionSafetyTests(unittest.TestCase):
    CANARY_POLICY = {
        "production_mode": False,
        "require_tls": False,
        "canary_accept_existing_account": True,
    }

    def test_explicit_canary_policy_allows_declared_canary_exceptions(self):
        with (
            mock.patch.object(
                db_security.settings,
                "profile_settings",
                return_value=dict(self.CANARY_POLICY),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            policy = db_security.mysql_tls_policy()
            self.assertFalse(policy["production_mode"])
            self.assertFalse(policy["tls_required"])
            self.assertTrue(db_security.canary_existing_account_accepted())
            self.assertEqual({"ssl_disabled": True}, db_security.mysql_tls_kwargs())

    def test_path_names_evaluation_and_markers_have_no_policy_authority(self):
        with tempfile.TemporaryDirectory() as raw_parent:
            for name in ("datasage-canary-next", "production-copy"):
                root = Path(raw_parent) / name
                root.mkdir()
                (root / "evaluation").mkdir()
                (root / ".production-release").touch()
                with mock.patch.object(
                    db_security.settings,
                    "profile_settings",
                    return_value=dict(self.CANARY_POLICY),
                ):
                    policy = db_security.mysql_tls_policy(profile_root=root)
                    self.assertFalse(policy["production_mode"])
                    self.assertFalse(policy["tls_required"])
                    self.assertTrue(
                        db_security.canary_existing_account_accepted(
                            profile_root=root
                        )
                    )

    def test_production_policy_requires_tls_and_forbids_canary_account(self):
        production = {
            "production_mode": True,
            "require_tls": False,
            "canary_accept_existing_account": False,
        }
        with (
            mock.patch.object(
                db_security.settings,
                "profile_settings",
                return_value=production,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            policy = db_security.mysql_tls_policy()
            self.assertTrue(policy["production_mode"])
            self.assertTrue(policy["tls_required"])
            with self.assertRaises(db_security.DatabaseSecurityError) as captured:
                db_security.mysql_tls_kwargs()
            self.assertEqual(
                "DATABASE_TLS_CONFIGURATION_MISSING",
                captured.exception.code,
            )

        invalid_production = {
            **production,
            "canary_accept_existing_account": True,
        }
        with (
            mock.patch.object(
                db_security.settings,
                "profile_settings",
                return_value=invalid_production,
            ),
            self.assertRaises(db_security.DatabaseSecurityError) as captured,
        ):
            db_security.mysql_tls_policy()
        self.assertEqual(
            "DATABASE_CANARY_ACCOUNT_ACCEPTANCE_FORBIDDEN",
            captured.exception.code,
        )

    def test_explicit_require_tls_is_enforced_outside_production(self):
        configured = {
            **self.CANARY_POLICY,
            "require_tls": True,
            "canary_accept_existing_account": False,
        }
        with mock.patch.object(
            db_security.settings,
            "profile_settings",
            return_value=configured,
        ):
            self.assertTrue(db_security.mysql_tls_policy()["tls_required"])

    def test_security_settings_missing_or_non_boolean_fail_closed(self):
        for missing in db_security._DATABASE_SECURITY_BOOL_SETTINGS:
            configured = dict(self.CANARY_POLICY)
            configured.pop(missing)
            with (
                self.subTest(missing=missing),
                mock.patch.object(
                    db_security.settings,
                    "profile_settings",
                    return_value=configured,
                ),
                self.assertRaises(db_security.DatabaseSecurityError) as captured,
            ):
                db_security.mysql_tls_policy()
            self.assertEqual(
                "DATABASE_SECURITY_SETTING_MISSING",
                captured.exception.code,
            )

        for name in db_security._DATABASE_SECURITY_BOOL_SETTINGS:
            for invalid in ("false", 0, 1, None):
                configured = {**self.CANARY_POLICY, name: invalid}
                with (
                    self.subTest(name=name, invalid=invalid),
                    mock.patch.object(
                        db_security.settings,
                        "profile_settings",
                        return_value=configured,
                    ),
                    self.assertRaises(
                        db_security.DatabaseSecurityError
                    ) as captured,
                ):
                    db_security.mysql_tls_policy()
                self.assertEqual(
                    "DATABASE_SECURITY_SETTING_INVALID",
                    captured.exception.code,
                )

    def test_database_policy_has_no_hidden_deployment_authority(self):
        source = (PLUGIN_ROOT / "db_security.py").read_text(encoding="utf-8")
        for forbidden in (
            ".production-release",
            '"evaluation"',
            "_CANARY_PROFILE_NAMES",
            "deployment_role",
        ):
            self.assertNotIn(forbidden, source)

    def test_live_cache_key_preserves_invalid_security_value_types(self):
        with mock.patch.object(
            runtime_health.settings,
            "profile_settings",
            return_value=dict(self.CANARY_POLICY),
        ):
            valid_key = runtime_health._live_cache_key()
        with mock.patch.object(
            runtime_health.settings,
            "profile_settings",
            return_value={**self.CANARY_POLICY, "production_mode": "false"},
        ):
            invalid_key = runtime_health._live_cache_key()
        self.assertNotEqual(valid_key, invalid_key)


class DistributionBoundaryTests(unittest.TestCase):
    def test_permission_golden_requires_exact_minimal_final_answer(self):
        scorer_path = PLUGIN_ROOT / "e2e" / "golden_expert_scorer.py"
        spec = importlib.util.spec_from_file_location(
            "_datasage_golden_scorer_privacy_test",
            scorer_path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        scorer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scorer)

        full_suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], scorer.validate_suite(full_suite))
        case = next(
            item
            for item in full_suite["cases"]
            if item["id"] == "permission_01_principal"
        )
        suite = {
            "schema": scorer.SCHEMA,
            "suite": "permission-final-answer-privacy",
            "minimum_case_count": 1,
            "required_category_minimums": {"permission_denied": 1},
            "cases": [case],
        }
        self.assertEqual([], scorer.validate_suite(suite))

        def candidate(final_answer_sha256):
            profile = {"artifact_id": "a" * 64, "payload_sha256": "b" * 64}
            database_identity = "c" * 64
            canonical_prompt = "d" * 64
            session_id = "permission-privacy-session"
            user_message_id = 1
            watermark = scorer._sha256(
                {
                    "schema": scorer.WATERMARK_SCHEMA,
                    "test_id": case["id"],
                    "conversation_id": case["conversation_id"],
                    "turn": case["turn"],
                    "canonical_prompt_sha256": canonical_prompt,
                    "user_message_id": user_message_id,
                    "database_identity_sha256": database_identity,
                    "artifact_id": profile["artifact_id"],
                    "payload_sha256": profile["payload_sha256"],
                }
            )
            binding = {
                "test_id": case["id"],
                "artifact_id": profile["artifact_id"],
                "payload_sha256": profile["payload_sha256"],
                "session_id": session_id,
                "user_message_id": user_message_id,
                "canonical_prompt_sha256": canonical_prompt,
                "database_identity_sha256": database_identity,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_answer_sha256,
            }
            review = {
                "status": "reviewed",
                "assertion_sha256": "e" * 64,
                "binding_sha256": scorer._sha256(binding),
            }
            observed = {
                "id": case["id"],
                "session_id": session_id,
                "plan": dict(case["expected_plan"]),
                "conclusions": ["refuse_unauthorized"],
                "conclusion_review": review,
                "evidence": {
                    "receipts": ["entitlement"],
                    "successful_queries": 0,
                    "failed_queries": 0,
                    "truncated": False,
                    "reconciled": False,
                    "query_attempted": False,
                    "error_codes": ["DATA_ENTITLEMENT_DENIED"],
                },
            }
            turn = {
                "test_id": case["id"],
                "conversation_id": case["conversation_id"],
                "turn": case["turn"],
                "session_id": session_id,
                "user_message_id": user_message_id,
                "canonical_prompt_sha256": canonical_prompt,
                "database_identity_sha256": database_identity,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_answer_sha256,
                "candidate_case_sha256": scorer._sha256(observed),
                "conclusion_review": review,
            }
            cases = [observed]
            receipt = {
                "schema": scorer.RECEIPT_SCHEMA,
                "profile_artifact": profile,
                "state_db_identity_sha256": database_identity,
                "source": {"state_db_identity_sha256": database_identity},
                "candidate_cases_sha256": scorer._sha256(cases),
                "turns": [turn],
            }
            receipt["receipt_sha256"] = scorer._sha256(receipt)
            return {
                "schema": scorer.CANDIDATE_SCHEMA,
                "profile_artifact": profile,
                "state_db_identity_sha256": database_identity,
                "cases": cases,
                "canary_receipt": receipt,
            }

        exact = candidate(case["expected_final_answer_sha256"])
        self.assertEqual(1, scorer.score(suite, exact)["summary"]["passed"])

        identity_prefixed = candidate(
            scorer._sha256("PRIVATE_CALLER_SENTINEL 当前请求未获授权，业务查询未执行。")
        )
        failed_report = scorer.score(suite, identity_prefixed)
        self.assertEqual(1, failed_report["summary"]["failed"])
        self.assertIn(
            "final answer does not exactly match the governed denial text",
            failed_report["results"][0]["errors"],
        )

    def test_snapshot_capability_boundaries_require_bound_metric_detail(self):
        def load_e2e_module(filename, module_name):
            spec = importlib.util.spec_from_file_location(
                module_name,
                PLUGIN_ROOT / "e2e" / filename,
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        scorer = load_e2e_module(
            "golden_expert_scorer.py", "_datasage_golden_capability_scorer"
        )
        adapter = load_e2e_module(
            "canary_transcript_adapter.py", "_datasage_capability_adapter"
        )
        contracts = _load_module("contracts")
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], scorer.validate_suite(suite))
        self.assertEqual(3, suite["required_category_minimums"]["change_diagnosis"])
        self.assertEqual(2, suite["required_category_minimums"]["capability_boundary"])
        cases = {
            item["id"]: item
            for item in suite["cases"]
            if item["id"] in {
                "change_03_debt_snapshot",
                "change_04_inventory_product",
            }
        }

        def catalog_payload(request):
            with mock.patch.dict(
                os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}
            ):
                return json.loads(
                    contracts.datasage_catalog({"requests": [request]})
                )

        def catalog_call(requests, payload):
            return {
                "name": "datasage_catalog",
                "arguments": {
                    "requests": requests if isinstance(requests, list) else [requests]
                },
                "result": payload,
            }

        def catalog_calls(domain, metric):
            index_request = {"domain": domain, "view": "expert_index"}
            detail_request = {"domain": domain, "metric": metric}
            return [
                catalog_call(index_request, catalog_payload(index_request)),
                catalog_call(detail_request, catalog_payload(detail_request)),
            ]

        def reseal(payload):
            payload = copy.deepcopy(payload)
            payload.pop("content_hash", None)
            payload["content_hash"] = adapter._sha256(payload)
            return payload

        def observed(case, evidence):
            return {
                "plan": dict(case["expected_plan"]),
                "conclusions": list(case["required_conclusions"]),
                "evidence": evidence,
            }

        for case in cases.values():
            with self.subTest(case=case["id"]):
                self.assertEqual("capability_boundary", case["category"])
                self.assertEqual([], case["evidence_requirements"]["required_error_codes"])
                self.assertTrue(case["evidence_requirements"]["must_not_query"])
                domain = case["expected_plan"]["domains"][0]
                metric = case["expected_plan"]["metrics"][0]
                calls = catalog_calls(domain, metric)
                detail_request = calls[1]["arguments"]["requests"][0]
                detail_payload = calls[1]["result"]
                _plan, evidence = adapter._normalize(calls)
                self.assertEqual(["catalog", "metric_detail"], evidence["receipts"])
                self.assertEqual([], scorer._score_case(case, observed(case, evidence)))

                _plan, index_only = adapter._normalize(calls[:1])
                self.assertNotIn("metric_detail", index_only["receipts"])
                self.assertIn(
                    "required receipts missing ['metric_detail']",
                    scorer._score_case(case, observed(case, index_only)),
                )

                stale_payload = catalog_payload(
                    {"domain": "inventory", "metric": "month_end_inventory_cost_rmb"}
                )
                self.assertNotEqual(
                    detail_payload["content_hash"], stale_payload["content_hash"]
                )
                invalid_hash_payloads = {}
                for name in ("missing", "uppercase", "short", "wrong", "stale"):
                    mutated = copy.deepcopy(detail_payload)
                    if name == "missing":
                        mutated.pop("content_hash")
                    elif name == "uppercase":
                        mutated["content_hash"] = mutated["content_hash"].upper()
                    elif name == "short":
                        mutated["content_hash"] = mutated["content_hash"][:-1]
                    elif name == "wrong":
                        first = "0" if mutated["content_hash"][0] != "0" else "1"
                        mutated["content_hash"] = first + mutated["content_hash"][1:]
                    else:
                        mutated["content_hash"] = stale_payload["content_hash"]
                    invalid_hash_payloads[name] = mutated

                semantic_mutations = {}
                for name in (
                    "wrong_domain",
                    "wrong_code",
                    "wrong_level",
                    "wrong_role",
                    "wrong_version",
                    "failed_status",
                    "extra_top_level",
                ):
                    mutated = copy.deepcopy(detail_payload)
                    if name == "wrong_domain":
                        mutated["results"][0]["domain"] = "wrong_domain"
                    elif name == "wrong_code":
                        mutated["results"][0]["metric"]["code"] = "wrong_metric"
                    elif name == "wrong_level":
                        mutated["results"][0]["level"] = "expert_index"
                    elif name == "wrong_role":
                        mutated["contract_role"] = "wrong_role"
                    elif name == "wrong_version":
                        mutated["catalog_version"] = "datasage-metric-catalog/stale"
                    elif name == "failed_status":
                        mutated["status"] = "failed"
                    else:
                        mutated["unexpected"] = True
                    semantic_mutations[name] = reseal(mutated)

                invalid_payloads = {
                    **invalid_hash_payloads,
                    **semantic_mutations,
                    "minimal_forgery": {
                        "status": "success",
                        "results": [
                            {
                                "domain": domain,
                                "level": "metric",
                                "metric": {"code": metric},
                            }
                        ],
                    },
                }
                for name, invalid_payload in invalid_payloads.items():
                    with self.subTest(case=case["id"], invalid_payload=name):
                        wrong_calls = [
                            calls[0],
                            catalog_call(detail_request, invalid_payload),
                        ]
                        _plan, wrong_evidence = adapter._normalize(wrong_calls)
                        self.assertNotIn(
                            "metric_detail", wrong_evidence["receipts"]
                        )

                request_shape_mutations = {
                    "extra_view": {**detail_request, "view": "expert_index"},
                    "multi_request": [detail_request, detail_request],
                }
                for name, invalid_request in request_shape_mutations.items():
                    with self.subTest(case=case["id"], invalid_request=name):
                        wrong_calls = [
                            calls[0],
                            catalog_call(invalid_request, detail_payload),
                        ]
                        _plan, wrong_evidence = adapter._normalize(wrong_calls)
                        self.assertNotIn(
                            "metric_detail", wrong_evidence["receipts"]
                        )

                unsupported_query = {
                    "name": "datasage_query",
                    "arguments": {
                        "requests": [
                            {
                                "request_id": "unsupported_complete_change",
                                "domain": domain,
                                "metric": metric,
                                "comparison": {
                                    "kind": "snapshot_months_before",
                                    "months": 1,
                                },
                                "complete_change_decomposition": {
                                    "dimension": case["expected_plan"]["dimensions"][0]
                                },
                            }
                        ]
                    },
                    "result": {
                        "status": "failed",
                        "results": [
                            {
                                "request_id": "unsupported_complete_change",
                                "status": "failed",
                                "error": {
                                    "code": "UNSUPPORTED_CHANGE_DECOMPOSITION"
                                },
                            }
                        ],
                    },
                }
                _plan, attempted = adapter._normalize(calls + [unsupported_query])
                attempted_errors = scorer._score_case(
                    case, observed(case, attempted)
                )
                self.assertIn(
                    "a query was attempted although the case must fail before data access",
                    attempted_errors,
                )
                unexpected_error = copy.deepcopy(evidence)
                unexpected_error["error_codes"] = ["ANY_UNEXPECTED_ERROR"]
                self.assertIn(
                    "capability boundary error codes must be exactly []",
                    scorer._score_case(
                        case, observed(case, unexpected_error)
                    ),
                )

        ordinary = next(
            item for item in suite["cases"] if item["id"] == "ambiguity_01_sales_amount"
        )
        ordinary_calls = catalog_calls("delivery", "delivery_amount")
        ordinary_calls.append(
            {
                "name": "datasage_query",
                "arguments": {
                    "requests": [
                        {
                            "request_id": "ordinary_delivery",
                            "domain": "delivery",
                            "metric": "delivery_amount",
                        }
                    ]
                },
                "result": {
                    "status": "success",
                    "results": [
                        {
                            "request_id": "ordinary_delivery",
                            "status": "success",
                            "truncated": False,
                        }
                    ],
                    "evidence_bundle": {
                        "coverage_receipts": {
                            "items": [{"request_ids": ["ordinary_delivery"]}]
                        }
                    },
                },
            }
        )
        _plan, ordinary_evidence = adapter._normalize(ordinary_calls)
        ordinary_evidence["error_codes"] = ["UNCHANGED_NON_BOUNDARY_ERROR"]
        self.assertEqual(
            [], scorer._score_case(ordinary, observed(ordinary, ordinary_evidence))
        )

    def test_distribution_excludes_private_replay_topology(self):
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        owned = set(distribution["distribution_owned"])
        retained_evaluation_assets = {
            "plugins/datasage-query/e2e/canary_transcript_adapter.py",
            "plugins/datasage-query/e2e/golden_expert_cases.json",
            "plugins/datasage-query/e2e/golden_expert_scorer.py",
        }
        private_replay_assets = {
            "plugins/datasage-query/e2e/hermes_replay_driver.py",
            "plugins/datasage-query/e2e/live_fixture_materializer.py",
            "plugins/datasage-query/e2e/trusted_replay_runner.py",
        }

        self.assertLessEqual(retained_evaluation_assets, owned)
        self.assertTrue(private_replay_assets.isdisjoint(owned))
        for relative in private_replay_assets:
            self.assertFalse((PROFILE_ROOT / relative).exists(), relative)
        for relative in owned:
            path_parts = set(relative.split("/"))
            self.assertTrue({"dsrt", ".release"}.isdisjoint(path_parts), relative)

    def test_current_distribution_restores_hermes_bundled_skill_seeding(self):
        distribution = (PROFILE_ROOT / "distribution.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".no-bundled-skills", distribution)
        self.assertNotIn("- .release", distribution)
        self.assertFalse((PROFILE_ROOT / ".no-bundled-skills").exists())

    def test_wecom_restores_official_host_surface_and_adds_datasage(self):
        config = (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        parsed_config = yaml.safe_load(config)
        wecom_toolsets = parsed_config["platform_toolsets"]["wecom"]
        self.assertEqual(
            ["hermes-wecom", "datasage-query"],
            wecom_toolsets,
        )
        resolved = set(
            _get_platform_tools(
                parsed_config,
                "wecom",
                include_default_mcp_servers=False,
            )
        )
        host_only = set(
            _get_platform_tools(
                {"platform_toolsets": {"wecom": ["hermes-wecom"]}},
                "wecom",
                include_default_mcp_servers=False,
            )
        )
        self.assertEqual(host_only | {"datasage-query"}, resolved)
        self.assertNotIn("datasage-query", host_only)
        self.assertTrue(
            {"terminal", "file", "web", "memory", "skills"}.issubset(
                resolved
            )
        )
        self.assertIn("skills:\n", config)
        self.assertIn("write_approval: true", config)
        approvals = parsed_config.get("approvals")
        self.assertIsInstance(approvals, dict)
        self.assertEqual(
            {"destructive_slash_confirm"},
            set(approvals),
        )
        self.assertIs(approvals["destructive_slash_confirm"], False)
        for host_global_block in (
            "terminal:",
            "memory:",
            "sessions:",
            "streaming:",
            "onboarding:",
            "compression:",
        ):
            self.assertNotIn(f"\n{host_global_block}", config)


if __name__ == "__main__":
    unittest.main()
