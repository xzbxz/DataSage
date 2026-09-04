"""Static contract checks for the delivery-specific L3 answer guidance.

These checks intentionally read only the Profile-owned guidance files. They do
not import the query plugin, call a model, access a database, or start Hermes.
"""

from __future__ import annotations

from pathlib import Path
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROFILE_ROOT / "skills" / "business-analytics" / "datasage"
SOUL = PROFILE_ROOT / "SOUL.md"
SKILL = SKILL_ROOT / "SKILL.md"
ANSWER_BOUNDARY = SKILL_ROOT / "references" / "answer-boundary.md"
QUERY_RULES = SKILL_ROOT / "references" / "query-rules.md"
DELIVERY_ANALYSIS = SKILL_ROOT / "references" / "delivery-analysis.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Normalize prose wrapping for stable static assertions."""
    return " ".join(text.lower().split())


class DeliveryL3AnswerBoundaryContractTests(unittest.TestCase):
    def test_delivery_reference_is_present_and_versioned(self) -> None:
        text = _read(DELIVERY_ANALYSIS)
        self.assertIn("Rule ID: `datasage.delivery-analysis/v1`", text)
        self.assertIn("## Semantic lock", text)
        self.assertIn("## Evidence chain", text)
        self.assertIn("## State and disclosure contract", text)
        self.assertIn("evidence contract, not a fixed planner recipe", _compact(text))

    def test_semantic_lock_covers_delivery_meanings(self) -> None:
        text = _compact(_read(DELIVERY_ANALYSIS))
        for term in (
            "gross",
            "net",
            "versus line",
            "warehouse",
            "business time",
            "external-customer",
            "return settlement",
            "pending",
            "current-master",
        ):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_evidence_chain_orders_l3_dependencies(self) -> None:
        text = _read(DELIVERY_ANALYSIS)
        stages = (
            "### 1. Baseline",
            "### 2. Question-relevant structure",
            "### 3. Falsifiable hypotheses",
            "### 4. Interpretation strength",
            "### 5. Recommendation binding",
        )
        positions = [text.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions))
        for term in (
            "compatible benchmark",
            "structural contribution",
            "discriminating evidence",
            "causal conclusion",
            "Owner role",
            "Trigger",
            "Risk",
            "Verification metric",
        ):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_typed_states_and_delivery_disclosures_are_explicit(self) -> None:
        text = _read(DELIVERY_ANALYSIS).lower()
        for term in (
            "unknown bucket",
            "truncated",
            "has_more",
            "return-settlement period",
            "pending",
            "current-master",
            "empty",
            "undefined",
            "incomplete",
            "timeout",
        ):
            with self.subTest(term=term):
                self.assertIn(term, text)
        self.assertIn("never use a later caveat to repair an earlier unsupported claim", text)

    def test_surface_routing_keeps_soul_short_and_detail_on_demand(self) -> None:
        soul = _read(SOUL)
        skill = _read(SKILL)
        query_rules = _read(QUERY_RULES)
        answer_boundary = _read(ANSWER_BOUNDARY)

        soul_compact = _compact(soul)
        self.assertIn("complex delivery analysis follows", soul_compact)
        for term in (
            "semantic lock",
            "compatible baseline/benchmark",
            "relevant structure",
            "falsifiable hypotheses",
            "owner role",
            "trigger",
            "risk",
            "verification metric",
            "related cuts or decomposition are not causal proof",
        ):
            with self.subTest(term=term):
                self.assertIn(term, soul_compact)
        # Detailed semantic fields remain in the on-demand reference, not the
        # directly injected identity prompt.
        for term in ("gross versus net", "return-settlement period", "unknown bucket"):
            with self.subTest(detail=term):
                self.assertNotIn(term, soul_compact)

        self.assertIn("datasage.delivery-analysis/v1", skill)
        self.assertIn("references/delivery-analysis.md", skill)
        self.assertIn("supplemental guidance", _compact(skill))
        self.assertIn("not a fixed planner recipe", _compact(skill))

        self.assertIn("## Delivery L3 request framing", query_rules)
        self.assertIn("datasage.delivery-analysis/v1", query_rules)
        for term in ("gross", "net", "order", "warehouse", "external-customer", "return-settlement"):
            with self.subTest(term=term):
                self.assertIn(term, query_rules.lower())

        self.assertIn("## Delivery L3 owner boundary", answer_boundary)
        self.assertIn("datasage.delivery-analysis/v1", answer_boundary)
        self.assertIn("owned solely", _compact(answer_boundary))
        self.assertNotIn("### 1. Semantic lock", answer_boundary)

    def test_delivery_detail_has_single_owner_and_soul_compact_chain(self) -> None:
        soul = _compact(_read(SOUL))
        answer_boundary = _compact(_read(ANSWER_BOUNDARY))
        delivery = _read(DELIVERY_ANALYSIS)

        self.assertEqual(1, delivery.count("Rule ID: `datasage.delivery-analysis/v1`"))
        self.assertIn("detailed delivery guidance is owned solely", answer_boundary)
        self.assertIn("[`datasage.delivery-analysis/v1`](delivery-analysis.md)", _read(ANSWER_BOUNDARY))
        for term in (
            "semantic lock",
            "compatible baseline/benchmark",
            "relevant structure",
            "falsifiable hypotheses",
            "owner role",
            "trigger",
            "risk",
            "verification metric",
        ):
            with self.subTest(term=term):
                self.assertIn(term, soul)
        for detail_heading in (
            "### 1. Baseline",
            "### 2. Question-relevant structure",
            "### 3. Falsifiable hypotheses",
            "### 4. Interpretation strength",
            "### 5. Recommendation binding",
        ):
            with self.subTest(detail_heading=detail_heading):
                self.assertIn(detail_heading, delivery)


if __name__ == "__main__":
    unittest.main()
