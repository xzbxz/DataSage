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
    def test_release_identity_and_eager_small_toolset_are_content_consistent(self):
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        plugin = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        skill = _frontmatter(SKILL_PATH)
        self.assertEqual(str(distribution["version"]), str(plugin["version"]))
        self.assertEqual(str(distribution["version"]), str(skill["version"]))
        self.assertRegex(config["model"]["default"], r"^[a-z0-9][a-z0-9._-]+$")
        self.assertEqual("off", config["tools"]["tool_search"]["enabled"])
        self.assertNotIn("disabled", config["skills"])
        self.assertTrue((PROFILE_ROOT / ".no-bundled-skills").is_file())
        self.assertFalse(
            (PROFILE_ROOT / "skills" / "datasage" / "datasage-query-patterns").exists()
        )
        settings = config["plugins"]["entries"]["datasage-query"]["settings"]
        self.assertNotIn("max_result_bytes", settings)
        self.assertNotIn("max_batch_bytes", settings)
        self.assertGreater(settings["max_tool_result_chars"], 0)

    def test_plugin_registers_tools_without_answer_state_hooks(self):
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
        self.assertEqual(
            ["datasage.evidence-boundaries"],
            [entry["id"] for entry in registration.prompt_sections],
        )

    def test_datasage_skill_is_compact_native_and_tool_gated(self):
        path = SKILL_PATH
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

    def test_references_use_the_native_skill_surface(self):
        linked = {
            path.name
            for path in (SKILL_PATH.parent / "references").iterdir()
            if path.is_file()
        }
        self.assertEqual(
            {
                "answer-boundary.md",
                "entity-guidance.md",
                "planning-semantics.yaml",
                "query-rules.md",
            },
            linked,
        )
        skill = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn('skill_view(name="datasage", file_path=', skill)
        self.assertFalse(hasattr(schemas, "DATASAGE_REFERENCE"))

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
        with mock.patch.object(wire, "tool_result_char_limit", return_value=900):
            result = json.loads(wire.enforce_tool_result_budget("datasage_query", payload))
        self.assertEqual("partial", result["status"])
        self.assertGreater(len(result["results"]), 0)
        self.assertGreater(result["omitted_result_count"], 0)
        self.assertTrue(result["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
