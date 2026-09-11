"""Focused target catalog/schema/wire regressions."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest

import jsonschema
import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_ROOT = PLUGIN_ROOT / "contracts"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

PACKAGE = "datasage_target_catalog_schema_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

contracts = importlib.import_module(f"{PACKAGE}.contracts")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
wire = importlib.import_module(f"{PACKAGE}.wire")


def _catalog(request: dict[str, object]) -> dict[str, object]:
    raw = contracts.datasage_catalog({"requests": [request]})
    return json.loads(wire.enforce_tool_result_budget("datasage_catalog", raw))


class TargetCatalogSchemaTests(unittest.TestCase):
    def test_default_target_catalog_preserves_operations_and_mode_dimensions(self):
        payload = _catalog({"domain": "target"})
        self.assertEqual("success", payload["status"])
        result = payload["results"][0]
        self.assertEqual(8, result["metric_count"])

        semantics = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        raw_metrics = semantics["metrics"]
        observed = {item["code"]: item for item in result["metrics"]}
        self.assertEqual(set(raw_metrics), set(observed))

        for code, metric in raw_metrics.items():
            with self.subTest(metric=code):
                expected_dimensions, by_mode = contracts._metric_dimension_contract(
                    metric, raw_metrics
                )
                projected = observed[code]
                self.assertEqual(expected_dimensions, projected["allowed_dimensions"])
                if by_mode:
                    self.assertEqual(
                        by_mode,
                        projected["dimensions_by_attribution_mode"],
                    )
                    self.assertEqual(
                        set(by_mode),
                        set(projected["operation_summary_by_attribution_mode"]),
                    )

        for code in ("delivery_target_completion", "receipt_target_completion"):
            metric = observed[code]
            operations = metric["operation_summary_by_attribution_mode"]
            self.assertIn("dimension_breakdown", operations["transaction_detail"])
            self.assertIn(
                "complete_target_gap_decomposition",
                operations["transaction_detail"],
            )
            self.assertNotIn(
                "complete_target_gap_decomposition",
                operations["salesperson_allocation"],
            )

    def test_model_schema_rejects_cross_ledger_target_gap_operation(self):
        model_parameters = schemas.model_tool_schema(schemas.DATASAGE_QUERY)[
            "parameters"
        ]
        valid = {
            "requests": [
                {
                    "request_id": "target_gap_ok",
                    "domain": "target",
                    "metric": "delivery_target_completion",
                    "attribution_mode": "transaction_detail",
                    "complete_target_gap_decomposition": {"dimension": "customer"},
                }
            ]
        }
        jsonschema.validate(valid, model_parameters)

        invalid = {
            "requests": [
                {
                    "request_id": "target_gap_bad",
                    "domain": "target",
                    "metric": "delivery_target_completion",
                    "attribution_mode": "salesperson_allocation",
                    "complete_target_gap_decomposition": {"dimension": "customer"},
                }
            ]
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, model_parameters)

        time_bucket_conflict = {
            "requests": [
                {
                    "request_id": "target_gap_time_bucket",
                    "domain": "target",
                    "metric": "delivery_target_completion",
                    "attribution_mode": "transaction_detail",
                    "time_bucket": "month",
                    "complete_target_gap_decomposition": {"dimension": "customer"},
                }
            ]
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(time_bucket_conflict, model_parameters)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(time_bucket_conflict["requests"][0], schemas.REQUEST)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                time_bucket_conflict,
                schemas.DATASAGE_QUERY["parameters"],
            )

        non_target = {
            "requests": [
                {
                    "request_id": "target_gap_wrong_domain",
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "complete_target_gap_decomposition": {"dimension": "customer"},
                }
            ]
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(non_target, model_parameters)

    def test_target_metric_detail_keeps_gap_mode_separate_from_customer_breakdown(self):
        detail = _catalog(
            {"domain": "target", "metric": "delivery_target_completion"}
        )["results"][0]["metric"]
        by_mode = detail["operation_summary_by_attribution_mode"]
        gap = detail["target_gap_decomposition"]
        self.assertEqual("transaction_detail", gap["required_attribution_mode"])
        self.assertIn("complete_target_gap_decomposition", by_mode["transaction_detail"])
        self.assertNotIn(
            "complete_target_gap_decomposition", by_mode["salesperson_allocation"]
        )
        self.assertEqual(
            set(detail["allowed_dimensions"]),
            set(
                dimension["code"]
                for dimension in _catalog(
                    {"domain": "target", "metric": "delivery_target_completion"}
                )["results"][0]["dimensions"]
            ),
        )

    def test_catalog_views_keep_flat_operations_common_across_attribution_modes(self):
        views = {
            "summary": _catalog({"domain": "target"})["results"][0],
            "expert_index": _catalog(
                {"domain": "target", "view": "expert_index"}
            )["results"][0],
            "detail": _catalog(
                {"domain": "target", "metric": "delivery_target_completion"}
            )["results"][0],
        }
        metrics = {
            "summary": {
                item["code"]: item for item in views["summary"]["metrics"]
            },
            "expert_index": {
                item["code"]: {**views["expert_index"].get("metric_defaults", {}), **item}
                for item in views["expert_index"]["metrics"]
            },
            "detail": views["detail"]["metric"],
        }

        completion = [
            metrics["summary"]["delivery_target_completion"]["operation_summary"],
            metrics["expert_index"]["delivery_target_completion"][
                "operation_summary"
            ],
            metrics["detail"]["operation_summary"],
        ]
        self.assertTrue(all(item == completion[0] for item in completion[1:]))
        self.assertNotIn("complete_target_gap_decomposition", completion[0])

        by_mode = metrics["summary"]["delivery_target_completion"][
            "operation_summary_by_attribution_mode"
        ]
        self.assertIn(
            "complete_target_gap_decomposition",
            by_mode["transaction_detail"],
        )
        self.assertNotIn(
            "complete_target_gap_decomposition",
            by_mode["salesperson_allocation"],
        )
        self.assertEqual(
            by_mode,
            metrics["expert_index"]["delivery_target_completion"][
                "operation_summary_by_attribution_mode"
            ],
        )
        self.assertEqual(
            by_mode,
            metrics["detail"]["operation_summary_by_attribution_mode"],
        )

        raw = yaml.safe_load(
            (CONTRACT_ROOT / "target-semantics.yaml").read_text(encoding="utf-8")
        )
        raw_metrics = raw["metrics"]
        for code, metric in raw_metrics.items():
            raw_modes = metric.get("allowed_attribution_modes")
            mode_count = len(raw_modes) if isinstance(raw_modes, list) else 1
            if mode_count != 1:
                continue
            with self.subTest(metric=code):
                item = metrics["summary"][code]
                self.assertEqual(
                    item["operation_summary"],
                    next(iter(item["operation_summary_by_attribution_mode"].values())),
                )

    def test_query_compact_wire_keeps_target_reconciliation_and_claim_facts(self):
        payload = {
            "status": "success",
            "results": [
                {
                    "request_id": "target_customer",
                    "status": "success",
                    "data_state": "rows",
                    "business_metric_ref": "delivery_target_completion",
                    "business_metric_label": "出库目标完成情况",
                    "business_dimension_labels": ["客户"],
                    "claim_ledger": [
                        {
                            "claim_id": "claim_1",
                            "dimensions": [{"label": "客户", "value": "A"}],
                            "facts": {
                                "target_amount": "100",
                                "actual_amount": "80",
                                "gap": "20",
                                "completion_rate": "0.8",
                            },
                            "states": {"target_state": "set"},
                            "allowed_relations": ["observation", "dimension_breakdown"],
                            "unit": "人民币元",
                            "currency": "CNY",
                        }
                    ],
                    "target_gap_reconciliation": {
                        "status": "not_reconciled",
                        "operation": "complete_target_gap_decomposition",
                        "causal_attribution_authorized": False,
                    },
                    "row_count": 1,
                    "truncated": False,
                    "requested_limit": 100,
                    "effective_limit": 100,
                    "has_more": False,
                    "applied_time_range": {
                        "start": "2026-08-01",
                        "end": "2026-09-01",
                    },
                }
            ],
        }
        compact = wire.compact_query_payload(payload)
        result = compact["results"][0]
        self.assertEqual(
            payload["results"][0]["target_gap_reconciliation"],
            result["target_gap_reconciliation"],
        )
        self.assertEqual(payload["results"][0]["claim_ledger"][0]["facts"], result["rows"][0]["facts"])
        self.assertEqual(
            payload["results"][0]["claim_ledger"][0]["states"],
            result["rows"][0]["states"],
        )


if __name__ == "__main__":
    unittest.main()
