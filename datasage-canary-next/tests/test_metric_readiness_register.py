"""R16: the metric readiness register must stay complete, current and honest.

The register turns an implicit state ("no availability block means available") into
an explicit per-metric record, and it only lets a metric be marked verified with an
independent evidence record.  These tests keep it covering every registered metric,
keep its contract-derived fields equal to the contracts, and prove the verification
rules actually reject a bare or self-referential claim.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

import metric_readiness

PROFILE_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = PROFILE_ROOT / metric_readiness.REGISTER_PATH
EXPECTED_TOTAL = 238  # 215 preserved IDs + 23 original-currency variants
VALID_EVIDENCE = {
    "kind": "independent_sql",
    "reference": "reviewed reconciliation query, ticket 4711",
    "artifact_sha256": "a" * 64,
    "approved_by": "data-owner",
    "approved_on": "2026-09-23",
}


class MetricReadinessRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        self.fresh = metric_readiness.build_register(PROFILE_ROOT, self.register)

    def _row(self, domain: str, metric: str) -> dict:
        for row in self.register["metrics"]:
            if row["domain"] == domain and row["metric"] == metric:
                return row
        self.fail(f"{domain}/{metric} is not in the register")

    def test_register_covers_every_registered_metric_once(self) -> None:
        keys = [(row["domain"], row["metric"]) for row in self.register["metrics"]]
        self.assertEqual(EXPECTED_TOTAL, len(keys))
        self.assertEqual(len(keys), len(set(keys)), "a metric must appear once")
        self.assertEqual(
            sorted((row["domain"], row["metric"]) for row in self.fresh["metrics"]),
            sorted(keys),
        )

    def test_register_contract_fields_match_the_current_contracts(self) -> None:
        """A contract change must be reflected in the register, not silently skipped."""

        self.assertEqual(self.fresh["metrics"], self.register["metrics"])
        self.assertEqual(self.fresh["summary"], self.register["summary"])
        self.assertEqual(self.fresh["generated_from"], self.register["generated_from"])

    def test_register_is_structurally_valid(self) -> None:
        self.assertEqual([], metric_readiness.validate_register(self.register))
        self.assertTrue(self.register["not_a_release_gate"])
        self.assertEqual(metric_readiness.SCHEMA, self.register["schema"])
        # Nothing is verified yet: the register starts from the contract state.
        self.assertEqual(0, self.register["summary"]["verified"])
        self.assertEqual(
            self.register["summary"]["total"], self.register["summary"]["unverified"]
        )

    def test_declared_state_is_visible_including_the_implicit_default(self) -> None:
        """Most metrics are available by omission; the register must say so."""

        implicit = [
            row
            for row in self.register["metrics"]
            if row["declared_availability"] == "available"
            and not row["declared_by_contract"]
        ]
        contract_available = [
            row
            for row in self.register["metrics"]
            if row["declared_availability"] == "available"
            and row["declared_by_contract"]
        ]
        self.assertEqual(
            self.register["summary"]["declared_available"],
            len(implicit) + len(contract_available),
        )
        self.assertGreater(len(implicit), 0)
        self.assertEqual(
            self.register["summary"]["declared_by_contract"],
            len(contract_available)
            + sum(
                1
                for row in self.register["metrics"]
                if row["declared_by_contract"]
                and row["declared_availability"] != "available"
            ),
        )
        for row in implicit:
            with self.subTest(metric=row["metric"]):
                self.assertIsNone(
                    row["owner"], "an implicit default has no owner recorded"
                )
        for row in contract_available:
            with self.subTest(metric=row["metric"]):
                self.assertIsNotNone(
                    row["owner"], "an explicit declaration must name an owner"
                )

    def test_verified_metrics_need_independent_evidence(self) -> None:
        bare = copy.deepcopy(self.register)
        bare["metrics"][0]["verified"] = True
        bare["summary"]["verified"] = 1
        bare["summary"]["unverified"] -= 1
        problems = metric_readiness.validate_register(bare)
        self.assertTrue(
            any("without an independent evidence record" in item for item in problems),
            problems,
        )

        self_referential = copy.deepcopy(bare)
        self_referential["metrics"][0]["independent_evidence"] = [
            {**VALID_EVIDENCE, "kind": "engine_output"}
        ]
        problems = metric_readiness.validate_register(self_referential)
        self.assertTrue(
            any("evidence kind must be" in item for item in problems), problems
        )

        complete = copy.deepcopy(bare)
        complete["metrics"][0]["independent_evidence"] = [dict(VALID_EVIDENCE)]
        self.assertEqual([], metric_readiness.validate_register(complete))

    def test_contract_declared_unavailable_metrics_stay_unverified(self) -> None:
        pending = [
            row
            for row in self.register["metrics"]
            if row["declared_availability"] != "available"
        ]
        self.assertTrue(pending, "the register should carry the declared gaps")
        for row in pending:
            with self.subTest(metric=row["metric"]):
                self.assertFalse(row["verified"])

        corrupted = copy.deepcopy(self.register)
        for row in corrupted["metrics"]:
            if row["declared_availability"] != "available":
                row["verified"] = True
                row["independent_evidence"] = [dict(VALID_EVIDENCE)]
        corrupted["summary"]["verified"] = len(pending)
        corrupted["summary"]["unverified"] -= len(pending)
        problems = metric_readiness.validate_register(corrupted)
        self.assertTrue(
            any("while the contract declares" in item for item in problems), problems
        )

    def test_summary_must_match_the_rows(self) -> None:
        corrupted = copy.deepcopy(self.register)
        corrupted["summary"]["verified"] = 7
        problems = metric_readiness.validate_register(corrupted)
        self.assertTrue(any("summary.verified" in item for item in problems), problems)


if __name__ == "__main__":
    unittest.main()
