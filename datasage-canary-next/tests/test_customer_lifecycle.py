import copy
import hashlib
import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_business_contracts as base

a = importlib.import_module(base.TEST_PACKAGE + ".workflow_customer_audit")
slow = importlib.import_module(base.TEST_PACKAGE + ".workflow_live_slow")


def sample():
    baseline = [
        {"whse_dept": "HCM", "goods_no": "G1", "attr_val": "R", "total_piece": "1.4"},
        {"whse_dept": "HCM", "goods_no": "G1", "attr_val": "R", "total_piece": "1.1"},
        {"whse_dept": "HCM", "goods_no": "G2", "attr_val": "B", "total_piece": "4"},
        {"whse_dept": "HCM", "goods_no": "G3", "attr_val": "Z", "total_piece": "7"},
    ]
    mapping = {
        "productsByCustomer": {
            "1": [{"goods_no": "G1", "whse_dept": "HCM"}, {"goods_no": "G2", "whse_dept": "HCM"}],
            "2": [{"goods_no": "G1", "whse_dept": "HCM"}],
        },
        "customerInfo": {
            "1": {"customer_no": "SYN-C1", "name": "Synthetic One", "sales": "S"},
            "2": {"customer_no": "SYN-C2", "name": "Synthetic Two", "sales": "S"},
        },
        "wecomBySales": {"S": "synthetic-account"},
        "employeeBySales": {"S": {"wecom_account": "synthetic-account", "region": "HCM"}},
    }
    return baseline, mapping, a.wf.contact_plan(baseline, mapping)


class CustomerLifecycleTests(unittest.TestCase):
    def test_population_reconciliation_is_bidirectional_and_independent(self):
        baseline, mapping, plan = sample()
        result = a.verify_plan(baseline, mapping, plan)
        self.assertTrue(result["population_reconciled"])
        self.assertEqual(result["expected_customer_cards"], 2)

        missing_package = copy.deepcopy(plan)
        missing_package["sales_packages"].pop()
        with self.assertRaisesRegex(ValueError, "PACKAGE_POPULATION"):
            a.verify_plan(baseline, mapping, missing_package)

        missing_customer = copy.deepcopy(plan)
        missing_customer["sales_packages"][0]["customers"].pop()
        with self.assertRaisesRegex(ValueError, "CARD_POPULATION"):
            a.verify_plan(baseline, mapping, missing_customer)

        missing_exception = copy.deepcopy(plan)
        missing_exception["audit_rows"].pop()
        with self.assertRaisesRegex(ValueError, "EXCEPTION_POPULATION"):
            a.verify_plan(baseline, mapping, missing_exception)

        extra_exception = copy.deepcopy(plan)
        extra_exception["audit_rows"].append({"product_dept": "HCM", "goods_no": "G9", "color": "Z", "exception": "No Matched Customer"})
        with self.assertRaisesRegex(ValueError, "EXCEPTION_POPULATION"):
            a.verify_plan(baseline, mapping, extra_exception)

    def test_expected_population_preserves_source_business_key_text(self):
        baseline, mapping, _ = sample()
        baseline[0]["goods_no"] = "G1 "
        baseline[1]["goods_no"] = "G1 "
        plan = a.wf.contact_plan(baseline, mapping)
        self.assertEqual(a.verify_plan(baseline, mapping, plan)["population_reconciled"], True)
        self.assertEqual(plan["sales_packages"][0]["customers"][0]["products"], [["G2", "B", 4]])

    def test_current_generation_isolated_from_history(self):
        class Store:
            def rows(self, *args):
                def row(generation):
                    return {
                        "cycle_id": f"ca-hcm-2026-w38-g{generation}",
                        "payload": {"summary": {"department": "HCM", "week": "2026-W38", "generation": generation}},
                    }

                return [row(0), row(1)]

        with patch.object(a.live, "DEPARTMENTS", ("HCM",)):
            current, history = a._current_audit_records(Store(), "2026-W38")
        self.assertEqual([item["generation"] for item in current], [1])
        self.assertEqual([item["generation"] for item in history], [0])

        class Ambiguous(Store):
            def rows(self, *args):
                row = {"cycle_id": "ca-hcm-2026-w38-g1", "payload": {"summary": {"department": "HCM", "week": "2026-W38", "generation": 1}}}
                return [row, copy.deepcopy(row)]

        with patch.object(a.live, "DEPARTMENTS", ("HCM",)):
            with self.assertRaisesRegex(ValueError, "GENERATION_AMBIGUOUS"):
                a._current_audit_records(Ambiguous(), "2026-W38")

    def test_sample_batch_key_and_each_package_scope_are_bound(self):
        scope = {
            "version": 2,
            "week": "2026-W38",
            "departments": {"HCM": {"generation": 1, "scope": "hcm-2026-w38-g1"}},
            "scope_digest": "scope-digest",
            "key": "customer-sample-2026-w38-sscope-digest-v2",
        }

        class Store:
            def rows(self, *args):
                return [{"cycle_id": scope["key"], "status": "planned", "payload": {"scope": scope, "selected": []}}]

        self.assertIsNotNone(a.batch_record(Store(), "2026-W38", scope)[0])
        package = {"department": "HCM", "week": "2026-W38", "generation": 1, "audit_scope": "hcm-2026-w38-g1"}
        a._validate_sample_scope({"scope": scope, "selected": [package]}, scope)
        with self.assertRaisesRegex(ValueError, "PACKAGE_SCOPE"):
            a._validate_sample_scope({"scope": scope, "selected": [{**package, "generation": 0}]}, scope)

    def test_audit_cannot_use_older_generation_than_latest_freeze(self):
        class Store:
            def rows(self, *args):
                return [
                    {"cycle_id": "ca-hcm-2026-w38-g0", "payload": {"summary": {"department": "HCM", "week": "2026-W38", "generation": 0}}},
                    {"cycle_id": "ls-hcm-2026-w38-g1", "payload": {"generation": 1}},
                ]

        with patch.object(a.live, "DEPARTMENTS", ("HCM",)):
            with self.assertRaisesRegex(ValueError, "LATEST_GENERATION_UNAUDITED"):
                a._current_audit_records(Store(), "2026-W38")

    def test_pure_summary_preserves_legacy_batch_counts_separately(self):
        audited_packages = [{"status": "validated", "previously_sampled_owner": True, "previously_sampled_identical": index < 7, "package": {"account": f"old-owner-{index}", "customers": []}} for index in range(8)]
        current = [{"row": {"cycle_id": "ca-hcm-2026-w38-g0"}, "department": "HCM", "generation": 0, "data": {"summary": {"department": "HCM", "week": "2026-W38", "generation": 0, "customer_cards": 8}, "packages": audited_packages}}]
        old_sample = {"selected": [{"account": "old-extra", "customers": []}, {"account": "old-extra-2", "customers": []}], "sent": True, "receipt": {"status": "provider_accepted_not_human_read"}}
        result = a.summarize_records(current, [], "2026-W38", legacy_batch={"cycle_id": "customer-sample-batch-v1", "status": "committed", "payload": old_sample})
        self.assertEqual(result["recorded_history_logical_package_delivery_coverage"], 10)
        self.assertEqual(result["recorded_history_current_content_delivery_coverage"], 9)
        self.assertEqual(result["verified_current_scope_logical_package_delivery_coverage"], 0)
        self.assertEqual(result["coverage_schema"], "customer-package-coverage/v2")

    def test_lifecycle_separates_preparation_from_bound_receipt(self):
        _, _, plan = sample()
        package = plan["sales_packages"][0]
        digest = a.package_digest(package)
        prior = {"owners": {package["account"]}, "digests": {digest}, "proof": {"verified": True, "human_confirmed": "unknown", "receipt_status": "provider_accepted_not_human_read"}}
        life = a._package_lifecycle(package, prepared=True, prior=prior)
        self.assertEqual({"selected", "prepared", "provider_accepted", "human_confirmed", "receipt_bound"}, set(life) & {"selected", "prepared", "provider_accepted", "human_confirmed", "receipt_bound"})
        self.assertTrue(life["selected"] and life["prepared"] and life["provider_accepted"] and life["receipt_bound"])
        self.assertEqual(life["human_confirmed"], "unknown")

        with patch.object(a, "_receipt_binding", return_value={"verified": False, "provider_accepted": False, "human_confirmed": "unknown", "receipt_status": "provider_accepted_not_human_read"}):
            prior = a._prior_customer_delivery({"selected_packages": [package], "phases": {"customer": {"receipt": {"status": "provider_accepted_not_human_read"}}}})
        self.assertFalse(prior["owners"])
        accepted_proof = {"verified": True, "provider_accepted": True, "human_confirmed": "unknown", "receipt_status": "provider_accepted_not_human_read"}
        with patch.object(a, "_receipt_binding", return_value=accepted_proof):
            prior = a._prior_customer_delivery({"selected_packages": [package], "selected_package_digests": [digest], "phases": {"customer": {"evidence": {"customer_package_digests": [digest]}, "receipt": {"status": "provider_accepted_not_human_read"}}}})
        self.assertEqual(prior["owners"], {package["account"]})
        old = a._stored_lifecycle({"status": "validated", "previously_sampled_owner": True, "previously_sampled_identical": True})
        self.assertFalse(old["provider_accepted"])
        self.assertTrue(old["legacy_provider_accepted_claim"] and old["legacy_same_content_claim"])

    def test_receipt_from_another_package_does_not_count(self):
        review = importlib.import_module(base.TEST_PACKAGE + ".workflow_delivery_review")
        current_binding = {"scope": "hcm-2026-w38-g1", "phase": "customer"}
        other_binding = {"scope": "idk-2026-w38-g1", "phase": "customer"}
        accepted = {"status": "provider_accepted_not_human_read", "delivery_binding": other_binding}
        with patch.object(a.delivery, "validate_delivery_binding", return_value=True):
            with patch.object(review, "accepted", side_effect=lambda receipt, expected: receipt.get("delivery_binding") == expected):
                proof = a._receipt_binding(accepted, current_binding)
        self.assertFalse(proof["provider_accepted"])
        self.assertFalse(proof["verified"])

    def test_zip_generation_is_reentrant_and_preserves_collision_evidence(self):
        _, _, plan = sample()
        package = plan["sales_packages"][0]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = a.wf.customer_zip(package, root, "2026-W38")
            first_bytes = first.read_bytes()
            second = a.wf.customer_zip(package, root, "2026-W38")
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, second.read_bytes())
            self.assertTrue((root / ("images_" + a.wf.account_token(package["account"])[:12])).is_dir())

            second.write_bytes(b"old-evidence")
            with self.assertRaisesRegex(a.wf.WorkflowError, "DESTINATION_COLLISION"):
                a.wf.customer_zip(package, root, "2026-W38")
            self.assertEqual(second.read_bytes(), b"old-evidence")

    def test_zip_publish_race_and_stale_image_cache_are_fail_closed(self):
        _, _, plan = sample()
        package = plan["sales_packages"][0]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            token = a.wf.account_token(package["account"])[:12]
            archive = root / f"sales_{token}_2026-W38.zip"
            original_link = a.wf.os.link

            def race(source, target):
                if Path(target) == archive and not archive.exists():
                    archive.write_bytes(b"concurrent-old-evidence")
                return original_link(source, target)

            with patch.object(a.wf.os, "link", side_effect=race):
                with self.assertRaisesRegex(a.wf.WorkflowError, "DESTINATION_COLLISION"):
                    a.wf.customer_zip(package, root, "2026-W38")
            self.assertEqual(archive.read_bytes(), b"concurrent-old-evidence")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = a.wf.customer_zip(package, root, "2026-W38")
            image = root / ("images_" + a.wf.account_token(package["account"])[:12]) / "00000.png"
            image.write_bytes(b"stale-cache")
            with self.assertRaisesRegex(a.wf.WorkflowError, "IMAGE_OUTPUT_COLLISION"):
                a.wf.customer_zip(package, root, "2026-W38")
            self.assertEqual(image.read_bytes(), b"stale-cache")
            self.assertTrue(archive.is_file())

    def test_live_slow_latest_rejects_duplicate_generation(self):
        class Store:
            def rows(self, *args):
                payload = {"generation": 0}
                return [
                    {"cycle_id": "ls-hcm-2026-w38-g0", "payload": payload},
                    {"cycle_id": "ls-hcm-2026-w38-g0-copy", "payload": payload},
                ]

        with self.assertRaisesRegex(ValueError, "MULTIPLE_LIVE_GENERATION"):
            slow.latest(Store(), "HCM", "2026-W38")


if __name__ == "__main__":
    unittest.main()
