"""Regression checks for the DataSage Profile-surface remediation.

These checks intentionally stay at the Profile/configuration boundary. They do
not import the DataSage plugin implementation or mutate Hermes host code.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROFILE_ROOT / "config.yaml"
SKILL_PATH = PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "SKILL.md"
ANSWER_BOUNDARY_PATH = (
    PROFILE_ROOT
    / "skills"
    / "business-analytics"
    / "datasage"
    / "references"
    / "answer-boundary.md"
)

SUPPORTED_DOMAINS = {
    "delivery",
    "receipt",
    "receivable",
    "target",
    "customer_risk",
    "inventory",
}


def _load_config() -> dict[str, object]:
    parsed = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("config.yaml must contain a mapping")
    return parsed


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    _, raw, _ = text.split("---", 2)
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise AssertionError("SKILL.md frontmatter must contain a mapping")
    return parsed


def _load_entitlements():
    """Load only the authorization module under an isolated package name."""

    package_name = "_datasage_remediation_profile_surface"
    plugin_root = PROFILE_ROOT / "plugins" / "datasage-query"
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_root)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.entitlements")


class RemediationProfileSurfaceTests(unittest.TestCase):
    def test_wecom_declared_toolsets_are_narrow_and_cli_is_unchanged(self):
        config = _load_config()
        platform_toolsets = config["platform_toolsets"]
        self.assertEqual(
            ["hermes-cli", "datasage-query"],
            platform_toolsets["cli"],
        )
        self.assertEqual(
            ["skills", "clarify", "datasage-query"],
            platform_toolsets["wecom"],
        )
        self.assertNotIn("hermes-wecom", platform_toolsets["wecom"])

    def test_resolved_wecom_tools_exclude_system_and_mutation_tools(self):
        """Resolve the final host tool surface and enforce the deny boundary."""

        os.environ.setdefault("HERMES_HOME", str(PROFILE_ROOT))
        try:
            from hermes_cli.tools_config import _get_platform_tools
            from toolsets import resolve_toolset
        except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
            self.skipTest(f"Hermes host runtime unavailable: {exc}")

        config = _load_config()
        resolved_toolsets = _get_platform_tools(
            config,
            "wecom",
            include_default_mcp_servers=False,
        )
        resolved_tools: set[str] = set()
        for toolset in resolved_toolsets:
            expanded = resolve_toolset(toolset)
            resolved_tools.update(expanded or [toolset])

        forbidden_exact = {
            "terminal",
            "process",
            "read",
            "read_file",
            "write",
            "write_file",
            "patch",
            "search_files",
            "code",
            "execute_code",
            "delegate",
            "delegate_task",
            "cron",
            "cronjob",
            "browser",
        }
        forbidden_prefixes = (
            "read_",
            "write_",
            "browser_",
            "code_",
            "delegate_",
            "cron_",
        )
        violations = sorted(
            tool
            for tool in resolved_tools
            if tool in forbidden_exact
            or any(tool.startswith(prefix) for prefix in forbidden_prefixes)
        )
        self.assertEqual([], violations)

        skills_tools = set(resolve_toolset("skills"))
        self.assertIn("skill_view", skills_tools)
        self.assertIn("skill_manage", skills_tools)

    def test_wecom_private_group_and_data_access_match_business_policy(self):
        config = _load_config()
        wecom = config["platforms"]["wecom"]
        extra = wecom["extra"]
        home_channel = wecom["home_channel"]
        home_user = home_channel["user_id"]

        self.assertIsInstance(home_user, str)
        self.assertTrue(home_user.strip())
        self.assertNotEqual("*", home_user)
        self.assertEqual(["*"], extra["allow_from"])
        self.assertEqual("allowlist", extra["group_policy"])
        self.assertEqual(["*"], extra["group_allow_from"])
        self.assertEqual(["*"], extra["groups"]["*"]["allow_from"])

        settings = config["plugins"]["entries"]["datasage-query"]["settings"]
        policy = settings["data_entitlements"]
        principals = policy["principals"]
        self.assertEqual(1, len(principals))
        principal = principals[0]
        self.assertEqual("*", principal["user_id"])
        self.assertEqual(["*"], principal["domains"])
        self.assertEqual(SUPPORTED_DOMAINS, set(principal["metrics"]))
        self.assertEqual(["*"], principal["entity_types"])
        self.assertTrue(principal["allow_unscoped_entity_resolution"])
        self.assertTrue(principal["allow_type_neutral_entity_resolution"])
        self.assertTrue(principal["allow_entity_resolution_all_rows"])
        self.assertTrue(principal["allow_all_rows"])

    def test_all_wecom_users_match_principal_with_all_row_access(self):
        """The business owner explicitly grants the governed surface to WeCom."""

        entitlements = _load_entitlements()
        config = _load_config()
        home_user = config["platforms"]["wecom"]["home_channel"]["user_id"]
        policy = config["plugins"]["entries"]["datasage-query"]["settings"][
            "data_entitlements"
        ]
        query_args = {
            "requests": [
                {
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "metric_filters": {},
                }
            ]
        }
        entity_args = {
            "domain": "delivery",
            "metric": "delivery_amount",
            "entity_types": ["department"],
        }

        for user_id in (home_user, "another-authenticated-wecom-user"):
            with self.subTest(user_id=user_id):
                def session_value(name: str) -> str:
                    if name == "HERMES_SESSION_PLATFORM":
                        return "wecom"
                    if name == "HERMES_SESSION_USER_ID":
                        return user_id
                    return ""

                with mock.patch.object(
                    entitlements,
                    "_session_value",
                    side_effect=session_value,
                ):
                    rule = entitlements._principal_rule(policy)
                    self.assertIsNotNone(rule)
                    self.assertTrue(entitlements._query_allowed(rule, query_args))
                    self.assertTrue(entitlements._entity_allowed(rule, entity_args))

    def test_skill_frontmatter_and_answer_boundary_declare_scope(self):
        frontmatter = _frontmatter(SKILL_PATH)
        hermes = frontmatter["metadata"]["hermes"]
        self.assertEqual(SUPPORTED_DOMAINS, set(hermes["supported_domains"]))
        self.assertTrue(hermes["non_activation_examples"])

        answer_boundary = ANSWER_BOUNDARY_PATH.read_text(encoding="utf-8").casefold()
        for phrase in ("collections", "cash flow", "cash balance", "liquidity"):
            self.assertIn(phrase, answer_boundary)

        soul = (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8")
        for phrase in ("Primary users", "three risk levels", "human approver", "Escalate"):
            self.assertIn(phrase, soul)

    def test_enabled_skill_documentation_matches_denylist(self):
        config = _load_config()
        disabled = set(config["skills"]["disabled"])
        for name in (
            "document-to-action-items",
            "grounded-citations",
            "meeting-action-items",
            "weekly-review-planning",
        ):
            self.assertIn(name, disabled)

        readme = (PROFILE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("引用、会议/文档行动项和周度计划", readme)
        self.assertIn("文档、表格、PDF、演示文稿和 OCR", readme)

    def test_env_example_lists_current_keys_with_blank_values(self):
        expected = {
            "DEEPSEEK_API_KEY",
            "DATA_QUERY_MYSQL_HOST",
            "DATA_QUERY_MYSQL_PORT",
            "DATA_QUERY_MYSQL_DATABASE",
            "DATA_QUERY_MYSQL_USER",
            "DATA_QUERY_MYSQL_PASSWORD",
            "DATA_QUERY_MYSQL_SSL_CA",
            "WECOM_BOT_ID",
            "WECOM_SECRET",
            "WECOM_HOME_CHANNEL",
            "WECOM_HOME_CHANNEL_THREAD_ID",
        }
        assignments: dict[str, str] = {}
        for line in (PROFILE_ROOT / ".env.EXAMPLE").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match:
                assignments[match.group(1)] = match.group(2)
        self.assertEqual(expected, set(assignments))
        self.assertTrue(all(value == "" for value in assignments.values()))

    def test_profile_gitignore_keeps_example_and_remediation_tests_trackable(self):
        ignore = (PROFILE_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env.*", ignore)
        self.assertIn("!.env.EXAMPLE", ignore)
        self.assertIn("!tests/", ignore)
        self.assertIn("!tests/test_remediation_*.py", ignore)


if __name__ == "__main__":
    unittest.main()
