from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml

from plugin_registration_probe import probe_registration
from agent import skill_utils as hermes_skill_utils
from tools import skills_sync as hermes_skills_sync


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROFILE_ROOT / "skills" / "business-analytics" / "datasage"
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
REVIEWED_NATIVE_SKILLS = {
    "document-to-action-items",
    "docx",
    "grounded-citations",
    "meeting-action-items",
    "ocr-and-documents",
    "pdf",
    "powerpoint",
    "weekly-review-planning",
    "xlsx",
}


def _bundled_skill_inventory() -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    # Exercise the same source-directory resolution used by sync_skills(),
    # including an explicit HERMES_BUNDLED_SKILLS override when present.
    for path in hermes_skills_sync._get_bundled_dir().rglob("SKILL.md"):
        text = path.read_text(encoding="utf-8")
        _, raw, _ = text.split("---", 2)
        frontmatter = yaml.safe_load(raw)
        name = frontmatter["name"]
        if name in inventory:
            raise AssertionError(f"duplicate bundled Skill name: {name}")
        inventory[name] = frontmatter
    return inventory

class ExpertAuthorityInventoryTests(unittest.TestCase):
    def test_model_reference_owners_consumers_and_lifecycle_are_inventoried(self):
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        expected = {
            "datasage.query-rules/v1": "references/query-rules.md",
            "datasage.entity-guidance/v1": "references/entity-guidance.md",
            "datasage.answer-boundary/v1": "references/answer-boundary.md",
        }
        for rule_id, relative_path in expected.items():
            content = (SKILL_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(rule_id, content)
            self.assertIn(rule_id, architecture)
        self.assertIn("Owner", architecture)
        self.assertIn("Consumer", architecture)
        self.assertIn("生命周期", architecture)

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

    def test_plugin_uses_skill_references_without_a_resident_prompt_copy(self):
        _module, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_expert_authority_registration",
        )
        self.assertEqual([], registration.prompt_sections)

        answer_policy = (SKILL_ROOT / "references" / "answer-boundary.md").read_text(
            encoding="utf-8"
        )
        for detailed_policy in (
            "compatible governed target or benchmark",
            "Absolute receivable or overdue proximity",
            "requested_limit",
            "partial batch",
        ):
            self.assertIn(detailed_policy, answer_policy)

    def test_removed_companion_has_no_usage_or_snapshot_inventory_entry(self):
        usage = json.loads((PROFILE_ROOT / "skills" / ".usage.json").read_text(encoding="utf-8"))
        self.assertNotIn("datasage-query-patterns", usage)
        self.assertEqual("active", usage["datasage"]["state"])
        snapshot_path = PROFILE_ROOT / ".skills_prompt_snapshot.json"
        if snapshot_path.exists():
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot_names = {item["skill_name"] for item in snapshot["skills"]}
            self.assertIn("datasage", snapshot_names)
            self.assertNotIn("datasage-query-patterns", snapshot_names)

    def test_pinned_host_inventory_matches_reviewed_native_skill_denylist(self):
        config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        inventory = _bundled_skill_inventory()
        disabled = set(config["skills"]["disabled"])

        self.assertEqual("==0.20.5", distribution["hermes_requires"])
        self.assertFalse((PROFILE_ROOT / ".no-bundled-skills").exists())
        self.assertEqual(REVIEWED_NATIVE_SKILLS, set(inventory) - disabled)
        self.assertEqual(set(inventory) - REVIEWED_NATIVE_SKILLS, disabled)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(PROFILE_ROOT)}):
            hermes_skill_utils._raw_config_cache_clear()
            try:
                self.assertEqual(
                    disabled,
                    hermes_skill_utils.get_disabled_skill_names(platform="wecom"),
                )
            finally:
                hermes_skill_utils._raw_config_cache_clear()
        for name in REVIEWED_NATIVE_SKILLS:
            self.assertIn("windows", inventory[name]["platforms"])
        self.assertIn("skills.disabled", architecture)
        self.assertIn("快照差异审查", architecture)
        self.assertIn("新增、删除或重命名", architecture)


if __name__ == "__main__":
    unittest.main()
