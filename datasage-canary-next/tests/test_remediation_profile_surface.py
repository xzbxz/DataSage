"""Regression checks for the DataSage Profile-surface remediation.

These checks intentionally stay at the Profile/configuration boundary. They do
not import the DataSage plugin implementation or mutate Hermes host code.
"""

from __future__ import annotations

import importlib
import json
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
    "receivable",
    "inventory",
    "profit",
    "pattern_matching",
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
            ["clarify", "datasage-query", "code_execution"],
            platform_toolsets["wecom"],
        )
        self.assertNotIn("skills", platform_toolsets["wecom"])
        self.assertNotIn("hermes-wecom", platform_toolsets["wecom"])

    def test_resolved_wecom_tools_include_official_code_without_extra_bundles(self):
        """Check visible tools; execute_code itself has local file authority."""

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

        self.assertIn("execute_code", resolved_tools)

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
        self.assertTrue(skills_tools)
        self.assertTrue(skills_tools.isdisjoint(resolved_tools))

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
        self.assertNotIn("data_entitlements", settings)
        for removed in (
            "mysql_health_connect_timeout_seconds",
            "mysql_grant_audit_timeout_seconds",
        ):
            self.assertNotIn(removed, settings)

        plugin_manifest = yaml.safe_load(
            (PROFILE_ROOT / "plugins" / "datasage-query" / "plugin.yaml").read_text(
                encoding="utf-8"
            )
        )
        schema = plugin_manifest["config_schema"]
        for removed in (
            "data_entitlements",
            "mysql_health_connect_timeout_seconds",
            "mysql_grant_audit_timeout_seconds",
        ):
            self.assertNotIn(removed, schema)

    def test_wecom_does_not_require_skill_references(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("restricted WeCom", skill)
        self.assertIn("references are never a query prerequisite", skill)
        self.assertIn("skill-enabled CLI or maintenance", skill)
        self.assertIn('skill_view(name="datasage", file_path=', skill)
        self.assertNotIn("Load only the governing reference", skill)
        verification = skill.split("## Verification", 1)[1]
        self.assertIn("absence never blocks a WeCom query", verification)
        self.assertNotIn("reference was loaded", verification)

    def test_all_wecom_users_are_admitted_without_data_policy(self):
        """Every bound WeCom member gets the same complete DataSage surface."""

        entitlements = _load_entitlements()
        query_args = {
            "requests": [
                {
                    "domain": "an-unlisted-domain",
                    "metric": "an-unlisted-metric",
                    "metric_filters": {"customer": ["any-row"]},
                }
            ]
        }

        for user_id in ("home-user", "another-authenticated-wecom-user"):
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
                    self.assertTrue(
                        entitlements.coarse_authorized("datasage_query", query_args)
                    )
                    self.assertTrue(
                        entitlements.authorized("datasage_query", query_args)
                    )

    def test_trusted_replay_requires_exact_bound_source_marker(self):
        entitlements = _load_entitlements()
        cases = (
            ("replay", "datasage-trusted-replay", "replay-user", True),
            ("replay", "forged-source", "replay-user", False),
            ("cli", "datasage-trusted-replay", "replay-user", False),
            ("replay", "datasage-trusted-replay", "", False),
        )

        for platform, source, user_id, expected in cases:
            with self.subTest(platform=platform, source=source, user_id=user_id):
                def session_value(name: str) -> str:
                    return {
                        "HERMES_SESSION_PLATFORM": platform,
                        "HERMES_SESSION_SOURCE": source,
                        "HERMES_SESSION_USER_ID": user_id,
                    }.get(name, "")

                with mock.patch.object(
                    entitlements,
                    "_session_value",
                    side_effect=session_value,
                ):
                    self.assertEqual(
                        expected,
                        entitlements.authorized(
                            "datasage_query",
                            {"requests": [{"domain": "anything", "metric": "anything"}]},
                        ),
                    )

    def test_authorization_audit_contains_only_decision_fields(self):
        entitlements = _load_entitlements()
        args = {
            "requests": [
                {
                    "request_id": "sensitive-request-id",
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "metric_filters": {"customer": "Sensitive Customer"},
                }
            ]
        }

        with (
            mock.patch.object(
                entitlements,
                "_session_value",
                side_effect=lambda name: {
                    "HERMES_SESSION_PLATFORM": "wecom",
                    "HERMES_SESSION_USER_ID": "user-1",
                }.get(name, ""),
            ),
            self.assertLogs(entitlements.logger, level="INFO") as captured,
        ):
            self.assertTrue(entitlements.authorized("datasage_query", args))

        line = captured.output[-1]
        event = json.loads(line[line.index("{") :])
        self.assertEqual(
            {"event", "tool", "reason", "principal_ref"},
            set(event),
        )
        self.assertEqual("datasage_entitlement_decision", event["event"])
        self.assertEqual("datasage_query", event["tool"])
        self.assertEqual("wecom_authenticated_member", event["reason"])
        self.assertRegex(event["principal_ref"], r"^[0-9a-f]{64}$")
        self.assertNotIn("Sensitive Customer", captured.output[-1])
        self.assertNotIn("sensitive-request-id", captured.output[-1])

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
        for name in ("docx", "xlsx", "pdf", "powerpoint", "hermes-agent"):
            self.assertIn(f"`{name}`", readme)
        self.assertIn("ocr-and-documents", disabled)
        self.assertIn("已不在当前 core 集合", readme)

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
