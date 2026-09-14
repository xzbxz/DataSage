"""Offline contract tests for the Profile -> Hermes tool-schema wire path.

These tests deliberately stop before any model client, config loader, gateway,
database, or network operation.  They exercise the same pure schema stages used
by Hermes: Profile projection, the official generic sanitizer, and the
Chat-Completions transport's pre-wire shape.
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys
import types
import unittest

import jsonschema


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"


try:
    from tools.schema_sanitizer import sanitize_tool_schemas
    from agent.transports.chat_completions import ChatCompletionsTransport
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - exercised without Hermes
    sanitize_tool_schemas = None
    ChatCompletionsTransport = None
    _OFFICIAL_IMPORT_ERROR = exc
else:
    _OFFICIAL_IMPORT_ERROR = None


if _OFFICIAL_IMPORT_ERROR is None:
    _official_root = Path(importlib.import_module("tools.schema_sanitizer").__file__).resolve().parents[1]
    if not (_official_root / "agent" / "transports" / "chat_completions.py").is_file():
        _OFFICIAL_IMPORT_ERROR = RuntimeError(
            "the imported Hermes schema sanitizer is not from an install containing the official transport"
        )


if _OFFICIAL_IMPORT_ERROR is None:
    # The Profile directory name contains a hyphen, so use the same isolated
    # package-namespace technique as the existing Profile tests.  Importing
    # schemas.py only loads pure contract definitions; it does not call the
    # plugin register() entrypoint or initialize HERMES_HOME.
    _PACKAGE = "_datasage_host_tool_schema_tests"
    _package = types.ModuleType(_PACKAGE)
    _package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules.setdefault(_PACKAGE, _package)
    schemas = importlib.import_module(f"{_PACKAGE}.schemas")
else:
    schemas = None


def _final_wire_tool(canonical_schema: dict) -> tuple[dict, dict, dict]:
    """Return (Profile projection, final tool, transport kwargs) in memory only."""
    assert sanitize_tool_schemas is not None
    assert ChatCompletionsTransport is not None
    projected = schemas.model_tool_schema(canonical_schema)
    registered_shape = {"type": "function", "function": projected}
    sanitized = sanitize_tool_schemas([registered_shape])
    transport = ChatCompletionsTransport()
    converted = transport.convert_tools(sanitized)
    kwargs = transport.build_kwargs(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "schema probe"}],
        tools=converted,
    )
    return projected, converted[0], kwargs


def _parameters(final_tool: dict) -> dict:
    return final_tool["function"]["parameters"]


def _accepts(schema: dict, value: object) -> bool:
    try:
        jsonschema.Draft7Validator(schema).validate(value)
    except jsonschema.ValidationError:
        return False
    return True


@unittest.skipIf(_OFFICIAL_IMPORT_ERROR is not None, f"official Hermes schema path unavailable: {_OFFICIAL_IMPORT_ERROR}")
class HostToolSchemaTests(unittest.TestCase):
    def test_final_wire_shape_uses_official_sanitizer_and_chat_transport(self) -> None:
        """Every Profile tool reaches Chat Completions as one OpenAI function tool."""
        expected_names = (
            "datasage_catalog",
            "datasage_entity_resolve",
            "datasage_query",
        )
        for name in expected_names:
            with self.subTest(tool=name):
                canonical = copy.deepcopy(getattr(schemas, name.upper()))
                before = copy.deepcopy(canonical)
                projected, final_tool, kwargs = _final_wire_tool(canonical)

                self.assertEqual(before, canonical)
                self.assertEqual(name, final_tool["function"]["name"])
                self.assertEqual(final_tool, kwargs["tools"][0])
                self.assertEqual("function", final_tool["type"])
                self.assertEqual("object", _parameters(final_tool)["type"])
                self.assertIsInstance(_parameters(final_tool).get("properties"), dict)
                self.assertIsInstance(projected, dict)

    def test_catalog_mutual_exclusion_survives_final_wire(self) -> None:
        """The final host shape keeps ordinary/scorecard and metric/view prohibitions."""
        _, final_tool, _ = _final_wire_tool(schemas.DATASAGE_CATALOG)
        parameters = _parameters(final_tool)
        cases = {
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
                    {"domain": "delivery", "metric": "delivery_amount", "view": "full"}
                ]
            },
        }
        expected = {
            "ordinary": True,
            "scorecard": True,
            "mixed_scorecard": False,
            "metric_and_view": False,
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assertEqual(expected[label], _accepts(parameters, value))

    def test_query_cross_field_guards_survive_final_wire(self) -> None:
        """The final host shape retains delivery-scope and target-gap guard behavior."""
        _, final_tool, _ = _final_wire_tool(schemas.DATASAGE_QUERY)
        parameters = _parameters(final_tool)
        cases = {
            "delivery_scope_on_delivery": {
                "requests": [
                    {
                        "request_id": "scope_ok",
                        "domain": "delivery",
                        "metric": "delivery_amount",
                        "delivery_scope": "default_net",
                    }
                ]
            },
            "delivery_scope_on_receipt": {
                "requests": [
                    {
                        "request_id": "scope_bad",
                        "domain": "receipt",
                        "metric": "net_receipt_amount",
                        "delivery_scope": "default_net",
                    }
                ]
            },
            "target_gap_transaction_detail": {
                "requests": [
                    {
                        "request_id": "target_gap_ok",
                        "domain": "target",
                        "metric": "delivery_target_completion",
                        "attribution_mode": "transaction_detail",
                        "complete_target_gap_decomposition": {"dimension": "customer"},
                    }
                ]
            },
            "target_gap_salesperson_allocation": {
                "requests": [
                    {
                        "request_id": "target_gap_bad",
                        "domain": "target",
                        "metric": "delivery_target_completion",
                        "attribution_mode": "salesperson_allocation",
                        "complete_target_gap_decomposition": {"dimension": "customer"},
                    }
                ]
            },
        }
        expected = {
            "delivery_scope_on_delivery": True,
            "delivery_scope_on_receipt": False,
            "target_gap_transaction_detail": True,
            "target_gap_salesperson_allocation": False,
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assertEqual(expected[label], _accepts(parameters, value))


if __name__ == "__main__":
    unittest.main()
