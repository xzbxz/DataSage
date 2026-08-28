"""Schema/runtime equivalence tests for the public DataSage request contract."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import jsonschema


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
PACKAGE = "datasage_request_contract_equivalence"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

capability_contract = importlib.import_module(f"{PACKAGE}.capability_contract")
request_contract = importlib.import_module(f"{PACKAGE}.request_contract")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
tools = importlib.import_module(f"{PACKAGE}.tools")
runtime_health = importlib.import_module(f"{PACKAGE}.runtime_health")
entitlements = importlib.import_module(f"{PACKAGE}.entitlements")
db_runtime = importlib.import_module(f"{PACKAGE}.db_runtime")
entities = importlib.import_module(f"{PACKAGE}.entities")


def _request(request_id: str = "q1", **overrides):
    value = {
        "request_id": request_id,
        "domain": "delivery",
        "metric": "contract_probe_metric",
    }
    value.update(overrides)
    return value


class RequestContractEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.request_validator = jsonschema.Draft7Validator(schemas.REQUEST)
        self.envelope_validator = jsonschema.Draft7Validator(
            schemas.DATASAGE_QUERY["parameters"]
        )

    def _runtime_request_valid(self, request) -> bool:
        try:
            tools._validate_request(copy.deepcopy(request))
        except tools.QueryFailure:
            return False
        return True

    def _contract_envelope_valid(self, envelope) -> bool:
        try:
            request_contract.validate_query_envelope(copy.deepcopy(envelope))
        except request_contract.RequestContractError:
            return False
        return True

    def assertRequestEquivalent(self, request, expected: bool):
        schema_valid = not list(self.request_validator.iter_errors(request))
        runtime_valid = self._runtime_request_valid(request)
        self.assertEqual(expected, schema_valid)
        self.assertEqual(schema_valid, runtime_valid)

    def assertEnvelopeEquivalent(self, envelope, expected: bool):
        schema_valid = not list(self.envelope_validator.iter_errors(envelope))
        contract_valid = self._contract_envelope_valid(envelope)
        self.assertEqual(expected, schema_valid)
        self.assertEqual(schema_valid, contract_valid)

    def test_string_boundaries_are_generated_from_one_contract(self):
        valid = (
            _request(request_id="r" * request_contract.REQUEST_ID.max_length),
            _request(metric="m" * request_contract.METRIC_CODE.max_length),
            _request(dimensions=["d" * request_contract.DIMENSION_CODE.max_length]),
            _request(decomposition_of_request_id="overall"),
        )
        invalid = (
            _request(request_id=" "),
            _request(request_id="r" * (request_contract.REQUEST_ID.max_length + 1)),
            _request(metric="\n"),
            _request(metric="m" * (request_contract.METRIC_CODE.max_length + 1)),
            _request(decomposition_of_request_id=" "),
            _request(dimensions=[" "]),
            _request(
                dimensions=[
                    "d" * (request_contract.DIMENSION_CODE.max_length + 1)
                ]
            ),
            _request(dimensions=["department", "department"]),
            _request(
                dimensions=[
                    f"dimension_{index}"
                    for index in range(request_contract.MAX_GROUP_DIMENSIONS + 1)
                ]
            ),
        )
        for request in valid:
            with self.subTest(valid=request):
                self.assertRequestEquivalent(request, True)
        for request in invalid:
            with self.subTest(invalid=request):
                self.assertRequestEquivalent(request, False)

    def test_public_schema_omits_legacy_mode_and_purpose_but_runtime_shim_accepts_metric(self):
        properties = schemas.REQUEST["properties"]
        self.assertNotIn("mode", properties)
        self.assertNotIn("purpose", properties)
        self.assertEqual(
            {"request_id", "domain", "metric"},
            set(schemas.REQUEST["required"]),
        )

        legacy = _request(mode="metric", purpose="legacy caller context")
        self.assertTrue(list(self.request_validator.iter_errors(legacy)))
        normalized = tools._validate_request(copy.deepcopy(legacy))
        self.assertEqual("metric", normalized["mode"])
        self.assertNotIn("purpose", normalized)
        self.assertEqual(
            tools.evidence.semantic_request_fingerprint(_request()),
            tools.evidence.semantic_request_fingerprint(legacy),
        )

        with self.assertRaises(tools.QueryFailure):
            tools._validate_request(_request(mode="dataset"))

    def test_envelope_and_batch_boundaries_are_equivalent(self):
        maximum_requests = [
            _request(f"q{index}")
            for index in range(request_contract.PUBLIC_REQUEST_LIMIT)
        ]
        too_many_calculations = [
            {
                "calculation_id": f"calc{index}",
                "operation": "difference",
                "left_request_id": "q1",
                "right_request_id": "q1",
            }
            for index in range(request_contract.PUBLIC_CALCULATION_LIMIT + 1)
        ]
        valid = (
            {"requests": [_request()]},
            {"requests": maximum_requests},
            {
                "requests": [_request()],
                "calculations": [
                    {
                        "calculation_id": "calc1",
                        "operation": "difference",
                        "left_request_id": "q1",
                        "right_request_id": "q1",
                    }
                ],
            },
        )
        invalid = (
            {},
            {"requests": []},
            {"requests": [*maximum_requests, _request("overflow")]},
            {"requests": [_request()], "unexpected": True},
            {"requests": ["not-an-object"]},
            {"requests": [_request()], "calculations": None},
            {
                "requests": [_request()],
                "calculations": too_many_calculations,
            },
            {
                "requests": [_request()],
                "calculations": [
                    {
                        "calculation_id": " ",
                        "operation": "difference",
                        "left_request_id": "q1",
                        "right_request_id": "q1",
                    }
                ],
            },
        )
        for envelope in valid:
            with self.subTest(valid=envelope):
                self.assertEnvelopeEquivalent(envelope, True)
        for envelope in invalid:
            with self.subTest(invalid=envelope):
                self.assertEnvelopeEquivalent(envelope, False)

    def test_runtime_relational_constraints_reject_schema_shape_only_inputs(self):
        duplicate_request_ids = {
            "requests": [_request("duplicate"), _request("duplicate")]
        }
        unknown_calculation_reference = {
            "requests": [_request()],
            "calculations": [
                {
                    "calculation_id": "calc1",
                    "operation": "difference",
                    "left_request_id": "missing",
                    "right_request_id": "q1",
                }
            ],
        }
        for envelope in (duplicate_request_ids, unknown_calculation_reference):
            with self.subTest(envelope=envelope):
                # Draft 7 cannot express uniqueness/reference integrity across
                # sibling object properties; these remain runtime-only facts.
                self.assertFalse(list(self.envelope_validator.iter_errors(envelope)))
                self.assertFalse(self._contract_envelope_valid(envelope))

    def test_schema_limits_delegate_to_request_and_capability_contracts(self):
        self.assertEqual(
            capability_contract.PUBLIC_REQUEST_LIMIT,
            request_contract.PUBLIC_REQUEST_LIMIT,
        )
        parameters = schemas.DATASAGE_QUERY["parameters"]["properties"]
        self.assertEqual(
            request_contract.PUBLIC_REQUEST_LIMIT,
            parameters["requests"]["maxItems"],
        )
        self.assertEqual(
            request_contract.PUBLIC_CALCULATION_LIMIT,
            parameters["calculations"]["maxItems"],
        )
        self.assertEqual(
            request_contract.MAX_METRIC_FILTERS,
            schemas.REQUEST["properties"]["metric_filters"]["maxProperties"],
        )
        self.assertEqual(
            request_contract.MAX_FILTER_VALUES,
            schemas.REQUEST["properties"]["metric_filters"]
            ["additionalProperties"]["oneOf"][-1]["maxItems"],
        )

    def test_runtime_guard_reuses_prevalidated_dispatch_on_ready_path(self):
        args = {"requests": [_request()]}
        envelope = request_contract.validate_query_envelope(args)
        with (
            mock.patch.object(
                tools,
                "_validate_query_dispatch",
                return_value=envelope,
            ) as validate,
            mock.patch.object(
                runtime_health,
                "query_readiness_status",
                return_value={"ready": True},
            ),
            mock.patch.object(
                entitlements,
                "authorized",
                return_value=True,
            ),
            mock.patch.object(
                tools,
                "_datasage_query_with_slot",
                return_value=json.dumps({"status": "success"}),
            ) as execute,
        ):
            payload = json.loads(tools.entitlement_guarded_datasage_query(args))

        self.assertEqual("success", payload["status"])
        self.assertEqual(1, validate.call_count)
        validated = execute.call_args.kwargs.get("_validated_query_envelope")
        self.assertIsInstance(
            validated, request_contract.ValidatedQueryEnvelope
        )

    def test_nested_calculations_fail_before_entitlement_readiness_or_database(self):
        args = {
            "requests": [
                _request(
                    calculations=[
                        {
                            "calculation_id": "calc1",
                            "operation": "difference",
                            "left_request_id": "q1",
                            "right_request_id": "q1",
                        }
                    ]
                )
            ]
        }
        with (
            mock.patch.object(entitlements, "authorized") as authorized,
            mock.patch.object(runtime_health, "query_readiness_status") as readiness,
            mock.patch.object(db_runtime, "connect") as connect,
            mock.patch.object(tools, "_execute") as execute,
        ):
            payload = json.loads(tools.entitlement_guarded_datasage_query(args))

        self.assertEqual("failed", payload["status"])
        self.assertEqual("INVALID_INPUT", payload["error"]["code"])
        self.assertEqual("requests[0].calculations", payload["error"]["path"])
        self.assertIn("top level", payload["error"]["hint"])
        authorized.assert_not_called()
        readiness.assert_not_called()
        connect.assert_not_called()
        execute.assert_not_called()

    def test_business_semantics_fail_before_entitlement_or_readiness(self):
        args = {"requests": [_request(metric="not_a_registered_metric")]}
        with (
            mock.patch.object(entitlements, "authorized") as authorized,
            mock.patch.object(runtime_health, "query_readiness_status") as readiness,
        ):
            payload = json.loads(tools.entitlement_guarded_datasage_query(args))

        self.assertEqual("failed", payload["status"])
        self.assertNotEqual("DATA_ENTITLEMENT_DENIED", payload["error"]["code"])
        self.assertEqual("requests[0].metric", payload["error"]["path"])
        authorized.assert_not_called()
        readiness.assert_not_called()

    def test_validated_requests_preserve_existing_entitlement_decision(self):
        args = {"requests": [_request(metric="delivery_amount")]}
        envelope = request_contract.validate_query_envelope(args)
        rule = {
            "domains": ["delivery"],
            "metrics": {"delivery": ["delivery_amount"]},
            "allow_all_rows": True,
        }
        self.assertEqual(
            entitlements._query_allowed(rule, args),
            entitlements._query_allowed(
                rule,
                args,
                validated_requests=envelope.requests,
            ),
        )

    def test_entity_schema_and_runtime_share_enums_limits_and_uniqueness(self):
        validator = jsonschema.Draft7Validator(
            schemas.DATASAGE_ENTITY_RESOLVE["parameters"]
        )
        valid = {
            "token": "Acme",
            "entity_types": [capability_contract.ENTITY_TYPES[0]],
            "limit": capability_contract.ENTITY_RESOLVE_HARD_LIMIT,
        }
        self.assertFalse(list(validator.iter_errors(valid)))
        self.assertEqual(
            capability_contract.ENTITY_RESOLVE_HARD_LIMIT,
            entities._validate_args(valid)[-1],
        )

        invalid_values = (
            {**valid, "entity_types": [valid["entity_types"][0]] * 2},
            {**valid, "entity_types": ["unknown_entity_type"]},
            {**valid, "limit": capability_contract.ENTITY_RESOLVE_HARD_LIMIT + 1},
            {**valid, "attribution_mode": "invented_mode", "metric": "metric"},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertTrue(list(validator.iter_errors(value)))
                with self.assertRaises(entities.EntityFailure) as caught:
                    entities._validate_args(value)
                self.assertEqual("INVALID_INPUT", caught.exception.code)

    def test_capability_contract_owns_domain_semantic_paths(self):
        self.assertEqual(
            set(capability_contract.SUPPORTED_DOMAINS),
            set(entities._SEMANTIC_PATHS),
        )
        self.assertEqual(
            {
                domain: capability_contract.DOMAIN_SOURCES[domain]["semantics"]
                for domain in capability_contract.SUPPORTED_DOMAINS
            },
            entities._SEMANTIC_PATHS,
        )

    def test_entity_runtime_contract_owns_normalization_and_policy(self):
        registry = entities._registry()
        self.assertEqual(
            capability_contract.ENTITY_NORMALIZATION_STEPS,
            tuple(registry["normalization"]),
        )
        self.assertEqual(
            capability_contract.ENTITY_RUNTIME_POLICY,
            registry["policy"],
        )
        self.assertEqual("acme co", entities.normalize_text("  ＡＣＭＥ   Co  "))

    def test_direct_executor_validates_an_invalid_envelope_once(self):
        with mock.patch.object(
            tools,
            "_validated_query_envelope",
            wraps=tools._validated_query_envelope,
        ) as validate:
            payload = json.loads(tools.datasage_query({"requests": []}))
        self.assertEqual("failed", payload["status"])
        self.assertEqual("INVALID_INPUT", payload["error"]["code"])
        self.assertEqual(1, validate.call_count)


if __name__ == "__main__":
    unittest.main()
