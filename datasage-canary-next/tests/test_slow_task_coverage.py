import copy
import importlib
import unittest

import test_business_contracts as base


coverage = importlib.import_module(base.TEST_PACKAGE + ".slow_task_coverage")


def synthetic_inputs():
    baseline = [
        {"whse_dept": "HCM", "goods_no": "G1", "attr_val": "Red", "total_piece": "1.4"},
        {"whse_dept": "HCM", "goods_no": "G1", "attr_val": "Red", "total_piece": "1.1"},
        {"whse_dept": "HCM", "goods_no": "G2", "attr_val": "Blue", "total_piece": "4"},
        {"whse_dept": "HCM", "goods_no": "G3", "attr_val": "Unmatched", "total_piece": "7"},
    ]
    mapping = {
        "productsByCustomer": {
            "c1": [{"goods_no": "G1", "whse_dept": "HCM"}, {"goods_no": "G1", "whse_dept": "HCM"}, {"goods_no": "G2", "whse_dept": "HCM"}],
            "c2": [{"goods_no": "G1", "whse_dept": "HCM"}],
            "c3": [{"goods_no": "G2", "whse_dept": "HCM"}],
        },
        "customerInfo": {
            "c1": {"customer_no": "C-1", "name": "Customer One", "sales": "S"},
            "c2": {"customer_no": "C-2", "name": "Customer Two", "sales": "S"},
            "c3": {"customer_no": "C-3", "name": None, "sales": "S"},
        },
        "wecomBySales": {"S": "sales-account"},
        "employeeBySales": {"S": {"wecom_account": "sales-account", "region": "HCM"}},
    }
    return baseline, mapping


class SlowTaskCoverageTests(unittest.TestCase):
    def test_relation_count_is_not_customer_count_and_unmatched_grain_is_visible(self):
        baseline, mapping = synthetic_inputs()
        result = coverage.build_coverage(baseline, mapping)
        population = result["population"]
        self.assertEqual(population["candidate_product_grains"], 3)
        self.assertEqual(population["matched_product_grains"], 2)
        self.assertEqual(population["unmatched_product_grains"], 1)
        self.assertEqual(population["mapped_customer_ids"], 3)
        self.assertEqual(population["assigned_customer_ids"], 2)
        self.assertEqual(population["unassigned_customer_ids"], 1)
        self.assertEqual(population["relation_count"], 4)
        self.assertEqual(population["unmatched_product_relation_count"], 1)
        self.assertEqual(result["reason_counts"], {"Missing Customer Name": 1, "No Matched Customer": 1})
        self.assertEqual(result["reason_relation_counts"], {"Missing Customer Name": 1, "No Matched Customer": 1})
        self.assertEqual(result["reason_customer_counts"], {"Missing Customer Name": 1})

    def test_duplicate_relation_does_not_duplicate_customer_or_grain(self):
        baseline, mapping = synthetic_inputs()
        result = coverage.build_coverage(baseline, mapping)
        g1_rows = [row for row in result["relations"] if row["goods_no"] == "G1" and row["status"] != "unmatched_product"]
        self.assertEqual(len(g1_rows), 2)
        self.assertEqual(len({row["customer_id"] for row in g1_rows}), 2)

    def test_invalid_identity_is_unassigned_with_reason_and_not_packaged(self):
        baseline, mapping = synthetic_inputs()
        plan = {"sales_packages": [{"account": "sales-account", "region": "HCM", "sales_name": "S", "customers": [{"customer_id": "c1", "customer_no": "C-1", "customer_name": "Customer One", "products": [["G1", "Red", 3], ["G2", "Blue", 4]]}]}]}
        result = coverage.build_coverage(baseline, mapping, plan)
        self.assertFalse(result["plan_checks"]["passed"])
        self.assertIn("c2", result["plan_checks"]["missing_customer_ids"])
        self.assertIn("Missing Customer Name", result["reason_counts"])

    def test_plan_duplicate_customer_and_wrong_product_are_detected(self):
        baseline, mapping = synthetic_inputs()
        plan = {"sales_packages": [{"account": "sales-account", "region": "HCM", "sales_name": "S", "customers": [
            {"customer_id": "c1", "customer_no": "C-1", "customer_name": "Customer One", "products": [["G1", "Red", 99]]},
            {"customer_id": "c1", "customer_no": "C-1", "customer_name": "Customer One", "products": [["G1", "Red", 3]]},
        ]}]}
        result = coverage.build_coverage(baseline, mapping, plan)
        checks = result["plan_checks"]
        self.assertIn("c1", checks["duplicate_customer_ids"])
        self.assertIn("c1", checks["cross_package_duplicate_customer_ids"])
        self.assertIn("c1", checks["product_mismatches"])
        self.assertFalse(checks["passed"])

    def test_product_dept_and_personnel_region_are_separate_report_fields(self):
        baseline, mapping = synthetic_inputs()
        mapping = copy.deepcopy(mapping)
        mapping["employeeBySales"]["S"]["region"] = "IDK"
        result = coverage.build_coverage(baseline, mapping)
        row = next(item for item in result["relations"] if item["customer_id"] == "c1")
        self.assertEqual(row["product_dept"], "HCM")
        self.assertEqual(row["personnel_region"], "IDK")
        sheets = coverage.coverage_sheets(result, "HCM")
        headers = next(sheet[1] for sheet in sheets if sheet[0] == "Coverage")
        self.assertIn("Product Dept", headers)
        self.assertIn("Personnel Region", headers)
        self.assertEqual([sheet[0] for sheet in sheets], ["Summary", "Coverage", "Exceptions", "Unmatched Products", "Notes"])

    def test_cross_department_same_customer_is_one_card_with_aggregated_rounding(self):
        baseline = [
            {"whse_dept": "HCM", "goods_no": "G1", "attr_val": "Red", "total_piece": "1.4"},
            {"whse_dept": "IDK", "goods_no": "G1", "attr_val": "Red", "total_piece": "1.1"},
        ]
        mapping = {
            "productsByCustomer": {"c1": [{"goods_no": "G1", "whse_dept": "HCM"}, {"goods_no": "G1", "whse_dept": "IDK"}]},
            "customerInfo": {"c1": {"customer_no": "C-1", "name": "Customer One", "sales": "S"}},
            "wecomBySales": {"S": "sales-account"},
            "employeeBySales": {"S": {"wecom_account": "sales-account", "region": "HCM"}},
        }
        plan = {"sales_packages": [{"account": "sales-account", "region": "HCM", "sales_name": "S", "customers": [{"customer_id": "c1", "customer_no": "C-1", "customer_name": "Customer One", "products": [["G1", "Red", 3]]}]}]}
        result = coverage.build_coverage(baseline, mapping, plan)
        self.assertEqual(result["population"]["relation_count"], 2)
        self.assertEqual(result["population"]["matched_customer_ids"], 1)
        self.assertEqual(result["population"]["cross_product_dept_customer_count"], 1)
        self.assertTrue(result["plan_checks"]["passed"])
        self.assertIsNone(result['population']['generated_customer_ids'])
        generated = coverage.build_coverage(baseline, mapping, plan, generated_customer_ids=['c1'])
        self.assertEqual(1, generated['population']['generated_customer_ids'])
        with self.assertRaisesRegex(ValueError, 'GENERATED_POPULATION_MISMATCH'):
            coverage.build_coverage(baseline, mapping, plan, generated_customer_ids=[])


if __name__ == "__main__":
    unittest.main()
