"""Focused L3 regression tests for delivery semantic authority and disclosures."""

from __future__ import annotations

import re
import importlib
import os
from pathlib import Path
import sys
import types
import unittest

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = PROFILE_ROOT / "plugins" / "datasage-query" / "contracts"
DELIVERY_PATH = CONTRACT_ROOT / "delivery-semantics.yaml"
DATASETS_PATH = CONTRACT_ROOT / "datasets.yaml"

PACKAGE = "datasage_delivery_l3_semantics_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PROFILE_ROOT / "plugins" / "datasage-query")]
sys.modules.setdefault(PACKAGE, package)
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)


class DeliveryL3SemanticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.delivery = yaml.safe_load(DELIVERY_PATH.read_text(encoding="utf-8"))
        cls.datasets = yaml.safe_load(DATASETS_PATH.read_text(encoding="utf-8"))
        cls.metrics = cls.delivery["metrics"]
        cls.query_tools = importlib.import_module(f"{PACKAGE}.tools")

    def test_quantity_capability_is_shared_unit_scoped_and_available(self) -> None:
        expected_quantity = {
            "warehouse_gross_delivery_quantity",
            "warehouse_return_quantity",
            "warehouse_delivery_quantity",
            "gross_delivery_quantity",
            "return_quantity",
            "delivery_quantity",
            "return_quantity_rate",
            "order_quantity",
        }
        pending = {
            code
            for code, metric in self.metrics.items()
            if metric.get("availability", {}).get("status") == "pending_validation"
        }
        self.assertEqual(set(), pending)

        capability = self.delivery["capability_contracts"]["quantity_metrics"]
        self.assertEqual("delivery_quantity_metrics", capability["code"])
        self.assertEqual("available_with_unit_scope", capability["status"])
        self.assertIs(capability["model_visible"], True)
        self.assertIn("按计量单位分别回答", capability["public_description"])
        self.assertEqual(
            ["m", "y", "kg", "Pcs", "m2", "tao"],
            capability["canonical_units"],
        )
        self.assertEqual("governed_unit_scoped_capability", capability["projection"]["kind"])

        policy_objects = {
            id(self.metrics[code]["unit_policy"]) for code in expected_quantity
        }
        self.assertEqual(1, len(policy_objects))
        for code in expected_quantity:
            metric = self.metrics[code]
            self.assertNotIn("availability", metric)
            self.assertIn("unit", metric["allowed_dimensions"])
            self.assertEqual("group_or_filter", metric["unit_policy"]["mode"])
            self.assertIs(metric["unit_policy"]["require_filter_or_group"], True)
            self.assertIs(metric["unit_policy"]["never_sum_mixed_unit"], True)

        unit = self.delivery["dimensions"]["unit"]
        self.assertEqual(["unit"], unit["columns"])
        self.assertEqual(
            ["m", "y", "kg", "Pcs", "m2", "tao"],
            unit["value_contract"]["allowed_values"],
        )
        self.assertEqual("y", unit["value_contract"]["canonical_aliases"]["码"])
        self.assertEqual("Pcs", unit["value_contract"]["canonical_aliases"]["PCS"])
        self.assertEqual("m2", unit["value_contract"]["canonical_aliases"]["平方"])
        self.assertIn(
            "待业务owner确认",
            unit["value_contract"]["business_meanings"]["tao"],
        )

        physical_keys = {
            "table",
            "tables",
            "column",
            "columns",
            "measure",
            "formula",
            "joins",
            "required_filters",
            "time_field",
        }

        def walk(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(not physical_keys.intersection(value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(capability)

    def test_color_authority_is_transaction_snapshot_with_sku_reference_only(self) -> None:
        for code in ("color", "color_label"):
            with self.subTest(dimension=code):
                definition = self.delivery["dimensions"][code]
                self.assertEqual("fact", definition["source"]["type"])
                self.assertEqual("transaction_fact_snapshot", definition["authority"])
                self.assertEqual(["vk_ods.goods_sku_ods"], definition["reference_sources"])
                self.assertIn("交易快照", definition["lineage"])
                self.assertIn("仅作参考血缘", definition["lineage"])
                self.assertIn("覆盖事实值", definition["lineage"])

        sale_warnings = " ".join(
            self.datasets["datasets"]["vk_dwd.sale_bill_goods_detail_dwd"]["warnings"]
        )
        self.assertIn("transaction fact snapshot", sale_warnings)
        self.assertIn("reference/lineage only", sale_warnings)

    def test_derived_net_and_rate_paths_use_owner_approved_organization(self) -> None:
        derived = (
            "delivery_amount",
            "delivery_amount_original",
            "delivery_quantity",
            "delivery_roll_count",
            "return_amount_rate",
            "return_quantity_rate",
            "return_roll_rate",
        )
        for code in derived:
            with self.subTest(metric=code):
                metric = self.metrics[code]
                self.assertIn("organization", metric["allowed_dimensions"])
                disclosure_ids = {
                    declaration["id"] for declaration in metric["disclosures"]
                }
                self.assertIn(
                    "delivery.net-organization.owner-confirmed-equivalence",
                    disclosure_ids,
                )
                self.assertIn("delivery.net-flow.period-scope", disclosure_ids)
                self.assertIn(
                    "delivery.net-flow.current-master-reclassification", disclosure_ids
                )

        self.assertIn(
            "organization", self.metrics["delivery_amount"]["change_decomposition"]["dimensions"]
        )
        self.assertIn("organization", self.metrics["gross_delivery_amount"]["allowed_dimensions"])
        self.assertIn("organization", self.metrics["return_amount"]["allowed_dimensions"])
        self.assertIn(
            "delivery.gross-organization.fact-source",
            {item["id"] for item in self.metrics["gross_delivery_amount"]["disclosures"]},
        )
        self.assertIn(
            "delivery.return-organization.fact-source",
            {item["id"] for item in self.metrics["return_amount"]["disclosures"]},
        )
        return_organization = self.delivery["return_dimension_overrides"]["organization"]
        self.assertEqual("biz_org", return_organization["columns"][0]["column"])
        self.assertEqual(
            "owner_approved_equivalence",
            return_organization["cross_fact_merge"],
        )

    def test_public_disclosures_cover_net_period_current_master_and_inbound_warehouse(self) -> None:
        public = self.delivery["public_disclosures"]
        self.assertEqual("required_always", public["net_period_scope"]["mode"])
        self.assertEqual(
            "required_always",
            public["warehouse_delivery_amount_original_scope"]["mode"],
        )
        original_physical_scope_text = public["warehouse_delivery_amount_original_scope"]["text"]
        self.assertIn("同币种", original_physical_scope_text)
        self.assertIn("实际退入仓", original_physical_scope_text)
        self.assertIn("负数", original_physical_scope_text)
        self.assertEqual(
            "required_when", public["net_current_master_reclassification"]["mode"]
        )
        self.assertEqual(
            {
                "any_request_dimension_or_filter_present": [
                    "customer_label",
                    "product_category",
                    "ht_product",
                ]
            },
            public["net_current_master_reclassification"]["when"],
        )
        self.assertEqual(
            "required_when", public["return_current_master_reclassification"]["mode"]
        )
        self.assertEqual(
            {
                "any_request_dimension_or_filter_present": [
                    "customer_label",
                    "product_category",
                    "ht_product",
                ]
            },
            public["return_current_master_reclassification"]["when"],
        )
        self.assertIn("unknown/null", public["net_current_master_reclassification"]["text"])
        self.assertIn("不从分组或总体中丢弃", public["net_current_master_reclassification"]["text"])
        self.assertIn("unknown/null", public["return_current_master_reclassification"]["text"])
        self.assertIn("不从分组或总体中丢弃", public["return_current_master_reclassification"]["text"])
        self.assertEqual("required_always", public["return_completed_period"]["mode"])
        self.assertEqual("required_when", public["physical_actual_inbound"]["mode"])
        self.assertEqual("required_when", public["net_organization_equivalence"]["mode"])
        self.assertEqual(
            {"any_request_dimension_or_filter_present": ["organization"]},
            public["net_organization_equivalence"]["when"],
        )
        self.assertEqual("required_always", public["quantity_unit_scope"]["mode"])
        self.assertEqual("required_when", public["final_supplier_scope"]["mode"])
        self.assertEqual("required_when", public["gross_organization_source"]["mode"])
        self.assertEqual(
            {"any_request_dimension_or_filter_present": ["organization"]},
            public["gross_organization_source"]["when"],
        )
        self.assertEqual("required_when", public["return_organization_source"]["mode"])
        self.assertEqual(
            {"any_request_dimension_or_filter_present": ["organization"]},
            public["return_organization_source"]["when"],
        )
        for name, declaration in public.items():
            with self.subTest(disclosure=name):
                self.assertTrue(declaration["text"].strip())
                self.assertIsNone(
                    re.search(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b", declaration["text"])
                )

        physical_net = (
            "warehouse_delivery_amount",
            "warehouse_delivery_quantity",
            "warehouse_delivery_roll_count",
            "warehouse_delivery_amount_original",
        )
        for code in physical_net:
            ids = {item["id"] for item in self.metrics[code]["disclosures"]}
            self.assertIn("delivery.net-flow.period-scope", ids)
            self.assertIn("delivery.physical.net.actual-inbound-warehouse", ids)
            self.assertIn(
                "delivery.net-organization.owner-confirmed-equivalence", ids
            )
        self.assertIn(
            "delivery.warehouse-delivery-amount-original.net-scope",
            {item["id"] for item in self.metrics["warehouse_delivery_amount_original"]["disclosures"]},
        )

        direct_returns = (
            "return_amount",
            "return_amount_original",
            "return_quantity",
            "return_roll_count",
            "return_document_count",
            "returned_delivery_order_count",
            "warehouse_return_amount",
            "warehouse_return_amount_original",
            "warehouse_return_roll_count",
            "warehouse_return_quantity",
        )
        for code in direct_returns:
            with self.subTest(metric=code):
                ids = {item["id"] for item in self.metrics[code]["disclosures"]}
                self.assertIn("delivery.return.completed-settlement-period", ids)

        for code in (
            "return_amount",
            "return_amount_original",
            "return_quantity",
            "return_roll_count",
            "return_document_count",
            "returned_delivery_order_count",
        ):
            with self.subTest(metric=code):
                self.assertIn(
                    "delivery.return.current-master-reclassification",
                    {item["id"] for item in self.metrics[code]["disclosures"]},
                )

        business_net = (
            "delivery_amount",
            "delivery_amount_original",
            "delivery_quantity",
            "delivery_roll_count",
            "return_amount_rate",
            "return_quantity_rate",
            "return_roll_rate",
        )
        for code in business_net:
            ids = {item["id"] for item in self.metrics[code]["disclosures"]}
            self.assertIn("delivery.net-flow.period-scope", ids)
            self.assertIn("delivery.net-flow.current-master-reclassification", ids)

        known_dimensions = set(self.delivery["dimensions"])

        def applies(name: str, request: dict[str, object]) -> bool:
            return self.query_tools._disclosure_applies(
                public[name],
                request=request,
                known_dimension_codes=known_dimensions,
                inventory_scope=None,
                data_state="rows",
                truncated=False,
            )

        self.assertTrue(applies("net_period_scope", {"dimensions": []}))
        self.assertFalse(applies("net_current_master_reclassification", {"dimensions": []}))
        self.assertTrue(
            applies("net_current_master_reclassification", {"dimensions": ["customer_label"]})
        )
        self.assertTrue(
            applies(
                "net_current_master_reclassification",
                {"dimensions": [], "metric_filters": {"product_category": ["x"]}},
            )
        )
        self.assertFalse(
            applies("net_current_master_reclassification", {"dimensions": ["customer"]})
        )
        self.assertFalse(
            applies("return_current_master_reclassification", {"dimensions": []})
        )
        self.assertTrue(
            applies("return_current_master_reclassification", {"dimensions": ["ht_product"]})
        )
        self.assertFalse(applies("gross_organization_source", {"dimensions": []}))
        self.assertTrue(
            applies(
                "gross_organization_source",
                {"dimensions": [], "metric_filters": {"organization": ["x"]}},
            )
        )
        self.assertTrue(
            applies("gross_organization_source", {"dimensions": ["organization"]})
        )
        self.assertFalse(applies("return_organization_source", {"dimensions": []}))
        self.assertTrue(
            applies(
                "return_organization_source",
                {"dimensions": [], "metric_filters": {"organization": ["x"]}},
            )
        )
        self.assertFalse(applies("physical_actual_inbound", {"dimensions": []}))
        self.assertTrue(
            applies("physical_actual_inbound", {"dimensions": ["warehouse"]})
        )
        self.assertFalse(applies("net_organization_equivalence", {"dimensions": []}))
        self.assertTrue(
            applies("net_organization_equivalence", {"dimensions": ["organization"]})
        )

    def test_ready_goods_is_fact_based_and_lifecycle_table_is_reference_only(self) -> None:
        self.assertNotIn("vk_ods.ready_goods_detail", self.delivery["tables"])
        reference = next(
            item
            for item in self.delivery["reference_only_tables"]
            if item["table"] == "vk_ods.ready_goods_detail"
        )
        self.assertEqual("reference_only", reference["role"])
        self.assertIs(reference["executable"], False)
        ready = self.delivery["dimensions"]["ready_goods"]
        self.assertEqual("fact", ready["source"]["type"])
        self.assertEqual(["is_ready"], ready["columns"])
        self.assertIn("备货和试备货", ready["semantics"])

        dataset = self.datasets["datasets"]["vk_ods.ready_goods_detail"]
        self.assertEqual("reference_only", dataset["execution_role"])
        self.assertIs(dataset["executable"], False)
        self.assertNotIn("方案A", DELIVERY_PATH.read_text(encoding="utf-8"))

    def test_department_and_actual_missing_contracts_are_explicit(self) -> None:
        department = self.delivery["dimensions"]["department"]
        self.assertEqual(["customer_dept"], department["columns"])
        self.assertIn("用户只说部门", department["semantics"])
        self.assertIn("不得替代", department["semantics"])
        self.assertEqual(
            ["biz_dept"],
            self.delivery["dimensions"]["business_department"]["columns"],
        )
        self.assertEqual(
            ["biz_region"], self.delivery["dimensions"]["business_region"]["columns"]
        )

        missing = self.delivery["data_state_contract"]["actual_missing"]
        self.assertEqual("missing", missing["state"])
        self.assertIs(missing["numeric_zero_equivalence"], False)
        self.assertIn("不等于业务发生量为零", missing["rule"])
        self.assertEqual(
            "preserve_typed_missing_and_do_not_coalesce_to_zero",
            missing["runtime_requirement"],
        )

        for table in (
            "vk_dwd.sale_bill_goods_detail_dwd",
            "vk_dwd.delivery_return_detail_dwd",
            "vk_dwd.delivery_bill_barcode_detail_dwd",
        ):
            warnings = " ".join(self.datasets["datasets"][table]["warnings"])
            self.assertIn("missing actual state", warnings)
            self.assertIn("not a proven business zero", warnings)

    def test_required_filters_and_currency_policy_remain_governed(self) -> None:
        scopes = self.delivery["execution_policy"]["dataset_mode"]["detail_scopes"]
        self.assertEqual(
            {"bill_status": {"op": "eq", "value": 6}, "is_inner_cus": {"op": "eq", "value": "n"}},
            scopes["delivery"]["required_filters"],
        )
        self.assertEqual(
            {"is_inner_cus": {"op": "eq", "value": "n"}},
            scopes["order"]["required_filters"],
        )
        self.assertEqual(
            {
                "status": {"op": "eq", "value": 4},
                "complnt_type": {"op": "eq", "value": 1},
                "channel_type": {"op": "eq", "value": 1},
                "is_inner_cus": {"op": "eq", "value": "n"},
            },
            scopes["return"]["required_filters"],
        )
        original_codes = [code for code in self.metrics if code.endswith("_original")]
        self.assertEqual(8, len(original_codes))  # Includes the approved original return amount rate.
        for code in original_codes:
            policy = self.metrics[code]["currency_policy"]
            self.assertEqual("original_currency", policy["mode"])
            self.assertIs(policy["require_filter_or_group"], True)
            self.assertIs(policy["never_sum_mixed_currency"], True)


if __name__ == "__main__":
    unittest.main()
