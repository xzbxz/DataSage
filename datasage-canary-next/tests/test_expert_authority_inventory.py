from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
    "docx",
    "ocr-and-documents",
    "pdf",
    "powerpoint",
    "xlsx",
}
DATASAGE_REFERENCE_OWNERS = {
    "datasage.query-rules/v1": "references/query-rules.md",
    "datasage.entity-guidance/v1": "references/entity-guidance.md",
    "datasage.answer-boundary/v1": "references/answer-boundary.md",
}
DATASAGE_ARCHITECTURE_ROWS = {
    "datasage.query-rules/v1": (
        "Skill 请求规则",
        "Hermes 按需加载、库存测试",
        "请求构造方法；live schema/catalog 拥有可用字段和值",
    ),
    "datasage.entity-guidance/v1": (
        "Skill 实体指导",
        "Hermes 按需加载、库存测试",
        "模型安全投影；不能创建或覆盖实体映射",
    ),
    "datasage.answer-boundary/v1": (
        "Skill 最终回答策略",
        "Hermes 按需加载、库存测试",
        "详细回答边界；插件不注册 prompt 副本",
    ),
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
        for rule_id, relative_path in DATASAGE_REFERENCE_OWNERS.items():
            content = (SKILL_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(rule_id, content)
            row = re.search(
                rf"^\| `{re.escape(rule_id)}` \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$",
                architecture,
                re.MULTILINE,
            )
            self.assertIsNotNone(row, f"missing architecture row for {rule_id}")
            self.assertEqual(
                DATASAGE_ARCHITECTURE_ROWS[rule_id],
                tuple(cell.strip() for cell in row.groups()),
            )

        discovered_references = {
            f"references/{path.name}"
            for path in (SKILL_ROOT / "references").glob("*.md")
        }
        self.assertEqual(
            set(DATASAGE_REFERENCE_OWNERS.values()),
            discovered_references,
            "every model-visible reference must be registered to one Rule ID owner",
        )

    def test_rule_definitions_are_unique_and_rule_links_reach_their_owner(self):
        documents = [SKILL_ROOT / "SKILL.md"] + sorted(
            (SKILL_ROOT / "references").glob("*.md")
        )
        definitions: dict[str, list[Path]] = {}
        for path in documents:
            text = path.read_text(encoding="utf-8")
            for rule_id in re.findall(r"^Rule ID: `([^`]+)`$", text, re.MULTILINE):
                definitions.setdefault(rule_id, []).append(path)

        self.assertEqual(set(DATASAGE_REFERENCE_OWNERS), set(definitions))
        for rule_id, owners in definitions.items():
            with self.subTest(rule_id=rule_id):
                self.assertEqual(
                    [SKILL_ROOT / DATASAGE_REFERENCE_OWNERS[rule_id]],
                    owners,
                )

        linked_edges: set[tuple[str, str]] = set()
        link_pattern = re.compile(r"\[`(datasage\.[^`]+/v\d+)`\]\(([^)#]+\.md)\)")
        for source in documents:
            text = source.read_text(encoding="utf-8")
            links = link_pattern.findall(text)
            linked_rule_ids = [rule_id for rule_id, _link in links]
            defined_rule_ids = re.findall(
                r"^Rule ID: `([^`]+)`$", text, re.MULTILINE
            )
            mentioned_rule_ids = re.findall(r"`(datasage\.[^`]+/v\d+)`", text)
            self.assertEqual(
                sorted(defined_rule_ids + linked_rule_ids),
                sorted(mentioned_rule_ids),
                f"bare or malformed rule reference in {source}",
            )
            for rule_id, link in links:
                target = (source.parent / link).resolve()
                with self.subTest(source=source.name, rule_id=rule_id):
                    self.assertTrue(target.is_file(), f"missing rule target: {target}")
                    target_text = target.read_text(encoding="utf-8")
                    self.assertIn(f"Rule ID: `{rule_id}`", target_text)
                linked_edges.add((source.name, rule_id))

        self.assertTrue(
            {
                ("SKILL.md", "datasage.query-rules/v1"),
                ("SKILL.md", "datasage.answer-boundary/v1"),
                ("SKILL.md", "datasage.entity-guidance/v1"),
                ("query-rules.md", "datasage.answer-boundary/v1"),
                ("query-rules.md", "datasage.entity-guidance/v1"),
            }.issubset(linked_edges)
        )

    def test_skill_routes_complex_quantitative_work_to_answer_owner(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        governing = skill.split("## Governing references", 1)[1].split(
            "## Workflow", 1
        )[0]
        answer_owner = governing.split("datasage.answer-boundary/v1", 1)[1]
        self.assertIn("Load it before", answer_owner)
        for trigger in (
            "comparison",
            "ranking",
            "target",
            "decomposition",
            "causal",
            "multi-row calculation",
            "truncated",
            "partial",
            "failed",
        ):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, answer_owner)

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

        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("插件不注册 prompt 副本", architecture)
        self.assertNotIn("插件安全锚点", architecture)

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

    def test_runtime_contract_and_repository_have_no_removed_authority_sediment(self):
        datasets = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "datasets.yaml").read_text(
                encoding="utf-8"
            )
        )["datasets"]
        removed_dataset_sections = {
            "allocation_contract",
            "candidate_key_sets",
            "data_quality",
            "dataset_query_policies",
            "derived_fields",
            "identity_mappings",
        }
        for dataset_name, definition in datasets.items():
            with self.subTest(dataset=dataset_name):
                self.assertTrue(
                    removed_dataset_sections.isdisjoint(definition),
                    removed_dataset_sections.intersection(definition),
                )

        tools_text = (PLUGIN_ROOT / "tools.py").read_text(encoding="utf-8")
        self.assertNotIn("_EVIDENCE_INTERPRETATION", tools_text)

        repository_ignore = (PROFILE_ROOT.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("planning-semantics.yaml", repository_ignore)
        self.assertNotIn(".no-bundled-skills", repository_ignore)

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
            related = set(
                inventory[name]
                .get("metadata", {})
                .get("hermes", {})
                .get("related_skills", [])
            )
            bundled_related = related.intersection(inventory)
            self.assertTrue(
                bundled_related.issubset(REVIEWED_NATIVE_SKILLS),
                f"enabled bundled Skill {name} points to disabled companions: "
                f"{sorted(bundled_related - REVIEWED_NATIVE_SKILLS)}",
            )
        self.assertIn("skills.disabled", architecture)
        self.assertIn("related_skills", architecture)
        self.assertIn("快照差异审查", architecture)
        self.assertIn("新增、删除或重命名", architecture)


if __name__ == "__main__":
    unittest.main()
