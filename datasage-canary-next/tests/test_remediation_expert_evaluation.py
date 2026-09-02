"""Focused regression tests for the Golden expert-value gate.

These tests stay model- and network-free.  They exercise only the shared
Golden scorer contract and the transcript adapter's reviewer payload shape.
"""

from __future__ import annotations

import copy
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
    ROOT / "tests" / "run_live_release_evidence.py",
)
SUITE = json.loads(
    (E2E / "golden_expert_cases.json").read_text(encoding="utf-8")
)


class GoldenExpertGateTests(unittest.TestCase):
    def test_original_suite_is_preserved_and_holdout_rubric_covers_twelve_cases(self):
        self.assertEqual(51, len(SUITE["cases"]))
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


if __name__ == "__main__":
    unittest.main()
