"""Targeted L3 delivery Golden contract checks.

These checks deliberately keep business-value arithmetic in a small local
fixture.  The semantic Golden scorer is only asked to validate plan/evidence
contract shape and must report that final text and business values remain
unverified until an external replay/evidence layer is used.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "plugins" / "datasage-query" / "e2e"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCORER = _load("delivery_l3_scorer", E2E / "golden_expert_scorer.py")
SUITE = json.loads(
    (E2E / "golden_expert_cases.json").read_text(encoding="utf-8")
)
L3_CASES = {case["id"]: case for case in SUITE["l3_cases"]}


def _passing(case: dict) -> dict:
    constraints = case["plan_constraints"]
    requirement = case["evidence_requirements"]
    return {
        "plan": {
            key: copy.deepcopy(value)
            for key, value in constraints.items()
            if not key.startswith("must_not_")
        },
        "conclusions": list(case["required_conclusions"]),
        "evidence": {
            "receipts": list(requirement["required_receipts"]),
            "successful_queries": requirement["minimum_successful_queries"],
            "failed_queries": 0,
            "truncated": False,
            "reconciled": requirement["require_reconciled_decomposition"],
            "query_attempted": not requirement["must_not_query"],
            "error_codes": list(requirement["required_error_codes"]),
        },
    }


class DeliveryL3GoldenTests(unittest.TestCase):
    def test_l3_catalog_is_eight_cases_and_keeps_live_gate_pending(self):
        self.assertEqual(8, len(L3_CASES))
        self.assertEqual([], SCORER.validate_suite(SUITE))
        self.assertTrue(
            all(
                case["evaluation_mode"] == "design/offline contract"
                and case["formal_release_eligible"] is False
                and case["strict_plan"] is True
                for case in L3_CASES.values()
            )
        )
        self.assertEqual(
            "not_verified_for_candidate",
            SUITE["release_validation"]["live_model_replay_status"],
        )
        self.assertEqual(
            "blocked_pending_live_model_replay",
            SUITE["release_validation"]["release_gate_status"],
        )
        selected = SCORER.select_suite(SUITE, list(L3_CASES))
        self.assertEqual(8, len(selected["cases"]))
        self.assertFalse(selected["formal_release_eligible"])
        self.assertEqual([], SCORER.validate_suite(selected))

    def test_numeric_fixture_is_checked_here_not_claimed_by_semantic_scorer(self):
        case = L3_CASES["l3_01_delivery_gross_return_net_reconciliation"]
        self.assertEqual([], SCORER._score_case(case, _passing(case)))
        gross = Decimal("321.45")
        returned = Decimal("21.45")
        self.assertEqual(Decimal("300.00"), gross - returned)
        self.assertEqual("not_verified", SCORER.VERIFICATION_SCOPE["business_values"])
        self.assertEqual("not_verified", SCORER.VERIFICATION_SCOPE["business_arithmetic"])

    def test_order_and_unit_scoped_quantity_contracts_are_distinct(self):
        order = L3_CASES["l3_02_delivery_order_amount_positive"]
        quantity = L3_CASES["l3_03_delivery_quantity_unit_scoped"]
        self.assertEqual(["order_amount"], order["plan_constraints"]["metrics"])
        self.assertEqual(["delivery_quantity"], quantity["plan_constraints"]["metrics"])
        self.assertEqual(["unit"], quantity["plan_constraints"]["dimensions"])
        self.assertFalse(quantity["evidence_requirements"]["must_not_query"])


if __name__ == "__main__":
    unittest.main()
