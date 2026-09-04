"""Target-module Golden coverage and structured evidence contract tests.

These tests are model-, database-, and network-free.  They exercise only the
Golden suite and its generic scorer; production query and semantic contracts
remain out of scope here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "plugins" / "datasage-query" / "e2e"


def _load(name: str, path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCORER = _load("target_golden_scorer", E2E / "golden_expert_scorer.py")
SUITE = json.loads(
    (E2E / "golden_expert_cases.json").read_text(encoding="utf-8")
)
CASES = {case["id"]: case for case in SUITE["cases"]}
NEW_CASE_IDS = {
    "target_06_salesperson_customer_delivery",
    "target_07_salesperson_customer_receipt",
    "target_08_salesperson_gap_diagnosis",
    "target_08_salesperson_gap_diagnosis_followup",
    "target_09_target_entity_ambiguity",
    "target_10_salesperson_zero_or_future",
}


def _passing_observed(case: dict) -> dict:
    constraints = case["plan_constraints"]
    requirement = case["evidence_requirements"]
    evidence = {
        "receipts": list(requirement["required_receipts"]),
        "successful_queries": requirement["minimum_successful_queries"],
        "failed_queries": 0,
        "truncated": False,
        "reconciled": requirement["require_reconciled_decomposition"],
        "query_attempted": not requirement["must_not_query"],
        "error_codes": list(requirement["required_error_codes"]),
    }
    structured = requirement.get("structured_evidence_requirements")
    if structured is not None:
        evidence["structured_evidence"] = copy.deepcopy(structured)
    return {
        "plan": {
            key: copy.deepcopy(value)
            for key, value in constraints.items()
            if not key.startswith("must_not_")
        },
        "conclusions": list(case["required_conclusions"]),
        "evidence": evidence,
    }


class TargetGoldenCaseTests(unittest.TestCase):
    def test_new_target_cases_validate_and_cover_the_requested_shapes(self):
        self.assertEqual([], SCORER.validate_suite(SUITE))
        self.assertTrue(NEW_CASE_IDS.issubset(CASES))
        self.assertEqual(58, len(SUITE["cases"]))

        for case_id in NEW_CASE_IDS:
            with self.subTest(case_id=case_id):
                case = CASES[case_id]
                self.assertIn(
                    "structured_evidence_requirements",
                    case["evidence_requirements"],
                )

        delivery = CASES["target_06_salesperson_customer_delivery"]
        receipt = CASES["target_07_salesperson_customer_receipt"]
        self.assertEqual(
            ["salesperson", "customer"],
            delivery["plan_constraints"]["dimensions"],
        )
        self.assertEqual(
            ["salesperson", "customer"],
            receipt["plan_constraints"]["dimensions"],
        )
        self.assertEqual(
            "delivery_target_completion",
            delivery["plan_constraints"]["metrics"][0],
        )
        self.assertEqual(
            "receipt_target_completion",
            receipt["plan_constraints"]["metrics"][0],
        )
        self.assertEqual(
            ["target", "actual", "return"],
            delivery["evidence_requirements"][
                "structured_evidence_requirements"
            ]["source_receipts"],
        )
        self.assertEqual(
            ["target", "actual", "refund"],
            receipt["evidence_requirements"][
                "structured_evidence_requirements"
            ]["source_receipts"],
        )

    def test_customer_drilldown_is_ordinary_grouping_not_complete_decomposition(self):
        for case_id in (
            "target_06_salesperson_customer_delivery",
            "target_07_salesperson_customer_receipt",
            "target_08_salesperson_gap_diagnosis_followup",
        ):
            with self.subTest(case_id=case_id):
                case = CASES[case_id]
                constraints = case["plan_constraints"]
                self.assertNotIn(
                    "complete_target_gap_decomposition", constraints["operations"]
                )
                self.assertIn(
                    "complete_target_gap_decomposition",
                    constraints["must_not_operations"],
                )
                self.assertNotIn(
                    "target_gap_decomposition",
                    case["evidence_requirements"]["required_receipts"],
                )

    def test_structured_evidence_enforces_authority_identity_receipts_reconciliation_and_fallback(self):
        case = CASES["target_06_salesperson_customer_delivery"]
        observed = _passing_observed(case)
        self.assertEqual([], SCORER._score_case(case, observed))

        mutations = {
            "source_authority": lambda evidence: evidence.__setitem__(
                "source_authority", "transaction_detail"
            ),
            "customer_id_binding": lambda evidence: evidence["identity_bindings"].pop(
                "customer"
            ),
            "actual_receipt": lambda evidence: evidence["source_receipts"].remove(
                "actual"
            ),
            "return_receipt": lambda evidence: evidence["source_receipts"].remove(
                "return"
            ),
            "reconciled_totals": lambda evidence: evidence.__setitem__(
                "reconciled_totals", False
            ),
            "base_fallback": lambda evidence: evidence.__setitem__(
                "base_fallback", True
            ),
            "extra_receipt": lambda evidence: evidence[
                "source_receipts"
            ].append("base"),
            "stale_identity_binding": lambda evidence: evidence[
                "identity_bindings"
            ].update({"department": "department_id"}),
            "duplicate_receipt": lambda evidence: evidence[
                "source_receipts"
            ].append("actual"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(observed)
                mutate(candidate["evidence"]["structured_evidence"])
                errors = SCORER._score_case(case, candidate)
                self.assertTrue(
                    any("structured evidence" in error for error in errors),
                    errors,
                )

        causal = copy.deepcopy(observed)
        causal["conclusions"].append("claim_causal_driver")
        self.assertTrue(
            any(
                "claim_causal_driver" in error
                for error in SCORER._score_case(case, causal)
            )
        )
        fallback_operation = copy.deepcopy(observed)
        fallback_operation["plan"]["operations"].append(
            "complete_target_gap_decomposition"
        )
        self.assertTrue(
            any(
                "complete_target_gap_decomposition" in error
                for error in SCORER._score_case(case, fallback_operation)
            )
        )

    def test_two_turn_gap_diagnosis_preserves_entity_and_forbids_causal_shortcuts(self):
        first = CASES["target_08_salesperson_gap_diagnosis"]
        second = CASES["target_08_salesperson_gap_diagnosis_followup"]
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual([1, 2], [first["turn"], second["turn"]])
        self.assertEqual(
            first["plan_constraints"]["context_bindings"],
            second["plan_constraints"]["context_bindings"],
        )
        self.assertEqual("new", first["plan_constraints"]["context_action"])
        self.assertEqual("preserve", second["plan_constraints"]["context_action"])
        self.assertIn(
            "state_noncausal", second["required_conclusions"]
        )
        self.assertIn(
            "claim_causal_driver", second["forbidden_conclusions"]
        )
        self.assertIn(
            "claim_unreconciled_contribution", second["forbidden_conclusions"]
        )
        self.assertEqual([], SCORER._score_case(first, _passing_observed(first)))
        self.assertEqual([], SCORER._score_case(second, _passing_observed(second)))

    def test_target_entity_ambiguity_requires_error_and_no_business_query(self):
        case = CASES["target_09_target_entity_ambiguity"]
        observed = _passing_observed(case)
        self.assertEqual([], SCORER._score_case(case, observed))

        queried = copy.deepcopy(observed)
        queried["evidence"]["query_attempted"] = True
        errors = SCORER._score_case(case, queried)
        self.assertTrue(any("query was attempted" in error for error in errors))

        missing_error = copy.deepcopy(observed)
        missing_error["evidence"]["error_codes"] = []
        errors = SCORER._score_case(case, missing_error)
        self.assertTrue(any("ENTITY_AMBIGUOUS" in error for error in errors))

    def test_salesperson_zero_or_future_case_preserves_target_only_boundary(self):
        case = CASES["target_10_salesperson_zero_or_future"]
        constraints = case["plan_constraints"]
        self.assertEqual(["salesperson"], constraints["dimensions"])
        self.assertEqual(["future_target_only"], constraints["operations"])
        self.assertEqual(
            "future_published_month", constraints["time_semantics"]
        )
        self.assertEqual([], SCORER._score_case(case, _passing_observed(case)))

        invalid = _passing_observed(case)
        invalid["conclusions"].append("report_future_completion")
        errors = SCORER._score_case(case, invalid)
        self.assertTrue(any("report_future_completion" in error for error in errors))

    def test_structured_evidence_requirement_schema_is_strict(self):
        invalid = copy.deepcopy(SUITE)
        requirement = invalid["cases"][-1]["evidence_requirements"]
        requirement["structured_evidence_requirements"]["unknown"] = True
        errors = SCORER.validate_suite(invalid)
        self.assertTrue(
            any("structured_evidence_requirements" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
