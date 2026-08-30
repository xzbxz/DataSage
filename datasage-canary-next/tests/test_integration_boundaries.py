"""Precise regression tests for the Hermes/DataSage integration boundary."""

from __future__ import annotations

import ast
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

from plugin_registration_probe import probe_registration


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

    def test_official_plugin_entry_validates_business_before_entitlement_denial(
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
        business_validation.assert_called_once()
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
                    "entity_type": "department",
                    "canonical_id": "d1",
                    "canonical_code": "BKK",
                    "display_name": "Thailand department",
                    "filter_values": ["Thailand department"],
                    "match_kind": "contains",
                    "confidence": "candidate",
                    "filter_role": "department",
                },
                {
                    "entity_type": "customer_region",
                    "canonical_id": "r1",
                    "canonical_code": "TH",
                    "display_name": "Thailand region",
                    "filter_values": ["Thailand region"],
                    "match_kind": "contains",
                    "confidence": "candidate",
                    "filter_role": "customer_region",
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

    def test_distribution_allows_reviewed_bundled_skill_sync(self):
        distribution = (PROFILE_ROOT / "distribution.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("- .no-bundled-skills", distribution)
        self.assertNotIn("- .release", distribution)
        marker = PROFILE_ROOT / ".no-bundled-skills"
        self.assertFalse(marker.exists())

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
        self.assertEqual("datasage-live-release-contract/v2", contract["schema"])
        self.assertEqual(2, contract["case_plan"]["turns_per_session"])
        self.assertEqual(2, len(contract["case_plan"]["case_ids"]))
        self.assertEqual(3, contract["case_plan"]["runs"])
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
        path = PROFILE_ROOT / "tests" / "run_live_release_evidence.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(
            {"run_agent", "AIAgent", "planner", "trusted_replay_runner", "hermes_replay_driver"}.intersection(imports)
        )
        self.assertNotIn("from run_agent import", source)
        self.assertNotIn("AIAgent(", source)
        self.assertNotIn("latest", source)
        self.assertNotIn("_rebind", source)
        self.assertNotIn("command[-len(template):]", source)
        self.assertIn('shapes["session_export"]', source)
        self.assertIn("canary_transcript_adapter.py", source)
        self.assertIn("golden_expert_scorer.py", source)
        self.assertIn('"private" / commit', source)
        self.assertNotIn("delivery_obligations", source)
        self.assertIn('choices=("capture", "finalize")', source)
        self.assertIn("PYTHONNOUSERSITE", source)
        self.assertIn("partial finalize artifacts exist", source)
        self.assertIn("expected_user_id_sha256", source)
        self.assertIn("_set_read_only", source)
        self.assertIn("capture.sha256", source)
        self.assertIn("_python_provenance", source)
        self.assertNotIn('ack.get("target")', source)
        self.assertIn(".capture-stage-", source)
        self.assertIn("stage.replace(root)", source)
        self.assertNotIn('shapes["outbound"]', source)
        self.assertNotIn("protocol probe", source)
        self.assertIn('"processes": {"session_export": export_record}', source)
        self.assertIn('"processes": {"adapter": adapter_record, "scorer": scorer_record}', source)

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

    def test_terminal_assistant_allows_closed_clarify_but_rejects_pending_tool(self):
        runner = self._runner_module()
        prompts = ["first", "second"]
        closed = [
            {"id": 1, "role": "user", "content": "first"},
            {"id": 2, "role": "assistant", "content": None, "tool_calls": [{"id": "clarify-1", "function": {"name": "clarify"}}]},
            {"id": 3, "role": "tool", "tool_call_id": "clarify-1", "tool_name": "clarify", "content": "declined"},
            {"id": 4, "role": "assistant", "content": "final one", "tool_calls": None},
            {"id": 5, "role": "user", "content": "second"},
            {"id": 6, "role": "assistant", "content": "final two", "tool_calls": None},
        ]
        self.assertEqual(2, len(runner._endpoints(closed, prompts)))
        with self.assertRaisesRegex(RuntimeError, "unclosed tool flow"):
            runner._endpoints([*closed[:2], *closed[3:]], prompts)

    def test_inbound_capture_rejects_push_and_tool_name_entitlement_bypass(self):
        runner = self._runner_module()
        prompts = ["first", "second"]

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
            runner._endpoints(transcript("datasage_push", "datasage_push", "{}"), prompts)

        denial = json.dumps({"error": {"code": "DATA_ENTITLEMENT_DENIED"}})
        for tool_name in ("datasage_catalog", ""):
            with self.subTest(tool_name=tool_name), self.assertRaisesRegex(RuntimeError, "name does not match"):
                runner._endpoints(
                    transcript("datasage_query", tool_name, denial), prompts
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
