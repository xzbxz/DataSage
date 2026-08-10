from __future__ import annotations

import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft7Validator


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "datasage_query_test_package"
os.environ["HERMES_HOME"] = str(PLUGIN_DIR.parents[1])


def _load_package_module(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


evidence = _load_package_module("evidence")
contracts = _load_package_module("contracts")
schemas = _load_package_module("schemas")
tools = _load_package_module("tools")

EXPECTED_CROSS_EVIDENCE_GUARDRAILS = {
    "cross_metric_common_cause_or_contribution": "not_authorized",
    "joint_or_systemic_inference_from_independent_marginals": "not_authorized",
}


def _request(request_id: str, role: str | None = None) -> dict:
    request = {
        "request_id": request_id,
        "domain": "delivery",
        "mode": "metric",
        "purpose": "offline contract test",
        "metric": "net_delivery_amount",
        "analysis_intent": "performance_review",
    }
    if role is not None:
        request["evidence_role"] = role
    return request


def _result(
    request_id: str,
    *,
    status: str = "success",
    data_state: str = "rows",
    truncated: bool = False,
    relations: list[str] | None = None,
    reconciled: bool = False,
) -> dict:
    claim_ledger = []
    if relations is not None:
        claim = {
            "request_id": request_id,
            "allowed_relations": relations,
            "facts": {"secret": 1},
        }
        if set(relations) & {"structural_contribution", "change_driver"}:
            claim["facts"]["net_change_contribution_rate"] = 1
            claim["relation_semantics"] = {
                "structural_contribution": "structural_not_causal"
            }
        evidence.seal_claim(claim)
        claim_ledger.append(claim)
    result = {
        "request_id": request_id,
        "status": status,
        "data_state": data_state,
        "truncated": truncated,
        "claim_ledger": claim_ledger,
        "allowed_reasoning_topics": ["demand_timing"],
        "change_reconciliation": None,
        "error": {"code": "TEST_FAILURE"} if status != "success" else None,
    }
    if reconciled:
        claim_ids = [claim["claim_id"] for claim in claim_ledger]
        structural_ids = [
            claim["claim_id"]
            for claim in claim_ledger
            if set(claim.get("allowed_relations") or [])
            & {"structural_contribution", "change_driver"}
        ]
        result["change_reconciliation"] = {
            "status": "reconciled",
            "overall_request_id": f"{request_id}-overall",
            "overall_claim_id": "claim_00000000000000000000",
            "partition_claim_ids": claim_ids,
            "driver_claim_ids": structural_ids,
            "nonzero_driver_count": len(structural_ids),
        }
        evidence.seal_reconciliation(result["change_reconciliation"])
    return result


class EvidenceBundleTests(unittest.TestCase):
    def test_model_schema_hides_legacy_intent_but_keeps_role_contract(self) -> None:
        properties = schemas.REQUEST["properties"]
        self.assertNotIn("analysis_intent", properties)
        self.assertEqual(
            properties["evidence_role"]["enum"],
            list(evidence.EVIDENCE_ROLES),
        )
        self.assertIn("analysis_intent", tools._SCOPE_PRESENTATION_KEYS)
        self.assertIn("evidence_role", tools._SCOPE_PRESENTATION_KEYS)
        legacy_request = _request("q1", "comparison")
        validator = Draft7Validator(schemas.REQUEST)
        self.assertFalse(validator.is_valid(legacy_request))
        self.assertEqual(tools._validate_request(legacy_request)["request_id"], "q1")

    def test_runtime_rejects_invalid_expert_metadata(self) -> None:
        request = _request("q1", "comparison")
        request["analysis_intent"] = "invented_intent"
        with self.assertRaises(tools.QueryFailure):
            tools._validate_request(request)

        crossed = _request("q2", "contribution_analysis")
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request(crossed)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_rep6_cross_filled_role_fails_closed_before_execution(self) -> None:
        requests = [
            {
                "request_id": f"{dimension}_decomp",
                "domain": "delivery",
                "mode": "metric",
                "purpose": f"analyze July change by {dimension}",
                "metric": "delivery_amount",
                "calendar_month": "2026-07",
                "comparison": {"kind": "previous_period"},
                "complete_change_decomposition": {"dimension": dimension},
                "evidence_role": "contribution_analysis",
            }
            for dimension in ("customer", "department")
        ]
        validator = Draft7Validator(schemas.DATASAGE_QUERY["parameters"])
        self.assertFalse(validator.is_valid({"requests": requests}))

        with mock.patch.object(tools, "_execute") as execute:
            payload = json.loads(
                tools.runtime_guarded_datasage_query({"requests": requests})
            )

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
        execute.assert_not_called()

    def test_legacy_intent_remains_runtime_compatible_and_visible(self) -> None:
        request = _request("q1", "composition")
        request["analysis_intent"] = "contribution_analysis"

        validated = tools._validate_request(request)
        bundle = evidence.build_evidence_bundle(
            [validated],
            [_result("q1", relations=["observation", "dimension_breakdown"])],
        )

        self.assertEqual(
            bundle["items"][0]["analysis_intent"],
            "contribution_analysis",
        )
        self.assertEqual(bundle["items"][0]["evidence_role"], "composition")

    def test_bundle_reports_role_coverage_without_copying_business_values(self) -> None:
        bundle = evidence.build_evidence_bundle(
            [_request("q1", "comparison")],
            [_result("q1", relations=["observation", "period_comparison"])],
        )
        self.assertEqual(bundle["version"], "evidence-bundle/v1")
        self.assertEqual(bundle["coverage"]["requested_role_labels"], ["comparison"])
        self.assertFalse(bundle["coverage"]["role_labels_authorize_claims"])
        self.assertEqual(bundle["evidence_gaps"], [])
        self.assertIn("period_comparison", bundle["items"][0]["supports"])
        self.assertIn("hypothesis_direction", bundle["items"][0]["supports"])
        self.assertEqual(
            {
                key: bundle["answer_guardrails"][key]
                for key in EXPECTED_CROSS_EVIDENCE_GUARDRAILS
            },
            EXPECTED_CROSS_EVIDENCE_GUARDRAILS,
        )
        self.assertNotIn("facts", json.dumps(bundle))
        self.assertNotIn("secret", json.dumps(bundle))

    def test_truncated_request_is_reported_without_automatic_retry(self) -> None:
        bundle = evidence.build_evidence_bundle(
            [_request("q1", "composition")],
            [_result("q1", truncated=True, data_state="truncated", relations=["dimension_breakdown"])],
        )
        self.assertEqual(bundle["evidence_gaps"][0]["reason"], "source_truncated")
        self.assertNotIn("should_continue", bundle)
        self.assertIn("SOURCE_TRUNCATED", bundle["items"][0]["limitations"])

    def test_runtime_readiness_failure_keeps_the_evidence_bundle_shape(self) -> None:
        runtime_health = _load_package_module("runtime_health")
        with mock.patch.object(
            runtime_health,
            "query_readiness_status",
            return_value={
                "ready": False,
                "reason_code": "DATABASE_CONFIGURATION_MISSING",
            },
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [_request("q1", "outcome")]}
                )
            )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["metric_contexts"], [])
        self.assertEqual(payload["evidence_bundle"]["version"], "evidence-bundle/v1")
        self.assertEqual(payload["evidence_bundle"]["evidence_gaps"][0]["reason"], "request_failed")
        self.assertEqual(
            {
                key: payload["evidence_bundle"]["answer_guardrails"][key]
                for key in EXPECTED_CROSS_EVIDENCE_GUARDRAILS
            },
            EXPECTED_CROSS_EVIDENCE_GUARDRAILS,
        )

    def test_empty_and_undefined_are_complete_typed_states_not_retry_loops(self) -> None:
        for state, support in (
            ("empty", "empty_result_state"),
            ("undefined", "undefined_result_state"),
        ):
            with self.subTest(state=state):
                bundle = evidence.build_evidence_bundle(
                    [_request("q1", "outcome")],
                    [_result("q1", data_state=state)],
                )
                self.assertEqual(bundle["evidence_gaps"], [])
                self.assertIn(support, bundle["items"][0]["supports"])

    def test_reconciliation_is_required_for_structural_contribution(self) -> None:
        supported = evidence.build_evidence_bundle(
            [_request("q1", "composition")],
            [_result("q1", relations=["observation", "structural_contribution"], reconciled=True)],
        )
        unsupported = evidence.build_evidence_bundle(
            [_request("q1", "composition")],
            [_result("q1", relations=["observation", "structural_contribution"], reconciled=False)],
        )
        legacy_supported = evidence.build_evidence_bundle(
            [_request("q1", "composition")],
            [_result("q1", relations=["observation", "change_driver"], reconciled=True)],
        )
        self.assertIn("structural_contribution", supported["items"][0]["supports"])
        self.assertIn(
            "structural_contribution",
            legacy_supported["items"][0]["supports"],
        )
        self.assertNotIn("change_driver", legacy_supported["items"][0]["supports"])
        self.assertNotIn("structural_contribution", unsupported["items"][0]["supports"])

    def test_typed_not_reconciled_truncation_stays_visible_and_fail_closed(self) -> None:
        result = _result(
            "q1",
            truncated=True,
            data_state="truncated",
            relations=["observation", "period_comparison", "dimension_breakdown"],
        )
        result["change_reconciliation"] = {
            "status": "not_reconciled",
            "operation": "complete_change_decomposition",
            "reason_code": "PARTITION_TRUNCATED",
            "overall_request_id": "q1-overall",
        }

        bundle = evidence.build_evidence_bundle(
            [_request("q1", "composition")],
            [result],
        )

        item = bundle["items"][0]
        self.assertEqual(item["reconciliation"], "not_reconciled")
        self.assertEqual(item["completeness"], "truncated")
        self.assertNotIn("structural_contribution", item["supports"])
        self.assertEqual(
            item["limitations"],
            [
                "COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED",
                "SOURCE_TRUNCATED",
                "STRUCTURAL_CONTRIBUTION_NOT_RECONCILED",
            ],
        )
        self.assertEqual(
            bundle["evidence_gaps"],
            [
                {
                    "request_id": "q1",
                    "reason": "source_truncated",
                    "evidence_role": "composition",
                }
            ],
        )

    def test_one_failed_request_is_visible_without_upgrading_or_collapsing_its_role(self) -> None:
        bundle = evidence.build_evidence_bundle(
            [_request("q1", "comparison"), _request("q2", "comparison")],
            [
                _result("q1", relations=["observation"]),
                _result("q2", status="failed", data_state="failed"),
            ],
        )
        self.assertEqual(bundle["coverage"]["requested_role_labels"], ["comparison"])
        self.assertEqual(bundle["evidence_gaps"], [
            {"request_id": "q2", "reason": "request_failed", "evidence_role": "comparison"}
        ])
        self.assertEqual(
            {
                key: bundle["answer_guardrails"][key]
                for key in EXPECTED_CROSS_EVIDENCE_GUARDRAILS
            },
            EXPECTED_CROSS_EVIDENCE_GUARDRAILS,
        )
        self.assertIn("hypothesis_direction", bundle["items"][0]["supports"])
        self.assertEqual(bundle["items"][1]["supports"], [])
        self.assertNotIn("should_continue", bundle)

    def test_legacy_request_remains_accepted_and_unclassified(self) -> None:
        request = _request("q1")
        request.pop("analysis_intent")
        tools._validate_request(request)
        bundle = evidence.build_evidence_bundle(
            [request], [_result("q1", relations=["observation"])]
        )
        self.assertEqual(bundle["coverage"]["unspecified_request_ids"], ["q1"])

    def test_omitting_annotations_does_not_change_proof_authority(self) -> None:
        annotated = _request("q1", "composition")
        unannotated = dict(annotated)
        unannotated.pop("analysis_intent")
        unannotated.pop("evidence_role")
        result = _result(
            "q1",
            relations=["observation", "structural_contribution"],
            reconciled=True,
        )

        tools._validate_request(unannotated)
        annotated_bundle = evidence.build_evidence_bundle([annotated], [result])
        unannotated_bundle = evidence.build_evidence_bundle([unannotated], [result])

        annotated_item = annotated_bundle["items"][0]
        unannotated_item = unannotated_bundle["items"][0]
        self.assertEqual(annotated_item["supports"], unannotated_item["supports"])
        self.assertEqual(
            annotated_item["reconciliation"],
            unannotated_item["reconciliation"],
        )
        self.assertEqual(
            annotated_bundle["answer_guardrails"],
            unannotated_bundle["answer_guardrails"],
        )
        self.assertNotIn("analysis_intent", unannotated_item)
        self.assertNotIn("evidence_role", unannotated_item)


class ExpertCatalogTests(unittest.TestCase):
    def test_expert_index_is_smaller_and_legacy_summary_is_unchanged(self) -> None:
        compact = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "delivery", "view": "expert_index"}]}
            )
        )
        legacy = json.loads(
            contracts.datasage_catalog({"requests": [{"domain": "delivery"}]})
        )
        self.assertEqual(compact["status"], "success")
        self.assertEqual(compact["results"][0]["level"], "expert_index")
        self.assertEqual(legacy["results"][0]["level"], "summary")
        self.assertNotIn("analysis_affordances", compact["results"][0])
        self.assertTrue(
            all(
                "exact_default_lookup_supported" not in item
                for item in legacy["results"][0]["metrics"]
            )
        )
        self.assertLess(len(json.dumps(compact)), len(json.dumps(legacy)))
        metrics = {item["code"]: item for item in compact["results"][0]["metrics"]}
        self.assertTrue(metrics["delivery_amount"]["exact_default_lookup_supported"])
        self.assertFalse(
            metrics["delivery_amount_original"]["exact_default_lookup_supported"]
        )

    def test_metric_and_view_are_mutually_exclusive(self) -> None:
        payload = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {
                            "domain": "delivery",
                            "metric": "net_delivery_amount",
                            "view": "expert_index",
                        }
                    ]
                }
            )
        )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
        validator = Draft7Validator(schemas.DATASAGE_CATALOG["parameters"])
        self.assertFalse(
            validator.is_valid(
                {
                    "requests": [
                        {
                            "domain": "delivery",
                            "metric": "net_delivery_amount",
                            "view": "expert_index",
                        }
                    ]
                }
            )
        )

    def test_catalog_proof_capabilities_cannot_be_confused_with_request_roles(self) -> None:
        summary = json.loads(
            contracts.datasage_catalog({"requests": [{"domain": "delivery"}]})
        )
        self.assertEqual(summary["status"], "success")
        affordances = summary["results"][0]["analysis_affordances"]
        self.assertEqual(affordances["version"], "datasage-analysis-affordances/v7")
        capabilities = affordances["capabilities"]
        self.assertTrue(capabilities)
        self.assertTrue(all("proof_capability" in item for item in capabilities))
        self.assertTrue(all("evidence_role" not in item for item in capabilities))
        self.assertTrue(
            set(item["proof_capability"] for item in capabilities).isdisjoint(
                evidence.EVIDENCE_ROLES
            )
        )

    def test_catalog_distinguishes_bounded_same_statement_proof_from_full_rows(self) -> None:
        detail = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {"domain": "delivery", "metric": "delivery_amount"}
                    ]
                }
            )
        )["results"][0]
        planning = detail["analysis_affordances"][
            "selected_metric_change_planning"
        ]["complete_change_decomposition"]
        bounded = planning["evidence_shape_separation"][
            "bounded_same_statement_proof"
        ]
        self.assertEqual(
            bounded["authorization"],
            "returned_nonzero_rows_structural_contribution_only",
        )
        self.assertEqual(
            bounded["population_detail"],
            "incomplete_hidden_tail_and_residual_must_be_explicit",
        )
        self.assertEqual(
            bounded["driver_count_semantics"],
            "returned_nonzero_driver_count_covers_returned_rows_only_full_partition_row_count_covers_population",
        )
        self.assertEqual(bounded["causality"], "never_authorized")
        self.assertEqual(
            detail["metric"]["change_decomposition_policy"],
            "structural_contribution_after_reconciled_full_rows_or_same_statement_full_partition_aggregate_proof_with_bounded_claims",
        )


class ExpertVocabularyAlignmentTests(unittest.TestCase):
    def test_shared_playbooks_are_reachable_through_skill_view(self) -> None:
        root = PLUGIN_DIR.parents[1]
        datasage_skill = (root / "skills/datasage/SKILL.md").read_text(
            encoding="utf-8"
        )
        shared_skill = root / "skills/common-data-foundation/SKILL.md"
        self.assertTrue(shared_skill.is_file())
        self.assertNotIn("../common-data-foundation", datasage_skill)
        self.assertIn("name: common-data-foundation", datasage_skill)
        self.assertIn("file_path: references/expert-playbooks.yaml", datasage_skill)

    def test_playbook_and_evaluation_vocabularies_match_plugin(self) -> None:
        root = PLUGIN_DIR.parents[1]
        playbook = yaml.safe_load(
            (root / "skills/common-data-foundation/references/expert-playbooks.yaml").read_text(
                encoding="utf-8"
            )
        )
        cases_path = root / "evaluation/expert-core/cases.yaml"
        if not cases_path.is_file():
            self.skipTest("evaluation corpus not present in this deployment")
        cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
        intents = set(evidence.ANALYSIS_INTENTS)
        roles = set(evidence.EVIDENCE_ROLES)
        self.assertEqual(set(playbook["playbooks"]), intents)
        self.assertEqual(set(playbook["evidence_roles"]), roles)
        self.assertEqual(set(cases["taxonomies"]["intents"]), intents)
        self.assertEqual(set(cases["taxonomies"]["evidence_roles"]), roles)
        for intent, spec in playbook["playbooks"].items():
            required = set(spec["required_roles"])
            optional = set(spec["optional_roles"])
            self.assertLessEqual(required | optional, roles, intent)
            self.assertFalse(required & optional, intent)
        for case in cases["cases"]:
            expected = case["expected"]
            if expected["must_clarify"]:
                continue
            intent_playbook = playbook["playbooks"][expected["intent"]]
            required = set(intent_playbook["required_roles"])
            available = required | set(intent_playbook["optional_roles"])
            self.assertLessEqual(required, set(expected["evidence_roles"]), case["id"])
            self.assertLessEqual(set(expected["evidence_roles"]), available, case["id"])


if __name__ == "__main__":
    unittest.main()
