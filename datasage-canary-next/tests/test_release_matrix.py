"""R27 guards: independent truth, holdout separation and an honest release matrix.

The record may not drift from its sources, may not let a model output become the truth, may
not let a holdout case enter tuning, may not hide or delete a failure, and may never be
signed by the assistant.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

import release_matrix

PROFILE_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = PROFILE_ROOT / "tests" / "fixtures" / "release_matrix.json"
STATUSES = ("verified", "partial", "unverified", "blocked_external")
TRUTH_KINDS = ("independent_sql", "manual_fixture", "owner_statement", "expert_review")
FORBIDDEN_KINDS = ("model_output", "engine_output", "self_assessment")
REQUIRED_SURFACES = release_matrix.DOMAIN_SURFACES + release_matrix.OTHER_SURFACES


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


class ReleaseMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = _matrix()

    def test_every_published_surface_is_present_with_sample_pass_and_reproduction(self) -> None:
        capabilities = self.matrix["capabilities"]
        self.assertEqual(set(REQUIRED_SURFACES), set(capabilities))
        for surface, row in capabilities.items():
            with self.subTest(surface=surface):
                self.assertGreater(row["sample"], 0)
                self.assertIsInstance(row["pass"], int)
                self.assertGreaterEqual(row["pass"], 0)
                self.assertLessEqual(row["pass"], row["sample"])
                self.assertIn(row["status"], STATUSES)
                self.assertTrue(row["sample_source"].strip())
                path = row["reproduction_path"].split("::")[0]
                self.assertTrue((PROFILE_ROOT / path).exists(), f"{path} must exist")
                self.assertIs(False, row["released"])

    def test_a_failure_is_stated_not_deleted(self) -> None:
        for surface, row in self.matrix["capabilities"].items():
            with self.subTest(surface=surface):
                if row["pass"] < row["sample"] or row["status"] != "verified":
                    self.assertTrue(
                        str(row["failure_reason"]).strip(),
                        f"{surface} must state why it is not verified",
                    )
                if row["status"] == "verified":
                    self.assertEqual(row["sample"], row["pass"])

    def test_unreproducible_data_is_kept_out_of_the_matrix(self) -> None:
        self.assertEqual([], release_matrix.unreproducible_problems(self.matrix))

        drifted = copy.deepcopy(self.matrix)
        drifted["capabilities"]["domain.delivery"]["reproducible"] = False
        self.assertEqual(
            [
                "domain.delivery: unreproducible data must not carry measured numbers"
            ],
            release_matrix.unreproducible_problems(drifted),
        )

        for surface, candidate in self.matrix["capabilities"].items():
            with self.subTest(surface=surface):
                self.assertIs(True, candidate["reproducible"])
                self.assertIsNotNone(candidate["pass"])

    def test_independent_truth_never_comes_from_the_model(self) -> None:
        truth = self.matrix["truth"]
        self.assertEqual(24, len(truth))
        for row in truth:
            with self.subTest(case=row["case_id"]):
                self.assertEqual("pending", row["truth_status"])
                self.assertIsNone(row["independent_truth"])
                self.assertIsNone(row["adjudication"])

        accepted = copy.deepcopy(truth[0])
        accepted.update(
            {
                "truth_status": "accepted",
                "independent_truth": {"net_rmb": 150000},
                "reviewer_role": "业务 owner",
                "reviewed_on": "2026-09-23",
                "evidence": [
                    {
                        "kind": "model_output",
                        "reference": "the assistant's own answer",
                        "artifact_sha256": "c" * 64,
                    }
                ],
            }
        )
        self.assertIn(accepted["evidence"][0]["kind"], FORBIDDEN_KINDS)
        self.assertNotIn(accepted["evidence"][0]["kind"], TRUTH_KINDS)

        good = copy.deepcopy(accepted)
        good["evidence"] = [
            {
                "kind": "expert_review",
                "reference": "独立复核记录",
                "artifact_sha256": "d" * 64,
            }
        ]
        self.assertIn(good["evidence"][0]["kind"], TRUTH_KINDS)
        self.assertTrue(good["reviewer_role"])
        self.assertTrue(good["reviewed_on"])

    def test_holdout_cases_do_not_enter_tuning_and_await_confirmation(self) -> None:
        holdout = self.matrix["holdout"]
        cases = {row["case_id"] for row in self.matrix["truth"]}
        self.assertTrue(holdout["case_ids"])
        self.assertEqual(
            [],
            sorted(set(holdout["case_ids"]) & set(holdout["tuning_case_ids"])),
            "a case cannot be both holdout and tuning",
        )
        self.assertEqual(cases, set(holdout["case_ids"]) | set(holdout["tuning_case_ids"]))
        self.assertTrue(set(holdout["case_ids"]).issubset(cases))
        for case_id in holdout["case_ids"]:
            with self.subTest(case=case_id):
                row = next(item for item in self.matrix["truth"] if item["case_id"] == case_id)
                self.assertIs(True, row["holdout"])
        self.assertIsNone(
            holdout["confirmed_by"],
            "the split waits for an owner; until then it is a proposal",
        )
        self.assertIn("确认", holdout["note"])

    def test_the_assistant_never_signs_or_releases(self) -> None:
        signoff = self.matrix["signoff"]
        self.assertIsNone(signoff["signed_by"])
        self.assertIsNone(signoff["signed_on"])
        for surface, row in self.matrix["capabilities"].items():
            with self.subTest(surface=surface):
                self.assertIsNone(row["release_decision"])
                self.assertIsNone(row["release_decision_owner"])
                self.assertIs(False, row["released"])
        self.assertIn("不代签发布", signoff["note"])
        self.assertIn("never signs a release", self.matrix["policy"])

    def test_no_capability_is_silently_removed(self) -> None:
        for entry in self.matrix["retired"]:
            with self.subTest(surface=entry.get("surface")):
                self.assertTrue(str(entry.get("reason") or "").strip())
                self.assertIn(entry["status"], ("withdrawn", "unverified"))
        self.assertEqual([], self.matrix["retired"], "nothing has been withdrawn yet")

    def test_the_matrix_still_matches_its_sources(self) -> None:
        derived = release_matrix._derived()
        self.assertEqual(derived["capability_counts"], self.matrix["capability_counts"])
        for surface, row in derived["capabilities"].items():
            with self.subTest(surface=surface):
                stored = self.matrix["capabilities"][surface]
                for key in ("sample", "pass", "sample_source", "reproducible"):
                    self.assertEqual(row[key], stored[key])


if __name__ == "__main__":
    unittest.main()
