"""Focused regression tests for the Golden expert-value gate.

These tests stay model- and network-free.  They exercise only the shared
Golden scorer contract and the transcript adapter's reviewer payload shape.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
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


SCORER = _load("remediation_golden_scorer", E2E / "golden_expert_scorer.py")
ADAPTER = _load("remediation_transcript_adapter", E2E / "canary_transcript_adapter.py")
RUNNER = _load(
    "remediation_live_runner",
    ROOT / "tests" / "business_replay.py",
)
SUITE = json.loads(
    (E2E / "golden_expert_cases.json").read_text(encoding="utf-8")
)


class GoldenExpertGateTests(unittest.TestCase):
    def test_original_suite_is_preserved_and_holdout_rubric_covers_twelve_cases(self):
        self.assertEqual(58, len(SUITE["cases"]))
        self.assertEqual([], SCORER.validate_suite(SUITE))
        holdout_ids = SUITE["release_validation"]["decision_holdout_gate"][
            "case_ids"
        ]
        self.assertEqual(12, len(holdout_ids))
        selected = [
            case
            for case in SUITE["cases"]
            if case["id"] in set(holdout_ids)
        ]
        self.assertEqual(12, len(selected))
        self.assertGreaterEqual(len({case["category"] for case in selected}), 8)
        self.assertGreater(len({case["conversation_id"] for case in selected}), 1)
        for case in selected:
            requirement = case["decision_quality_requirements"]
            self.assertEqual(
                set(SCORER.DECISION_QUALITY_DIMENSIONS),
                set(requirement["required_dimensions"]),
            )
            self.assertEqual(1, requirement["minimum_score"])
        trusted_ids = SUITE["release_validation"]["trusted_replay_gate"][
            "case_ids"
        ]
        trusted = [case for case in SUITE["cases"] if case["id"] in trusted_ids]
        self.assertEqual(2, len(trusted))
        self.assertTrue(
            all("decision_quality_requirements" in case for case in trusted)
        )

    def test_adversarial_behavior_gate_covers_activation_injection_and_context(self):
        gate = SUITE["release_validation"]["adversarial_behavior_gate"]
        self.assertEqual(7, gate["case_count"])
        self.assertEqual(7, len(gate["case_ids"]))
        by_id = {case["id"]: case for case in SUITE["cases"]}
        selected = [by_id[case_id] for case_id in gate["case_ids"]]
        self.assertEqual(
            {"activation_boundary", "prompt_injection", "memory_compaction"},
            {case["category"] for case in selected},
        )
        activation = [
            case for case in selected if case["category"] == "activation_boundary"
        ]
        self.assertTrue(
            all(case["evidence_requirements"]["must_not_query"] for case in activation)
        )
        compacted = [
            case
            for case in selected
            if case["conversation_id"] == "compaction_context_recovery"
        ]
        self.assertEqual([1, 2], [case["turn"] for case in compacted])
        followup = compacted[1]
        self.assertEqual(["top_n"], followup["plan_constraints"]["operations"])
        self.assertEqual("replace", followup["plan_constraints"]["context_action"])
        self.assertEqual(
            {"department": "example_region_a", "limit": 5},
            followup["plan_constraints"]["context_bindings"],
        )
        self.assertEqual(
            ["preserve_period_entity_and_limit", "report_top_n"],
            followup["required_conclusions"],
        )
        self.assertNotIn(
            "reload_missing_metric_detail", followup["required_conclusions"]
        )
        self.assertEqual(
            ["query", "coverage", "context_transition"],
            followup["evidence_requirements"]["required_receipts"],
        )
        self.assertNotIn(
            "catalog", followup["evidence_requirements"]["required_receipts"]
        )
        self.assertNotIn(
            "metric_detail", followup["evidence_requirements"]["required_receipts"]
        )
        tampered = copy.deepcopy(SUITE)
        tampered["release_validation"]["adversarial_behavior_gate"][
            "case_count"
        ] = 6
        self.assertTrue(
            any(
                "adversarial_behavior_gate" in error
                for error in SCORER.validate_suite(tampered)
            )
        )

    def test_prompt_leak_check_is_exact_and_does_not_flag_normal_language(self):
        self.assertEqual([], SCORER.validate_suite(SUITE))
        normal = copy.deepcopy(SUITE)
        normal["cases"][0]["prompt"] = "请查询本月净出库额，并说明统计口径。"
        self.assertEqual([], SCORER.validate_suite(normal))

        leaked = copy.deepcopy(SUITE)
        leaked["cases"][0]["prompt"] = "请返回 report_observed_change。"
        errors = SCORER.validate_suite(leaked)
        self.assertTrue(any("contract identifier leak" in error for error in errors))

    def test_domain_metric_pair_is_not_reconstructed_from_independent_sets(self):
        case = copy.deepcopy(SUITE["cases"][0])
        case["plan_constraints"]["domain_metric_pairs"] = [
            {"domain": "delivery", "metric": "delivery_amount"}
        ]
        observed = {
            "plan": {
                **case["plan_constraints"],
                "domain_metric_pairs": [
                    {"domain": "delivery", "metric": "wrong_metric"}
                ],
            },
            "conclusions": case["required_conclusions"],
            "evidence": {
                "receipts": case["evidence_requirements"]["required_receipts"],
                "successful_queries": 2,
                "failed_queries": 0,
                "truncated": False,
                "reconciled": True,
                "query_attempted": True,
                "error_codes": [],
            },
        }
        errors = SCORER._score_case(case, observed)
        self.assertTrue(any("domain_metric_pairs" in error for error in errors))

        observed["plan"]["domain_metric_pairs"] = [
            {"domain": "delivery", "metric": "delivery_amount"}
        ]
        self.assertEqual([], SCORER._score_case(case, observed))

    def test_safety_and_expert_value_scores_are_independent_and_both_gate(self):
        case = next(
            item
            for item in SUITE["cases"]
            if "decision_quality_requirements" in item
        )
        good_quality = {
            dimension: 1 for dimension in SCORER.DECISION_QUALITY_DIMENSIONS
        }
        quality, errors = SCORER._score_decision_quality(
            case, {"decision_quality": good_quality}
        )
        self.assertEqual([], errors)
        self.assertTrue(quality["passed"])
        self.assertEqual(7, quality["score"])

        bad_quality = dict(good_quality)
        bad_quality["material_risks"] = 0
        quality, errors = SCORER._score_decision_quality(
            case, {"decision_quality": bad_quality}
        )
        self.assertFalse(quality["passed"])
        self.assertTrue(any("material_risks" in error for error in errors))

    def test_adapter_reviewer_quality_is_strictly_seven_dimensions(self):
        good = {
            dimension: 2 for dimension in ADAPTER.DECISION_QUALITY_DIMENSIONS
        }
        self.assertEqual(good, ADAPTER._validate_decision_quality(good))
        with self.assertRaisesRegex(ValueError, "exactly the seven"):
            ADAPTER._validate_decision_quality({"conclusion_clarity": 2})
        invalid = dict(good)
        invalid["actionability"] = 3
        with self.assertRaisesRegex(ValueError, "0..2"):
            ADAPTER._validate_decision_quality(invalid)

    def test_live_runner_preserves_consensus_decision_quality_for_adapter(self):
        quality = {
            dimension: 1 for dimension in ADAPTER.DECISION_QUALITY_DIMENSIONS
        }
        assertion = RUNNER._review_assertion(
            {
                "reviewer_id": "consensus-reviewer",
                "labels": ["report_available_evidence"],
                "decision_quality": quality,
            },
            adapter=ADAPTER,
            profile={"artifact_id": "a" * 40, "payload_sha256": "b" * 64},
            case_id="case-1",
            session_id="session-1",
            user_id=1,
            prompt_sha="c" * 64,
            session_export_sha256="d" * 64,
            watermark="e" * 64,
            final_sha="f" * 64,
            fixture_sha="1" * 64,
            database_ref_sha="2" * 64,
        )
        self.assertEqual(quality, assertion["decision_quality"])

    def _l3_case(self, case_id):
        return next(case for case in SUITE["l3_cases"] if case["id"] == case_id)

    def _passing_l3_observed(self, case):
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
        return {
            "plan": {
                key: copy.deepcopy(value)
                for key, value in constraints.items()
                if not key.startswith("must_not_")
            },
            "conclusions": list(case["required_conclusions"]),
            "evidence": evidence,
        }

    def test_l3_extension_and_frozen_base_except_explicit_domain_relocation(self):
        self.assertEqual(58, len(SUITE["cases"]))
        self.assertEqual(8, len(SUITE["l3_cases"]))
        frozen_base = copy.deepcopy(SUITE["cases"])
        relocated = {
            "change_05_delivery_receipt": ["receipt"],
            "causal_03_customer_risk": ["receivable", "receipt"],
            "boundary_03_grouped_formal_turnover_undefined": ["receivable"],
            "multiturn_07_live_balance_correction": ["receivable"],
        }
        for case in frozen_base:
            if case["id"] in relocated:
                self.assertEqual(relocated[case["id"]], case["plan_constraints"]["domains"])
                case["plan_constraints"]["domains"] = ["customer_risk"]
        # Keep the original digest: only the explicit namespace relocation is
        # normalized. Prompts, conclusions and prohibitions may not drift.
        canonical = json.dumps(
            frozen_base,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            "e3eafdcfb76f4fde7e44e6ef572189f158dbd39548c45e6dbaaaaf76d66bd744",
            hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual([], SCORER.validate_suite(SUITE))
        ids = [case["id"] for case in SUITE["l3_cases"]]
        selected = SCORER.select_suite(SUITE, ids)
        self.assertEqual(ids, [case["id"] for case in selected["cases"]])
        self.assertEqual([], SCORER.validate_suite(selected))

    def test_l3_gross_return_net_and_order_cases_are_contracts_not_value_claims(self):
        reconciliation = self._l3_case(
            "l3_01_delivery_gross_return_net_reconciliation"
        )
        observed = self._passing_l3_observed(reconciliation)
        self.assertEqual([], SCORER._score_case(reconciliation, observed))
        gross = Decimal("125.50")
        returned = Decimal("25.50")
        net = gross - returned
        self.assertEqual(Decimal("100.00"), net)

        order = self._l3_case("l3_02_delivery_order_amount_positive")
        self.assertIn("order_amount", order["plan_constraints"]["metrics"])
        self.assertNotIn(
            "order_amount", order["plan_constraints"]["must_not_metrics"]
        )
        self.assertEqual([], SCORER._score_case(order, self._passing_l3_observed(order)))
        self.assertEqual(
            "not_verified", SCORER.VERIFICATION_SCOPE["business_values"]
        )
        self.assertEqual(
            "not_verified", SCORER.VERIFICATION_SCOPE["business_arithmetic"]
        )

    def test_l3_quantity_unit_scope_and_internal_scope_require_safe_conclusions(self):
        quantity = self._l3_case("l3_03_delivery_quantity_unit_scoped")
        observed = self._passing_l3_observed(quantity)
        self.assertEqual([], SCORER._score_case(quantity, observed))
        unscoped = copy.deepcopy(observed)
        unscoped["plan"]["dimensions"] = []
        self.assertTrue(
            any(
                "plan.dimensions" in error
                for error in SCORER._score_case(quantity, unscoped)
            )
        )
        substituted = copy.deepcopy(observed)
        substituted["conclusions"].append("substitute_delivery_amount")
        self.assertTrue(
            any(
                "substitute_delivery_amount" in error
                for error in SCORER._score_case(quantity, substituted)
            )
        )
        mixed = copy.deepcopy(observed)
        mixed["conclusions"].append("report_mixed_unit_total")
        self.assertTrue(
            any(
                "report_mixed_unit_total" in error
                for error in SCORER._score_case(quantity, mixed)
            )
        )

        scope = self._l3_case("l3_04_delivery_internal_customer_scope")
        self.assertEqual([], SCORER._score_case(scope, self._passing_l3_observed(scope)))
        wrong_scope = copy.deepcopy(self._passing_l3_observed(scope))
        wrong_scope["conclusions"].append("claim_internal_customers_included")
        self.assertTrue(
            any(
                "claim_internal_customers_included" in error
                for error in SCORER._score_case(scope, wrong_scope)
            )
        )

    def test_l3_owner_confirmed_organization_and_top5_cases_preserve_scope(self):
        identity = self._l3_case("l3_05_delivery_owner_confirmed_organization")
        observed = self._passing_l3_observed(identity)
        self.assertEqual([], SCORER._score_case(identity, observed))
        missing_scope = copy.deepcopy(observed)
        missing_scope["conclusions"].remove(
            "state_owner_confirmed_organization_equivalence"
        )
        self.assertTrue(
            any(
                "state_owner_confirmed_organization_equivalence" in error
                for error in SCORER._score_case(identity, missing_scope)
            )
        )

        top5 = self._l3_case("l3_06_delivery_top5_ties_population_scope")
        self.assertEqual([], SCORER._score_case(top5, self._passing_l3_observed(top5)))
        rows = [
            ("p1", Decimal("50.00")),
            ("p2", Decimal("40.00")),
            ("p3", Decimal("40.00")),
            ("p4", Decimal("30.00")),
            ("p5", Decimal("20.00")),
            ("p6", Decimal("10.00")),
        ]
        displayed = rows[:5]
        self.assertEqual(
            ["p1", "p2", "p3", "p4", "p5"],
            [entity for entity, _value in displayed],
        )
        self.assertEqual(Decimal("180.00"), sum(value for _entity, value in displayed))
        self.assertEqual(Decimal("190.00"), sum(value for _entity, value in rows))
        bad_scope = copy.deepcopy(self._passing_l3_observed(top5))
        bad_scope["conclusions"].append("equate_top_n_total_with_full_total")
        self.assertTrue(
            any(
                "equate_top_n_total_with_full_total" in error
                for error in SCORER._score_case(top5, bad_scope)
            )
        )

    def test_l3_open_diagnosis_requires_boundary_labels_and_quality_shape(self):
        case = self._l3_case("l3_08_delivery_open_diagnosis")
        observed = self._passing_l3_observed(case)
        observed["decision_quality"] = {
            dimension: 1 for dimension in SCORER.DECISION_QUALITY_DIMENSIONS
        }
        self.assertEqual([], SCORER._score_case(case, observed))
        quality, errors = SCORER._score_decision_quality(case, observed)
        self.assertEqual([], errors)
        self.assertTrue(quality["passed"])
        for label in (
            "report_benchmark",
            "report_reconciled_structure",
            "propose_falsifiable_hypotheses",
            "state_evidence_insufficient",
            "recommend_validation_action",
            "define_review_metric",
        ):
            self.assertIn(label, case["required_conclusions"])
        forbidden = copy.deepcopy(observed)
        forbidden["conclusions"].append("claim_causal_driver")
        self.assertTrue(
            any(
                "claim_causal_driver" in error
                for error in SCORER._score_case(case, forbidden)
            )
        )

    def test_replacement_turn_rejects_extra_stale_bindings_and_old_dimensions(self):
        case = copy.deepcopy(self._l3_case("l3_06_delivery_top5_ties_population_scope"))
        case["plan_constraints"]["context_action"] = "replace"
        case["plan_constraints"]["context_bindings"] = {
            "department": "example_region_b",
            "limit": 5,
        }
        observed = self._passing_l3_observed(case)
        self.assertEqual([], SCORER._score_case(case, observed))

        extra_binding = copy.deepcopy(observed)
        extra_binding["plan"]["context_bindings"]["old_department"] = (
            "example_region_a"
        )
        self.assertTrue(
            any(
                "strict plan.context_bindings" in error
                for error in SCORER._score_case(case, extra_binding)
            )
        )

        stale_binding = copy.deepcopy(observed)
        stale_binding["plan"]["context_bindings"]["department"] = (
            "example_region_a"
        )
        self.assertTrue(
            any(
                "expected" in error
                for error in SCORER._score_case(case, stale_binding)
            )
        )

        stale_dimension = copy.deepcopy(observed)
        stale_dimension["plan"]["dimensions"] = ["customer", "product"]
        self.assertTrue(
            any(
                "strict plan.dimensions" in error
                for error in SCORER._score_case(case, stale_dimension)
            )
        )

    def test_observed_plan_conclusion_and_evidence_lists_reject_duplicates(self):
        case = self._l3_case("l3_06_delivery_top5_ties_population_scope")
        baseline = self._passing_l3_observed(case)
        for mutate, text in (
            (
                lambda value: value["plan"]["domains"].append("delivery"),
                "plan.domains",
            ),
            (
                lambda value: value["plan"]["metrics"].append("delivery_amount"),
                "plan.metrics",
            ),
            (
                lambda value: value["plan"]["dimensions"].append("product"),
                "plan.dimensions",
            ),
            (
                lambda value: value["plan"]["operations"].append("top_n"),
                "plan.operations",
            ),
            (
                lambda value: value["conclusions"].append("report_top_n"),
                "conclusions",
            ),
            (
                lambda value: value["evidence"]["receipts"].append("query"),
                "evidence.receipts",
            ),
            (
                lambda value: value["evidence"]["error_codes"].extend(["E", "E"]),
                "evidence.error_codes",
            ),
        ):
            with self.subTest(field=text):
                mutated = copy.deepcopy(baseline)
                mutate(mutated)
                self.assertTrue(
                    any(
                        "duplicate" in error
                        for error in SCORER._score_case(case, mutated)
                    )
                )

    def test_malformed_observed_lists_and_query_counts_return_structured_errors(self):
        case = self._l3_case("l3_06_delivery_top5_ties_population_scope")
        baseline = self._passing_l3_observed(case)
        mutations = (
            ("plan.domains", lambda value: value["plan"].__setitem__("domains", None)),
            ("conclusions", lambda value: value.__setitem__("conclusions", {})),
            ("receipts", lambda value: value["evidence"].__setitem__("receipts", {})),
            (
                "successful_queries",
                lambda value: value["evidence"].__setitem__("successful_queries", []),
            ),
            (
                "failed_queries",
                lambda value: value["evidence"].__setitem__("failed_queries", {}),
            ),
            (
                "error_codes",
                lambda value: value["evidence"].__setitem__("error_codes", {}),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(field=label):
                observed = copy.deepcopy(baseline)
                mutate(observed)
                errors = SCORER._score_case(case, observed)
                self.assertTrue(errors)
                self.assertTrue(all(isinstance(error, str) for error in errors))


if __name__ == "__main__":
    unittest.main()
