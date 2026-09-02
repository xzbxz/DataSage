"""Focused regression tests for the contract-layer remediation changes."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_ROOT = PLUGIN_ROOT / "contracts"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

PACKAGE = "datasage_contract_remediation_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

capability_contract = importlib.import_module(f"{PACKAGE}.capability_contract")
contracts = importlib.import_module(f"{PACKAGE}.contracts")


class ContractRemediationTests(unittest.TestCase):
    def _catalog(self, request: dict[str, object]) -> dict[str, object]:
        return json.loads(contracts.datasage_catalog({"requests": [request]}))

    def test_metric_detail_binds_common_execution_contract_identities(self) -> None:
        payload = self._catalog({"domain": "delivery", "metric": "delivery_amount"})
        self.assertEqual("success", payload["status"])
        detail = payload["results"][0]
        versions = detail["source_versions"]

        datasets = yaml.safe_load(
            (CONTRACT_ROOT / "datasets.yaml").read_text(encoding="utf-8")
        )
        policy = yaml.safe_load(
            (CONTRACT_ROOT / "query-policy.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(datasets["version"], versions["datasets"]["version"])
        self.assertEqual(policy["version"], versions["query_policy"]["version"])
        self.assertEqual(
            contracts.contract_store.content_signature(
                contracts._DATASETS_CONTRACT_PATH
            )[1],
            versions["datasets"]["content_sha256"],
        )
        self.assertEqual(
            contracts.contract_store.content_signature(
                capability_contract.QUERY_POLICY_PATH
            )[1],
            versions["query_policy"]["content_sha256"],
        )
        self.assertRegex(versions["datasets"]["content_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            versions["query_policy"]["content_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertNotIn("detail_receipt", detail)

    def test_metric_detail_source_identity_changes_when_common_contract_digest_changes(self) -> None:
        original_signature = contracts.contract_store.content_signature
        datasets_path = contracts._DATASETS_CONTRACT_PATH
        policy_path = capability_contract.QUERY_POLICY_PATH

        for changed_path in (datasets_path, policy_path):
            with self.subTest(changed_path=changed_path):
                digests = {
                    datasets_path: "a" * 64,
                    policy_path: "b" * 64,
                }

                def signature(path: str) -> tuple[str, str]:
                    if path in digests:
                        return path, digests[path]
                    return original_signature(path)

                with mock.patch.object(
                    contracts.contract_store,
                    "content_signature",
                    side_effect=signature,
                ):
                    first = self._catalog(
                        {"domain": "delivery", "metric": "delivery_amount"}
                    )["results"][0]
                    digests[changed_path] = "c" * 64
                    second = self._catalog(
                        {"domain": "delivery", "metric": "delivery_amount"}
                    )["results"][0]

                self.assertNotEqual(
                    first["source_versions"][
                        "datasets" if changed_path == datasets_path else "query_policy"
                    ]["content_sha256"],
                    second["source_versions"][
                        "datasets" if changed_path == datasets_path else "query_policy"
                    ]["content_sha256"],
                )

    def test_customer_risk_related_metric_refs_are_explicit_and_projected(self) -> None:
        expected = yaml.safe_load(
            (CONTRACT_ROOT / "customer_risk-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )["related_metric_refs"]
        for request in (
            {"domain": "customer_risk"},
            {"domain": "customer_risk", "view": "expert_index"},
            {"domain": "customer_risk", "metric": "formal_receivable_turnover_days"},
        ):
            with self.subTest(request=request):
                result = self._catalog(request)["results"][0]
                self.assertEqual(expected, result["related_metric_refs"])
                self.assertNotIn("evidence_axes", result)
                for reference in result["related_metric_refs"]:
                    self.assertEqual({"domain", "metric"}, set(reference))

    def test_related_metric_refs_reject_missing_or_unavailable_metrics(self) -> None:
        semantics = contracts._read_yaml(
            capability_contract.DOMAIN_SOURCES["customer_risk"]["semantics"]
        )

        legacy = copy.deepcopy(semantics)
        legacy["evidence_axes"] = {}
        with self.assertRaises(contracts.ContractFailure) as legacy_error:
            contracts._related_metric_refs_projection("customer_risk", legacy)
        self.assertEqual("CONTRACT_UNAVAILABLE", legacy_error.exception.code)

        missing = copy.deepcopy(semantics)
        missing["related_metric_refs"][0] = {
            "domain": "receivable",
            "metric": "not_a_metric",
        }
        with self.assertRaises(contracts.ContractFailure) as missing_error:
            contracts._related_metric_refs_projection("customer_risk", missing)
        self.assertEqual("CONTRACT_UNAVAILABLE", missing_error.exception.code)

        unavailable = copy.deepcopy(semantics)
        unavailable["related_metric_refs"][0] = {
            "domain": "target",
            "metric": "receipt_allocated_target_amount",
        }
        with self.assertRaises(contracts.ContractFailure) as unavailable_error:
            contracts._related_metric_refs_projection("customer_risk", unavailable)
        self.assertEqual("CONTRACT_UNAVAILABLE", unavailable_error.exception.code)

    def test_malformed_unavailable_metric_fails_catalog_compile(self) -> None:
        semantics = contracts._read_yaml(
            capability_contract.DOMAIN_SOURCES["delivery"]["semantics"]
        )
        malformed = copy.deepcopy(semantics)
        malformed["metrics"]["delivery_amount"]["availability"] = {
            "status": "pending_validation"
        }
        with self.assertRaises(contracts.ContractFailure) as caught:
            contracts._model_semantic_projection("delivery", malformed)
        self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)

    def test_availability_validator_separates_shape_validation_from_denial(self) -> None:
        valid_pending = {
            "availability": {
                "status": "pending_validation",
                "error_code": "PENDING_REVIEW",
                "message": "owner review required",
            }
        }
        self.assertEqual(
            "pending_validation",
            capability_contract.validate_availability(valid_pending),
        )
        with self.assertRaises(capability_contract.AvailabilityContractError):
            capability_contract.ensure_available(valid_pending)
        with self.assertRaisesRegex(
            capability_contract.AvailabilityContractError,
            "不可用指标缺少结构化错误定义",
        ):
            capability_contract.validate_availability(
                {"availability": {"status": "pending_validation"}}
            )

    def test_receipt_target_is_monthly_and_scope_disclosures_are_present(self) -> None:
        target = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "month", target["metrics"]["receipt_target_amount"]["time_granularity"]
        )
        self.assertTrue(target["metrics"]["delivery_target_amount"]["disclosures"])
        self.assertTrue(target["metrics"]["receipt_target_amount"]["disclosures"])

        inventory = yaml.safe_load(
            (CONTRACT_ROOT / "inventory-semantics.yaml").read_text(encoding="utf-8")
        )
        for metric in (
            "month_end_inventory_quantity",
            "month_end_inventory_cost_original",
            "month_end_inventory_ddp_rmb",
            "month_end_inventory_ddp_original",
        ):
            with self.subTest(metric=metric):
                self.assertTrue(inventory["metrics"][metric]["disclosures"])

    def test_receipt_internal_customer_override_and_target_history_wording(self) -> None:
        receipt = yaml.safe_load(
            (CONTRACT_ROOT / "receipt-semantics.yaml").read_text(encoding="utf-8")
        )
        disclosures = receipt["default_disclosures"]
        override = next(
            item
            for item in disclosures
            if item["id"] == "receipt.domain.scope.explicit-internal-filter"
        )
        self.assertEqual("required_when", override["mode"])
        self.assertEqual(
            ["internal_customer"],
            override["when"]["any_request_dimension_or_filter_present"],
        )
        self.assertIn("本次实际筛选", override["text"])

        target = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        for metric in ("delivery_target_completion", "receipt_target_completion"):
            with self.subTest(metric=metric):
                text = "\n".join(
                    declaration["text"]
                    for declaration in target["metrics"][metric]["disclosures"]
                    if isinstance(declaration, dict) and "text" in declaration
                )
                self.assertNotIn("本月使用完整月目标与截至当前实际", text)
                self.assertIn("所选期间", text)

    def test_datasets_domain_values_close_over_supported_domains(self) -> None:
        datasets = yaml.safe_load(
            (CONTRACT_ROOT / "datasets.yaml").read_text(encoding="utf-8")
        )
        supported = set(capability_contract.DOMAIN_SOURCES) | {"shared"}
        for table, definition in datasets["datasets"].items():
            with self.subTest(table=table):
                self.assertTrue(set(definition.get("domains", [])) <= supported)
                self.assertNotIn("order", definition.get("domains", []))


if __name__ == "__main__":
    unittest.main()
