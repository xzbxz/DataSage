"""Regression tests for the lightweight DataSage conversation boundary."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
PACKAGE = "datasage_conversation_receipt_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault(PACKAGE, package)

contracts = importlib.import_module(f"{PACKAGE}.contracts")
entities = importlib.import_module(f"{PACKAGE}.entities")
schemas = importlib.import_module(f"{PACKAGE}.schemas")
tools = importlib.import_module(f"{PACKAGE}.tools")


class ConversationBoundaryTests(unittest.TestCase):
    @staticmethod
    def _resolved(token: str = "HCM") -> dict:
        payload = json.loads(
            entities.datasage_entity_resolve(
                {
                    "token": token,
                    "entity_types": ["department"],
                    "domain": "delivery",
                    "metric": "delivery_amount",
                }
            )
        )
        if payload.get("status") != "resolved":
            raise AssertionError(payload)
        return payload["candidates"][0]

    def _request(
        self,
        *,
        metric_filters: dict[str, object] | None = None,
        metric: str = "delivery_amount",
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "request_id": "q1",
            "domain": "delivery",
            "metric": metric,
        }
        if metric_filters is not None:
            request["metric_filters"] = metric_filters
        return request

    def test_catalog_detail_and_resolver_have_no_model_receipts(self):
        self.assertNotIn("detail_receipt", schemas.REQUEST["properties"])
        self.assertNotIn("resolution_receipts", schemas.REQUEST["properties"])
        detail = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]}
            )
        )
        self.assertNotIn("detail_receipt", detail["results"][0])

        candidate = self._resolved()
        self.assertEqual("胡志明", candidate["display_name"])
        self.assertEqual("department", candidate["entity_type"])
        self.assertNotIn("resolution_receipt", candidate)
        self.assertNotIn("resolution_session_binding", candidate)

    def test_unique_registered_entity_can_be_preflighted_without_receipt_or_session(self):
        request = self._request(metric_filters={"department": "HCM"})
        prepared = tools._prepare_one(
            request,
            deadline_at=None,
            resolution_cache={},
            max_unique_lookups=10,
            preflight_stats={},
        )
        self.assertEqual("HCM", prepared["request"]["metric_filters"]["department"])
        self.assertEqual(
            "registered_exact",
            prepared["resolved_entities"][0]["resolution_path"],
        )

    def test_query_rejects_old_receipt_fields_as_unknown_input(self):
        request = self._request()
        request["detail_receipt"] = "0" * 64
        with self.assertRaises(tools.QueryFailure) as context:
            tools._validate_request_plan_without_entities(request)
        self.assertEqual("INVALID_INPUT", context.exception.code)

        request = self._request(metric_filters={"department": "HCM"})
        request["resolution_receipts"] = ["0" * 64]
        with self.assertRaises(tools.QueryFailure) as context:
            tools._validate_request_plan_without_entities(request)
        self.assertEqual("INVALID_INPUT", context.exception.code)

    def test_explicit_period_reloads_contract_without_detail_receipt(self):
        request = self._request()
        request["time_range"] = {"start": "2026-07-01", "end": "2026-08-01"}
        validated, _datasets, _semantics = tools._validate_request_plan_without_entities(request)
        self.assertEqual(request["time_range"], validated["time_range"])

    def test_ambiguous_or_missing_entity_fails_closed_and_guides_resolver(self):
        semantics = contracts.execution_contracts("delivery")[1]
        request = self._request(metric_filters={"customer": "not-a-real-customer"})

        def no_matches(_sql, _params, _limit):
            return [], False

        with self.assertRaises(entities.EntityFailure) as context:
            entities.canonicalize_metric_request(
                request,
                semantics,
                exact_lookup=no_matches,
            )
        self.assertEqual("ENTITY_NOT_FOUND", context.exception.code)
        self.assertIn("datasage_entity_resolve", str(context.exception))

        ambiguous = self._request(metric_filters={"customer": "ambiguous"})

        def two_matches(_sql, _params, _limit):
            return [
                {
                    "entity_type": "customer",
                    "canonical_id": "d1",
                    "canonical_code": "D1",
                    "display_name": "Ambiguous A",
                    "matched_value": "ambiguous",
                    "match_rank": 0,
                },
                {
                    "entity_type": "customer",
                    "canonical_id": "d2",
                    "canonical_code": "D2",
                    "display_name": "Ambiguous B",
                    "matched_value": "ambiguous",
                    "match_rank": 0,
                },
            ], False

        with self.assertRaises(entities.EntityFailure) as context:
            entities.canonicalize_metric_request(
                ambiguous,
                semantics,
                exact_lookup=two_matches,
            )
        self.assertEqual("ENTITY_AMBIGUOUS", context.exception.code)
        self.assertIn("datasage_entity_resolve", str(context.exception))

    def test_entity_only_resolver_still_returns_bounded_candidates(self):
        payload = json.loads(
            entities.datasage_entity_resolve(
                {"token": "HCM", "entity_types": ["department"]}
            )
        )
        self.assertIn(payload["status"], {"resolved", "ambiguous"})
        self.assertIn("candidates", payload)
        self.assertNotIn("resolution_receipt", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
