"""Precise regression tests for the Hermes/DataSage integration boundary."""

from __future__ import annotations

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
from tools import clarify_tool as _hermes_clarify_registration  # noqa: F401
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


class RuntimeBoundaryTests(unittest.TestCase):
    def test_runtime_gate_has_no_dsrt_release_or_subprocess_prerequisite(self):
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
        self.assertEqual("hermes_managed", status["runtime"])

        missing = PROFILE_ROOT / "does-not-exist"
        self.assertFalse(
            runtime_health.runtime_identity_status(profile_root=missing)["ready"]
        )

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

        self.assertIn('authority="git"', context)
        self.assertIn('immutable="process"', context)
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
    @staticmethod
    def _bool_setting(name: str, default: bool = False) -> bool:
        return False

    def test_regular_marker_enables_production_without_release_manifest(self):
        with tempfile.TemporaryDirectory() as raw_parent:
            root = Path(raw_parent) / "datasage-canary-next"
            root.mkdir()
            (root / ".production-release").touch()
            with mock.patch.object(
                db_security.settings,
                "get_bool",
                side_effect=self._bool_setting,
            ):
                policy = db_security.mysql_tls_policy(profile_root=root)
            self.assertTrue(policy["production_mode"])
            self.assertTrue(policy["tls_required"])
            self.assertFalse((root / ".release").exists())

    def test_configured_production_mode_still_enables_tls(self):
        with tempfile.TemporaryDirectory() as raw_parent:
            root = Path(raw_parent) / "datasage-canary-next"
            root.mkdir()

            def configured(name: str, default: bool = False) -> bool:
                return name == "production_mode"

            with mock.patch.object(
                db_security.settings,
                "get_bool",
                side_effect=configured,
            ):
                policy = db_security.mysql_tls_policy(profile_root=root)
            self.assertTrue(policy["production_mode"])
            self.assertTrue(policy["tls_required"])

    def test_non_regular_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw_parent:
            root = Path(raw_parent) / "datasage-canary-next"
            root.mkdir()
            (root / ".production-release").mkdir()
            with self.assertRaises(db_security.DatabaseSecurityError) as captured:
                db_security.mysql_tls_policy(profile_root=root)
            self.assertEqual(
                "DATABASE_PRODUCTION_MARKER_INVALID",
                captured.exception.code,
            )

    def test_marker_forbids_canary_existing_account_exception(self):
        with tempfile.TemporaryDirectory() as raw_parent:
            root = Path(raw_parent) / "datasage-canary-next"
            root.mkdir()
            (root / ".production-release").touch()
            with (
                mock.patch.object(
                    db_security.settings,
                    "get_bool",
                    return_value=True,
                ),
                self.assertRaises(db_security.DatabaseSecurityError) as captured,
            ):
                db_security.canary_existing_account_accepted(profile_root=root)
            self.assertEqual(
                "DATABASE_CANARY_ACCOUNT_ACCEPTANCE_FORBIDDEN",
                captured.exception.code,
            )


class DistributionBoundaryTests(unittest.TestCase):
    def test_current_distribution_restores_hermes_bundled_skill_seeding(self):
        distribution = (PROFILE_ROOT / "distribution.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".no-bundled-skills", distribution)
        self.assertNotIn("- .release", distribution)
        self.assertFalse((PROFILE_ROOT / ".no-bundled-skills").exists())

    def test_wecom_narrow_surface_is_an_explicit_channel_exception(self):
        config = (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        parsed_config = yaml.safe_load(config)
        self.assertIn("Intentional channel-security exception", config)
        wecom_toolsets = parsed_config["platform_toolsets"]["wecom"]
        self.assertIsInstance(wecom_toolsets, list)
        self.assertEqual(
            {"datasage-query", "clarify", "todo"},
            set(wecom_toolsets),
        )
        self.assertEqual(3, len(wecom_toolsets))
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
