"""Focused offline behavior tests for the delivery runtime L3 fixes."""

from __future__ import annotations

import copy
import importlib
import json
import os
from datetime import date
from pathlib import Path
import sys
import types
import unittest


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)

TEST_PACKAGE = "datasage_delivery_l3_runtime_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")


def _request(domain: str, metric: str, **overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "request_id": f"l3_{domain}_{metric}",
        "domain": domain,
        "metric": metric,
        "dimensions": [],
    }
    request.update(overrides)
    return request


def _validated_plan(raw: dict[str, object], semantics: dict[str, object]):
    return tools._validate_metric_contract(tools._validate_request(raw), semantics)


class DeliveryL3RuntimeTests(unittest.TestCase):
    def test_derived_metric_availability_is_checked_per_operand_and_preserves_code(self):
        datasets, semantics = contracts.execution_contracts("delivery")

        composite_semantics = copy.deepcopy(semantics)
        composite_semantics["metrics"]["gross_delivery_amount"]["availability"] = {
            "status": "blocked",
            "error_code": "SYNTHETIC_COMPOSITE_CHILD_BLOCKED",
            "message": "synthetic child gate",
        }
        composite_request = _request("delivery", "delivery_amount")
        with self.assertRaises(tools.QueryFailure) as composite_failure:
            _validated_plan(composite_request, composite_semantics)
        self.assertEqual(
            "SYNTHETIC_COMPOSITE_CHILD_BLOCKED", composite_failure.exception.code
        )

        ratio_semantics = copy.deepcopy(semantics)
        ratio_semantics["metrics"]["return_amount"]["availability"] = {
            "status": "blocked",
            "error_code": "SYNTHETIC_RATIO_CHILD_BLOCKED",
            "message": "synthetic child gate",
        }
        ratio_request = _request("delivery", "return_amount_rate")
        with self.assertRaises(tools.QueryFailure) as ratio_failure:
            _validated_plan(ratio_request, ratio_semantics)
        self.assertEqual("SYNTHETIC_RATIO_CHILD_BLOCKED", ratio_failure.exception.code)

        target_datasets, target_semantics = contracts.execution_contracts("target")
        target_semantics = copy.deepcopy(target_semantics)
        target_semantics["metrics"]["delivery_target_completion"]["availability"] = {
            "status": "blocked",
            "error_code": "SYNTHETIC_ALLOCATED_SOURCE_BLOCKED",
            "message": "synthetic source gate",
        }
        allocated_request = _request(
            "target",
            "allocated_net_delivery_amount",
            attribution_mode="salesperson_allocation",
        )
        with self.assertRaises(tools.QueryFailure) as allocated_failure:
            normalized = tools._validate_request(allocated_request)
            tools._validate_metric_contract(normalized, target_semantics)
            tools._build_metric_query(
                normalized,
                target_datasets,
                target_semantics,
                tools._metric_query_limit(normalized),
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual(
            "SYNTHETIC_ALLOCATED_SOURCE_BLOCKED", allocated_failure.exception.code
        )

    def test_availability_traversal_fails_closed_on_reference_cycle(self):
        _datasets, semantics = contracts.execution_contracts("delivery")
        semantics = copy.deepcopy(semantics)
        semantics["metrics"]["delivery_amount"]["components"][0]["metric"] = (
            "delivery_amount"
        )
        with self.assertRaises(tools.QueryFailure) as failure:
            tools._ensure_metric_tree_available("delivery_amount", semantics)
        self.assertEqual("CONTRACT_UNAVAILABLE", failure.exception.code)

    def test_metric_tree_rejects_malformed_roots_and_child_references(self):
        _datasets, delivery_semantics = contracts.execution_contracts("delivery")
        for root in (None, "", 1, []):
            with self.subTest(root=root), self.assertRaises(tools.QueryFailure) as failure:
                tools._ensure_metric_tree_available(root, delivery_semantics)
            self.assertEqual("CONTRACT_UNAVAILABLE", failure.exception.code)

        malformed_cases = (
            ("components", "delivery_amount", "metric", None),
            ("components", "delivery_amount", "metric", "missing_child"),
            ("ratio", "return_amount_rate", "numerator", None),
            ("ratio", "return_amount_rate", "denominator", "missing_child"),
        )
        for section, metric, field, value in malformed_cases:
            with self.subTest(section=section, metric=metric, field=field, value=value):
                semantics = copy.deepcopy(delivery_semantics)
                if section == "components":
                    semantics["metrics"][metric]["components"][0][field] = value
                else:
                    semantics["metrics"][metric][section][field] = value
                with self.assertRaises(tools.QueryFailure) as failure:
                    tools._ensure_metric_tree_available(metric, semantics)
                self.assertEqual("CONTRACT_UNAVAILABLE", failure.exception.code)

        _target_datasets, target_semantics = contracts.execution_contracts("target")
        target_semantics = copy.deepcopy(target_semantics)
        target_semantics["metrics"]["allocated_net_delivery_amount"][
            "source_completion_metric"
        ] = None
        with self.assertRaises(tools.QueryFailure) as source_failure:
            tools._ensure_metric_tree_available(
                "allocated_net_delivery_amount", target_semantics
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", source_failure.exception.code)

        # The public metric boundary still owns the normal unknown-root error.
        with self.assertRaises(tools.QueryFailure) as public_failure:
            tools._validate_metric_contract(
                tools._validate_request(_request("delivery", "missing_metric")),
                delivery_semantics,
            )
        self.assertEqual("UNSUPPORTED_METRIC", public_failure.exception.code)

        # Valid available trees remain no-ops for the normal published paths.
        tools._ensure_metric_tree_available("delivery_amount", delivery_semantics)
        tools._ensure_metric_tree_available("return_amount_rate", delivery_semantics)

    def test_missing_actual_is_unknown_and_cannot_authorize_target_status_or_gap(self):
        row = {
            "metric_value": None,
            "completion_rate": None,
            "target_amount_rmb": "100",
            "actual_amount_rmb": None,
            "gap_amount_rmb": None,
            "target_data_state": "set",
            "actual_data_state": "missing",
            "period_state": "in_progress",
            tools._INTERNAL_MATCH_COUNT: 1,
        }
        public_rows, data_state = tools._evidence_rows_and_state([row], False)
        self.assertEqual("undefined", data_state)
        self.assertIsNone(public_rows[0]["actual_amount_rmb"])
        self.assertIsNone(public_rows[0]["gap_amount_rmb"])
        self.assertIsNone(public_rows[0]["completion_rate"])

        claims = tools._claim_ledger(
            "missing_actual",
            "metric_target_completion",
            "target completion",
            "比例",
            [],
            {
                "start": "2026-08-01",
                "end": "2026-09-01",
                "source": "explicit",
            },
            "scope",
            "projection",
            False,
            [row],
        )
        self.assertNotIn("target_status", claims[0]["allowed_relations"])
        self.assertFalse(
            tools._target_status_is_coherent(
                {
                    "metric_value": None,
                    "completion_rate": None,
                    "target_amount_rmb": "100",
                    "actual_amount_rmb": None,
                    "gap_amount_rmb": None,
                },
                {
                    "target_data_state": "set",
                    "actual_data_state": "missing",
                    "period_state": "in_progress",
                },
            )
        )
        self.assertIsNone(tools._target_gap_claim_amounts(claims[0]))

        _datasets, semantics = contracts.execution_contracts("target")
        request = _request(
            "target",
            "delivery_target_completion",
            attribution_mode="transaction_detail",
            calendar_month="2026-08",
        )
        normalized = _validated_plan(request, semantics)
        sql, _params, _scope = tools._build_metric_query(
            normalized,
            _datasets,
            semantics,
            tools._metric_query_limit(normalized),
            observed_on=date(2026, 8, 18),
        )
        self.assertGreaterEqual(
            sql.count("OR COALESCE(a.__matched_row_count, 0) = 0"), 3
        )

    def test_target_gap_finalization_rejects_missing_actual_state(self):
        raw_request = _request(
            "target",
            "delivery_target_completion",
            attribution_mode="transaction_detail",
            time_range={"start": "2026-08-01", "end": "2026-09-01"},
            complete_target_gap_decomposition={"dimension": "department"},
        )
        raw_request.pop("dimensions")
        expanded, operation_partitions = tools._expand_complete_target_gap_decompositions(
            [raw_request]
        )
        overall_request, partition_request = expanded

        def result_for(request: dict[str, object], dimensions: list[dict[str, str]]):
            claim = {
                "request_id": request["request_id"],
                "metric_ref": "metric_target_completion",
                "dimensions": dimensions,
                "scope_fingerprint": "scope",
                "projection_fingerprint": f"projection_{request['request_id']}",
                "period": {"start": "2026-08-01", "end": "2026-09-01"},
                "source_truncated": False,
                "allowed_relations": ["target_status"],
                "facts": {
                    "target_amount_rmb": "100",
                    "actual_amount_rmb": "0",
                    "gap_amount_rmb": "100",
                    "completion_rate": "0",
                    "metric_value": "0",
                },
                "states": {
                    "target_data_state": "set",
                    "actual_data_state": "missing",
                    "period_state": "completed",
                },
            }
            tools.evidence.seal_claim(claim)
            return {
                "request_id": request["request_id"],
                "status": "success",
                "data_state": "rows",
                "_snapshot_group_marker": "snapshot",
                "scope_fingerprint": "scope",
                "business_metric_ref": "metric_target_completion",
                "business_metric_unit": "比例",
                "applied_time_range": {
                    "start": "2026-08-01",
                    "end": "2026-09-01",
                },
                "claim_ledger": [claim],
                "row_count": 1,
                "truncated": False,
            }

        results = [
            result_for(overall_request, []),
            result_for(
                partition_request,
                [{"label": "部门", "value": "department-a"}],
            ),
        ]
        contexts = [{"request": overall_request}, {"request": partition_request}]
        tools._finalize_target_gap_decompositions(
            contexts, results, operation_partitions
        )
        reconciliation = results[1]["target_gap_reconciliation"]
        self.assertEqual("not_reconciled", reconciliation["status"])
        self.assertEqual("TARGET_STATE_INCOMPLETE", reconciliation["reason_code"])

    def test_comparison_accepts_selected_dimension_order(self):
        datasets, semantics = contracts.execution_contracts("delivery")
        request = _request(
            "delivery",
            "delivery_amount",
            dimensions=["customer"],
            time_range={"start": "2026-07-01", "end": "2026-08-01"},
            comparison={"kind": "previous_period"},
            order_by={"field": "customer_name", "direction": "asc"},
        )
        normalized = _validated_plan(request, semantics)
        sql, _params, scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            tools._metric_query_limit(normalized),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["customer_id", "customer_name"], scope["dimension_outputs"])
        self.assertIn("ORDER BY `customer_name` ASC LIMIT %s", sql)

    def test_unhashable_domain_is_a_typed_branch_failure(self):
        payload = json.loads(
            tools.datasage_query(
                {
                    "requests": [
                        {
                            "request_id": "bad_domain",
                            "domain": [],
                            "metric": "delivery_amount",
                        }
                    ]
                }
            )
        )
        self.assertEqual("failed", payload["status"])
        self.assertEqual("INVALID_INPUT", payload["results"][0]["error"]["code"])

    def test_matched_elapsed_yoy_preserves_current_duration_across_leap_day(self):
        current, prior, _alignment = tools._year_over_year_matched_elapsed_ranges(
            "2024-02-28", "2024-03-10", date(2024, 3, 1)
        )
        self.assertEqual(
            {"start": "2024-02-28", "end": "2024-03-02"}, current
        )
        self.assertEqual(
            {"start": "2023-02-28", "end": "2023-03-03"}, prior
        )
        annotated = tools._annotate_period_evidence(
            {
                "current": {**current, "source": "explicit"},
                "comparison": {**prior, "source": "explicit"},
                "comparison_alignment": _alignment,
            },
            date(2024, 3, 1),
        )
        self.assertEqual("compatible", annotated["comparison_compatibility"]["status"])

    def test_quantity_requires_one_unit_or_unit_group_and_normalizes_aliases(self):
        datasets, semantics = contracts.execution_contracts("delivery")

        missing_scope = _validated_plan(
            _request("delivery", "delivery_quantity"), semantics
        )
        with self.assertRaises(tools.QueryFailure) as missing_failure:
            tools._build_metric_query(
                missing_scope,
                datasets,
                semantics,
                tools._metric_query_limit(missing_scope),
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("UNIT_SCOPE_REQUIRED", missing_failure.exception.code)

        grouped = _validated_plan(
            _request("delivery", "delivery_quantity", dimensions=["unit"]),
            semantics,
        )
        sql, _params, scope = tools._build_metric_query(
            grouped,
            datasets,
            semantics,
            tools._metric_query_limit(grouped),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["unit"], scope["dimension_outputs"])
        self.assertIn("`unit`", sql)

        for alias, canonical in (("码", "y"), ("PCS", "Pcs"), ("平方", "m2")):
            with self.subTest(alias=alias):
                filtered = _validated_plan(
                    _request(
                        "delivery",
                        "delivery_quantity",
                        metric_filters={"unit": alias},
                    ),
                    semantics,
                )
                filtered = tools._validate_metric_filter_value_contracts(
                    filtered, semantics
                )
                self.assertEqual(canonical, filtered["metric_filters"]["unit"])
                _sql, params, _scope = tools._build_metric_query(
                    filtered,
                    datasets,
                    semantics,
                    tools._metric_query_limit(filtered),
                    observed_on=date(2026, 8, 18),
                )
                self.assertIn(canonical, params)

        multiple = _validated_plan(
            _request(
                "delivery",
                "delivery_quantity",
                metric_filters={"unit": ["m", "y"]},
            ),
            semantics,
        )
        with self.assertRaises(tools.QueryFailure) as multiple_failure:
            tools._build_metric_query(
                multiple,
                datasets,
                semantics,
                tools._metric_query_limit(multiple),
                observed_on=date(2026, 8, 18),
            )
        self.assertEqual("UNIT_SCOPE_REQUIRED", multiple_failure.exception.code)

    def test_final_supplier_is_physical_gross_only(self):
        datasets, semantics = contracts.execution_contracts("delivery")
        gross = _validated_plan(
            _request(
                "delivery",
                "warehouse_gross_delivery_amount",
                dimensions=["final_supplier"],
                delivery_scope="explicit_gross",
            ),
            semantics,
        )
        sql, _params, scope = tools._build_metric_query(
            gross,
            datasets,
            semantics,
            tools._metric_query_limit(gross),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(
            ["final_supplier_id", "final_supplier_no", "final_supplier_name"],
            scope["dimension_outputs"],
        )
        self.assertIn("`final_supplier_id`", sql)

        with self.assertRaises(tools.QueryFailure) as net_failure:
            _validated_plan(
                _request(
                    "delivery",
                    "warehouse_delivery_amount",
                    dimensions=["final_supplier"],
                ),
                semantics,
            )
        self.assertEqual("UNSUPPORTED_DIMENSION", net_failure.exception.code)

    def test_owner_confirmed_organization_and_department_roles_compile(self):
        datasets, semantics = contracts.execution_contracts("delivery")
        request = _validated_plan(
            _request(
                "delivery",
                "delivery_amount",
                dimensions=["department", "business_department"],
            ),
            semantics,
        )
        sql, _params, scope = tools._build_metric_query(
            request,
            datasets,
            semantics,
            tools._metric_query_limit(request),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["customer_dept", "biz_dept"], scope["dimension_outputs"])
        self.assertIn("`customer_dept`", sql)
        self.assertIn("`biz_dept`", sql)

        organization = _validated_plan(
            _request("delivery", "delivery_amount", dimensions=["organization"]),
            semantics,
        )
        org_sql, _params, org_scope = tools._build_metric_query(
            organization,
            datasets,
            semantics,
            tools._metric_query_limit(organization),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["org_name"], org_scope["dimension_outputs"])
        self.assertIn("`biz_org`", org_sql)

        physical_org = _validated_plan(
            _request(
                "delivery", "warehouse_delivery_amount", dimensions=["organization"]
            ),
            semantics,
        )
        _physical_sql, _params, physical_scope = tools._build_metric_query(
            physical_org,
            datasets,
            semantics,
            tools._metric_query_limit(physical_org),
            observed_on=date(2026, 8, 18),
        )
        self.assertEqual(["org_name"], physical_scope["dimension_outputs"])


if __name__ == "__main__":
    unittest.main()
