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
from hermes_cli.plugins import PluginManager
from hermes_cli.tools_config import _get_platform_tools
from tools import clarify_tool as _hermes_clarify_registration  # noqa: F401
from tools import tool_search as hermes_tool_search
from tools.registry import ToolRegistry, registry as hermes_registry


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
SKILL_PATH = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "SKILL.md"
)
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
schemas = _load_module("schemas")


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

    def test_registered_scorecard_requires_entitlement_for_every_bundle_metric(self):
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
        self.assertIsNone(loaded.error)
        specs = loaded.module.entitlements.SCORECARD_METRICS
        domains = sorted({str(spec["domain"]) for spec in specs})
        metrics = {
            domain: sorted(
                {
                    str(spec["metric"])
                    for spec in specs
                    if spec["domain"] == domain
                }
            )
            for domain in domains
        }
        full_rule = {
            "tools": ["datasage_catalog"],
            "domains": domains,
            "metrics": metrics,
            "allow_catalog_discovery": True,
        }

        def invoke(rule):
            with (
                mock.patch.dict(
                    os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}
                ),
                mock.patch.object(
                    loaded.module.entitlements.settings,
                    "profile_settings",
                    return_value={"data_entitlements": {}},
                ),
                mock.patch.object(
                    loaded.module.entitlements,
                    "_principal_rule",
                    return_value=rule,
                ),
            ):
                return json.loads(
                    isolated_registry.dispatch(
                        "datasage_catalog",
                        {"requests": [{"view": "performance_scorecard"}]},
                    )
                )

        allowed = invoke(full_rule)
        self.assertEqual("success", allowed["status"], allowed)
        self.assertEqual(
            "datasage-catalog-model-wire/v3", allowed["model_wire_version"]
        )
        denied_rule = copy.deepcopy(full_rule)
        first_domain = str(specs[0]["domain"])
        denied_rule["metrics"][first_domain].remove(str(specs[0]["metric"]))
        denied = invoke(denied_rule)
        self.assertEqual("DATA_ENTITLEMENT_DENIED", denied["error"]["code"])


class GitGovernedSkillTests(unittest.TestCase):
    def test_plugin_exposes_skill_and_no_answer_mutation_hooks(self):
        main_skill = SKILL_PATH.read_text(encoding="utf-8")
        registration = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        manifest = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        self.assertLess(len(main_skill), 6_000)
        self.assertEqual(
            {
                "datasage_catalog",
                "datasage_entity_resolve",
                "datasage_query",
            },
            set(manifest["provides_tools"]),
        )
        self.assertNotIn("provides_hooks", manifest)
        self.assertEqual(3, registration.count("ctx.register_tool("))
        self.assertEqual(1, registration.count("ctx.register_system_prompt_section("))
        self.assertNotIn("ctx.register_hook(", registration)
        self.assertNotIn("answer_guard", registration)
        self.assertNotIn("transform_llm_output", registration)
        self.assertNotIn("post_tool_call", registration)
        self.assertNotIn("pre_llm_call", registration)
        self.assertNotIn("frozen_wecom_skill_hook", registration)
        self.assertIn("requires_toolsets: [datasage-query]", main_skill)

    def test_soul_denial_rule_requires_trusted_signal_and_preserves_mixed_turn(self):
        normalized = " ".join(
            (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("The DataSage plugin owns metric definitions", normalized)
        self.assertIn("permissions", normalized)
        self.assertIn("If one branch fails", (
            SKILL_PATH
        ).read_text(encoding="utf-8"))
        self.assertNotIn("DATA_ENTITLEMENT_DENIED", normalized)

    def test_official_plugin_manager_renders_one_static_evidence_section(self):
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
            self.assertIsNone(loaded.error)
            self.assertEqual(
                {
                    "datasage_catalog",
                    "datasage_entity_resolve",
                    "datasage_query",
                },
                manager._plugin_tool_names,
            )
            self.assertEqual(
                {"datasage.evidence-boundaries"},
                set(manager._system_prompt_sections),
            )
            registered_section = manager._system_prompt_sections[
                "datasage.evidence-boundaries"
            ]
            self.assertIsInstance(registered_section.content, str)
            self.assertEqual("after_memory", registered_section.position)
            self.assertEqual(900, registered_section.max_chars)
            self.assertLessEqual(len(registered_section.content), 900)

            rendered = manager.render_system_prompt_sections(
                {
                    "session_id": "alpha5-prompt-section-test",
                    "platform": "wecom",
                    "profile_name": "datasage-expert-next",
                }
            )
            self.assertEqual(1, len(rendered))
            self.assertEqual("datasage.evidence-boundaries", rendered[0].id)
            self.assertEqual(registered_section.content.strip(), rendered[0].content)
            self.assertIn("scope compatibility", rendered[0].content)
            self.assertIn("governed benchmark", rendered[0].content)
            self.assertIn("arithmetic relationships", rendered[0].content)

            for hook_name in (
                "transform_llm_output",
                "post_tool_call",
                "pre_llm_call",
            ):
                with self.subTest(hook_name=hook_name):
                    self.assertEqual(
                        [],
                        manager.invoke_hook(
                            hook_name,
                            platform="wecom",
                            is_first_turn=True,
                        ),
                    )
            # Only the bounded static evidence section is always on. The full
            # expert workflow remains an ordinary Hermes on-demand Skill.

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
        self.assertEqual("datasage_catalog", catalog_description["name"])
        self.assertIn("governed", catalog_description["description"].casefold())
        domain_description = catalog_description["parameters"]["properties"][
            "requests"
        ]["items"]["properties"]["domain"]["description"]
        self.assertNotIn("first catalog request must", domain_description)
        self.assertNotIn("do not first load", domain_description)


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
    def test_permission_golden_requires_semantic_denial_without_fixed_model_text(self):
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
            "plan_constraint_semantics": "required-and-forbidden-subsets/v1",
            "minimum_case_count": 1,
            "required_category_minimums": {"permission_denied": 1},
            "cases": [case],
        }
        self.assertEqual([], scorer.validate_suite(suite))

        def candidate(final_answer_sha256, conclusions=None):
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
                "plan": dict(case["plan_constraints"]),
                "conclusions": list(conclusions or ["refuse_unauthorized"]),
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

        first_wording = candidate(scorer._sha256("semantic refusal wording one"))
        second_wording = candidate(scorer._sha256("semantic refusal wording two"))
        self.assertEqual(1, scorer.score(suite, first_wording)["summary"]["passed"])
        self.assertEqual(1, scorer.score(suite, second_wording)["summary"]["passed"])

        leaked = candidate(
            scorer._sha256("refusal that exposes protected data"),
            ["refuse_unauthorized", "reveal_data"],
        )
        failed_report = scorer.score(suite, leaked)
        self.assertEqual(1, failed_report["summary"]["failed"])
        self.assertTrue(
            any(
                "reveal_data" in error
                for error in failed_report["results"][0]["errors"]
            )
        )

    def test_department_scorecard_reviewed_raw_draft_blocks_generation_release(self):
        scorer_path = PLUGIN_ROOT / "e2e" / "golden_expert_scorer.py"
        spec = importlib.util.spec_from_file_location(
            "_datasage_department_scorecard_release_scorer",
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
        self.assertEqual(36, full_suite["minimum_case_count"])
        self.assertEqual(6, full_suite["required_category_minimums"]["ambiguity"])
        case = next(
            item
            for item in full_suite["cases"]
            if item["id"] == "ambiguity_06_department_performance_scorecard"
        )
        constraints = case["plan_constraints"]
        self.assertEqual([], constraints["domains"])
        self.assertEqual([], constraints["metrics"])
        self.assertEqual(["department"], constraints["dimensions"])
        self.assertEqual(
            ["performance_scorecard_first"],
            constraints["operations"],
        )
        self.assertEqual(
            {"fixed_scorecard_bundle", "fixed_metric_count", "fixed_call_order"},
            set(constraints["must_not_operations"]),
        )
        self.assertEqual(
            "2026H1_flows_with_current_overdue_snapshot",
            constraints["time_semantics"],
        )
        self.assertEqual(1, case["evidence_requirements"]["minimum_successful_queries"])
        self.assertEqual(
            {
                "report_metrics_individually",
                "report_governed_target_status",
                "disclose_period_flow_and_current_snapshot_scopes",
            },
            set(case["required_conclusions"]),
        )
        self.assertEqual(
            {
                "claim_scope_incompatible_overall_ranking",
                "claim_unsupported_mechanism_causality",
                "infer_equal_risk_from_absolute_overdue_similarity",
                "infer_ungoverned_overall_health_or_strength",
            },
            set(case["forbidden_conclusions"]),
        )

        suite = {
            "schema": scorer.SCHEMA,
            "suite": "department-scorecard-generation-release",
            "plan_constraint_semantics": "required-and-forbidden-subsets/v1",
            "minimum_case_count": 1,
            "required_category_minimums": {"ambiguity": 1},
            "cases": [case],
        }
        self.assertEqual([], scorer.validate_suite(suite))

        def candidate(final_answer_sha256, conclusions):
            profile = {"artifact_id": "a" * 64, "payload_sha256": "b" * 64}
            database_identity = "c" * 64
            canonical_prompt = "d" * 64
            session_id = "department-scorecard-release-session"
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
                "assertion_sha256": scorer._sha256(
                    {
                        "final_answer_sha256": final_answer_sha256,
                        "conclusions": conclusions,
                    }
                ),
                "binding_sha256": scorer._sha256(binding),
            }
            adaptive_plan = {
                key: copy.deepcopy(value)
                for key, value in case["plan_constraints"].items()
                if not key.startswith("must_not_")
            }
            adaptive_plan["domains"] = ["delivery", "target", "receivable"]
            adaptive_plan["metrics"] = [
                "delivery_amount",
                "delivery_target_completion",
                "overdue_receivable_amount",
            ]
            adaptive_plan["operations"] = [
                "performance_scorecard_first",
                "parallel_evidence",
            ]
            observed = {
                "id": case["id"],
                "session_id": session_id,
                "plan": adaptive_plan,
                "conclusions": list(conclusions),
                "conclusion_review": review,
                "evidence": {
                    "receipts": ["catalog", "query", "coverage"],
                    "successful_queries": 5,
                    "failed_queries": 0,
                    "truncated": False,
                    "reconciled": False,
                    "query_attempted": True,
                    "error_codes": [],
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

        reviewed_bad_raw_draft_sha256 = (
            "59729b742e6e360f92e5c6d7b5c9c26f7489db27aa687437c8cc17be85030eb4"
        )
        self.assertEqual(64, len(reviewed_bad_raw_draft_sha256))
        bad_conclusions = [
            *case["required_conclusions"],
            *case["forbidden_conclusions"],
        ]
        known_bad = candidate(reviewed_bad_raw_draft_sha256, bad_conclusions)
        self.assertEqual(
            reviewed_bad_raw_draft_sha256,
            known_bad["canary_receipt"]["turns"][0]["final_answer_sha256"],
        )
        failed_report = scorer.score(suite, known_bad)
        self.assertEqual(1, failed_report["summary"]["failed"])
        errors = failed_report["results"][0]["errors"]
        for label in case["forbidden_conclusions"]:
            self.assertTrue(
                any(label in error for error in errors),
                f"forbidden conclusion was not reported: {label}",
            )

        clean_sha256 = scorer._sha256(
            {
                "control": "clean-required-labels",
                "test_id": case["id"],
            }
        )
        clean = candidate(clean_sha256, case["required_conclusions"])
        clean_report = scorer.score(suite, clean)
        self.assertEqual(1, clean_report["summary"]["passed"], clean_report)
        self.assertEqual([], clean_report["results"][0]["errors"])

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
        wire = _load_module("wire")
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], scorer.validate_suite(suite))
        self.assertGreaterEqual(
            suite["required_category_minimums"]["change_diagnosis"], 4
        )
        self.assertGreaterEqual(
            suite["required_category_minimums"]["capability_boundary"], 3
        )
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
                "plan": dict(case["plan_constraints"]),
                "conclusions": list(case["required_conclusions"]),
                "evidence": evidence,
            }

        for case in cases.values():
            with self.subTest(case=case["id"]):
                self.assertEqual("capability_boundary", case["category"])
                self.assertEqual([], case["evidence_requirements"]["required_error_codes"])
                self.assertTrue(case["evidence_requirements"]["must_not_query"])
                domain = case["plan_constraints"]["domains"][0]
                metric = case["plan_constraints"]["metrics"][0]
                calls = catalog_calls(domain, metric)
                detail_request = calls[1]["arguments"]["requests"][0]
                detail_payload = calls[1]["result"]
                _plan, evidence = adapter._normalize(calls)
                self.assertEqual(["catalog", "metric_detail"], evidence["receipts"])
                self.assertEqual([], scorer._score_case(case, observed(case, evidence)))

                compact_detail_payload = json.loads(
                    wire.enforce_tool_result_budget(
                        "datasage_catalog", json.dumps(detail_payload)
                    )
                )
                compact_calls = [
                    calls[0],
                    catalog_call(detail_request, compact_detail_payload),
                ]
                _plan, compact_evidence = adapter._normalize(compact_calls)
                self.assertIn("metric_detail", compact_evidence["receipts"])
                compact_without_receipt = copy.deepcopy(compact_detail_payload)
                compact_without_receipt["results"][0].pop("detail_receipt")
                _plan, compact_invalid_evidence = adapter._normalize(
                    [
                        calls[0],
                        catalog_call(detail_request, compact_without_receipt),
                    ]
                )
                self.assertNotIn(
                    "metric_detail", compact_invalid_evidence["receipts"]
                )

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
                                    "dimension": case["plan_constraints"]["dimensions"][0]
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

        compact_query_call = {
            "name": "datasage_query",
            "arguments": ordinary_calls[-1]["arguments"],
            "result": {
                "status": "success",
                "model_wire_version": "datasage-query-model-wire/v2",
                "results": [
                    {
                        "request_id": "ordinary_delivery",
                        "status": "success",
                        "truncated": False,
                        "rows": [],
                    }
                ],
                "evidence_bundle": {
                    "items": [
                        {
                            "request_id": "ordinary_delivery",
                            "status": "success",
                        }
                    ]
                },
            },
        }
        _plan, compact_query_evidence = adapter._normalize([compact_query_call])
        self.assertEqual(1, compact_query_evidence["successful_queries"])
        self.assertIn("coverage", compact_query_evidence["receipts"])
        self.assertNotIn(
            "TRANSCRIPT_REQUEST_RESULT_MISMATCH",
            compact_query_evidence["error_codes"],
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

        self.assertIn("plugins/datasage-query", owned)
        self.assertIn("tests", owned)
        self.assertFalse(
            any(item.startswith("plugins/datasage-query/") for item in owned)
        )
        self.assertFalse(any(item.startswith("tests/") for item in owned))
        self.assertTrue(private_replay_assets.isdisjoint(owned))
        for relative in retained_evaluation_assets:
            self.assertTrue((PROFILE_ROOT / relative).is_file(), relative)
        for relative in private_replay_assets:
            self.assertFalse((PROFILE_ROOT / relative).exists(), relative)
        for relative in owned:
            path_parts = set(relative.split("/"))
            self.assertTrue({"dsrt", ".release"}.isdisjoint(path_parts), relative)

    def test_current_distribution_uses_official_bundled_skill_opt_out(self):
        distribution = (PROFILE_ROOT / "distribution.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("- .no-bundled-skills", distribution)
        self.assertNotIn("- .release", distribution)
        marker = PROFILE_ROOT / ".no-bundled-skills"
        self.assertTrue(marker.is_file())
        self.assertIn("hermes skills opt-out", marker.read_text(encoding="utf-8"))

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
        self.assertIn("datasage-query", resolved)
        self.assertTrue(
            {"terminal", "file", "web", "memory", "skills"}.issubset(
                resolved
            )
        )
        self.assertIn("skills:\n", config)
        self.assertIn("write_approval: true", config)
        self.assertEqual(
            {"write_approval": True},
            parsed_config["memory"],
        )
        approvals = parsed_config.get("approvals")
        self.assertIsInstance(approvals, dict)
        self.assertEqual(
            {"destructive_slash_confirm"},
            set(approvals),
        )
        self.assertIs(approvals["destructive_slash_confirm"], False)
        for host_global_block in (
            "terminal:",
            "sessions:",
            "streaming:",
            "onboarding:",
            "compression:",
        ):
            self.assertNotIn(f"\n{host_global_block}", config)


if __name__ == "__main__":
    unittest.main()
