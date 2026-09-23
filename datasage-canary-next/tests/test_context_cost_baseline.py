"""R28 guards: measured numbers only, unmeasured work declared, gates untouched.

The baseline may not drift from the workspace, may not carry an estimated number in the
measured section, may not describe an unapplied optimisation as applied, and may not lower
a quality gate.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import context_cost

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = PROFILE_ROOT / "tests" / "fixtures" / "context_cost_baseline.json"
CANDIDATE_STATUS = "candidate_unapplied"
APPLIED_STATUS = "already_in_effect"


def _register() -> dict:
    return json.loads(REGISTER_PATH.read_text(encoding="utf-8"))


class ContextCostBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register = _register()

    def test_every_measured_number_declares_how_it_was_measured(self) -> None:
        measured = self.register["measured"]
        self.assertTrue(measured, "run tests/context_cost.py write to fill the baseline")
        for section, values in measured.items():
            with self.subTest(section=section):
                self.assertIsInstance(values, dict)
                if section == "skill_context":
                    self.assertGreater(values["total_bytes"], 0)
                    self.assertEqual(values["file_count"], len(values["files"]))
                    continue
                if section == "tool_surface":
                    self.assertGreater(values["provided_tool_count"], 0)
                    self.assertGreater(values["manifest_bytes"], 0)
                    self.assertIn("channel_tool_count", values)
                    continue
                if section == "payloads":
                    for label, entry in values.items():
                        with self.subTest(label=label):
                            self.assertEqual("measured", entry["method"])
                            self.assertTrue(entry["measured_from"])
                            self.assertGreater(entry["raw_bytes"], 0)
                            self.assertGreater(entry["wire_bytes"], 0)
                    continue
                self.assertEqual("measured", values["method"])
                self.assertTrue(values["measured_from"])

    def test_the_baseline_still_matches_the_workspace(self) -> None:
        measured = context_cost.measure()
        for section, values in measured.items():
            with self.subTest(section=section):
                self.assertEqual(
                    values,
                    self.register["measured"].get(section),
                    "re-run tests/context_cost.py write after a real change",
                )

    def test_unmeasured_work_names_a_reason_an_owner_and_a_procedure(self) -> None:
        unmeasured = self.register["unmeasured"]
        self.assertTrue(unmeasured, "the unmeasured list is the honest half of this record")
        for item in unmeasured:
            with self.subTest(item=item["id"]):
                self.assertEqual("unmeasured", item["status"])
                for field in ("item", "reason", "owner_role", "how_to_measure"):
                    self.assertTrue(str(item[field]).strip(), f"{field} must not be empty")
                blob = json.dumps(item, ensure_ascii=False)
                self.assertNotIn("达标", blob)
                self.assertNotIn("已实测", blob)

    def test_no_number_was_converted_from_bytes_into_tokens(self) -> None:
        text = json.dumps(self.register, ensure_ascii=False)
        self.assertIn(
            "must never be converted into token estimates", self.register["policy"]
        )
        self.assertNotRegex(text, r"token[s]?\s*[=:]\s*\d")
        self.assertNotIn("estimated_tokens", text)

    def test_degradation_points_are_triggered_owned_and_honestly_labelled(self) -> None:
        for point in self.register["degradation_points"]:
            with self.subTest(point=point["id"]):
                self.assertIn(point["status"], (APPLIED_STATUS, CANDIDATE_STATUS))
                self.assertTrue(point["trigger"].strip())
                self.assertTrue(point["owner_role"].strip())
                if point["status"] == APPLIED_STATUS:
                    self.assertIs(True, point["applied"])
                    evidence = point["evidence"]
                    if evidence.startswith("measured."):
                        node = self.register["measured"]
                        for part in evidence.split(".")[1:]:
                            self.assertIn(part, node)
                            node = node[part]
                    else:
                        self.assertTrue(
                            (PROFILE_ROOT / evidence).exists(),
                            f"{evidence} must exist",
                        )
                else:
                    self.assertIs(False, point["applied"])
                    self.assertTrue(
                        str(point["evidence_required"]).strip(),
                        "an unapplied change needs the evidence that would justify it",
                    )

    def test_no_quality_gate_was_lowered_for_cost(self) -> None:
        gates = self.register["quality_gates"]
        self.assertEqual(3, len(gates))
        for gate in gates:
            with self.subTest(gate=gate["id"]):
                self.assertEqual("unchanged", gate["status"])
                self.assertTrue((PROFILE_ROOT / gate["reference"]).exists())
        self.assertIn("No cost change may relax a quality gate", self.register["policy"])


if __name__ == "__main__":
    unittest.main()
