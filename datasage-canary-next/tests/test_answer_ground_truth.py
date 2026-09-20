"""H02 final-answer Ground Truth and official-export binding tests."""

from __future__ import annotations

import copy
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


SCORER = _load("h02_golden_scorer", E2E / "golden_expert_scorer.py")
REPLAY = _load("h02_business_replay", ROOT / "tests" / "business_replay.py")
GT = _load("h02_answer_ground_truth", E2E / "answer_ground_truth.py")


PROMPT = "示例A区2026年9月出库目标完成情况如何？"
ANSWER = """2026年9月目标完成情况：
| 指标 | 数值 |
| 目标 | 100.00 万元 |
| 实际 | 80.00 万元 |
| 差额 | -20.00 万元 |
| 完成率 | 80.00 % |
| 欠款 | 未知（未提供）万元 |

正文：2026年9月目标为100.00万元，实际为80.00万元，差额为-20.00万元，完成率为80.00%。2026年9月欠款未知，不能据此判断原因；建议复核明细后再决定。
"""


def _export(answer: str = ANSWER) -> tuple[bytes, dict]:
    session = {
        "id": "h02-session",
        "messages": [
            {
                "id": 1,
                "role": "user",
                "content": PROMPT,
                "session_id": "h02-session",
                "active": 1,
            },
            {
                "id": 2,
                "role": "assistant",
                "content": answer,
                "tool_calls": [],
                "session_id": "h02-session",
                "active": 1,
            },
        ],
    }
    payload = (json.dumps(session, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    export_sha = hashlib.sha256(payload).hexdigest()
    bindings = {
        "schema": "datasage-canary-bindings/v2",
        "session_export_sha256": export_sha,
        "turns": [
            {
                "test_id": "answer_fixture_case",
                "conversation_id": "answer_fixture",
                "turn": 1,
                "session_id": "h02-session",
                "user_message_id": 1,
                "final_message_id": 2,
                "canonical_prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            }
        ],
    }
    return payload, bindings


def _ground_truth() -> dict:
    return {
        "schema": GT.GROUND_TRUTH_SCHEMA,
        "fixture_id": "h02-manual-sql-target-fixture",
        "source": {
            "kind": "manual_fixture",
            "reference": "h02-synthetic-manual-arithmetic-fixture-20260919",
            "artifact_sha256": "1" * 64,
        },
        "cases": [
            {
                "case_id": "answer_fixture_case",
                "facts": [
                    {
                        "id": "target",
                        "labels": ["目标"],
                        "value": "100.00",
                        "unit": "万元",
                        "state": "known",
                        "period": "2026-09",
                        "tolerance": "0.01",
                    },
                    {
                        "id": "actual",
                        "labels": ["实际"],
                        "value": "80.00",
                        "unit": "万元",
                        "state": "known",
                        "period": "2026-09",
                        "tolerance": "0.01",
                    },
                    {
                        "id": "gap",
                        "labels": ["差额"],
                        "value": "-20.00",
                        "unit": "万元",
                        "state": "known",
                        "period": "2026-09",
                        "tolerance": "0.01",
                    },
                    {
                        "id": "completion_rate",
                        "labels": ["完成率"],
                        "value": "80.00",
                        "unit": "%",
                        "state": "known",
                        "period": "2026-09",
                        "tolerance": "0.01",
                    },
                    {
                        "id": "debt",
                        "labels": ["欠款"],
                        "value": None,
                        "unit": "万元",
                        "state": "unknown",
                        "period": "2026-09",
                        "tolerance": "0.01",
                    },
                ],
                "arithmetic": [
                    {
                        "id": "gap_from_target_actual",
                        "operation": "subtract",
                        "operands": ["actual", "target"],
                        "expected_fact": "gap",
                        "tolerance": "0.01",
                    },
                    {
                        "id": "completion_from_target_actual",
                        "operation": "divide_percent",
                        "operands": ["actual", "target"],
                        "expected_fact": "completion_rate",
                        "tolerance": "0.01",
                    },
                ],
                "table_text": [
                    {"fact_id": "target", "label": "目标", "require_table": True, "require_text": True},
                    {"fact_id": "actual", "label": "实际", "require_table": True, "require_text": True},
                    {"fact_id": "gap", "label": "差额", "require_table": True, "require_text": True},
                    {"fact_id": "completion_rate", "label": "完成率", "require_table": True, "require_text": True},
                ],
                "review_requirements": {
                    "required_labels": ["report_target_gap", "state_noncausal"],
                    "forbidden_labels": ["claim_causal_driver"],
                    "advice_boundary": "bounded",
                },
            }
        ],
    }


def _review(export_sha: str, final_sha: str, *, causal: bool = False) -> dict:
    labels = ["report_target_gap", "state_noncausal"]
    if causal:
        labels.append("claim_causal_driver")
    return {
        "schema": GT.REVIEW_SCHEMA,
        "source": {
            "kind": "manual_review",
            "reference": "h02-reviewer-01-20260919",
            "artifact_sha256": "2" * 64,
        },
        "session_export_sha256": export_sha,
        "cases": [
            {
                "case_id": "answer_fixture_case",
                "status": "reviewed",
                "reviewer_id": "reviewer-01",
                "labels": labels,
                "advice_boundary": "bounded",
                "final_answer_sha256": final_sha,
            }
        ],
    }


def _suite_and_candidate(answer_sha: str, export_sha: str) -> tuple[dict, dict]:
    case = {
        "id": "answer_fixture_case",
        "category": "target_gap",
        "conversation_id": "answer_fixture",
        "turn": 1,
        "prompt": PROMPT,
        "plan_constraints": {
            "domains": ["target"],
            "metrics": ["delivery_target_completion"],
            "dimensions": ["department"],
            "operations": ["target_actual_gap"],
            "time_semantics": "2026-09",
            "context_action": "new",
            "must_not_metrics": [],
            "domain_metric_pairs": [
                {"domain": "target", "metric": "delivery_target_completion"}
            ],
        },
        "required_conclusions": ["report_target_gap"],
        "allowed_conclusions": ["report_target_gap", "state_noncausal"],
        "forbidden_conclusions": ["claim_causal_driver"],
        "evidence_requirements": {
            "required_receipts": ["catalog", "query", "coverage"],
            "minimum_successful_queries": 1,
            "allow_partial_failure": False,
            "require_untruncated": True,
            "require_reconciled_decomposition": False,
            "must_not_query": False,
            "required_error_codes": [],
        },
    }
    suite = {
        "schema": SCORER.SCHEMA,
        "suite": "h02-answer-fixture",
        "plan_constraint_semantics": "required-and-forbidden-subsets/v1",
        "minimum_case_count": 1,
        "required_category_minimums": {"target_gap": 1},
        "cases": [case],
    }
    profile = {"artifact_id": "a" * 64, "payload_sha256": "b" * 64}
    prompt_sha = hashlib.sha256(PROMPT.encode()).hexdigest()
    watermark = SCORER._sha256(
        {
            "schema": SCORER.WATERMARK_SCHEMA,
            "test_id": case["id"],
            "conversation_id": case["conversation_id"],
            "turn": case["turn"],
            "canonical_prompt_sha256": prompt_sha,
            "user_message_id": 1,
            "session_export_sha256": export_sha,
            "artifact_id": profile["artifact_id"],
            "payload_sha256": profile["payload_sha256"],
        }
    )
    binding = {
        "test_id": case["id"],
        "artifact_id": profile["artifact_id"],
        "payload_sha256": profile["payload_sha256"],
        "session_id": "h02-session",
        "user_message_id": 1,
        "canonical_prompt_sha256": prompt_sha,
        "session_export_sha256": export_sha,
        "watermark_sha256": watermark,
        "final_answer_sha256": answer_sha,
    }
    review_assertion = {
        "status": "reviewed",
        "assertion_sha256": "e" * 64,
        "binding_sha256": SCORER._sha256(binding),
    }
    observed = {
        "id": case["id"],
        "session_id": "h02-session",
        "plan": {
            **copy.deepcopy(case["plan_constraints"]),
            "context_bindings": {},
        },
        "conclusions": ["report_target_gap"],
        "conclusion_review": review_assertion,
        "evidence": {
            "receipts": ["catalog", "query", "coverage"],
            "successful_queries": 1,
            "failed_queries": 0,
            "truncated": False,
            "reconciled": False,
            "query_attempted": True,
            "error_codes": [],
        },
    }
    cases = [observed]
    turn = {
        "test_id": case["id"],
        "conversation_id": case["conversation_id"],
        "turn": 1,
        "session_id": "h02-session",
        "user_message_id": 1,
        "final_message_id": 2,
        "canonical_prompt_sha256": prompt_sha,
        "session_export_sha256": export_sha,
        "watermark_sha256": watermark,
        "final_answer_sha256": answer_sha,
        "candidate_case_sha256": SCORER._sha256(observed),
        "conclusion_review": review_assertion,
    }
    receipt = {
        "schema": SCORER.RECEIPT_SCHEMA,
        "profile_artifact": profile,
        "session_export_sha256": export_sha,
        "source": {
            "platform": "wecom",
            "format": "hermes_sessions_export_jsonl",
            "session_export_sha256": export_sha,
        },
        "candidate_cases_sha256": SCORER._sha256(cases),
        "turns": [turn],
    }
    receipt["receipt_sha256"] = SCORER._sha256(receipt)
    candidate = {
        "schema": SCORER.CANDIDATE_SCHEMA,
        "profile_artifact": profile,
        "session_export_sha256": export_sha,
        "cases": cases,
        "canary_receipt": receipt,
    }
    return suite, candidate


class AnswerGroundTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload, self.bindings = _export()
        self.export_sha = hashlib.sha256(self.payload).hexdigest()
        self.observations = REPLAY.extract_final_answer_observations(
            self.payload, self.bindings
        )
        self.answer_sha = self.observations["answers"][0]["final_answer_sha256"]
        self.ground_truth = _ground_truth()
        self.suite, self.candidate = _suite_and_candidate(
            self.answer_sha, self.export_sha
        )

    def test_official_export_observation_is_hash_and_endpoint_bound(self):
        self.assertEqual(ANSWER, self.observations["answers"][0]["final_answer_text"])
        tampered = copy.deepcopy(self.observations)
        tampered["answers"][0]["final_answer_text"] = ANSWER.replace("80.00 %", "81.00 %")
        with self.assertRaisesRegex(ValueError, "hash"):
            SCORER.score(
                self.suite,
                self.candidate,
                answer_observations=tampered,
                answer_ground_truth=self.ground_truth,
            )

    def test_clean_answer_passes_numeric_arithmetic_unit_and_table_checks(self):
        report = SCORER.score(
            self.suite,
            self.candidate,
            answer_observations=self.observations,
            answer_ground_truth=self.ground_truth,
            answer_review=_review(self.export_sha, self.answer_sha),
        )
        self.assertEqual("passed", report["answer_validation"]["status"])
        self.assertTrue(report["gate"]["passed"])
        result = report["answer_validation"]["results"]["answer_fixture_case"]
        self.assertTrue(all(item["status"] == "passed" for item in result["dimensions"].values()))
        self.assertEqual("verified", report["verification_scope"]["business_values"])
        self.assertEqual("verified", report["verification_scope"]["business_arithmetic"])
        self.assertEqual("verified", report["verification_scope"]["table_text_consistency"])

    def test_wrong_value_and_wrong_unit_are_rejected_even_when_plan_evidence_pass(self):
        wrong_text = ANSWER.replace("-20.00 万元", "-10.00 万元").replace(
            "80.00 万元", "80.00 元"
        )
        payload, bindings = _export(wrong_text)
        observations = REPLAY.extract_final_answer_observations(payload, bindings)
        suite, candidate = _suite_and_candidate(
            observations["answers"][0]["final_answer_sha256"],
            hashlib.sha256(payload).hexdigest(),
        )
        report = SCORER.score(
            suite,
            candidate,
            answer_observations=observations,
            answer_ground_truth=self.ground_truth,
            answer_review=_review(
                hashlib.sha256(payload).hexdigest(),
                observations["answers"][0]["final_answer_sha256"],
            ),
        )
        result = report["answer_validation"]["results"]["answer_fixture_case"]
        self.assertEqual("failed", report["answer_validation"]["status"])
        self.assertEqual("failed", result["dimensions"]["numbers"]["status"])
        self.assertEqual("failed", result["dimensions"]["units"]["status"])
        self.assertEqual("failed", result["dimensions"]["arithmetic"]["status"])
        self.assertFalse(report["gate"]["passed"])

    def test_unknown_value_must_remain_unknown_and_not_be_filled_with_zero(self):
        zero_text = ANSWER.replace("未知（未提供）万元", "0.00 万元")
        payload, bindings = _export(zero_text)
        observations = REPLAY.extract_final_answer_observations(payload, bindings)
        result = GT.score_answer_case(
            GT.validate_ground_truth(self.ground_truth)["cases"][0],
            observations["answers"][0]["final_answer_text"],
        )
        self.assertEqual("failed", result["dimensions"]["numbers"]["status"])
        self.assertTrue(
            any("unknown" in error for error in result["dimensions"]["numbers"]["errors"])
        )

    def test_attached_multiple_numbers_remain_unverified_instead_of_guessing(self):
        ambiguous = ANSWER.replace(
            "实际为80.00万元，差额为-20.00万元",
            "实际为80.00万元（目标100.00万元），差额为-20.00万元",
        )
        result = GT.score_answer_case(
            GT.validate_ground_truth(self.ground_truth)["cases"][0], ambiguous
        )
        self.assertEqual("not_verified", result["dimensions"]["numbers"]["status"])
        self.assertTrue(
            any("multiple numeric values" in error for error in result["dimensions"]["numbers"]["errors"])
        )

    def test_thousands_separator_stays_one_numeric_value(self):
        fact = copy.deepcopy(
            GT.validate_ground_truth(self.ground_truth)["cases"][0]["facts"][0]
        )
        fact["value"] = "1000.00"
        occurrence = GT._fact_occurrence("目标为1,000.00万元", fact)
        self.assertFalse(occurrence["ambiguous"])
        self.assertEqual([1000], [int(value) for value in occurrence["numbers"]])

    def test_unicode_normalization_cannot_truncate_the_bound_value(self):
        case = {
            "case_id": "unicode-value-binding",
            "facts": [{"id": "income", "labels": ["收入"], "value": "0",
                       "unit": "元", "state": "known", "period": None, "tolerance": "0"}],
            "arithmetic": [], "table_text": [], "review_requirements": None,
        }
        # These characters expand under NFKC. A position from normalized text
        # must never slice the original text and turn the literal 100 into 00.
        for prefix in ("", "㍿", "㎡㎡㎡"):
            with self.subTest(prefix=prefix):
                result = GT.score_answer_case(case, prefix + "收入100元。")
                self.assertEqual("failed", result["dimensions"]["numbers"]["status"])
                expected = copy.deepcopy(case)
                expected["facts"][0]["value"] = "100"
                correct = GT.score_answer_case(expected, prefix + "收入１００元。")
                self.assertEqual("passed", correct["dimensions"]["numbers"]["status"])

    def test_period_must_be_bound_to_the_fact_statement(self):
        fact = {
            "id": "income",
            "labels": ["收入"],
            "value": "100.00",
            "unit": "元",
            "state": "known",
            "period": "2026-09",
            "tolerance": "0.01",
        }
        case = {
            "case_id": "period-binding",
            "facts": [fact],
            "arithmetic": [],
            "table_text": [],
            "review_requirements": None,
        }
        wrong_period = "2026年8月收入为100.00元。2026年9月只讨论库存。"
        wrong_result = GT.score_answer_case(case, wrong_period)
        self.assertEqual("failed", wrong_result["dimensions"]["numbers"]["status"])
        unbound_period = "收入为100.00元。2026年9月只讨论库存。"
        unbound_result = GT.score_answer_case(case, unbound_period)
        self.assertEqual("not_verified", unbound_result["dimensions"]["numbers"]["status"])

    def test_governed_short_units_allow_numeric_adjacency_but_not_ordinary_words(self):
        self.assertEqual((True, False), GT._line_unit_ok("数量100y", "y"))
        self.assertEqual((True, False), GT._line_unit_ok("数量12PCS", "Pcs"))
        self.assertEqual((True, False), GT._line_unit_ok("数量2m2", "m2"))
        self.assertEqual((True, False), GT._line_unit_ok("数量3m", "m"))
        self.assertEqual((False, False), GT._line_unit_ok("amount 100", "m"))
        self.assertEqual((False, False), GT._line_unit_ok("数量100m3", "m"))

    def test_same_label_conflict_and_mixed_units_fail_even_when_one_occurrence_is_correct(self):
        conflicting = ANSWER.replace(
            "实际为80.00万元，差额为-20.00万元",
            "实际为81.00元，差额为-20.00万元",
        )
        result = GT.score_answer_case(
            GT.validate_ground_truth(self.ground_truth)["cases"][0], conflicting
        )
        self.assertEqual("failed", result["dimensions"]["numbers"]["status"])
        self.assertEqual("failed", result["dimensions"]["units"]["status"])
        self.assertEqual("failed", result["dimensions"]["table_text_consistency"]["status"])

    def test_empty_arithmetic_and_table_assertions_remain_unverified(self):
        ground_truth = GT.validate_ground_truth(self.ground_truth)
        ground_truth["cases"][0]["arithmetic"] = []
        ground_truth["cases"][0]["table_text"] = []
        result = GT.score_answer_case(ground_truth["cases"][0], ANSWER)
        self.assertEqual("not_verified", result["dimensions"]["arithmetic"]["status"])
        self.assertEqual(
            "not_verified",
            result["dimensions"]["table_text_consistency"]["status"],
        )

    def test_causal_boundary_requires_independent_review_and_does_not_use_keyword_blacklist(self):
        no_review = SCORER.score(
            self.suite,
            self.candidate,
            answer_observations=self.observations,
            answer_ground_truth=self.ground_truth,
        )
        self.assertEqual(
            "not_verified",
            no_review["answer_validation"]["results"]["answer_fixture_case"]["dimensions"][
                "conclusion_boundary"
            ]["status"],
        )
        causal = SCORER.score(
            self.suite,
            self.candidate,
            answer_observations=self.observations,
            answer_ground_truth=self.ground_truth,
            answer_review=_review(self.export_sha, self.answer_sha, causal=True),
        )
        self.assertEqual("failed", causal["answer_validation"]["status"])
        self.assertEqual(
            "failed",
            causal["answer_validation"]["results"]["answer_fixture_case"]["dimensions"][
                "conclusion_boundary"
            ]["status"],
        )


if __name__ == "__main__":
    unittest.main()
