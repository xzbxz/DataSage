from __future__ import annotations

import json
from pathlib import Path
import unittest

import yaml

from plugin_registration_probe import probe_registration


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROFILE_ROOT / "skills" / "business-analytics" / "datasage"
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"

class ExpertAuthorityInventoryTests(unittest.TestCase):
    def test_model_reference_owners_consumers_and_lifecycle_are_inventoried(self):
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        expected = {
            "datasage.query-rules/v1": "references/query-rules.md",
            "datasage.entity-guidance/v1": "references/entity-guidance.md",
            "datasage.planning-semantics/v1": "references/planning-semantics.yaml",
            "datasage.answer-boundary/v1": "references/answer-boundary.md",
        }
        for rule_id, relative_path in expected.items():
            content = (SKILL_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(rule_id, content)
            self.assertIn(rule_id, architecture)
        self.assertIn("Owner", architecture)
        self.assertIn("Consumer", architecture)
        self.assertIn("生命周期", architecture)

    def test_planning_reference_is_method_only_and_schema_owns_role_enum(self):
        planning = yaml.safe_load(
            (SKILL_ROOT / "references" / "planning-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        authority = planning["authority"]
        self.assertEqual("datasage.planning-semantics/v1", authority["id"])
        self.assertFalse(authority["runtime_authority"])
        self.assertFalse(authority["metric_authority"])
        self.assertEqual(
            "live_datasage_query_schema", authority["evidence_role_enum_source"]
        )
        self.assertTrue(authority["consumers"])

    def test_maintainer_rationale_cannot_become_model_or_runtime_authority(self):
        maintainer = (
            PLUGIN_ROOT / "contracts" / "entity-rules-maintainer.md"
        ).read_text(encoding="utf-8")
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        rule_id = "datasage.entity-maintainer-rationale/v1"
        self.assertIn(rule_id, maintainer)
        self.assertIn(rule_id, architecture)
        self.assertIn("Consumers:", maintainer)
        self.assertIn("non-model, non-runtime", maintainer)
        self.assertNotIn("entity-rules-maintainer", skill)

    def test_plugin_prompt_has_canonical_rule_anchor_with_existing_safety(self):
        module, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_expert_authority_registration",
        )
        self.assertEqual(1, len(registration.prompt_sections))
        section = registration.prompt_sections[0]
        prompt = section["content"]
        max_chars = section["max_chars"]
        self.assertEqual(module.DATASAGE_EVIDENCE_BOUNDARIES, prompt)
        self.assertLessEqual(len(prompt), max_chars)
        self.assertIn("datasage.answer-boundary/v1", prompt)
        self.assertIn("references/answer-boundary.md", prompt)
        for phrase in (
            "scope compatibility",
            "governed benchmark",
            "arithmetic relationships",
            "Do not infer profitability",
        ):
            self.assertIn(phrase, prompt)

    def test_removed_companion_has_no_active_usage_or_prompt_entry(self):
        usage = json.loads((PROFILE_ROOT / "skills" / ".usage.json").read_text(encoding="utf-8"))
        snapshot = json.loads(
            (PROFILE_ROOT / ".skills_prompt_snapshot.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("datasage-query-patterns", usage)
        self.assertEqual("active", usage["datasage"]["state"])
        self.assertEqual(["datasage"], [item["skill_name"] for item in snapshot["skills"]])

    def test_bundled_skill_marker_remains_until_native_allowlist_exists(self):
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertTrue((PROFILE_ROOT / ".no-bundled-skills").is_file())
        self.assertIn("skills.disabled", architecture)
        self.assertIn("keep_skills", architecture)
        self.assertIn("持久 allowlist", architecture)
        self.assertIn("TODO", architecture)


if __name__ == "__main__":
    unittest.main()
