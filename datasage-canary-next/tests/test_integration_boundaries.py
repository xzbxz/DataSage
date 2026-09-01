"""Precise regression tests for the Hermes/DataSage integration boundary."""

from __future__ import annotations

import builtins
from contextlib import ExitStack
import copy
from datetime import date, datetime
import hashlib
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
from agent.background_review import (
    is_background_review_enabled,
    load_background_review_settings,
)
from hermes_cli.plugins import PluginManager
from hermes_cli.tools_config import _get_platform_tools
from tools import clarify_tool as _hermes_clarify_registration  # noqa: F401
from tools import skill_provenance as hermes_skill_provenance
from tools import tool_search as hermes_tool_search
from tools import write_approval as hermes_write_approval
from tools.registry import ToolRegistry, registry as hermes_registry

from plugin_registration_probe import RegistrationProbe, probe_registration


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


def _load_e2e_module(name: str):
    qualified_name = f"_datasage_{name}_integration_tests"
    existing = sys.modules.get(qualified_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        qualified_name,
        PLUGIN_ROOT / "e2e" / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load e2e test module: {name}")
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
    def test_register_binds_official_plugin_relative_config_reader(self):
        plugin, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_integration_config_registration",
            config={
                "max_rows": 17,
                "mysql_allowed_grant_scopes": [" analytics.* "],
            },
        )

        self.assertEqual(17, plugin.settings.get_int("max_rows", 5, 1, 100))
        self.assertEqual(
            ["analytics.*"],
            plugin.settings.get_list("mysql_allowed_grant_scopes"),
        )
        self.assertEqual(
            ["max_rows", "mysql_allowed_grant_scopes"],
            registration.config_reads,
        )

    def test_unbound_config_defaults_and_security_remains_fail_closed(self):
        previous_reader = settings._CONFIG_READER
        settings.bind_config_reader(None)
        try:
            self.assertEqual("safe", settings.get("unknown", "safe"))
            self.assertEqual(5, settings.get_int("max_rows", 5, 1, 10))
            self.assertEqual([], settings.get_list("mysql_allowed_grant_scopes"))
            with self.assertRaises(db_security.DatabaseSecurityError) as captured:
                db_security.mysql_tls_policy()
            self.assertEqual(
                "DATABASE_SECURITY_SETTING_MISSING",
                captured.exception.code,
            )
            self.assertFalse(entitlements.authorized("datasage_catalog", {}))
        finally:
            settings.bind_config_reader(previous_reader)

    def test_runtime_path_gate_has_no_private_runtime_prerequisite(self):
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
        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_integration_startup_registration",
        )
        self.assertEqual([], registration.hooks)
        self.assertEqual(
            {
                schemas.DATASAGE_CATALOG["name"],
                schemas.DATASAGE_ENTITY_RESOLVE["name"],
                schemas.DATASAGE_QUERY["name"],
            },
            {entry["name"] for entry in registration.tools},
        )


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

    def test_official_plugin_entry_denies_before_business_validation(
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
        original_get = loaded.module.entitlements.settings.get
        with (
            mock.patch.dict(sys.modules, {"gateway": gateway}),
            mock.patch.object(
                loaded.module.entitlements.settings,
                "get",
                side_effect=lambda key, default=None: (
                    policy["data_entitlements"]
                    if key == "data_entitlements"
                    else original_get(key, default)
                ),
            ),
            mock.patch.object(
                loaded.module.tools,
                "_contracts",
                wraps=loaded.module.tools._contracts,
            ) as business_validation,
            mock.patch.object(
                loaded.module.tools,
                "runtime_guarded_datasage_query",
                side_effect=AssertionError("runtime readiness reached"),
            ) as runtime_sentinel,
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
        business_validation.assert_not_called()
        runtime_sentinel.assert_not_called()
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
        registration_probe = RegistrationProbe()
        loaded.module.register(registration_probe)
        catalog_handler = next(
            tool["handler"]
            for tool in registration_probe.tools
            if tool["name"] == "datasage_catalog"
        )
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
        original_get = loaded.module.entitlements.settings.get

        def invoke(rule):
            with (
                mock.patch.dict(
                    os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}
                ),
                mock.patch.object(
                    loaded.module.entitlements.settings,
                    "get",
                    side_effect=lambda key, default=None: (
                        {}
                        if key == "data_entitlements"
                        else original_get(key, default)
                    ),
                ),
                mock.patch.object(
                    loaded.module.entitlements,
                    "_principal_rule",
                    return_value=rule,
                ),
            ):
                return json.loads(
                    catalog_handler(
                        {"requests": [{"view": "performance_scorecard"}]}
                    )
                )

        allowed = invoke(full_rule)
        self.assertIn("status", allowed, allowed)
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
        manifest = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_integration_skill_registration",
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
        self.assertEqual(
            set(manifest["provides_tools"]),
            {entry["name"] for entry in registration.tools},
        )
        self.assertEqual([], registration.hooks)
        self.assertEqual([], registration.prompt_sections)
        self.assertIn("requires_toolsets: [datasage-query]", main_skill)

    def test_registered_tool_schemas_use_deepseek_documented_subset(self):
        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_deepseek_schema_registration",
        )
        forbidden = set(schemas._MODEL_SCHEMA_OMIT_KEYS) | {"oneOf"}

        def keys(value):
            if isinstance(value, dict):
                found = set(value)
                for child in value.values():
                    found.update(keys(child))
                return found
            if isinstance(value, list):
                found = set()
                for child in value:
                    found.update(keys(child))
                return found
            return set()

        for entry in registration.tools:
            with self.subTest(tool=entry["name"]):
                self.assertFalse(forbidden & keys(entry["schema"]))
        self.assertIn(
            "dependencies",
            schemas.DATASAGE_ENTITY_RESOLVE["parameters"],
            "model projection must not mutate the canonical runtime contract",
        )

    def test_soul_denial_rule_requires_trusted_signal_and_preserves_mixed_turn(self):
        normalized = " ".join(
            (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8").split()
        )
        self.assertIn("The DataSage plugin owns metric definitions", normalized)
        self.assertIn("permissions", normalized)
        answer_boundary = (
            SKILL_PATH.parent / "references" / "answer-boundary.md"
        ).read_text(encoding="utf-8")
        self.assertIn("If one branch fails", answer_boundary)
        self.assertNotIn("DATA_ENTITLEMENT_DENIED", normalized)

    def test_official_plugin_manager_keeps_skill_guidance_on_demand(self):
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
            self.assertEqual({}, manager._system_prompt_sections)
            rendered = manager.render_system_prompt_sections(
                {
                    "session_id": "alpha5-prompt-section-test",
                    "platform": "wecom",
                    "profile_name": "datasage-expert-next",
                }
            )
            self.assertEqual([], rendered)

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
            # SOUL supplies the stable role boundary; the full expert workflow
            # remains an ordinary Hermes on-demand Skill.

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
                "get",
                side_effect=dict(self.CANARY_POLICY).get,
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
                    "get",
                    side_effect=dict(self.CANARY_POLICY).get,
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
                "get",
                side_effect=production.get,
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
                "get",
                side_effect=invalid_production.get,
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
            "get",
            side_effect=configured.get,
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
                    "get",
                    side_effect=configured.get,
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
                        "get",
                        side_effect=configured.get,
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

    def test_live_cache_key_preserves_invalid_security_value_types(self):
        with mock.patch.object(
            runtime_health.settings,
            "get",
            side_effect=dict(self.CANARY_POLICY).get,
        ):
            valid_key = runtime_health._live_cache_key()
        with mock.patch.object(
            runtime_health.settings,
            "get",
            side_effect={
                **self.CANARY_POLICY,
                "production_mode": "false",
            }.get,
        ):
            invalid_key = runtime_health._live_cache_key()
        self.assertNotEqual(valid_key, invalid_key)


class DistributionBoundaryTests(unittest.TestCase):
    @staticmethod
    def _catalog_call(request, result):
        return {
            "name": "datasage_catalog",
            "arguments": {"requests": [request]},
            "result": {"status": "success", "results": [result]},
        }

    @staticmethod
    def _metric_detail_call(adapter, domain, metric, time_policy):
        payload = {
            "status": "success",
            "catalog_version": "datasage-metric-catalog/v1",
            "contract_role": "governed_metric_catalog",
            "query_policy": {},
            "results": [
                {
                    "level": "metric",
                    "domain": domain,
                    "metric": {"code": metric, "time_policy": time_policy},
                }
            ],
        }
        payload["content_hash"] = adapter._sha256(payload)
        return {
            "name": "datasage_catalog",
            "arguments": {"requests": [{"domain": domain, "metric": metric}]},
            "result": payload,
        }

    @staticmethod
    def _query_call(requests, results, covered_ids):
        requests_by_id = {
            request.get("request_id"): request
            for request in requests
            if isinstance(request, dict)
        }
        normalized_results = []
        for result in results:
            normalized = dict(result)
            if normalized.get("status") == "success":
                request = requests_by_id.get(normalized.get("request_id"), {})
                metric_ref = "metric_" + hashlib.sha256(
                    f"{request.get('domain')}\0{request.get('metric')}".encode("utf-8")
                ).hexdigest()[:16]
                normalized.setdefault("data_state", "rows")
                normalized.setdefault("business_metric_ref", metric_ref)
                normalized.setdefault("business_metric_label", "Delivery amount")
                normalized.setdefault("row_count", 1)
                normalized.setdefault(
                    "rows",
                    [
                        {
                            "claim_id": "claim_" + "a" * 20,
                            "dimensions": [],
                            "facts": {"metric_value": "1"},
                            "states": {"metric_data_state": "complete"},
                            "allowed_relations": ["observation"],
                            "unit": "CNY",
                            "currency": "CNY",
                        }
                    ],
                )
                normalized.setdefault("truncated", False)
                normalized.setdefault(
                    "applied_time_range",
                    (
                        {
                            **request["time_range"],
                            "source": "explicit",
                            "calendar_evidence": {
                                "version": "calendar-period-evidence/v2",
                                "observation_basis": "business_clock_query_observation",
                                "observed_on": "2026-08-30",
                                "period_state": "in_progress",
                                "source_freshness": "not_proven",
                            },
                        }
                        if isinstance(request.get("time_range"), dict)
                        else {
                            "source": "current_snapshot",
                            "as_of_date": "2026-08-30",
                            "resolution_state": "resolved",
                        }
                    ),
                )
                normalized.setdefault("error", None)
            normalized_results.append(normalized)
        return {
            "name": "datasage_query",
            "arguments": {"requests": requests},
            "result": {
                "model_wire_version": "datasage-query-model-wire/v3",
                "evidence_bundle": {
                    "items": [
                        {"status": "success", "request_id": request_id}
                        for request_id in covered_ids
                    ]
                },
                "results": normalized_results,
            },
        }

    @staticmethod
    def _official_export_fixture(adapter):
        session_id = "synthetic-official-export"
        profile = {
            "profile_id": "datasage-canary-next",
            "artifact_id": "a" * 64,
            "payload_sha256": "b" * 64,
        }
        exported = {
            "id": session_id,
            "source": "wecom",
            "profile_name": profile["profile_id"],
            "messages": [
                {
                    "id": 1,
                    "session_id": session_id,
                    "active": 1,
                    "role": "user",
                    "content": "synthetic prompt",
                    "tool_call_id": None,
                    "tool_calls": None,
                    "tool_name": None,
                    "platform_message_id": "user-platform",
                },
                {
                    "id": 2,
                    "session_id": session_id,
                    "active": 1,
                    "role": "assistant",
                    "content": "synthetic final",
                    "tool_call_id": None,
                    "tool_calls": None,
                    "tool_name": None,
                    "platform_message_id": "assistant-platform",
                },
            ],
        }
        payload = (json.dumps(exported, ensure_ascii=False) + "\n").encode("utf-8")
        export_sha = hashlib.sha256(payload).hexdigest()
        prompt_sha = adapter._sha256("synthetic prompt")
        turn = {
            "test_id": "synthetic_case",
            "conversation_id": "synthetic_conversation",
            "turn": 1,
            "session_id": session_id,
            "user_message_id": 1,
            "final_message_id": 2,
            "canonical_prompt_sha256": prompt_sha,
            "watermark_sha256": adapter._watermark(
                test_id="synthetic_case",
                conversation_id="synthetic_conversation",
                turn=1,
                canonical_prompt_sha256=prompt_sha,
                user_message_id=1,
                session_export_sha256=export_sha,
                profile=profile,
            ),
        }
        bindings = {
            "schema": adapter.BINDING_SCHEMA,
            "captured_at": "2026-08-30T00:00:00+00:00",
            "transcript_source": "wecom",
            "profile_artifact": profile,
            "session_export_sha256": export_sha,
            "turns": [turn],
        }
        return exported, payload, bindings

    @staticmethod
    def _rebind_official_export(adapter, exported, bindings):
        payload = (json.dumps(exported, ensure_ascii=False) + "\n").encode("utf-8")
        rebound = copy.deepcopy(bindings)
        export_sha = hashlib.sha256(payload).hexdigest()
        rebound["session_export_sha256"] = export_sha
        turn = rebound["turns"][0]
        turn["watermark_sha256"] = adapter._watermark(
            test_id=turn["test_id"],
            conversation_id=turn["conversation_id"],
            turn=turn["turn"],
            canonical_prompt_sha256=turn["canonical_prompt_sha256"],
            user_message_id=turn["user_message_id"],
            session_export_sha256=export_sha,
            profile=rebound["profile_artifact"],
        )
        return payload, rebound

    def test_official_export_adapter_is_stable_and_hash_bound(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        _exported, payload, bindings = self._official_export_fixture(adapter)
        first = adapter.adapt(payload, bindings)
        second = adapter.adapt(payload, copy.deepcopy(bindings))
        self.assertEqual(first, second)
        self.assertTrue(adapter.verify_receipt(first))
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            first["session_export_sha256"],
        )
        forged = copy.deepcopy(bindings)
        forged["session_export_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match the official export"):
            adapter.adapt(payload, forged)
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            adapter.parse_official_session_export('{"id":"a","id":"b","messages":[]}')

    def test_official_export_adapter_fails_closed_on_session_and_message_tampering(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        exported, _payload, bindings = self._official_export_fixture(adapter)

        mutations = []

        def mutation(label, callback, pattern):
            value = copy.deepcopy(exported)
            changed_bindings = copy.deepcopy(bindings)
            callback(value, changed_bindings)
            payload, rebound = self._rebind_official_export(
                adapter, value, changed_bindings
            )
            mutations.append((label, payload, rebound, pattern))

        mutation("session", lambda value, _binding: value.__setitem__("id", "other"), "session does not match")
        mutation("source", lambda value, _binding: value.__setitem__("source", "cli"), "session source")
        mutation("profile", lambda value, _binding: value.__setitem__("profile_name", "other"), "Profile does not match")
        mutation("order", lambda value, _binding: value["messages"].reverse(), "strictly increasing")
        mutation("duplicate", lambda value, _binding: value["messages"][1].__setitem__("id", 1), "duplicate message IDs")
        for label, active in (
            ("active-missing", ...),
            ("active-none", None),
            ("active-false", False),
            ("active-true", True),
            ("active-string", "0"),
            ("active-zero", 0),
            ("active-two", 2),
        ):
            def change_active(value, _binding, active=active):
                if active is ...:
                    value["messages"][1].pop("active")
                else:
                    value["messages"][1]["active"] = active

            mutation(label, change_active, "active flag must be integer 1")
        mutation("cross-session", lambda value, _binding: value["messages"][1].__setitem__("session_id", "other"), "cross-session")
        mutation("endpoint", lambda _value, binding: binding["turns"][0].__setitem__("final_message_id", 99), "endpoints do not exist")

        def unclosed_tool(value, binding):
            value["messages"][1] = {
                **value["messages"][1],
                "content": None,
                "tool_calls": [{
                    "id": "unclosed",
                    "function": {"name": "datasage_catalog", "arguments": "{}"},
                }],
            }
            value["messages"].append({
                "id": 3,
                "session_id": value["id"],
                "active": 1,
                "role": "assistant",
                "content": "synthetic final",
                "tool_call_id": None,
                "tool_calls": None,
                "tool_name": None,
                "platform_message_id": "assistant-platform-final",
            })
            binding["turns"][0]["final_message_id"] = 3

        mutation("tool-closure", unclosed_tool, "no persisted tool result")

        def result_before_call(value, binding):
            value["messages"][1:] = [
                {
                    **value["messages"][1],
                    "id": 2,
                    "role": "tool",
                    "content": "{}",
                    "tool_call_id": "catalog-before-call",
                    "tool_calls": None,
                    "tool_name": "datasage_catalog",
                },
                {
                    **value["messages"][1],
                    "id": 3,
                    "role": "assistant",
                    "content": None,
                    "tool_call_id": None,
                    "tool_calls": [{
                        "id": "catalog-before-call",
                        "function": {
                            "name": "datasage_catalog", "arguments": "{}",
                        },
                    }],
                    "tool_name": None,
                },
                {**value["messages"][1], "id": 4},
            ]
            binding["turns"][0]["final_message_id"] = 4

        mutation("result-before-call", result_before_call, "result precedes its declaration")
        for label, payload, rebound, pattern in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, pattern):
                adapter.adapt(payload, rebound)

    def test_time_requires_successful_result_and_matching_coverage(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        request = {
            "request_id": "r1",
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        failed_plan, _ = adapter._normalize(
            [self._query_call([request], [{"request_id": "r1", "status": "error"}], [])]
        )
        missing_coverage_plan, missing_coverage_evidence = adapter._normalize(
            [self._query_call([request], [{"request_id": "r1", "status": "success"}], [])]
        )
        successful_plan, successful_evidence = adapter._normalize(
            [self._query_call([request], [{"request_id": "r1", "status": "success"}], ["r1"])]
        )
        self.assertEqual("no_query", failed_plan["time_semantics"])
        self.assertEqual("no_query", missing_coverage_plan["time_semantics"])
        self.assertIn("TRANSCRIPT_REQUEST_RESULT_MISMATCH", missing_coverage_evidence["error_codes"])
        self.assertEqual("range:2026-01-01/2026-09-01", successful_plan["time_semantics"])
        self.assertEqual(["query", "coverage"], successful_evidence["receipts"])

    def test_bound_catalog_and_covered_queries_derive_ytd_flow_snapshot_semantics(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        requests = [
            {
                "request_id": "flow",
                "domain": "delivery",
                "metric": "delivery_amount",
                "dimensions": ["department"],
                "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
            },
            {
                "request_id": "snapshot",
                "domain": "receivable",
                "metric": "overdue_receivable_amount",
                "dimensions": ["department"],
            },
        ]
        calls = [
            self._metric_detail_call(adapter, "delivery", "delivery_amount", "current_month"),
            self._metric_detail_call(adapter, "receivable", "overdue_receivable_amount", "current_snapshot"),
            self._query_call(
                requests,
                [
                    {"request_id": "flow", "status": "success"},
                    {"request_id": "snapshot", "status": "success"},
                ],
                ["flow", "snapshot"],
            ),
        ]
        plan, evidence = adapter._normalize(calls)
        self.assertEqual("2026_ytd_flows_with_separate_snapshots", plan["time_semantics"])
        self.assertIn("metric_detail", evidence["receipts"])
        self.assertIn("coverage", evidence["receipts"])
        failed_snapshot_calls = calls[:2] + [
            self._query_call(
                requests,
                [
                    {"request_id": "flow", "status": "success"},
                    {"request_id": "snapshot", "status": "error"},
                ],
                ["flow"],
            )
        ]
        failed_snapshot_plan, _ = adapter._normalize(failed_snapshot_calls)
        self.assertNotEqual(
            "2026_ytd_flows_with_separate_snapshots",
            failed_snapshot_plan["time_semantics"],
        )

    def test_only_structured_unconfirmed_ambiguity_derives_clarification(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        arguments = {
            "token": "Thailand",
            "domain": "delivery",
            "metric": "delivery_amount",
        }
        ambiguous = {
            "status": "ambiguous",
            "resolution_path": "master_candidate_query",
            "token": "Thailand",
            "must_clarify": True,
            "must_stop_business_query": True,
            "candidate_count": 2,
            "candidate_count_is_lower_bound": False,
            "truncated": False,
            "candidates": [
                {
                    "entity_type": "customer",
                    "canonical_id": "c1",
                    "canonical_code": "TH-CUSTOMER",
                    "display_name": "Thailand Trading",
                    "filter_values": ["c1"],
                    "match_kind": "contains",
                    "confidence": "candidate",
                    "filter_role": "customer",
                },
                {
                    "entity_type": "supplier",
                    "canonical_id": "s1",
                    "canonical_code": "TH-SUPPLIER",
                    "display_name": "Thailand Supply",
                    "filter_values": ["s1"],
                    "match_kind": "contains",
                    "confidence": "candidate",
                    "filter_role": "supplier",
                },
            ],
        }
        plan, evidence = adapter._normalize(
            [{"name": "datasage_entity_resolve", "arguments": arguments, "result": ambiguous}]
        )
        self.assertIn("clarify_entity_mapping", plan["operations"])
        self.assertIn("ENTITY_AMBIGUOUS", evidence["error_codes"])
        self.assertTrue(adapter._is_unconfirmed_entity_ambiguity(plan, evidence))

        continued_plan = copy.deepcopy(plan)
        continued_evidence = copy.deepcopy(evidence)
        continued_evidence["query_attempted"] = True
        self.assertFalse(
            adapter._is_unconfirmed_entity_ambiguity(continued_plan, continued_evidence)
        )
        guarded_query = {
            "name": "datasage_query",
            "arguments": {
                "requests": [
                    {
                        "request_id": "thailand_guarded",
                        "domain": "delivery",
                        "metric": "delivery_amount",
                        "metric_filters": {"customer": ["c1"]},
                    }
                ]
            },
            "result": {
                "status": "success",
                "request_count": 1,
                "answer_scope_line": "No rows for the exact governed scope.",
                "metric_contexts": [],
                "results": [
                    {
                        "request_id": "thailand_guarded",
                        "status": "success",
                        "data_state": "empty",
                        "business_metric_ref": "metric_delivery_amount",
                        "business_metric_label": "Delivery amount",
                        "row_count": 0,
                        "truncated": False,
                        "requested_limit": 100,
                        "effective_limit": 100,
                        "has_more": False,
                        "error": None,
                    }
                ],
            },
        }
        queried_plan, queried_evidence = adapter._normalize(
            [
                {
                    "name": "datasage_entity_resolve",
                    "arguments": arguments,
                    "result": ambiguous,
                },
                guarded_query,
            ]
        )
        self.assertTrue(queried_evidence["query_attempted"])
        self.assertFalse(
            adapter._is_unconfirmed_entity_ambiguity(
                queried_plan, queried_evidence
            )
        )

        scorer = _load_e2e_module("golden_expert_scorer")
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        case = next(
            item
            for item in suite["cases"]
            if item["id"] == "multiturn_08_rc5_thailand_followup"
        )
        errors = scorer._score_case(
            case,
            {
                "plan": queried_plan,
                "conclusions": list(case["required_conclusions"]),
                "evidence": queried_evidence,
            },
        )
        self.assertIn(
            "a query was attempted although the case must fail before data access",
            errors,
        )
        noncanonical = dict(ambiguous, must_stop_business_query=False)
        noncanonical_plan, noncanonical_evidence = adapter._normalize(
            [{"name": "datasage_entity_resolve", "arguments": arguments, "result": noncanonical}]
        )
        self.assertNotIn("clarify_entity_mapping", noncanonical_plan["operations"])
        self.assertNotIn("ENTITY_AMBIGUOUS", noncanonical_evidence["error_codes"])

        for invalid_arguments, invalid_payload in (
            ({}, ambiguous),
            (arguments, dict(ambiguous, token="different")),
            (arguments, dict(ambiguous, candidate_count=1)),
            (
                arguments,
                {
                    **ambiguous,
                    "candidates": [
                        {
                            key: value
                            for key, value in candidate.items()
                            if key not in {"canonical_id", "canonical_code"}
                        }
                        for candidate in ambiguous["candidates"]
                    ],
                },
            ),
        ):
            invalid_plan, invalid_evidence = adapter._normalize(
                [
                    {
                        "name": "datasage_entity_resolve",
                        "arguments": invalid_arguments,
                        "result": invalid_payload,
                    }
                ]
            )
            self.assertNotIn("clarify_entity_mapping", invalid_plan["operations"])
            self.assertNotIn("ENTITY_AMBIGUOUS", invalid_evidence["error_codes"])

    def test_live_success_requires_substantive_business_evidence(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        request = {
            "request_id": "flow",
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        call = self._query_call(
            [request], [{"request_id": "flow", "status": "success"}], ["flow"]
        )
        call["result"]["source_evidence_ref"] = {
            "schema": adapter.MODEL_SOURCE_REFERENCE_SCHEMA,
            "source_ref_sha256": "a" * 64,
        }
        empty_call = copy.deepcopy(call)
        empty_call["result"]["results"][0].update(row_count=0, rows=[])
        empty_plan, empty_evidence = adapter._normalize(
            [empty_call],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=date(2026, 8, 30),
        )
        self.assertEqual(0, empty_evidence["successful_queries"])
        self.assertEqual("no_query", empty_plan["time_semantics"])

        placeholder_call = copy.deepcopy(call)
        placeholder_call["result"]["results"][0].update(
            business_metric_ref="placeholder",
            applied_time_range={},
            rows=[{"x": "x"}],
        )
        placeholder_plan, placeholder_evidence = adapter._normalize(
            [placeholder_call],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=date(2026, 8, 30),
        )
        self.assertEqual(0, placeholder_evidence["successful_queries"])
        self.assertEqual("no_query", placeholder_plan["time_semantics"])

        valid_plan, valid_evidence = adapter._normalize(
            [call],
            expected_business_database_ref_sha256="a" * 64,
            expected_observed_on=date(2026, 8, 30),
        )
        self.assertEqual(1, valid_evidence["successful_queries"])
        self.assertEqual(
            "range:2026-01-01/2026-09-01", valid_plan["time_semantics"]
        )

    def test_live_ytd_composite_is_bound_to_capture_observation_month(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        requests = [
            {
                "request_id": "flow",
                "domain": "delivery",
                "metric": "delivery_amount",
                "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
            },
            {
                "request_id": "snapshot",
                "domain": "receivable",
                "metric": "overdue_receivable_amount",
            },
        ]
        calls = [
            self._metric_detail_call(adapter, "delivery", "delivery_amount", "current_month"),
            self._metric_detail_call(
                adapter,
                "receivable",
                "overdue_receivable_amount",
                "current_snapshot",
            ),
            self._query_call(
                requests,
                [
                    {"request_id": "flow", "status": "success"},
                    {"request_id": "snapshot", "status": "success"},
                ],
                ["flow", "snapshot"],
            ),
        ]
        current_plan, _ = adapter._normalize(
            calls, expected_observed_on=date(2026, 8, 30)
        )
        stale_plan, _ = adapter._normalize(
            calls, expected_observed_on=date(2026, 7, 31)
        )
        self.assertEqual(
            "2026_ytd_flows_with_separate_snapshots",
            current_plan["time_semantics"],
        )
        self.assertNotEqual(
            "2026_ytd_flows_with_separate_snapshots",
            stale_plan["time_semantics"],
        )

    def test_empty_calls_do_not_emit_semantic_receipts(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        _plan, evidence = adapter._normalize(
            [
                {
                    "name": "datasage_catalog",
                    "arguments": {"requests": []},
                    "result": {"status": "success", "results": []},
                },
                {
                    "name": "datasage_entity_resolve",
                    "arguments": {},
                    "result": {"status": "ambiguous", "candidates": []},
                },
                self._query_call([], [], []),
            ]
        )
        self.assertNotIn("catalog", evidence["receipts"])
        self.assertNotIn("entity_resolution", evidence["receipts"])
        self.assertNotIn("query", evidence["receipts"])

    def test_first_persisted_scorecard_call_derives_existing_operation(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        plan, evidence = adapter._normalize(
            [
                self._catalog_call(
                    {"view": "performance_scorecard"},
                    {"level": "performance_scorecard"},
                )
            ]
        )
        self.assertEqual(["performance_scorecard_first"], plan["operations"])
        self.assertEqual(["catalog"], evidence["receipts"])

    def test_later_scorecard_call_does_not_claim_first_operation(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        plan, _evidence = adapter._normalize(
            [
                self._catalog_call(
                    {"domain": "delivery", "view": "expert_index"},
                    {"level": "expert_index", "domain": "delivery"},
                ),
                self._catalog_call(
                    {"view": "performance_scorecard"},
                    {"level": "performance_scorecard"},
                ),
            ]
        )
        self.assertNotIn("performance_scorecard_first", plan["operations"])

    def test_review_trace_cannot_inject_or_delete_scorecard_first(self):
        adapter = _load_e2e_module("canary_transcript_adapter")
        direct_plan, _evidence = adapter._normalize(
            [
                self._catalog_call(
                    {"domain": "delivery", "view": "expert_index"},
                    {"level": "expert_index", "domain": "delivery"},
                )
            ]
        )
        injected_trace = copy.deepcopy(direct_plan)
        injected_trace["operations"] = ["performance_scorecard_first"]
        with self.assertRaisesRegex(
            ValueError, "contradicts persisted scorecard call"
        ):
            adapter._validate_performance_scorecard_trace(
                direct_plan, injected_trace
            )

        scorecard_plan, _evidence = adapter._normalize(
            [
                self._catalog_call(
                    {"view": "performance_scorecard"},
                    {"level": "performance_scorecard"},
                )
            ]
        )
        deleted_trace = copy.deepcopy(scorecard_plan)
        deleted_trace["operations"] = []
        with self.assertRaisesRegex(
            ValueError, "contradicts persisted scorecard call"
        ):
            adapter._validate_performance_scorecard_trace(
                scorecard_plan, deleted_trace
            )

    def test_broad_reviews_allow_direct_discovery_but_explicit_scorecard_does_not(self):
        scorer = _load_e2e_module("golden_expert_scorer")
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {case["id"]: case for case in suite["cases"]}

        def observed(case, operations):
            constraints = case["plan_constraints"]
            requirements = case["evidence_requirements"]
            return {
                "plan": {
                    "domains": ["delivery"],
                    "metrics": ["delivery_amount"],
                    "dimensions": list(constraints["dimensions"]),
                    "operations": list(operations),
                    "time_semantics": constraints["time_semantics"],
                    "context_action": constraints["context_action"],
                    "context_bindings": dict(
                        constraints.get("context_bindings", {})
                    ),
                },
                "conclusions": list(case["required_conclusions"]),
                "evidence": {
                    "receipts": list(requirements["required_receipts"]),
                    "successful_queries": requirements[
                        "minimum_successful_queries"
                    ],
                    "failed_queries": 0,
                    "truncated": False,
                    "reconciled": requirements[
                        "require_reconciled_decomposition"
                    ],
                    "query_attempted": not requirements["must_not_query"],
                    "error_codes": list(requirements["required_error_codes"]),
                },
            }

        optional_case_ids = (
            "ambiguity_06_department_performance_scorecard",
            "ambiguity_07_live_idk_scorecard",
            "ambiguity_08_live_vietnam_scorecard",
            "ambiguity_09_live_thai_kim_scorecard",
            "ambiguity_10_rc5_vietnam_scorecard",
        )
        for case_id in optional_case_ids:
            with self.subTest(case_id=case_id):
                case = cases[case_id]
                self.assertEqual([], case["plan_constraints"]["operations"])
                self.assertEqual([], scorer._score_case(case, observed(case, [])))

        explicit = cases["ambiguity_05_performance_scorecard"]
        direct_errors = scorer._score_case(explicit, observed(explicit, []))
        self.assertTrue(
            any("performance_scorecard_first" in error for error in direct_errors)
        )
        self.assertEqual(
            [],
            scorer._score_case(
                explicit, observed(explicit, ["performance_scorecard_first"])
            ),
        )

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
            session_export_sha = "c" * 64
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
                    "session_export_sha256": session_export_sha,
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
                "session_export_sha256": session_export_sha,
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
                "decision_quality": {
                    dimension: 1
                    for dimension in scorer.DECISION_QUALITY_DIMENSIONS
                },
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
                "session_export_sha256": session_export_sha,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_answer_sha256,
                "candidate_case_sha256": scorer._sha256(observed),
                "conclusion_review": review,
            }
            cases = [observed]
            receipt = {
                "schema": scorer.RECEIPT_SCHEMA,
                "profile_artifact": profile,
                "session_export_sha256": session_export_sha,
                "source": {
                    "platform": "wecom",
                    "format": "hermes_sessions_export_jsonl",
                    "session_export_sha256": session_export_sha,
                },
                "candidate_cases_sha256": scorer._sha256(cases),
                "turns": [turn],
            }
            receipt["receipt_sha256"] = scorer._sha256(receipt)
            return {
                "schema": scorer.CANDIDATE_SCHEMA,
                "profile_artifact": profile,
                "session_export_sha256": session_export_sha,
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

        nonhex = candidate(scorer._sha256("nonhex review assertion"))
        nonhex_review = nonhex["cases"][0]["conclusion_review"]
        nonhex_review["assertion_sha256"] = "g" * 64
        nonhex["canary_receipt"]["turns"][0]["conclusion_review"] = copy.deepcopy(
            nonhex_review
        )
        nonhex["canary_receipt"]["turns"][0]["candidate_case_sha256"] = (
            scorer._sha256(nonhex["cases"][0])
        )
        nonhex["canary_receipt"]["candidate_cases_sha256"] = scorer._sha256(
            nonhex["cases"]
        )
        receipt_body = {
            key: value
            for key, value in nonhex["canary_receipt"].items()
            if key != "receipt_sha256"
        }
        nonhex["canary_receipt"]["receipt_sha256"] = scorer._sha256(receipt_body)
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            scorer.score(suite, nonhex)

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
        self.assertEqual(51, full_suite["minimum_case_count"])
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
        self.assertEqual([], constraints["operations"])
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
            session_export_sha = "c" * 64
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
                    "session_export_sha256": session_export_sha,
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
                "session_export_sha256": session_export_sha,
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
            adaptive_plan["operations"] = ["parallel_evidence"]
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
                "session_export_sha256": session_export_sha,
                "watermark_sha256": watermark,
                "final_answer_sha256": final_answer_sha256,
                "candidate_case_sha256": scorer._sha256(observed),
                "conclusion_review": review,
            }
            cases = [observed]
            receipt = {
                "schema": scorer.RECEIPT_SCHEMA,
                "profile_artifact": profile,
                "session_export_sha256": session_export_sha,
                "source": {
                    "platform": "wecom",
                    "format": "hermes_sessions_export_jsonl",
                    "session_export_sha256": session_export_sha,
                },
                "candidate_cases_sha256": scorer._sha256(cases),
                "turns": [turn],
            }
            receipt["receipt_sha256"] = scorer._sha256(receipt)
            return {
                "schema": scorer.CANDIDATE_SCHEMA,
                "profile_artifact": profile,
                "session_export_sha256": session_export_sha,
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
        source_only_evaluation_assets = {
            "plugins/datasage-query/e2e/canary_transcript_adapter.py",
            "plugins/datasage-query/e2e/golden_expert_cases.json",
            "plugins/datasage-query/e2e/golden_expert_scorer.py",
        }
        private_replay_assets = {
            "plugins/datasage-query/e2e/hermes_replay_driver.py",
            "plugins/datasage-query/e2e/live_fixture_materializer.py",
            "plugins/datasage-query/e2e/trusted_replay_runner.py",
        }

        required_runtime_assets = {
            "plugins/datasage-query/plugin.yaml",
            "plugins/datasage-query/__init__.py",
            "plugins/datasage-query/db_executor.py",
            "plugins/datasage-query/receipt_cache.py",
            "plugins/datasage-query/tools.py",
            "plugins/datasage-query/vendor/pymysql/__init__.py",
            "plugins/datasage-query/vendor/pymysql-1.2.0.dist-info/METADATA",
        }
        source_only_assets = source_only_evaluation_assets | {
            "plugins/datasage-query/contracts/entity-rules-maintainer.md",
            "build_release_receipt.py",
            "tests",
            "ARCHITECTURE.md",
            "docs/history",
            "plugins/datasage-query/vendor/pymysql/__pycache__",
            "plugins/datasage-query/vendor/pymysql/constants/__pycache__",
        }

        def is_distributed(relative):
            return any(
                relative == entry or relative.startswith(entry.rstrip("/") + "/")
                for entry in owned
            )

        self.assertTrue(required_runtime_assets.issubset(owned))
        self.assertNotIn("plugins/datasage-query", owned)
        self.assertTrue(all(not is_distributed(item) for item in source_only_assets))
        self.assertFalse(any(item.startswith("tests/") for item in owned))
        self.assertTrue(private_replay_assets.isdisjoint(owned))
        for relative in source_only_evaluation_assets:
            self.assertTrue((PROFILE_ROOT / relative).is_file(), relative)
        for relative in private_replay_assets:
            self.assertFalse((PROFILE_ROOT / relative).exists(), relative)
        for relative in owned:
            path_parts = set(relative.split("/"))
            self.assertTrue({"dsrt", ".release"}.isdisjoint(path_parts), relative)

    def test_official_distribution_materializer_produces_runtime_only_payload(self):
        from hermes_cli.profile_distribution import _copy_dist_payload, read_manifest

        manifest = read_manifest(PROFILE_ROOT)
        self.assertIsNotNone(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "installed"
            _copy_dist_payload(
                PROFILE_ROOT,
                installed,
                manifest,
                preserve_config=False,
            )
            required = {
                "distribution.yaml",
                "SOUL.md",
                "config.yaml",
                "profile.yaml",
                "plugins/datasage-query/plugin.yaml",
                "plugins/datasage-query/db_executor.py",
                "plugins/datasage-query/receipt_cache.py",
                "plugins/datasage-query/tools.py",
                "plugins/datasage-query/vendor/pymysql/__init__.py",
                "skills/business-analytics/datasage/SKILL.md",
            }
            source_only = {
                "ARCHITECTURE.md",
                "build_release_receipt.py",
                "tests",
                "docs/history",
                "plugins/datasage-query/e2e",
                "plugins/datasage-query/contracts/entity-rules-maintainer.md",
            }
            self.assertEqual(
                set(),
                {relative for relative in required if not (installed / relative).exists()},
            )
            self.assertEqual(
                set(),
                {relative for relative in source_only if (installed / relative).exists()},
            )
            self.assertEqual([], list(installed.rglob("*.pyc")))

    def test_materialized_plugin_loads_registers_and_runs_database_free_preflight(self):
        import hermes_cli.plugins as plugins_module
        from hermes_cli.profile_distribution import _copy_dist_payload, read_manifest

        package_prefix = "hermes_plugins.datasage_query"
        prior_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == package_prefix or name.startswith(package_prefix + ".")
        }
        prior_bare_scope = dict(plugins_module._BARE_MODULE_SCOPE)
        for name in prior_modules:
            sys.modules.pop(name, None)

        try:
            with tempfile.TemporaryDirectory() as temporary:
                installed = Path(temporary) / "installed"
                manifest_data = read_manifest(PROFILE_ROOT)
                self.assertIsNotNone(manifest_data)
                _copy_dist_payload(
                    PROFILE_ROOT,
                    installed,
                    manifest_data,
                    preserve_config=False,
                )

                with mock.patch.dict(
                    os.environ,
                    {"HERMES_HOME": str(installed)},
                    clear=False,
                ):
                    plugin_root = installed / "plugins" / "datasage-query"
                    manager = PluginManager(scope_key=str(installed))
                    manifest = manager._parse_manifest(
                        plugin_root / "plugin.yaml",
                        plugin_root,
                        "user",
                        "",
                    )
                    self.assertIsNotNone(manifest)
                    module = manager._load_directory_module(manifest)
                    registration = RegistrationProbe()
                    module.register(registration)

                    self.assertEqual(
                        {
                            "datasage_catalog",
                            "datasage_entity_resolve",
                            "datasage_query",
                        },
                        {item["name"] for item in registration.tools},
                    )
                    for imported in (
                        module.tools.db_executor,
                        module.tools.receipt_cache,
                    ):
                        self.assertTrue(
                            Path(imported.__file__).resolve().is_relative_to(
                                installed.resolve()
                            )
                        )

                    receipt = module.tools._current_metric_detail_receipt(
                        "delivery",
                        "delivery_amount",
                    )
                    self.assertRegex(receipt, r"^[0-9a-f]{64}$")

                    response = json.loads(
                        module.tools.entitlement_guarded_datasage_query(
                            {
                                "requests": [
                                    {
                                        "request_id": "installed_preflight",
                                        "domain": "delivery",
                                        "metric": "delivery_amount",
                                        "dimensions": [],
                                    }
                                ]
                            }
                        )
                    )
                    self.assertEqual(
                        "DATA_ENTITLEMENT_DENIED",
                        response["error"]["code"],
                    )
        finally:
            for name in list(sys.modules):
                if name == package_prefix or name.startswith(package_prefix + "."):
                    sys.modules.pop(name, None)
            sys.modules.update(prior_modules)
            plugins_module._BARE_MODULE_SCOPE.clear()
            plugins_module._BARE_MODULE_SCOPE.update(prior_bare_scope)

    def test_distribution_allows_reviewed_bundled_skill_sync(self):
        distribution = (PROFILE_ROOT / "distribution.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("- .no-bundled-skills", distribution)
        self.assertNotIn("- .release", distribution)
        marker = PROFILE_ROOT / ".no-bundled-skills"
        self.assertFalse(marker.exists())

    def test_wecom_uses_minimal_skill_and_datasage_surface(self):
        config = (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        parsed_config = yaml.safe_load(config)
        wecom_toolsets = parsed_config["platform_toolsets"]["wecom"]
        self.assertEqual(
            ["skills", "clarify", "datasage-query"],
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
        self.assertIn("skills", resolved)
        self.assertIn("clarify", resolved)
        self.assertTrue(
            {"terminal", "file", "web", "memory"}.isdisjoint(resolved)
        )
        self.assertIn("skills:\n", config)
        self.assertIn("write_approval: true", config)
        self.assertEqual(
            {"write_approval": True},
            parsed_config["memory"],
        )
        self.assertEqual(
            {"background_review": {"enabled": False}},
            parsed_config["auxiliary"],
        )
        self.assertFalse(
            is_background_review_enabled(
                parsed_config["auxiliary"]["background_review"]
            )
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

    def test_automatic_review_is_off_and_manual_review_writes_still_stage(self):
        parsed_config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertIsInstance(parsed_config, dict)

        with mock.patch.dict(
            os.environ,
            {"HERMES_HOME": str(PROFILE_ROOT)},
        ), mock.patch(
            "hermes_cli.config.ensure_hermes_home",
            return_value=None,
        ):
            enabled, task_config = load_background_review_settings()
            self.assertFalse(enabled)
            self.assertIs(task_config.get("enabled"), False)

            origin_token = hermes_skill_provenance.set_current_write_origin(
                hermes_skill_provenance.BACKGROUND_REVIEW
            )
            try:
                for subsystem in (
                    hermes_write_approval.MEMORY,
                    hermes_write_approval.SKILLS,
                ):
                    with self.subTest(subsystem=subsystem):
                        self.assertTrue(
                            hermes_write_approval.write_approval_enabled(
                                subsystem
                            )
                        )
                        decision = hermes_write_approval.evaluate_gate(subsystem)
                        self.assertTrue(decision.stage)
                        self.assertFalse(decision.allow)
                        self.assertFalse(decision.blocked)
            finally:
                hermes_skill_provenance.reset_current_write_origin(origin_token)


class LiveReleaseEvidenceBoundaryTests(unittest.TestCase):
    @staticmethod
    def _runner_module():
        path = PROFILE_ROOT / "tests" / "run_live_release_evidence.py"
        spec = importlib.util.spec_from_file_location(
            "_datasage_live_release_evidence_test", path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load live release evidence runner")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_live_contract_is_fixed_to_official_wecom_inbound_and_two_by_three_plan(self):
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("datasage-live-release-contract/v6", contract["schema"])
        self.assertEqual(2, contract["case_plan"]["turns_per_session"])
        self.assertEqual(2, len(contract["case_plan"]["case_ids"]))
        self.assertEqual(3, contract["case_plan"]["runs"])
        self.assertEqual(
            contract["case_plan"]["case_ids"],
            [item["case_id"] for item in contract["turn_completion_policy"]],
        )
        self.assertEqual(
            [1, 2],
            [item["turn"] for item in contract["turn_completion_policy"]],
        )
        self.assertEqual(
            ["ordinary_text", "ordinary_text"],
            [item["assistant_completion"] for item in contract["turn_completion_policy"]],
        )
        self.assertEqual(
            [0, 0],
            [
                item["maximum_blocking_clarify_calls"]
                for item in contract["turn_completion_policy"]
            ],
        )
        shapes = contract["execution"]["command_shapes"]
        prefix = ["{python}", "-B", "-m", "hermes_cli.main"]
        self.assertEqual({"session_export", "adapter", "scorer"}, set(shapes))
        self.assertEqual(prefix + ["sessions", "export"], shapes["session_export"][:6])
        self.assertIn("--session-id", shapes["session_export"])
        self.assertEqual(["{python}", "-B", "{adapter}"], shapes["adapter"][:3])
        self.assertEqual(["{python}", "-B", "{scorer}"], shapes["scorer"][:3])
        rendered = json.dumps(shapes, sort_keys=True)
        for forbidden in ('"latest"', '"-c"', '"--continue"', '"-z"', '"--oneshot"'):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual("wecom", contract["inbound"]["platform"])
        self.assertEqual("dm", contract["inbound"]["chat_type"])
        self.assertEqual("sha256_only", contract["inbound"]["identity_storage"])
        self.assertEqual(
            "canonical_jsonl_raw_retained_endpoint_ignores_only_carrier_free_exact_session_meta_unknown_roles_fail_closed",
            contract["inbound"]["endpoint_projection_policy"],
        )
        self.assertEqual(
            "4d497bc168a1782eeaffb82b3cfa1f9ae212e86d9fa6dea721fe34712a3179e7",
            contract["inbound"]["expected_user_id_sha256"],
        )
        self.assertEqual(
            contract["inbound"]["expected_user_id_sha256"],
            contract["inbound"]["expected_chat_id_sha256"],
        )
        self.assertEqual("forbidden_in_finalize", contract["outbound"]["collection"])
        self.assertEqual("not_verified", contract["outbound"]["status"])
        self.assertEqual(2, contract["review_policy"]["reviews_per_run_case"])
        self.assertEqual(2, len(set(contract["review_policy"]["trusted_reviewer_id_sha256"])))
        self.assertEqual(
            "trusted_human_review_not_mechanically_proven",
            contract["review_policy"]["semantic_assurance"],
        )
        self.assertEqual(
            "sha256_sidecar_bound_to_both_trusted_reviews",
            contract["capture_integrity_policy"]["manifest_digest"],
        )
        self.assertEqual(
            "effective_path_pth_and_customization_content_sha256",
            contract["python_provenance_policy"]["site_packages"],
        )
        self.assertEqual(
            "hermes_cli_main_session_export_from_pinned_checkout",
            contract["python_provenance_policy"]["import_origins"],
        )
        self.assertFalse(contract["runtime_readiness_policy"]["configuration_values_recorded"])
        self.assertEqual(
            "external_gate_not_auto_passed",
            contract["runtime_readiness_policy"]["database_account"],
        )
        self.assertEqual(
            "external_gate_not_auto_passed",
            contract["runtime_readiness_policy"]["database_tls"],
        )

    def test_live_runner_contains_no_private_agent_or_replay_engine(self):
        forbidden_roots = {
            "run_agent",
            "planner",
            "trusted_replay_runner",
            "hermes_replay_driver",
        }
        imported: list[str] = []
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            if (
                name.split(".", 1)[0] in forbidden_roots
                or "AIAgent" in (fromlist or ())
            ):
                raise AssertionError(f"private runtime dependency imported: {name}")
            return real_import(name, globals, locals, fromlist, level)

        import_guard = mock.patch("builtins.__import__", side_effect=guarded_import)
        import_guard.start()
        self.addCleanup(import_guard.stop)
        runner = self._runner_module()
        self.assertTrue(imported)
        self.assertTrue(forbidden_roots.isdisjoint(name.split(".", 1)[0] for name in imported))
        for private_name in (*sorted(forbidden_roots), "AIAgent"):
            self.assertFalse(hasattr(runner, private_name))

        builder = runner._BUILDER
        self.assertIs(runner._export, builder._export_session)
        self.assertIs(
            runner._turn_completion_policies,
            builder._live_turn_completion_policies,
        )
        self.assertIs(runner._endpoints, builder._live_endpoints)
        adapter = runner._load_module("_datasage_live_adapter_behavior", runner.ADAPTER_PATH)
        scorer = runner._load_module("_datasage_live_scorer_behavior", runner.SCORER_PATH)
        self.assertTrue(callable(adapter.adapt))
        self.assertTrue(callable(scorer.score))

        contract = runner._read_json(runner.CONTRACT_PATH)
        builder._validate_live_contract(contract)
        shape = contract["execution"]["command_shapes"]["session_export"]
        bindings = {"python": "python", "exact_session_id": "session-1"}
        expanded = runner._expand(shape, bindings)
        self.assertEqual(len(shape), len(expanded))
        self.assertEqual("session-1", expanded[shape.index("{exact_session_id}")])
        with self.assertRaisesRegex(RuntimeError, "missing command binding"):
            runner._expand(shape, {"python": "python"})
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            runner.subprocess, "run"
        ) as subprocess_run:
            with self.assertRaisesRegex(RuntimeError, "argv length"):
                runner._run(
                    [*expanded, "unexpected"],
                    shape,
                    cwd=PROFILE_ROOT,
                    env={},
                    timeout=1,
                    stream_dir=Path(temporary),
                    stream_prefix="not-created",
                )
        subprocess_run.assert_not_called()

        with tempfile.TemporaryDirectory() as temporary:
            environment = runner._environment(Path(temporary))
        self.assertEqual("1", environment["PYTHONNOUSERSITE"])
        self.assertEqual("1", environment["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual(str(PROFILE_ROOT), environment["HERMES_HOME"])

        contract = runner._read_json(runner.CONTRACT_PATH)
        golden = runner._read_json(runner.GOLDEN_PATH)
        case_ids = contract["case_plan"]["case_ids"]
        cases_by_id = {case["id"]: case for case in golden["cases"]}
        cases = [cases_by_id[case_id] for case_id in case_ids]
        commit = "c" * 40
        hermes_commit = contract["host"]["hermes_git_commit"]
        commit_timestamp = datetime.fromisoformat("2026-08-30T00:00:00+00:00")
        session_ids = [f"synthetic-run-{index}" for index in range(1, 4)]
        reviewer_ids = ("synthetic-reviewer-one", "synthetic-reviewer-two")
        reviewer_hashes = dict(
            zip(reviewer_ids, contract["review_policy"]["trusted_reviewer_id_sha256"])
        )
        real_sha = runner._sha

        def guarded_sha(value):
            if isinstance(value, str) and value in reviewer_hashes:
                return reviewer_hashes[value]
            return real_sha(value)

        def official_export(session_id, run_index):
            messages = []
            for turn, case in enumerate(cases, 1):
                user_id = turn * 2 - 1
                messages.extend(
                    [
                        {
                            "id": user_id,
                            "session_id": session_id,
                            "active": 1,
                            "role": "user",
                            "content": case["prompt"],
                            "tool_call_id": None,
                            "tool_calls": None,
                            "tool_name": None,
                            "platform_message_id": f"user-{run_index}-{turn}",
                        },
                        {
                            "id": user_id + 1,
                            "session_id": session_id,
                            "active": 1,
                            "role": "assistant",
                            "content": f"synthetic final {turn}",
                            "tool_call_id": None,
                            "tool_calls": None,
                            "tool_name": None,
                            "platform_message_id": f"assistant-{run_index}-{turn}",
                        },
                    ]
                )
            exported = {
                "id": session_id,
                "source": "wecom",
                "profile_name": "datasage-canary-next",
                "user_id": "synthetic-user",
                "chat_id": "synthetic-chat",
                "chat_type": "dm",
                "title": f"datasage-live-{commit[:12]}-run-{run_index}",
                "started_at": commit_timestamp.isoformat(),
                "fresh_run_nonce": f"{run_index:032x}",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "model_fingerprint_sha256": "b" * 64,
                "system_prompt_sha256": "c" * 64,
                "profile_content_sha256": "d" * 64,
                "messages": messages,
            }
            return (json.dumps(exported, ensure_ascii=False) + "\n").encode("utf-8")

        exports = {
            session_id: official_export(session_id, run_index)
            for run_index, session_id in enumerate(session_ids, 1)
        }

        def synthetic_origin(
            session,
            *,
            contract,
            commit,
            run_index,
            commit_timestamp,
        ):
            return {
                "source": contract["inbound"]["platform"],
                "chat_type": contract["inbound"]["chat_type"],
                "user_id_sha256": contract["inbound"]["expected_user_id_sha256"],
                "chat_id_sha256": contract["inbound"]["expected_chat_id_sha256"],
                "title": f"datasage-live-{commit[:12]}-run-{run_index}",
                "started_at": commit_timestamp.isoformat(),
                "session_id": session["id"],
            }

        def synthetic_process(command, **_kwargs):
            if "sessions" in command:
                session_id = command[command.index("--session-id") + 1]
                stdout = exports[session_id]
            elif "--session-export" in command:
                Path(command[command.index("--output") + 1]).write_text(
                    '{"synthetic":"candidate"}\n', encoding="utf-8"
                )
                stdout = b"synthetic adapter complete\n"
            elif "--cases" in command:
                Path(command[command.index("--output") + 1]).write_text(
                    '{"synthetic":"score"}\n', encoding="utf-8"
                )
                stdout = b"synthetic scorer complete\n"
            else:
                raise AssertionError(f"unexpected external command: {command}")
            return runner.subprocess.CompletedProcess(command, 0, stdout, b"")

        def validated_reviews(review_set, **_kwargs):
            return {
                case_id: [
                    review
                    for review in review_set["reviews"]
                    if review["case_id"] == case_id
                ]
                for case_id in case_ids
            }

        builder = runner._BUILDER
        receipt = {
            "name": contract["subject"]["name"],
            "version": contract["subject"]["version"],
            "content_sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_dir = temporary_root / "evidence"
            hermes_root = temporary_root / "hermes"
            hermes_python = hermes_root / "venv" / "Scripts" / "python.exe"
            hermes_python.parent.mkdir(parents=True)
            hermes_python.touch()
            reviews_path = temporary_root / "reviews.json"
            try:
                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(runner, "EVIDENCE_DIR", evidence_dir)
                    )
                    runtime_probe = stack.enter_context(
                        mock.patch.object(
                            runner,
                            "_runtime",
                            return_value=(
                                hermes_root,
                                hermes_python,
                                commit,
                                hermes_commit,
                            ),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            runner,
                            "_python_provenance",
                            return_value={"schema": "synthetic-python-provenance"},
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            runner,
                            "_source",
                            side_effect=lambda _commit, path: {
                                "path": path,
                                "sha256": real_sha(path),
                            },
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            runner,
                            "_commit_timestamp",
                            return_value=commit_timestamp,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            runner, "_session_origin", side_effect=synthetic_origin
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(runner, "_sha", side_effect=guarded_sha)
                    )
                    process_probe = stack.enter_context(
                        mock.patch.object(
                            runner.subprocess,
                            "run",
                            side_effect=synthetic_process,
                        )
                    )
                    for patcher in (
                        mock.patch.object(builder, "_validate_live_contract"),
                        mock.patch.object(
                            builder, "build_receipt", return_value=receipt
                        ),
                        mock.patch.object(builder, "_live_capture_artifact"),
                        mock.patch.object(builder, "_live_artifact"),
                        mock.patch.object(builder, "_validate_process_record"),
                        mock.patch.object(
                            builder,
                            "_validate_python_provenance",
                            side_effect=lambda proof, *_args: proof,
                        ),
                        mock.patch.object(
                            builder,
                            "_validate_live_reviews",
                            side_effect=validated_reviews,
                        ),
                        mock.patch.object(
                            builder, "_reject_internal_conclusion_codes"
                        ),
                        mock.patch.object(builder, "_validate_review_evidence"),
                        mock.patch.object(
                            builder,
                            "_review_consensus_id",
                            return_value="synthetic-consensus",
                        ),
                    ):
                        stack.enter_context(patcher)

                    common = [
                        "--hermes-python",
                        str(hermes_python),
                        "--hermes-root",
                        str(hermes_root),
                    ]
                    capture_arguments = ["capture", *common]
                    for session_id in session_ids:
                        capture_arguments.extend(["--session-id", session_id])
                    self.assertEqual(0, runner.main(capture_arguments))

                    capture_path = evidence_dir / "private" / commit / "capture.json"
                    capture = runner._read_json(capture_path)
                    capture_sha = runner._artifact(capture_path)["sha256"]
                    reviews = []
                    for run in capture["runs"]:
                        run_index = run["run_index"]
                        session_id = session_ids[run_index - 1]
                        for case, final_sha in zip(
                            cases, run["session"]["final_answer_sha256"]
                        ):
                            for reviewer_id in reviewer_ids:
                                reviews.append(
                                    {
                                        "run_index": run_index,
                                        "case_id": case["id"],
                                        "session_id_sha256": real_sha(session_id),
                                        "final_answer_sha256": final_sha,
                                        "capture_sha256": capture_sha,
                                        "reviewer_id": reviewer_id,
                                        "labels": [],
                                        "decision_quality": {
                                            dimension: 1
                                            for dimension in builder.DECISION_QUALITY_DIMENSIONS
                                        },
                                        "evidence": [],
                                        "reviewed_at": commit_timestamp.isoformat(),
                                    }
                                )
                    runner._write_json(
                        reviews_path,
                        {
                            "schema": "datasage-live-review-batch/v1",
                            "reviews": reviews,
                        },
                    )
                    self.assertEqual(
                        0,
                        runner.main(
                            [
                                "finalize",
                                *common,
                                "--reviews",
                                str(reviews_path),
                                "--fixture-attestation-sha256",
                                "e" * 64,
                                "--business-database-ref-sha256",
                                "f" * 64,
                            ]
                        ),
                    )

                self.assertEqual(4, runtime_probe.call_count)
                self.assertEqual(9, process_probe.call_count)
                self.assertTrue((evidence_dir / f"live-release-{commit}.json").is_file())
                self.assertTrue(
                    forbidden_roots.isdisjoint(
                        name.split(".", 1)[0] for name in imported
                    )
                )
            finally:
                for path in evidence_dir.rglob("*") if evidence_dir.exists() else []:
                    if path.is_file():
                        path.chmod(0o600)

    def test_wecom_capture_requires_exact_three_unique_session_ids_before_runtime(self):
        runner = self._runner_module()
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        args = types.SimpleNamespace(session_ids=["one", "one", "three"])
        with mock.patch.object(runner, "_runtime") as runtime:
            with self.assertRaisesRegex(RuntimeError, "three unique ordered"):
                with mock.patch.object(runner, "_require_capture_caller_readiness"):
                    runner._capture(args, contract, {}, object())
        runtime.assert_not_called()

    def test_wecom_session_origin_requires_dm_hash_title_and_commit_time(self):
        runner = self._runner_module()
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(encoding="utf-8")
        )
        identity = "synthetic-self"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        contract["inbound"]["expected_user_id_sha256"] = digest
        contract["inbound"]["expected_chat_id_sha256"] = digest
        session = {
            "id": "complete-session",
            "source": "wecom",
            "chat_type": "dm",
            "user_id": identity,
            "chat_id": identity,
            "title": "datasage-live-111111111111-run-1",
            "started_at": "2026-08-30T00:00:01+00:00",
        }
        origin = runner._session_origin(
            session,
            contract=contract,
            commit="1" * 40,
            run_index=1,
            commit_timestamp=datetime.fromisoformat("2026-08-30T00:00:00+00:00"),
        )
        self.assertEqual(digest, origin["user_id_sha256"])
        self.assertEqual(digest, origin["chat_id_sha256"])
        for key, value in (("source", "cli"), ("chat_type", "group"), ("title", "wrong")):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                runner._session_origin(
                    {**session, key: value}, contract=contract, commit="1" * 40,
                    run_index=1, commit_timestamp=datetime.fromisoformat("2026-08-30T00:00:00+00:00"),
                )

    def test_terminal_assistant_requires_zero_blocking_clarify_and_closed_tools(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]
        ordinary = [
            {"id": 1, "role": "user", "content": "first"},
            {"id": 2, "role": "assistant", "content": "final one", "tool_calls": None},
            {"id": 3, "role": "user", "content": "second"},
            {
                "id": 4,
                "role": "assistant",
                "content": "候选 A、候选 B；请确认后我再查询。",
                "tool_calls": None,
            },
        ]
        self.assertEqual(
            [(1, 2), (3, 4)],
            [item[:2] for item in runner._endpoints(ordinary, prompts, policy)],
        )

        closed_tool = [
            {"id": 1, "role": "user", "content": "first"},
            {
                "id": 2,
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "catalog-1",
                    "function": {"name": "datasage_catalog", "arguments": "{}"},
                }],
            },
            {
                "id": 3,
                "role": "tool",
                "tool_call_id": "catalog-1",
                "tool_name": "datasage_catalog",
                "content": "{}",
            },
            {"id": 4, "role": "assistant", "content": "final one", "tool_calls": None},
            {"id": 5, "role": "user", "content": "second"},
            {"id": 6, "role": "assistant", "content": "final two", "tool_calls": None},
        ]
        self.assertEqual(2, len(runner._endpoints(closed_tool, prompts, policy)))
        with self.assertRaisesRegex(
            RuntimeError, "pending tool calls must be closed by the next non-system"
        ):
            runner._endpoints(
                [closed_tool[0], closed_tool[1], *closed_tool[3:]],
                prompts,
                policy,
            )

        blocking_clarify = copy.deepcopy(closed_tool)
        blocking_clarify[1]["tool_calls"][0]["id"] = "clarify-1"
        blocking_clarify[1]["tool_calls"][0]["function"] = {
            "name": "clarify",
            "arguments": json.dumps({"question": "Which entity?"}),
        }
        blocking_clarify[2]["tool_call_id"] = "clarify-1"
        blocking_clarify[2]["tool_name"] = "clarify"
        with self.assertRaisesRegex(
            RuntimeError, "blocking clarify tool calls are forbidden"
        ):
            runner._endpoints(blocking_clarify, prompts, policy)

        builder_path = PROFILE_ROOT / "build_release_receipt.py"
        spec = importlib.util.spec_from_file_location(
            "_datasage_live_release_builder_test", builder_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual(2, len(builder._live_endpoints(ordinary, prompts, policy)))
        with self.assertRaisesRegex(
            ValueError, "blocking clarify tool calls are forbidden"
        ):
            builder._live_endpoints(blocking_clarify, prompts, policy)

    def test_synthetic_three_turn_confirmation_reuses_endpoints_without_live_evidence(self):
        runner = self._runner_module()
        builder = runner._BUILDER
        prompts = [
            "越南今年的经营情况",
            "泰国呢",
            "我确认指的是 Thai Kim（部门），继续看今年经营表现。",
        ]
        policy = [
            {
                "case_id": f"synthetic_confirmation_turn_{turn}",
                "turn": turn,
                "assistant_completion": "ordinary_text",
                "maximum_blocking_clarify_calls": 0,
            }
            for turn in range(1, 4)
        ]
        transcript = [
            {"id": 1, "role": "user", "content": prompts[0]},
            {"id": 2, "role": "assistant", "content": "越南经营表现答复。"},
            {"id": 3, "role": "user", "content": prompts[1]},
            {
                "id": 4,
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "resolver-turn-2",
                        "function": {
                            "name": "datasage_entity_resolve",
                            "arguments": json.dumps(
                                {"query": "泰国", "entity_type": "department"},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
            {
                "id": 5,
                "role": "tool",
                "tool_call_id": "resolver-turn-2",
                "tool_name": "datasage_entity_resolve",
                "content": json.dumps(
                    {"status": "ambiguous", "candidates": ["Thai Kim", "BKK"]},
                    ensure_ascii=False,
                ),
            },
            {
                "id": 6,
                "role": "assistant",
                "content": "候选是 Thai Kim（部门）和 BKK（部门）；请确认后我再查询。",
                "tool_calls": None,
            },
            {"id": 7, "role": "user", "content": prompts[2]},
            {
                "id": 8,
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "query-turn-3",
                        "function": {
                            "name": "datasage_query",
                            "arguments": json.dumps(
                                {"entity_type": "department", "entity": "Thai Kim"},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
            {
                "id": 9,
                "role": "tool",
                "tool_call_id": "query-turn-3",
                "tool_name": "datasage_query",
                "content": json.dumps({"status": "success", "rows": []}),
            },
            {
                "id": 10,
                "role": "assistant",
                "content": "已按部门 Thai Kim 查询；这是经营表现答复。",
                "tool_calls": None,
            },
        ]
        expected = [
            (1, 2, hashlib.sha256("越南经营表现答复。".encode("utf-8")).hexdigest()),
            (
                3,
                6,
                hashlib.sha256(
                    "候选是 Thai Kim（部门）和 BKK（部门）；请确认后我再查询。".encode(
                        "utf-8"
                    )
                ).hexdigest(),
            ),
            (
                7,
                10,
                hashlib.sha256(
                    "已按部门 Thai Kim 查询；这是经营表现答复。".encode("utf-8")
                ).hexdigest(),
            ),
        ]
        self.assertEqual(expected, runner._endpoints(transcript, prompts, policy))
        self.assertEqual(expected, builder._live_endpoints(transcript, prompts, policy))

        tracked_contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        synthetic_contract = copy.deepcopy(tracked_contract)
        synthetic_contract["case_plan"]["case_ids"] = [
            item["case_id"] for item in policy
        ]
        synthetic_contract["case_plan"]["turns_per_session"] = 3
        synthetic_contract["turn_completion_policy"] = policy
        with self.assertRaisesRegex(ValueError, "exactly two unique case IDs"):
            builder._validate_live_contract(synthetic_contract)

    def test_legacy_function_call_and_pending_interposition_fail_closed(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]

        for function_name in ("clarify", "datasage_catalog"):
            legacy = [
                {"id": 1, "role": "user", "content": "first"},
                {
                    "id": 2,
                    "role": "assistant",
                    "content": None,
                    "function_call": {"name": function_name, "arguments": "{}"},
                },
                {"id": 3, "role": "assistant", "content": "final one"},
                {"id": 4, "role": "user", "content": "second"},
                {"id": 5, "role": "assistant", "content": "final two"},
            ]
            with self.subTest(function_name=function_name), self.assertRaisesRegex(
                RuntimeError, "legacy function_call is forbidden"
            ):
                runner._endpoints(legacy, prompts, policy)

        initial_call = {
            "id": 2,
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "catalog-1",
                "function": {"name": "datasage_catalog", "arguments": "{}"},
            }],
        }
        result = {
            "id": 4,
            "role": "tool",
            "tool_call_id": "catalog-1",
            "tool_name": "datasage_catalog",
            "content": "{}",
        }
        tail = [
            {"id": 5, "role": "assistant", "content": "final one"},
            {"id": 6, "role": "user", "content": "second"},
            {"id": 7, "role": "assistant", "content": "final two"},
        ]
        interposed = [
            {"id": 1, "role": "user", "content": "first"},
            initial_call,
            {"id": 3, "role": "assistant", "content": "interposed"},
            result,
            *tail,
        ]
        with self.assertRaisesRegex(
            RuntimeError, "pending tool calls must be closed by the next non-system"
        ):
            runner._endpoints(interposed, prompts, policy)

        second_batch = copy.deepcopy(interposed)
        second_batch[2] = {
            "id": 3,
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "query-1",
                "function": {"name": "datasage_query", "arguments": "{}"},
            }],
        }
        with self.assertRaisesRegex(
            RuntimeError, "pending tool calls must close before a new assistant"
        ):
            runner._endpoints(second_batch, prompts, policy)

    def test_parallel_tool_results_close_in_declaration_order(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]
        transcript = [
            {"id": 1, "role": "user", "content": "first"},
            {
                "id": 2,
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "catalog-1",
                        "function": {"name": "datasage_catalog", "arguments": "{}"},
                    },
                    {
                        "id": "resolver-1",
                        "function": {
                            "name": "datasage_entity_resolve",
                            "arguments": "{}",
                        },
                    },
                ],
            },
            {"id": 3, "role": "system", "content": "transport note"},
            {
                "id": 4,
                "role": "tool",
                "tool_call_id": "catalog-1",
                "tool_name": "datasage_catalog",
                "content": "{}",
            },
            {"id": 5, "role": "system", "content": "transport note"},
            {
                "id": 6,
                "role": "tool",
                "tool_call_id": "resolver-1",
                "tool_name": "datasage_entity_resolve",
                "content": "{}",
            },
            {"id": 7, "role": "assistant", "content": "final one"},
            {"id": 8, "role": "user", "content": "second"},
            {"id": 9, "role": "assistant", "content": "final two"},
        ]
        self.assertEqual(2, len(runner._endpoints(transcript, prompts, policy)))

        reversed_results = copy.deepcopy(transcript)
        reversed_results[3], reversed_results[5] = (
            reversed_results[5],
            reversed_results[3],
        )
        with self.assertRaisesRegex(RuntimeError, "in declaration order"):
            runner._endpoints(reversed_results, prompts, policy)

    def test_endpoint_projection_ignores_only_session_meta_and_retains_raw_export(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]
        raw_messages = [
            {"id": 1, "role": "user", "content": "first"},
            {
                "id": 2,
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "catalog-1",
                    "function": {"name": "datasage_catalog", "arguments": "{}"},
                }],
            },
            {
                "id": 3,
                "role": "session_meta",
                "content": None,
                "tool_calls": None,
                "tool_call_id": None,
                "tool_name": None,
                "function_call": None,
                "tools": [{"type": "function", "function": {"name": "official_tool"}}],
                "model": "official-model",
                "timestamp": 1.0,
            },
            {
                "id": 4,
                "role": "tool",
                "tool_call_id": "catalog-1",
                "tool_name": "datasage_catalog",
                "content": "{}",
            },
            {"id": 5, "role": "assistant", "content": "final one", "tool_calls": None},
            {"id": 6, "role": "session_meta", "content": None, "platform": "wecom"},
            {"id": 7, "role": "user", "content": "second"},
            {
                "id": 8,
                "role": "assistant",
                "content": "候选 A、候选 B；请确认后我再查询。",
                "tool_calls": None,
            },
            {"id": 9, "role": "session_meta", "content": None, "tools": []},
        ]
        payload = json.dumps(
            {"id": "official-session", "messages": raw_messages},
            ensure_ascii=False,
        )
        _, exported_messages = runner._export(payload)
        retained = copy.deepcopy(exported_messages)

        self.assertEqual(
            [(1, 5), (7, 8)],
            [item[:2] for item in runner._endpoints(exported_messages, prompts, policy)],
        )
        self.assertEqual(retained, exported_messages)
        self.assertEqual(
            3,
            sum(item.get("role") == "session_meta" for item in exported_messages),
        )

        adversarial_carriers = (
            (
                "nonempty-content",
                {"content": "hidden conversational text"},
                "session_meta contains conversational or tool-flow payload",
            ),
            ("push-tool-calls", {"tool_calls": [{
                "id": "push-1",
                "function": {"name": "datasage_push", "arguments": "{}"},
            }]}, "session_meta contains conversational or tool-flow payload"),
            (
                "fake-tool-result",
                {"tool_call_id": "push-1", "tool_name": "datasage_push"},
                "session_meta contains conversational or tool-flow payload",
            ),
            (
                "legacy-function-call",
                {"function_call": {"name": "datasage_push", "arguments": "{}"}},
                "legacy function_call is forbidden",
            ),
        )
        session_meta_index = next(
            index
            for index, item in enumerate(exported_messages)
            if item.get("role") == "session_meta"
        )
        for label, carriers, pattern in adversarial_carriers:
            adversarial = copy.deepcopy(exported_messages)
            adversarial[session_meta_index].update(carriers)
            unchanged = copy.deepcopy(adversarial)
            with self.subTest(carrier=label), self.assertRaisesRegex(
                RuntimeError, pattern
            ):
                runner._endpoints(adversarial, prompts, policy)
            self.assertEqual(unchanged, adversarial)

        unsupported = copy.deepcopy(exported_messages)
        unsupported.insert(-1, {"id": 10, "role": "audit_meta", "content": None})
        with self.assertRaisesRegex(RuntimeError, "unsupported conversational role"):
            runner._endpoints(unsupported, prompts, policy)

    def test_turn_completion_policy_is_zero_clarify_and_single_owned(self):
        runner = self._runner_module()
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]
        self.assertEqual(policy, runner._turn_completion_policies(policy, 2))

        invalid = copy.deepcopy(policy)
        invalid[0]["maximum_blocking_clarify_calls"] = 1
        with self.assertRaisesRegex(
            RuntimeError, "blocking clarify tool calls must be forbidden"
        ):
            runner._turn_completion_policies(invalid, 2)

        invalid = copy.deepcopy(policy)
        invalid[1]["assistant_completion"] = "blocking_tool"
        with self.assertRaisesRegex(
            RuntimeError, "ordinary assistant text"
        ):
            runner._turn_completion_policies(invalid, 2)

        builder = runner._BUILDER
        self.assertIs(runner._export, builder._export_session)
        self.assertIs(
            runner._turn_completion_policies,
            builder._live_turn_completion_policies,
        )
        self.assertIs(runner._endpoints, builder._live_endpoints)
        self.assertFalse(hasattr(builder, "_live_evidence_producer"))

        builder_path = PROFILE_ROOT / "build_release_receipt.py"
        spec = importlib.util.spec_from_file_location(
            "_datasage_live_policy_owner_direct_test", builder_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        direct_builder = importlib.util.module_from_spec(spec)
        with mock.patch(
            "importlib.util.spec_from_file_location",
            side_effect=AssertionError("builder must not load runner or another owner"),
        ):
            spec.loader.exec_module(direct_builder)
        self.assertTrue(callable(direct_builder._export_session))
        self.assertTrue(callable(direct_builder._live_endpoints))
        self.assertFalse(hasattr(direct_builder, "_live_evidence_producer"))

        contract_document = {"turn_completion_policy": policy}
        golden_document = {"cases": []}
        with (
            mock.patch.object(
                runner, "_read_json", side_effect=[contract_document, golden_document]
            ),
            mock.patch.object(runner, "_load_module", side_effect=AssertionError),
            mock.patch.object(builder, "_validate_live_contract") as validate,
            mock.patch.object(runner, "_capture", return_value=0) as capture,
        ):
            exit_code = runner.main(
                [
                    "capture",
                    "--hermes-python",
                    "python",
                    "--hermes-root",
                    ".",
                    "--session-id",
                    "run-1",
                    "--session-id",
                    "run-2",
                    "--session-id",
                    "run-3",
                ]
            )
        self.assertEqual(0, exit_code)
        validate.assert_called_once_with(contract_document)
        capture.assert_called_once()

    def test_inbound_capture_rejects_push_and_tool_name_entitlement_bypass(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        policy = contract["turn_completion_policy"]

        def transcript(function_name, tool_name, content):
            return [
                {"id": 1, "role": "user", "content": "first"},
                {
                    "id": 2,
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call-1", "function": {"name": function_name}}],
                },
                {"id": 3, "role": "tool", "tool_call_id": "call-1", "tool_name": tool_name, "content": content},
                {"id": 4, "role": "assistant", "content": "final one", "tool_calls": None},
                {"id": 5, "role": "user", "content": "second"},
                {"id": 6, "role": "assistant", "content": "final two", "tool_calls": None},
            ]

        with self.assertRaisesRegex(RuntimeError, "datasage_push is forbidden"):
            runner._endpoints(
                transcript("datasage_push", "datasage_push", "{}"),
                prompts,
                policy,
            )

        denial = json.dumps({"error": {"code": "DATA_ENTITLEMENT_DENIED"}})
        for tool_name in ("datasage_catalog", ""):
            with self.subTest(tool_name=tool_name), self.assertRaisesRegex(RuntimeError, "name does not match"):
                runner._endpoints(
                    transcript("datasage_query", tool_name, denial), prompts, policy
                )

    def test_capture_stage_cleanup_is_scoped_to_exact_private_root(self):
        runner = self._runner_module()
        commit = "1" * 40
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence"
            private = evidence / "private"
            stage = private / f".capture-stage-{commit}-{'a' * 32}"
            stage.mkdir(parents=True)
            (stage / "partial").write_text("partial", encoding="utf-8")
            with mock.patch.object(runner, "EVIDENCE_DIR", evidence):
                runner._cleanup_capture_stage(stage, commit)
                self.assertFalse(stage.exists())
                outside = evidence / f".capture-stage-{commit}-{'b' * 32}"
                outside.mkdir()
                with self.assertRaisesRegex(RuntimeError, "unscoped"):
                    runner._cleanup_capture_stage(outside, commit)
                self.assertTrue(outside.exists())

    def test_nonretryable_entitlement_is_detected_from_structured_tool_result(self):
        runner = self._runner_module()
        denied = [
            {
                "role": "tool",
                "tool_name": "datasage_catalog",
                "content": json.dumps(
                    {"error": {"code": "DATA_ENTITLEMENT_DENIED"}}
                ),
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "nonretryable DATA_ENTITLEMENT_DENIED"):
            runner._fail_on_nonretryable_tool_result(denied)
        runner._fail_on_nonretryable_tool_result(
            [
                {
                    "role": "tool",
                    "tool_name": "datasage_catalog",
                    "content": json.dumps(
                        {"status": "error", "message": "DATA_ENTITLEMENT_DENIED"}
                    ),
                }
            ]
        )

    def test_live_contract_reuses_the_tracked_golden_cases_without_legacy_ids(self):
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        release = suite["release_validation"]
        contract = json.loads(
            (PROFILE_ROOT / "tests" / "fixtures" / "live_release_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            contract["case_plan"]["case_ids"],
            release["trusted_replay_gate"]["case_ids"],
        )
        selected = [
            case for case in suite["cases"]
            if case["id"] in contract["case_plan"]["case_ids"]
        ]
        self.assertEqual([1, 2], [case["turn"] for case in selected])
        self.assertEqual(1, len({case["conversation_id"] for case in selected}))
        self.assertEqual([5, 2], [len(case["required_conclusions"]) for case in selected])
        self.assertEqual("no_query", selected[1]["plan_constraints"]["time_semantics"])
        self.assertNotIn("session_id", contract)
        self.assertNotIn("message_id", json.dumps(contract, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
