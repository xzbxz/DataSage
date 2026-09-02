from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest

import jsonschema


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
PACKAGE = "datasage_compact_payload_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PACKAGE] = package

contracts = importlib.import_module(f"{PACKAGE}.contracts")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
tools = importlib.import_module(f"{PACKAGE}.tools")
wire = importlib.import_module(f"{PACKAGE}.wire")


def _catalog(args: dict[str, object]) -> tuple[str, dict[str, object]]:
    raw = contracts.datasage_catalog(args)
    compact = json.loads(wire.enforce_tool_result_budget("datasage_catalog", raw))
    return raw, compact


def _claim(request_id: str, index: int, *, truncated: bool) -> dict[str, object]:
    return {
        "claim_id": f"claim_{request_id}_{index}",
        "claim_seal": "sha256_" + str(index).zfill(64),
        "request_id": request_id,
        "metric_ref": f"metric_{request_id}",
        "dimensions": [{"label": "customer", "value": f"entity_{index}"}],
        "scope_entities": [{"type": "department", "value": "one_scope"}],
        "period": {"start": "2026-01-01", "end": "2026-07-01"},
        "scope_fingerprint": f"scope_{request_id}",
        "projection_fingerprint": f"projection_{request_id}",
        "unit": "人民币元",
        "currency": "CNY",
        "facts": {"metric_value": str(1000 - index)},
        "states": {"observation_state": "reported"},
        "source_truncated": truncated,
        "allowed_relations": ["observation", "dimension_breakdown"],
    }


def _query_result(
    request_id: str,
    *,
    truncated: bool,
    structural_limitation: bool,
) -> dict[str, object]:
    claims = [_claim(request_id, index, truncated=truncated) for index in range(10)]
    return {
        "request_id": request_id,
        "status": "success",
        "data_state": "truncated" if truncated else "rows",
        "business_metric_ref": f"metric_{request_id}",
        "business_metric_label": f"Metric {request_id}",
        "business_dimension_labels": ["客户"],
        "scope_fingerprint": f"scope_{request_id}",
        "projection_fingerprint": f"projection_{request_id}",
        "claim_ledger": claims,
        "disclosure_contract_version": "metric-disclosure-ledger/v1",
        "disclosure_ledger": [
            {
                "disclosure_id": "shared.scope",
                "disclosure_seal": "sha256_" + "f" * 64,
                "request_id": request_id,
                "metric_ref": f"metric_{request_id}",
                "scope_fingerprint": f"scope_{request_id}",
                "projection_fingerprint": f"projection_{request_id}",
                "text": "Shared governed scope disclosure.",
                "applies": True,
            }
        ],
        "disclosure_ledger_seal": "sha256_" + "e" * 64,
        "change_reconciliation": (
            {"status": "not_reconciled", "reason_code": "ordinary_ranking"}
            if structural_limitation
            else None
        ),
        "row_count": len(claims),
        "truncated": truncated,
        "requested_limit": 10,
        "effective_limit": 10,
        "has_more": truncated,
        "applied_time_range": {"start": "2026-01-01", "end": "2026-07-01"},
        "error": None,
    }


def _query_payload() -> dict[str, object]:
    results = [
        _query_result("ranked", truncated=True, structural_limitation=False),
        _query_result("structure", truncated=False, structural_limitation=True),
    ]
    return {
        "status": "success",
        "request_count": 2,
        "answer_scope_line": "查询范围：同一测试期间",
        "metric_contexts": [
            {
                "business_metric_ref": f"metric_{request_id}",
                "label": f"Metric {request_id}",
                "definition": "Governed test definition.",
                "unit": "人民币元",
            }
            for request_id in ("ranked", "structure")
        ],
        "evidence_bundle": {
            "version": "evidence-bundle/v1",
            "coverage": {"request_count": 2},
            "coverage_receipts": {
                "items": [
                    {"semantic_request_fingerprint": "x" * 64},
                    {"semantic_request_fingerprint": "y" * 64},
                ]
            },
            "items": [
                {
                    "request_id": "ranked",
                    "status": "success",
                    "data_state": "truncated",
                    "completeness": "truncated",
                    "reconciliation": "not_requested_or_unavailable",
                    "supports": ["observation", "dimension_breakdown"],
                    "limitations": ["SOURCE_TRUNCATED"],
                },
                {
                    "request_id": "structure",
                    "status": "success",
                    "data_state": "rows",
                    "completeness": "complete",
                    "reconciliation": "not_requested_or_unavailable",
                    "supports": ["observation", "dimension_breakdown"],
                    "limitations": ["STRUCTURAL_CONTRIBUTION_NOT_RECONCILED"],
                },
            ],
            "evidence_gaps": [],
        },
        "results": results,
        "source_evidence_ref": {
            "schema": "datasage-query-model-source-reference/v1",
            "source_ref_sha256": "a" * 64,
        },
        "calculation_count": 1,
        "calculations": [
            {
                "calculation_id": "c1",
                "status": "success",
                "operation": "ratio",
                "value": "1.1",
                "unit": "ratio",
                "relation_semantics": {"derived_observation": True},
                "calculation_seal": "sha256_" + "b" * 64,
            }
        ],
    }


class CompactPayloadTests(unittest.TestCase):
    def test_catalog_schema_exposes_scorecard_and_explicit_full_views(self):
        variants = schemas.DATASAGE_CATALOG["parameters"]["properties"]["requests"]["items"]["oneOf"]
        domain_views = variants[0]["properties"]["view"]["enum"]
        self.assertEqual({"expert_index", "full", "audit"}, set(domain_views))
        self.assertEqual(
            "performance_scorecard",
            variants[1]["properties"]["view"]["const"],
        )

    def test_scorecard_is_publicly_exclusive_and_runtime_rejects_mixed_calls_atomically(
        self,
    ):
        mixed = {
            "requests": [
                {"view": "performance_scorecard"},
                {"domain": "delivery", "view": "expert_index"},
            ]
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(mixed, schemas.DATASAGE_CATALOG["parameters"])

        raw, compact = _catalog(mixed)
        self.assertEqual("failed", compact["status"])
        self.assertEqual("INVALID_INPUT", compact["error"]["code"])
        for payload in (json.loads(raw), compact):
            self.assertNotIn("results", payload)
            self.assertNotIn("failed_request_count", payload)
            self.assertNotIn("failures", payload)
            self.assertNotIn(
                "REDUNDANT_WITH_SCORECARD",
                json.dumps(payload, ensure_ascii=False),
            )

    def test_default_catalog_is_compact_and_full_view_remains_compatible(self):
        raw, compact = _catalog({"requests": [{"domain": "delivery"}]})
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        metric = compact["results"][0]["metrics"][0]
        for field in (
            "code",
            "label",
            "business_definition",
            "time_policy",
            "unit",
            "operation_summary",
        ):
            self.assertIn(field, metric)
        self.assertNotIn("planning_guidance", compact["results"][0])
        self.assertNotIn("analysis_affordances", compact["results"][0])

        _raw_full, full = _catalog(
            {"requests": [{"domain": "delivery", "view": "full"}]}
        )
        self.assertEqual("audit_full", full["results"][0]["projection_mode"])
        self.assertIn("capability_affordances", full["results"][0])
        self.assertNotIn("planning_guidance", full["results"][0])
        self.assertNotIn("analysis_affordances", full["results"][0])

    def test_metric_detail_compacts_without_losing_query_planning_fields(self):
        raw, compact = _catalog(
            {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]}
        )
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(rendered), len(raw))
        detail = compact["results"][0]
        self.assertNotIn("detail_receipt", detail)
        self.assertTrue(detail["dimensions"])
        self.assertIn("allowed_dimensions", detail["metric"])
        self.assertEqual(
            ["previous_period", "year_over_year"],
            detail["metric"]["comparison_kinds"],
        )
        self.assertIn("complete_change_decomposition", detail["metric"]["operation_summary"])
        self.assertTrue(compact["dimension_value_policies"])
        self.assertNotIn("reasoning_topics", rendered)

    def test_scorecard_returns_candidate_lenses_without_execution_receipts(self):
        raw, compact = _catalog({"requests": [{"view": "performance_scorecard"}]})
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        scorecard = compact["results"][0]
        self.assertGreater(scorecard["metric_count"], 0)
        candidates = [
            candidate
            for lens in scorecard["candidate_lenses"]
            for candidate in lens["candidates"]
        ]
        self.assertEqual(scorecard["metric_count"], len(candidates))
        profitability = next(
            item
            for item in scorecard["candidate_lenses"]
            if item["lens"] == "profitability"
        )
        self.assertEqual("not_available", profitability["status"])
        self.assertEqual([], profitability["candidates"])
        self.assertTrue(
            any(
                "remains unassessed" in boundary
                for boundary in scorecard["evidence_boundaries"]
            )
        )
        for forbidden in (
            "recommended_bundle",
            "recipe",
            "request_template",
            "request_id_hint",
            "reasoning_topics",
            "optional_view",
            "default_planner",
            "invocation_policy",
            "selection_owner",
            "ordering_owner",
            "interpretation_owner",
        ):
            self.assertNotIn(forbidden, rendered)
            self.assertNotIn(forbidden, raw)

        query_requests = []
        for index, detail in enumerate(candidates, start=1):
            self.assertEqual("metric", detail["level"])
            self.assertNotIn("dimensions", detail)
            domain = detail["domain"]
            metric = detail["metric"]["code"]
            request = {
                "request_id": f"adaptive_candidate_{index}",
                "domain": domain,
                "metric": metric,
                "dimensions": [],
            }
            if domain == "target":
                request["attribution_mode"] = "transaction_detail"
            query_requests.append(request)
        jsonschema.validate(
            {"requests": query_requests}, schemas.DATASAGE_QUERY["parameters"]
        )

    def test_query_wire_deduplicates_proof_envelopes_and_keeps_answer_evidence(self):
        payload = _query_payload()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        rendered = wire.enforce_tool_result_budget("datasage_query", raw)
        compact = json.loads(rendered)
        self.assertLess(len(rendered), len(raw) * 0.55)
        self.assertEqual("datasage-query-model-wire/v3", compact["model_wire_version"])
        self.assertEqual(1, len(compact["disclosures"]))
        self.assertEqual(["ranked", "structure"], compact["disclosures"][0]["request_ids"])
        result = compact["results"][0]
        self.assertNotIn("allowed_reasoning_topics", rendered)
        self.assertEqual(10, len(result["rows"]))
        self.assertIn("facts", result["rows"][0])
        self.assertIn("states", result["rows"][0])
        for field in ("requested_limit", "effective_limit", "has_more", "truncated"):
            self.assertIn(field, result)
        self.assertEqual(["SOURCE_TRUNCATED"], result["limitations"])
        self.assertNotIn("answer_constraints", compact)
        items = {
            item["request_id"]: item
            for item in compact["evidence_bundle"]["items"]
        }
        self.assertEqual("truncated", items["ranked"]["completeness"])
        self.assertIn("SOURCE_TRUNCATED", items["ranked"]["limitations"])
        self.assertIn(
            "STRUCTURAL_CONTRIBUTION_NOT_RECONCILED",
            items["structure"]["limitations"],
        )
        self.assertEqual("success", compact["calculations"][0]["status"])
        self.assertNotIn("answer_guardrails", compact["evidence_bundle"])
        self.assertNotIn('"rule":', rendered)
        self.assertEqual(1, compact["calculation_count"])
        for forbidden in (
            "claim_seal",
            "disclosure_seal",
            "scope_fingerprint",
            "projection_fingerprint",
            "claim_ledger",
            "disclosure_ledger",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_wire_keeps_typed_supports_and_calculation_scope_without_projection(self):
        payload = _query_payload()
        payload["evidence_bundle"]["items"][0]["supports"].append("target_status")
        payload["calculations"][0].update(
            {
                "operands": [
                    {"request_id": "ranked"},
                    {"request_id": "structure"},
                ],
                "scope_compatibility": {
                    "same_metric_basis": True,
                    "same_filter_scope": True,
                    "same_unit": True,
                },
                "period_compatibility": {
                    "status": "compatible",
                    "reason_codes": [],
                },
            }
        )
        compact = json.loads(wire.enforce_tool_result_budget("datasage_query", payload))
        self.assertNotIn("answer_constraints", compact)
        item = compact["evidence_bundle"]["items"][0]
        self.assertIn("target_status", item["supports"])
        calculation = compact["calculations"][0]
        self.assertEqual("success", calculation["status"])
        self.assertEqual(
            {
                "same_metric_basis": True,
                "same_filter_scope": True,
                "same_unit": True,
            },
            calculation["scope_compatibility"],
        )

    def test_wire_keeps_failed_calculation_as_typed_fact(self):
        payload = _query_payload()
        payload["calculations"][0].update(
            {
                "status": "failed",
                "operands": [
                    {"request_id": "ranked"},
                    {"request_id": "structure"},
                ],
                "error": {
                    "code": "CALCULATION_SCOPE_MISMATCH",
                    "message": "typed failure",
                    "retryable": False,
                },
            }
        )
        compact = json.loads(wire.enforce_tool_result_budget("datasage_query", payload))
        self.assertNotIn("answer_constraints", compact)
        calculation = compact["calculations"][0]
        self.assertEqual("failed", calculation["status"])
        self.assertEqual(
            "CALCULATION_SCOPE_MISMATCH",
            calculation["error"]["code"],
        )
        self.assertEqual(
            ["ranked", "structure"],
            [operand["request_id"] for operand in calculation["operands"]],
        )

    def test_typed_reconciliation_is_the_limitation_authority(self):
        without_reconciliation = _query_result(
            "without_reconciliation",
            truncated=False,
            structural_limitation=False,
        )
        plain = tools.evidence.build_evidence_bundle(
            [{"request_id": "without_reconciliation"}],
            [without_reconciliation],
        )
        self.assertNotIn(
            "STRUCTURAL_CONTRIBUTION_NOT_RECONCILED",
            plain["items"][0]["limitations"],
        )

        with_reconciliation = _query_result(
            "typed_reconciliation",
            truncated=False,
            structural_limitation=True,
        )
        typed = tools.evidence.build_evidence_bundle(
            [{"request_id": "typed_reconciliation"}],
            [with_reconciliation],
        )
        self.assertIn(
            "STRUCTURAL_CONTRIBUTION_NOT_RECONCILED",
            typed["items"][0]["limitations"],
        )

    def test_large_query_retains_every_branch_for_hermes_spillover(self):
        huge = _query_result("huge", truncated=False, structural_limitation=False)
        for claim in huge["claim_ledger"]:
            claim["facts"]["metric_value"] = "x" * 12_000
        small = _query_result("small", truncated=False, structural_limitation=False)
        small["claim_ledger"] = small["claim_ledger"][:1]
        small["row_count"] = 1
        payload = _query_payload()
        payload["results"] = [huge, small]
        payload["metric_contexts"] = [
            {
                "business_metric_ref": "metric_huge",
                "label": "Huge",
                "definition": "Oversized branch.",
                "unit": "人民币元",
            },
            {
                "business_metric_ref": "metric_small",
                "label": "Small",
                "definition": "Retainable branch.",
                "unit": "人民币元",
            },
        ]
        payload["evidence_bundle"]["items"] = [
            {
                "request_id": request_id,
                "status": "success",
                "data_state": "rows",
                "completeness": "complete",
                "reconciliation": "not_requested_or_unavailable",
                "supports": ["observation"],
                "limitations": [],
            }
            for request_id in ("huge", "small")
        ]
        rendered = wire.enforce_tool_result_budget("datasage_query", payload)
        compact = json.loads(rendered)
        self.assertGreater(len(rendered), 90_000)
        self.assertEqual(
            ["huge", "small"],
            [item["request_id"] for item in compact["results"]],
        )
        self.assertEqual(2, len(compact["evidence_bundle"]["items"]))
        self.assertEqual(2, len(compact["metric_contexts"]))
        self.assertNotIn("omitted_result_count", compact)
        self.assertNotIn("answer_constraints", compact)

    def test_query_wire_keeps_row_scope_when_scopes_vary(self):
        payload = _query_payload()
        payload["results"][0]["claim_ledger"][1]["scope_entities"] = [
            {"type": "department", "value": "another_scope"}
        ]
        compact = json.loads(wire.enforce_tool_result_budget("datasage_query", payload))
        rows = compact["results"][0]["rows"]
        self.assertNotIn("scope_entities", compact["results"][0])
        self.assertEqual("one_scope", rows[0]["scope_entities"][0]["value"])
        self.assertEqual("another_scope", rows[1]["scope_entities"][0]["value"])


if __name__ == "__main__":
    unittest.main()
