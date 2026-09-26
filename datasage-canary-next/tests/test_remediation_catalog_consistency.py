"""M04/M05 catalog, availability, and executor-contract consistency checks."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

PACKAGE = "datasage_catalog_consistency_tests"
_spec = importlib.util.spec_from_file_location(
    PACKAGE,
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("unable to load isolated DataSage test package")
_package = importlib.util.module_from_spec(_spec)
sys.modules[PACKAGE] = _package
_spec.loader.exec_module(_package)

capability_contract = importlib.import_module(f"{PACKAGE}.capability_contract")
contracts = importlib.import_module(f"{PACKAGE}.contracts")
tools = importlib.import_module(f"{PACKAGE}.tools")
analytical_queries = importlib.import_module(f"{PACKAGE}.analytical_queries")


class CatalogConsistencyTests(unittest.TestCase):
    def _catalog(self, request: dict[str, object]) -> dict[str, object]:
        return json.loads(contracts.datasage_catalog({"requests": [request]}))

    def _receivable_semantics(self) -> dict[str, object]:
        return copy.deepcopy(contracts.execution_contracts("receivable")[1])

    def _target_semantics(self) -> dict[str, object]:
        return copy.deepcopy(contracts.execution_contracts("target")[1])

    def test_known_pending_receivable_is_discoverable_but_not_selectable(self) -> None:
        index = self._catalog({"domain": "receivable", "view": "expert_index"})
        self.assertEqual("success", index["status"])
        result = index["results"][0]
        self.assertEqual(72, result["metric_count"])  # 71 prior active + original formal DSO
        pending = [
            item
            for item in result.get("pending_capabilities", [])
            if item.get("code") == "receivable_quantity"
        ]
        self.assertEqual(1, len(pending))
        self.assertFalse(pending[0]["selectable"])
        self.assertEqual(
            "SEMANTIC_UNIT_RECONCILIATION_REQUIRED",
            pending[0]["error"]["code"],
        )
        self.assertNotIn("delivery_scope_policy", pending[0])
        self.assertNotIn("scope_flags", pending[0])

        detail = self._catalog(
            {"domain": "receivable", "metric": "receivable_quantity"}
        )
        self.assertEqual("success", detail["status"])
        detail_metric = detail["results"][0]["metric"]
        self.assertTrue(detail_metric["pending"])
        self.assertFalse(detail_metric["selectable"])
        self.assertEqual(
            "SEMANTIC_UNIT_RECONCILIATION_REQUIRED",
            detail_metric["error"]["code"],
        )
        self.assertEqual([], detail["results"][0]["dimensions"])

    def test_pending_preflight_fails_before_database_access(self) -> None:
        semantics = contracts.execution_contracts("receivable")[1]
        with mock.patch.object(
            tools, "_connect", side_effect=AssertionError("DB access is forbidden")
        ):
            with self.assertRaises(tools.QueryFailure) as caught:
                tools._validate_metric_contract(
                    {"domain": "receivable", "metric": "receivable_quantity"},
                    semantics,
                )
        self.assertEqual(
            "SEMANTIC_UNIT_RECONCILIATION_REQUIRED", caught.exception.code
        )

    def test_unknown_metric_remains_unknown(self) -> None:
        detail = self._catalog(
            {"domain": "receivable", "metric": "metric_that_does_not_exist"}
        )
        self.assertEqual("failed", detail["status"])
        self.assertEqual("METRIC_UNAVAILABLE", detail["error"]["code"])

    def test_invalid_availability_shape_is_not_pending(self) -> None:
        semantics = self._receivable_semantics()
        semantics["metrics"]["receivable_quantity"]["availability"] = {
            "status": "pending_validation"
        }
        with self.assertRaises(contracts.ContractFailure) as caught:
            contracts._model_semantic_projection("receivable", semantics)
        self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)
        with self.assertRaises(tools.QueryFailure) as preflight_error:
            tools._validate_metric_contract(
                {"domain": "receivable", "metric": "receivable_quantity"}, semantics,
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", preflight_error.exception.code)

    def test_invalid_path_availability_has_structured_errors_at_all_consumers(self) -> None:
        semantics = self._target_semantics()
        metric = semantics["metrics"]["delivery_target_completion"]
        metric["paths"]["transaction_detail"]["availability"] = {"status": "pending_validation"}
        request = {
            "domain": "target", "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail", "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
        }
        with self.assertRaises(contracts.ContractFailure) as catalog_error:
            contracts._model_semantic_projection("target", semantics)
        with self.assertRaises(tools.QueryFailure) as preflight_error:
            tools._validate_metric_contract(request, semantics)
        with self.assertRaises(analytical_queries.AnalysisQueryError) as builder_error:
            analytical_queries._target_completion_query(
                request, metric, contracts.execution_contracts("target")[0], 100,
            )
        for caught in (catalog_error, preflight_error, builder_error):
            self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)

    def test_valid_pending_path_does_not_block_available_path_shape(self) -> None:
        semantics = self._target_semantics()
        metric = semantics["metrics"]["delivery_target_completion"]
        metric["paths"]["salesperson_allocation"] = {
            "availability": {
                "status": "pending_validation", "error_code": "SYNTHETIC_PENDING",
                "message": "Synthetic path still requires independent verification.",
            }
        }
        capability_contract.validate_metric_execution_contract(metric)
        tools._validate_metric_contract(
            {"domain": "target", "metric": "delivery_target_completion",
             "attribution_mode": "transaction_detail"}, semantics,
        )

    def test_target_unit_is_shared_by_catalog_and_preflight(self) -> None:
        semantics = self._target_semantics()
        metric = semantics["metrics"]["delivery_target_completion"]
        capability_contract.validate_metric_execution_contract(metric)
        catalog = self._catalog(
            {"domain": "target", "metric": "delivery_target_completion"}
        )
        self.assertEqual("success", catalog["status"])
        self.assertEqual("比例", catalog["results"][0]["metric"]["unit"])
        tools._validate_metric_contract(
            {
                "domain": "target",
                "metric": "delivery_target_completion",
                "attribution_mode": "transaction_detail",
            },
            semantics,
        )
        metric["unit"] = "pct"
        with self.assertRaises(capability_contract.CapabilityContractError) as caught:
            capability_contract.validate_metric_execution_contract(metric)
        self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)

        with self.assertRaises(contracts.ContractFailure) as catalog_error:
            contracts._model_semantic_projection("target", semantics)
        self.assertEqual("CONTRACT_UNAVAILABLE", catalog_error.exception.code)

        with self.assertRaises(tools.QueryFailure) as preflight_error:
            tools._validate_metric_contract(
                {
                    "domain": "target",
                    "metric": "delivery_target_completion",
                    "attribution_mode": "transaction_detail",
                },
                semantics,
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", preflight_error.exception.code)

    def test_target_paths_and_components_share_shape_guard(self) -> None:
        semantics = self._target_semantics()
        metric = semantics["metrics"]["delivery_target_completion"]
        path = metric["paths"]["transaction_detail"]
        path["actual"]["components"] = [{"sign": 1}]

        with self.assertRaises(capability_contract.CapabilityContractError):
            capability_contract.validate_metric_execution_contract(metric)
        with self.assertRaises(contracts.ContractFailure) as catalog_error:
            contracts._model_semantic_projection("target", semantics)
        self.assertEqual("CONTRACT_UNAVAILABLE", catalog_error.exception.code)
        with self.assertRaises(tools.QueryFailure) as preflight_error:
            tools._validate_metric_contract(
                {
                    "domain": "target",
                    "metric": "delivery_target_completion",
                    "attribution_mode": "transaction_detail",
                },
                semantics,
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", preflight_error.exception.code)

        request = {
            "domain": "target",
            "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail",
            "dimensions": [],
            "metric_filters": {},
            "time_range": {"start": "2026-09-01", "end": "2026-10-01"},
        }
        datasets = contracts.execution_contracts("target")[0]
        with self.assertRaises(analytical_queries.AnalysisQueryError) as builder_error:
            analytical_queries._target_completion_query(
                request,
                metric,
                datasets,
                100,
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", builder_error.exception.code)

    def test_target_shape_guard_keeps_valid_leaf_and_aggregate_paths(self) -> None:
        semantics = self._target_semantics()
        metric = semantics["metrics"]["delivery_target_completion"]
        capability_contract.validate_metric_execution_contract(metric)

        path = metric["paths"]["transaction_detail"]
        component = path["actual"]["components"][0]
        path["actual"] = {
            key: component[key] for key in ("table", "measure", "time_field")
        }
        capability_contract.validate_metric_execution_contract(metric)

    def test_target_shape_guard_rejects_missing_required_shape_fields(self) -> None:
        mutations = {
            "empty_target": lambda path: path.__setitem__("target", {}),
            "empty_component": lambda path: path["actual"].__setitem__(
                "components", [{}]
            ),
            "component_without_measure": lambda path: path["actual"][
                "components"
            ][0].pop("measure"),
            "component_without_time_field": lambda path: path["actual"][
                "components"
            ][0].pop("time_field"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                semantics = self._target_semantics()
                path = semantics["metrics"]["delivery_target_completion"][
                    "paths"
                ]["transaction_detail"]
                mutate(path)
                with self.assertRaises(capability_contract.CapabilityContractError):
                    capability_contract.validate_metric_execution_contract(
                        semantics["metrics"]["delivery_target_completion"]
                    )

    def test_failed_projection_does_not_mutate_valid_contract_projection(self) -> None:
        valid = self._catalog({"domain": "target", "metric": "delivery_target_completion"})
        self.assertEqual("success", valid["status"])
        semantics = self._target_semantics()
        semantics["metrics"]["delivery_target_completion"]["unit"] = "C24_BAD_UNIT"
        with self.assertRaises(contracts.ContractFailure):
            contracts._model_semantic_projection("target", semantics)
        again = self._catalog(
            {"domain": "target", "metric": "delivery_target_completion"}
        )
        self.assertEqual("success", again["status"])
        self.assertEqual(
            valid["results"][0]["metric"]["unit"],
            again["results"][0]["metric"]["unit"],
        )


if __name__ == "__main__":
    unittest.main()
