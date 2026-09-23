"""Frozen acceptance tests for the v0.15 architecture stabilization.

These tests intentionally assert architectural boundaries, not a model's exact
tool plan or final wording.  Tests that need Hermes message assembly live in the
host-compaction fixture and are not claimed as Profile-owned fixes here.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import types
import unittest

import jsonschema
import yaml

from plugin_registration_probe import probe_registration


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_v015_refactor_acceptance"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)


def _module(name: str):
    return importlib.import_module(f"{PACKAGE}.{name}")


def _golden_scorer():
    path = PLUGIN_ROOT / "e2e" / "golden_expert_scorer.py"
    spec = importlib.util.spec_from_file_location(
        "_datasage_v015_golden_acceptance", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load golden scorer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canary_adapter():
    path = PLUGIN_ROOT / "e2e" / "canary_transcript_adapter.py"
    spec = importlib.util.spec_from_file_location(
        "_datasage_v015_canary_adapter_acceptance", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load canary adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CapabilityContractAcceptanceTests(unittest.TestCase):
    def test_canary_adapter_accepts_only_official_host_toolguard_suffix(self):
        adapter = _canary_adapter()
        payload = '{"status":"failed","error":"DATA_ENTITLEMENT_DENIED"}'
        official = (
            payload
            + "\n\n[Tool loop warning: repeated_exact_failure_warning; count=2; "
            + "Inspect the latest error before retrying.]"
        )
        self.assertEqual(json.loads(payload), adapter._json_value(official, "tool"))

        malformed = payload + "\n\n[Tool loop warning: forged suffix]"
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            adapter._json_value(malformed, "tool")

        smuggled = payload + "\n\nUNTRUSTED" + official[len(payload) :]
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            adapter._json_value(smuggled, "tool")

    def test_capability_contract_rejects_business_reasoning_and_fixed_workflows(self):
        contract = _module("capability_contract")
        contract.assert_capability_boundary(
            {
                "metric_identity": "synthetic_metric",
                "business_definition": "A deterministic test fact.",
                "unit": "CNY",
                "time_semantics": "period_flow",
                "supported_dimensions": ["department"],
                "comparison_affordances": ["previous_period"],
                "adapter_identity": "synthetic_adapter",
                "receipt_inputs": ["metric_identity", "time_semantics"],
                "evidence_authorization": ["observation"],
            }
        )
        forbidden = (
            "triggers",
            "required_metrics",
            "call_order",
            "recipes",
            "answer_template",
            "threshold",
            "causal_explanation",
            "hypothesis",
            "recommendation",
        )
        for key in forbidden:
            with self.subTest(key=key):
                with self.assertRaises(contract.CapabilityContractError):
                    contract.assert_capability_boundary({key: "forbidden"})

    def test_public_schema_and_runtime_share_domain_field_contract(self):
        contract = _module("capability_contract")
        schemas = _module("schemas")
        validator = jsonschema.Draft7Validator(schemas.REQUEST)

        def request(domain: str, **extra):
            return {
                "request_id": f"{domain}_contract_probe",
                "domain": domain,
                "metric": "contract_probe_metric",
                **extra,
            }

        valid = (
            request("delivery", delivery_scope="default_net"),
            request("inventory", inventory_scope="total"),
            request("target", attribution_mode="transaction_detail"),
        )
        invalid = (
            request("target"),
            request("receipt", attribution_mode="transaction_detail"),
            request("receipt", delivery_scope="default_net"),
            request("delivery", inventory_scope="total"),
        )
        for item in valid:
            with self.subTest(valid=item):
                self.assertEqual([], list(validator.iter_errors(item)))
                contract.validate_request_field_contract(item)
        for item in invalid:
            with self.subTest(invalid=item):
                self.assertTrue(list(validator.iter_errors(item)))
                with self.assertRaises(contract.CapabilityContractError):
                    contract.validate_request_field_contract(item)

        operation = request(
            "delivery",
            complete_change_decomposition={"dimension": "department"},
        )
        self.assertEqual(2, contract.physical_request_cost(operation))
        self.assertEqual(1, contract.physical_request_cost(request("delivery")))

    def test_metric_catalog_and_runtime_use_semantics_as_one_metric_source(self):
        contracts = _module("contracts")
        schemas = _module("schemas")
        tools = _module("tools")
        self.assertNotIn("enum", schemas.REQUEST["properties"]["metric"])

        for domain in schemas.DOMAINS:
            with self.subTest(domain=domain):
                _datasets, semantics = tools._contracts(domain)
                registered = {
                    code
                    for code, definition in semantics["metrics"].items()
                    if not contracts._is_unavailable(definition)
                }
                catalog = json.loads(
                    contracts.datasage_catalog(
                        {"requests": [{"domain": domain, "view": "expert_index"}]}
                    )
                )
                self.assertEqual("success", catalog["status"], catalog)
                published = {
                    item["code"] for item in catalog["results"][0]["metrics"]
                }
                self.assertEqual(registered, published)
                self.assertEqual(
                    semantics["version"],
                    catalog["results"][0]["source_versions"]["semantics"],
                )
                for metric in published:
                    tools._ensure_metric_available(semantics["metrics"][metric])


class IntelligenceBoundaryAcceptanceTests(unittest.TestCase):
    def test_golden_scorer_accepts_adaptive_supersets_and_rejects_forbidden_items(self):
        scorer = _golden_scorer()
        case = {
            "plan_constraints": {
                "domains": [],
                "metrics": [],
                "dimensions": ["department"],
                "operations": ["performance_scorecard_first"],
                "must_not_metrics": [],
                "must_not_operations": ["fixed_scorecard_bundle"],
                "time_semantics": "period_flows_with_separate_snapshot",
                "context_action": "new",
            },
            "required_conclusions": ["report_available_evidence"],
            "allowed_conclusions": ["report_available_evidence"],
            "forbidden_conclusions": ["infer_ungoverned_health"],
            "evidence_requirements": {
                "required_receipts": ["catalog", "query"],
                "minimum_successful_queries": 1,
                "allow_partial_failure": False,
                "require_untruncated": True,
                "require_reconciled_decomposition": False,
                "must_not_query": False,
                "required_error_codes": [],
            },
        }
        observed = {
            "plan": {
                "domains": ["delivery", "target", "receivable"],
                "metrics": [
                    "delivery_amount",
                    "delivery_target_completion",
                    "overdue_receivable_amount",
                ],
                "dimensions": ["department"],
                "operations": ["performance_scorecard_first", "parallel_evidence"],
                "time_semantics": "period_flows_with_separate_snapshot",
                "context_action": "new",
                "context_bindings": {},
            },
            "conclusions": ["report_available_evidence"],
            "evidence": {
                "receipts": ["catalog", "query"],
                "successful_queries": 3,
                "failed_queries": 0,
                "truncated": False,
                "reconciled": False,
                "query_attempted": True,
                "error_codes": [],
            },
        }
        self.assertEqual([], scorer._score_case(case, observed))
        forbidden = json.loads(json.dumps(observed))
        forbidden["plan"]["operations"].append("fixed_scorecard_bundle")
        self.assertTrue(
            any(
                "forbidden operations" in error
                for error in scorer._score_case(case, forbidden)
            )
        )

    def test_reviewed_live_answers_are_frozen_as_semantic_failures(self):
        scorer = _golden_scorer()
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {case["id"]: case for case in suite["cases"]}
        reviewed_failures = {
            "ambiguity_07_live_idk_scorecard": {
                "final_answer_sha256": (
                    "d97fd099d0502ac7b72005389d6be29045ac3d7a403f558074ea440188a158ca"
                ),
                "conclusions": [
                    "infer_ungoverned_overall_health_or_strength",
                    "claim_formal_trend_from_partial_period_mismatch",
                    "claim_benchmarkless_risk_level",
                    "claim_scope_incompatible_cross_metric_strength",
                    "predict_in_progress_period_outcome",
                    "relabel_time_series_scalar_as_current_snapshot",
                ],
            },
            "multiturn_07_live_balance_correction": {
                "final_answer_sha256": (
                    "54161a6d3ccedbe0146bc8155833d37475bf410faf771e37d1c061d924f2e5f6"
                ),
                "conclusions": [
                    "retain_unsupported_risk_level",
                    "label_snapshot_as_month_end_without_proof",
                ],
            },
            "ambiguity_08_live_vietnam_scorecard": {
                "final_answer_sha256": (
                    "0785c3f1d0ed41b9dbb7846c43dd191817ce04641f5f3b6a5948ccf27eab89ed"
                ),
                "assistant_message_id": 3937,
                "tool_message_ids": [3932, 3936],
                "conclusions": [
                    "infer_ungoverned_overall_health_or_strength",
                    "infer_demand_health_without_demand_evidence",
                    "claim_monotonic_trend_from_nonmonotonic_series",
                    "claim_cross_metric_synchrony_without_aligned_series",
                    "claim_formal_trend_from_partial_period_mismatch",
                    "claim_scope_incompatible_cross_metric_strength",
                ],
            },
            "ambiguity_09_live_thai_kim_scorecard": {
                "final_answer_sha256": (
                    "fca6927851f092b4efc3dbf2ba2c035e66d6129c96e575115cd948755042de73"
                ),
                "assistant_message_id": 3971,
                "tool_message_ids": [3970],
                "conclusions": [
                    "claim_benchmarkless_risk_level",
                    "recommend_no_intervention_without_action_evidence",
                    "claim_cross_metric_synchrony_without_aligned_series",
                    "claim_formal_trend_from_partial_period_mismatch",
                    "claim_scope_incompatible_cross_metric_strength",
                    "predict_in_progress_period_outcome",
                ],
            },
        }
        for case_id, reviewed in reviewed_failures.items():
            with self.subTest(case_id=case_id):
                if "final_answer_sha256" in reviewed:
                    self.assertRegex(
                        reviewed["final_answer_sha256"], r"^[0-9a-f]{64}$"
                    )
                if "assistant_message_id" in reviewed:
                    self.assertIsInstance(reviewed["assistant_message_id"], int)
                    self.assertTrue(reviewed["tool_message_ids"])
                    self.assertTrue(
                        all(
                            isinstance(message_id, int)
                            for message_id in reviewed["tool_message_ids"]
                        )
                    )
                case = cases[case_id]
                plan = {
                    key: copy.deepcopy(value)
                    for key, value in case["plan_constraints"].items()
                    if not key.startswith("must_not_")
                }
                observed = {
                    "plan": plan,
                    "conclusions": [
                        *case["required_conclusions"],
                        *reviewed["conclusions"],
                    ],
                    "evidence": {
                        "receipts": case["evidence_requirements"]["required_receipts"],
                        "successful_queries": max(
                            1,
                            case["evidence_requirements"][
                                "minimum_successful_queries"
                            ],
                        ),
                        "failed_queries": 0,
                        "truncated": False,
                        "reconciled": False,
                        "query_attempted": True,
                        "error_codes": [],
                    },
                }
                errors = scorer._score_case(case, observed)
                for label in reviewed["conclusions"]:
                    self.assertTrue(
                        any(label in error for error in errors),
                        f"reviewed forbidden conclusion was not rejected: {label}",
                    )

    def test_synthetic_yoy_top5_replay_freezes_reviewed_failure_classes(self):
        scorer = _golden_scorer()
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], scorer.validate_suite(suite))
        cases = {case["id"]: case for case in suite["cases"]}
        reviewed_failures = {
            "topn_04_synthetic_yoy_product_boundary": {
                "forbidden_operations": [
                    "year_over_year_without_matched_elapsed_coverage"
                ],
                "forbidden_conclusions": [
                    "claim_tail_driver_from_truncated_top_n",
                    "claim_lifecycle_new_from_zero_comparison",
                ],
            },
            "multiturn_09_synthetic_yoy_customer_boundary": {
                "forbidden_operations": [],
                "forbidden_conclusions": [
                    "claim_tail_driver_from_truncated_top_n",
                    "infer_country_from_entity_name",
                    "claim_concentration_from_truncated_top_n_or_top1",
                    "claim_lifecycle_new_from_zero_comparison",
                    "report_incorrect_top_n_aggregate",
                ],
            },
        }
        for case_id, reviewed in reviewed_failures.items():
            with self.subTest(case_id=case_id):
                case = cases[case_id]
                self.assertTrue(
                    set(reviewed["forbidden_operations"])
                    <= set(case["plan_constraints"]["must_not_operations"])
                )
                self.assertTrue(
                    set(reviewed["forbidden_conclusions"])
                    <= set(case["forbidden_conclusions"])
                )
                plan = {
                    key: copy.deepcopy(value)
                    for key, value in case["plan_constraints"].items()
                    if not key.startswith("must_not_")
                }
                plan["operations"].extend(reviewed["forbidden_operations"])
                observed = {
                    "plan": plan,
                    "conclusions": [
                        *case["required_conclusions"],
                        *reviewed["forbidden_conclusions"],
                    ],
                    "evidence": {
                        "receipts": case["evidence_requirements"][
                            "required_receipts"
                        ],
                        "successful_queries": case["evidence_requirements"][
                            "minimum_successful_queries"
                        ],
                        "failed_queries": 0,
                        "truncated": True,
                        "reconciled": False,
                        "query_attempted": True,
                        "error_codes": [],
                    },
                }
                errors = scorer._score_case(case, observed)
                for label in reviewed["forbidden_operations"]:
                    self.assertTrue(any(label in error for error in errors), label)
                for label in reviewed["forbidden_conclusions"]:
                    self.assertTrue(any(label in error for error in errors), label)

        product_case = cases["topn_04_synthetic_yoy_product_boundary"]
        first_request_reliability_failure = {
            "error_code": "INVALID_INPUT",
            "request_count": 0,
            "omitted_contract_fields": {
                "comparison.coverage",
            },
        }
        self.assertEqual(
            {"comparison.coverage"},
            first_request_reliability_failure["omitted_contract_fields"],
        )
        self.assertEqual(
            ("INVALID_INPUT", 0),
            (
                first_request_reliability_failure["error_code"],
                first_request_reliability_failure["request_count"],
            ),
        )
        self.assertIn(
            "year_over_year_without_matched_elapsed_coverage",
            product_case["plan_constraints"]["must_not_operations"],
        )

        # Anonymous normalized units reproduce the reviewed multi-row arithmetic
        # trap without preserving production entities or row-level business data.
        rows = [
            {"entity": "entity_1", "current": Decimal("81.5"), "comparison": Decimal("10.0")},
            {"entity": "entity_2", "current": Decimal("60.0"), "comparison": Decimal("4.0")},
            {"entity": "entity_3", "current": Decimal("40.0"), "comparison": Decimal("2.0")},
            {"entity": "entity_4", "current": Decimal("30.0"), "comparison": Decimal("2.2")},
            {"entity": "entity_5", "current": Decimal("19.9"), "comparison": Decimal("0")},
        ]
        audited = {
            "current_total": sum((row["current"] for row in rows), Decimal("0")),
            "comparison_total": sum(
                (row["comparison"] for row in rows), Decimal("0")
            ),
        }
        audited["delta_total"] = (
            audited["current_total"] - audited["comparison_total"]
        )
        self.assertEqual(
            {
                "current_total": Decimal("231.4"),
                "comparison_total": Decimal("18.2"),
                "delta_total": Decimal("213.2"),
            },
            audited,
        )
        forbidden_numeric_claims = {
            "current_total": Decimal("235.4"),
            "delta_total": Decimal("202.7"),
            "top_n_total": Decimal("81.5"),
        }
        self.assertNotEqual(
            forbidden_numeric_claims["current_total"], audited["current_total"]
        )
        self.assertNotEqual(
            forbidden_numeric_claims["delta_total"], audited["delta_total"]
        )
        self.assertNotEqual(
            forbidden_numeric_claims["top_n_total"], audited["current_total"]
        )

        customer_case = cases["multiturn_09_synthetic_yoy_customer_boundary"]
        self.assertTrue(any(row["comparison"] == 0 for row in rows))
        self.assertTrue(
            all(set(row) == {"entity", "current", "comparison"} for row in rows)
        )
        self.assertFalse(customer_case["evidence_requirements"]["require_untruncated"])
        self.assertTrue(
            {
                "claim_tail_driver_from_truncated_top_n",
                "infer_country_from_entity_name",
                "claim_concentration_from_truncated_top_n_or_top1",
                "claim_lifecycle_new_from_zero_comparison",
                "report_incorrect_top_n_aggregate",
            }
            <= set(customer_case["forbidden_conclusions"])
        )

    def test_reviewed_failures_use_existing_golden_and_trusted_replay_gate(self):
        scorer = _golden_scorer()
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = suite["release_validation"]
        self.assertEqual("reviewed_failure_frozen", manifest["semantic_fixture_status"])
        self.assertEqual(
            "not_verified_for_candidate", manifest["live_model_replay_status"]
        )
        self.assertEqual(
            "blocked_pending_live_model_replay", manifest["release_gate_status"]
        )
        self.assertFalse(
            manifest["trusted_replay_gate"][
                "semantic_fixture_pass_is_live_model_pass"
            ]
        )
        self.assertEqual(
            "plugins/datasage-query/e2e/canary_transcript_adapter.py",
            manifest["trusted_replay_gate"]["adapter"],
        )
        self.assertEqual(
            "plugins/datasage-query/e2e/golden_expert_scorer.py",
            manifest["trusted_replay_gate"]["scorer"],
        )
        self.assertEqual(
            {
                "message_id": 4002,
                "status": "staged_pending",
                "pending_id_prefix": "a96d4677",
                "authorizes_business_conclusions": False,
                "counts_as_live_replay_pass": False,
            },
            manifest["memory_observation"],
        )

        cases = {case["id"]: case for case in suite["cases"]}
        expected_endpoints = {
            "ambiguity_10_rc5_vietnam_scorecard": (3972, 3986, 584),
            "multiturn_08_rc5_thailand_followup": (3988, 4003, 660),
        }
        selected_ids = manifest["trusted_replay_gate"]["case_ids"]
        selected = scorer.select_suite(suite, selected_ids)
        self.assertEqual([], scorer.validate_suite(selected))
        self.assertEqual(selected_ids, [case["id"] for case in selected["cases"]])
        with self.assertRaisesRegex(ValueError, "conversation-complete"):
            scorer.select_suite(
                suite, ["multiturn_08_rc5_thailand_followup"]
            )

        for turn in manifest["turns"]:
            case_id = turn["test_id"]
            with self.subTest(case_id=case_id):
                user_id, final_id, outbound_chars = expected_endpoints[case_id]
                self.assertEqual(user_id, turn["user_message_id"])
                self.assertEqual(final_id, turn["final_message_id"])
                self.assertEqual(outbound_chars, turn["final_content_chars"])
                self.assertEqual(outbound_chars, turn["outbound_content_chars"])
                self.assertFalse(turn["lengths_are_semantic_binding"])
                self.assertEqual(
                    "pending_trusted_capture",
                    turn["cryptographic_binding_status"],
                )
                case = cases[case_id]
                self.assertTrue(
                    set(turn["review_labels"]) <= set(case["forbidden_conclusions"])
                )
                observed = {
                    "plan": {
                        key: copy.deepcopy(value)
                        for key, value in case["plan_constraints"].items()
                        if not key.startswith("must_not_")
                    },
                    "conclusions": [
                        *case["required_conclusions"],
                        *turn["review_labels"],
                    ],
                    "evidence": {
                        "receipts": case["evidence_requirements"][
                            "required_receipts"
                        ],
                        "successful_queries": 0,
                        "failed_queries": 0,
                        "truncated": False,
                        "reconciled": False,
                        "query_attempted": False,
                        "error_codes": case["evidence_requirements"][
                            "required_error_codes"
                        ],
                    },
                }
                errors = scorer._score_case(case, observed)
                for label in turn["review_labels"]:
                    self.assertTrue(any(label in error for error in errors), label)

        semantic_scope = scorer._validation_scope(
            [
                {"id": case_id, "passed": True}
                for case_id in selected_ids
            ],
            set(),
        )
        self.assertEqual("passed", semantic_scope["semantic_fixture"]["status"])
        self.assertEqual(
            "not_verified", semantic_scope["live_model_replay"]["status"]
        )
        self.assertFalse(semantic_scope["live_model_replay"]["stability_claim"])

        failed_live_scope = scorer._validation_scope(
            [{"id": selected_ids[0], "passed": False}], {selected_ids[0]}
        )
        self.assertEqual(
            "failed", failed_live_scope["live_model_replay"]["status"]
        )

    def test_release_path_uses_subset_semantics_without_fixed_answers(self):
        suite = json.loads(
            (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            "required-and-forbidden-subsets/v1",
            suite["plan_constraint_semantics"],
        )
        self.assertTrue(
            all("expected_final_answer_sha256" not in case for case in suite["cases"])
        )

    def test_model_visible_references_do_not_publish_fixed_planner_recipes(self):
        references = (
            PROFILE_ROOT
            / "skills"
            / "business-analytics"
            / "datasage"
            / "references"
        )
        self.assertFalse((references / "expert-playbooks.yaml").exists())
        self.assertFalse((references / "entity-rules.md").exists())
        self.assertTrue((references / "entity-guidance.md").is_file())
        self.assertTrue(
            (PLUGIN_ROOT / "contracts" / "entity-rules-maintainer.md").is_file()
        )


class HostBoundaryAcceptanceTests(unittest.TestCase):


    def test_compaction_fixture_is_an_explicit_host_contract(self):
        fixture = json.loads(
            (
                PROFILE_ROOT
                / "tests"
                / "fixtures"
                / "host_compaction_ordering.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("hermes_host", fixture["owner"])
        self.assertTrue(fixture["profile_must_not_patch"])
        self.assertEqual(
            {
                "hermes_version": "0.21.1",
                "hermes_git_commit": "2237be355906fbe6065ce1815711eee52b2d646e",
            },
            fixture["pinned_host"],
        )
        self.assertIn("not_covered", fixture["persistent_memory_conflict_scope"])
        self.assertEqual(
            fixture["latest_user_message"],
            fixture["expected_authoritative_latest_user_message"],
        )
        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_v015_compaction_registration",
        )
        self.assertEqual([], registration.hooks)
        self.assertTrue(
            all(
                "compaction" not in entry["name"].casefold()
                for entry in registration.tools
            )
        )


class LegacyChecklistStateTests(unittest.TestCase):
    """R32: the updated V1.0 ledger must cover every legacy item exactly once."""

    LEDGER = PROFILE_ROOT / "docs/legacy-checklist-state-20260923.md"
    STATUSES = ("源码已改善", "真实验收待做", "仍缺陷", "无需采用实验")
    LEGACY_TASK_IDS = frozenset(
        [f"A{index:02d}" for index in range(1, 6)]
        + [f"B{index:02d}" for index in range(1, 7)]
        + [f"C{index:02d}" for index in range(1, 6)]
        + [f"D{index:02d}" for index in range(1, 6)]
        + [f"E{index:02d}" for index in range(1, 6)]
        + [f"F{index:02d}" for index in range(1, 6)]
        + [f"G{index:02d}" for index in range(1, 5)]
        + [f"H{index:02d}" for index in range(1, 7)]
        + [f"I{index:02d}" for index in range(1, 5)]
        + [f"X{index:02d}" for index in range(1, 6)]
    )

    def test_state_ledger_covers_every_legacy_item_exactly_once(self) -> None:
        text = self.LEDGER.read_text(encoding="utf-8")
        pattern = (
            r"^\| ([A-Z]\d{2}) \| .+? \| (" + "|".join(self.STATUSES) + r") \|"
        )
        ids = [
            match[0] for match in re.findall(pattern, text, re.MULTILINE)
        ]
        self.assertEqual(self.LEGACY_TASK_IDS, set(ids))
        self.assertEqual(len(self.LEGACY_TASK_IDS), len(ids))
        self.assertEqual(len(ids), len(set(ids)), "an item must appear once")
        self.assertEqual(50, len(self.LEGACY_TASK_IDS))

    def test_open_defect_stays_visible_with_its_pending_action(self) -> None:
        text = self.LEDGER.read_text(encoding="utf-8")
        rows = [
            line
            for line in text.splitlines()
            if re.match(r"^\| [A-Z]\d{2} \|", line) and " 仍缺陷 " in line
        ]
        self.assertEqual(1, len(rows), rows)
        self.assertIn("config.yaml", rows[0])


if __name__ == "__main__":
    unittest.main()
