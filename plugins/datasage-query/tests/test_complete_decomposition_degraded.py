from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
PACKAGE = "complete_decomposition_degraded_test_package"
os.environ["HERMES_HOME"] = str(REPO_ROOT)


def _load_package_module(name: str):
    package = sys.modules.get(PACKAGE)
    if package is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


tools = _load_package_module("tools")


def _operation_request() -> dict:
    return {
        "request_id": "customer-change",
        "domain": "delivery",
        "mode": "metric",
        "purpose": "offline complete decomposition contract test",
        "analysis_intent": "contribution_analysis",
        "evidence_role": "composition",
        "delivery_scope": "default_net",
        "metric": "delivery_amount",
        "time_range": {"start": "2026-07-01", "end": "2026-08-01"},
        "comparison": {"kind": "previous_period"},
        "complete_change_decomposition": {"dimension": "customer"},
    }


def _claim(
    request_id: str,
    *,
    current: int,
    comparison: int,
    dimension: str | None,
    truncated: bool,
) -> dict:
    relations = ["observation", "period_comparison"]
    dimensions = []
    if dimension is not None:
        relations.append("dimension_breakdown")
        dimensions.append({"label": "customer", "value": dimension})
    return {
        "request_id": request_id,
        "dimensions": dimensions,
        "facts": {
            "metric_value": current,
            "comparison_value": comparison,
            "delta_value": current - comparison,
        },
        "states": {},
        "source_truncated": truncated,
        "allowed_relations": relations,
    }


def _result(
    request: dict,
    claims: list[dict],
    *,
    truncated: bool,
    projection: str,
) -> dict:
    return {
        "request_id": request["request_id"],
        "status": "success",
        "data_state": "truncated" if truncated else "rows",
        "business_metric_ref": "metric_delivery_amount",
        "business_metric_unit": "CNY",
        "scope_fingerprint": "scope_delivery_all_customers",
        "projection_fingerprint": projection,
        "applied_time_range": {
            "current": {
                "start": "2026-07-01",
                "end": "2026-08-01",
                "source": "explicit",
            },
            "comparison": {
                "start": "2026-06-01",
                "end": "2026-07-01",
                "source": "explicit",
            },
        },
        "claim_ledger": claims,
        "change_reconciliation": None,
        "truncated": truncated,
    }


def _operation_chain(*, truncated_partition: bool):
    expanded, operation_partitions = tools._expand_complete_change_decompositions(
        [_operation_request()]
    )
    overall_request, partition_request = expanded
    capability = {"mode": "additive_partition", "dimensions": ["customer"]}
    semantics = {
        "metrics": {
            "delivery_amount": {"change_decomposition": capability},
        }
    }
    prepared = [
        {"request": overall_request, "semantics": semantics},
        {"request": partition_request, "semantics": semantics},
    ]
    contexts = [tools._decomposition_context(item) for item in prepared]

    overall = _result(
        overall_request,
        [
            _claim(
                overall_request["request_id"],
                current=200,
                comparison=300,
                dimension=None,
                truncated=False,
            )
        ],
        truncated=False,
        projection="projection_delivery_overall",
    )
    partition_claims = [
        _claim(
            partition_request["request_id"],
            current=75,
            comparison=100,
            dimension="customer-a",
            truncated=truncated_partition,
        )
    ]
    if not truncated_partition:
        partition_claims.append(
            _claim(
                partition_request["request_id"],
                current=125,
                comparison=200,
                dimension="customer-b",
                truncated=False,
            )
        )
    partition = _result(
        partition_request,
        partition_claims,
        truncated=truncated_partition,
        projection="projection_delivery_customer",
    )
    overall["_snapshot_group_marker"] = "snapshot_test_group"
    partition["_snapshot_group_marker"] = "snapshot_test_group"
    return contexts, [overall, partition], operation_partitions


class CompleteDecompositionDegradedTests(unittest.TestCase):
    def test_mismatched_snapshot_marker_never_authorizes_structure(self) -> None:
        contexts, results, _operation_partitions = _operation_chain(
            truncated_partition=False
        )
        results[1]["_snapshot_group_marker"] = "snapshot_other_group"

        tools._authorize_change_decompositions(contexts, results)

        self.assertNotIn("_change_reconciliation_pending", results[1])
        self.assertTrue(
            all(
                "structural_contribution" not in claim["allowed_relations"]
                for claim in results[1]["claim_ledger"]
            )
        )

    def test_missing_snapshot_marker_has_specific_failure_reason(self) -> None:
        _contexts, results, _operation_partitions = _operation_chain(
            truncated_partition=False
        )
        results[1].pop("_snapshot_group_marker")

        self.assertEqual(
            tools._complete_decomposition_failure_reason(results[0], results[1]),
            "SNAPSHOT_CONSISTENCY_UNPROVEN",
        )

    def test_valid_bounded_proof_mismatch_is_not_reported_as_truncation(self) -> None:
        _contexts, results, _operation_partitions = _operation_chain(
            truncated_partition=True
        )
        partition = results[1]
        partition["complete_partition_proof"] = {
            "version": "same-statement-window-partition-proof/v1",
            "metric_value": 201,
            "comparison_value": 300,
            "delta_value": -99,
            "full_partition_row_count": 2,
        }
        partition["complete_partition_proof_failure"] = None

        self.assertEqual(
            tools._complete_decomposition_failure_reason(results[0], partition),
            "PARTITION_DOES_NOT_RECONCILE",
        )

    def test_truncated_partition_stays_fail_closed_through_the_real_chain(self) -> None:
        contexts, results, operation_partitions = _operation_chain(
            truncated_partition=True
        )
        partition = results[1]

        tools._authorize_change_decompositions(contexts, results)

        self.assertNotIn("_change_reconciliation_pending", partition)
        for claim in partition["claim_ledger"]:
            self.assertNotIn("structural_contribution", claim["allowed_relations"])
            self.assertNotIn("net_change_contribution_rate", claim["facts"])

        tools._tag_complete_decomposition_reconciliations(
            results, operation_partitions
        )
        tools._seal_claim_ids(results)
        tools._seal_change_reconciliations(results)
        tools._finalize_complete_decomposition_outcomes(
            results, operation_partitions
        )

        overall = results[0]
        self.assertEqual(
            partition["change_reconciliation"],
            {
                "status": "not_reconciled",
                "operation": "complete_change_decomposition",
                "reason_code": "PARTITION_TRUNCATED",
                "overall_request_id": overall["request_id"],
            },
        )
        for claim in partition["claim_ledger"]:
            self.assertNotIn("structural_contribution", claim["allowed_relations"])
            self.assertNotIn("net_change_contribution_rate", claim["facts"])

        public_partition = tools._model_wire_result(partition)
        self.assertEqual(
            public_partition["change_reconciliation"],
            partition["change_reconciliation"],
        )
        self.assertNotIn("_change_reconciliation_pending", public_partition)

    def test_complete_partition_still_reconciles_through_the_same_chain(self) -> None:
        contexts, results, operation_partitions = _operation_chain(
            truncated_partition=False
        )
        partition = results[1]

        tools._authorize_change_decompositions(contexts, results)

        self.assertIn("_change_reconciliation_pending", partition)
        self.assertEqual(
            [
                claim["facts"]["net_change_contribution_rate"]
                for claim in partition["claim_ledger"]
            ],
            ["0.25", "0.75"],
        )
        self.assertTrue(
            all(
                "structural_contribution" in claim["allowed_relations"]
                for claim in partition["claim_ledger"]
            )
        )

        tools._tag_complete_decomposition_reconciliations(
            results, operation_partitions
        )
        tools._seal_claim_ids(results)
        tools._seal_change_reconciliations(results)
        tools._finalize_complete_decomposition_outcomes(
            results, operation_partitions
        )

        reconciliation = partition["change_reconciliation"]
        self.assertEqual(reconciliation["status"], "reconciled")
        self.assertEqual(
            reconciliation["operation"], "complete_change_decomposition"
        )
        self.assertEqual(
            reconciliation["overall_request_id"], results[0]["request_id"]
        )
        self.assertEqual(reconciliation["driver_row_count"], 2)
        self.assertEqual(reconciliation["nonzero_driver_count"], 2)
        self.assertTrue(reconciliation["reconciliation_id"].startswith("reconciliation_"))


if __name__ == "__main__":
    unittest.main()
