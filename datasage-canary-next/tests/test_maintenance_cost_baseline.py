"""R30 guards: measured maintenance cost, orphan candidates, no deletion by convenience.

An orphan candidate is evidence of low visibility only. This guard keeps the record measured,
keeps a disposition and an owner requirement on every candidate, refuses verdict language,
and protects the retention artifacts from being dropped to shrink the maintenance surface.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import maintenance_cost

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = PROFILE_ROOT / "tests" / "fixtures" / "maintenance_cost_baseline.json"
VERDICT_WORDS = ("无用", "useless", "not needed", "worthless", "obsolete")


def _register() -> dict:
    return json.loads(REGISTER_PATH.read_text(encoding="utf-8"))


class MaintenanceCostBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register = _register()

    def test_the_surface_and_change_cost_are_measured_not_estimated(self) -> None:
        surface = self.register["surface"]
        self.assertTrue(surface)
        for category, counts in surface.items():
            with self.subTest(category=category):
                self.assertGreater(counts["files"], 0)
                self.assertGreater(counts["lines"], 0)
                self.assertEqual(counts["files"], len(counts["names"]))
        cost = self.register["change_cost"]
        self.assertEqual("measured", cost["method"])
        self.assertTrue(cost["measured_from"])
        history = cost["history"]
        self.assertGreater(history["code_files_per_commit_median"], 0)
        self.assertGreater(history["test_files_per_commit_median"], 0)
        self.assertEqual(40, history["window_commits"])

    def test_the_baseline_still_matches_the_workspace(self) -> None:
        measured = maintenance_cost.measure()
        for key in ("surface", "change_cost", "orphan_candidates"):
            with self.subTest(key=key):
                self.assertEqual(measured[key], self.register[key])

    def test_every_orphan_candidate_has_evidence_and_a_disposition(self) -> None:
        candidates = self.register["orphan_candidates"]
        options = self.register["disposition_options"]
        for item in candidates:
            with self.subTest(path=item["path"]):
                self.assertEqual("candidate", item["status"])
                self.assertTrue(str(item["evidence"]).strip())
                self.assertEqual(0, item["inbound_references"])
                self.assertIn(item["disposition_proposed"], options)
                self.assertTrue((PROFILE_ROOT / item["path"]).exists())
        totals = self.register["orphan_totals"]
        self.assertEqual(len(candidates), totals["total"])
        self.assertEqual(
            sum(1 for item in candidates if item.get("owner")), totals["with_owner"]
        )
        self.assertEqual(
            sum(1 for item in candidates if not item.get("owner")),
            totals["without_owner"],
        )

    def test_no_candidate_is_called_useless(self) -> None:
        blob = json.dumps(self.register["orphan_candidates"], ensure_ascii=False).lower()
        for word in VERDICT_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word.lower(), blob)
        self.assertIn("未用不等于无用", self.register["policy"])

    def test_nothing_in_the_retention_list_disappears(self) -> None:
        statement = self.register["no_deletion_policy"]["statement"]
        self.assertIn("回执", statement)
        self.assertIn("审计", statement)
        self.assertIn("保留", statement)
        self.assertTrue(self.register["no_deletion_policy"]["protected"])
        for path in self.register["retention_artifacts"]:
            with self.subTest(path=path):
                self.assertTrue((PROFILE_ROOT / path).exists(), f"{path} was removed")

    def test_a_removal_can_never_be_justified_by_maintenance_load_alone(self) -> None:
        for item in self.register["orphan_candidates"]:
            with self.subTest(path=item["path"]):
                if item["disposition_proposed"] == "removal_requires_owner":
                    self.assertTrue(
                        str(item.get("disposition_reason") or "").strip(),
                        "a removal proposal must state its reason",
                    )
                if not item.get("owner"):
                    self.assertIn(
                        "add_owner", self.register["disposition_options"]
                    )


if __name__ == "__main__":
    unittest.main()
