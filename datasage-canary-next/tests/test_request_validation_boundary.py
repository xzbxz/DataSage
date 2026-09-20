"""Request validation remains usable without loading execution or operations."""
import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_datasage_request_boundary"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "plugins" / "datasage-query")]
sys.modules[PACKAGE] = package


class RequestValidationBoundaryTests(unittest.TestCase):
    def test_validation_does_not_load_execution_and_retains_field_errors(self):
        validation = importlib.import_module(f"{PACKAGE}.query_validation")
        errors = importlib.import_module(f"{PACKAGE}.query_errors")
        request = {"request_id": "q", "domain": "delivery", "metric": "delivery_amount"}
        self.assertEqual({**request, "mode": "metric"}, validation._validate_request(dict(request)))
        with self.assertRaises(errors.QueryFailure) as caught:
            validation._validate_request({**request, "limit": True}, request_path="requests[2]")
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        self.assertEqual("requests[2].limit", caught.exception.path)
        for name in ("tools", "db_executor", "operations", "local_report", "acceptance_delivery"):
            self.assertNotIn(f"{PACKAGE}.{name}", sys.modules)

    def test_public_query_boundary_catches_the_same_error_type(self):
        # A separate namespace keeps the import-boundary assertion independent
        # of unittest's execution order and other tests importing the public API.
        alias = PACKAGE + "_public"
        other = types.ModuleType(alias)
        other.__path__ = package.__path__
        sys.modules[alias] = other
        tools = importlib.import_module(f"{alias}.tools")
        errors = importlib.import_module(f"{alias}.query_errors")
        self.assertIs(tools.QueryFailure, errors.QueryFailure)
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request({"request_id": "q", "domain": "unknown", "metric": "x"})
        self.assertEqual("INVALID_INPUT", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
