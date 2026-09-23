"""Target-map admission tests for the purchase price runner."""

import importlib
import unittest

import test_business_contracts as base


runner = importlib.import_module(base.TEST_PACKAGE + ".purchase_price_runner")


class PurchasePriceTargetSchemaTests(unittest.TestCase):
    def test_safe_robot_reference_is_accepted_without_url(self):
        target = {
            "platform": "wecom_webhook",
            "target_kind": "robot_group",
            "target_ref": "a" * 64,
            "webhook_ref": "purchase_price",
        }
        value = runner._validate_group_target({"target_map": {"purchase": target}}, required=True)
        self.assertEqual(target, value["purchase"])

    def test_raw_url_test_reference_and_non_digest_are_rejected(self):
        cases = (
            {
                "platform": "wecom_webhook",
                "target_kind": "robot_group",
                "target_ref": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
                "webhook_ref": "purchase_price",
            },
            {
                "platform": "wecom_webhook",
                "target_kind": "robot_group",
                "target_ref": "a" * 64,
                "webhook_ref": "test_webhook",
            },
            {
                "platform": "wecom_webhook",
                "target_kind": "robot_group",
                "target_ref": "not-a-digest",
                "webhook_ref": "purchase_price",
            },
        )
        for target in cases:
            with self.subTest(target=target), self.assertRaisesRegex(Exception, "REFERENCE|TARGET"):
                runner._validate_group_target({"target_map": {"purchase": target}}, required=True)

    def test_existing_appchat_target_remains_supported(self):
        target = {
            "platform": "wecom_app_http",
            "app_name": "existing-app",
            "corp_id": "corp",
            "agent_id": "1000043",
            "target_kind": "appchat",
            "target_id": "purchase-appchat",
        }
        value = runner._validate_group_target({"target_map": {"purchase": target}}, required=True)
        self.assertEqual(target, value["purchase"])

    def test_appchat_rejects_extra_secret_fields_and_control_characters(self):
        base_target = {
            "platform": "wecom_app_http",
            "app_name": "existing-app",
            "corp_id": "corp",
            "agent_id": "1000043",
            "target_kind": "appchat",
            "target_id": "purchase-appchat",
        }
        for target in (
            {**base_target, "webhook_url": "https://secret.invalid"},
            {**base_target, "target_id": "purchase\nappchat"},
        ):
            with self.subTest(target=target), self.assertRaisesRegex(Exception, "TARGET|REQUIRED"):
                runner._validate_group_target({"target_map": {"purchase": target}}, required=True)


if __name__ == "__main__":
    unittest.main()
