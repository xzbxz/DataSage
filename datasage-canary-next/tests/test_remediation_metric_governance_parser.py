from __future__ import annotations

import copy
from datetime import date, datetime
import importlib
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(ROOT)
PACKAGE = "datasage_metric_governance_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules.setdefault(PACKAGE, package)
governance = importlib.import_module(f"{PACKAGE}.metric_governance")
contract_store = importlib.import_module(f"{PACKAGE}.contract_store")


class MetricGovernanceParserTests(unittest.TestCase):
    def setUp(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()

    def tearDown(self) -> None:
        contract_store.reset_contract_snapshot_for_tests()

    def test_real_contract_reports_truthful_121_metric_state(self):
        result = governance.load_contract(date(2026, 9, 1))
        summary = result["summary"]
        self.assertEqual(121, summary["metric_count"])
        self.assertEqual(
            {"available": 108, "pending_validation": 13},
            summary["availability"],
        )
        self.assertEqual({"active": 121}, summary["lifecycle"])
        self.assertEqual({"missing": 121}, summary["review"])
        self.assertEqual({"allowed": 108, "denied": 13}, summary["execution"])
        self.assertEqual({"pass": 0, "block": 121}, summary["release"])
        self.assertEqual(
            108,
            summary["blocker_counts"]["METRIC_GOVERNANCE_OWNER_MISSING"],
        )
        self.assertEqual(
            121,
            summary["blocker_counts"]["METRIC_GOVERNANCE_REVIEW_MISSING"],
        )
        self.assertEqual(
            13,
            summary["blocker_counts"]["METRIC_GOVERNANCE_VALIDATION_PENDING"],
        )
        self.assertTrue(result["contract_path"].endswith("metric-governance.yaml"))
        self.assertEqual(64, len(result["contract_sha256"]))

    def _fixtures(self):
        payload = yaml.safe_load(
            (PLUGIN / "contracts" / "metric-governance.yaml").read_text(
                encoding="utf-8"
            )
        )
        semantics = governance._semantic_metrics()
        return payload, semantics

    def _load_mutated(self, payload, semantics, observed=date(2026, 9, 1)):
        with mock.patch.object(
            governance,
            "_strict_governance_payload",
            return_value=(payload, "synthetic-governance.yaml", "a" * 64),
        ), mock.patch.object(
            governance,
            "_semantic_metrics",
            return_value=semantics,
        ):
            return governance.load_contract(observed)

    def test_active_deprecated_retired_and_review_matrix(self):
        payload, semantics = self._fixtures()
        record = payload["metrics"]["delivery"]["delivery_amount"]
        record.update(
            {
                "owner_role": "datasage.data_governance.delivery",
                "reviewed_at": "2026-08-01T00:00:00+08:00",
                "review_interval_days": 365,
            }
        )
        current = self._load_mutated(payload, semantics)
        status = current["metrics"]["delivery"]["delivery_amount"][
            "derived_status"
        ]
        self.assertEqual("current", status["review"])
        self.assertEqual("allowed", status["execution"])
        self.assertEqual("pass", status["release"])

        deprecated = copy.deepcopy(payload)
        deprecated["metrics"]["delivery"]["delivery_amount"][
            "lifecycle"
        ] = "deprecated"
        status = self._load_mutated(deprecated, semantics)["metrics"]["delivery"][
            "delivery_amount"
        ]["derived_status"]
        self.assertEqual("allowed_with_warning", status["execution"])
        self.assertEqual(["METRIC_DEPRECATED"], status["warnings"])

        retired = copy.deepcopy(payload)
        retired["metrics"]["delivery"]["delivery_amount"][
            "lifecycle"
        ] = "retired"
        status = self._load_mutated(retired, semantics)["metrics"]["delivery"][
            "delivery_amount"
        ]["derived_status"]
        self.assertEqual("denied", status["execution"])
        self.assertIn("METRIC_RETIRED", status["warnings"])

        overdue = copy.deepcopy(payload)
        overdue["metrics"]["delivery"]["delivery_amount"].update(
            {
                "reviewed_at": "2020-01-01T00:00:00+08:00",
                "review_interval_days": 30,
            }
        )
        status = self._load_mutated(overdue, semantics)["metrics"]["delivery"][
            "delivery_amount"
        ]["derived_status"]
        self.assertEqual("overdue", status["review"])
        self.assertEqual("allowed", status["execution"])
        self.assertEqual("block", status["release"])

    def test_coverage_unknown_keys_and_invalid_values_fail_closed(self):
        payload, semantics = self._fixtures()
        missing = copy.deepcopy(payload)
        missing["metrics"]["delivery"].pop("delivery_amount")
        with self.assertRaises(governance.MetricGovernanceError) as context:
            self._load_mutated(missing, semantics)
        self.assertEqual("METRIC_GOVERNANCE_COVERAGE_MISMATCH", context.exception.code)

        extra = copy.deepcopy(payload)
        extra["metrics"]["delivery"]["invented_metric"] = copy.deepcopy(
            extra["metrics"]["delivery"]["delivery_amount"]
        )
        with self.assertRaises(governance.MetricGovernanceError):
            self._load_mutated(extra, semantics)

        unknown = copy.deepcopy(payload)
        unknown["metrics"]["delivery"]["delivery_amount"]["derived_status"] = {}
        with self.assertRaises(governance.MetricGovernanceError):
            self._load_mutated(unknown, semantics)

        invalid = copy.deepcopy(payload)
        invalid["metrics"]["delivery"]["delivery_amount"]["lifecycle"] = "gone"
        with self.assertRaises(governance.MetricGovernanceError) as context:
            self._load_mutated(invalid, semantics)
        self.assertEqual("METRIC_GOVERNANCE_LIFECYCLE_INVALID", context.exception.code)

    def test_duplicate_yaml_key_and_naive_observed_time_are_rejected(self):
        with self.assertRaises(governance.MetricGovernanceError):
            yaml.load(
                "schema: one\nschema: two\n",
                Loader=governance._UniqueSafeLoader,
            )
        with self.assertRaises(governance.MetricGovernanceError) as context:
            governance.load_contract(datetime(2026, 9, 1))
        self.assertEqual(
            "METRIC_GOVERNANCE_OBSERVED_AT_INVALID",
            context.exception.code,
        )

    def test_review_timestamp_owner_and_mapping_key_are_strict(self):
        with self.assertRaises(governance.MetricGovernanceError):
            yaml.load(
                "? [a, b]\n: c\n",
                Loader=governance._UniqueSafeLoader,
            )
        payload, semantics = self._fixtures()
        for value in (
            "2026-08-01 00:00:00+08:00",
            "2026-08-01T00:00:00+08",
            "2026-08-01T00:00:00+0800",
        ):
            with self.subTest(reviewed_at=value):
                mutated = copy.deepcopy(payload)
                mutated["metrics"]["delivery"]["delivery_amount"][
                    "reviewed_at"
                ] = value
                with self.assertRaises(governance.MetricGovernanceError) as context:
                    self._load_mutated(mutated, semantics)
                self.assertEqual(
                    "METRIC_GOVERNANCE_REVIEW_DATE_INVALID",
                    context.exception.code,
                )
        unsafe = copy.deepcopy(payload)
        unsafe["metrics"]["delivery"]["delivery_amount"][
            "owner_role"
        ] = "vk_dw.secret_column"
        with self.assertRaises(governance.MetricGovernanceError) as context:
            self._load_mutated(unsafe, semantics)
        self.assertEqual("METRIC_GOVERNANCE_OWNER_INVALID", context.exception.code)

    def test_results_are_detached_and_snapshot_pins_governance(self):
        first = governance.load_contract(date(2026, 9, 1))
        first["metrics"]["delivery"]["delivery_amount"]["owner_role"] = "mutated"
        second = governance.load_contract(date(2026, 9, 1))
        self.assertIsNone(
            second["metrics"]["delivery"]["delivery_amount"]["owner_role"]
        )
        self.assertIn(governance.GOVERNANCE_PATH, contract_store.PINNED_CONTRACT_PATHS)


if __name__ == "__main__":
    unittest.main()
