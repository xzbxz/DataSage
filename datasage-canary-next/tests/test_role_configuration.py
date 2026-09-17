"""Offline role configuration tests using synthetic public/private inputs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from role_config_fixture import REGIONS, legacy_document as _legacy_document, public_document as _public_document


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "datasage-query"
_SPEC = importlib.util.spec_from_file_location(
    "role_configuration_under_test",
    PLUGIN / "workflow_roles.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load workflow_roles.py")
roles = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(roles)


class RoleConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        public_path = self.root / roles.LEGACY_RELATIVE_PATH
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(json.dumps(_public_document(), indent=2), encoding="utf-8")
        self.legacy_path = self.root / "legacy-recipient-reference.json"
        self.legacy_path.write_text(json.dumps(_legacy_document(), indent=2), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_legacy_returns_only_metadata(self) -> None:
        result = roles.prepare(self.legacy_path, self.root)
        self.assertEqual("ready_to_import", result["status"])
        self.assertEqual(len(REGIONS), result["region_count"])
        self.assertEqual(len(REGIONS), result["executor_count"])
        self.assertEqual(len(REGIONS), result["manager_count"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("syn-exec", encoded)
        self.assertNotIn("Synthetic Executor", encoded)

    def test_import_then_load_derives_public_departments(self) -> None:
        destination = self.root / roles.ACTIVE_RELATIVE_PATH
        source_before = hashlib.sha256(self.legacy_path.read_bytes()).hexdigest()
        imported = roles.import_legacy(self.legacy_path, destination, self.root)
        self.assertEqual("imported", imported["status"])
        self.assertEqual(source_before, hashlib.sha256(self.legacy_path.read_bytes()).hexdigest())
        self.assertNotIn("syn-exec", json.dumps(imported, ensure_ascii=False))

        reference = roles.load(self.root)
        self.assertEqual(["HCM"], reference["regions"]["HCM"]["departments"])
        self.assertEqual(
            ["BKK Sales"],
            reference["regions"]["BKK"]["dynamic_sales_departments"],
        )
        self.assertEqual("synthetic-exec-hcm", reference["regions"]["HCM"]["executors"][0]["account"])

    def test_import_round_trip_preserves_private_targets_and_metadata_exactly(self) -> None:
        destination = self.root / roles.ACTIVE_RELATIVE_PATH
        roles.import_legacy(self.legacy_path, destination, self.root)
        legacy = _legacy_document()
        public = _public_document()
        reference = roles.load(self.root)

        for region in public["regions"]:
            # Private personnel targets are copied byte-for-byte as structured
            # values; only the public department fields are re-derived.
            self.assertEqual(
                legacy["regions"][region]["executors"],
                reference["regions"][region]["executors"],
            )
            self.assertEqual(
                legacy["regions"][region]["managers"],
                reference["regions"][region]["managers"],
            )
            self.assertEqual(
                public["regions"][region]["departments"],
                reference["regions"][region]["departments"],
            )
            self.assertEqual(
                public["regions"][region]["task_sales_departments"],
                reference["regions"][region]["dynamic_sales_departments"],
            )
        self.assertEqual(legacy["price_manager_fixed"], reference["price_manager_fixed"])
        self.assertEqual(legacy["purchase_target"], reference["purchase_target"])
        self.assertEqual(legacy["approval"], reference["approval"])
        self.assertEqual(legacy["reference"], reference["reference"])

    def test_missing_active_configuration_does_not_fallback_to_report_runs(self) -> None:
        old_location = self.root / "report_runs" / "reminder_acceptance" / "legacy-recipient-reference.json"
        old_location.parent.mkdir(parents=True, exist_ok=True)
        old_location.write_bytes(self.legacy_path.read_bytes())
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.load(self.root)
        self.assertEqual("ROLE_CONFIGURATION_REQUIRED", caught.exception.code)
        diagnosis = roles.diagnose(self.root)
        self.assertEqual("blocked", diagnosis["status"])
        self.assertEqual("ROLE_CONFIGURATION_REQUIRED", diagnosis["code"])
        self.assertNotIn("syn-exec", json.dumps(diagnosis, ensure_ascii=False))

    def test_import_is_no_clobber_and_destination_stays_in_private_local(self) -> None:
        destination = self.root / roles.ACTIVE_RELATIVE_PATH
        roles.import_legacy(self.legacy_path, destination, self.root)
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.import_legacy(self.legacy_path, destination, self.root)
        self.assertEqual("ROLE_CONFIGURATION_EXISTS", caught.exception.code)
        outside = self.root / "plugins" / "datasage-query" / "should-not-be-written.json"
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.import_legacy(self.legacy_path, outside, self.root)
        self.assertEqual("ROLE_IMPORT_DESTINATION_OUTSIDE_PRIVATE_LOCAL", caught.exception.code)
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.import_legacy(self.legacy_path, destination, self.root, overwrite=True)
        self.assertEqual("ROLE_CONFIGURATION_REPLACE_REQUIRES_REVIEW", caught.exception.code)

    def test_import_rejects_reparse_point_without_following_it(self) -> None:
        local = self.root / "local"
        local.mkdir()
        with mock.patch.object(roles, "_is_reparse_point", return_value=True):
            with self.assertRaises(roles.RoleConfigurationError) as caught:
                roles.import_legacy(
                    self.legacy_path,
                    local / "workflow-roles.json",
                    self.root,
                )
        self.assertEqual("ROLE_IMPORT_DESTINATION_OUTSIDE_PRIVATE_LOCAL", caught.exception.code)
        self.assertFalse((local / "workflow-roles.json").exists())

    def test_duplicate_private_target_fails_validation(self) -> None:
        legacy = _legacy_document()
        duplicate = json.loads(json.dumps(legacy))
        duplicate["regions"]["HCM"]["executors"].append(
            {"account": "synthetic-exec-hcm", "name": "Synthetic Executor HCM duplicate"}
        )
        private_path = self.root / "private.json"
        private_path.write_text(
            json.dumps(roles._legacy_to_private(legacy, _public_document(), raw=self.legacy_path.read_bytes())),
            encoding="utf-8",
        )
        document = json.loads(private_path.read_text(encoding="utf-8"))
        document["regions"]["HCM"]["executors"].append(
            {"account": "synthetic-exec-hcm", "name": "Synthetic Executor HCM duplicate"}
        )
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.validate(document, self.root)
        self.assertEqual("ROLE_CONFIGURATION_DUPLICATE", caught.exception.code)

    def test_legacy_public_department_drift_is_rejected(self) -> None:
        drift = _legacy_document()
        drift["regions"]["HCM"]["dynamic_sales_departments"] = ["HCM Other"]
        drift_path = self.root / "drift.json"
        drift_path.write_text(json.dumps(drift), encoding="utf-8")
        with self.assertRaises(roles.RoleConfigurationError) as caught:
            roles.prepare(drift_path, self.root)
        self.assertEqual("ROLE_LEGACY_PUBLIC_DRIFT", caught.exception.code)

    def test_explicit_wrong_or_non_integer_legacy_versions_are_rejected(self) -> None:
        for version in (True, 1.0, 2):
            with self.subTest(version=version):
                wrong = _legacy_document()
                wrong["version"] = version
                wrong_path = self.root / "wrong-version.json"
                wrong_path.write_text(json.dumps(wrong), encoding="utf-8")
                with self.assertRaises(roles.RoleConfigurationError) as caught:
                    roles.prepare(wrong_path, self.root)
                self.assertEqual("ROLE_LEGACY_SOURCE_INVALID", caught.exception.code)

    def test_active_schema_requires_integer_version_one(self) -> None:
        private = roles._legacy_to_private(
            _legacy_document(), _public_document(), raw=self.legacy_path.read_bytes()
        )
        for version in (True, 1.0, 2):
            with self.subTest(version=version):
                wrong = json.loads(json.dumps(private))
                wrong["version"] = version
                with self.assertRaises(roles.RoleConfigurationError) as caught:
                    roles.validate(wrong, self.root)
                self.assertEqual("ROLE_CONFIGURATION_INVALID", caught.exception.code)

    def test_public_contract_requires_integer_version_one(self) -> None:
        public_path = self.root / roles.LEGACY_RELATIVE_PATH
        original = _public_document()
        for version in (True, 1.0, 2):
            with self.subTest(version=version):
                wrong = json.loads(json.dumps(original))
                wrong["version"] = version
                public_path.write_text(json.dumps(wrong), encoding="utf-8")
                with self.assertRaises(roles.RoleConfigurationError) as caught:
                    roles.prepare(self.legacy_path, self.root)
                self.assertEqual("ROLE_PUBLIC_CONTRACT_INVALID", caught.exception.code)
        public_path.write_text(json.dumps(original), encoding="utf-8")

    def test_contracts_capture_customer_fallback_and_fabric_start(self) -> None:
        profit = (ROOT / "plugins/datasage-query/contracts/profit-semantics.yaml").read_text(encoding="utf-8")
        fabric = (ROOT / "plugins/datasage-query/contracts/delivery-semantics.yaml").read_text(encoding="utf-8")
        self.assertIn("销售收入的正数部分按1%补入", profit)
        self.assertIn('source_history_start: "2025-01-01"', fabric)


if __name__ == "__main__":
    unittest.main()
