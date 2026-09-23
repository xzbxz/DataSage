"""R18: every domain method must carry a usable forward diagnostic path.

A symptom must be able to move to a verifiable explanation and a priority, not to a
list of metrics or a restatement of the limits.  Domain depth differs on purpose, so
the requirement is the shape: goal, candidate explanations, discriminating evidence,
materiality, candidate actions and stop conditions - each grounded in governed
metrics that really exist, with actions kept as advice.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "references"
)
REGISTER = PROFILE_ROOT / "tests" / "fixtures" / "metric_readiness_register.json"

# The seven governed domains plus the intentional cross-domain method.
DOMAIN_REFERENCES = (
    "delivery",
    "receipt",
    "receivable",
    "target",
    "inventory",
    "profit",
    "pattern-matching",
    "cross-domain",
)
DIAGNOSTIC_LABELS = (
    "**Goal**",
    "**Candidate explanations**",
    "**Discriminating evidence**",
    "**Materiality**",
    "**Candidate actions**",
    "**Stop when**",
)
# Backticked snake_case tokens inside a diagnostic path must be registered metrics.
METRIC_TOKEN = re.compile(r"`([a-z][a-z0-9_]{3,})`")


class DomainDiagnosticPathTests(unittest.TestCase):
    def _section(self, name: str) -> str:
        text = (REFERENCES / f"{name}-analysis.md").read_text(encoding="utf-8")
        self.assertIn("## Diagnostic path", text, f"{name} has no diagnostic path")
        return text.split("## Diagnostic path", 1)[1].split("\n## ", 1)[0]

    def test_every_domain_reference_offers_the_six_part_path(self) -> None:
        for name in DOMAIN_REFERENCES:
            section = self._section(name)
            for label in DIAGNOSTIC_LABELS:
                with self.subTest(reference=name, label=label):
                    self.assertIn(label, section)

    def test_diagnostic_paths_name_only_registered_metrics(self) -> None:
        codes = {
            row["metric"]
            for row in json.loads(REGISTER.read_text(encoding="utf-8"))["metrics"]
        }
        for name in DOMAIN_REFERENCES:
            for token in sorted(set(METRIC_TOKEN.findall(self._section(name)))):
                with self.subTest(reference=name, token=token):
                    self.assertIn(
                        token,
                        codes,
                        "a diagnostic path may only cite registered metrics",
                    )

    def test_actions_stay_advice_and_hand_high_impact_to_an_owner(self) -> None:
        for name in DOMAIN_REFERENCES:
            section = self._section(name)
            actions = section.split("**Candidate actions**", 1)[1].split("\n- ", 1)[0]
            with self.subTest(reference=name):
                self.assertIn("advice only", actions)
                self.assertTrue(
                    "approver" in actions or "owner" in actions,
                    "high-impact actions must name the accountable human",
                )

    def test_paths_keep_the_evidence_boundary_instead_of_promising_causes(self) -> None:
        for name in DOMAIN_REFERENCES:
            section = self._section(name)
            with self.subTest(reference=name):
                self.assertIn("Stop when", section)
                self.assertRegex(
                    section,
                    r"hypothes|not a|does not|not proof",
                    "the path must keep hypotheses and limits distinct from facts",
                )


if __name__ == "__main__":
    unittest.main()
