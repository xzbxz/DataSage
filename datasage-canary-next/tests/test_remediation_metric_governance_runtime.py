from __future__ import annotations

import copy
from datetime import date
import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(ROOT)
PACKAGE = "datasage_metric_governance_runtime_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules.setdefault(PACKAGE, package)
tools = importlib.import_module(f"{PACKAGE}.tools")
governance = importlib.import_module(f"{PACKAGE}.metric_governance")
contract_store = importlib.import_module(f"{PACKAGE}.contract_store")
runtime_health = importlib.import_module(f"{PACKAGE}.runtime_health")


class MetricGovernanceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()
        self.request = {
            "request_id": "q1",
            "domain": "delivery",
            "metric": "delivery_amount",
        }
        self.semantics = tools._contracts("delivery")[1]

    def tearDown(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()

    def test_missing_review_blocks_release_but_not_active_runtime(self):
        status = governance.metric_status(
            "delivery", "delivery_amount", date(2026, 9, 1)
        )
        self.assertEqual("missing", status["derived_status"]["review"])
        self.assertEqual("allowed", status["derived_status"]["execution"])
        normalized = tools._validate_metric_detail_gate(
            self.request,
            self.semantics,
            observed_on=date(2026, 9, 1),
        )
        self.assertNotIn("_governance_warnings", normalized)

    def test_retired_is_rejected_before_database(self):
        status = governance.metric_status(
            "delivery", "delivery_amount", date(2026, 9, 1)
        )
        status["lifecycle"] = "retired"
        status["derived_status"]["execution"] = "denied"
        status["derived_status"]["warnings"] = ["METRIC_RETIRED"]
        with mock.patch.object(
            tools.metric_governance, "metric_status", return_value=status
        ), mock.patch.object(tools, "_execute_with_source") as execute:
            with self.assertRaises(tools.QueryFailure) as context:
                tools._prepare_one(
                    self.request,
                    deadline_at=None,
                    resolution_cache={},
                    max_unique_lookups=10,
                    preflight_stats={},
                    period_observed_on=date(2026, 9, 1),
                )
        self.assertEqual("METRIC_RETIRED", context.exception.code)
        execute.assert_not_called()

    def test_deprecated_warning_survives_preflight_and_model_projection(self):
        status = governance.metric_status(
            "delivery", "delivery_amount", date(2026, 9, 1)
        )
        status["lifecycle"] = "deprecated"
        status["derived_status"]["execution"] = "allowed_with_warning"
        status["derived_status"]["warnings"] = ["METRIC_DEPRECATED"]
        with mock.patch.object(
            tools.metric_governance, "metric_status", return_value=status
        ):
            prepared = tools._prepare_one(
                self.request,
                deadline_at=None,
                resolution_cache={},
                max_unique_lookups=10,
                preflight_stats={},
                period_observed_on=date(2026, 9, 1),
            )
        self.assertEqual(["METRIC_DEPRECATED"], prepared["governance_warnings"])
        projected = tools._model_wire_result(
            {
                "request_id": "q1",
                "status": "failed",
                "data_state": "unavailable",
                "governance_warnings": prepared["governance_warnings"],
                "claim_ledger": [],
                "error": {
                    "code": "SYNTHETIC",
                    "message": "synthetic",
                    "retryable": False,
                },
            }
        )
        self.assertEqual(["METRIC_DEPRECATED"], projected["governance_warnings"])

    def test_invalid_governance_fails_closed(self):
        with mock.patch.object(
            tools.metric_governance,
            "metric_status",
            side_effect=governance.MetricGovernanceError(
                "METRIC_GOVERNANCE_CONTRACT_INVALID", "invalid"
            ),
        ):
            with self.assertRaises(tools.QueryFailure) as context:
                tools._validate_metric_detail_gate(
                    self.request,
                    self.semantics,
                    observed_on=date(2026, 9, 1),
                )
        self.assertEqual("CONTRACT_UNAVAILABLE", context.exception.code)

    def test_deprecated_warning_survives_readiness_failure(self):
        status = governance.metric_status(
            "delivery", "delivery_amount", date(2026, 9, 1)
        )
        status["lifecycle"] = "deprecated"
        status["derived_status"]["execution"] = "allowed_with_warning"
        status["derived_status"]["warnings"] = ["METRIC_DEPRECATED"]
        with mock.patch.object(
            tools.metric_governance, "metric_status", return_value=status
        ), mock.patch.object(
            runtime_health,
            "query_readiness_status",
            return_value={"ready": False, "reason_code": "DATABASE_UNAVAILABLE"},
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [self.request]},
                    _period_observed_on=date(2026, 9, 1),
                )
            )
        self.assertEqual("failed", payload["status"])
        self.assertEqual(
            ["METRIC_DEPRECATED"],
            payload["results"][0]["governance_warnings"],
        )


if __name__ == "__main__":
    unittest.main()
