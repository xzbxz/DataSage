"""R31 guards: a grey-release plan that waits for its owners and a rehearsed rollback.

The plan may not invent an endorsement, may not start a stage before its dependency is done,
may not contradict the release matrix, and may not claim a rollback that was never rehearsed.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

PROFILE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROFILE_ROOT / "tests" / "fixtures"
PLAN_PATH = FIXTURES / "gray_release_plan.json"
MATRIX_PATH = FIXTURES / "release_matrix.json"
STAGE_STATUSES = ("not_started", "in_progress", "completed", "stopped")
BLOCKING_BLOCKS = ("unverified", "blocked_external")


def _plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


class GrayReleasePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = _plan()
        self.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    def test_every_stage_has_criteria_a_receipt_slot_and_an_owner(self) -> None:
        stages = self.plan["stages"]
        self.assertGreaterEqual(len(stages), 4)
        orders = [stage["order"] for stage in stages]
        self.assertEqual(sorted(orders), list(range(1, len(stages) + 1)))
        for stage in stages:
            with self.subTest(stage=stage["id"]):
                self.assertIn(stage["status"], STAGE_STATUSES)
                self.assertTrue(stage["expansion_criteria"].strip())
                self.assertTrue(stage["stop_criteria"].strip())
                self.assertTrue(stage["receipt_required"].strip())
                self.assertTrue(stage["owner_role"].strip())
                self.assertIsNone(stage.get("receipt"))

    def test_no_stage_starts_before_its_dependency(self) -> None:
        by_id = {stage["id"]: stage for stage in self.plan["stages"]}
        for stage in self.plan["stages"]:
            for dependency in stage["depends_on"]:
                with self.subTest(stage=stage["id"], dependency=dependency):
                    self.assertIn(dependency, by_id)
                    self.assertLess(by_id[dependency]["order"], stage["order"])

    def test_the_plan_agrees_with_the_release_matrix(self) -> None:
        blocking = {
            surface
            for surface, row in self.matrix["capabilities"].items()
            if row["status"] in BLOCKING_BLOCKS
        }
        self.assertTrue(blocking, "the matrix must still name what blocks a release")
        declared = set(self.plan["blocking_preconditions"]["surfaces"])
        self.assertEqual(
            blocking,
            declared,
            "every unverified or externally blocked capability must be a precondition",
        )
        final = self.plan["stages"][-1]
        self.assertIn("blocking_preconditions", final["expansion_criteria"] + final["stop_criteria"])

    def test_external_preconditions_are_named_not_assumed(self) -> None:
        preconditions = self.plan["blocking_preconditions"]
        self.assertTrue(preconditions["items"])
        for item in preconditions["items"]:
            with self.subTest(item=item["id"]):
                self.assertTrue(item["item"].strip())
                self.assertTrue(item["source"].strip())
                self.assertEqual("pending", item["status"])
        self.assertIn("R24", preconditions["source_note"])

    def test_the_assistant_never_endorses_a_release(self) -> None:
        endorsement = self.plan["release_endorsement"]
        self.assertIsNone(endorsement["signed_by"])
        self.assertIsNone(endorsement["signed_on"])
        self.assertIn("不代签", endorsement["note"])
        for stage in self.plan["stages"]:
            with self.subTest(stage=stage["id"]):
                self.assertIsNone(stage["receipt"])
                self.assertEqual("not_started", stage["status"])

    def test_the_rollback_was_rehearsed_and_can_be_reproduced(self) -> None:
        rollback = self.plan["rollback"]
        self.assertEqual("measured", rollback["method"])
        self.assertTrue(rollback["steps"])
        for step in rollback["steps"]:
            with self.subTest(step=step["id"]):
                self.assertTrue(step["action"].strip())
                self.assertTrue(step["verification"].strip())
                self.assertTrue(step["verification_command"].strip())
        rehearsal = rollback["rehearsal"]
        self.assertTrue(rehearsal["clone_note"])
        self.assertEqual(rehearsal["head_before"], rehearsal["head_after"])
        self.assertTrue(rehearsal["clean_after_restore"])
        self.assertEqual(
            rehearsal["reverted_file_count"],
            rehearsal["change_set_file_count"],
            "the rehearsal must revert the whole change set, not part of it",
        )
        # At head and after the restore every recorded guard must pass.
        for phase in ("guards_at_head", "guards_after_restore"):
            with self.subTest(phase=phase):
                self.assertTrue(rehearsal[phase])
                for entry in rehearsal[phase]:
                    self.assertEqual(0, entry["returncode"], f"{entry['file']} in {phase}")
        # After the revert the earlier state is restored, so a guard may legitimately fail
        # there - but then the recorded failure text has to say why.
        self.assertTrue(rehearsal["guards_after_revert"])
        for entry in rehearsal["guards_after_revert"]:
            with self.subTest(entry=entry["file"]):
                self.assertIsInstance(entry["returncode"], int)
                if entry["returncode"] != 0:
                    self.assertTrue(
                        entry.get("failures"),
                        f"{entry['file']} failed after the revert without a recorded reason",
                    )
        self.assertIn("never in the live profile", rehearsal["clone_note"])

    def test_topology_facts_are_recorded_from_this_host(self) -> None:
        topology = self.plan["topology"]
        self.assertEqual(PROFILE_ROOT.name, topology["profile_relative_path"])
        self.assertTrue(PROFILE_ROOT.is_dir())
        self.assertTrue(topology["host_runtime_requirement"].strip())
        self.assertTrue(topology["host_tag"].strip())
        self.assertIn("datasage-query", topology["registered_plugins"])
        self.assertEqual(["wecom"], topology["channels"])
        self.assertFalse(topology["schedule_registered"])
        self.assertIs(False, topology["production_mode"])
        self.assertIs(False, topology["require_tls"])


if __name__ == "__main__":
    unittest.main()
