from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
TEST_PACKAGE = "datasage_query_dependency_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contract_store = importlib.import_module(f"{TEST_PACKAGE}.contract_store")
contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
db_runtime = importlib.import_module(f"{TEST_PACKAGE}.db_runtime")
entities = importlib.import_module(f"{TEST_PACKAGE}.entities")
runtime_health = importlib.import_module(f"{TEST_PACKAGE}.runtime_health")
sql_identifiers = importlib.import_module(f"{TEST_PACKAGE}.sql_identifiers")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


class ModuleDependencyTests(unittest.TestCase):
    def test_shared_yaml_cache_is_the_only_yaml_parser_owner(self) -> None:
        contract_store.parse_yaml_cached.cache_clear()
        path = "plugins/datasage-query/contracts/query-policy.yaml"
        via_tools = tools._read_yaml(path)
        via_contracts = contracts._read_yaml(path)
        self.assertIs(via_tools, via_contracts)
        self.assertFalse(hasattr(tools, "_parse_yaml_cached"))
        self.assertFalse(hasattr(contracts, "_parse_yaml_cached"))
        self.assertFalse(hasattr(tools, "_profile_root"))
        self.assertFalse(hasattr(contracts, "_profile_root"))

    def test_identifier_compatibility_facade_preserves_results_and_codes(self) -> None:
        self.assertEqual(
            sql_identifiers.quote_table("vk_dw.sample"),
            tools._quote_table("vk_dw.sample"),
        )
        self.assertEqual("`alias`.`column_1`", tools._qualified_identifier("alias", "column_1"))
        for invalid in ("", "bad-name", "schema.table.extra"):
            with self.subTest(invalid=invalid), self.assertRaises(tools.QueryFailure) as caught:
                tools._quote_table(invalid)
            self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_database_connection_compatibility_facade_delegates(self) -> None:
        sentinel = object()
        with mock.patch.object(db_runtime, "connect", return_value=sentinel) as connect:
            self.assertIs(sentinel, tools._connect(timeout_seconds=3))
        connect.assert_called_once_with(
            connect_timeout_seconds=None,
            read_timeout_seconds=None,
            timeout_seconds=3,
        )

        with mock.patch.object(
            db_runtime,
            "connect",
            side_effect=db_runtime.DatabaseRuntimeError(
                "CONFIGURATION_MISSING", "missing"
            ),
        ), self.assertRaises(tools.QueryFailure) as caught:
            tools._connect()
        self.assertEqual("CONFIGURATION_MISSING", caught.exception.code)

    def test_entity_executor_bridge_keeps_existing_monkeypatch_seam(self) -> None:
        with mock.patch.object(tools, "_execute", return_value=([], False)) as execute:
            payload = json.loads(entities.datasage_entity_resolve({"token": "未登记地域"}))
        self.assertEqual("not_found", payload["status"])
        execute.assert_called_once()

    def test_runtime_health_uses_shared_database_adapter(self) -> None:
        class Connection:
            _datasage_security_evidence = {
                "live_connection_verified": True,
                "grants_verified": True,
                "transport_mode": "tls",
            }

            def close(self) -> None:
                return None

        runtime_health._LIVE_CACHE = None
        with mock.patch.object(db_runtime, "connect", return_value=Connection()) as connect:
            status = runtime_health.live_database_security_status()
        self.assertTrue(status["ready"], status)
        connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
