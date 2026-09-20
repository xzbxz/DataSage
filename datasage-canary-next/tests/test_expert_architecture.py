from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml

from plugin_registration_probe import probe_registration


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
SKILL_PATH = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "SKILL.md"
)
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
PACKAGE = "datasage_expert_architecture_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PACKAGE] = package

contracts = importlib.import_module(f"{PACKAGE}.contracts")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
tools = importlib.import_module(f"{PACKAGE}.tools")
wire = importlib.import_module(f"{PACKAGE}.wire")


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    _, raw, _ = text.split("---", 2)
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise AssertionError("invalid skill frontmatter")
    return parsed


class ExpertArchitectureTests(unittest.TestCase):
    def test_plugin_skill_versions_and_reviewed_capabilities_are_consistent(self):
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        plugin = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        skill = _frontmatter(SKILL_PATH)
        self.assertEqual(str(plugin["version"]), str(skill["version"]))
        self.assertRegex(config["model"]["default"], r"^[a-z0-9][a-z0-9._-]+$")
        self.assertEqual("off", config["tools"]["tool_search"]["enabled"])
        self.assertTrue(config["skills"]["disabled"])
        self.assertFalse((PROFILE_ROOT / ".no-bundled-skills").exists())
        self.assertFalse(
            (PROFILE_ROOT / "skills" / "datasage" / "datasage-query-patterns").exists()
        )
        settings = config["plugins"]["entries"]["datasage-query"]["settings"]
        self.assertNotIn("max_result_bytes", settings)
        self.assertNotIn("max_batch_bytes", settings)
        self.assertNotIn("max_tool_result_chars", settings)

    def test_plugin_registers_tools_without_answer_state_hooks_or_prompt_sections(self):
        manifest = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_expert_architecture_registration",
        )
        self.assertEqual(
            set(manifest["provides_tools"]),
            {entry["name"] for entry in registration.tools},
        )
        self.assertNotIn("provides_hooks", manifest)
        self.assertEqual([], registration.hooks)
        self.assertEqual([], registration.prompt_sections)

    def test_datasage_skill_is_compact_native_and_tool_gated(self):
        path = SKILL_PATH
        content = path.read_text(encoding="utf-8")
        metadata = _frontmatter(path)
        hermes = metadata["metadata"]["hermes"]
        self.assertLess(len(content), 6_000)
        self.assertEqual(["datasage-query"], hermes["requires_toolsets"])
        self.assertEqual(
            ["datasage_catalog", "datasage_entity_resolve", "datasage_query"],
            hermes["requires_tools"],
        )
        description = metadata["description"]
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith("."))
        self.assertEqual(1, description.count("."))
        description_lower = description.casefold()
        self.assertIn("governed datasage company facts", description_lower)
        self.assertIn("public/user-provided", description_lower)

    def test_entity_reference_links_and_empty_result_boundary(self):
        skill_root = SKILL_PATH.parent

        def normalized(path: Path) -> str:
            return " ".join(path.read_text(encoding="utf-8").split())

        skill = normalized(SKILL_PATH)
        query_rules = normalized(skill_root / "references" / "query-rules.md")
        entity_guidance = normalized(
            skill_root / "references" / "entity-guidance.md"
        )
        answer_boundary = normalized(
            skill_root / "references" / "answer-boundary.md"
        )
        self.assertIn("datasage.entity-guidance/v1", skill)
        self.assertIn("datasage.entity-guidance/v1", query_rules)
        self.assertIn("datasage_catalog", query_rules)
        self.assertIn("empty bounded candidate result", entity_guidance)
        self.assertIn("does not prove that the entity is absent", entity_guidance)
        self.assertNotIn("empty query", entity_guidance)
        query_empty_boundary = (
            "does not prove that an entity or dimension value does not exist"
        )
        self.assertIn(query_empty_boundary, answer_boundary)


    def test_general_clarification_and_git_in_place_policy_have_one_owner(self):
        soul = (PROFILE_ROOT / "SOUL.md").read_text(encoding="utf-8")
        skill = SKILL_PATH.read_text(encoding="utf-8")
        clarification_rule = (
            "Ask for clarification only when materially different interpretations"
        )
        self.assertEqual(1, soul.count(clarification_rule))
        self.assertNotIn(clarification_rule, skill)

        readme = (PROFILE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("当前 Git 原地维护", readme)
        self.assertIn("不创建第二个安装实例", readme)
        self.assertNotIn("第一次迁移必须使用新名称", readme)
        self.assertNotIn("--name datasage-rc8-clean", readme)

    def test_references_use_the_native_skill_surface(self):
        linked = {
            path.name
            for path in (SKILL_PATH.parent / "references").iterdir()
            if path.is_file()
        }
        self.assertEqual(
            {
                "answer-boundary.md",
                "delivery-analysis.md",
                "entity-guidance.md",
                "query-rules.md",
                "receipt-analysis.md",
                "target-analysis.md",
                "inventory-analysis.md",
                "pattern-matching-analysis.md",
                "profit-analysis.md",
                "receivable-analysis.md",
                "cross-domain-analysis.md",
            },
            linked,
        )
        skill = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn('skill_view(name="datasage", file_path=', skill)
        self.assertFalse(hasattr(schemas, "DATASAGE_REFERENCE"))

    def test_batch_metric_details_return_independent_semantics(self):
        selections: list[tuple[str, str]] = []
        for domain in ("delivery", "inventory"):
            index = json.loads(contracts.datasage_catalog({
                "requests": [{"domain": domain, "view": "expert_index"}]
            }))
            metrics = index["results"][0]["metrics"]
            selections.append((domain, metrics[0]["code"]))
        payload = json.loads(contracts.datasage_catalog({
            "requests": [
                {"domain": domain, "metric": metric}
                for domain, metric in selections
            ]
        }))
        self.assertEqual("success", payload["status"])
        self.assertEqual(2, len(payload["results"]))
        for (domain, metric), result in zip(selections, payload["results"]):
            self.assertEqual(domain, result["domain"])
            self.assertEqual("metric", result["level"])
            self.assertEqual(metric, result["metric"]["code"])

    def test_ranked_limit_is_not_silently_reduced_to_ten(self):
        with mock.patch.object(tools, "_bounded_int", return_value=100):
            self.assertEqual(
                50,
                tools._metric_query_limit({
                    "limit": 50,
                    "order_by": {"field": "metric_value", "direction": "desc"},
                }),
            )
        for field in ("requested_limit", "effective_limit", "has_more"):
            self.assertIn(field, tools._MODEL_WIRE_RESULT_FIELDS)

    def test_large_batch_is_left_complete_for_hermes_host_handling(self):
        payload = {
            "status": "success",
            "results": [
                {"request_id": f"q{index}", "value": "x" * 40_000}
                for index in range(3)
            ],
            "evidence_bundle": {"large": "y" * 300},
        }
        rendered = wire.enforce_tool_result_budget("datasage_query", payload)
        result = json.loads(rendered)
        self.assertGreater(len(rendered), 90_000)
        self.assertEqual("success", result["status"])
        self.assertEqual(["q0", "q1", "q2"], [item["request_id"] for item in result["results"]])
        self.assertNotIn("omitted_result_count", result)
        self.assertNotIn("error", result)

    def test_large_batch_reaches_hermes_host_spillover_intact(self):
        host_storage = importlib.import_module("tools.tool_result_storage")
        rendered = wire.enforce_tool_result_budget(
            "datasage_query",
            {
                "status": "success",
                "results": [
                    {"request_id": f"q{index}", "value": "x" * 40_000}
                    for index in range(3)
                ],
                "evidence_bundle": {"coverage": {"request_count": 3}},
            },
        )

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                host_storage,
                "get_spillover_dir",
                return_value=Path(temporary) / "cache" / "spillover",
            ):
                persisted = host_storage.maybe_persist_tool_result(
                    rendered,
                    "datasage_query",
                    "datasage_large_batch",
                    env=None,
                )

            saved_path = host_storage.extract_persisted_path(persisted)
            self.assertIsNotNone(saved_path)
            self.assertEqual(rendered, Path(saved_path).read_text(encoding="utf-8"))
            self.assertIn("<persisted-output>", persisted)


if __name__ == "__main__":
    unittest.main()
