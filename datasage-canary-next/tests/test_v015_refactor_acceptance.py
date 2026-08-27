"""Frozen acceptance tests for the v0.15 architecture stabilization.

These tests intentionally assert architectural boundaries, not a model's exact
tool plan or final wording.  Tests that need Hermes message assembly live in the
host-compaction fixture and are not claimed as Profile-owned fixes here.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
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
                "mode": "metric",
                "metric": "contract_probe_metric",
                "purpose": "offline schema/runtime equivalence probe",
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


class ReleaseAndHostBoundaryAcceptanceTests(unittest.TestCase):
    def test_release_identity_is_computed_from_candidate_content(self):
        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        config = yaml.safe_load(
            (PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        plugin = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        skill_text = (
            PROFILE_ROOT
            / "skills"
            / "business-analytics"
            / "datasage"
            / "SKILL.md"
        ).read_text(
            encoding="utf-8"
        )
        _, skill_frontmatter, _ = skill_text.split("---", 2)
        skill = yaml.safe_load(skill_frontmatter)
        self.assertEqual(str(distribution["version"]), str(plugin["version"]))
        self.assertEqual(str(distribution["version"]), str(skill["version"]))
        self.assertEqual("source" in distribution, "installed_at" in distribution)
        if "source" in distribution:
            self.assertTrue(str(distribution["source"]).strip())
            self.assertTrue(str(distribution["installed_at"]).strip())

        identity_inputs = {
            "profile_version": str(distribution["version"]),
            "hermes_requires": distribution["hermes_requires"],
            "model_provider": config["model"]["provider"],
            "model_default": config["model"]["default"],
            "reasoning_effort": config["agent"]["reasoning_effort"],
            "capability_contract_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "capability_contract.py").read_bytes()
            ).hexdigest(),
            "schema_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "schemas.py").read_bytes()
            ).hexdigest(),
            "golden_suite_sha256": hashlib.sha256(
                (PLUGIN_ROOT / "e2e" / "golden_expert_cases.json").read_bytes()
            ).hexdigest(),
        }
        release_identity = hashlib.sha256(
            json.dumps(
                identity_inputs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertRegex(release_identity, r"^[0-9a-f]{64}$")

    def test_release_receipt_normalizes_hermes_install_metadata(self):
        builder_path = PROFILE_ROOT / "build_release_receipt.py"
        spec = importlib.util.spec_from_file_location(
            "datasage_release_receipt_builder", builder_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        distribution = yaml.safe_load(
            (PROFILE_ROOT / "distribution.yaml").read_text(encoding="utf-8")
        )
        installed = dict(distribution)
        installed.update(
            {
                "name": "isolated-install-name",
                "source": "C:/isolated/source",
                "installed_at": "2026-08-24T00:00:00+00:00",
            }
        )
        source = {
            key: value
            for key, value in distribution.items()
            if key not in builder.INSTALLER_MANIFEST_FIELDS
        }
        self.assertEqual(
            builder._canonical_manifest_bytes(source),
            builder._canonical_manifest_bytes(installed),
        )

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
        self.assertEqual("expected_failure_before_host_fix", fixture["pre_fix_state"])
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


if __name__ == "__main__":
    unittest.main()
