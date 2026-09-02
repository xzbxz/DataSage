"""Inventory gates for semantic single-sourcing and pending capability lifecycle."""

from __future__ import annotations

import copy
import importlib
import os
from pathlib import Path
import re
import sys
import types
import unittest

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_ROOT = PROFILE_ROOT / "plugins" / "datasage-query" / "contracts"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

TEST_PACKAGE = "datasage_semantic_inventory_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(TEST_PACKAGE, package)

contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
capability_contract = importlib.import_module(f"{TEST_PACKAGE}.capability_contract")


class SemanticSingleSourceTests(unittest.TestCase):
    def test_answer_notes_alias_identical_disclosure_text_without_value_drift(self):
        pair_count = 0
        anchor_names: set[str] = set()
        alias_names: set[str] = set()
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            anchor_names.update(re.findall(r"&(?P<name>answer_note_[A-Za-z0-9_]+)", raw))
            alias_names.update(re.findall(r"\*(?P<name>answer_note_[A-Za-z0-9_]+)", raw))
            for metric in parsed.get("metrics", {}).values():
                answer_note = metric.get("answer_note")
                if answer_note is None:
                    continue
                for disclosure in metric.get("disclosures", []):
                    if not isinstance(disclosure, dict):
                        continue
                    if disclosure.get("text") == answer_note:
                        pair_count += 1
                        self.assertIs(disclosure["text"], answer_note)

        self.assertEqual(65, pair_count)
        self.assertEqual(65, len(anchor_names))
        self.assertEqual(anchor_names, alias_names)

    def test_machine_contracts_exclude_unconsumed_root_defaults(self):
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            with self.subTest(path=path.name):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertNotIn("defaults", parsed)
                self.assertNotIn("analysis_defaults", parsed)
        datasets = yaml.safe_load(
            (CONTRACT_ROOT / "datasets.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual({"blocked_columns"}, set(datasets.get("defaults", {})))

    def test_value_contract_templates_are_consumed_by_yaml_aliases(self):
        consumed_templates = 0
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            templates = parsed.get("value_contract_templates", {})
            dimensions = parsed.get("dimensions", {})
            for template in templates.values():
                references = [
                    definition
                    for definition in dimensions.values()
                    if isinstance(definition, dict)
                    and definition.get("value_contract") is template
                ]
                self.assertTrue(references, f"unused value contract template in {path.name}")
                consumed_templates += 1
        self.assertGreater(consumed_templates, 0)

    def test_allocated_net_metrics_inherit_canonical_path_dimensions_without_copy(self):
        target = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        metrics = target["metrics"]
        expected_dimensions = ["salesperson", "department", "organization"]
        expected_sources = {
            "allocated_net_delivery_amount": "delivery_target_completion",
            "allocated_net_receipt_amount": "receipt_target_completion",
        }

        # Exercise the same inheritance helper used by the catalog projection,
        # with the canonical path made available in-memory. The real path is
        # pending and therefore intentionally fail-closed during this test.
        available_metrics = copy.deepcopy(metrics)
        sorted_dimensions = sorted(expected_dimensions)
        for metric_code, source_code in expected_sources.items():
            metric = metrics[metric_code]
            self.assertNotIn(
                "allowed_dimensions",
                metric,
                f"{metric_code} must not duplicate canonical path dimensions",
            )
            self.assertEqual(source_code, metric["source_completion_metric"])
            self.assertEqual("salesperson_allocation", metric["source_path"])
            source_path = metrics[source_code]["paths"]["salesperson_allocation"]
            self.assertEqual(expected_dimensions, source_path["allowed_dimensions"])

            available_path = available_metrics[source_code]["paths"][
                "salesperson_allocation"
            ]
            available_path.pop("availability", None)
            inherited, by_attribution = contracts._metric_dimension_contract(
                available_metrics[metric_code], available_metrics
            )
            self.assertEqual(sorted_dimensions, inherited)
            self.assertEqual(
                sorted_dimensions,
                by_attribution["salesperson_allocation"],
            )

    def test_allocated_net_metrics_fail_closed_when_canonical_path_is_pending(self):
        target = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        metrics = target["metrics"]
        for metric_code in (
            "allocated_net_delivery_amount",
            "allocated_net_receipt_amount",
        ):
            with self.subTest(metric=metric_code):
                inherited, by_attribution = contracts._metric_dimension_contract(
                    metrics[metric_code], metrics
                )
                self.assertEqual([], inherited)
                self.assertEqual({}, by_attribution)


class PendingCapabilityLifecycleTests(unittest.TestCase):
    EXPECTED_PENDING_METRICS = {
        "delivery": {
            "warehouse_gross_delivery_quantity",
            "warehouse_return_quantity",
            "warehouse_delivery_quantity",
            "gross_delivery_quantity",
            "return_quantity",
            "delivery_quantity",
            "return_quantity_rate",
            "order_quantity",
        },
        "receivable": {"receivable_quantity"},
        "target": {
            "delivery_allocated_target_amount",
            "receipt_allocated_target_amount",
            "allocated_net_delivery_amount",
            "allocated_net_receipt_amount",
        },
    }

    def test_exact_pending_metric_set_remains_blocked_and_denied(self):
        actual: dict[str, set[str]] = {}
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            domain = parsed.get("domain")
            for metric_code, metric in parsed.get("metrics", {}).items():
                availability = metric.get("availability")
                if (
                    isinstance(availability, dict)
                    and availability.get("status") == "pending_validation"
                ):
                    actual.setdefault(str(domain), set()).add(str(metric_code))

        self.assertEqual(self.EXPECTED_PENDING_METRICS, actual)
        for domain, metric_codes in self.EXPECTED_PENDING_METRICS.items():
            parsed = yaml.safe_load(
                (CONTRACT_ROOT / f"{domain}-semantics.yaml").read_text(
                    encoding="utf-8"
                )
            )
            expected_evidence = (
                {
                    "executable_unit_filter_or_group_contract",
                    "production_read_only_reconciliation",
                    "mixed_unit_regression",
                }
                if domain in {"delivery", "receivable"}
                else {
                    "production_read_only_reconciliation",
                    "attribution_completeness_validation",
                    "trusted_live_replay",
                    "semantic_inventory_regression",
                }
            )
            for metric_code in metric_codes:
                with self.subTest(domain=domain, metric=metric_code):
                    metric = parsed["metrics"][metric_code]
                    availability = metric["availability"]
                    self.assertEqual("pending_validation", availability["status"])
                    self.assertEqual(
                        "blocked", availability["activation_gate"]["state"]
                    )
                    self.assertEqual(
                        expected_evidence,
                        set(availability["activation_gate"]["required_evidence"]),
                    )
                    with self.assertRaises(
                        capability_contract.AvailabilityContractError
                    ) as caught:
                        capability_contract.ensure_available(metric)
                    self.assertEqual(availability["error_code"], caught.exception.code)

    def test_every_pending_metric_has_an_owner_and_blocked_activation_gate(self):
        pending: list[tuple[str, str, dict]] = []
        for path in sorted(CONTRACT_ROOT.glob("*-semantics.yaml")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            for metric_code, metric in parsed.get("metrics", {}).items():
                availability = metric.get("availability")
                if (
                    isinstance(availability, dict)
                    and availability.get("status") == "pending_validation"
                ):
                    pending.append((path.name, metric_code, availability))

        self.assertEqual(13, len(pending))
        for path_name, metric_code, availability in pending:
            with self.subTest(path=path_name, metric=metric_code):
                self.assertTrue(str(availability.get("owner", "")).strip())
                gate = availability.get("activation_gate")
                self.assertIsInstance(gate, dict)
                self.assertEqual("blocked", gate.get("state"))
                self.assertTrue(gate.get("required_evidence"))
                self.assertEqual(
                    "owner_and_release_review",
                    gate.get("activation_authority"),
                )
                review = availability.get("review")
                self.assertIsInstance(review, dict)
                self.assertTrue(review.get("cadence"))
                self.assertTrue(review.get("triggers"))
                lifecycle = availability.get("lifecycle")
                self.assertIsInstance(lifecycle, dict)
                self.assertEqual("pending_validation", lifecycle.get("state"))
                self.assertEqual(
                    "remains_unavailable_until_gate_passes",
                    lifecycle.get("activation_effect"),
                )


if __name__ == "__main__":
    unittest.main()
