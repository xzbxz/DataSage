"""L3 delivery catalog, compact-wire, and model-schema regressions."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import sys
import types
import unittest

import jsonschema
import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
CONTRACT_ROOT = PLUGIN_ROOT / "contracts"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

PACKAGE = "datasage_delivery_l3_catalog_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

capability_contract = importlib.import_module(f"{PACKAGE}.capability_contract")
contracts = importlib.import_module(f"{PACKAGE}.contracts")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
tools = importlib.import_module(f"{PACKAGE}.tools")
wire = importlib.import_module(f"{PACKAGE}.wire")


PENDING_METRICS = {
    "warehouse_gross_delivery_quantity",
    "warehouse_return_quantity",
    "warehouse_delivery_quantity",
    "gross_delivery_quantity",
    "return_quantity",
    "delivery_quantity",
    "return_quantity_rate",
    "order_quantity",
}


def _catalog(request: dict[str, object], *, compact: bool = False) -> dict[str, object]:
    payload = contracts.datasage_catalog({"requests": [request]})
    if compact:
        payload = wire.enforce_tool_result_budget("datasage_catalog", payload)
    return json.loads(payload)


def _schema_accepts(schema: dict[str, object], value: object) -> bool:
    try:
        jsonschema.Draft7Validator(schema).validate(value)
    except jsonschema.ValidationError:
        return False
    return True


class DeliveryL3CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.semantics = yaml.safe_load(
            (CONTRACT_ROOT / "delivery-semantics.yaml").read_text(encoding="utf-8")
        )
        cls.metrics = cls.semantics["metrics"]
        cls.available = {
            code
            for code, definition in cls.metrics.items()
            if not contracts._is_unavailable(definition)
        }

    def test_pending_capabilities_are_non_executable_and_wire_stable(self) -> None:
        for request in (
            {"domain": "delivery"},
            {"domain": "delivery", "view": "expert_index"},
            {"domain": "delivery", "view": "full"},
            {"domain": "delivery", "view": "audit"},
        ):
            with self.subTest(request=request):
                raw = _catalog(request)
                compact = _catalog(request, compact=True)
                for payload in (raw, compact):
                    result = payload["results"][0]
                    pending = {
                        item["code"]: item
                        for item in result["pending_capabilities"]
                    }
                    executable = {item["code"] for item in result["metrics"]}
                    self.assertEqual(PENDING_METRICS, set(pending))
                    self.assertTrue(PENDING_METRICS.isdisjoint(executable))
                    for item in pending.values():
                        self.assertEqual("pending_validation", item["status"])
                        self.assertEqual("blocked", item["activation_gate"])
                        self.assertFalse(item["selectable"])
                        self.assertEqual(
                            "SEMANTIC_UNIT_RECONCILIATION_REQUIRED",
                            item["error"]["code"],
                        )
                        self.assertTrue(item["label"])
                        self.assertTrue(item["reason"])

        detail = _catalog({"domain": "delivery", "metric": "delivery_quantity"})
        compact_detail = _catalog(
            {"domain": "delivery", "metric": "delivery_quantity"},
            compact=True,
        )
        for payload in (detail, compact_detail):
            result = payload["results"][0]
            self.assertEqual("success", payload["status"])
            self.assertEqual("metric", result["level"])
            self.assertEqual([], result["dimensions"])
            self.assertEqual(result["metric"], result["pending_capability"])
            self.assertFalse(result["metric"]["selectable"])
            self.assertEqual(
                "SEMANTIC_UNIT_RECONCILIATION_REQUIRED",
                result["metric"]["error"]["code"],
            )

    def test_all_available_metrics_and_capabilities_remain_complete(self) -> None:
        summary = _catalog({"domain": "delivery"})["results"][0]
        expert = _catalog(
            {"domain": "delivery", "view": "expert_index"}
        )["results"][0]
        self.assertEqual(27, summary["metric_count"])
        self.assertEqual(27, expert["metric_count"])
        self.assertEqual(
            self.available,
            {item["code"] for item in summary["metrics"]},
        )
        self.assertEqual(
            self.available,
            {item["code"] for item in expert["metrics"]},
        )
        for code in sorted(self.available):
            with self.subTest(metric=code):
                detail = _catalog({"domain": "delivery", "metric": code})
                compact = _catalog(
                    {"domain": "delivery", "metric": code}, compact=True
                )
                self.assertEqual("success", detail["status"])
                self.assertEqual("success", compact["status"])
                raw_metric = detail["results"][0]["metric"]
                compact_metric = compact["results"][0]["metric"]
                self.assertEqual(
                    raw_metric["allowed_dimensions"],
                    compact_metric["allowed_dimensions"],
                )
                self.assertEqual(
                    raw_metric["operation_summary"],
                    compact_metric["operation_summary"],
                )
                self.assertEqual(
                    ["previous_period", "year_over_year"],
                    raw_metric["comparison_kinds"],
                )

    def test_scope_policy_is_derived_from_runtime_for_every_delivery_metric(self) -> None:
        for code, definition in self.metrics.items():
            with self.subTest(metric=code):
                detail = _catalog({"domain": "delivery", "metric": code})
                metric = (
                    detail["results"][0]["metric"]
                    if code in self.available
                    else next(
                        item
                        for item in _catalog({"domain": "delivery"})["results"][0][
                            "pending_capabilities"
                        ]
                        if item["code"] == code
                    )
                )
                policy = metric["delivery_scope_policy"]
                self.assertTrue(
                    set(policy["allowed_scopes"]).issubset(
                        set(capability_contract.DELIVERY_SCOPES)
                    )
                )
                for scope in (None, *capability_contract.DELIVERY_SCOPES):
                    request = {
                        "domain": "delivery",
                        "mode": "metric",
                        "metric": code,
                    }
                    if scope is not None:
                        request["delivery_scope"] = scope
                    try:
                        tools._validate_delivery_metric_scope(request)
                    except tools.QueryFailure:
                        runtime_accepts = False
                    else:
                        runtime_accepts = True
                    catalog_accepts = (
                        scope is None
                        and not policy["required"]
                    ) or (
                        scope is not None and scope in policy["allowed_scopes"]
                    )
                    self.assertEqual(runtime_accepts, catalog_accepts)

                self.assertIn("external_customers_only", metric["scope_flags"])
                self.assertTrue(metric["scope_flags"]["external_customers_only"])
                self.assertIn("answer_boundary_summary", metric)
                self.assertTrue(metric["answer_boundary_summary"])

        self.assertTrue(
            _catalog({"domain": "delivery", "metric": "return_amount"})[
                "results"
            ][0]["metric"]["scope_flags"]["completed_returns_only"]
        )
        self.assertFalse(
            _catalog({"domain": "delivery", "metric": "return_amount_rate"})[
                "results"
            ][0]["metric"]["scope_flags"]["completed_returns_only"]
        )

    def test_answer_boundaries_survive_all_catalog_projections_without_physical_names(
        self,
    ) -> None:
        physical_identifiers = (
            contracts._physical_identifiers(self.semantics)
            - contracts._business_tokens(self.semantics)
            - {"status"}
        )
        table_reference = re.compile(
            r"(?i)\bvk_(?:dw|dwd|ods)\.[A-Za-z_][A-Za-z0-9_]*\b"
        )
        for request in (
            {"domain": "delivery"},
            {"domain": "delivery", "view": "expert_index"},
            {"domain": "delivery", "view": "full"},
            {"domain": "delivery", "view": "audit"},
            {"domain": "delivery", "metric": "delivery_amount"},
        ):
            with self.subTest(request=request):
                for compact in (False, True):
                    payload = _catalog(request, compact=compact)
                    rendered = json.dumps(payload, ensure_ascii=False)
                    self.assertIsNone(table_reference.search(rendered))
                    for identifier in physical_identifiers:
                        self.assertIsNone(
                            re.search(
                                rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
                                rendered,
                                flags=re.IGNORECASE,
                            ),
                            identifier,
                        )
                result = _catalog(request, compact=True)["results"][0]
                if request.get("metric"):
                    self.assertTrue(result["metric"]["answer_boundary_summary"])
                else:
                    for item in result["metrics"]:
                        self.assertTrue(item["answer_boundary_summary"])

    def test_deepseek_static_guards_match_canonical_catalog_and_delivery_scope(self) -> None:
        catalog_canonical = schemas.DATASAGE_CATALOG["parameters"]
        catalog_model = schemas.model_tool_schema(schemas.DATASAGE_CATALOG)[
            "parameters"
        ]
        query_canonical = schemas.DATASAGE_QUERY["parameters"]
        query_model = schemas.model_tool_schema(schemas.DATASAGE_QUERY)[
            "parameters"
        ]
        for schema in (catalog_model, query_model):
            jsonschema.Draft7Validator.check_schema(schema)
            jsonschema.Draft202012Validator.check_schema(schema)

        catalog_cases = {
            "ordinary": {"requests": [{"domain": "delivery"}]},
            "scorecard": {"requests": [{"view": "performance_scorecard"}]},
            "mixed_scorecard": {
                "requests": [
                    {"view": "performance_scorecard"},
                    {"domain": "delivery", "view": "expert_index"},
                ]
            },
            "metric_and_view": {
                "requests": [
                    {
                        "domain": "delivery",
                        "metric": "delivery_amount",
                        "view": "full",
                    }
                ]
            },
            "seven_requests": {"requests": [{"domain": "delivery"}] * 7},
        }
        for label, value in catalog_cases.items():
            with self.subTest(catalog_case=label):
                canonical_accepts = _schema_accepts(catalog_canonical, value)
                model_accepts = _schema_accepts(catalog_model, value)
                if label == "seven_requests":
                    # DeepSeek compatibility intentionally omits array-size
                    # keywords; runtime still enforces the public 1-6 bound.
                    self.assertFalse(canonical_accepts)
                    self.assertTrue(model_accepts)
                else:
                    self.assertEqual(canonical_accepts, model_accepts)
        self.assertTrue(_schema_accepts(catalog_model, catalog_cases["ordinary"]))
        self.assertTrue(_schema_accepts(catalog_model, catalog_cases["scorecard"]))
        self.assertFalse(_schema_accepts(catalog_model, catalog_cases["mixed_scorecard"]))
        self.assertFalse(_schema_accepts(catalog_model, catalog_cases["metric_and_view"]))
        self.assertTrue(_schema_accepts(catalog_model, catalog_cases["seven_requests"]))
        runtime_seven = json.loads(
            contracts.datasage_catalog(catalog_cases["seven_requests"])
        )
        self.assertEqual("failed", runtime_seven["status"])
        self.assertEqual("INVALID_INPUT", runtime_seven["error"]["code"])

        query_cases = {
            "delivery_scope_non_delivery": {
                "requests": [
                    {
                        "request_id": "scope_bad",
                        "domain": "receipt",
                        "metric": "net_receipt_amount",
                        "delivery_scope": "default_net",
                    }
                ]
            },
            "delivery_scope_delivery": {
                "requests": [
                    {
                        "request_id": "scope_ok",
                        "domain": "delivery",
                        "metric": "delivery_amount",
                        "delivery_scope": "default_net",
                    }
                ]
            },
        }
        for label, value in query_cases.items():
            with self.subTest(query_case=label):
                self.assertEqual(
                    _schema_accepts(query_canonical, value),
                    _schema_accepts(query_model, value),
                )
        self.assertFalse(
            _schema_accepts(query_model, query_cases["delivery_scope_non_delivery"])
        )
        self.assertTrue(
            _schema_accepts(query_model, query_cases["delivery_scope_delivery"])
        )


if __name__ == "__main__":
    unittest.main()
