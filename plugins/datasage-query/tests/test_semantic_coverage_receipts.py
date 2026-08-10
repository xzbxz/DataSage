from __future__ import annotations

import importlib
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "datasage_query_semantic_receipts_test_package"
os.environ["HERMES_HOME"] = str(PLUGIN_DIR.parents[1])


def _load_package_module(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


evidence = _load_package_module("evidence")
tools = _load_package_module("tools")


def _request(request_id: str = "q1", **overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "request_id": request_id,
        "domain": "delivery",
        "mode": "metric",
        "purpose": "semantic receipt contract test",
        "metric": "delivery_amount",
        "calendar_month": "2026-07",
        "comparison": {"kind": "previous_period"},
    }
    request.update(overrides)
    return request


def _sealed_result(
    request_id: str,
    fingerprint: str,
    *,
    status: str = "success",
    data_state: str = "rows",
    truncated: bool = False,
    relations: tuple[str, ...] = ("observation", "period_comparison"),
    reconciliation: str | None = None,
) -> dict[str, object]:
    claim = {
        "claim_id": "claim_unsealed_0",
        "claim_seal": "unsealed",
        "request_id": request_id,
        "allowed_relations": list(relations),
        "facts": {
            "secret_business_value": 123456789,
            "physical_table": "vk_ai.delivery_fact",
        },
    }
    if "structural_contribution" in relations or "change_driver" in relations:
        claim["facts"]["net_change_contribution_rate"] = 1
        claim["relation_semantics"] = {
            "structural_contribution": "structural_not_causal"
        }
    result: dict[str, object] = {
        "request_id": request_id,
        "status": status,
        "data_state": data_state,
        "row_count": 1,
        "truncated": truncated,
        "_semantic_request_fingerprint": fingerprint,
        "claim_ledger": [claim],
        "allowed_reasoning_topics": [],
        "change_reconciliation": (
            {"status": reconciliation} if reconciliation is not None else None
        ),
        "error": {"code": "TEST_FAILURE"} if status != "success" else None,
    }
    tools._seal_claim_ids([result])
    if reconciliation == "reconciled":
        claim_ids = [claim["claim_id"] for claim in result["claim_ledger"]]
        structural_ids = [
            claim["claim_id"]
            for claim in result["claim_ledger"]
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


def _empty_result(request_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "status": "success",
        "data_state": "empty",
        "row_count": 0,
        "truncated": False,
        "_semantic_request_fingerprint": fingerprint,
        "claim_ledger": [],
        "allowed_reasoning_topics": [],
        "change_reconciliation": None,
        "error": None,
    }


class SemanticRequestFingerprintTests(unittest.TestCase):
    @staticmethod
    def _compiled_fingerprints(
        request: dict[str, object],
    ) -> tuple[str, str, str]:
        normalized = tools._validate_request(request)
        datasets, semantics = tools._contracts(str(normalized["domain"]))
        _sql, _params, scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            100,
        )
        metric = semantics["metrics"][str(normalized["metric"])]
        scope_fingerprint, projection_fingerprint = tools._scope_fingerprints(
            normalized,
            scope,
            metric,
            datasets,
        )
        return (
            scope_fingerprint,
            projection_fingerprint,
            tools._semantic_request_fingerprint(
                normalized,
                scope_fingerprint=scope_fingerprint,
            ),
        )

    def test_presentation_metadata_and_period_spelling_are_normalized(self) -> None:
        calendar = _request(
            "calendar",
            purpose="first wording",
            analysis_intent="change_diagnosis",
            evidence_role="outcome",
        )
        explicit_range = _request(
            "range",
            purpose="different wording",
            analysis_intent="performance_review",
            evidence_role="comparison",
        )
        explicit_range.pop("calendar_month")
        explicit_range["time_range"] = {
            "start": "2026-07-01",
            "end": "2026-08-01",
        }

        self.assertEqual(
            tools._semantic_request_fingerprint(calendar),
            tools._semantic_request_fingerprint(explicit_range),
        )

    def test_default_scopes_and_complete_operation_default_are_normalized(self) -> None:
        implicit_delivery = _request("implicit-delivery")
        explicit_delivery = _request(
            "explicit-delivery", delivery_scope="default_net"
        )
        self.assertEqual(
            tools._semantic_request_fingerprint(implicit_delivery),
            tools._semantic_request_fingerprint(explicit_delivery),
        )

        implicit_operation = _request(
            "implicit-operation",
            complete_change_decomposition={"dimension": "customer"},
        )
        implicit_operation.pop("comparison")
        explicit_operation = _request(
            "explicit-operation",
            complete_change_decomposition={"dimension": "customer"},
        )
        self.assertEqual(
            tools._semantic_request_fingerprint(implicit_operation),
            tools._semantic_request_fingerprint(explicit_operation),
        )

        inventory_implicit = {
            "request_id": "inventory-implicit",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "inventory default",
            "metric": "current_inventory_quantity",
        }
        inventory_explicit = {
            **inventory_implicit,
            "request_id": "inventory-explicit",
            "inventory_scope": "on_hand",
        }
        self.assertEqual(
            tools._semantic_request_fingerprint(
                inventory_implicit, scope_fingerprint="scope_inventory_on_hand"
            ),
            tools._semantic_request_fingerprint(
                inventory_explicit, scope_fingerprint="scope_inventory_on_hand"
            ),
        )

    def test_real_contract_compile_merges_implicit_and_explicit_defaults(self) -> None:
        implicit_delivery = _request("implicit-delivery")
        explicit_delivery = _request(
            "explicit-delivery",
            delivery_scope="default_net",
        )
        self.assertEqual(
            self._compiled_fingerprints(implicit_delivery),
            self._compiled_fingerprints(explicit_delivery),
        )

        implicit_inventory = {
            "request_id": "implicit-inventory",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "inventory default compile",
            "metric": "current_warehouse_age_days",
        }
        explicit_inventory = {
            **implicit_inventory,
            "request_id": "explicit-inventory",
            "inventory_scope": "on_hand",
        }
        self.assertEqual(
            self._compiled_fingerprints(implicit_inventory),
            self._compiled_fingerprints(explicit_inventory),
        )

    def test_every_evidence_changing_axis_remains_distinct(self) -> None:
        baseline = _request("baseline")
        variants = {
            "metric": _request("metric", metric="delivery_order_count"),
            "domain": _request("domain", domain="receipt"),
            "scope": _request("scope", delivery_scope="explicit_gross"),
            "filter": _request(
                "filter", metric_filters={"customer": "customer-secret-001"}
            ),
            "period": _request("period", calendar_month="2026-08"),
            "dimension": _request("dimension", dimensions=["customer"]),
            "ranking": _request(
                "ranking",
                dimensions=["customer"],
                order_by={"field": "metric_value", "direction": "desc"},
                limit=5,
            ),
        }
        baseline_fingerprint = tools._semantic_request_fingerprint(baseline)

        self.assertRegex(
            baseline_fingerprint,
            re.compile(r"^semreq_v1_[0-9a-f]{64}$"),
        )
        for axis, request in variants.items():
            with self.subTest(axis=axis):
                self.assertNotEqual(
                    baseline_fingerprint,
                    tools._semantic_request_fingerprint(request),
                )
        self.assertNotIn(
            "customer-secret-001",
            tools._semantic_request_fingerprint(variants["filter"]),
        )

    def test_filter_and_dimension_order_do_not_create_false_differences(self) -> None:
        first = _request(
            "first",
            dimensions=["customer", "department"],
            metric_filters={"customer": ["B", "A"], "department": "X"},
        )
        second = _request(
            "second",
            dimensions=["department", "customer"],
            metric_filters={"department": "X", "customer": ["A", "B"]},
        )
        self.assertEqual(
            tools._semantic_request_fingerprint(first),
            tools._semantic_request_fingerprint(second),
        )

    def test_model_result_hides_internal_fingerprint_and_private_projection(self) -> None:
        fingerprint = tools._semantic_request_fingerprint(_request())
        wire = tools._model_wire_result(
            {
                "request_id": "q1",
                "status": "success",
                "_semantic_request_fingerprint": fingerprint,
                "_semantic_projection": {"physical_table": "secret"},
            }
        )
        self.assertNotIn("semantic_request_fingerprint", wire)
        self.assertNotIn("_semantic_request_fingerprint", wire)
        self.assertNotIn("_semantic_projection", wire)

    def test_compiled_scope_identity_prevents_entity_binding_false_merge(self) -> None:
        request = _request(
            "entity",
            dimensions=["customer"],
            order_by={"field": "metric_value", "direction": "desc"},
            limit=5,
        )
        customer_scope = tools._semantic_request_fingerprint(
            request,
            scope_fingerprint="scope_customer_binding_a",
        )
        other_customer_scope = tools._semantic_request_fingerprint(
            request,
            scope_fingerprint="scope_customer_binding_b",
        )
        self.assertNotEqual(customer_scope, other_customer_scope)


class CoverageReceiptTests(unittest.TestCase):
    def test_only_successful_sealed_results_are_receipted(self) -> None:
        fingerprint = tools._semantic_request_fingerprint(_request())
        unsealed = _sealed_result("unsealed", fingerprint)
        unsealed["claim_ledger"][0].pop("claim_seal")
        bundle = evidence.build_evidence_bundle(
            [
                _request("success"),
                _request("failed"),
                _request("unsealed"),
            ],
            [
                _sealed_result("success", fingerprint),
                _sealed_result("failed", fingerprint, status="failed"),
                unsealed,
            ],
        )

        receipts = bundle["coverage_receipts"]
        self.assertEqual(receipts["version"], "semantic-coverage-receipts/v1")
        self.assertEqual(len(receipts["items"]), 1)
        self.assertEqual(receipts["items"][0]["request_ids"], ["success"])
        self.assertEqual(receipts["items"][0]["completeness"], "complete")

    def test_garbage_and_self_inconsistent_claim_seals_are_rejected(self) -> None:
        fingerprint = tools._semantic_request_fingerprint(_request())
        garbage = _sealed_result("garbage", fingerprint)
        garbage["claim_ledger"][0]["claim_seal"] = "garbage"
        mismatched = _sealed_result("mismatched", fingerprint)
        valid_seal = mismatched["claim_ledger"][0]["claim_seal"]
        mismatched["claim_ledger"][0]["claim_seal"] = (
            f"{valid_seal[:-1]}{'0' if valid_seal[-1] != '0' else '1'}"
        )
        wrong_id = _sealed_result("wrong-id", fingerprint)
        wrong_id["claim_ledger"][0]["claim_id"] = "claim_00000000000000000000"

        bundle = evidence.build_evidence_bundle(
            [_request("garbage"), _request("mismatched"), _request("wrong-id")],
            [garbage, mismatched, wrong_id],
        )
        self.assertEqual(bundle["coverage_receipts"]["items"], [])

    def test_forged_reconciled_status_never_adds_structural_support(self) -> None:
        request = _request("forged-reconciliation")
        fingerprint = tools._semantic_request_fingerprint(request)
        result = _sealed_result("forged-reconciliation", fingerprint)
        result["change_reconciliation"] = {
            "status": "reconciled",
            "overall_request_id": "other",
        }
        evidence.seal_reconciliation(result["change_reconciliation"])

        bundle = evidence.build_evidence_bundle([request], [result])
        item = bundle["items"][0]
        self.assertEqual(item["reconciliation"], "invalid_reconciliation")
        self.assertNotIn("structural_contribution", item["supports"])

    def test_validly_sealed_foreign_claim_cannot_authorize_another_result(self) -> None:
        request = _request("bound-request")
        fingerprint = tools._semantic_request_fingerprint(
            request,
            scope_fingerprint="scope_expected",
        )
        result = _sealed_result("bound-request", fingerprint)
        result["scope_fingerprint"] = "scope_expected"
        result["projection_fingerprint"] = "projection_expected"
        claim = result["claim_ledger"][0]
        claim.update(
            {
                "request_id": "foreign-request",
                "scope_fingerprint": "scope_foreign",
                "projection_fingerprint": "projection_foreign",
            }
        )
        evidence.seal_claim(claim)

        bundle = evidence.build_evidence_bundle([request], [result])
        self.assertEqual(bundle["coverage_receipts"]["items"], [])

    def test_result_fingerprint_must_match_request_and_compiled_scope(self) -> None:
        request = _request("scope-bound")
        wrong_fingerprint = tools._semantic_request_fingerprint(
            request,
            scope_fingerprint="scope_other",
        )
        result = _sealed_result("scope-bound", wrong_fingerprint)
        result["scope_fingerprint"] = "scope_expected"

        bundle = evidence.build_evidence_bundle([request], [result])
        self.assertEqual(bundle["coverage_receipts"]["items"], [])

    def test_empty_state_cannot_be_receipted_through_nonempty_claim_path(self) -> None:
        request = _request("false-empty")
        fingerprint = tools._semantic_request_fingerprint(request)
        malformed = _sealed_result(
            "false-empty",
            fingerprint,
            data_state="empty",
        )

        bundle = evidence.build_evidence_bundle([request], [malformed])
        self.assertEqual(bundle["coverage_receipts"]["items"], [])

    def test_successful_empty_is_a_typed_terminal_receipt(self) -> None:
        request = _request("empty")
        fingerprint = tools._semantic_request_fingerprint(request)
        bundle = evidence.build_evidence_bundle(
            [request], [_empty_result("empty", fingerprint)]
        )
        receipt = bundle["coverage_receipts"]["items"][0]
        self.assertEqual(receipt["evidence_state"], "empty")
        self.assertEqual(receipt["completeness"], "limited")
        self.assertEqual(receipt["supports"], ["empty_result_state"])

    def test_receipt_preserves_rows_zero_undefined_and_truncated_states(self) -> None:
        states = (
            ("rows", False, "complete"),
            ("zero", False, "complete"),
            ("undefined", False, "limited"),
            ("truncated", True, "truncated"),
        )
        requests = [_request(state) for state, _truncated, _complete in states]
        results = [
            _sealed_result(
                state,
                tools._semantic_request_fingerprint(_request(state)),
                data_state=state,
                truncated=truncated,
            )
            for state, truncated, _complete in states
        ]
        bundle = evidence.build_evidence_bundle(requests, results)
        receipts_by_id = {
            receipt["request_ids"][0]: receipt
            for receipt in bundle["coverage_receipts"]["items"]
        }
        for state, _truncated, completeness in states:
            with self.subTest(state=state):
                self.assertEqual(receipts_by_id[state]["evidence_state"], state)
                self.assertEqual(
                    receipts_by_id[state]["completeness"], completeness
                )

    def test_truncated_receipt_stays_explicit_and_never_claims_complete(self) -> None:
        request = _request("truncated")
        fingerprint = tools._semantic_request_fingerprint(request)
        bundle = evidence.build_evidence_bundle(
            [request],
            [
                _sealed_result(
                    "truncated",
                    fingerprint,
                    data_state="truncated",
                    truncated=True,
                    relations=("observation", "dimension_breakdown"),
                )
            ],
        )
        receipt = bundle["coverage_receipts"]["items"][0]
        self.assertEqual(receipt["completeness"], "truncated")
        self.assertEqual(receipt["supports"], ["dimension_breakdown", "observation"])

    def test_complete_decomposition_overall_receipts_are_aggregated(self) -> None:
        overall_request_a = _request("decomp-a-overall")
        overall_request_b = _request("decomp-b-overall")
        partition_request = _request(
            "decomp-a-partition",
            dimensions=["customer"],
            decomposition_of_request_id="decomp-a-overall",
        )
        overall_fingerprint = tools._semantic_request_fingerprint(overall_request_a)
        partition_fingerprint = tools._semantic_request_fingerprint(partition_request)
        bundle = evidence.build_evidence_bundle(
            [overall_request_a, overall_request_b, partition_request],
            [
                _sealed_result("decomp-a-overall", overall_fingerprint),
                _sealed_result("decomp-b-overall", overall_fingerprint),
                _sealed_result(
                    "decomp-a-partition",
                    partition_fingerprint,
                    relations=(
                        "observation",
                        "period_comparison",
                        "dimension_breakdown",
                        "structural_contribution",
                    ),
                    reconciliation="reconciled",
                ),
            ],
        )
        receipts = bundle["coverage_receipts"]["items"]
        self.assertEqual(len(receipts), 2)
        overall = next(
            item
            for item in receipts
            if item["semantic_request_fingerprint"] == overall_fingerprint
        )
        partition = next(
            item
            for item in receipts
            if item["semantic_request_fingerprint"] == partition_fingerprint
        )
        self.assertEqual(
            overall["request_ids"],
            ["decomp-a-overall", "decomp-b-overall"],
        )
        self.assertIn("structural_contribution", partition["supports"])
        self.assertEqual(partition["reconciliation"], "reconciled")

    def test_receipts_are_compact_and_never_copy_facts_or_physical_details(self) -> None:
        request = _request(
            "secret-filter",
            metric_filters={"customer": "customer-secret-001"},
        )
        fingerprint = tools._semantic_request_fingerprint(request)
        bundle = evidence.build_evidence_bundle(
            [request], [_sealed_result("secret-filter", fingerprint)]
        )
        encoded = json.dumps(
            bundle["coverage_receipts"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertLess(len(encoded.encode("utf-8")), 1024)
        for forbidden in (
            "123456789",
            "customer-secret-001",
            "delivery.fact",
            "physical_table",
            "secret_business_value",
            "facts",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(bundle["version"], "evidence-bundle/v1")


if __name__ == "__main__":
    unittest.main()
