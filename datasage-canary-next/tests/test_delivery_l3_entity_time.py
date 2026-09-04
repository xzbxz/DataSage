"""Offline L3 regression tests for delivery entity/time/context boundaries."""

from __future__ import annotations

from datetime import date
import hashlib
import importlib
import importlib.util
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

PACKAGE = "datasage_delivery_l3_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PACKAGE] = package

entities = importlib.import_module(f"{PACKAGE}.entities")
contracts = importlib.import_module(f"{PACKAGE}.contracts")
tools = importlib.import_module(f"{PACKAGE}.tools")


def _load_adapter():
    qualified = f"{PACKAGE}.canary_transcript_adapter"
    existing = sys.modules.get(qualified)
    if existing is not None:
        return existing
    path = PLUGIN_ROOT / "e2e" / "canary_transcript_adapter.py"
    spec = importlib.util.spec_from_file_location(qualified, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load transcript adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_adapter()


class DeliveryL3EntityTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.datasets, cls.semantics = contracts.execution_contracts("delivery")

    @staticmethod
    def _master_row(
        entity_type: str,
        canonical_id: str,
        canonical_code: str | None,
        display_name: str,
        token: str,
    ) -> dict[str, object]:
        return {
            "entity_type": entity_type,
            "canonical_id": canonical_id,
            "canonical_code": canonical_code,
            "display_name": display_name,
            "matched_value": token,
            "match_rank": 0,
        }

    def test_source_exact_values_are_not_entity_resolved(self) -> None:
        values = (
            ("department", "delivery_amount", "部門 Ω\t原值"),
            ("warehouse_department", "warehouse_gross_delivery_amount", "仓储部 Ω"),
            ("organization", "delivery_amount", "组织 Ω\t原值"),
            ("bill_type", "return_amount", "退货类型 Ω"),
        )
        for role, metric, value in values:
            with self.subTest(role=role):
                lookup_calls: list[object] = []

                def exact_lookup(*args):
                    lookup_calls.append(args)
                    return [], False

                normalized, evidence = entities.canonicalize_metric_request(
                    {
                        "domain": "delivery",
                        "metric": metric,
                        "metric_filters": {role: value},
                    },
                    self.semantics,
                    exact_lookup=exact_lookup,
                )
                self.assertEqual(value, normalized["metric_filters"][role])
                self.assertEqual([], evidence)
                self.assertEqual([], lookup_calls)

        registered, evidence = entities.canonicalize_metric_request(
            {
                "domain": "delivery",
                "metric": "delivery_amount",
                "metric_filters": {"department": "HCM含家纺"},
            },
            self.semantics,
            exact_lookup=lambda *_args: ([], False),
        )
        self.assertEqual(["HCM", "HCM-HT"], registered["metric_filters"]["department"])
        self.assertEqual("registered_exact", evidence[0]["resolution_path"])

    def test_delivery_master_entities_bind_ids_and_same_names_fail_closed(self) -> None:
        cases = (
            ("customer", "c1", "C-1", "客户甲", "customer_id"),
            ("salesperson", "s1", "S-1", "业务员甲", "sales_id"),
            ("product", "p1", "P-1", "产品甲", "goods_id"),
        )
        for role, canonical_id, canonical_code, display_name, identity_column in cases:
            with self.subTest(role=role):
                row = self._master_row(
                    role,
                    canonical_id,
                    canonical_code,
                    display_name,
                    display_name,
                )
                normalized, evidence = entities.canonicalize_metric_request(
                    {
                        "domain": "delivery",
                        "metric": "delivery_amount",
                        "metric_filters": {role: display_name},
                    },
                    self.semantics,
                    exact_lookup=lambda *_args, row=row: ([row], False),
                )
                self.assertEqual(canonical_id, normalized["metric_filters"][role])
                binding = normalized["_entity_bindings"][role]
                self.assertEqual("canonical_id", binding["value_field"])
                self.assertIn(identity_column, binding["identity_columns"])
                self.assertEqual("master_exact_preflight", evidence[0]["resolution_path"])

                ambiguous_rows = [
                    row,
                    {
                        **row,
                        "canonical_id": f"{canonical_id}-other",
                        "canonical_code": f"{canonical_code}-other",
                    },
                ]
                with self.assertRaises(entities.EntityFailure) as caught:
                    entities.canonicalize_metric_request(
                        {
                            "domain": "delivery",
                            "metric": "delivery_amount",
                            "metric_filters": {role: display_name},
                        },
                        self.semantics,
                        exact_lookup=lambda *_args, rows=ambiguous_rows: (rows, False),
                    )
                self.assertEqual("ENTITY_AMBIGUOUS", caught.exception.code)

    def test_explicit_unsupported_resolver_role_fails_before_candidate_query(self) -> None:
        calls: list[object] = []

        def no_rows(*args):
            calls.append(args)
            return [], False

        with mock.patch.object(entities.db_runtime, "execute", side_effect=no_rows):
            payload = json.loads(
                entities.datasage_entity_resolve(
                    {
                        "token": "仓库甲",
                        "entity_types": ["warehouse"],
                        "domain": "delivery",
                        "metric": "delivery_amount",
                    }
                )
            )
        self.assertEqual("failed", payload["status"])
        self.assertEqual("UNSUPPORTED_ENTITY_ROLE", payload["error"]["code"])
        self.assertEqual([], calls)

        calls.clear()
        supplier_row = self._master_row(
            "supplier",
            "supplier-1",
            "SUP-1",
            "供应商甲",
            "供应商甲",
        )
        with mock.patch.object(
            entities.db_runtime,
            "execute",
            return_value=([supplier_row], False),
        ) as supplier_lookup:
            payload = json.loads(
                entities.datasage_entity_resolve(
                    {
                        "token": "供应商甲",
                        "entity_types": ["supplier"],
                        "domain": "delivery",
                        "metric": "warehouse_gross_delivery_amount",
                    }
                )
            )
        self.assertEqual("resolved", payload["status"])
        self.assertEqual("final_supplier", payload["candidates"][0]["filter_role"])
        self.assertEqual("supplier-1", payload["candidates"][0]["canonical_id"])
        supplier_lookup.assert_called_once()

    def test_final_supplier_filter_binds_supplier_identity(self) -> None:
        row = self._master_row(
            "supplier",
            "supplier-1",
            "SUP-1",
            "供应商甲",
            "供应商甲",
        )
        normalized, evidence = entities.canonicalize_metric_request(
            {
                "domain": "delivery",
                "metric": "warehouse_gross_delivery_amount",
                "metric_filters": {"final_supplier": "供应商甲"},
            },
            self.semantics,
            exact_lookup=lambda *_args: ([row], False),
        )
        self.assertEqual(
            "supplier-1",
            normalized["metric_filters"]["final_supplier"],
        )
        binding = normalized["_entity_bindings"]["final_supplier"]
        self.assertEqual("canonical_id", binding["value_field"])
        self.assertIn("final_supplier_id", binding["identity_columns"])
        self.assertEqual("master_exact_preflight", evidence[0]["resolution_path"])

    @staticmethod
    def _calendar_evidence(observed_on: date, state: str) -> dict[str, object]:
        return {
            "version": "calendar-period-evidence/v2",
            "observed_on": observed_on.isoformat(),
            "observation_basis": "business_clock_query_observation",
            "period_state": state,
            "source_freshness": "not_proven",
        }

    def test_strict_adapter_accepts_default_calendar_completed_and_future_months(self) -> None:
        observed_on = date(2026, 8, 25)
        cases = (
            (
                "default",
                {"domain": "delivery", "metric": "delivery_amount"},
                {"start": "2026-08-01", "end": "2026-09-01", "source": "default_current_month"},
                "in_progress",
            ),
            (
                "calendar",
                {
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "calendar_month": "2026-08",
                },
                {"start": "2026-08-01", "end": "2026-09-01", "source": "explicit"},
                "in_progress",
            ),
            (
                "completed",
                {
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "calendar_month": "2026-07",
                },
                {"start": "2026-07-01", "end": "2026-08-01", "source": "explicit"},
                "completed",
            ),
            (
                "future",
                {
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "calendar_month": "2026-09",
                },
                {"start": "2026-09-01", "end": "2026-10-01", "source": "explicit"},
                "not_started",
            ),
        )
        for label, request, applied, state in cases:
            with self.subTest(label=label):
                applied = {
                    **applied,
                    "calendar_evidence": self._calendar_evidence(observed_on, state),
                }
                self.assertTrue(
                    ADAPTER._applied_time_matches_live_request(
                        applied, request, observed_on
                    )
                )

    def test_strict_adapter_rejects_invalid_time_forms_before_snapshot_fallback(self) -> None:
        observed_on = date(2026, 8, 25)
        snapshot = {
            "source": "current_snapshot",
            "as_of_date": "2026-08-25",
            "resolution_state": "resolved",
        }
        invalid_requests = (
            {"domain": "delivery", "metric": "delivery_amount", "time_range": None},
            {"domain": "delivery", "metric": "delivery_amount", "time_range": []},
            {
                "domain": "delivery",
                "metric": "delivery_amount",
                "time_range": {
                    "start": "2026-08-01",
                    "end": "2026-09-01",
                    "extra": "must reject",
                },
            },
            {
                "domain": "delivery",
                "metric": "delivery_amount",
                "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
                "calendar_month": "2026-08",
            },
            {"domain": "delivery", "metric": "delivery_amount", "calendar_month": None},
            {"domain": "delivery", "metric": "delivery_amount", "calendar_month": "2026-8"},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                self.assertFalse(
                    ADAPTER._applied_time_matches_live_request(
                        snapshot, request, observed_on
                    )
                )

    def test_strict_adapter_validates_flow_comparison_endpoints_and_bindings(self) -> None:
        observed_on = date(2026, 8, 18)

        def flow(start: str, end: str, state: str) -> dict[str, object]:
            return {
                "start": start,
                "end": end,
                "source": "explicit",
                "calendar_evidence": self._calendar_evidence(observed_on, state),
            }

        previous_request = {
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "comparison": {"kind": "previous_period"},
        }
        previous_value = {
            "current": flow("2026-08-01", "2026-09-01", "in_progress"),
            "comparison": flow("2026-07-01", "2026-08-01", "completed"),
            "comparison_compatibility": {
                "status": "coverage_mismatch",
                "reason_codes": ["PERIOD_COVERAGE_MISMATCH"],
            },
        }
        self.assertTrue(
            ADAPTER._applied_time_matches_live_request(
                previous_value, previous_request, observed_on
            )
        )
        previous_missing = dict(previous_value)
        previous_missing.pop("comparison")
        self.assertFalse(
            ADAPTER._applied_time_matches_live_request(
                previous_missing, previous_request, observed_on
            )
        )
        previous_wrong = dict(previous_request)
        previous_wrong["comparison"] = {"kind": "previous_period", "months": 1}
        self.assertFalse(
            ADAPTER._applied_time_matches_live_request(
                previous_value, previous_wrong, observed_on
            )
        )

        yoy_request = {
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "comparison": {"kind": "year_over_year", "coverage": "matched_elapsed"},
        }
        alignment = {
            "version": "matched-elapsed-comparison/v1",
            "kind": "year_over_year",
            "coverage": "matched_elapsed",
            "observed_on": "2026-08-18",
            "requested_current_start": "2026-08-01",
            "requested_current_end": "2026-09-01",
            "effective_current_end": "2026-08-19",
            "current_was_clipped": True,
        }
        yoy_value = {
            "current": flow("2026-08-01", "2026-08-19", "in_progress"),
            "comparison": flow("2025-08-01", "2025-08-19", "completed"),
            "comparison_alignment": alignment,
        }
        self.assertTrue(
            ADAPTER._applied_time_matches_live_request(
                yoy_value, yoy_request, observed_on
            )
        )
        early_cut = json.loads(json.dumps(yoy_value))
        early_cut["current"]["end"] = "2026-08-18"
        early_cut["current"]["calendar_evidence"]["period_state"] = "completed"
        early_cut["comparison"]["end"] = "2025-08-18"
        early_cut["comparison_alignment"]["effective_current_end"] = "2026-08-18"
        self.assertFalse(
            ADAPTER._applied_time_matches_live_request(
                early_cut, yoy_request, observed_on
            )
        )
        invalid_yoy_variants = []
        tampered = json.loads(json.dumps(yoy_value))
        tampered["comparison"]["end"] = "2025-08-20"
        invalid_yoy_variants.append(tampered)
        tampered = json.loads(json.dumps(yoy_value))
        tampered["comparison_alignment"]["effective_current_end"] = "2026-08-20"
        invalid_yoy_variants.append(tampered)
        tampered = json.loads(json.dumps(yoy_value))
        tampered["comparison_alignment"]["requested_current_end"] = "2026-08-19"
        invalid_yoy_variants.append(tampered)
        tampered = json.loads(json.dumps(yoy_value))
        tampered["comparison_alignment"]["extra"] = "reject"
        invalid_yoy_variants.append(tampered)
        for invalid in invalid_yoy_variants:
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    ADAPTER._applied_time_matches_live_request(
                        invalid, yoy_request, observed_on
                    )
                )

        snapshot_request = {
            "domain": "inventory",
            "metric": "current_inventory_amount_rmb",
            "comparison": {"kind": "snapshot_months_before", "months": 2},
        }
        snapshot_value = {
            "current": {
                "source": "latest_snapshot",
                "snapshot_month": "2026-08",
                "resolution_state": "resolved",
            },
            "comparison": {
                "source": "latest_snapshot_offset",
                "snapshot_month": "2026-06",
                "months_before": 2,
                "resolution_state": "resolved",
            },
            "comparison_compatibility": {"status": "compatible", "reason_codes": []},
        }
        self.assertTrue(
            ADAPTER._applied_time_matches_live_request(
                snapshot_value, snapshot_request, observed_on
            )
        )
        for field, value in (
            ("months_before", 1),
            ("snapshot_month", "2026-07"),
        ):
            invalid = json.loads(json.dumps(snapshot_value))
            invalid["comparison"][field] = value
            self.assertFalse(
                ADAPTER._applied_time_matches_live_request(
                    invalid, snapshot_request, observed_on
                )
            )
        invalid = json.loads(json.dumps(snapshot_value))
        invalid["comparison_alignment"] = {}
        self.assertFalse(
            ADAPTER._applied_time_matches_live_request(
                invalid, snapshot_request, observed_on
            )
        )

    def test_capture_business_date_uses_asia_shanghai_and_strict_snapshot_text(self) -> None:
        self.assertEqual(
            date(2026, 8, 26),
            ADAPTER._captured_business_date("2026-08-25T23:30:00-05:00"),
        )
        with self.assertRaises(ValueError):
            ADAPTER._captured_business_date("2026-08-25T23:30:00")
        observed_on = date(2026, 8, 25)
        for field, value in (
            ("as_of_date", observed_on),
            ("snapshot_month", date(2026, 8, 1)),
        ):
            request = {
                "domain": "inventory",
                "metric": "current_inventory_amount_rmb",
            }
            applied = (
                {
                    "source": "current_snapshot",
                    "as_of_date": value,
                    "resolution_state": "resolved",
                }
                if field == "as_of_date"
                else {
                    "source": "latest_snapshot",
                    "snapshot_month": value,
                    "resolution_state": "resolved",
                }
            )
            self.assertFalse(
                ADAPTER._applied_time_matches_live_request(
                    applied, request, observed_on
                )
            )

    def test_ambiguity_preserve_requires_role_dimension_and_filter_compatibility(self) -> None:
        compatible = {
            "dimensions": ["customer"],
            "context_bindings": {
                "filter_fingerprints": {"customer": "a" * 64}
            },
        }
        self.assertTrue(
            ADAPTER._ambiguity_context_compatible(
                compatible,
                frozenset({"customer"}),
                ADAPTER._plan_filter_signature(compatible),
            )
        )
        changed_set = {**compatible, "dimensions": ["customer", "supplier"]}
        self.assertFalse(
            ADAPTER._ambiguity_context_compatible(
                changed_set,
                frozenset({"customer", "product"}),
                ADAPTER._plan_filter_signature(compatible),
            )
        )
        wrong_role = {**compatible, "dimensions": ["supplier"]}
        self.assertFalse(
            ADAPTER._ambiguity_context_compatible(
                wrong_role,
                frozenset({"customer"}),
                ADAPTER._plan_filter_signature(compatible),
            )
        )
        missing_filter = {"dimensions": ["customer"], "context_bindings": {"filter_fingerprints": {}}}
        self.assertFalse(
            ADAPTER._ambiguity_context_compatible(
                missing_filter,
                frozenset({"customer"}),
                ADAPTER._plan_filter_signature(compatible),
            )
        )

    def test_delivery_runtime_default_and_explicit_period_states(self) -> None:
        observed_on = date(2026, 8, 18)
        base = {
            "request_id": "delivery_l3_period",
            "domain": "delivery",
            "mode": "metric",
            "metric": "delivery_amount",
            "dimensions": [],
        }
        cases = (
            ("default", {}, ("2026-08-01", "2026-09-01"), "in_progress"),
            (
                "completed",
                {"calendar_month": "2026-07"},
                ("2026-07-01", "2026-08-01"),
                "completed",
            ),
            (
                "future",
                {"calendar_month": "2026-09"},
                ("2026-09-01", "2026-10-01"),
                "not_started",
            ),
        )
        for label, extra, expected_bounds, expected_state in cases:
            with self.subTest(label=label):
                request = {**base, **extra}
                normalized = tools._validate_request(request)
                normalized = tools._validate_metric_contract(normalized, self.semantics)
                _sql, _params, scope = tools._build_metric_query(
                    normalized,
                    self.datasets,
                    self.semantics,
                    10,
                    observed_on=observed_on,
                )
                time_range = scope["time_range"]
                self.assertEqual(expected_bounds, (time_range["start"], time_range["end"]))
                annotated = tools._annotate_period_evidence(
                    tools._public_time_range(time_range), observed_on
                )
                self.assertEqual(
                    expected_state,
                    annotated["calendar_evidence"]["period_state"],
                )

    def test_period_only_signature_preserves_context_without_preserving_changed_scope(self) -> None:
        base = {
            "domains": ["delivery"],
            "metrics": ["delivery_amount"],
            "domain_metric_pairs": [{"domain": "delivery", "metric": "delivery_amount"}],
            "dimensions": ["product"],
            "operations": ["top_n"],
            "time_semantics": "range:2026-08-01/2026-09-01",
            "context_action": "new",
            "context_bindings": {
                "filter_fingerprints": {"customer": "a" * 64}
            },
        }
        period_only = {
            **base,
            "operations": ["top_n", "previous_period"],
            "time_semantics": "range:2026-09-01/2026-10-01",
        }
        changed_entity = {
            **period_only,
            "context_bindings": {
                "filter_fingerprints": {"customer": "b" * 64}
            },
        }
        changed_dimension = {**period_only, "dimensions": ["customer"]}
        self.assertEqual(
            ADAPTER._period_insensitive_plan_signature(base),
            ADAPTER._period_insensitive_plan_signature(period_only),
        )
        self.assertNotEqual(
            ADAPTER._period_insensitive_plan_signature(period_only),
            ADAPTER._period_insensitive_plan_signature(changed_entity),
        )
        self.assertNotEqual(
            ADAPTER._period_insensitive_plan_signature(period_only),
            ADAPTER._period_insensitive_plan_signature(changed_dimension),
        )

    def test_period_only_adaptation_preserves_session_context(self) -> None:
        """Exercise the no-review-trace path, not only its helper fingerprint."""

        session_id = "delivery-l3-session"
        profile = {
            "profile_id": "datasage-canary-next",
            "artifact_id": "a" * 64,
            "payload_sha256": "b" * 64,
        }
        prompts = (
            "查八月按客户的净出库。",
            "再看七月，其他条件不变。",
        )
        requests = (
            {
                "request_id": "delivery_l3_turn_1",
                "domain": "delivery",
                "metric": "delivery_amount",
                "dimensions": ["customer"],
                "metric_filters": {"department": "North"},
                "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            },
            {
                "request_id": "delivery_l3_turn_2",
                "domain": "delivery",
                "metric": "delivery_amount",
                "dimensions": ["customer"],
                "metric_filters": {"department": "North"},
                "time_range": {"start": "2026-07-01", "end": "2026-08-01"},
            },
        )

        def query_call(request: dict[str, object], call_id: str) -> dict[str, object]:
            result = {
                "status": "success",
                "request_count": 1,
                "model_wire_version": "datasage-query-model-wire/v3",
                "results": [
                    {
                        "request_id": request["request_id"],
                        "status": "success",
                        "data_state": "rows",
                        "business_metric_ref": "metric_delivery_l3",
                        "business_metric_label": "净出库",
                        "row_count": 1,
                        "truncated": False,
                        "requested_limit": 100,
                        "effective_limit": 100,
                        "has_more": False,
                        "error": None,
                    }
                ],
                "evidence_bundle": {
                    "items": [{"request_id": request["request_id"], "status": "success"}]
                },
            }
            return {
                "assistant": {
                    "id": None,
                    "session_id": session_id,
                    "active": 1,
                    "role": "assistant",
                    "content": None,
                    "tool_call_id": None,
                    "tool_name": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {
                                "name": "datasage_query",
                                "arguments": json.dumps(
                                    {"requests": [request]}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                    "platform_message_id": f"platform-{call_id}-assistant",
                },
                "tool": {
                    "id": None,
                    "session_id": session_id,
                    "active": 1,
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                    "tool_call_id": call_id,
                    "tool_name": "datasage_query",
                    "tool_calls": None,
                    "platform_message_id": f"platform-{call_id}-tool",
                },
            }

        messages: list[dict[str, object]] = []
        next_id = 1
        turn_rows: list[tuple[int, int, str]] = []
        for turn, (prompt, request) in enumerate(zip(prompts, requests), start=1):
            user_id = next_id
            messages.append(
                {
                    "id": user_id,
                    "session_id": session_id,
                    "active": 1,
                    "role": "user",
                    "content": prompt,
                    "tool_calls": None,
                    "tool_call_id": None,
                    "tool_name": None,
                    "platform_message_id": f"platform-user-{turn}",
                }
            )
            next_id += 1
            call_id = f"delivery-l3-query-{turn}"
            rows = query_call(request, call_id)
            rows["assistant"]["id"] = next_id
            messages.append(rows["assistant"])
            next_id += 1
            rows["tool"]["id"] = next_id
            messages.append(rows["tool"])
            next_id += 1
            final_id = next_id
            messages.append(
                {
                    "id": final_id,
                    "session_id": session_id,
                    "active": 1,
                    "role": "assistant",
                    "content": f"turn {turn} result",
                    "tool_calls": None,
                    "tool_call_id": None,
                    "tool_name": None,
                    "platform_message_id": f"platform-final-{turn}",
                }
            )
            next_id += 1
            turn_rows.append((user_id, final_id, prompt))

        export = json.dumps(
            {
                "id": session_id,
                "source": "wecom",
                "profile_name": profile["profile_id"],
                "messages": messages,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        export_sha = hashlib.sha256(export).hexdigest()
        turns = []
        for turn, ((user_id, final_id, prompt), request) in enumerate(
            zip(turn_rows, requests), start=1
        ):
            prompt_sha = ADAPTER._sha256(prompt)
            turns.append(
                {
                    "test_id": f"delivery_l3_period_{turn}",
                    "conversation_id": "delivery_l3_period",
                    "turn": turn,
                    "session_id": session_id,
                    "user_message_id": user_id,
                    "final_message_id": final_id,
                    "canonical_prompt_sha256": prompt_sha,
                    "watermark_sha256": ADAPTER._watermark(
                        test_id=f"delivery_l3_period_{turn}",
                        conversation_id="delivery_l3_period",
                        turn=turn,
                        canonical_prompt_sha256=prompt_sha,
                        user_message_id=user_id,
                        session_export_sha256=export_sha,
                        profile=profile,
                    ),
                }
            )

        candidate = ADAPTER.adapt(
            export,
            {
                "schema": ADAPTER.BINDING_SCHEMA,
                "captured_at": "2026-08-25",
                "transcript_source": "wecom",
                "profile_artifact": profile,
                "session_export_sha256": export_sha,
                "turns": turns,
            },
        )
        self.assertEqual("new", candidate["cases"][0]["plan"]["context_action"])
        self.assertEqual("preserve", candidate["cases"][1]["plan"]["context_action"])
        self.assertIn("context_transition", candidate["cases"][1]["evidence"]["receipts"])

    def test_internal_customer_filter_cannot_override_delivery_scope(self) -> None:
        request = {
            "request_id": "delivery_l3_internal",
            "domain": "delivery",
            "mode": "metric",
            "metric": "delivery_amount",
            "dimensions": [],
            "metric_filters": {"is_inner_cus": "y"},
        }
        normalized = tools._validate_request(request)
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_metric_contract(normalized, self.semantics)
        self.assertEqual("UNSUPPORTED_DIMENSION", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
