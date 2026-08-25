from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
PACKAGE = "datasage_expert_architecture_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PACKAGE] = package

contracts = importlib.import_module(f"{PACKAGE}.contracts")
references = importlib.import_module(f"{PACKAGE}.references")
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
    def test_release_identity_and_eager_small_toolset_are_content_consistent(self):
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        plugin = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        skill = _frontmatter(
            PROFILE_ROOT / "skills" / "datasage" / "datasage" / "SKILL.md"
        )
        self.assertEqual(str(distribution["version"]), str(plugin["version"]))
        self.assertEqual(str(distribution["version"]), str(skill["version"]))
        self.assertRegex(config["model"]["default"], r"^[a-z0-9][a-z0-9._-]+$")
        self.assertEqual("off", config["tools"]["tool_search"]["enabled"])
        disabled = set(config["skills"]["disabled"])
        self.assertNotIn("datasage", disabled)
        self.assertNotIn("datasage-query-patterns", disabled)
        self.assertFalse(
            (PROFILE_ROOT / "skills" / "datasage" / "datasage-query-patterns").exists()
        )
        settings = config["plugins"]["entries"]["datasage-query"]["settings"]
        self.assertNotIn("max_result_bytes", settings)
        self.assertNotIn("max_batch_bytes", settings)
        self.assertGreater(settings["max_tool_result_chars"], 0)

    def test_plugin_registers_tools_without_answer_state_hooks(self):
        source = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        manifest = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(4, source.count("ctx.register_tool("))
        self.assertEqual(
            [
                "datasage_catalog",
                "datasage_entity_resolve",
                "datasage_reference",
                "datasage_query",
            ],
            manifest["provides_tools"],
        )
        self.assertNotIn("provides_hooks", manifest)
        self.assertNotIn("ctx.register_hook(", source)
        self.assertNotIn("answer_guard", source)
        self.assertNotIn("transform_llm_output", source)
        self.assertNotIn("post_tool_call", source)
        self.assertNotIn("pre_llm_call", source)
        self.assertNotIn("frozen_wecom_skill_hook", source)

    def test_datasage_skill_is_compact_native_and_tool_gated(self):
        path = PROFILE_ROOT / "skills" / "datasage" / "datasage" / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        metadata = _frontmatter(path)
        hermes = metadata["metadata"]["hermes"]
        self.assertLess(len(content), 6_000)
        self.assertEqual(["datasage-query"], hermes["requires_toolsets"])
        self.assertEqual(
            ["datasage_catalog", "datasage_query"],
            hermes["requires_tools"],
        )

    def test_analysis_intent_is_reachable_from_public_schema(self):
        intent = schemas.REQUEST["properties"]["analysis_intent"]
        self.assertEqual(set(tools.evidence.ANALYSIS_INTENTS), set(intent["enum"]))

    def test_reference_schema_couples_each_source_to_its_sections(self):
        variants = schemas.DATASAGE_REFERENCE["parameters"]["properties"]["requests"]["items"]["oneOf"]
        self.assertEqual(set(references.SOURCE_IDS), {
            variant["properties"]["source_id"]["const"] for variant in variants
        })
        for variant in variants:
            source_id = variant["properties"]["source_id"]["const"]
            self.assertEqual(
                set(references.SECTION_IDS_BY_SOURCE[source_id]),
                set(variant["properties"]["section_id"]["enum"]),
            )

    def test_batch_metric_details_return_independent_receipts(self):
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
        receipts = [result["detail_receipt"] for result in payload["results"]]
        self.assertEqual(2, len(set(receipts)))
        for (domain, metric), receipt in zip(selections, receipts):
            self.assertIn(receipt, tools._current_metric_detail_receipts(domain, metric))

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

    def test_oversized_batch_returns_partial_prefix(self):
        payload = {
            "status": "success",
            "results": [
                {"request_id": f"q{index}", "value": "x" * 240}
                for index in range(3)
            ],
            "evidence_bundle": {"large": "y" * 300},
        }
        with mock.patch.object(wire, "tool_result_char_limit", return_value=750):
            result = json.loads(wire.enforce_tool_result_budget("datasage_query", payload))
        self.assertEqual("partial", result["status"])
        self.assertGreater(len(result["results"]), 0)
        self.assertGreater(result["omitted_result_count"], 0)
        self.assertTrue(result["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
