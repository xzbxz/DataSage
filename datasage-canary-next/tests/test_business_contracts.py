from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
TEST_PACKAGE = "datasage_query_contract_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class BusinessContractTests(unittest.TestCase):
    def test_all_datasage_skills_require_the_plugin_toolset(self) -> None:
        for relative_path in (
            "skills/datasage/SKILL.md",
            "skills/datasage/datasage-query-patterns/SKILL.md",
            "skills/common-data-foundation/SKILL.md",
        ):
            content = (PROFILE_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("requires_toolsets: [datasage-query]", content)

    def test_top_n_truncation_guidance_depends_on_returned_state(self) -> None:
        content = (
            PROFILE_ROOT / "skills/datasage/datasage-query-patterns/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Read `truncated` and `data_state` exactly as returned", content)
        self.assertIn("When\n   the result is not truncated", content)
        self.assertNotIn('Expect `data_state: "truncated"`', content)

    def test_domain_analysis_seeds_use_an_adaptive_soft_budget(self) -> None:
        for domain in ("receipt", "receivable", "inventory", "target"):
            content = (
                PROFILE_ROOT
                / "skills"
                / f"{domain}-query"
                / "references"
                / "planner-contract.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("自适应软预算", content)
            self.assertIn("不设固定查询、追问或轮数上限", content)
            self.assertNotIn("最多在首批结果暴露实质缺口时追加一次", content)

    def test_delivery_planner_has_no_dev1_acceptance_orphan(self) -> None:
        for relative_path in (
            "skills/delivery-query/references/planner-contract.yaml",
            "plugins/datasage-query/contracts/delivery-semantics.yaml",
        ):
            content = (PROFILE_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("phase_a_acceptance", content)
            self.assertNotIn("dev1_single_metric_vertical_slice", content)

    def test_all_model_catalog_details_avoid_legacy_driver_vocabulary(self) -> None:
        checked_metrics = 0
        structural_metrics = 0
        for domain in (
            "delivery",
            "receipt",
            "receivable",
            "target",
            "customer_risk",
            "inventory",
        ):
            summary = json.loads(
                contracts.datasage_catalog({"requests": [{"domain": domain}]})
            )
            self.assertEqual("success", summary["status"], domain)
            for metric in summary["results"][0]["metrics"]:
                payload = json.loads(
                    contracts.datasage_catalog(
                        {
                            "requests": [
                                {"domain": domain, "metric": metric["code"]}
                            ]
                        }
                    )
                )
                self.assertEqual("success", payload["status"], metric["code"])
                serialized = json.dumps(payload, ensure_ascii=False).casefold()
                self.assertNotIn("driver", serialized, metric["code"])
                if metric["supports_change_decomposition"]:
                    self.assertIn(
                        "structural_contributor_count_semantics", serialized
                    )
                    structural_metrics += 1
                checked_metrics += 1
        self.assertGreater(checked_metrics, 100)
        self.assertGreater(structural_metrics, 0)

    def test_model_wire_renames_legacy_reconciliation_fields_without_mutation(self) -> None:
        internal_reconciliation = {
            "driver_projection_fingerprint": "projection-1",
            "driver_current_sum": 12,
            "driver_comparison_sum": 8,
            "driver_delta_sum": 4,
            "returned_driver_row_count": 2,
            "unreturned_driver_row_count": 1,
            "driver_claim_ids": ["claim-1"],
            "driver_row_count": 3,
            "returned_nonzero_driver_count": 1,
            "nonzero_driver_count_scope": "returned_rows_only",
            "nonzero_driver_count": 1,
        }
        projected = tools._model_wire_result(
            {
                "request_id": "partition",
                "change_reconciliation": internal_reconciliation,
            }
        )
        serialized = json.dumps(projected, ensure_ascii=False).casefold()
        self.assertNotIn("driver", serialized)
        public = projected["change_reconciliation"]
        self.assertEqual(["claim-1"], public["structural_contributor_claim_ids"])
        self.assertEqual(3, public["full_partition_row_count"])
        self.assertEqual(1, public["returned_nonzero_contributor_count"])
        self.assertIn("driver_claim_ids", internal_reconciliation)

    def test_shared_dimension_labels_are_business_specific(self) -> None:
        for domain in ("delivery", "customer_risk"):
            projection = contracts._domain_contract(domain, "planner")["planner"]
            labels = {
                dimension["code"]: dimension["label"]
                for dimension in projection["dimensions"]
            }
            self.assertEqual("客户部门", labels["department"])
            self.assertEqual("业务组织", labels["organization"])


if __name__ == "__main__":
    unittest.main()
