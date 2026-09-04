"""Text-level regression checks for target-answer evidence boundaries.

These tests intentionally stay at the Profile prompt/reference boundary. They
do not import the DataSage query engine, call a model, touch a database, or
send a WeCom message.
"""

from __future__ import annotations

from pathlib import Path
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SOUL_PATH = PROFILE_ROOT / "SOUL.md"
REFERENCE_ROOT = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "references"
)
ANSWER_BOUNDARY_PATH = REFERENCE_ROOT / "answer-boundary.md"
QUERY_RULES_PATH = REFERENCE_ROOT / "query-rules.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TargetAnswerBoundaryTests(unittest.TestCase):
    def test_soul_exposes_wecom_critical_target_order(self):
        soul = _read(SOUL_PATH)
        self.assertIn("## Target answer boundary (WeCom-critical)", soul)
        baseline = soul.index("First obtain and report the four compatible target facts")
        breakdown = soul.index("Only after that baseline succeeds may the answer show")
        contribution = soul.index("A structural contribution is allowed only when")
        causal = soul.index("A complete decomposition requires an operation")
        evidence_gate = soul.index("Successful and applicable customer, order, receivable")
        self.assertLess(baseline, breakdown)
        self.assertLess(breakdown, contribution)
        self.assertLess(contribution, causal)
        self.assertLess(causal, evidence_gate)
        for marker in ("target", "net registered actual", "gap", "completion rate"):
            self.assertIn(marker, soul)
        self.assertIn("钱没收回", soul)
        self.assertIn("下单节奏", soul)

    def test_answer_reference_distinguishes_breakdown_contribution_and_causality(self):
        answer = _read(ANSWER_BOUNDARY_PATH)
        section = answer[answer.index("## Target completion and") :]
        for heading in (
            "## Target completion and “why not complete”",
            "**Ordinary customer breakdown**",
            "**Structural contribution**",
            "**Complete or causal decomposition**",
        ):
            self.assertIn(heading, answer)
        self.assertIn("report, in this order: target, net registered", section)
        self.assertIn("actual, gap, and completion rate", section)
        baseline = section.index("report, in this order: target")
        breakdown = section.index("**Ordinary customer breakdown**")
        contribution = section.index("**Structural contribution**")
        decomposition = section.index("**Complete or causal decomposition**")
        self.assertLess(baseline, breakdown)
        self.assertLess(breakdown, contribution)
        self.assertLess(contribution, decomposition)
        self.assertIn("catalog-advertised dimension is not evidence", answer)
        self.assertIn("causal evidence is insufficient", answer)
        self.assertIn("钱没收回", answer)
        self.assertIn("下单节奏", answer)

    def test_query_rules_enforce_target_first_and_fail_closed_drilldown(self):
        rules = _read(QUERY_RULES_PATH)
        self.assertIn("## Target question planning", rules)
        first = rules.index("1. Query the exact target completion metric")
        second = rules.index("2. If customer detail is requested")
        third = rules.index("3. Treat ordinary customer breakdown")
        failure = rules.index("5. If driver evidence is failed")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertLess(third, failure)
        for marker in (
            "The first answer must report target, net",
            "registered actual, gap, and completion rate",
            "do not fill it from",
            "a related actual metric or another ledger",
            "catalog",
            "metadata alone does not authorize a customer drill",
            "Do not silently change",
            "UNSUPPORTED_TARGET_GAP_DECOMPOSITION",
            "钱没收回",
            "下单节奏",
        ):
            self.assertIn(marker, rules)

    def test_forbidden_cause_phrases_are_not_verified_claims(self):
        for path in (SOUL_PATH, ANSWER_BOUNDARY_PATH, QUERY_RULES_PATH):
            text = _read(path)
            with self.subTest(path=path.name):
                self.assertIn("钱没收回", text)
                self.assertIn("下单节奏", text)
                self.assertRegex(text, r"(?s)(钱没收回.*?(?:hypoth|假设|verified cause|证据))")
                self.assertRegex(text, r"(?s)(下单节奏.*?(?:hypoth|假设|verified cause|证据))")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
