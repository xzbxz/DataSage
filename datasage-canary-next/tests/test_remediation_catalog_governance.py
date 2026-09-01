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
PACKAGE = "datasage_catalog_governance_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules.setdefault(PACKAGE, package)
contracts = importlib.import_module(f"{PACKAGE}.contracts")
governance = importlib.import_module(f"{PACKAGE}.metric_governance")
contract_store = importlib.import_module(f"{PACKAGE}.contract_store")


def _catalog(request):
    return json.loads(contracts.datasage_catalog({"requests": [request]}))


class CatalogGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()

    def tearDown(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()

    def test_full_is_business_only_and_audit_is_complete_governance(self):
        full = _catalog({"domain": "delivery", "view": "full"})
        audit = _catalog({"domain": "delivery", "view": "audit"})
        self.assertEqual("success", full["status"])
        self.assertEqual("success", audit["status"])
        full_result = full["results"][0]
        audit_result = audit["results"][0]
        self.assertEqual("business_full", full_result["projection_mode"])
        self.assertEqual("governance_audit", audit_result["projection_mode"])
        self.assertEqual(27, full_result["metric_count"])
        self.assertEqual(35, audit_result["metric_count"])
        self.assertEqual(
            {"available": 27, "pending_validation": 8},
            audit_result["governance_summary"]["availability"],
        )
        full_text = json.dumps(full_result, ensure_ascii=False, sort_keys=True)
        for forbidden in ("owner_role", "review", "release_blockers", "governance_summary"):
            self.assertNotIn(forbidden, full_text)
        self.assertNotEqual(full["content_hash"], audit["content_hash"])

        allowed_metric_keys = {
            "code",
            "availability",
            "owner_role",
            "lifecycle",
            "review",
            "execution",
            "release",
            "warnings",
            "release_blockers",
        }
        for item in audit_result["metrics"]:
            self.assertEqual(allowed_metric_keys, set(item))
        audit_text = json.dumps(audit_result, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "table",
            "column",
            "formula",
            "validation_gate",
            "required_evidence",
            "activation_authority",
        ):
            self.assertNotIn(forbidden, audit_text)

    def test_all_audit_domains_cover_121_metrics_and_13_pending(self):
        requests = [
            {"domain": domain, "view": "audit"}
            for domain in contracts.DOMAIN_SOURCES
        ]
        payload = json.loads(contracts.datasage_catalog({"requests": requests}))
        self.assertEqual("success", payload["status"])
        self.assertEqual(121, sum(item["metric_count"] for item in payload["results"]))
        self.assertEqual(
            13,
            sum(
                item["governance_summary"]["availability"].get(
                    "pending_validation", 0
                )
                for item in payload["results"]
            ),
        )

    def test_deprecated_warns_and_retired_is_hidden_and_rejected(self):
        base = governance.load_contract(date(2026, 9, 1))
        deprecated = copy.deepcopy(base)
        deprecated["metrics"]["delivery"]["delivery_amount"][
            "lifecycle"
        ] = "deprecated"
        deprecated["metrics"]["delivery"]["delivery_amount"][
            "derived_status"
        ]["warnings"] = ["METRIC_DEPRECATED"]
        with mock.patch.object(
            contracts, "_metric_governance_contract", return_value=deprecated
        ):
            detail = _catalog({"domain": "delivery", "metric": "delivery_amount"})
        self.assertEqual("success", detail["status"])
        self.assertEqual(
            ["METRIC_DEPRECATED"],
            detail["results"][0]["governance_warnings"],
        )

        retired = copy.deepcopy(base)
        retired["metrics"]["delivery"]["delivery_amount"][
            "lifecycle"
        ] = "retired"
        with mock.patch.object(
            contracts, "_metric_governance_contract", return_value=retired
        ):
            detail = _catalog({"domain": "delivery", "metric": "delivery_amount"})
            full = _catalog({"domain": "delivery", "view": "full"})
        self.assertEqual("failed", detail["status"])
        self.assertEqual("METRIC_RETIRED", detail["error"]["code"])
        self.assertNotIn(
            "delivery_amount",
            {item["code"] for item in full["results"][0]["metrics"]},
        )

    def test_invalid_governance_fails_catalog_closed(self):
        with mock.patch.object(
            contracts,
            "_metric_governance_contract",
            side_effect=contracts.ContractFailure(
                "CONTRACT_UNAVAILABLE", "governance unavailable"
            ),
        ):
            payload = _catalog({"domain": "delivery", "view": "audit"})
        self.assertEqual("failed", payload["status"])
        self.assertEqual("CONTRACT_UNAVAILABLE", payload["error"]["code"])

    def test_unknown_metric_keeps_metric_unavailable_taxonomy(self):
        payload = _catalog({"domain": "delivery", "metric": "not_a_metric"})
        self.assertEqual("failed", payload["status"])
        self.assertEqual("METRIC_UNAVAILABLE", payload["error"]["code"])

    def test_audit_rejects_unsafe_owner_if_parser_is_bypassed(self):
        unsafe = governance.load_contract(date(2026, 9, 1))
        unsafe["metrics"]["delivery"]["delivery_amount"][
            "owner_role"
        ] = "vk_dw.secret_column"
        with mock.patch.object(
            contracts, "_metric_governance_contract", return_value=unsafe
        ):
            payload = _catalog({"domain": "delivery", "view": "audit"})
        self.assertEqual("failed", payload["status"])
        self.assertEqual("CONTRACT_UNAVAILABLE", payload["error"]["code"])


if __name__ == "__main__":
    unittest.main()
