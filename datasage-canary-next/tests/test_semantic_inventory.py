"""Inventory gates for semantic single-sourcing and pending capability lifecycle."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = PROFILE_ROOT / "plugins" / "datasage-query" / "contracts"
class SemanticSingleSourceTests(unittest.TestCase):
    def test_answer_notes_alias_identical_disclosure_text_without_value_drift(self):
        pair_count = 0
        anchor_names: set[str] = set()
        alias_names: set[str] = set()
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            anchor_names.update(re.findall(r"&(?P<name>answer_note_[A-Za-z0-9_]+)", raw))
            alias_names.update(re.findall(r"\*(?P<name>answer_note_[A-Za-z0-9_]+)", raw))
            for metric in parsed.get("metrics", {}).values():
                answer_note = metric.get("answer_note")
                if answer_note is None:
                    continue
                for disclosure in metric.get("disclosures", []):
                    if not isinstance(disclosure, dict):
                        continue
                    if disclosure.get("text") == answer_note:
                        pair_count += 1
                        self.assertIs(disclosure["text"], answer_note)

        self.assertEqual(65, pair_count)
        self.assertEqual(65, len(anchor_names))
        self.assertEqual(anchor_names, alias_names)

    def test_machine_contracts_exclude_unconsumed_root_defaults(self):
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            with self.subTest(path=path.name):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertNotIn("defaults", parsed)
                self.assertNotIn("analysis_defaults", parsed)
        datasets = yaml.safe_load(
            (CONTRACT_ROOT / "datasets.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual({"blocked_columns"}, set(datasets.get("defaults", {})))

    def test_value_contract_templates_are_consumed_by_yaml_aliases(self):
        consumed_templates = 0
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            templates = parsed.get("value_contract_templates", {})
            dimensions = parsed.get("dimensions", {})
            for template in templates.values():
                references = [
                    definition
                    for definition in dimensions.values()
                    if isinstance(definition, dict)
                    and definition.get("value_contract") is template
                ]
                self.assertTrue(references, f"unused value contract template in {path.name}")
                consumed_templates += 1
        self.assertGreater(consumed_templates, 0)


class PendingCapabilityLifecycleTests(unittest.TestCase):
    def test_every_pending_metric_has_an_owner_and_blocked_activation_gate(self):
        pending: list[tuple[str, str, dict]] = []
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            for metric_code, metric in parsed.get("metrics", {}).items():
                availability = metric.get("availability")
                if (
                    isinstance(availability, dict)
                    and availability.get("status") == "pending_validation"
                ):
                    pending.append((path.name, metric_code, availability))

        self.assertEqual(13, len(pending))
        for path_name, metric_code, availability in pending:
            with self.subTest(path=path_name, metric=metric_code):
                self.assertTrue(str(availability.get("owner", "")).strip())
                gate = availability.get("activation_gate")
                self.assertIsInstance(gate, dict)
                self.assertEqual("blocked", gate.get("state"))
                self.assertTrue(gate.get("required_evidence"))
                self.assertEqual(
                    "owner_and_release_review",
                    gate.get("activation_authority"),
                )
                review = availability.get("review")
                self.assertIsInstance(review, dict)
                self.assertTrue(review.get("cadence"))
                self.assertTrue(review.get("triggers"))
                lifecycle = availability.get("lifecycle")
                self.assertIsInstance(lifecycle, dict)
                self.assertEqual("pending_validation", lifecycle.get("state"))
                self.assertEqual(
                    "remains_unavailable_until_gate_passes",
                    lifecycle.get("activation_effect"),
                )


if __name__ == "__main__":
    unittest.main()
