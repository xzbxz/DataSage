"""Regression tests for entity-selection receipts and detail recovery."""

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


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
PACKAGE = "datasage_conversation_receipt_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

contracts = importlib.import_module(f"{PACKAGE}.contracts")
entities = importlib.import_module(f"{PACKAGE}.entities")
receipt_cache = importlib.import_module(f"{PACKAGE}.receipt_cache")
tools = importlib.import_module(f"{PACKAGE}.tools")


class ConversationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        receipt_cache.clear_resolution_receipts()

    def tearDown(self) -> None:
        receipt_cache.clear_resolution_receipts()

    @staticmethod
    def _resolved(token: str = "HCM", *, session_id: str | None = "session-a") -> dict:
        kwargs = {"session_id": session_id} if session_id is not None else {}
        payload = json.loads(
            entities.datasage_entity_resolve(
                {
                    "token": token,
                    "entity_types": ["department"],
                    "domain": "delivery",
                    "metric": "delivery_amount",
                },
                **kwargs,
            )
        )
        if payload.get("status") != "resolved":
            raise AssertionError(payload)
        return payload["candidates"][0]

    @staticmethod
    def _detail_receipt(domain: str = "delivery", metric: str = "delivery_amount") -> str:
        payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": domain, "metric": metric}]}
            )
        )
        return str(payload["results"][0]["detail_receipt"])

    def _request(
        self,
        *,
        metric_filters: dict[str, object] | None = None,
        resolution_receipts: list[str] | None = None,
        metric: str = "delivery_amount",
        detail_receipt: str | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "request_id": "q1",
            "domain": "delivery",
            "metric": metric,
        }
        if detail_receipt is not None:
            request["detail_receipt"] = detail_receipt
        if metric_filters is not None:
            request["metric_filters"] = metric_filters
        if resolution_receipts is not None:
            request["resolution_receipts"] = resolution_receipts
        return request

    def _validated_with_receipts(
        self,
        request: dict[str, object],
        *,
        session_id: str | None = "session-a",
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        request["detail_receipt"] = self._detail_receipt()
        validated, _datasets, semantics = tools._validate_request_plan_without_entities(
            request,
            trusted_session_id=session_id,
        )
        records = validated.pop("_resolution_receipt_records", [])
        validated.pop("resolution_receipts", None)
        return validated, records

    def test_resolver_returns_random_selection_receipt_and_explicit_session_state(self):
        candidate = self._resolved()
        token = candidate.get("resolution_receipt")
        self.assertIsInstance(token, str)
        self.assertEqual(64, len(token))
        self.assertEqual("bound", candidate.get("resolution_session_binding"))
        self.assertEqual("not_proven", candidate.get("confirmation_state"))
        record = receipt_cache.get_resolution_receipt(token)
        self.assertIsNotNone(record)
        self.assertEqual("delivery", record["domain"])
        self.assertEqual("delivery_amount", record["metric"])
        self.assertEqual("department", record["entity_type"])
        self.assertEqual("department", record["filter_role"])
        self.assertEqual("not_proven", record["confirmation_state"])
        self.assertNotIn("session-a", json.dumps(record, ensure_ascii=False))

    def test_unbound_session_is_explicit_but_cannot_authorize_query(self):
        candidate = self._resolved(session_id=None)
        self.assertEqual("session_unbound", candidate["resolution_session_binding"])
        request = self._request(
            metric_filters={"department": "HCM"},
            resolution_receipts=[candidate["resolution_receipt"]],
        )
        with self.assertRaises(tools.QueryFailure) as context:
            self._validated_with_receipts(request, session_id=None)
        self.assertEqual(
            "RESOLUTION_RECEIPT_SESSION_REQUIRED",
            context.exception.code,
        )
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                request,
                contracts.execution_contracts("delivery")[1],
                session_id="session-b",
            )
        self.assertEqual("RESOLUTION_RECEIPT_SESSION_MISMATCH", context.exception.code)

    def test_missing_or_unmanufactured_receipt_is_rejected_before_db(self):
        request = self._request(
            metric_filters={"department": "HCM"},
            detail_receipt=self._detail_receipt(),
        )
        with mock.patch.object(tools, "_execute_with_source") as execute:
            with self.assertRaises(tools.QueryFailure) as context:
                tools._prepare_one(
                    request,
                    deadline_at=None,
                    resolution_cache={},
                    max_unique_lookups=10,
                    preflight_stats={},
                    trusted_session_id="session-a",
                )
        self.assertEqual("RESOLUTION_RECEIPT_INVALID", context.exception.code)
        execute.assert_not_called()

        fake = "0" * 64
        request["resolution_receipts"] = [fake]
        with self.assertRaises(tools.QueryFailure) as context:
            tools._prepare_one(
                request,
                deadline_at=None,
                resolution_cache={},
                max_unique_lookups=10,
                preflight_stats={},
                trusted_session_id="session-a",
            )
        self.assertEqual("RESOLUTION_RECEIPT_INVALID", context.exception.code)

    def test_wrong_session_metric_and_entity_are_rejected(self):
        candidate = self._resolved()
        token = candidate["resolution_receipt"]
        request = self._request(
            metric_filters={"department": "HCM"},
            resolution_receipts=[token],
        )
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                request,
                contracts.execution_contracts("delivery")[1],
                session_id="session-b",
            )
        self.assertEqual("RESOLUTION_RECEIPT_SESSION_MISMATCH", context.exception.code)

        wrong_metric = copy.deepcopy(request)
        wrong_metric["metric"] = "gross_delivery_amount"
        wrong_metric["detail_receipt"] = self._detail_receipt(
            metric="gross_delivery_amount"
        )
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                wrong_metric,
                contracts.execution_contracts("delivery")[1],
                session_id="session-a",
            )
        self.assertEqual("RESOLUTION_RECEIPT_CONTEXT_MISMATCH", context.exception.code)

        wrong_entity = copy.deepcopy(request)
        wrong_entity["metric_filters"] = {"customer": "HCM"}
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                wrong_entity,
                contracts.execution_contracts("delivery")[1],
                session_id="session-a",
            )
        self.assertEqual("RESOLUTION_RECEIPT_SCOPE_MISMATCH", context.exception.code)

    def test_tampered_and_contract_drifted_receipts_fail_closed(self):
        candidate = self._resolved()
        token = candidate["resolution_receipt"]
        request = self._request(
            metric_filters={"department": "HCM"},
            resolution_receipts=[token],
        )
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        request["resolution_receipts"] = [tampered]
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                request,
                contracts.execution_contracts("delivery")[1],
                session_id="session-a",
            )
        self.assertEqual("RESOLUTION_RECEIPT_INVALID", context.exception.code)

        request["resolution_receipts"] = [token]
        with mock.patch.object(
            receipt_cache,
            "resolution_contract_signature",
            return_value="sha256_" + "f" * 64,
        ):
            with self.assertRaises(entities.EntityFailure) as context:
                entities.validate_resolution_receipts(
                    request,
                    contracts.execution_contracts("delivery")[1],
                    session_id="session-a",
                )
        self.assertEqual("RESOLUTION_RECEIPT_STALE", context.exception.code)

    def test_multiple_entity_filters_require_exact_receipt_coverage_and_identity(self):
        first = self._resolved("HCM")
        second = self._resolved("HN")
        request = self._request(
            metric_filters={"department": ["HCM", "HN"]},
            resolution_receipts=[
                first["resolution_receipt"],
                second["resolution_receipt"],
            ],
        )
        validated, records = self._validated_with_receipts(request)
        self.assertEqual(2, len(records))
        self.assertEqual(
            {"HCM", "HN"},
            {value for record in records for value in record["filter_values"]},
        )
        entities.validate_resolution_receipt_bindings(validated, records)

        request["resolution_receipts"] = [first["resolution_receipt"]]
        with self.assertRaises(entities.EntityFailure) as context:
            entities.validate_resolution_receipts(
                request,
                contracts.execution_contracts("delivery")[1],
                session_id="session-a",
            )
        self.assertEqual("RESOLUTION_RECEIPT_SCOPE_MISMATCH", context.exception.code)

    def test_detail_receipt_error_has_deterministic_compaction_recovery(self):
        request = self._request(
            metric_filters=None,
            detail_receipt=None,
        )
        request["time_range"] = {"start": "2026-07-01", "end": "2026-08-01"}
        with self.assertRaises(tools.QueryFailure) as context:
            tools._validate_request_plan_without_entities(request)
        failure = context.exception
        self.assertEqual("METRIC_DETAIL_REQUIRED", failure.code)
        self.assertEqual("reload_metric_detail", failure.recovery_action)
        self.assertEqual(
            {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]},
            failure.catalog_request,
        )

        request["detail_receipt"] = "0" * 64
        with self.assertRaises(tools.QueryFailure) as context:
            tools._validate_request_plan_without_entities(request)
        self.assertEqual("METRIC_DETAIL_RECEIPT_INVALID", context.exception.code)
        self.assertEqual("reload_metric_detail", context.exception.recovery_action)

    def test_no_entity_default_query_remains_compatible(self):
        request = self._request()
        validated, _datasets, semantics = tools._validate_request_plan_without_entities(request)
        self.assertNotIn("resolution_receipts", validated)
        self.assertEqual("delivery_amount", validated["metric"])
        self.assertEqual([], entities.validate_resolution_receipts(validated, semantics))


if __name__ == "__main__":
    unittest.main()
