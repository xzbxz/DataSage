"""Inventory gates for semantic single-sourcing and pending capability lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = PROFILE_ROOT / "plugins" / "datasage-query" / "contracts"
PLANNING_REFERENCE = (
    PROFILE_ROOT
    / "skills"
    / "business-analytics"
    / "datasage"
    / "references"
    / "planning-semantics.yaml"
)

BASELINE_PARSED_SHA256 = {
    "customer_risk-semantics.yaml": "1641c882ddb935b5a5f1a1d17d243c7c1327109a75292d43e3512816d4cd8ba5",
    "delivery-semantics.yaml": "27a7e02163fa6da79d8b039d0ae3cfe3182e4aa874a05f08fecdf38586276b52",
    "inventory-semantics.yaml": "b17b52899dbd179c8994ce370d30d64468a06ab59129dc01894fb35b78cb05cb",
    "receipt-semantics.yaml": "b5fda985febb1a510c9a270475534f0270d02080e7fbed32ab81f0e5934d2268",
    "receivable-semantics.yaml": "1b7686a7dbb5d5efe0a0d24bf6a79a0e38efd062d8366b3541e587a8ea928a34",
    "target-semantics.yaml": "2d2e720601cc4e08ff85ed88b2ca06729f06a3fd821e85b11f8a06fffa1a7fc6",
}
LIFECYCLE_KEYS = {"owner", "activation_gate", "review", "lifecycle"}


def _canonical_digest(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _without_pending_lifecycle(value: object) -> object:
    if isinstance(value, dict):
        pending = value.get("status") == "pending_validation"
        return {
            key: _without_pending_lifecycle(item)
            for key, item in value.items()
            if not (pending and key in LIFECYCLE_KEYS)
        }
    if isinstance(value, list):
        return [_without_pending_lifecycle(item) for item in value]
    return value


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

    def test_anchor_refactor_preserves_the_prechange_parsed_contract(self):
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            with self.subTest(path=path.name):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                normalized = _without_pending_lifecycle(parsed)
                self.assertEqual(
                    BASELINE_PARSED_SHA256[path.name],
                    _canonical_digest(normalized),
                )


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

    def test_planning_reference_projects_live_roles_without_a_local_enum(self):
        reference = yaml.safe_load(PLANNING_REFERENCE.read_text(encoding="utf-8"))
        self.assertNotIn("evidence_roles", reference)
        projection = reference["live_schema_projection"]
        self.assertEqual("datasage_query", projection["source"])
        self.assertEqual("evidence_role", projection["field"])
        self.assertEqual("forbidden", projection["local_enum"])
        self.assertNotIn("values", projection)
        self.assertNotIn("allowed_values", projection)
        self.assertTrue(projection["lifecycle"]["owner"])
        self.assertTrue(projection["lifecycle"]["refresh_trigger"])


if __name__ == "__main__":
    unittest.main()
