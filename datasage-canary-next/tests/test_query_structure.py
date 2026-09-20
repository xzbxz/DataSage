"""E02 query/operator dependency and compatibility checks."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"


def _package() -> str:
    name = "datasage_query_structure_" + uuid.uuid4().hex
    package = types.ModuleType(name)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = package
    return name


def _dataset_contract() -> dict:
    return {
        "datasets": {
            "vk_ods.slow_moving_goods_ods": {
                "allowed_columns": [
                    "id",
                    "source_unit",
                    "goods_num",
                    "piece_num",
                    "promotion_price",
                    "whse_dept",
                    "is_whitelist",
                ],
                "forbidden_columns": [],
            }
        }
    }


class QueryStructureTests(unittest.TestCase):
    def test_idk_handler_and_catalog_do_not_load_operations(self) -> None:
        package = _package()
        handlers = importlib.import_module(f"{package}.analytical_handlers")

        handler = handlers.get_handler("idk_unpriced")
        self.assertIsNotNone(handler)
        self.assertEqual("idk_query", handler.module)
        builder = handler.resolve("builder")
        self.assertEqual(f"{package}.idk_query", builder.__module__)

        fields = handlers.public_fact_fields()
        self.assertIn("idk_null_price_rows", fields)
        self.assertNotIn(f"{package}.operations", sys.modules)

        contracts = importlib.import_module(f"{package}.contracts")
        payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "inventory", "metric": "idk_unpriced_pool"}]}
            )
        )
        self.assertEqual("success", payload["status"])
        self.assertNotIn(f"{package}.operations", sys.modules)

    def test_operations_facade_reuses_pure_builder_and_contract(self) -> None:
        package = _package()
        pure = importlib.import_module(f"{package}.idk_query")
        operations = importlib.import_module(f"{package}.operations")
        request = {
            "request_id": "idk-structure",
            "domain": "inventory",
            "mode": "metric",
            "metric": "idk_unpriced_pool",
            "dimensions": ["unit"],
            "metric_filters": {"unit": "m"},
        }
        datasets = _dataset_contract()

        expected = pure.build_idk_query(request, None, datasets, {}, 10)
        actual = operations.build_idk_query(request, None, datasets, {}, 10)
        self.assertEqual(expected, actual)
        self.assertIs(operations.FACT_FIELDS, pure.FACT_FIELDS)
        self.assertEqual(operations.policy(), pure.policy())
        self.assertEqual(operations._table("vk_ods.slow_moving_goods_ods"), pure._table("vk_ods.slow_moving_goods_ods"))

    def test_operations_builder_keeps_policy_patch_seam(self) -> None:
        package = _package()
        operations = importlib.import_module(f"{package}.operations")
        request = {
            "request_id": "idk-policy-seam",
            "domain": "inventory",
            "mode": "metric",
            "metric": "idk_unpriced_pool",
            "dimensions": ["unit"],
        }
        datasets = _dataset_contract()
        configured = {
            "idk": {
                "table": "vk_ods.slow_moving_goods_ods",
                "department": "PATCHED",
                "minimum_quantity_exclusive": 11,
                "whitelist_value": "n",
            }
        }
        with mock.patch.object(operations, "policy", return_value=configured), mock.patch.object(
            operations, "_table", return_value="`patched`.`table`"
        ):
            sql, params, _scope = operations.build_idk_query(
                request, None, datasets, {}, 10
            )
        self.assertIn("`patched`.`table`", sql)
        self.assertEqual(["PATCHED", 11, "n", 11], params)

    def test_operator_table_facade_keeps_operation_error(self) -> None:
        package = _package()
        operations = importlib.import_module(f"{package}.operations")

        with self.assertRaises(operations.OperationError) as caught:
            operations._table("VK_ODS.slow_moving_goods_ods")
        self.assertEqual("OPERATION_SOURCE_INVALID", str(caught.exception))
        self.assertIs(operations.OperationError, importlib.import_module(f"{package}.idk_query").OperationError)


if __name__ == "__main__":
    unittest.main()
