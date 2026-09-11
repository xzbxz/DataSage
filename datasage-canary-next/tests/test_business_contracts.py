from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import importlib
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

import jsonschema
import yaml

from plugin_registration_probe import probe_registration


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
SKILL_PATH = (
    PROFILE_ROOT / "skills" / "business-analytics" / "datasage" / "SKILL.md"
)
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
TEST_PACKAGE = "datasage_query_contract_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
capability_contract = importlib.import_module(f"{TEST_PACKAGE}.capability_contract")
contract_store = importlib.import_module(f"{TEST_PACKAGE}.contract_store")
entities = importlib.import_module(f"{TEST_PACKAGE}.entities")
schemas = importlib.import_module(f"{TEST_PACKAGE}.schemas")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")
wire = importlib.import_module(f"{TEST_PACKAGE}.wire")
runtime_health = importlib.import_module(f"{TEST_PACKAGE}.runtime_health")
canary_transcript_adapter = importlib.import_module(
    f"{TEST_PACKAGE}.e2e.canary_transcript_adapter"
)


def _main_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _answer_boundary() -> str:
    return (SKILL_PATH.parent / "references" / "answer-boundary.md").read_text(
        encoding="utf-8"
    )


def _query_rules() -> str:
    return (SKILL_PATH.parent / "references" / "query-rules.md").read_text(
        encoding="utf-8"
    )


class BusinessContractTests(unittest.TestCase):
    @staticmethod
    def _read_only_source_evidence() -> dict[str, object]:
        evidence: dict[str, object] = {
            "schema": "datasage-query-source-evidence/v1",
            "identity_sha256": "1" * 64,
            "connection_verified": True,
            "transport_mode": "tls",
            "transport_policy_verified": True,
            "grant_policy": "strict_object_read_only",
            "grants_verified": True,
            "read_only": True,
            "source_commitment_sha256": "2" * 64,
        }
        canonical = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence["security_evidence_sha256"] = hashlib.sha256(
            b"datasage-query-source-evidence/v1\x00" + canonical
        ).hexdigest()
        return evidence

    @staticmethod
    def _scalar_calculation_result(
        request_id: str,
        value: str,
        *,
        period: tuple[str, str],
        filter_scope: dict[str, object] | None = None,
        share_partition_dimensions: tuple[str, ...] = (),
    ) -> dict[str, object]:
        metric_ref = "metric_calculation_fixture"
        scope_fingerprint = f"scope_{request_id}"
        projection_fingerprint = f"projection_{request_id}"
        applied_time_range = {"start": period[0], "end": period[1]}
        claim: dict[str, object] = {
            "request_id": request_id,
            "metric_ref": metric_ref,
            "metric_label": "计算测试指标",
            "dimensions": [],
            "scope_entities": [],
            "period": applied_time_range,
            "scope_fingerprint": scope_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "unit": "人民币元",
            "currency": "CNY",
            "facts": {"metric_value": value},
            "states": {},
            "source_truncated": False,
            "allowed_relations": ["observation"],
        }
        tools.evidence.seal_claim(claim)
        disclosure: dict[str, object] = {
            "disclosure_id": "synthetic.calculation-scope",
            "contract_version": "metric-disclosure/v1",
            "request_id": request_id,
            "metric_ref": metric_ref,
            "scope_fingerprint": scope_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "mode": "required_always",
            "order": 0,
            "text": "synthetic governed calculation scope",
            "applies": True,
        }
        disclosure_canonical = json.dumps(
            disclosure,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        disclosure["disclosure_seal"] = (
            "sha256_" + hashlib.sha256(disclosure_canonical).hexdigest()
        )
        ledger = [disclosure]
        ledger_canonical = json.dumps(
            {
                "contract_version": "metric-disclosure-ledger/v1",
                "request_id": request_id,
                "metric_ref": metric_ref,
                "scope_fingerprint": scope_fingerprint,
                "projection_fingerprint": projection_fingerprint,
                "ledger": ledger,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return {
            "request_id": request_id,
            "status": "success",
            "data_state": "rows",
            "business_metric_ref": metric_ref,
            "business_metric_label": "计算测试指标",
            "scope_fingerprint": scope_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "claim_ledger": [claim],
            "disclosure_contract_version": "metric-disclosure-ledger/v1",
            "disclosure_ledger": ledger,
            "disclosure_ledger_seal": (
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest()
            ),
            "row_count": 1,
            "truncated": False,
            "applied_time_range": applied_time_range,
            "_calculation_scope": {
                "version": "governed-calculation-scope/v1",
                "metric_basis_fingerprint": "basis_calculation_fixture",
                "filter_scope": dict(filter_scope or {}),
                "share_partition_dimensions": list(share_partition_dimensions),
            },
        }

    @staticmethod
    def _comparison_claim(
        current: str,
        comparison: str,
        delta: str,
        *,
        dimensions: list[dict[str, str]],
    ) -> dict[str, object]:
        return {
            "dimensions": dimensions,
            "source_truncated": False,
            "allowed_relations": ["period_comparison"],
            "facts": {
                "metric_value": current,
                "comparison_value": comparison,
                "delta_value": delta,
            },
            "states": {"comparison_state": "complete"},
        }

    @classmethod
    def _run_synthetic_change_pipeline(
        cls,
        *,
        overall_triplet: tuple[str, str, str],
        partition_triplets: tuple[tuple[str, str, str], ...],
        mismatch: str | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        capability = {
            "mode": "additive_partition",
            "dimensions": ["synthetic_partition"],
        }
        contexts = [
            {
                "request": {"request_id": "overall"},
                "dimensions": [],
                "capability": capability,
            },
            {
                "request": {
                    "request_id": "partition",
                    "decomposition_of_request_id": "overall",
                },
                "dimensions": ["synthetic_partition"],
                "capability": capability,
            },
        ]
        shared_result = {
            "status": "success",
            "data_state": "complete",
            "_snapshot_group_marker": "synthetic-snapshot",
            "scope_fingerprint": "synthetic-scope",
            "business_metric_ref": "synthetic-metric",
            "business_metric_unit": "synthetic-unit",
            "applied_time_range": {
                "start": "synthetic-start",
                "end": "synthetic-end",
            },
            "truncated": False,
        }
        results: list[dict[str, object]] = [
            {
                **shared_result,
                "request_id": "overall",
                "projection_fingerprint": "synthetic-overall-projection",
                "row_count": 1,
                "claim_ledger": [
                    cls._comparison_claim(
                        *overall_triplet,
                        dimensions=[],
                    )
                ],
            },
            {
                **shared_result,
                "request_id": "partition",
                "projection_fingerprint": "synthetic-partition-projection",
                "row_count": len(partition_triplets),
                "claim_ledger": [
                    cls._comparison_claim(
                        *triplet,
                        dimensions=[
                            {
                                "code": "synthetic_partition",
                                "value": f"partition-{index}",
                            }
                        ],
                    )
                    for index, triplet in enumerate(partition_triplets)
                ],
            },
        ]
        if mismatch == "snapshot":
            results[1]["_snapshot_group_marker"] = "different-snapshot"
        elif mismatch == "scope":
            results[1]["scope_fingerprint"] = "different-scope"
        elif mismatch == "period":
            results[1]["applied_time_range"] = {
                "start": "different-start",
                "end": "different-end",
            }
        elif mismatch is not None:
            raise AssertionError(mismatch)
        operation_partitions = {"partition": "overall"}
        tools._authorize_change_decompositions(contexts, results)
        tools._tag_complete_decomposition_reconciliations(
            results,
            operation_partitions,
        )
        for result in results:
            for claim in result["claim_ledger"]:
                claim.update(
                    {
                        "request_id": result["request_id"],
                        "metric_ref": result["business_metric_ref"],
                        "scope_fingerprint": result["scope_fingerprint"],
                        "projection_fingerprint": result[
                            "projection_fingerprint"
                        ],
                        "period": dict(result["applied_time_range"]),
                    }
                )
        tools._seal_claim_ids(results)
        tools._seal_change_reconciliations(results)
        tools._finalize_complete_decomposition_outcomes(
            results,
            operation_partitions,
        )
        for result in results:
            disclosure = {
                "disclosure_id": "synthetic.scope",
                "contract_version": "metric-disclosure/v1",
                "request_id": result["request_id"],
                "metric_ref": result["business_metric_ref"],
                "scope_fingerprint": result["scope_fingerprint"],
                "projection_fingerprint": result["projection_fingerprint"],
                "mode": "required_always",
                "order": 0,
                "text": "synthetic governed scope",
                "applies": True,
            }
            item_canonical = json.dumps(
                disclosure,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            disclosure["disclosure_seal"] = (
                "sha256_" + hashlib.sha256(item_canonical).hexdigest()
            )
            result["disclosure_contract_version"] = (
                "metric-disclosure-ledger/v1"
            )
            result["disclosure_ledger"] = [disclosure]
            ledger_canonical = json.dumps(
                {
                    "contract_version": result[
                        "disclosure_contract_version"
                    ],
                    "request_id": result["request_id"],
                    "metric_ref": result["business_metric_ref"],
                    "scope_fingerprint": result["scope_fingerprint"],
                    "projection_fingerprint": result[
                        "projection_fingerprint"
                    ],
                    "ledger": result["disclosure_ledger"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            result["disclosure_ledger_seal"] = (
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest()
            )
        model_wire = [tools._model_wire_result(result) for result in results]
        return results, model_wire

    @classmethod
    def _run_snapshot_change_operation(
        cls,
        *,
        partition_truncated: bool = False,
        partition_population_size: int | None = None,
        inconsistent_partition_snapshot: bool = False,
        coverage_case: str = "complete",
    ) -> tuple[
        dict[str, object],
        dict[str, dict[str, object]],
        list[tuple[str, tuple[object, ...], int]],
    ]:
        source_evidence = cls._read_only_source_evidence()
        sql_calls: list[tuple[str, tuple[object, ...], int]] = []
        captured_raw: dict[str, dict[str, object]] = {}
        dimension_rows = tuple(
            {"whse_id": f"w{index}", "whse_name": f"W{index}"}
            for index in range(1, max(2, partition_population_size or 0) + 1)
        )

        class Snapshot:
            marker = "offline-snapshot-group"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, _params, _limit, **_kwargs):
                sql_calls.append((sql, tuple(_params), _limit))
                partition = tools._INTERNAL_PARTITION_ROW_COUNT in sql
                full_known = (
                    partition_population_size
                    if partition_population_size is not None
                    else 3 if partition_truncated else 2
                )
                base = {
                    tools._INTERNAL_MATCH_COUNT: full_known,
                    tools._INTERNAL_SNAPSHOT_MONTH: "2026-07",
                    tools._INTERNAL_COMPARISON_SNAPSHOT_MONTH: "2026-06",
                    "current_missing_value_count": 0,
                    "current_known_value_count": full_known,
                    "comparison_missing_value_count": 0,
                    "comparison_known_value_count": full_known,
                    "current_metric_data_state": "complete",
                    "comparison_metric_data_state": "complete",
                }
                if coverage_case == "missing":
                    base.update(
                        current_missing_value_count=full_known,
                        current_known_value_count=0,
                        current_metric_data_state="missing",
                    )
                elif coverage_case == "incomplete":
                    base.update(
                        current_missing_value_count=1,
                        current_known_value_count=full_known - 1,
                        current_metric_data_state="incomplete",
                    )
                elif coverage_case == "invalid":
                    base["current_known_value_count"] = "1.5"
                if not partition:
                    return [
                        {
                            **base,
                            "metric_value": "100",
                            "comparison_value": "80",
                            "delta_value": "20",
                            "change_rate": "0.25",
                        }
                    ], False, source_evidence
                if partition_population_size is not None:
                    all_triplets = (
                        (("100", "80", "20"),)
                        + (("0", "0", "0"),) * (partition_population_size - 1)
                    )
                    triplets = all_triplets[:_limit]
                    result_truncated = len(all_triplets) > _limit
                else:
                    triplets = (
                        (("40", "30", "10"), ("30", "25", "5"))
                        if partition_truncated
                        else (("60", "50", "10"), ("40", "30", "10"))
                    )
                    result_truncated = partition_truncated
                rows = []
                for index, (current, comparison, delta) in enumerate(triplets):
                    row = {
                        **base,
                        **dimension_rows[index],
                        "metric_value": current,
                        "comparison_value": comparison,
                        "delta_value": delta,
                        "change_rate": "0",
                    }
                    if coverage_case == "complete":
                        row.update(
                            current_known_value_count=1,
                            comparison_known_value_count=1,
                        )
                    elif coverage_case == "mismatch":
                        row.update(
                            current_known_value_count=2 if index == 0 else 1,
                            comparison_known_value_count=1,
                        )
                    if result_truncated:
                        row.update(
                            {
                                tools._INTERNAL_PARTITION_CURRENT: "100",
                                tools._INTERNAL_PARTITION_COMPARISON: "80",
                                tools._INTERNAL_PARTITION_DELTA: "20",
                                tools._INTERNAL_PARTITION_ROW_COUNT: full_known,
                            }
                        )
                        if coverage_case != "proof_missing":
                            row.update(
                                {
                                    tools._INTERNAL_PARTITION_CURRENT_MISSING: 0,
                                    tools._INTERNAL_PARTITION_CURRENT_KNOWN: full_known,
                                    tools._INTERNAL_PARTITION_COMPARISON_MISSING: 0,
                                    tools._INTERNAL_PARTITION_COMPARISON_KNOWN: full_known,
                                }
                            )
                    rows.append(row)
                if inconsistent_partition_snapshot:
                    rows[1][tools._INTERNAL_SNAPSHOT_MONTH] = "2026-08"
                return rows, result_truncated, source_evidence

        original_projection = tools._model_wire_result

        def capture_projection(result, **projection_kwargs):
            captured_raw.setdefault(str(result.get("request_id")), result)
            return original_projection(result, **projection_kwargs)

        request = {
            "request_id": "inventory_snapshot_partition",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "offline snapshot decomposition proof",
            "metric": "month_end_inventory_cost_rmb",
            "comparison": {"kind": "snapshot_months_before", "months": 1},
            "complete_change_decomposition": {"dimension": "warehouse"},
        }
        with (
            mock.patch.object(
                tools,
                "_consistent_snapshot_executor",
                side_effect=lambda **_kwargs: Snapshot(),
            ),
            mock.patch.object(
                tools,
                "_model_wire_result",
                side_effect=capture_projection,
            ),
        ):
            payload = json.loads(tools.datasage_query({"requests": [request]}))
        return payload, captured_raw, sql_calls

    @classmethod
    def _synthetic_target_gap_inputs(
        cls,
        *,
        target_data_state: str = "set",
        period_state: str = "current",
        partition_truncated: bool = False,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str]]:
        raw_request = {
            "request_id": "target_gap_partition",
            "domain": "target",
            "mode": "metric",
            "purpose": "offline target gap reconciliation proof",
            "metric": "delivery_target_completion",
            "attribution_mode": "transaction_detail",
            "time_range": {"start": "2026-08-01", "end": "2026-09-01"},
            "complete_target_gap_decomposition": {"dimension": "department"},
        }
        expanded, operation_partitions = (
            tools._expand_complete_target_gap_decompositions([raw_request])
        )
        overall_request, partition_request = expanded
        tools._validate_target_gap_decomposition_capability(
            {
                "request": overall_request,
                "semantics": tools._read_yaml(
                    "plugins/datasage-query/contracts/target-semantics.yaml"
                ),
            },
            "department",
        )
        contexts = [{"request": request} for request in expanded]
        if target_data_state == "set":
            amount_rows = (
                (("300", "240", "60", "0.8"),),
                (("100", "90", "10", "0.9"), ("200", "150", "50", "0.75")),
            )
        elif target_data_state == "missing":
            amount_rows = ((("0", "0", "0", None),), (("0", "0", "0", None),))
        elif target_data_state == "incomplete":
            amount_rows = (
                (("300", "240", None, None),),
                (("300", "240", None, None),),
            )
        else:
            amount_rows = (
                (("300", None, None, None),),
                (("300", None, None, None),),
            )
        shared = {
            "status": "success",
            "data_state": "complete",
            "_snapshot_group_marker": "target-gap-snapshot",
            "scope_fingerprint": "target-gap-scope",
            "business_metric_ref": "delivery_target_completion",
            "business_metric_unit": "比例",
            "applied_time_range": {"start": "2026-08-01", "end": "2026-09-01"},
        }
        results: list[dict[str, object]] = []
        for index, (request, rows) in enumerate(zip(expanded, amount_rows)):
            claims: list[dict[str, object]] = []
            for row_index, (target, actual, gap, completion) in enumerate(rows):
                claim = {
                    "request_id": request["request_id"],
                    "metric_ref": "delivery_target_completion",
                    "dimensions": (
                        []
                        if index == 0
                        else [{"code": "department", "value": f"dept-{row_index}"}]
                    ),
                    "scope_fingerprint": "target-gap-scope",
                    "projection_fingerprint": f"target-gap-projection-{index}",
                    "period": dict(shared["applied_time_range"]),
                    "source_truncated": False,
                    "allowed_relations": ["target_status"],
                    "facts": {
                        "target_amount_rmb": target,
                        "actual_amount_rmb": actual,
                        "gap_amount_rmb": gap,
                        "completion_rate": completion,
                        "metric_value": completion,
                    },
                    "states": {
                        "target_data_state": target_data_state,
                        "period_state": period_state,
                        "actual_data_state": "set",
                    },
                }
                tools.evidence.seal_claim(claim)
                claims.append(claim)
            results.append(
                {
                    **shared,
                    "request_id": request["request_id"],
                    "projection_fingerprint": f"target-gap-projection-{index}",
                    "claim_ledger": claims,
                    "row_count": len(claims),
                    "truncated": partition_truncated if index == 1 else False,
                    "data_state": (
                        "truncated" if index == 1 and partition_truncated else "complete"
                    ),
                }
            )
        return contexts, results, operation_partitions

    def test_datasage_skill_coexists_with_reviewed_native_skills(self) -> None:
        config = yaml.safe_load((PROFILE_ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual("off", config["tools"]["tool_search"]["enabled"])
        content = _main_skill()
        self.assertIn("requires_toolsets: [datasage-query]", content)
        self.assertIn(
            "requires_tools: [datasage_catalog, datasage_entity_resolve, datasage_query]",
            content,
        )
        self.assertFalse((PROFILE_ROOT / ".no-bundled-skills").exists())
        self.assertTrue(config["skills"]["disabled"])
        self.assertFalse(
            (
                PROFILE_ROOT
                / "skills"
                / "datasage"
                / "datasage-query-patterns"
            ).exists()
        )

    def test_user_visible_skills_declare_the_official_tool_search_bridge(self) -> None:
        main_skill = _main_skill()
        request_policy = _query_rules()
        for bridge_name in ("`tool_search`", "`tool_describe`", "`tool_call`"):
            self.assertNotIn(bridge_name, main_skill)
        for direct_tool in (
            "`datasage_catalog`",
            "`datasage_query`",
            "`datasage_entity_resolve`",
        ):
            self.assertIn(direct_tool, main_skill + request_policy)
        self.assertNotIn("`datasage_reference`", main_skill)

    def test_main_skill_top_n_disclosure_depends_on_returned_state(self) -> None:
        content = _answer_boundary()
        normalized = " ".join(content.split())
        self.assertIn("A Top-N result describes only the returned ranking", normalized)
        for field in ("`requested_limit`", "`effective_limit`", "`has_more`"):
            self.assertIn(field, normalized)
        self.assertEqual(100, schemas.REQUEST["properties"]["limit"]["maximum"])
        with mock.patch.object(tools, "_bounded_int", return_value=100):
            self.assertEqual(
                50,
                tools._metric_query_limit(
                    {
                        "limit": 50,
                        "order_by": {"field": "metric_value", "direction": "desc"},
                    }
                ),
            )

    def test_skill_hardens_truncated_top_n_and_multirow_arithmetic(self) -> None:
        skill = " ".join(_main_skill().split())
        boundary = " ".join(_answer_boundary().split())

        self.assertIn("do not turn an unreturned tail into a driver", skill)
        for required in (
            "never attribute the total change to the unreturned tail",
            "Never infer geography, category, ownership",
            "does not establish lifecycle-new status",
            "Do not claim concentration or dispersion from a Top-1 value",
            "use an available calculation tool",
            "state the exact operands and formula",
            "label the result as a derived observation",
        ):
            self.assertIn(required, boundary)

    def test_complete_change_finalization_reports_noncausal_structural_contribution(
        self,
    ) -> None:
        answer_policy = _answer_boundary()
        self.assertIn(
            "Structural contribution requires an explicitly reconciled decomposition",
            answer_policy,
        )
        self.assertIn(
            "Correlation and decomposition alone never authorize causality",
            answer_policy,
        )
        semantics = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "delivery-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        answer_contract = semantics["metrics"]["delivery_amount"]["answer_contract"]
        self.assertTrue(any("不代表业务原因或驱动" in item for item in answer_contract))
        detail = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]}
            )
        )
        self.assertEqual("success", detail["status"])

    def test_model_visible_contribution_rate_wire_contract_is_direct_use_only(
        self,
    ) -> None:
        payload = json.loads(
            contracts.datasage_catalog({"requests": [{"domain": "delivery"}]})
        )
        self.assertEqual("success", payload["status"])
        serialized_catalog = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("analysis_affordances", serialized_catalog)
        self.assertNotIn("reasoning_topics", serialized_catalog)

        results, model_wire = self._run_synthetic_change_pipeline(
            overall_triplet=("65.0000", "60", "5.0000"),
            partition_triplets=(
                ("11", "10", "1"),
                ("8", "10", "-2"),
                ("16", "10", "6"),
                ("10", "10", "0"),
                ("10.0001", "10", "0.0001"),
                ("9.9999", "10", "-0.0001"),
            ),
        )
        partition = results[1]
        reconciliation = partition["change_reconciliation"]
        self.assertEqual("reconciled", reconciliation["status"])
        self.assertEqual(
            "complete_change_decomposition",
            reconciliation["operation"],
        )
        self.assertTrue(tools.evidence._reconciliation_is_valid(partition))
        claims = partition["claim_ledger"]
        self.assertTrue(
            all(tools.evidence._claim_is_validly_sealed(claim) for claim in claims)
        )
        returned_rates = [
            claim["facts"].get("net_change_contribution_rate")
            for claim in claims
        ]
        self.assertEqual(
            ["0.2", "-0.4", "1.2", None, "0.00002", "-0.00002"],
            returned_rates,
        )
        self.assertTrue(
            all(
                isinstance(rate, str)
                for rate in returned_rates
                if rate is not None
            )
        )
        self.assertGreater(Decimal(returned_rates[0]), 0)
        self.assertLess(Decimal(returned_rates[1]), 0)
        self.assertGreater(abs(Decimal(returned_rates[2])), 1)
        self.assertGreater(Decimal(returned_rates[4]), 0)
        self.assertLess(Decimal(returned_rates[5]), 0)
        zero_partition = claims[3]
        self.assertEqual("0", zero_partition["facts"]["delta_value"])
        self.assertNotIn(
            "structural_contribution",
            zero_partition["allowed_relations"],
        )
        self.assertNotIn(
            "net_change_contribution_rate",
            zero_partition["facts"],
        )
        wire_claims = model_wire[1]["claim_ledger"]
        self.assertEqual(returned_rates, [
            claim["facts"].get("net_change_contribution_rate")
            for claim in wire_claims
        ])
        self.assertTrue(
            all("delta_value" in claim["facts"] for claim in wire_claims)
        )

    def test_zero_overall_success_fails_closed_without_contribution_rates(self) -> None:
        results, model_wire = self._run_synthetic_change_pipeline(
            overall_triplet=("20", "20", "0"),
            partition_triplets=(
                ("10", "10", "0"),
                ("10", "10", "0"),
            ),
        )
        self.assertTrue(all(result["status"] == "success" for result in results))
        partition = results[1]
        reconciliation = partition["change_reconciliation"]
        self.assertEqual("not_reconciled", reconciliation["status"])
        self.assertEqual(
            "complete_change_decomposition",
            reconciliation["operation"],
        )
        self.assertEqual("OVERALL_CHANGE_ZERO", reconciliation["reason_code"])
        for claim in partition["claim_ledger"]:
            self.assertTrue(tools.evidence._claim_is_validly_sealed(claim))
            self.assertNotIn(
                "structural_contribution",
                claim["allowed_relations"],
            )
            self.assertNotIn(
                "net_change_contribution_rate",
                claim["facts"],
            )
        wire_reconciliation = model_wire[1]["change_reconciliation"]
        self.assertEqual("not_reconciled", wire_reconciliation["status"])
        self.assertEqual(
            "complete_change_decomposition",
            wire_reconciliation["operation"],
        )

    def test_catalog_comparison_capabilities_match_the_runtime(self) -> None:
        cases = (
            (
                "delivery",
                "delivery_amount",
                ["previous_period", "year_over_year"],
            ),
            (
                "inventory",
                "month_end_inventory_cost_rmb",
                ["snapshot_months_before"],
            ),
            ("inventory", "current_inventory_amount_rmb", []),
            ("receivable", "open_receivable_amount", []),
            ("target", "delivery_target_completion", []),
        )
        for domain, metric, expected in cases:
            with self.subTest(domain=domain, metric=metric):
                detail = json.loads(
                    contracts.datasage_catalog(
                        {"requests": [{"domain": domain, "metric": metric}]}
                    )
                )["results"][0]
                self.assertEqual(expected, detail["metric"]["comparison_kinds"])
                self.assertIs(
                    detail["metric"]["supports_generic_comparison"],
                    bool(expected),
                )

        index = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "inventory", "view": "expert_index"}]}
            )
        )["results"][0]
        by_code = {item["code"]: item for item in index["metrics"]}
        self.assertEqual(
            ["snapshot_months_before"],
            by_code["month_end_inventory_cost_rmb"]["comparison_kinds"],
        )
        self.assertEqual(
            [], by_code["current_inventory_amount_rmb"]["comparison_kinds"]
        )

        unsupported = {
            "request_id": "current_snapshot_previous_period",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "offline exact comparison capability proof",
            "metric": "current_inventory_amount_rmb",
            "dimensions": [],
            "inventory_scope": "total",
            "time_range": {"start": "2026-06-01", "end": "2026-07-01"},
            "comparison": {"kind": "previous_period"},
        }
        normalized = tools._validate_request(unsupported)
        datasets, semantics = tools._contracts("inventory")
        with self.assertRaises(tools.QueryFailure) as failure:
            tools._validate_pre_entity_metric_plan(
                normalized, datasets, semantics
            )
        self.assertEqual("INVALID_PLAN", failure.exception.code)

    def test_year_over_year_matched_elapsed_is_typed_aligned_and_wire_authorized(
        self,
    ) -> None:
        observed_on = date(2026, 8, 25)
        request = {
            "request_id": "matched_elapsed_yoy",
            "domain": "delivery",
            "metric": "delivery_amount",
            "dimensions": [],
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
            "comparison": {
                "kind": "year_over_year",
                "coverage": "matched_elapsed",
            },
        }
        jsonschema.validate(
            {"requests": [request]},
            schemas.DATASAGE_QUERY["parameters"],
        )
        normalized = tools._validate_request(request)
        datasets, semantics = tools._contracts("delivery")
        _sql, params, scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            tools._metric_query_limit(normalized),
            observed_on=observed_on,
        )
        for boundary in (
            "2026-01-01",
            "2026-08-26",
            "2025-01-01",
            "2025-08-26",
        ):
            self.assertIn(boundary, params)
        self.assertNotIn("2026-09-01", params)
        self.assertEqual(
            {"start": "2026-01-01", "end": "2026-08-26", "source": "explicit"},
            scope["time_range"]["current"],
        )
        self.assertEqual(
            {"start": "2025-01-01", "end": "2025-08-26", "source": "explicit"},
            scope["time_range"]["comparison"],
        )
        alignment = scope["time_range"]["comparison_alignment"]
        self.assertEqual("matched_elapsed", alignment["coverage"])
        self.assertEqual("2026-09-01", alignment["requested_current_end"])
        self.assertEqual("2026-08-26", alignment["effective_current_end"])
        self.assertIs(alignment["current_was_clipped"], True)

        public_period = tools._public_time_range(scope["time_range"])
        annotated = tools._annotate_period_evidence(public_period, observed_on)
        self.assertEqual(
            "compatible",
            annotated["comparison_compatibility"]["status"],
        )
        self.assertEqual(
            "in_progress",
            annotated["current"]["calendar_evidence"]["period_state"],
        )
        self.assertEqual(
            "not_proven",
            annotated["current"]["calendar_evidence"]["source_freshness"],
        )

        claims = tools._claim_ledger(
            "matched_elapsed_yoy",
            "metric_calculation_fixture",
            "计算测试指标",
            "人民币元",
            [],
            annotated,
            "scope_matched_elapsed_yoy",
            "projection_matched_elapsed_yoy",
            False,
            [
                {
                    "metric_value": "120",
                    "comparison_value": "100",
                    "delta_value": "20",
                    "change_rate": "0.2",
                }
            ],
        )
        self.assertIn("period_comparison", claims[0]["allowed_relations"])
        tools.evidence.seal_claim(claims[0])
        result = self._scalar_calculation_result(
            "matched_elapsed_yoy",
            "120",
            period=("2026-01-01", "2026-08-26"),
        )
        result["applied_time_range"] = annotated
        result["claim_ledger"] = claims
        projected = tools._model_wire_result(result)
        self.assertEqual("20", projected["claim_ledger"][0]["facts"]["delta_value"])
        self.assertEqual("0.2", projected["claim_ledger"][0]["facts"]["change_rate"])

    def test_run_one_preserves_matched_elapsed_alignment_and_compatibility(
        self,
    ) -> None:
        observed_on = date(2026, 8, 28)
        raw_request = {
            "request_id": "yoy_result_validation_regression",
            "domain": "delivery",
            "metric": "delivery_amount",
            "dimensions": [],
            "time_range": {"start": "2026-07-01", "end": "2026-08-01"},
            "comparison": {
                "kind": "year_over_year",
                "coverage": "matched_elapsed",
            },
        }
        request, datasets, semantics = tools._validate_request_plan_without_entities(
            raw_request,
            observed_on=observed_on,
        )
        prepared = {
            "request": request,
            "datasets": datasets,
            "semantics": semantics,
            "resolved_entities": [],
            "entity_resolution_db_call_count": 0,
        }
        sql_calls: list[tuple[str, list[object], int]] = []

        def execute_query(sql, params, limit, **_kwargs):
            sql_calls.append((sql, list(params), limit))
            return (
                [
                    {
                        "metric_value": "120.00",
                        "comparison_value": "100.00",
                        "delta_value": "20.00",
                        "change_rate": "0.2",
                        tools._INTERNAL_MATCH_COUNT: 2,
                    }
                ],
                False,
                self._read_only_source_evidence(),
            )

        result = tools._run_one(
            raw_request,
            prepared=prepared,
            execute_query=execute_query,
            period_observed_on=observed_on,
        )

        self.assertEqual("success", result["status"], result)
        self.assertEqual(1, result["business_sql_attempted_count"])
        self.assertEqual(1, result["business_sql_confirmed_count"])
        self.assertEqual(1, len(sql_calls))
        self.assertTrue(sql_calls[0][0])
        self.assertEqual(
            {
                "version": "matched-elapsed-comparison/v1",
                "kind": "year_over_year",
                "coverage": "matched_elapsed",
                "observed_on": "2026-08-28",
                "requested_current_start": "2026-07-01",
                "requested_current_end": "2026-08-01",
                "effective_current_end": "2026-08-01",
                "current_was_clipped": False,
            },
            result["applied_time_range"]["comparison_alignment"],
        )
        self.assertEqual(
            {"status": "compatible", "reason_codes": []},
            result["applied_time_range"]["comparison_compatibility"],
        )

    def test_year_over_year_requires_matched_elapsed_contract(self) -> None:
        base = {
            "request_id": "invalid_yoy_contract",
            "domain": "delivery",
            "metric": "delivery_amount",
            "dimensions": [],
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        invalid = {
            **base,
            "comparison": {"kind": "year_over_year"},
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"requests": [invalid]},
                schemas.DATASAGE_QUERY["parameters"],
            )
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_request(invalid)
        self.assertEqual("INVALID_INPUT", caught.exception.code)

        calendar_month = {
            key: value for key, value in base.items() if key != "time_range"
        }
        calendar_month.update(
            {
                "request_id": "calendar_month_yoy_contract",
                "calendar_month": "2026-08",
                "comparison": {
                    "kind": "year_over_year",
                    "coverage": "matched_elapsed",
                },
            }
        )
        jsonschema.validate(
            {"requests": [calendar_month]},
            schemas.DATASAGE_QUERY["parameters"],
        )
        normalized = tools._validate_request(calendar_month)
        self.assertEqual(
            {"start": "2026-08-01", "end": "2026-09-01"},
            normalized["time_range"],
        )

    def test_year_over_year_boundaries_are_frozen_completed_and_leap_safe(
        self,
    ) -> None:
        completed_current, completed_prior, completed_alignment = (
            tools._year_over_year_matched_elapsed_ranges(
                "2025-01-01",
                "2025-09-01",
                date(2026, 8, 25),
            )
        )
        self.assertEqual(
            {"start": "2025-01-01", "end": "2025-09-01"},
            completed_current,
        )
        self.assertEqual(
            {"start": "2024-01-01", "end": "2024-09-01"},
            completed_prior,
        )
        self.assertIs(completed_alignment["current_was_clipped"], False)

        leap_current, leap_prior, leap_alignment = (
            tools._year_over_year_matched_elapsed_ranges(
                "2024-02-29",
                "2024-03-10",
                date(2024, 3, 1),
            )
        )
        self.assertEqual(
            {"start": "2024-02-29", "end": "2024-03-02"},
            leap_current,
        )
        self.assertEqual(
            {"start": "2023-02-28", "end": "2023-03-02"},
            leap_prior,
        )
        leap_period = {
            "current": {**leap_current, "source": "explicit"},
            "comparison": {**leap_prior, "source": "explicit"},
            "comparison_alignment": leap_alignment,
        }
        annotated = tools._annotate_period_evidence(
            leap_period,
            date(2024, 3, 1),
        )
        self.assertEqual(
            "compatible",
            annotated["comparison_compatibility"]["status"],
        )

        tampered = copy.deepcopy(annotated)
        tampered["comparison_alignment"]["effective_current_end"] = "2024-03-03"
        self.assertEqual(
            "not_assessable",
            tools.assess_period_compatibility(
                tampered["current"],
                tampered["comparison"],
                comparison_alignment=tampered["comparison_alignment"],
            )["status"],
        )
        with self.assertRaises(tools.QueryFailure) as not_started:
            tools._year_over_year_matched_elapsed_ranges(
                "2026-09-01",
                "2026-10-01",
                date(2026, 8, 25),
            )
        self.assertEqual("INVALID_PLAN", not_started.exception.code)

    def test_complete_change_decomposition_reaches_matched_elapsed_yoy(self) -> None:
        request = {
            "request_id": "matched_elapsed_yoy_partition",
            "domain": "delivery",
            "metric": "delivery_amount",
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
            "comparison": {
                "kind": "year_over_year",
                "coverage": "matched_elapsed",
            },
            "complete_change_decomposition": {"dimension": "customer"},
        }
        jsonschema.validate(
            {"requests": [request]},
            schemas.DATASAGE_QUERY["parameters"],
        )
        expanded, partitions = tools._expand_complete_change_decompositions(
            [request]
        )
        self.assertEqual(2, len(expanded))
        self.assertEqual(
            {"matched_elapsed_yoy_partition"},
            set(partitions),
        )
        datasets, semantics = tools._contracts("delivery")
        scopes = []
        for branch in expanded:
            normalized = tools._validate_request(branch)
            _sql, params, scope = tools._build_metric_query(
                normalized,
                datasets,
                semantics,
                tools._metric_query_limit(normalized),
                observed_on=date(2026, 8, 25),
            )
            self.assertIn("2026-08-26", params)
            self.assertIn("2025-08-26", params)
            annotated = tools._annotate_period_evidence(
                tools._public_time_range(scope["time_range"]),
                date(2026, 8, 25),
            )
            self.assertEqual(
                "compatible",
                annotated["comparison_compatibility"]["status"],
            )
            scopes.append(annotated)
        self.assertEqual(scopes[0], scopes[1])

    def test_catalog_metric_and_view_are_mechanically_exclusive(self) -> None:
        invalid = {
            "requests": [
                {
                    "domain": "delivery",
                    "metric": "delivery_amount",
                    "view": "expert_index",
                }
            ]
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                invalid,
                schemas.DATASAGE_CATALOG["parameters"],
            )
        runtime = json.loads(contracts.datasage_catalog(invalid))
        self.assertEqual("failed", runtime["status"])
        self.assertEqual("INVALID_INPUT", runtime["error"]["code"])

    def test_catalog_scorecard_mixed_with_other_branch_fails_atomically(self) -> None:
        scorecard = {"view": "performance_scorecard"}
        receivable = {"domain": "receivable", "view": "expert_index"}
        for requests in ([scorecard, receivable], [receivable, scorecard]):
            with self.subTest(requests=requests):
                with (
                    mock.patch.object(
                        contracts,
                        "_catalog_performance_scorecard",
                        side_effect=AssertionError("scorecard branch must not execute"),
                    ) as scorecard_handler,
                    mock.patch.object(
                        contracts,
                        "_domain_contract",
                        side_effect=AssertionError("domain branch must not execute"),
                    ) as domain_handler,
                ):
                    runtime = json.loads(
                        contracts.datasage_catalog({"requests": requests})
                    )
                scorecard_handler.assert_not_called()
                domain_handler.assert_not_called()
                self.assertEqual("failed", runtime["status"])
                self.assertEqual("INVALID_INPUT", runtime["error"]["code"])
                self.assertNotIn("results", runtime)
                self.assertNotIn("failures", runtime)
                self.assertNotIn("failed_request_count", runtime)
                self.assertNotIn("REDUNDANT", json.dumps(runtime, ensure_ascii=False))

    def test_catalog_scorecard_alone_preserves_success_behavior(self) -> None:
        runtime = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"view": "performance_scorecard"}]}
            )
        )
        self.assertEqual("success", runtime["status"])
        self.assertEqual(1, len(runtime["results"]))
        self.assertEqual("performance_scorecard", runtime["results"][0]["level"])
        self.assertNotIn("failures", runtime)

    def test_catalog_ordinary_multi_domain_preserves_success_behavior(self) -> None:
        runtime = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {"domain": "receivable", "view": "expert_index"},
                        {"domain": "delivery", "view": "expert_index"},
                    ]
                }
            )
        )
        self.assertEqual("success", runtime["status"])
        self.assertEqual(2, len(runtime["results"]))
        self.assertEqual(
            ["receivable", "delivery"],
            [result["domain"] for result in runtime["results"]],
        )
        self.assertNotIn("failures", runtime)

    def test_ratio_comparison_preserves_undefined_values_as_null(self) -> None:
        request = {
            "request_id": "ratio_null_comparison",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "offline ratio null compilation proof",
            "metric": "return_amount_rate",
            "dimensions": [],
            "calendar_month": "2026-07",
            "comparison": {"kind": "previous_period"},
        }
        normalized = tools._validate_request(request)
        datasets, semantics = tools._contracts("delivery")
        tools._validate_pre_entity_metric_plan(normalized, datasets, semantics)
        sql, _params, _scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            tools._metric_query_limit(normalized),
        )
        self.assertIn("c.metric_value AS metric_value", sql)
        self.assertIn("p.metric_value AS comparison_value", sql)
        self.assertIn(
            "CASE WHEN c.metric_value IS NOT NULL AND p.metric_value IS NOT NULL",
            sql,
        )
        self.assertNotIn(
            "COALESCE(c.metric_value, 0) AS metric_value", sql
        )
        self.assertNotIn(
            "COALESCE(p.metric_value, 0) AS comparison_value", sql
        )

    def test_future_target_keeps_a_published_target_state(self) -> None:
        request = {
            "request_id": "future_target_state",
            "domain": "target",
            "mode": "metric",
            "purpose": "offline future target compilation proof",
            "metric": "delivery_target_completion",
            "dimensions": [],
            "attribution_mode": "transaction_detail",
            "calendar_month": "2099-01",
        }
        normalized = tools._validate_request(request)
        datasets, semantics = tools._contracts("target")
        tools._validate_pre_entity_metric_plan(normalized, datasets, semantics)
        sql, _params, _scope = tools._build_metric_query(
            normalized,
            datasets,
            semantics,
            tools._metric_query_limit(normalized),
        )
        self.assertRegex(
            sql,
            r"CASE WHEN .* = 0 AND 'not_started' IN \('not_started', 'includes_future'\) THEN 'not_set_for_future' WHEN .* = 0 THEN 'missing'",
        )
        self.assertTrue(
            tools._target_status_is_coherent(
                {
                    "metric_value": None,
                    "completion_rate": None,
                    "target_amount_rmb": "100",
                    "actual_amount_rmb": None,
                    "gap_amount_rmb": None,
                },
                {"target_data_state": "set", "period_state": "not_started"},
            )
        )

    def test_snapshot_change_metrics_compile_from_published_capabilities(self) -> None:
        domain = "inventory"
        metric = "month_end_inventory_cost_rmb"
        dimensions = ("warehouse", "product")
        detail = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": domain, "metric": metric}]}
            )
        )["results"][0]
        self.assertEqual(
            "datasage-mini-inventory-semantics/v11",
            detail["source_versions"]["semantics"],
        )
        self.assertEqual(
            list(dimensions),
            detail["metric"]["change_decomposition_dimensions"],
        )
        self.assertNotIn("analysis_affordances", detail)
        request = {
            "request_id": "inventory_compile_partition",
            "domain": domain,
            "mode": "metric",
            "purpose": "offline snapshot compile proof",
            "metric": metric,
            "comparison": {"kind": "snapshot_months_before", "months": 1},
            "complete_change_decomposition": {"dimension": dimensions[0]},
        }
        expanded, links = tools._expand_complete_change_decompositions([request])
        self.assertEqual(2, len(expanded))
        self.assertEqual({request["request_id"]}, set(links))
        self.assertNotIn("limit", expanded[0])
        self.assertEqual(20, expanded[1]["limit"])
        scopes = []
        for expanded_request in expanded:
            normalized = tools._validate_request(expanded_request)
            datasets, semantics = tools._contracts(domain)
            tools._validate_pre_entity_metric_plan(
                normalized, datasets, semantics
            )
            sql, _params, scope = tools._build_metric_query(
                normalized,
                datasets,
                semantics,
                tools._metric_query_limit(normalized),
            )
            self.assertIn("current_known_value_count", sql)
            self.assertNotIn("value_coverage_rate AS", sql)
            scopes.append(scope["time_range"])
        self.assertEqual(scopes[0], scopes[1])
        self.assertEqual("latest_snapshot", scopes[0]["current"]["source"])
        self.assertEqual(
            {"source": "latest_snapshot_offset", "months_before": 1},
            scopes[0]["comparison"],
        )

        receivable = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "receivable-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("datasage-mini-receivable-semantics/v9", receivable["version"])
        for code in ("positive_debt_amount", "overdue_receivable_amount"):
            self.assertNotIn("change_decomposition", receivable["metrics"][code])

    def test_snapshot_complete_executor_reconciles_and_fails_closed(self) -> None:
        payload, raw, sql_calls = self._run_snapshot_change_operation()
        partition_id = "inventory_snapshot_partition"
        partition = raw[partition_id]
        self.assertEqual("success", payload["status"])
        self.assertEqual(2, len(sql_calls))
        self.assertEqual(
            "reconciled", tools.evidence._reconciliation_status(partition)
        )
        self.assertEqual(
            {
                "current_missing_value_count": 0,
                "current_known_value_count": 2,
                "comparison_missing_value_count": 0,
                "comparison_known_value_count": 2,
            },
            partition["change_reconciliation"]["completeness_proof"]["overall"],
        )
        self.assertTrue(
            all(
                tools.evidence.claim_is_valid_for_result(claim, partition)
                and claim["relation_semantics"]["structural_contribution"]
                == "structural_not_causal"
                and "value_coverage_rate" not in claim["facts"]
                for claim in partition["claim_ledger"]
            )
        )
        self.assertIn("structural_contribution", tools.evidence._supports(partition))
        bundle = next(
            item
            for item in payload["evidence_bundle"]["items"]
            if item["request_id"] == partition_id
        )
        self.assertIn("structural_contribution", bundle["supports"])

        truncated_payload, truncated_raw, _calls = self._run_snapshot_change_operation(
            partition_truncated=True,
        )
        truncated = truncated_raw[partition_id]
        self.assertEqual("success", truncated_payload["status"])
        self.assertEqual(
            "same_statement_window_full_partition",
            truncated["change_reconciliation"]["proof_mode"],
        )
        self.assertFalse(
            truncated["change_reconciliation"][
                "complete_population_claims_returned"
            ]
        )
        self.assertEqual(
            "reconciled", tools.evidence._reconciliation_status(truncated)
        )
        self.assertEqual(
            3,
            truncated["change_reconciliation"]["completeness_proof"][
                "partition_totals"
            ]["current_known_value_count"],
        )

        bounded_payload, bounded_raw, bounded_calls = self._run_snapshot_change_operation(
            partition_population_size=21,
        )
        bounded = bounded_raw[partition_id]
        self.assertEqual("success", bounded_payload["status"])
        self.assertEqual(20, bounded["requested_limit"])
        self.assertEqual(20, bounded["effective_limit"])
        self.assertEqual(20, bounded["row_count"])
        self.assertEqual(20, len(bounded["rows"]))
        self.assertTrue(bounded["truncated"])
        self.assertTrue(bounded["has_more"])
        partition_sql, partition_params, partition_limit = bounded_calls[1]
        self.assertEqual(20, partition_limit)
        self.assertEqual(21, partition_params[-1])
        self.assertIn("LIMIT %s", partition_sql)
        self.assertIn(tools._INTERNAL_PARTITION_ROW_COUNT, partition_sql)
        bounded_reconciliation = bounded["change_reconciliation"]
        self.assertEqual("reconciled", bounded_reconciliation["status"])
        self.assertEqual(
            "same_statement_window_full_partition",
            bounded_reconciliation["proof_mode"],
        )
        self.assertEqual(21, bounded_reconciliation["full_partition_row_count"])
        self.assertEqual(20, bounded_reconciliation["returned_driver_row_count"])
        self.assertEqual(1, bounded_reconciliation["unreturned_driver_row_count"])
        self.assertFalse(
            bounded_reconciliation["complete_population_claims_returned"]
        )

        invalid_payload, invalid_raw, _calls = self._run_snapshot_change_operation(
            inconsistent_partition_snapshot=True,
        )
        invalid = invalid_raw[partition_id]
        self.assertEqual(
            "failed",
            invalid_payload["status"],
            "an internal overall success must not upgrade the one public failed branch",
        )
        self.assertNotIn("error", invalid_payload)
        self.assertEqual("CONTRACT_UNAVAILABLE", invalid["error"]["code"])
        self.assertEqual(
            "not_reconciled", invalid["change_reconciliation"]["status"]
        )
        self.assertNotIn(
            "structural_contribution", tools.evidence._supports(invalid)
        )

        expected_coverage_reasons = {
            "missing": "DATA_COVERAGE_INCOMPLETE",
            "incomplete": "DATA_COVERAGE_INCOMPLETE",
            "invalid": "DATA_COVERAGE_PROOF_INVALID",
            "mismatch": "DATA_COVERAGE_PROOF_MISMATCH",
        }
        for coverage_case, reason in expected_coverage_reasons.items():
            with self.subTest(coverage_case=coverage_case):
                case_payload, case_raw, _calls = self._run_snapshot_change_operation(
                    coverage_case=coverage_case,
                )
                result = case_raw[partition_id]
                self.assertEqual("success", case_payload["status"])
                self.assertEqual(
                    {
                        "status": "not_reconciled",
                        "operation": "complete_change_decomposition",
                        "reason_code": reason,
                        "overall_request_id": result["change_reconciliation"][
                            "overall_request_id"
                        ],
                    },
                    result["change_reconciliation"],
                )
                self.assertNotIn(
                    "structural_contribution", tools.evidence._supports(result)
                )

        proof_payload, proof_raw, _calls = self._run_snapshot_change_operation(
            partition_truncated=True,
            coverage_case="proof_missing",
        )
        self.assertEqual("success", proof_payload["status"])
        self.assertEqual(
            "DATA_COVERAGE_PROOF_UNAVAILABLE",
            proof_raw[partition_id]["change_reconciliation"]["reason_code"],
        )

        for mismatch in ("snapshot", "scope", "period"):
            with self.subTest(mismatch=mismatch):
                results, _wire = self._run_synthetic_change_pipeline(
                    overall_triplet=("100", "80", "20"),
                    partition_triplets=(("60", "50", "10"), ("40", "30", "10")),
                    mismatch=mismatch,
                )
                partition = results[1]
                self.assertEqual(
                    "not_reconciled", partition["change_reconciliation"]["status"]
                )
                self.assertNotIn(
                    "structural_contribution", tools.evidence._supports(partition)
                )

        flow_snapshot = {
            "request_id": "invalid_flow_snapshot",
            "domain": "receipt",
            "mode": "metric",
            "purpose": "invalid snapshot operation must fail before database access",
            "metric": "net_receipt_amount",
            "comparison": {"kind": "snapshot_months_before", "months": 1},
            "complete_change_decomposition": {"dimension": "customer"},
        }
        with (
            mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=AssertionError("business database must not be accessed"),
            ) as execute,
            mock.patch.object(
                tools,
                "_consistent_snapshot_executor",
                side_effect=AssertionError("snapshot executor must not start"),
            ) as snapshot,
        ):
            failure = json.loads(
                tools.runtime_guarded_datasage_query({"requests": [flow_snapshot]})
            )
        self.assertNotIn("error", failure)
        self.assertEqual("INVALID_PLAN", failure["results"][0]["error"]["code"])
        execute.assert_not_called()
        snapshot.assert_not_called()

    def test_post_sql_failures_preserve_confirmed_source_evidence(self) -> None:
        raw_request = {
            "request_id": "post_sql_source_evidence",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "offline post-SQL source evidence regression",
            "metric": "delivery_amount",
            "calendar_month": "2026-08",
            "dimensions": ["customer"],
            "limit": 100,
            "order_by": {"field": "metric_value", "direction": "desc"},
        }
        request = tools._validate_delivery_metric_scope(
            tools._validate_request(raw_request)
        )
        datasets, semantics = tools._contracts("delivery")
        tools._validate_pre_entity_metric_plan(request, datasets, semantics)
        prepared = {
            "request": request,
            "datasets": datasets,
            "semantics": semantics,
            "resolved_entities": [],
            "entity_resolution_db_call_count": 0,
        }
        source_evidence = self._read_only_source_evidence()
        large_rows = [
            {
                "customer_id": f"customer-{index}",
                "customer_name": f"Customer {index} " + "x" * 1_500,
                "metric_value": "1.00",
            }
            for index in range(100)
        ]
        self.assertGreater(
            len(json.dumps(large_rows, ensure_ascii=False).encode("utf-8")),
            80_000,
        )

        original_bounded_int = tools._bounded_int

        def configured_result_budget(name, default, minimum, maximum):
            if name == "max_result_bytes":
                return 80_000
            return original_bounded_int(name, default, minimum, maximum)

        with mock.patch.object(
            tools,
            "_bounded_int",
            side_effect=configured_result_budget,
        ):
            oversized = tools._run_one(
                raw_request,
                prepared=prepared,
                execute_query=lambda *_args, **_kwargs: (
                    large_rows,
                    False,
                    source_evidence,
                ),
            )
        self.assertEqual(
            "success",
            oversized["status"],
            "raw internal size must not erase evidence that the model wire can compact",
        )
        self.assertEqual(1, oversized["business_sql_attempted_count"])
        self.assertEqual(1, oversized["business_sql_confirmed_count"])
        self.assertEqual(source_evidence, oversized["source_evidence_ref"])

        with mock.patch.object(
            tools,
            "_evidence_rows_and_state",
            side_effect=RuntimeError("synthetic post-SQL failure"),
        ):
            unexpected = tools._run_one(
                raw_request,
                prepared=prepared,
                execute_query=lambda *_args, **_kwargs: (
                    [{"customer_id": "customer-1", "customer_name": "C1", "metric_value": "1.00"}],
                    False,
                    source_evidence,
                ),
            )
        self.assertEqual("failed", unexpected["status"])
        self.assertEqual("INTERNAL_ERROR", unexpected["error"]["code"])
        self.assertEqual(1, unexpected["business_sql_confirmed_count"])
        self.assertEqual(source_evidence, unexpected["source_evidence_ref"])

    def test_source_evidence_invalidity_and_identity_drift_are_distinct(self) -> None:
        valid = self._read_only_source_evidence()
        invalid = dict(valid)
        invalid["identity_sha256"] = "2" * 64

        for label, reference in (("missing", None), ("invalid", invalid)):
            with self.subTest(label=label):
                with self.assertRaises(tools.QueryFailure) as raised:
                    tools._consistent_source_evidence_ref([reference])
                self.assertEqual(
                    "DATABASE_SOURCE_EVIDENCE_INVALID",
                    raised.exception.code,
                )
                self.assertEqual("database_security", raised.exception.stage)
                self.assertEqual(
                    "Database source evidence is invalid.",
                    raised.exception.message,
                )

        with self.assertRaises(tools.QueryFailure) as missing_batch:
            tools._batch_source_evidence_ref(
                [
                    {
                        "business_sql_confirmed_count": 1,
                        "source_evidence_ref": None,
                    }
                ]
            )
        self.assertEqual(
            "DATABASE_SOURCE_EVIDENCE_INVALID",
            missing_batch.exception.code,
        )

        changed = dict(valid)
        changed["identity_sha256"] = "2" * 64
        changed.pop("security_evidence_sha256")
        canonical = json.dumps(
            changed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        changed["security_evidence_sha256"] = hashlib.sha256(
            b"datasage-query-source-evidence/v1\x00" + canonical
        ).hexdigest()
        with self.assertRaises(tools.QueryFailure) as drift:
            tools._consistent_source_evidence_ref([valid, changed])
        self.assertEqual("DATABASE_IDENTITY_CHANGED", drift.exception.code)
        self.assertEqual("database_security", drift.exception.stage)

    def test_model_wire_rejects_invalid_structural_reconciliation(self) -> None:
        results, _wire = self._run_synthetic_change_pipeline(
            overall_triplet=("100", "80", "20"),
            partition_triplets=(("60", "50", "10"), ("40", "30", "10")),
        )
        valid = results[1]
        projected = tools._model_wire_result(valid)
        self.assertNotIn("error", projected)
        self.assertIn("structural_contributor_claim_ids", projected["change_reconciliation"])

        tampered_id = copy.deepcopy(valid)
        tampered_id["change_reconciliation"]["reconciliation_id"] = "tampered"
        tampered_projection = tools._model_wire_result(tampered_id)
        self.assertEqual([], tampered_projection["claim_ledger"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID", tampered_projection["error"]["code"]
        )

        tampered_reference = copy.deepcopy(valid)
        tampered_reference["change_reconciliation"]["driver_claim_ids"] = [
            "claim_not_in_partition"
        ]
        tools.evidence.seal_reconciliation(
            tampered_reference["change_reconciliation"]
        )
        reference_projection = tools._model_wire_result(tampered_reference)
        self.assertEqual([], reference_projection["claim_ledger"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID", reference_projection["error"]["code"]
        )

        missing = copy.deepcopy(valid)
        missing.pop("change_reconciliation")
        missing_projection = tools._model_wire_result(missing)
        self.assertEqual([], missing_projection["claim_ledger"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID", missing_projection["error"]["code"]
        )

        _payload, inventory_raw, _calls = self._run_snapshot_change_operation()
        inventory_valid = inventory_raw["inventory_snapshot_partition"]
        inventory_projection = tools._model_wire_result(inventory_valid)
        self.assertIsNone(inventory_projection.get("error"))

        invalid_coverage_receipts = []
        without_coverage_proof = copy.deepcopy(inventory_valid)
        without_coverage_proof["change_reconciliation"].pop(
            "completeness_proof"
        )
        invalid_coverage_receipts.append(without_coverage_proof)

        mismatched_coverage = copy.deepcopy(inventory_valid)
        mismatched_coverage["change_reconciliation"]["completeness_proof"][
            "overall"
        ]["current_known_value_count"] = 999
        invalid_coverage_receipts.append(mismatched_coverage)

        extra_coverage_field = copy.deepcopy(inventory_valid)
        extra_coverage_field["change_reconciliation"]["completeness_proof"][
            "unexpected"
        ] = True
        invalid_coverage_receipts.append(extra_coverage_field)

        invalid_coverage_type = copy.deepcopy(inventory_valid)
        invalid_coverage_type["change_reconciliation"]["completeness_proof"][
            "partition_totals"
        ]["comparison_known_value_count"] = "1.5"
        invalid_coverage_receipts.append(invalid_coverage_type)

        for invalid_coverage in invalid_coverage_receipts:
            tools.evidence.seal_reconciliation(
                invalid_coverage["change_reconciliation"]
            )
            invalid_projection = tools._model_wire_result(invalid_coverage)
            self.assertEqual([], invalid_projection["claim_ledger"])
            self.assertEqual(
                "EVIDENCE_INTEGRITY_INVALID",
                invalid_projection["error"]["code"],
            )

        _payload, truncated_raw, _calls = self._run_snapshot_change_operation(
            partition_truncated=True,
        )
        truncated_projection = tools._model_wire_result(
            truncated_raw["inventory_snapshot_partition"]
        )
        self.assertIsNone(truncated_projection.get("error"))
        self.assertEqual(
            "reconciled",
            truncated_projection["change_reconciliation"]["status"],
        )

        mismatch_results, _wire = self._run_synthetic_change_pipeline(
            overall_triplet=("100", "80", "20"),
            partition_triplets=(("60", "50", "10"), ("30", "20", "10")),
        )
        not_reconciled = tools._model_wire_result(mismatch_results[1])
        self.assertNotIn("error", not_reconciled)
        self.assertEqual(
            "not_reconciled", not_reconciled["change_reconciliation"]["status"]
        )
        self.assertEqual(
            "complete_change_decomposition",
            not_reconciled["change_reconciliation"]["operation"],
        )

        ordinary = tools._model_wire_result(results[0])
        self.assertNotIn("error", ordinary)
        self.assertNotIn("change_reconciliation", ordinary)
        self.assertIn("period_comparison", ordinary["claim_ledger"][0]["allowed_relations"])

        facts = {
            "metric_value": "1",
            "comparison_value": "0",
            "delta_value": "1",
        }
        self.assertFalse(
            tools._comparison_is_complete(facts, {"target_data_state": "not_present"})
        )
        facts.update(
            current_missing_value_count=0,
            current_known_value_count=0,
        )
        self.assertTrue(
            tools._comparison_is_complete(
                facts,
                {"current_metric_data_state": "not_present"},
            )
        )

    def test_analytical_metrics_do_not_publish_generic_change_ranking(self) -> None:
        analytical_metrics = {
            "inventory": ("inventory_turnover_days",),
            "target": ("delivery_target_completion", "receipt_target_completion"),
            "receipt": ("delivery_receipt_comparison",),
            "receivable": (
                "average_settlement_days",
                "formal_receivable_turnover_days",
                "maximum_settlement_days",
                "settlement_days_distribution",
            ),
        }
        self.assertEqual(8, sum(map(len, analytical_metrics.values())))
        for domain, metric_codes in analytical_metrics.items():
            for metric_code in metric_codes:
                payload = json.loads(
                    contracts.datasage_catalog(
                        {"requests": [{"domain": domain, "metric": metric_code}]}
                    )
                )
                self.assertEqual("success", payload["status"], metric_code)
                detail = payload["results"][0]
                self.assertIs(detail["metric"]["supports_generic_comparison"], False)
                self.assertEqual(
                    [],
                    detail["capability_affordances"]
                    ["selected_metric_capabilities"]["comparison_kinds"],
                )

    def test_target_dimension_limits_preserve_global_and_metric_arity(self) -> None:
        semantics = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "target-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("defaults", semantics)
        expected = {
            "delivery_target_amount": 3,
            "receipt_target_amount": 3,
            "delivery_target_completion": 2,
            "receipt_target_completion": 2,
        }
        for metric_code, limit in expected.items():
            payload = json.loads(
                contracts.datasage_catalog(
                    {"requests": [{"domain": "target", "metric": metric_code}]}
                )
            )
            self.assertEqual(
                limit,
                payload["results"][0]["metric"]["max_group_dimensions"],
            )

    def test_query_policy_has_one_typed_validator(self) -> None:
        raw = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "query-policy.yaml").read_text(
                encoding="utf-8"
            )
        )
        policy = capability_contract.parse_query_policy(raw)
        self.assertEqual(raw, policy.as_mapping())
        self.assertEqual(raw, contracts._query_policy_projection())

        malformed = []
        for key, value in (
            ("max_days", True),
            ("max_days", 0),
            ("wider_analysis", "silently_widen"),
        ):
            candidate = copy.deepcopy(raw)
            candidate["governed_metric_time_range"][key] = value
            malformed.append(candidate)
        with_extra_root = copy.deepcopy(raw)
        with_extra_root["planner_hint"] = "choose a metric"
        malformed.append(with_extra_root)
        for candidate in malformed:
            with self.subTest(candidate=candidate), self.assertRaises(
                capability_contract.CapabilityContractError
            ) as caught:
                capability_contract.parse_query_policy(candidate)
            self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)

    def test_value_contract_parser_owns_typed_normalization_and_rejections(self) -> None:
        raw = {
            "kind": "closed",
            "allowed_values": ["m", "y"],
            "canonical_aliases": {"M": "m"},
            "business_meanings": {"m": "metre", "y": "yard"},
        }
        contract = capability_contract.parse_value_contract(raw)
        self.assertEqual(raw, contract.as_mapping())
        self.assertEqual("m", contract.normalize_filter_value("M"))
        self.assertEqual(["m", "y"], contract.normalize_filter_value(["M", "y"]))

        malformed = (
            {"kind": "closed", "allowed_values": [1, 1]},
            {"kind": "closed", "allowed_values": [None]},
            {
                "kind": "closed",
                "allowed_values": ["m"],
                "canonical_aliases": {"M": None},
            },
            {
                "kind": "closed",
                "allowed_values": ["m"],
                "business_meanings": {"m": ""},
            },
            {"kind": "source_exact", "allowed_values": ["invented"]},
            {"kind": "source_exact", "unused_hint": "shadow policy"},
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate), self.assertRaises(
                capability_contract.CapabilityContractError
            ):
                capability_contract.parse_value_contract(candidate)
            with self.assertRaises(contracts.ContractFailure):
                contracts._value_contract_projection(candidate, dimension="test")
        for invalid_filter in (None, [], ["m", None]):
            with self.subTest(invalid_filter=invalid_filter), self.assertRaises(
                capability_contract.CapabilityContractError
            ):
                contract.normalize_filter_value(invalid_filter)

    def test_executor_consumes_shared_typed_policy_and_value_contracts(self) -> None:
        policy = contract_store.read_query_policy()
        with mock.patch.object(
            tools.contract_store,
            "read_query_policy",
            return_value=replace(policy, max_days=17),
        ):
            self.assertEqual(17, tools._max_metric_range_days())

        definition = {
            "filterable": True,
            "value_contract": {
                "kind": "closed",
                "allowed_values": ["m"],
                "canonical_aliases": {"M": "m"},
            },
        }
        with mock.patch.object(
            tools.capability_contract,
            "parse_value_contract",
            wraps=capability_contract.parse_value_contract,
        ) as shared_parser:
            normalized = tools._validate_metric_filter_value_contracts(
                {"metric_filters": {"unit": "M"}},
                {"dimensions": {"unit": definition}},
            )
        self.assertEqual("m", normalized["metric_filters"]["unit"])
        shared_parser.assert_called_once_with(
            definition["value_contract"],
            filterable_default=True,
        )

    def test_dataset_global_blocked_columns_remain_runtime_consumed(self) -> None:
        datasets = contracts._read_yaml(
            "plugins/datasage-query/contracts/datasets.yaml"
        )
        blocked = tools._blocked_columns(
            datasets,
            datasets["datasets"]["vk_dwd.customer_dwd"],
        )
        self.assertIn("cost_price", blocked)
        self.assertIn("partner_ids", blocked)
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._approved_column(
                "cost_price",
                {"cost_price"},
                blocked,
            )
        self.assertEqual("COLUMN_NOT_ALLOWED", caught.exception.code)

    def test_target_gap_typed_contract_rejects_malformed_authority(self) -> None:
        raw = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "target-gap-decomposition.yaml").read_text(
                encoding="utf-8"
            )
        )
        contract = capability_contract.parse_target_gap_contract(raw)
        self.assertEqual("datasage-target-gap-decomposition/v1", contract.version)
        self.assertEqual(
            "additive_gap_composition_not_causal",
            contract.receipt_interpretation_code,
        )
        self.assertEqual(contract, contract_store.read_target_gap_contract())

        malformed = []
        duplicate_metric = copy.deepcopy(raw)
        duplicate_metric["applicability"]["metrics"].append(
            duplicate_metric["applicability"]["metrics"][0]
        )
        malformed.append(duplicate_metric)
        overlapping_state = copy.deepcopy(raw)
        overlapping_state["fail_closed_target_data_states"].append("set")
        malformed.append(overlapping_state)
        missing_boundary = copy.deepcopy(raw)
        missing_boundary["receipt"].pop("interpretation_code")
        malformed.append(missing_boundary)
        inactive_rollout = copy.deepcopy(raw)
        inactive_rollout["rollout"]["status"] = "disabled"
        malformed.append(inactive_rollout)
        shadow_field = copy.deepcopy(raw)
        shadow_field["algebra"] = {"gap": "target - actual"}
        malformed.append(shadow_field)
        for candidate in malformed:
            with self.subTest(candidate=candidate), self.assertRaises(
                capability_contract.CapabilityContractError
            ) as caught:
                capability_contract.parse_target_gap_contract(candidate)
            self.assertEqual("CONTRACT_UNAVAILABLE", caught.exception.code)

    def test_target_gap_operation_is_projected_from_versioned_contract(self) -> None:
        expected = {
            "operation": "complete_target_gap_decomposition",
            "dimensions": ["customer", "department", "organization"],
            "required_attribution_mode": "transaction_detail",
        }
        for metric_code in ("delivery_target_completion", "receipt_target_completion"):
            payload = json.loads(
                contracts.datasage_catalog(
                    {"requests": [{"domain": "target", "metric": metric_code}]}
                )
            )
            detail = payload["results"][0]
            self.assertEqual(expected, detail["metric"]["target_gap_decomposition"])
            self.assertEqual(
                "datasage-target-gap-decomposition/v1",
                detail["source_versions"]["target_gap_decomposition"],
            )
            self.assertIn(
                "complete_target_gap_decomposition",
                detail["capability_affordances"]["capabilities"],
            )

    def test_target_gap_executor_reconciles_three_amounts_in_one_scope(self) -> None:
        contexts, results, operations = self._synthetic_target_gap_inputs()
        tools._finalize_target_gap_decompositions(contexts, results, operations)
        overall, partition = results
        receipt = partition["target_gap_reconciliation"]
        self.assertEqual("reconciled", receipt["status"])
        self.assertEqual(
            ("300", "240", "60"),
            tuple(
                receipt[key]
                for key in (
                    "partition_target_sum_rmb",
                    "partition_actual_sum_rmb",
                    "partition_gap_sum_rmb",
                )
            ),
        )
        self.assertEqual(
            overall["_snapshot_group_marker"], partition["_snapshot_group_marker"]
        )
        self.assertEqual(overall["scope_fingerprint"], partition["scope_fingerprint"])
        self.assertEqual(overall["applied_time_range"], partition["applied_time_range"])
        self.assertFalse(receipt["completion_rate_aggregated"])
        self.assertFalse(receipt["causal_attribution_authorized"])
        self.assertTrue(
            tools.evidence._target_gap_reconciliation_is_valid(
                partition,
                request=contexts[1]["request"],
                overall_result=overall,
            )
        )
        typed_contract = contract_store.read_target_gap_contract()
        with mock.patch.object(
            tools.evidence.contract_store,
            "read_target_gap_contract",
            return_value=replace(typed_contract, receipt_version="different/v1"),
        ):
            self.assertFalse(
                tools.evidence._target_gap_reconciliation_is_valid(
                    partition,
                    request=contexts[1]["request"],
                    overall_result=overall,
                )
            )
        self.assertIn(
            "target_gap_composition",
            tools.evidence._supports(
                partition,
                request=contexts[1]["request"],
                overall_result=overall,
            ),
        )

    def test_target_gap_truncation_returns_typed_not_reconciled(self) -> None:
        contexts, results, operations = self._synthetic_target_gap_inputs(
            partition_truncated=True
        )
        tools._finalize_target_gap_decompositions(contexts, results, operations)
        partition = results[1]
        self.assertEqual("truncated", partition["data_state"])
        self.assertEqual(
            {
                "status": "not_reconciled",
                "operation": "complete_target_gap_decomposition",
                "reason_code": "PARTITION_PROOF_UNAVAILABLE",
                "overall_request_id": operations["target_gap_partition"],
                "causal_attribution_authorized": False,
            },
            partition["target_gap_reconciliation"],
        )
        self.assertNotIn("target_gap_composition", tools.evidence._supports(partition))

    def test_target_gap_identity_mismatch_fails_closed(self) -> None:
        for mismatch in ("snapshot", "scope", "period"):
            with self.subTest(mismatch=mismatch):
                contexts, results, operations = self._synthetic_target_gap_inputs()
                if mismatch == "snapshot":
                    results[1]["_snapshot_group_marker"] = "different-snapshot"
                    expected_reason = "SNAPSHOT_CONSISTENCY_UNPROVEN"
                elif mismatch == "scope":
                    results[1]["scope_fingerprint"] = "different-scope"
                    expected_reason = "SCOPE_MISMATCH"
                else:
                    results[1]["applied_time_range"] = {
                        "start": "2026-07-01",
                        "end": "2026-08-01",
                    }
                    expected_reason = "SCOPE_MISMATCH"
                tools._finalize_target_gap_decompositions(
                    contexts, results, operations
                )
                receipt = results[1]["target_gap_reconciliation"]
                self.assertEqual("not_reconciled", receipt["status"])
                self.assertEqual(expected_reason, receipt["reason_code"])
                self.assertNotIn(
                    "target_gap_composition", tools.evidence._supports(results[1])
                )

    def test_target_gap_invalid_target_states_return_typed_failure(self) -> None:
        for target_state in ("missing", "incomplete", "not_set_for_future"):
            with self.subTest(target_state=target_state):
                contexts, results, operations = self._synthetic_target_gap_inputs(
                    target_data_state=target_state,
                    period_state=(
                        "not_started"
                        if target_state == "not_set_for_future"
                        else "current"
                    ),
                )
                tools._finalize_target_gap_decompositions(
                    contexts, results, operations
                )
                partition = results[1]
                receipt = partition["target_gap_reconciliation"]
                self.assertEqual("not_reconciled", receipt["status"])
                self.assertEqual("TARGET_STATE_INCOMPLETE", receipt["reason_code"])
                self.assertTrue(
                    all(
                        claim["states"]["target_data_state"] == target_state
                        for result in results
                        for claim in result["claim_ledger"]
                    )
                )
                self.assertNotIn(
                    "target_gap_composition", tools.evidence._supports(partition)
                )

    def test_domain_planner_recipes_are_not_model_visible(self) -> None:
        skill_surface = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [SKILL_PATH, *(SKILL_PATH.parent / "references").iterdir()]
            if path.is_file()
        )
        self.assertNotIn("planner_", skill_surface)
        self.assertFalse((PLUGIN_ROOT / "references.py").exists())

    def test_metric_capabilities_and_required_answer_scope_survive_model_wire(
        self,
    ) -> None:
        payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "receipt", "view": "expert_index"}]}
            )
        )
        self.assertEqual("success", payload["status"])
        expert_index = payload["results"][0]
        metrics = {item["code"]: item for item in expert_index["metrics"]}
        for item in metrics.values():
            self.assertIsInstance(item["requires_metric_detail"], bool)
            self.assertIs(
                item["requires_metric_detail"],
                item.get("exact_default_lookup_supported") is not True,
            )
        selected = metrics["net_receipt_amount"]
        self.assertIs(selected["exact_default_lookup_supported"], False)
        self.assertIs(selected["requires_metric_detail"], True)
        delivery_payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "delivery", "view": "expert_index"}]}
            )
        )
        delivery_metrics = {
            item["code"]: item
            for item in delivery_payload["results"][0]["metrics"]
        }
        self.assertIs(
            delivery_metrics["delivery_amount"]["exact_default_lookup_supported"],
            True,
        )
        self.assertIs(
            delivery_metrics["delivery_amount"]["requires_metric_detail"],
            False,
        )

        self.assertNotIn("metric_selection_boundary", expert_index)
        self.assertNotIn("next_step", expert_index)

        captured: dict[str, object] = {}

        def execute_query(sql, params, limit, **_kwargs):
            captured["sql"] = sql
            captured["params"] = list(params)
            captured["limit"] = limit
            return (
                [{"metric_value": "42.00"}],
                False,
                self._read_only_source_evidence(),
            )

        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=execute_query,
        ):
            query_payload = json.loads(
                tools.datasage_query(
                    {
                        "requests": [
                            {
                                "request_id": "receipt_month",
                                "domain": "receipt",
                                "mode": "metric",
                                "purpose": "synthetic contract test",
                                "metric": "net_receipt_amount",
                                "dimensions": [],
                                "calendar_month": "2026-07",
                            }
                        ]
                    }
                )
            )

        self.assertEqual("success", query_payload["status"])
        coverage_receipts = query_payload["evidence_bundle"][
            "coverage_receipts"
        ]["items"]
        self.assertEqual(1, len(coverage_receipts))
        self.assertEqual(["receipt_month"], coverage_receipts[0]["request_ids"])
        self.assertNotIn("detail_receipt", json.dumps(query_payload, ensure_ascii=False))
        self.assertIn("2026-07-01", captured["params"])
        self.assertIn("2026-08-01", captured["params"])
        result = query_payload["results"][0]
        self.assertEqual("2026-07-01", result["applied_time_range"]["start"])
        self.assertEqual("2026-08-01", result["applied_time_range"]["end"])
        self.assertEqual("explicit", result["applied_time_range"]["source"])
        self.assertEqual(
            "completed",
            result["applied_time_range"]["calendar_evidence"]["period_state"],
        )
        self.assertEqual(
            "not_proven",
            result["applied_time_range"]["calendar_evidence"]["source_freshness"],
        )
        self.assertEqual(
            "查询范围：2026-07-01 至 2026-07-31",
            query_payload["answer_scope_line"],
        )
        def disclosures_by_id(
            query_result: dict[str, object],
        ) -> dict[str, dict[str, object]]:
            return {
                item["disclosure_id"]: item
                for item in query_result["disclosure_ledger"]
            }

        def assert_ledger_seals(query_result: dict[str, object]) -> None:
            for disclosure in query_result["disclosure_ledger"]:
                canonical = json.dumps(
                    {
                        key: value
                        for key, value in disclosure.items()
                        if key != "disclosure_seal"
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                self.assertEqual(
                    "sha256_" + hashlib.sha256(canonical).hexdigest(),
                    disclosure["disclosure_seal"],
                )
            ledger_canonical = json.dumps(
                {
                    "contract_version": query_result[
                        "disclosure_contract_version"
                    ],
                    "request_id": query_result["request_id"],
                    "metric_ref": query_result["business_metric_ref"],
                    "scope_fingerprint": query_result["scope_fingerprint"],
                    "projection_fingerprint": query_result["projection_fingerprint"],
                    "ledger": query_result["disclosure_ledger"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            self.assertEqual(
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest(),
                query_result["disclosure_ledger_seal"],
            )

        disclosures = disclosures_by_id(result)
        self.assertEqual(
            {
                "receipt.domain.scope",
                "receipt.net.scope",
            },
            set(disclosures),
        )
        expected_disclosure_texts = {
            "receipt.domain.scope": "收款与退款金额默认包含内部客户并排除A状态；跨账本对照中其他金额侧以对应指标披露为准。",
            "receipt.net.scope": (
                "净收款为收款人民币金额减退款人民币金额；"
                "收款和退款范围均包含内部客户并排除A状态。"
            ),
        }
        self.assertEqual(
            expected_disclosure_texts,
            {key: item["text"] for key, item in disclosures.items()},
        )
        domain_scope = disclosures["receipt.domain.scope"]["text"]
        self.assertIn("内部客户", domain_scope)
        self.assertIn("排除A状态", domain_scope)
        self.assertNotIn("本次实际筛选范围", domain_scope)
        self.assertEqual("required_always", disclosures["receipt.domain.scope"]["mode"])
        net_scope = disclosures["receipt.net.scope"]["text"]
        self.assertIn("收款人民币金额减退款人民币金额", net_scope)
        self.assertIn("内部客户", net_scope)
        self.assertIn("排除A状态", net_scope)
        self.assertEqual(
            {
                "receipt.domain.scope": True,
                "receipt.net.scope": True,
            },
            {key: item["applies"] for key, item in disclosures.items()},
        )
        self.assertEqual(
            "metric-disclosure-ledger-model-projection/v1",
            result["disclosure_contract_version"],
        )
        assert_ledger_seals(result)

        def run_receipt_case(
            request: dict[str, object],
        ) -> dict[str, dict[str, object]]:
            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(tools.datasage_query({"requests": [request]}))
            self.assertEqual("success", payload["status"])
            query_result = payload["results"][0]
            assert_ledger_seals(query_result)
            return disclosures_by_id(query_result)

        base_receipt_request = {
            "domain": "receipt",
            "mode": "metric",
            "purpose": "synthetic disclosure condition test",
            "calendar_month": "2026-07",
        }
        for request in (
            {
                **base_receipt_request,
                "request_id": "receipt_usage_group",
                "metric": "receipt_amount",
                "dimensions": ["receipt_usage"],
            },
            {
                **base_receipt_request,
                "request_id": "receipt_usage_filter",
                "metric": "receipt_amount",
                "dimensions": [],
                "metric_filters": {"receipt_usage": "synthetic_usage"},
            },
            {
                **base_receipt_request,
                "request_id": "refund_usage_filter",
                "metric": "refund_amount",
                "dimensions": [],
                "metric_filters": {"refund_usage": "synthetic_usage"},
            },
        ):
            case_disclosures = run_receipt_case(request)
            self.assertIs(case_disclosures["receipt.domain.scope"]["applies"], True)
            self.assertIs(
                case_disclosures["receipt.domain.usage-scope"]["applies"],
                True,
            )
            self.assertNotIn("receipt.receipt.scope", case_disclosures)

        unrelated_disclosures = run_receipt_case(
            {
                **base_receipt_request,
                "request_id": "receipt_unrelated_filter",
                "metric": "receipt_amount",
                "dimensions": [],
                "metric_filters": {"currency": "CNY"},
            }
        )
        self.assertNotIn(
            "receipt.domain.usage-scope",
            unrelated_disclosures,
        )

        invalid_disclosure_conditions = (
            {"unexpected_condition": True},
            {"any_request_dimension_or_filter_present": []},
            {
                "any_request_dimension_or_filter_present": [
                    "receipt_usage",
                    "receipt_usage",
                ]
            },
            {"any_request_dimension_or_filter_present": ["ReceiptUsage"]},
            {"any_request_dimension_or_filter_present": [["receipt_usage"]]},
            {"any_request_dimension_or_filter_present": ["receipt_usgae"]},
        )
        _, receipt_semantics = tools._contracts("receipt")
        known_receipt_dimension_codes = set(receipt_semantics["dimensions"])
        self.assertTrue(
            {"receipt_usage", "refund_usage"} <= known_receipt_dimension_codes
        )
        for condition in invalid_disclosure_conditions:
            with self.assertRaises(tools.QueryFailure):
                tools._disclosure_applies(
                    {"mode": "required_when", "when": condition},
                    request={"dimensions": [], "metric_filters": {}},
                    known_dimension_codes=known_receipt_dimension_codes,
                    inventory_scope=None,
                    data_state="complete",
                    truncated=False,
                )

        invalid_registry_semantics = json.loads(
            json.dumps(receipt_semantics, ensure_ascii=False)
        )
        invalid_usage_scope = next(
            item
            for item in invalid_registry_semantics["default_disclosures"]
            if item["id"] == "receipt.domain.usage-scope"
        )
        invalid_usage_scope["when"] = {
            "any_request_dimension_or_filter_present": ["receipt_usgae"]
        }
        receipt_datasets, _ = tools._contracts("receipt")
        with self.assertRaises(tools.QueryFailure) as invalid_registry_error:
            tools._disclosure_ledger(
                request={
                    "request_id": "invalid_usage_registry",
                    "dimensions": [],
                    "metric_filters": {},
                },
                metric_ref="net_receipt_amount",
                metric_definition=receipt_semantics["metrics"]["net_receipt_amount"],
                datasets=receipt_datasets,
                scope_fingerprint="scope",
                projection_fingerprint="projection",
                data_state="complete",
                truncated=False,
                known_dimension_codes=known_receipt_dimension_codes,
                inherited_disclosures=invalid_registry_semantics[
                    "default_disclosures"
                ],
            )
        self.assertEqual("CONTRACT_UNAVAILABLE", invalid_registry_error.exception.code)

        receipt_contract = yaml.safe_load(
            (
                PROFILE_ROOT
                / "plugins"
                / "datasage-query"
                / "contracts"
                / "receipt-semantics.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("datasage-mini-receipt-semantics/v11", receipt_contract["version"])
        self.assertNotIn("answer_contract", receipt_contract)
        receipt_disclosures = {
            item["id"]: item
            for item in receipt_contract["default_disclosures"]
        }
        self.assertEqual(
            "用途以本次实际查询的用途分组或筛选范围为准。",
            receipt_disclosures["receipt.domain.usage-scope"]["text"],
        )

        query_description = schemas.DATASAGE_QUERY["description"]
        self.assertIn("answer_scope_line", query_description)
        self.assertIn(
            "explicit, non-default qualifier",
            schemas.REQUEST["properties"]["calendar_month"]["description"],
        )
        self.assertIn(
            "explicit, non-default qualifier",
            schemas.REQUEST["properties"]["time_range"]["description"],
        )

        request_policy = _query_rules()
        answer_policy = _answer_boundary()
        normalized = " ".join(request_policy.split())
        self.assertIn("exact metric detail request is optional planning help", normalized)
        self.assertIn("process contract snapshot pinned", normalized)
        self.assertIn("Preserve typed states", answer_policy)

    def test_current_metric_contract_fails_closed_for_availability_capabilities_and_time(
        self,
    ) -> None:
        base_request = {
            "request_id": "current_metric_contract",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "current metric contract validation",
            "metric": "delivery_amount",
            "dimensions": [],
        }

        def validate(raw_request: dict[str, object]) -> dict[str, object]:
            normalized = tools._validate_request(raw_request)
            _, semantics = tools._contracts(str(normalized["domain"]))
            return tools._validate_metric_contract(normalized, semantics)

        quantity = validate(
            {
                **base_request,
                "metric": "delivery_quantity",
                "dimensions": ["unit"],
            }
        )
        self.assertEqual(["unit"], quantity["dimensions"])

        missing_unit = validate({**base_request, "metric": "delivery_quantity"})
        delivery_datasets, delivery_semantics = tools._contracts("delivery")
        with self.assertRaises(tools.QueryFailure) as unit_scope:
            tools._validate_pre_entity_metric_plan(
                missing_unit,
                delivery_datasets,
                delivery_semantics,
            )
        self.assertEqual("UNIT_SCOPE_REQUIRED", unit_scope.exception.code)

        with self.assertRaises(tools.QueryFailure) as unsupported_dimension:
            validate({**base_request, "dimensions": ["warehouse"]})
        self.assertEqual(
            "UNSUPPORTED_DIMENSION",
            unsupported_dimension.exception.code,
        )

        with self.assertRaises(tools.QueryFailure) as range_too_wide:
            validate(
                {
                    **base_request,
                    "time_range": {
                        "start": "2020-01-01",
                        "end": "2026-01-01",
                    },
                }
            )
        self.assertEqual("QUERY_RANGE_TOO_WIDE", range_too_wide.exception.code)

        decomposition_request = {
            key: value
            for key, value in base_request.items()
            if key != "dimensions"
        }
        with self.assertRaises(tools.QueryFailure) as unsupported_decomposition:
            validate(
                {
                    **decomposition_request,
                    "calendar_month": "2026-07",
                    "metric": "gross_delivery_amount",
                    "complete_change_decomposition": {"dimension": "currency"},
                }
            )
        self.assertEqual(
            "UNSUPPORTED_CHANGE_DECOMPOSITION",
            unsupported_decomposition.exception.code,
        )

        accepted = validate(base_request)
        self.assertEqual("delivery_amount", accepted["metric"])
        self.assertEqual([], accepted["dimensions"])

    def test_physical_execution_budget_is_a_local_branch_failure(self) -> None:
        requests = [
            {
                "request_id": f"budget_normal_{index}",
                "domain": "delivery",
                "mode": "metric",
                "purpose": "offline physical budget branch probe",
                "metric": "delivery_amount",
                "dimensions": [],
            }
            for index in range(9)
        ]
        requests.append(
            {
                "request_id": "budget_complete_overflow",
                "domain": "receipt",
                "mode": "metric",
                "purpose": "offline physical budget complete operation probe",
                "metric": "net_receipt_amount",
                "calendar_month": "2026-07",
                "complete_change_decomposition": {"dimension": "customer"},
            }
        )
        with (
            mock.patch.object(
                runtime_health,
                "query_readiness_status",
                return_value={"ready": True},
            ),
            mock.patch.object(
                tools,
                "_execute_with_source",
                return_value=(
                    [{"metric_value": "42.00"}],
                    False,
                    self._read_only_source_evidence(),
                ),
            ) as execute,
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query({"requests": requests})
            )

        self.assertEqual("partial", payload["status"])
        self.assertEqual(10, payload["request_count"])
        by_id = {item["request_id"]: item for item in payload["results"]}
        for index in range(9):
            self.assertEqual("success", by_id[f"budget_normal_{index}"]["status"])
        self.assertEqual(
            "EXECUTION_BUDGET_EXCEEDED",
            by_id["budget_complete_overflow"]["error"]["code"],
        )
        self.assertEqual(9, execute.call_count)

    def test_generated_decomposition_ids_do_not_collide_or_leak_into_public_coverage(
        self,
    ) -> None:
        complete = {
            "request_id": "public_partition",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "offline generated ID collision boundary probe",
            "metric": "delivery_amount",
            "calendar_month": "2026-07",
            "complete_change_decomposition": {"dimension": "department"},
        }
        first_expansion, first_links = tools._expand_complete_change_decompositions(
            [complete], reserved_request_ids=[complete["request_id"]]
        )
        colliding_public_id = first_links[complete["request_id"]]
        colliding_public = {
            "request_id": colliding_public_id,
            "domain": "delivery",
            "mode": "metric",
            "purpose": "offline public ID collision boundary probe",
            "metric": "delivery_amount",
            "dimensions": [],
        }
        expanded, links = tools._expand_complete_change_decompositions(
            [complete, colliding_public],
            reserved_request_ids=[complete["request_id"], colliding_public_id],
        )
        expanded_ids = [item["request_id"] for item in expanded]
        self.assertEqual(len(expanded_ids), len(set(expanded_ids)))
        self.assertNotEqual(colliding_public_id, links[complete["request_id"]])
        self.assertIn(colliding_public_id, expanded_ids)
        self.assertNotEqual(first_expansion[0]["request_id"], expanded[0]["request_id"])

        with mock.patch.object(
            runtime_health,
            "query_readiness_status",
            return_value={
                "ready": False,
                "reason_code": "DATABASE_CONFIGURATION_MISSING",
            },
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [complete, colliding_public]}
                )
            )
        public_ids = [item["request_id"] for item in payload["results"]]
        self.assertEqual(
            [complete["request_id"], colliding_public_id], public_ids
        )
        self.assertEqual(len(public_ids), len(set(public_ids)))
        evidence_ids = {
            item["request_id"] for item in payload["evidence_bundle"]["items"]
        }
        self.assertEqual(set(public_ids), evidence_ids)
        self.assertEqual(
            len(public_ids), payload["evidence_bundle"]["coverage"]["request_count"]
        )

    def test_public_model_wire_and_bundle_fail_closed_together(self) -> None:
        request = {
            "request_id": "public_wire",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "public model wire integrity test",
            "metric": "delivery_amount",
            "dimensions": [],
        }
        source_evidence = self._read_only_source_evidence()

        def run_public(
            rows: list[dict[str, object]],
        ) -> dict[str, object]:
            with (
                mock.patch.object(
                    runtime_health,
                    "query_readiness_status",
                    return_value={"ready": True},
                ),
                mock.patch.object(
                    tools,
                    "_execute_with_source",
                    return_value=(rows, False, source_evidence),
                ),
            ):
                return json.loads(
                    tools.runtime_guarded_datasage_query(
                        {"requests": [request]}
                    )
                )

        for label, rows, expected_state in (
            ("zero", [{"metric_value": 0}], "zero"),
            ("empty", [], "empty"),
            ("undefined", [{"metric_value": None}], "undefined"),
        ):
            payload = run_public(rows)
            result = payload["results"][0]
            self.assertEqual("success", payload["status"], label)
            self.assertEqual(expected_state, result["data_state"], label)
            self.assertIsNone(result.get("error"), label)
            self.assertTrue(
                all(
                    disclosure["applies"] is True
                    for disclosure in result["disclosure_ledger"]
                ),
                label,
            )

        original_disclosure_ledger = tools._disclosure_ledger

        def reseal_ledger(
            ledger: list[dict[str, object]],
            *,
            request_id: str,
            metric_ref: object,
            scope_fingerprint: str,
            projection_fingerprint: str,
        ) -> str:
            canonical = json.dumps(
                {
                    "contract_version": "metric-disclosure-ledger/v1",
                    "request_id": request_id,
                    "metric_ref": metric_ref,
                    "scope_fingerprint": scope_fingerprint,
                    "projection_fingerprint": projection_fingerprint,
                    "ledger": ledger,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            return "sha256_" + hashlib.sha256(canonical).hexdigest()

        def ledger_with_valid_false(**kwargs: object):
            ledger, _ = original_disclosure_ledger(**kwargs)
            request_arg = kwargs["request"]
            assert isinstance(request_arg, dict)
            false_item: dict[str, object] = {
                "disclosure_id": "synthetic.conditional-scope",
                "contract_version": "metric-disclosure/v1",
                "request_id": request_arg["request_id"],
                "metric_ref": kwargs["metric_ref"],
                "scope_fingerprint": kwargs["scope_fingerprint"],
                "projection_fingerprint": kwargs["projection_fingerprint"],
                "mode": "required_when",
                "order": len(ledger),
                "text": "synthetic non-applicable disclosure",
                "applies": False,
            }
            canonical = json.dumps(
                false_item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            false_item["disclosure_seal"] = (
                "sha256_" + hashlib.sha256(canonical).hexdigest()
            )
            ledger.append(false_item)
            return ledger, reseal_ledger(
                ledger,
                request_id=str(request_arg["request_id"]),
                metric_ref=kwargs["metric_ref"],
                scope_fingerprint=str(kwargs["scope_fingerprint"]),
                projection_fingerprint=str(kwargs["projection_fingerprint"]),
            )

        with mock.patch.object(
            tools,
            "_disclosure_ledger",
            side_effect=ledger_with_valid_false,
        ):
            valid_false = run_public([{"metric_value": "10.00"}])
        valid_false_result = valid_false["results"][0]
        self.assertIsNone(valid_false_result.get("error"))
        self.assertNotIn(
            "synthetic.conditional-scope",
            {
                item["disclosure_id"]
                for item in valid_false_result["disclosure_ledger"]
            },
        )

        def flipped_applicability(**kwargs: object):
            ledger, _ = original_disclosure_ledger(**kwargs)
            target = next(item for item in ledger if item["applies"] is True)
            target["applies"] = False
            request_arg = kwargs["request"]
            assert isinstance(request_arg, dict)
            return ledger, reseal_ledger(
                ledger,
                request_id=str(request_arg["request_id"]),
                metric_ref=kwargs["metric_ref"],
                scope_fingerprint=str(kwargs["scope_fingerprint"]),
                projection_fingerprint=str(kwargs["projection_fingerprint"]),
            )

        with mock.patch.object(
            tools,
            "_disclosure_ledger",
            side_effect=flipped_applicability,
        ):
            flipped = run_public([{"metric_value": "10.00"}])
        flipped_result = flipped["results"][0]
        self.assertEqual([], flipped_result["claim_ledger"])
        self.assertEqual("undefined", flipped_result["data_state"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            flipped_result["error"]["code"],
        )
        flipped_item = flipped["evidence_bundle"]["items"][0]
        self.assertEqual([], flipped_item["supports"])
        self.assertNotEqual("complete", flipped_item["completeness"])

        original_claim_ledger = tools._claim_ledger

        def scope_mismatched_claim(*args: object, **kwargs: object):
            claims = original_claim_ledger(*args, **kwargs)
            claims[0]["scope_fingerprint"] = "scope_tampered"
            tools.evidence.seal_claim(claims[0])
            return claims

        with mock.patch.object(
            tools,
            "_claim_ledger",
            side_effect=scope_mismatched_claim,
        ):
            mismatched = run_public([{"metric_value": "10.00"}])
        mismatched_result = mismatched["results"][0]
        self.assertEqual([], mismatched_result["claim_ledger"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            mismatched_result["error"]["code"],
        )
        self.assertEqual(
            [],
            mismatched["evidence_bundle"]["items"][0]["supports"],
        )
        self.assertEqual(
            [],
            mismatched["evidence_bundle"]["coverage_receipts"]["items"],
        )

        with mock.patch.object(
            tools,
            "_evidence_rows_and_state",
            return_value=([], "rows"),
        ):
            all_claims_lost = run_public([{"metric_value": "10.00"}])
        lost_result = all_claims_lost["results"][0]
        self.assertEqual([], lost_result["claim_ledger"])
        self.assertEqual("undefined", lost_result["data_state"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            lost_result["error"]["code"],
        )
        self.assertEqual(
            [],
            all_claims_lost["evidence_bundle"]["items"][0]["supports"],
        )

    def test_snapshot_population_resolver_and_formal_dso_scope_disclosure(
        self,
    ) -> None:
        def run_query(
            request: dict[str, object], rows: list[dict[str, object]]
        ) -> tuple[dict[str, object], list[dict[str, object]]]:
            calls: list[dict[str, object]] = []

            def execute_query(sql, params, limit, **_kwargs):
                calls.append({"sql": sql, "params": list(params), "limit": limit})
                return rows, False, self._read_only_source_evidence()

            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(
                    tools.datasage_query(
                        {"requests": [request]},
                        session_id="business-contract-session",
                    )
                )
            self.assertEqual("success", payload["status"])
            self.assertEqual(1, len(calls))
            return payload["results"][0], calls

        def assert_disclosure_seals(query_result: dict[str, object]) -> None:
            ledger = query_result["disclosure_ledger"]
            self.assertIsInstance(ledger, list)
            for disclosure in ledger:
                canonical = json.dumps(
                    {
                        key: value
                        for key, value in disclosure.items()
                        if key != "disclosure_seal"
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                self.assertEqual(
                    "sha256_" + hashlib.sha256(canonical).hexdigest(),
                    disclosure["disclosure_seal"],
                )
            ledger_canonical = json.dumps(
                {
                    "contract_version": query_result[
                        "disclosure_contract_version"
                    ],
                    "request_id": query_result["request_id"],
                    "metric_ref": query_result["business_metric_ref"],
                    "scope_fingerprint": query_result["scope_fingerprint"],
                    "projection_fingerprint": query_result["projection_fingerprint"],
                    "ledger": ledger,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            self.assertEqual(
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest(),
                query_result["disclosure_ledger_seal"],
            )

        def expected_attestation_seal(attestation: dict[str, object]) -> str:
            canonical = json.dumps(
                {
                    key: value
                    for key, value in attestation.items()
                    if key != "attestation_seal"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            return "sha256_" + hashlib.sha256(canonical).hexdigest()

        def assert_attestation_and_claim_seals(
            query_result: dict[str, object],
        ) -> dict[str, object]:
            claim = query_result["claim_ledger"][0]
            attestation = claim["facts"]["calculation_attestation"]
            self.assertEqual(
                expected_attestation_seal(attestation),
                attestation["attestation_seal"],
            )
            claim_id, claim_seal = tools.evidence._canonical_claim_identity(claim)
            self.assertEqual(claim_id, claim["claim_id"])
            self.assertEqual(claim_seal, claim["claim_seal"])
            return attestation

        r2_request = {
            "request_id": "r2_default",
            "domain": "receivable",
            "mode": "metric",
            "purpose": "synthetic snapshot population test",
            "metric": "current_debt_amount",
            "dimensions": [],
        }
        r2_result, r2_calls = run_query(r2_request, [{"metric_value": "42.00"}])
        self.assertIsInstance(r2_result["business_metric_ref"], str)
        self.assertNotIn(
            "calculation_attestation",
            r2_result["claim_ledger"][0]["facts"],
        )
        r2_before_projection = json.loads(json.dumps(r2_result))
        self.assertEqual(r2_before_projection, tools._model_wire_result(r2_result))
        self.assertEqual(r2_before_projection, r2_result)
        r2_sql, r2_params = r2_calls[0]["sql"], r2_calls[0]["params"]
        self.assertIn("`f`.`is_inner_cus` = %s", r2_sql)
        self.assertIn(
            "FROM `vk_dw`.`customer_debt_bymonth_dw` AS `snapshot_f` "
            "WHERE `snapshot_f`.`is_inner_cus` = %s",
            r2_sql,
        )
        self.assertEqual(["n", "n", 101], r2_params)

        r2_filtered_result, r2_filtered_calls = run_query(
            {
                **r2_request,
                "request_id": "r2_user_filter",
                "metric_filters": {"ha_customer": "n"},
            },
            [{"metric_value": "42.00"}],
        )
        self.assertEqual("success", r2_filtered_result["status"])
        r2_filtered_sql = r2_filtered_calls[0]["sql"]
        self.assertIn("`f`.`is_ha_cus` = %s", r2_filtered_sql)
        self.assertNotIn("`snapshot_f`.`is_ha_cus`", r2_filtered_sql)
        self.assertEqual(
            ["n", "n", "n", 101],
            r2_filtered_calls[0]["params"],
        )

        _, positive_calls = run_query(
            {
                **r2_request,
                "request_id": "r2_positive",
                "metric": "positive_debt_amount",
            },
            [{"metric_value": "42.00"}],
        )
        positive_sql, positive_params = (
            positive_calls[0]["sql"],
            positive_calls[0]["params"],
        )
        self.assertIn("`f`.`debt_amount_rmb` > %s", positive_sql)
        self.assertNotIn("`snapshot_f`.`debt_amount_rmb`", positive_sql)
        self.assertEqual(["n", 0, "n", 101], positive_params)

        _, r2_offset_calls = run_query(
            {
                **r2_request,
                "request_id": "r2_snapshot_offset",
                "comparison": {"kind": "snapshot_months_before", "months": 2},
            },
            [
                {
                    "metric_value": "42.00",
                    "comparison_value": "41.00",
                    "delta_value": "1.00",
                    "change_rate": "0.0243902439",
                }
            ],
        )
        r2_offset_sql, r2_offset_params = (
            r2_offset_calls[0]["sql"],
            r2_offset_calls[0]["params"],
        )
        self.assertEqual(2, r2_offset_sql.count("`snapshot_f`.`is_inner_cus` = %s"))
        self.assertEqual(["n", "n", "n", "n", 2, 101], r2_offset_params)

        _, r3_calls = run_query(
            {
                "request_id": "r3_latest_inventory",
                "domain": "inventory",
                "mode": "metric",
                "purpose": "synthetic latest inventory regression",
                "metric": "month_end_inventory_cost_rmb",
                "dimensions": [],
            },
            [{"metric_value": "42.00"}],
        )
        r3_sql, r3_params = r3_calls[0]["sql"], r3_calls[0]["params"]
        self.assertIn(
            "(SELECT MAX(`bill_date`) FROM `vk_dwd`.`inventory_cost_dwd`)",
            r3_sql,
        )
        self.assertNotIn("snapshot_f", r3_sql)
        self.assertEqual([101], r3_params)

        _, r3_non_null_calls = run_query(
            {
                "request_id": "r3_latest_non_null_inventory",
                "domain": "inventory",
                "mode": "metric",
                "purpose": "synthetic latest non-null inventory regression",
                "metric": "oldest_inventory_days",
                "dimensions": [],
            },
            [{"metric_value": "42.00"}],
        )
        r3_non_null_sql = r3_non_null_calls[0]["sql"]
        self.assertIn(
            "(SELECT MAX(`bill_date`) FROM `vk_dwd`.`inventory_cost_dwd` "
            "WHERE `unclosed_days` IS NOT NULL)",
            r3_non_null_sql,
        )
        self.assertNotIn("snapshot_f", r3_non_null_sql)
        self.assertEqual([101], r3_non_null_calls[0]["params"])

        resolver_params: list[object] = []
        resolver_sql = tools._latest_snapshot_resolver_sql(
            "vk_dw.customer_debt_bymonth_dw",
            "bill_date",
            [("is_inner_cus", {"op": "eq", "value": "n"})],
            resolver_params,
            required_non_null="debt_amount_rmb",
        )
        self.assertEqual(
            "SELECT MAX(`snapshot_f`.`bill_date`) "
            "FROM `vk_dw`.`customer_debt_bymonth_dw` AS `snapshot_f` "
            "WHERE `snapshot_f`.`is_inner_cus` = %s "
            "AND `snapshot_f`.`debt_amount_rmb` IS NOT NULL",
            resolver_sql,
        )
        self.assertEqual(["n"], resolver_params)
        self.assertNotIn("`snapshot_f`.`is_ha_cus`", resolver_sql)
        self.assertNotIn("`snapshot_f`.`debt_amount` > %s", resolver_sql)

        formal_dso_disclosure_id = (
            "receivable.formal-receivable-turnover.external-customer.scope"
        )
        formal_dso_coverage_id = (
            "receivable.formal-receivable-turnover.coverage"
        )
        formal_dso_text = "正式应收周转天数的月末净欠款和毛出库分母均固定排除内部客户。"
        formal_dso_formula_id = (
            "receivable.formal-receivable-turnover.formula"
        )
        formal_dso_formula_text = (
            "正式应收周转天数按平均月末净欠款除以同期毛出库金额，再乘期间自然日数计算。"
            "结果表示平均净欠款相当于同期日均毛出库的多少天，可为正、零或负；"
            "负值表示净负余额的折合规模，不代表提前付款天数，具体成因需另查证据；"
            "零值可能来自零余额或正负抵消，不证明没有未收款项。"
        )
        receivable_contract = yaml.safe_load(
            (
                PROFILE_ROOT
                / "plugins/datasage-query/contracts/receivable-semantics.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "datasage-mini-receivable-semantics/v9",
            receivable_contract["version"],
        )
        formal_dso_declarations = receivable_contract["metrics"][
            "formal_receivable_turnover_days"
        ]["disclosures"]
        self.assertIn(
            {
                "id": formal_dso_disclosure_id,
                "mode": "required_always",
                "text": formal_dso_text,
            },
            formal_dso_declarations,
        )
        self.assertIn(
            {
                "id": formal_dso_formula_id,
                "mode": "required_always",
                "text": formal_dso_formula_text,
            },
            formal_dso_declarations,
        )
        self.assertEqual("receivable.external-customer.scope", receivable_contract["default_disclosures"][0]["id"])
        detail = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {
                            "domain": "receivable",
                            "metric": "formal_receivable_turnover_days",
                        }
                    ]
                }
            )
        )
        self.assertEqual("success", detail["status"])
        answer_contract = detail["results"][0]["metric"]["answer_contract"]
        self.assertTrue(answer_contract)
        self.assertTrue(all(isinstance(item, str) and item.strip() for item in answer_contract))
        # Numerical, coverage, seal and public-projection checks below own the
        # validity contract. Catalog prose does not prescribe answer ordering.

        dso_rows = [
            {
                "metric_value": "40.5555555556",
                "average_net_debt_rmb": "100.00",
                "delivery_amount_rmb": "900.00",
                "period_natural_days": 365,
                "snapshot_month_count": 13,
                # MySQL SUM(...) is normalized to a JSON-safe decimal string
                # before the formal DSO attestation is built.
                "effective_month_count": tools._json_value(Decimal("12")),
            }
        ]
        self.assertIsInstance(dso_rows[0]["effective_month_count"], str)
        dso_request = {
            "request_id": "r4_default",
            "domain": "receivable",
            "mode": "metric",
            "purpose": "synthetic formal DSO disclosure test",
            "metric": "formal_receivable_turnover_days",
            "dimensions": [],
        }
        captured_dso_raw: list[dict[str, object]] = []
        original_model_wire_result = tools._model_wire_result

        def capture_dso_raw(result, **projection_kwargs):
            before_projection = json.loads(json.dumps(result))
            projected = original_model_wire_result(result, **projection_kwargs)
            self.assertEqual(before_projection, result)
            captured_dso_raw.append(before_projection)
            return projected

        with mock.patch.object(
            tools,
            "_model_wire_result",
            side_effect=capture_dso_raw,
        ):
            dso_result, dso_calls = run_query(dso_request, dso_rows)
        # One safe checkpoint before expiry, then the final batch projection.
        # Both must preserve the same validated facts and disclosure seals.
        self.assertEqual(2, len(captured_dso_raw))
        self.assertEqual(captured_dso_raw[0]["rows"], captured_dso_raw[1]["rows"])
        for checkpoint in captured_dso_raw:
            assert_disclosure_seals(checkpoint)
            assert_attestation_and_claim_seals(checkpoint)
        dso_raw = captured_dso_raw[0]
        self.assertIn("rows", dso_raw)
        self.assertEqual(
            "verified",
            dso_raw["claim_ledger"][0]["facts"]["calculation_attestation"][
                "status"
            ],
        )
        assert_disclosure_seals(dso_raw)
        assert_attestation_and_claim_seals(dso_raw)
        dso_sql, dso_params = dso_calls[0]["sql"], dso_calls[0]["params"]
        self.assertIn(
            "CASE WHEN COUNT(DISTINCT bill_month) = 13 THEN SUM(", dso_sql
        )
        # Missing-snapshot behavior is executed in the independent analytical integrity fixtures.
        self.assertNotIn("HAVING COUNT(DISTINCT bill_month)", dso_sql)
        self.assertIn("`d`.`is_inner_cus` = %s", dso_sql)
        self.assertIn("`s`.`bill_status` = %s", dso_sql)
        self.assertIn("`s`.`is_inner_cus` = %s", dso_sql)
        self.assertEqual("n", dso_params[0])
        self.assertEqual(6, dso_params[5])
        self.assertEqual("n", dso_params[6])
        assert_disclosure_seals(dso_result)
        dso_disclosures = {
            item["disclosure_id"]: item for item in dso_result["disclosure_ledger"]
        }
        self.assertEqual(
            {"mode": "required_always", "text": formal_dso_text, "applies": True},
            {
                key: dso_disclosures[formal_dso_disclosure_id][key]
                for key in ("mode", "text", "applies")
            },
        )
        dso_wire = tools._model_wire_result(dso_result)
        self.assertNotIn("rows", dso_wire)
        self.assertLessEqual(set(dso_wire), set(tools._MODEL_WIRE_RESULT_FIELDS))
        self.assertEqual(dso_result["disclosure_ledger"], dso_wire["disclosure_ledger"])
        self.assertEqual(
            dso_result["disclosure_ledger_seal"],
            dso_wire["disclosure_ledger_seal"],
        )
        self.assertEqual(dso_result, dso_wire)
        attestation = assert_attestation_and_claim_seals(dso_result)
        self.assertEqual(
            "formal-receivable-turnover-calculation-attestation/v2",
            attestation["contract_version"],
        )
        self.assertEqual("verified", attestation["status"])
        self.assertEqual([], attestation["undefined_reason_codes"])
        self.assertTrue(all(attestation["guards"].values()))
        self.assertEqual(
            [
                "formal_receivable_turnover_value",
                "average_net_debt",
                "same_period_gross_delivery_amount",
                "gross_delivery_denominator_semantics",
                "period_natural_days",
                "snapshot_month_count",
                "effective_month_count",
            ],
            attestation["authorized_components"],
        )
        expected_component_values = {
            "metric_value": "40.5555555556",
            "average_net_debt_rmb": "100.00",
            "same_period_gross_delivery_rmb": "900.00",
            "period_natural_days": 365,
            "snapshot_month_count": 13,
            "effective_month_count": "12",
        }
        self.assertEqual(
            expected_component_values,
            attestation["component_values"],
        )
        self.assertEqual(
            "900.00",
            dso_result["claim_ledger"][0]["facts"][
                "same_period_gross_delivery_rmb"
            ],
        )
        self.assertNotIn(
            "delivery_amount_rmb",
            dso_result["claim_ledger"][0]["facts"],
        )
        dso_facts = dso_wire["claim_ledger"][0]["facts"]
        self.assertEqual(
            {
                "metric_value",
                "average_net_debt_rmb",
                "same_period_gross_delivery_rmb",
                "period_natural_days",
                "snapshot_month_count",
                "effective_month_count",
                "calculation_attestation",
            },
            set(dso_facts),
        )
        serialized_attestation = json.dumps(attestation, ensure_ascii=False)
        for implementation_token in (
            "delivery_amount_rmb",
            "SELECT ",
            "vk_dw",
            "vk_dwd",
            "`d`",
            "`s`",
        ):
            self.assertNotIn(implementation_token, serialized_attestation)
        serialized_wire = json.dumps(dso_wire, ensure_ascii=False)
        for private_token in (
            "delivery_amount_rmb",
            "debt_amount_rmb",
            "is_inner_cus",
            "bill_status",
            "vk_dw",
            "vk_dwd",
            "SELECT ",
            " FROM ",
        ):
            self.assertNotIn(private_token, serialized_wire)
        self.assertIn("900.00", serialized_wire)
        self.assertIn("same_period_gross_delivery_rmb", serialized_wire)

        def assert_attestation_tamper_breaks_both_seals(
            label: str, mutate
        ) -> None:
            tampered_claim = json.loads(
                json.dumps(dso_result["claim_ledger"][0], ensure_ascii=False)
            )
            tampered_attestation = tampered_claim["facts"][
                "calculation_attestation"
            ]
            mutate(tampered_attestation)
            self.assertNotEqual(
                expected_attestation_seal(tampered_attestation),
                tampered_attestation["attestation_seal"],
                label,
            )
            tampered_id, tampered_seal = tools.evidence._canonical_claim_identity(
                tampered_claim
            )
            self.assertNotEqual(
                dso_result["claim_ledger"][0]["claim_id"], tampered_id, label
            )
            self.assertNotEqual(
                dso_result["claim_ledger"][0]["claim_seal"], tampered_seal, label
            )

        attestation_field_mutations = {
            "contract_version": lambda item: item.__setitem__(
                "contract_version", "tampered"
            ),
            "status": lambda item: item.__setitem__("status", "undefined"),
            "guards": lambda item: item["guards"].__setitem__(
                "metric_value_present", False
            ),
            "authorized_components": lambda item: item[
                "authorized_components"
            ].pop(),
            "component_values": lambda item: item["component_values"].__setitem__(
                "average_net_debt_rmb", "101.00"
            ),
            "undefined_reason_codes": lambda item: item[
                "undefined_reason_codes"
            ].append("TAMPERED"),
            "request_id": lambda item: item.__setitem__("request_id", "tampered"),
            "metric_ref": lambda item: item.__setitem__("metric_ref", "tampered"),
            "scope_fingerprint": lambda item: item.__setitem__(
                "scope_fingerprint", "tampered"
            ),
            "projection_fingerprint": lambda item: item.__setitem__(
                "projection_fingerprint", "tampered"
            ),
            "attestation_seal": lambda item: item.__setitem__(
                "attestation_seal", "sha256_" + "0" * 64
            ),
        }
        for field, mutate in attestation_field_mutations.items():
            assert_attestation_tamper_breaks_both_seals(field, mutate)
        for guard in attestation["guards"]:
            assert_attestation_tamper_breaks_both_seals(
                f"guards.{guard}",
                lambda item, key=guard: item["guards"].__setitem__(key, False),
            )

        def assert_tamper_is_fail_closed(
            tampered_raw: dict[str, object],
        ) -> None:
            before_projection = json.loads(json.dumps(tampered_raw))
            projected = tools._model_wire_result(tampered_raw)
            self.assertEqual(before_projection, tampered_raw)
            self.assertEqual("undefined", projected["data_state"])
            self.assertEqual([], projected["claim_ledger"])
            self.assertEqual(0, projected["row_count"])
            self.assertNotIn("allowed_reasoning_topics", projected)
            self.assertEqual(
                "EVIDENCE_INTEGRITY_INVALID",
                projected["error"]["code"],
            )
            projected_disclosure_ids = {
                item["disclosure_id"] for item in projected["disclosure_ledger"]
            }
            self.assertIn(formal_dso_coverage_id, projected_disclosure_ids)
            self.assertIn(formal_dso_disclosure_id, projected_disclosure_ids)
            self.assertNotIn(formal_dso_formula_id, projected_disclosure_ids)
            self.assertEqual(
                "metric-disclosure-ledger-model-projection/v1",
                projected["disclosure_contract_version"],
            )
            assert_disclosure_seals(projected)
            self.assertNotIn(
                formal_dso_formula_text,
                json.dumps(projected, ensure_ascii=False),
            )

        claim_integrity_tamper = json.loads(json.dumps(dso_result))
        claim_integrity_tamper["claim_ledger"][0]["claim_seal"] = (
            "sha256_" + "0" * 64
        )
        assert_tamper_is_fail_closed(
            claim_integrity_tamper,
        )

        attestation_integrity_tamper = json.loads(json.dumps(dso_result))
        attestation_integrity_tamper["claim_ledger"][0]["facts"][
            "calculation_attestation"
        ]["status"] = "undefined"
        tools.evidence.seal_claim(attestation_integrity_tamper["claim_ledger"][0])
        assert_tamper_is_fail_closed(
            attestation_integrity_tamper,
        )

        attestation_missing = json.loads(json.dumps(dso_result))
        del attestation_missing["claim_ledger"][0]["facts"][
            "calculation_attestation"
        ]
        tools.evidence.seal_claim(attestation_missing["claim_ledger"][0])
        assert_tamper_is_fail_closed(
            attestation_missing,
        )

        attestation_semantic_tamper = json.loads(json.dumps(dso_result))
        semantic_attestation = attestation_semantic_tamper["claim_ledger"][0][
            "facts"
        ]["calculation_attestation"]
        semantic_attestation["status"] = "partial"
        semantic_attestation["attestation_seal"] = expected_attestation_seal(
            semantic_attestation
        )
        tools.evidence.seal_claim(
            attestation_semantic_tamper["claim_ledger"][0]
        )
        assert_tamper_is_fail_closed(
            attestation_semantic_tamper,
        )

        component_replacements = {
            "metric_value": "43.00",
            "average_net_debt_rmb": "101.00",
            "same_period_gross_delivery_rmb": "901.00",
            "period_natural_days": 364,
            "snapshot_month_count": 14,
            "effective_month_count": "13",
        }
        for fact_name, replacement in component_replacements.items():
            component_missing = json.loads(json.dumps(dso_result))
            del component_missing["claim_ledger"][0]["facts"][fact_name]
            tools.evidence.seal_claim(component_missing["claim_ledger"][0])
            assert_tamper_is_fail_closed(component_missing)

            component_replaced = json.loads(json.dumps(dso_result))
            component_replaced["claim_ledger"][0]["facts"][
                fact_name
            ] = replacement
            tools.evidence.seal_claim(component_replaced["claim_ledger"][0])
            assert_tamper_is_fail_closed(component_replaced)

        attestation_value_bare_tamper = json.loads(json.dumps(dso_result))
        bare_tamper_claim = attestation_value_bare_tamper["claim_ledger"][0]
        bare_tamper_claim["facts"]["calculation_attestation"][
            "component_values"
        ]["average_net_debt_rmb"] = "101.00"
        tools.evidence.seal_claim(bare_tamper_claim)
        assert_tamper_is_fail_closed(attestation_value_bare_tamper)

        attestation_invalid_reseal = json.loads(json.dumps(dso_result))
        invalid_reseal_claim = attestation_invalid_reseal["claim_ledger"][0]
        invalid_reseal_attestation = invalid_reseal_claim["facts"][
            "calculation_attestation"
        ]
        invalid_reseal_attestation["component_values"][
            "same_period_gross_delivery_rmb"
        ] = "-1"
        invalid_reseal_claim["facts"]["same_period_gross_delivery_rmb"] = "-1"
        invalid_reseal_attestation[
            "attestation_seal"
        ] = expected_attestation_seal(invalid_reseal_attestation)
        tools.evidence.seal_claim(invalid_reseal_claim)
        assert_tamper_is_fail_closed(attestation_invalid_reseal)

        period_binding_tamper = json.loads(json.dumps(dso_result))
        period_binding_tamper["claim_ledger"][0]["period"] = {
            "source": "requested_period",
            "start": "2023-02-01",
            "end": "2024-01-31",
        }
        tools.evidence.seal_claim(period_binding_tamper["claim_ledger"][0])
        assert_tamper_is_fail_closed(period_binding_tamper)

        ledger_seal_tamper = json.loads(json.dumps(dso_result))
        assert_attestation_and_claim_seals(ledger_seal_tamper)
        for disclosure in ledger_seal_tamper["disclosure_ledger"]:
            canonical = json.dumps(
                {
                    key: value
                    for key, value in disclosure.items()
                    if key != "disclosure_seal"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            self.assertEqual(
                "sha256_" + hashlib.sha256(canonical).hexdigest(),
                disclosure["disclosure_seal"],
            )
        ledger_seal_tamper["disclosure_ledger_seal"] = "sha256_" + "0" * 64
        ledger_seal_tamper_before = json.loads(json.dumps(ledger_seal_tamper))
        ledger_seal_tamper_wire = tools._model_wire_result(ledger_seal_tamper)
        self.assertEqual(ledger_seal_tamper_before, ledger_seal_tamper)
        self.assertEqual("undefined", ledger_seal_tamper_wire["data_state"])
        self.assertEqual([], ledger_seal_tamper_wire["claim_ledger"])
        self.assertEqual(0, ledger_seal_tamper_wire["row_count"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            ledger_seal_tamper_wire["error"]["code"],
        )
        self.assertEqual([], ledger_seal_tamper_wire["disclosure_ledger"])
        self.assertEqual(
            "metric-disclosure-ledger-model-projection/v1",
            ledger_seal_tamper_wire["disclosure_contract_version"],
        )
        assert_disclosure_seals(ledger_seal_tamper_wire)
        self.assertNotIn(
            formal_dso_formula_text,
            json.dumps(ledger_seal_tamper_wire, ensure_ascii=False),
        )

        undefined_cases = (
            (
                "missing_metric_value",
                {key: value for key, value in dso_rows[0].items() if key != "metric_value"},
                "METRIC_VALUE_PRESENT",
            ),
            (
                "missing_average",
                {key: value for key, value in dso_rows[0].items() if key != "average_net_debt_rmb"},
                "AVERAGE_NET_DEBT_PRESENT",
            ),
            (
                "missing_denominator",
                {key: value for key, value in dso_rows[0].items() if key != "delivery_amount_rmb"},
                "GROSS_DELIVERY_DENOMINATOR_PRESENT",
            ),
            (
                "null_denominator",
                {**dso_rows[0], "delivery_amount_rmb": None},
                "GROSS_DELIVERY_DENOMINATOR_PRESENT",
            ),
            (
                "non_finite_denominator",
                {**dso_rows[0], "delivery_amount_rmb": "NaN"},
                "GROSS_DELIVERY_DENOMINATOR_PRESENT",
            ),
            (
                "non_positive_denominator",
                {**dso_rows[0], "delivery_amount_rmb": "0"},
                "GROSS_DELIVERY_DENOMINATOR_POSITIVE",
            ),
            (
                "natural_days_mismatch",
                {**dso_rows[0], "period_natural_days": 364},
                "PERIOD_MATCHES_COMPLETE_WINDOW",
            ),
            (
                "snapshot_incomplete",
                {**dso_rows[0], "snapshot_month_count": 12},
                "COMPLETE_MONTH_END_SNAPSHOTS",
            ),
            (
                "effective_months_exceed_window",
                {**dso_rows[0], "effective_month_count": "13"},
                "EFFECTIVE_MONTH_COUNT_WITHIN_WINDOW",
            ),
            (
                "effective_months_fractional",
                {**dso_rows[0], "effective_month_count": "12.5"},
                "EFFECTIVE_MONTH_COUNT_WITHIN_WINDOW",
            ),
            (
                "effective_months_boolean",
                {**dso_rows[0], "effective_month_count": True},
                "EFFECTIVE_MONTH_COUNT_WITHIN_WINDOW",
            ),
        )
        for case_name, row, reason in undefined_cases:
            captured_undefined_raw: list[dict[str, object]] = []

            def capture_undefined_raw(raw_result, **projection_kwargs):
                before_projection = json.loads(json.dumps(raw_result))
                projected = original_model_wire_result(
                    raw_result,
                    **projection_kwargs,
                )
                self.assertEqual(before_projection, raw_result)
                captured_undefined_raw.append(before_projection)
                return projected

            context = (
                mock.patch.object(
                    tools,
                    "_model_wire_result",
                    side_effect=capture_undefined_raw,
                )
                if case_name == "effective_months_exceed_window"
                else mock.patch.object(
                    tools,
                    "_model_wire_result",
                    side_effect=original_model_wire_result,
                )
            )
            with context:
                result, calls = run_query(
                    {**dso_request, "request_id": f"r4_{case_name}"},
                    [row],
                )
            self.assertEqual(1, len(calls), case_name)
            case_attestation = assert_attestation_and_claim_seals(result)
            self.assertEqual("success", result["status"], case_name)
            self.assertIsNone(result["error"], case_name)
            self.assertEqual("undefined", result["data_state"], case_name)
            self.assertEqual("undefined", case_attestation["status"], case_name)
            self.assertEqual([], case_attestation["authorized_components"], case_name)
            self.assertEqual({}, case_attestation["component_values"], case_name)
            self.assertIn(reason, case_attestation["undefined_reason_codes"], case_name)
            self.assertNotIn(
                "metric_value",
                result["claim_ledger"][0]["facts"],
                case_name,
            )
            self.assertNotIn(
                "same_period_gross_delivery_rmb",
                result["claim_ledger"][0]["facts"],
                case_name,
            )
            self.assertNotIn("allowed_reasoning_topics", result, case_name)
            case_disclosure_ids = {
                item["disclosure_id"] for item in result["disclosure_ledger"]
            }
            self.assertIn(formal_dso_coverage_id, case_disclosure_ids, case_name)
            self.assertIn(formal_dso_disclosure_id, case_disclosure_ids, case_name)
            self.assertNotIn(formal_dso_formula_id, case_disclosure_ids, case_name)
            self.assertEqual(
                "metric-disclosure-ledger-model-projection/v1",
                result["disclosure_contract_version"],
                case_name,
            )
            self.assertNotIn(
                formal_dso_formula_text,
                json.dumps(result, ensure_ascii=False),
                case_name,
            )
            assert_disclosure_seals(result)
            self.assertEqual(result, tools._model_wire_result(result), case_name)
            if case_name == "effective_months_exceed_window":
                self.assertEqual(2, len(captured_undefined_raw))
                raw_result = captured_undefined_raw[0]
                raw_facts = raw_result["claim_ledger"][0]["facts"]
                self.assertIn("metric_value", raw_facts)
                self.assertEqual(
                    "undefined",
                    raw_facts["calculation_attestation"]["status"],
                )
                self.assertIn(
                    formal_dso_formula_id,
                    {
                        item["disclosure_id"]
                        for item in raw_result["disclosure_ledger"]
                    },
                )
                self.assertEqual(
                    "metric-disclosure-ledger/v1",
                    raw_result["disclosure_contract_version"],
                )
                self.assertNotEqual(
                    raw_result["disclosure_ledger_seal"],
                    result["disclosure_ledger_seal"],
                )
                assert_disclosure_seals(raw_result)
                assert_attestation_and_claim_seals(raw_result)
                for retained_fact in (
                    "average_net_debt_rmb",
                    "period_natural_days",
                    "snapshot_month_count",
                    "effective_month_count",
                ):
                    self.assertIn(
                        retained_fact,
                        result["claim_ledger"][0]["facts"],
                    )

        missing_period_result, missing_period_calls = run_query(
            {**dso_request, "request_id": "r4_missing_period_natural_days"},
            [
                {
                    key: value
                    for key, value in dso_rows[0].items()
                    if key != "period_natural_days"
                }
            ],
        )
        self.assertEqual(1, len(missing_period_calls))
        missing_period_attestation = assert_attestation_and_claim_seals(
            missing_period_result
        )
        self.assertEqual("undefined", missing_period_attestation["status"])
        self.assertEqual([], missing_period_attestation["authorized_components"])
        self.assertIn(
            "PERIOD_NATURAL_DAYS_PRESENT",
            missing_period_attestation["undefined_reason_codes"],
        )
        missing_period_wire = tools._model_wire_result(missing_period_result)
        self.assertNotIn("rows", missing_period_wire)
        missing_period_serialized = json.dumps(
            missing_period_wire,
            ensure_ascii=False,
        )
        for private_token in (
            "900.00",
            "delivery_amount_rmb",
            "debt_amount_rmb",
            "is_inner_cus",
            "bill_status",
            "vk_dw",
            "vk_dwd",
            "SELECT ",
            " FROM ",
        ):
            self.assertNotIn(private_token, missing_period_serialized)

        short_window_result, short_window_calls = run_query(
            {
                **dso_request,
                "request_id": "r4_short_window",
                "time_range": {"start": "2025-08-01", "end": "2026-07-01"},
            },
            [{**dso_rows[0], "period_natural_days": 334}],
        )
        self.assertEqual(1, len(short_window_calls))
        short_window_attestation = assert_attestation_and_claim_seals(
            short_window_result
        )
        self.assertEqual("undefined", short_window_attestation["status"])
        self.assertNotIn(
            "COMPLETE_NATURAL_MONTH_WINDOW",
            short_window_attestation["undefined_reason_codes"],
        )
        self.assertIn(
            "COMPLETE_MONTH_END_SNAPSHOTS",
            short_window_attestation["undefined_reason_codes"],
        )

        original_disclosure_ledger = tools._disclosure_ledger

        def disclosure_fault(disclosure_id, mode):
            def apply_fault(**kwargs):
                ledger, _seal = original_disclosure_ledger(**kwargs)
                altered = [dict(item) for item in ledger]
                if mode == "missing":
                    altered = [
                        item
                        for item in altered
                        if item["disclosure_id"] != disclosure_id
                    ]
                elif mode == "invalid_item_seal":
                    for item in altered:
                        if item["disclosure_id"] == disclosure_id:
                            item["disclosure_seal"] = "sha256_" + "0" * 64
                else:
                    raise AssertionError(mode)
                canonical = json.dumps(
                    {
                        "contract_version": "metric-disclosure-ledger/v1",
                        "request_id": str(kwargs["request"]["request_id"]),
                        "metric_ref": kwargs["metric_ref"],
                        "scope_fingerprint": kwargs["scope_fingerprint"],
                        "projection_fingerprint": kwargs["projection_fingerprint"],
                        "ledger": altered,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                return altered, "sha256_" + hashlib.sha256(canonical).hexdigest()

            return apply_fault

        disclosure_failures = (
            (
                "coverage_missing",
                formal_dso_coverage_id,
                "missing",
                "COVERAGE_DISCLOSURE_SEALED",
            ),
            (
                "coverage_seal_invalid",
                formal_dso_coverage_id,
                "invalid_item_seal",
                "COVERAGE_DISCLOSURE_SEALED",
            ),
            (
                "scope_missing",
                formal_dso_disclosure_id,
                "missing",
                "BOTH_EXTERNAL_CUSTOMER_SCOPES_DISCLOSED",
            ),
            (
                "scope_seal_invalid",
                formal_dso_disclosure_id,
                "invalid_item_seal",
                "BOTH_EXTERNAL_CUSTOMER_SCOPES_DISCLOSED",
            ),
            (
                "formula_missing",
                formal_dso_formula_id,
                "missing",
                "FORMULA_DISCLOSURE_SEALED",
            ),
            (
                "formula_seal_invalid",
                formal_dso_formula_id,
                "invalid_item_seal",
                "FORMULA_DISCLOSURE_SEALED",
            ),
        )
        for case_name, disclosure_id, mode, reason in disclosure_failures:
            with mock.patch.object(
                tools,
                "_disclosure_ledger",
                side_effect=disclosure_fault(disclosure_id, mode),
            ):
                result, calls = run_query(
                    {**dso_request, "request_id": f"r4_{case_name}"},
                    dso_rows,
                )
            self.assertEqual(1, len(calls), case_name)
            if mode == "invalid_item_seal":
                self.assertEqual([], result["claim_ledger"], case_name)
                self.assertEqual("undefined", result["data_state"], case_name)
                self.assertEqual(
                    "EVIDENCE_INTEGRITY_INVALID",
                    result["error"]["code"],
                    case_name,
                )
                continue
            case_attestation = assert_attestation_and_claim_seals(result)
            self.assertEqual("success", result["status"], case_name)
            self.assertIsNone(result["error"], case_name)
            self.assertEqual("undefined", case_attestation["status"], case_name)
            self.assertEqual([], case_attestation["authorized_components"], case_name)
            self.assertIn(reason, case_attestation["undefined_reason_codes"], case_name)

        for request in (
            {
                **dso_request,
                "request_id": "r4_grouped",
                "dimensions": ["department"],
            },
            {
                **dso_request,
                "request_id": "r4_filtered",
                "metric_filters": {"department": "HCM"},
            },
        ):
            result, _ = run_query(request, dso_rows)
            disclosures = {
                item["disclosure_id"]: item for item in result["disclosure_ledger"]
            }
            self.assertIs(disclosures[formal_dso_disclosure_id]["applies"], True)
            assert_disclosure_seals(result)

        delivery_receipt_result, _ = run_query(
            {
                "request_id": "delivery_receipt_comparison",
                "domain": "receipt",
                "mode": "metric",
                "purpose": "synthetic comparison scope regression",
                "metric": "delivery_receipt_comparison",
                "dimensions": [],
            },
            [],
        )
        delivery_receipt_ids = {
            item["disclosure_id"]
            for item in delivery_receipt_result["disclosure_ledger"]
        }
        self.assertIn("receipt.delivery-receipt.scope-asymmetry", delivery_receipt_ids)
        self.assertNotIn(formal_dso_disclosure_id, delivery_receipt_ids)

        main_skill = _main_skill()
        answer_policy = _answer_boundary()
        query_policy = _query_rules()
        self.assertLess(len(main_skill), 6_000)
        self.assertIn("Preserve typed states", answer_policy)
        self.assertIn("Never invent or", query_policy)
        self.assertIn("substitute a metric", query_policy)
        self.assertNotIn("formal-receivable-turnover-calculation-attestation/v2", main_skill)

    def test_delivery_internal_customer_exclusion_is_sealed_and_model_visible(
        self,
    ) -> None:
        semantics = yaml.safe_load(
            (
                PLUGIN_ROOT / "contracts" / "delivery-semantics.yaml"
            ).read_text(encoding="utf-8")
        )
        metrics = semantics["metrics"]

        def excludes_internal_customer(metric_code: str) -> bool:
            metric = metrics[metric_code]
            fixed = (metric.get("required_filters") or {}).get("is_inner_cus")
            if fixed == {"op": "eq", "value": "n"}:
                return True
            components = metric.get("components")
            if isinstance(components, list) and components:
                return all(
                    excludes_internal_customer(component["metric"])
                    for component in components
                )
            ratio = metric.get("ratio")
            if isinstance(ratio, dict):
                return all(
                    excludes_internal_customer(ratio[key])
                    for key in ("numerator", "denominator")
                )
            return False

        self.assertTrue(metrics)
        self.assertTrue(
            all(excludes_internal_customer(code) for code in metrics),
            "域级披露只能覆盖全部执行路径都固定排除内部客户的指标",
        )
        inherited = semantics["default_disclosures"]
        self.assertEqual(
            [
                {
                    "id": "delivery.external-customer.scope",
                    "mode": "required_always",
                    "text": "出库域指标固定排除内部客户。",
                },
                {
                    "id": "delivery.department.roles",
                    "mode": "required_when",
                    "when": {
                        "any_request_dimension_or_filter_present": [
                            "department",
                            "business_department",
                            "business_region",
                        ]
                    },
                    "text": "用户只说部门时使用默认归属部门；业务发生部门和业务发生地区为独立交易归属，不能与默认部门互相替代。",
                },
            ],
            inherited,
        )

        cases = (
            ("delivery_amount", {"metric_value": "80.00"}),
            (
                "return_amount_rate",
                {
                    "metric_value": "0.2",
                    "numerator_value": "20.00",
                    "denominator_value": "100.00",
                },
            ),
        )
        for metric_code, row in cases:
            with self.subTest(metric=metric_code):
                request_id = f"delivery_external_scope_{metric_code}"
                request = {
                    "request_id": request_id,
                    "domain": "delivery",
                    "mode": "metric",
                    "purpose": "synthetic external-customer scope test",
                    "metric": metric_code,
                    "dimensions": [],
                    "calendar_month": "2026-07",
                }
                calls: list[dict[str, object]] = []

                def execute_query(sql, params, limit, **_kwargs):
                    calls.append({"sql": sql, "params": params, "limit": limit})
                    return (
                        [{**row, tools._INTERNAL_MATCH_COUNT: 1}],
                        False,
                        self._read_only_source_evidence(),
                    )

                with mock.patch.object(
                    tools,
                    "_execute_with_source",
                    side_effect=execute_query,
                ):
                    payload = json.loads(
                        tools.datasage_query({"requests": [request]})
                    )

                self.assertEqual("success", payload["status"])
                self.assertEqual(1, len(calls))
                self.assertGreaterEqual(
                    str(calls[0]["sql"]).count("`f`.`is_inner_cus` = %s"),
                    2,
                )
                result = payload["results"][0]
                disclosures = {
                    item["disclosure_id"]: item
                    for item in result["disclosure_ledger"]
                }
                scope = disclosures["delivery.external-customer.scope"]
                self.assertIs(scope["applies"], True)
                self.assertEqual("required_always", scope["mode"])
                self.assertEqual("出库域指标固定排除内部客户。", scope["text"])
                self.assertTrue(
                    tools._disclosure_ledger_has_valid_seal(
                        result["disclosure_ledger"],
                        result["disclosure_ledger_seal"],
                        request_id=result["request_id"],
                        metric_ref=result["business_metric_ref"],
                        scope_fingerprint=result["scope_fingerprint"],
                        projection_fingerprint=result["projection_fingerprint"],
                        ledger_contract_version=result[
                            "disclosure_contract_version"
                        ],
                    )
                )
                model_text = json.dumps(result, ensure_ascii=False)
                self.assertIn("排除内部客户", model_text)
                self.assertNotIn("is_inner_cus", model_text)
                self.assertNotIn("vk_dwd", model_text)

    def test_delivery_time_range_defers_to_query_policy_authority(self) -> None:
        delivery = yaml.safe_load(
            (
                PLUGIN_ROOT / "contracts" / "delivery-semantics.yaml"
            ).read_text(encoding="utf-8")
        )
        policy = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "query-policy.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("answer_rules", delivery)
        time_policy = policy["governed_metric_time_range"]
        self.assertEqual(
            "split_into_independently_bounded_periods",
            time_policy["wider_analysis"],
        )
        catalog = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "delivery", "metric": "delivery_amount"}]}
            )
        )
        self.assertEqual(
            time_policy,
            catalog["query_policy"]["governed_metric_time_range"],
        )
        description = schemas.REQUEST["properties"]["time_range"]["description"]
        self.assertIn("Start-inclusive", description)
        self.assertIn("end-exclusive", description)

    def test_snapshot_month_evidence_and_typed_states_are_model_safe(self) -> None:
        def run_query(
            request: dict[str, object],
            rows: list[dict[str, object]],
        ) -> tuple[dict[str, object], dict[str, object], str]:
            calls: list[str] = []

            def execute_query(sql, _params, _limit, **_kwargs):
                calls.append(sql)
                return rows, False, self._read_only_source_evidence()

            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(tools.datasage_query({"requests": [request]}))
            self.assertEqual(1, len(calls))
            result = payload["results"][0] if payload["results"] else payload
            return payload, result, calls[0]

        latest_request = {
            "request_id": "inventory_latest_month_evidence",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "synthetic actual snapshot month test",
            "metric": "month_end_inventory_cost_rmb",
            "dimensions": [],
        }
        latest_payload, latest_result, latest_sql = run_query(
            latest_request,
            [
                {
                    "metric_value": "42.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_SNAPSHOT_MONTH: "2026-07",
                }
            ],
        )
        self.assertIn(
            "MAX(`f`.`bill_date`) AS `__snapshot_month`",
            latest_sql,
        )
        self.assertEqual(
            {
                "source": "latest_snapshot",
                "snapshot_month": "2026-07",
                "resolution_state": "resolved",
            },
            latest_result["applied_time_range"],
        )
        latest_claim = latest_result["claim_ledger"][0]
        self.assertEqual(
            latest_result["applied_time_range"],
            latest_claim["period"],
        )
        self.assertTrue(
            tools.evidence.claim_is_valid_for_result(latest_claim, latest_result)
        )
        self.assertEqual("查询范围：2026-07 业务月度快照", latest_payload["answer_scope_line"])
        self.assertNotIn("__snapshot_month", json.dumps(latest_result))

        non_null_payload, non_null_result, non_null_sql = run_query(
            {
                **latest_request,
                "request_id": "inventory_latest_non_null_month_evidence",
                "metric": "oldest_inventory_days",
            },
            [
                {
                    "metric_value": 120,
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_SNAPSHOT_MONTH: "2026-06-01",
                }
            ],
        )
        self.assertIn("`unclosed_days` IS NOT NULL", non_null_sql)
        self.assertEqual(
            {
                "source": "latest_non_null_snapshot",
                "snapshot_month": "2026-06",
                "resolution_state": "resolved",
            },
            non_null_result["applied_time_range"],
        )
        safe_wire = json.dumps(non_null_payload, ensure_ascii=False)
        self.assertNotIn("required_measure", safe_wire)
        self.assertNotIn("unclosed_days", safe_wire)

        explicit_payload, explicit_result, _ = run_query(
            {
                **latest_request,
                "request_id": "inventory_explicit_month",
                "calendar_month": "2026-05",
            },
            [{"metric_value": "12.00", tools._INTERNAL_MATCH_COUNT: 1}],
        )
        self.assertEqual("rows", explicit_result["data_state"])
        self.assertEqual("explicit", explicit_result["applied_time_range"]["source"])
        self.assertNotIn("resolution_state", explicit_result["applied_time_range"])
        self.assertEqual("查询范围：2026-05 至 2026-05", explicit_payload["answer_scope_line"])

        _, empty_latest, _ = run_query(
            {
                **latest_request,
                "request_id": "inventory_latest_no_data",
            },
            [
                {
                    "metric_value": None,
                    tools._INTERNAL_MATCH_COUNT: 0,
                    tools._INTERNAL_SNAPSHOT_MONTH: None,
                    "metric_data_state": "missing",
                }
            ],
        )
        self.assertEqual("empty", empty_latest["data_state"])
        self.assertEqual(
            "no_snapshot_data",
            empty_latest["applied_time_range"]["resolution_state"],
        )
        self.assertEqual([], empty_latest["claim_ledger"])

        required_empty_payload, required_empty, _ = run_query(
            {
                **latest_request,
                "request_id": "inventory_required_value_empty",
                "metric": "oldest_inventory_days",
            },
            [
                {
                    "metric_value": None,
                    tools._INTERNAL_MATCH_COUNT: 0,
                    tools._INTERNAL_SNAPSHOT_MONTH: None,
                    "metric_data_state": "missing",
                }
            ],
        )
        self.assertEqual("undefined", required_empty["data_state"])
        self.assertEqual(
            "required_value_unavailable",
            required_empty["applied_time_range"]["resolution_state"],
        )
        required_empty_wire = json.dumps(required_empty_payload, ensure_ascii=False)
        self.assertNotIn("required_measure", required_empty_wire)
        self.assertNotIn("unclosed_days", required_empty_wire)

        comparison_payload, comparison_result, comparison_sql = run_query(
            {
                "request_id": "receivable_snapshot_comparison_months",
                "domain": "receivable",
                "mode": "metric",
                "purpose": "synthetic snapshot comparison month test",
                "metric": "current_debt_amount",
                "dimensions": [],
                "comparison": {"kind": "snapshot_months_before", "months": 2},
            },
            [
                {
                    "metric_value": "42.00",
                    "comparison_value": "41.00",
                    "delta_value": "1.00",
                    "change_rate": "0.0243902439",
                    tools._INTERNAL_MATCH_COUNT: 2,
                    tools._INTERNAL_SNAPSHOT_MONTH: "2026-07",
                    tools._INTERNAL_COMPARISON_SNAPSHOT_MONTH: "2026-05",
                }
            ],
        )
        self.assertIn("AS `__comparison_snapshot_month`", comparison_sql)
        self.assertEqual(
            "2026-07",
            comparison_result["applied_time_range"]["current"]["snapshot_month"],
        )
        self.assertEqual(
            "2026-05",
            comparison_result["applied_time_range"]["comparison"]["snapshot_month"],
        )
        comparison_wire = json.dumps(comparison_payload, ensure_ascii=False)
        self.assertNotIn("__snapshot_month", comparison_wire)
        self.assertNotIn("bill_date", comparison_wire)

        malformed_payload, malformed_result, _ = run_query(
            {
                **latest_request,
                "request_id": "inventory_malformed_snapshot_evidence",
            },
            [
                {
                    "metric_value": "42.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_SNAPSHOT_MONTH: "July 2026",
                }
            ],
        )
        self.assertEqual("failed", malformed_result["status"])
        self.assertNotIn("July 2026", json.dumps(malformed_payload))
        with self.assertRaises(tools.QueryFailure) as malformed_evidence:
            tools._resolve_snapshot_time_evidence(
                {"source": "latest_snapshot"},
                [
                    {
                        tools._INTERNAL_SNAPSHOT_MONTH: "July 2026",
                    }
                ],
                "rows",
            )
        self.assertEqual(
            "CONTRACT_UNAVAILABLE",
            malformed_evidence.exception.code,
        )

    def test_overdue_current_snapshot_as_of_date_is_same_query_and_fail_closed(
        self,
    ) -> None:
        base_request = {
            "domain": "receivable",
            "mode": "metric",
            "purpose": "synthetic current snapshot as-of date test",
            "metric": "overdue_receivable_amount",
            "dimensions": [],
        }

        def run_query(
            request_id: str,
            rows: list[dict[str, object]],
            *,
            metric: str = "overdue_receivable_amount",
        ) -> tuple[dict[str, object], dict[str, object], list[str]]:
            sql_calls: list[str] = []

            def execute_query(sql, _params, _limit, **_kwargs):
                sql_calls.append(sql)
                return rows, False, self._read_only_source_evidence()

            request = {
                **base_request,
                "request_id": request_id,
                "metric": metric,
            }
            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(tools.datasage_query({"requests": [request]}))
            result = payload["results"][0] if payload.get("results") else payload
            return payload, result, sql_calls

        semantics = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "receivable-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("datasage-mini-receivable-semantics/v9", semantics["version"])
        current_snapshot_evidence_metrics = {
            metric_code
            for metric_code, definition in semantics["metrics"].items()
            if definition.get("current_snapshot_evidence") is not None
        }
        self.assertEqual(
            {"overdue_receivable_amount"},
            current_snapshot_evidence_metrics,
        )
        self.assertEqual(
            "database_current_date",
            semantics["metrics"]["overdue_receivable_amount"][
                "current_snapshot_evidence"
            ],
        )
        detail = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {
                            "domain": "receivable",
                            "metric": "overdue_receivable_amount",
                        }
                    ]
                }
            )
        )
        serialized_detail = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("current_snapshot_evidence", serialized_detail)
        self.assertNotIn("database_current_date", serialized_detail)

        for index, raw_date in enumerate(
            (date(2026, 8, 18), datetime(2026, 8, 18, 12, 30, 45))
        ):
            with self.subTest(valid_date_type=type(raw_date).__name__):
                payload, result, sql_calls = run_query(
                    f"overdue_valid_as_of_{index}",
                    [
                        {
                            "metric_value": "42.00",
                            tools._INTERNAL_MATCH_COUNT: 1,
                            tools._INTERNAL_AS_OF_DATE: raw_date,
                        }
                    ],
                )
                self.assertEqual(1, len(sql_calls))
                self.assertIn("CURDATE() AS `__as_of_date`", sql_calls[0])
                self.assertEqual("success", result["status"])
                self.assertEqual("rows", result["data_state"])
                expected_period = {
                    "source": "current_snapshot",
                    "as_of_date": "2026-08-18",
                    "resolution_state": "resolved",
                }
                self.assertEqual(expected_period, result["applied_time_range"])
                self.assertEqual(expected_period, result["claim_ledger"][0]["period"])
                self.assertTrue(
                    tools.evidence.claim_is_valid_for_result(
                        result["claim_ledger"][0], result
                    )
                )
                self.assertEqual(
                    "查询范围：截至 2026-08-18 的当前业务快照",
                    payload["answer_scope_line"],
                )
                wire = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("__as_of_date", wire)
                self.assertNotIn("CURDATE", wire)
                self.assertNotIn("receivable_bill_detail_dwd", wire)
                self.assertNotIn("bill_time", wire)

        _, zero_result, zero_calls = run_query(
            "overdue_zero_as_of",
            [
                {
                    "metric_value": "0.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                }
            ],
        )
        self.assertEqual(1, len(zero_calls))
        self.assertEqual("zero", zero_result["data_state"])
        self.assertEqual(
            "2026-08-18", zero_result["applied_time_range"]["as_of_date"]
        )

        _, empty_result, empty_calls = run_query(
            "overdue_empty_as_of",
            [
                {
                    "metric_value": None,
                    tools._INTERNAL_MATCH_COUNT: 0,
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                }
            ],
        )
        self.assertEqual(1, len(empty_calls))
        self.assertEqual("empty", empty_result["data_state"])
        self.assertEqual([], empty_result["claim_ledger"])
        self.assertEqual(
            "2026-08-18", empty_result["applied_time_range"]["as_of_date"]
        )

        undefined_cases = (
            ("missing", {"metric_value": "42.00", tools._INTERNAL_MATCH_COUNT: 1}, "as_of_date_unavailable"),
            ("null", {"metric_value": "42.00", tools._INTERNAL_MATCH_COUNT: 1, tools._INTERNAL_AS_OF_DATE: None}, "as_of_date_unavailable"),
            ("empty_string", {"metric_value": "42.00", tools._INTERNAL_MATCH_COUNT: 1, tools._INTERNAL_AS_OF_DATE: ""}, "as_of_date_unavailable"),
            ("malformed", {"metric_value": "42.00", tools._INTERNAL_MATCH_COUNT: 1, tools._INTERNAL_AS_OF_DATE: "2026/08/18"}, "as_of_date_invalid"),
        )
        for case_name, row, resolution_state in undefined_cases:
            with self.subTest(undefined_case=case_name):
                payload, result, sql_calls = run_query(
                    f"overdue_{case_name}_as_of",
                    [row],
                )
                self.assertEqual(1, len(sql_calls))
                self.assertEqual("success", result["status"])
                self.assertEqual("undefined", result["data_state"])
                self.assertEqual(
                    resolution_state,
                    result["applied_time_range"]["resolution_state"],
                )
                self.assertEqual(1, len(result["claim_ledger"]))
                claim = result["claim_ledger"][0]
                self.assertIsNone(claim["facts"]["metric_value"])
                self.assertTrue(tools.evidence.claim_is_valid_for_result(claim, result))
                wire = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("42.00", wire)
                self.assertNotIn("2026/08/18", wire)

        conflicting_payload, conflicting_result, conflicting_calls = run_query(
            "overdue_conflicting_as_of",
            [
                {
                    "metric_value": "21.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                },
                {
                    "metric_value": "21.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    tools._INTERNAL_AS_OF_DATE: "2026-08-19",
                },
            ],
        )
        self.assertEqual(1, len(conflicting_calls))
        self.assertEqual("undefined", conflicting_result["data_state"])
        self.assertEqual(
            "as_of_date_conflicting",
            conflicting_result["applied_time_range"]["resolution_state"],
        )
        self.assertNotIn("21.00", json.dumps(conflicting_payload))

        no_rows_payload, no_rows_result, no_rows_calls = run_query(
            "overdue_no_rows_as_of",
            [],
        )
        self.assertEqual(1, len(no_rows_calls))
        self.assertEqual("undefined", no_rows_result["data_state"])
        self.assertEqual(
            "as_of_date_unavailable",
            no_rows_result["applied_time_range"]["resolution_state"],
        )
        self.assertEqual(1, len(no_rows_result["claim_ledger"]))
        self.assertIsNone(
            no_rows_result["claim_ledger"][0]["facts"]["metric_value"]
        )
        self.assertNotIn("__as_of_date", json.dumps(no_rows_payload))

        other_payload, other_result, other_calls = run_query(
            "open_receivable_unchanged",
            [{"metric_value": "12.00", tools._INTERNAL_MATCH_COUNT: 1}],
            metric="open_receivable_amount",
        )
        self.assertEqual(1, len(other_calls))
        self.assertNotIn("__as_of_date", other_calls[0])
        self.assertEqual("rows", other_result["data_state"])
        self.assertEqual(
            {"source": "current_snapshot"}, other_result["applied_time_range"]
        )
        self.assertEqual("查询范围：当前业务快照", other_payload["answer_scope_line"])

    def test_current_inventory_observation_date_is_same_query_and_fail_closed(
        self,
    ) -> None:
        metric = "current_inventory_amount_rmb"
        answer_contract = (
            "当前库存观察日期是数据库查询日，仅表示该日查询时观察到的当前库存快照，"
            "不代表源数据或 ETL 刷新时点。"
        )
        semantics = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "inventory-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        evidence_metrics = {
            metric_code: definition.get("current_snapshot_evidence")
            for metric_code, definition in semantics["metrics"].items()
            if definition.get("current_snapshot_evidence") is not None
        }
        self.assertEqual(
            {metric: "database_query_date_observation"},
            evidence_metrics,
        )
        self.assertEqual(
            [answer_contract],
            semantics["metrics"][metric]["answer_contract"],
        )

        detail = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "inventory", "metric": metric}]}
            )
        )
        self.assertEqual("success", detail["status"])
        self.assertEqual(
            [answer_contract],
            detail["results"][0]["metric"]["answer_contract"],
        )
        self.assertNotIn(
            "database_query_date_observation",
            json.dumps(detail, ensure_ascii=False),
        )
        def run_query(
            request_id: str,
            rows: list[dict[str, object]],
            *,
            requested_metric: str = metric,
        ) -> tuple[dict[str, object], dict[str, object], list[str]]:
            sql_calls: list[str] = []

            def execute_query(sql, _params, _limit, **_kwargs):
                sql_calls.append(sql)
                return rows, False, self._read_only_source_evidence()

            request = {
                "request_id": request_id,
                "domain": "inventory",
                "mode": "metric",
                "purpose": "synthetic current inventory observation date test",
                "metric": requested_metric,
                "dimensions": [],
                "inventory_scope": "total",
            }
            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(tools.datasage_query({"requests": [request]}))
            result = payload["results"][0] if payload.get("results") else payload
            return payload, result, sql_calls

        def assert_disclosure_seals(result: dict[str, object]) -> None:
            ledger = result["disclosure_ledger"]
            self.assertIsInstance(ledger, list)
            for disclosure in ledger:
                canonical = json.dumps(
                    {
                        key: value
                        for key, value in disclosure.items()
                        if key != "disclosure_seal"
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                self.assertEqual(
                    "sha256_" + hashlib.sha256(canonical).hexdigest(),
                    disclosure["disclosure_seal"],
                )
            ledger_canonical = json.dumps(
                {
                    "contract_version": result["disclosure_contract_version"],
                    "request_id": result["request_id"],
                    "metric_ref": result["business_metric_ref"],
                    "scope_fingerprint": result["scope_fingerprint"],
                    "projection_fingerprint": result["projection_fingerprint"],
                    "ledger": ledger,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            self.assertEqual(
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest(),
                result["disclosure_ledger_seal"],
            )

        valid_period = {
            "source": "current_snapshot",
            "as_of_basis": "database_query_date_observation",
            "as_of_date": "2026-08-18",
            "resolution_state": "resolved",
        }
        complete_payload, complete_result, complete_calls = run_query(
            "inventory_observation_complete",
            [
                {
                    "metric_value": "100.00",
                    tools._INTERNAL_MATCH_COUNT: 2,
                    "missing_value_count": 0,
                    "known_value_count": 2,
                    "value_coverage_rate": "1.0000",
                    "metric_data_state": "complete",
                    tools._INTERNAL_AS_OF_DATE: date(2026, 8, 18),
                }
            ],
        )
        self.assertEqual(1, len(complete_calls))
        self.assertIn("CURDATE() AS `__as_of_date`", complete_calls[0])
        self.assertEqual("rows", complete_result["data_state"])
        self.assertEqual(valid_period, complete_result["applied_time_range"])
        complete_claim = complete_result["claim_ledger"][0]
        self.assertEqual(valid_period, complete_claim["period"])
        self.assertTrue(
            tools.evidence.claim_is_valid_for_result(
                complete_claim,
                complete_result,
            )
        )
        self.assertEqual(
            "查询范围：截至 2026-08-18 查询时观察到的当前库存快照",
            complete_payload["answer_scope_line"],
        )
        self.assertEqual(
            valid_period,
            tools._model_wire_result(complete_result)["applied_time_range"],
        )
        self.assertEqual(
            ["observation"],
            complete_payload["evidence_bundle"]["items"][0]["supports"],
        )
        assert_disclosure_seals(complete_result)

        safe_wire = json.dumps(complete_payload, ensure_ascii=False)
        for private_value in (
            "__as_of_date",
            "CURDATE",
            "inventory_barcode_detail_dw",
            "ddp_amount_rmb",
        ):
            self.assertNotIn(private_value, safe_wire)

        incomplete_payload, incomplete_result, incomplete_calls = run_query(
            "inventory_observation_incomplete",
            [
                {
                    "metric_value": "80.00",
                    tools._INTERNAL_MATCH_COUNT: 3,
                    "missing_value_count": 1,
                    "known_value_count": 2,
                    "value_coverage_rate": "0.6667",
                    "metric_data_state": "incomplete",
                    tools._INTERNAL_AS_OF_DATE: datetime(
                        2026, 8, 18, 13, 14, 15
                    ),
                }
            ],
        )
        self.assertEqual(1, len(incomplete_calls))
        self.assertEqual("incomplete", incomplete_result["data_state"])
        incomplete_claim = incomplete_result["claim_ledger"][0]
        self.assertEqual(valid_period, incomplete_claim["period"])
        self.assertEqual("80.00", incomplete_claim["facts"]["metric_value"])
        self.assertEqual(1, incomplete_claim["facts"]["missing_value_count"])
        self.assertEqual(2, incomplete_claim["facts"]["known_value_count"])
        self.assertEqual(
            "0.6667", incomplete_claim["facts"]["value_coverage_rate"]
        )
        self.assertEqual("data_incomplete", incomplete_payload["evidence_bundle"]["evidence_gaps"][0]["reason"])
        self.assertTrue(
            tools.evidence.claim_is_valid_for_result(
                incomplete_claim,
                incomplete_result,
            )
        )
        assert_disclosure_seals(incomplete_result)

        _, zero_result, zero_calls = run_query(
            "inventory_observation_zero",
            [
                {
                    "metric_value": "0.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    "missing_value_count": 0,
                    "known_value_count": 1,
                    "value_coverage_rate": "1.0000",
                    "metric_data_state": "complete",
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                }
            ],
        )
        self.assertEqual(1, len(zero_calls))
        self.assertEqual("zero", zero_result["data_state"])
        self.assertEqual("0.00", zero_result["claim_ledger"][0]["facts"]["metric_value"])
        self.assertEqual(valid_period, zero_result["applied_time_range"])

        _, empty_result, empty_calls = run_query(
            "inventory_observation_empty",
            [
                {
                    "metric_value": None,
                    tools._INTERNAL_MATCH_COUNT: 0,
                    "missing_value_count": None,
                    "known_value_count": None,
                    "value_coverage_rate": None,
                    "metric_data_state": "missing",
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                }
            ],
        )
        self.assertEqual(1, len(empty_calls))
        self.assertEqual("empty", empty_result["data_state"])
        self.assertEqual([], empty_result["claim_ledger"])
        self.assertEqual(valid_period, empty_result["applied_time_range"])

        undefined_cases = (
            (
                "missing",
                {},
                "as_of_date_unavailable",
            ),
            (
                "null",
                {tools._INTERNAL_AS_OF_DATE: None},
                "as_of_date_unavailable",
            ),
            (
                "empty_string",
                {tools._INTERNAL_AS_OF_DATE: ""},
                "as_of_date_unavailable",
            ),
            (
                "malformed",
                {tools._INTERNAL_AS_OF_DATE: "2026/08/18"},
                "as_of_date_invalid",
            ),
        )
        for case_name, date_fragment, resolution_state in undefined_cases:
            with self.subTest(undefined_case=case_name):
                payload, result, calls = run_query(
                    f"inventory_observation_{case_name}",
                    [
                        {
                            "metric_value": "42.00",
                            tools._INTERNAL_MATCH_COUNT: 2,
                            "missing_value_count": 1,
                            "known_value_count": 1,
                            "value_coverage_rate": "0.5000",
                            "metric_data_state": "incomplete",
                            **date_fragment,
                        }
                    ],
                )
                self.assertEqual(1, len(calls))
                self.assertEqual("undefined", result["data_state"])
                self.assertEqual(
                    resolution_state,
                    result["applied_time_range"]["resolution_state"],
                )
                self.assertEqual(
                    "database_query_date_observation",
                    result["applied_time_range"]["as_of_basis"],
                )
                claim = result["claim_ledger"][0]
                self.assertEqual({"metric_value": None}, claim["facts"])
                self.assertTrue(
                    tools.evidence.claim_is_valid_for_result(claim, result)
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("42.00", serialized)
                self.assertNotIn("known_value_count", serialized)
                self.assertNotIn("missing_value_count", serialized)
                self.assertNotIn("value_coverage_rate", serialized)
                self.assertNotIn("2026/08/18", serialized)

        conflicting_payload, conflicting_result, conflicting_calls = run_query(
            "inventory_observation_conflicting",
            [
                {
                    "metric_value": "21.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    "missing_value_count": 0,
                    "known_value_count": 1,
                    "value_coverage_rate": "1.0000",
                    "metric_data_state": "complete",
                    tools._INTERNAL_AS_OF_DATE: "2026-08-18",
                },
                {
                    "metric_value": "22.00",
                    tools._INTERNAL_MATCH_COUNT: 1,
                    "missing_value_count": 0,
                    "known_value_count": 1,
                    "value_coverage_rate": "1.0000",
                    "metric_data_state": "complete",
                    tools._INTERNAL_AS_OF_DATE: "2026-08-19",
                },
            ],
        )
        self.assertEqual(1, len(conflicting_calls))
        self.assertEqual("undefined", conflicting_result["data_state"])
        self.assertEqual(
            "as_of_date_conflicting",
            conflicting_result["applied_time_range"]["resolution_state"],
        )
        self.assertEqual(
            {"metric_value": None},
            conflicting_result["claim_ledger"][0]["facts"],
        )
        self.assertNotIn("21.00", json.dumps(conflicting_payload))
        self.assertNotIn("22.00", json.dumps(conflicting_payload))

        no_rows_payload, no_rows_result, no_rows_calls = run_query(
            "inventory_observation_no_rows",
            [],
        )
        self.assertEqual(1, len(no_rows_calls))
        self.assertEqual("undefined", no_rows_result["data_state"])
        self.assertEqual(
            "as_of_date_unavailable",
            no_rows_result["applied_time_range"]["resolution_state"],
        )
        self.assertEqual(
            {"metric_value": None},
            no_rows_result["claim_ledger"][0]["facts"],
        )
        self.assertNotIn("__as_of_date", json.dumps(no_rows_payload))

        other_payload, other_result, other_calls = run_query(
            "inventory_other_current_snapshot_unchanged",
            [{"metric_value": 3, tools._INTERNAL_MATCH_COUNT: 1}],
            requested_metric="current_inventory_roll_count",
        )
        self.assertEqual(1, len(other_calls))
        self.assertNotIn("__as_of_date", other_calls[0])
        self.assertEqual(
            {"source": "current_snapshot"},
            other_result["applied_time_range"],
        )
        self.assertEqual("查询范围：当前业务快照", other_payload["answer_scope_line"])

    def test_target_catalog_publishes_candidates_without_owning_clarification(self) -> None:
        payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "target", "view": "expert_index"}]}
            )
        )
        self.assertEqual("success", payload["status"])
        expert_index = payload["results"][0]
        metric_codes = {metric["code"] for metric in expert_index["metrics"]}
        completion_metrics = {
            "delivery_target_completion",
            "receipt_target_completion",
        }
        allocation_metrics = {
            "delivery_allocated_target_amount",
            "receipt_allocated_target_amount",
            "allocated_net_delivery_amount",
            "allocated_net_receipt_amount",
        }
        self.assertTrue(completion_metrics.issubset(metric_codes))
        self.assertTrue(allocation_metrics.issubset(metric_codes))

        self.assertNotIn("metric_selection_boundary", expert_index)
        self.assertNotIn("next_step", expert_index)
        serialized_index = json.dumps(expert_index, ensure_ascii=False)
        self.assertNotIn('"exact_match"', serialized_index)
        self.assertNotIn('"ambiguity"', serialized_index)

        for metric in sorted(completion_metrics):
            detail = json.loads(
                contracts.datasage_catalog(
                    {"requests": [{"domain": "target", "metric": metric}]}
                )
            )
            self.assertEqual("success", detail["status"], metric)
            projected = detail["results"][0]
            self.assertEqual(metric, projected["metric"]["code"])
            self.assertEqual(
                ["salesperson_allocation", "transaction_detail"],
                projected["metric"]["allowed_attribution_modes"],
            )
            self.assertEqual(
                ["customer", "department", "organization", "salesperson"],
                projected["metric"]["dimensions_by_attribution_mode"][
                    "salesperson_allocation"
                ],
            )
            self.assertIn(
                "salesperson", projected["metric"]["allowed_dimensions"]
            )
            serialized_detail = json.dumps(projected, ensure_ascii=False)
            self.assertNotIn("vk_dwd", serialized_detail)
            self.assertNotIn("split_dwd", serialized_detail)
            for forbidden in (
                "planning_guidance",
                "recipe_policy",
                "recipes",
                "next_step",
                "call_official_clarify",
            ):
                self.assertNotIn(forbidden, serialized_detail)

        for metric in sorted(allocation_metrics):
            detail = json.loads(
                contracts.datasage_catalog(
                    {"requests": [{"domain": "target", "metric": metric}]}
                )
            )
            self.assertEqual("success", detail["status"], metric)
            projected = detail["results"][0]["metric"]
            self.assertEqual(metric, projected["code"])
            self.assertEqual(
                "salesperson_allocation",
                projected["required_attribution_mode"],
            )
            self.assertIn("salesperson", projected["allowed_dimensions"])

    def test_model_planner_skill_contracts_are_absent_from_release_tree(self) -> None:
        planner_contracts = list(
            (PROFILE_ROOT / "skills").glob("*-query/references/planner-contract.yaml")
        )
        self.assertEqual([], planner_contracts)
        for path in (PLUGIN_ROOT / "contracts").glob("*-semantics.yaml"):
            semantics = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertNotIn("model_planner_contract", semantics, path.name)
            self.assertNotIn("triggers", semantics, path.name)

    def test_all_model_catalog_details_avoid_legacy_driver_vocabulary(self) -> None:
        checked_metrics = 0
        structural_metrics = 0
        for domain in (
            "delivery",
            "receipt",
            "receivable",
            "target",
            "receivable",
            "inventory",
        ):
            summary = json.loads(
                contracts.datasage_catalog({"requests": [{"domain": domain}]})
            )
            self.assertEqual("success", summary["status"], domain)
            for metric in summary["results"][0]["metrics"]:
                payload = json.loads(
                    contracts.datasage_catalog(
                        {
                            "requests": [
                                {"domain": domain, "metric": metric["code"]}
                            ]
                        }
                    )
                )
                self.assertEqual("success", payload["status"], metric["code"])
                serialized = json.dumps(payload, ensure_ascii=False).casefold()
                self.assertNotIn("driver", serialized, metric["code"])
                self.assertNotIn("analysis_affordances", serialized)
                self.assertNotIn("reasoning_topics", serialized)
                if metric["supports_change_decomposition"]:
                    self.assertTrue(
                        payload["results"][0]["metric"][
                            "change_decomposition_dimensions"
                        ]
                    )
                    structural_metrics += 1
                checked_metrics += 1
        self.assertGreater(checked_metrics, 100)
        self.assertGreater(structural_metrics, 0)

    def test_model_wire_does_not_mutate_or_publish_legacy_reconciliation_fields(self) -> None:
        internal_reconciliation = {
            "driver_projection_fingerprint": "projection-1",
            "driver_current_sum": 12,
            "driver_comparison_sum": 8,
            "driver_delta_sum": 4,
            "returned_driver_row_count": 2,
            "unreturned_driver_row_count": 1,
            "driver_claim_ids": ["claim-1"],
            "driver_row_count": 3,
            "returned_nonzero_driver_count": 1,
            "nonzero_driver_count_scope": "returned_rows_only",
            "nonzero_driver_count": 1,
        }
        projected = tools._model_wire_result(
            {
                "request_id": "partition",
                "change_reconciliation": internal_reconciliation,
            }
        )
        serialized = json.dumps(projected, ensure_ascii=False).casefold()
        self.assertNotIn("driver", serialized)
        self.assertNotIn("change_reconciliation", projected)
        self.assertIn("driver_claim_ids", internal_reconciliation)

    def test_model_wire_filters_unsealed_evidence_for_every_metric(self) -> None:
        def seal_disclosure(item: dict[str, object]) -> None:
            canonical = json.dumps(
                {
                    key: value
                    for key, value in item.items()
                    if key != "disclosure_seal"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            item["disclosure_seal"] = (
                "sha256_" + hashlib.sha256(canonical).hexdigest()
            )

        def result_fixture() -> dict[str, object]:
            result: dict[str, object] = {
                "request_id": "ordinary_metric",
                "status": "success",
                "data_state": "complete",
                "business_metric_ref": "delivery.delivery_amount",
                "business_metric_label": "出库金额",
                "scope_fingerprint": "scope_ordinary",
                "projection_fingerprint": "projection_ordinary",
                "row_count": 1,
                "truncated": False,
            }
            claim: dict[str, object] = {
                "request_id": result["request_id"],
                "metric_ref": result["business_metric_ref"],
                "scope_fingerprint": result["scope_fingerprint"],
                "projection_fingerprint": result["projection_fingerprint"],
                "dimensions": [],
                "facts": {"metric_value": "10.00"},
                "states": {},
                "source_truncated": False,
                "allowed_relations": ["observation"],
            }
            tools.evidence.seal_claim(claim)
            result["claim_ledger"] = [claim]
            ledger: list[dict[str, object]] = []
            for disclosure_id, applies in (
                ("delivery.scope", True),
                ("delivery.conditional-scope", False),
            ):
                item: dict[str, object] = {
                    "disclosure_id": disclosure_id,
                    "disclosure_seal": "unsealed",
                    "contract_version": "metric-disclosure/v1",
                    "request_id": result["request_id"],
                    "metric_ref": result["business_metric_ref"],
                    "scope_fingerprint": result["scope_fingerprint"],
                    "projection_fingerprint": result[
                        "projection_fingerprint"
                    ],
                    "mode": (
                        "required_always" if applies else "required_when"
                    ),
                    "order": len(ledger),
                    "text": f"synthetic {disclosure_id}",
                    "applies": applies,
                }
                seal_disclosure(item)
                ledger.append(item)
            result["disclosure_contract_version"] = (
                "metric-disclosure-ledger/v1"
            )
            result["disclosure_ledger"] = ledger
            ledger_canonical = json.dumps(
                {
                    "contract_version": result[
                        "disclosure_contract_version"
                    ],
                    "request_id": result["request_id"],
                    "metric_ref": result["business_metric_ref"],
                    "scope_fingerprint": result["scope_fingerprint"],
                    "projection_fingerprint": result[
                        "projection_fingerprint"
                    ],
                    "ledger": ledger,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            result["disclosure_ledger_seal"] = (
                "sha256_" + hashlib.sha256(ledger_canonical).hexdigest()
            )
            return result

        valid = result_fixture()
        valid_before = json.loads(json.dumps(valid))
        projected = tools._model_wire_result(valid)
        self.assertEqual(valid_before, valid)
        self.assertEqual(valid["claim_ledger"], projected["claim_ledger"])
        self.assertEqual("complete", projected["data_state"])
        self.assertNotIn("error", projected)
        self.assertNotIn("allowed_reasoning_topics", projected)
        self.assertEqual(
            ["delivery.scope"],
            [
                item["disclosure_id"]
                for item in projected["disclosure_ledger"]
            ],
        )
        self.assertTrue(
            all(item["applies"] is True for item in projected["disclosure_ledger"])
        )
        self.assertEqual(
            "metric-disclosure-ledger-model-projection/v1",
            projected["disclosure_contract_version"],
        )
        self.assertEqual(projected, tools._model_wire_result(projected))

        tamper_cases = {
            "value": lambda item: item["claim_ledger"][0]["facts"].__setitem__(
                "metric_value", "999.00"
            ),
            "scope": lambda item: (
                item["claim_ledger"][0].__setitem__(
                    "scope_fingerprint", "scope_other"
                ),
                tools.evidence.seal_claim(item["claim_ledger"][0]),
            ),
            "truncated": lambda item: item.__setitem__("truncated", True),
            "claim_seal": lambda item: item["claim_ledger"][0].__setitem__(
                "claim_seal", "sha256_" + "0" * 64
            ),
            "claim_ledger_missing": lambda item: item.pop("claim_ledger"),
        }
        for label, mutate in tamper_cases.items():
            tampered = result_fixture()
            mutate(tampered)
            before_projection = json.loads(json.dumps(tampered))
            failed_closed = tools._model_wire_result(tampered)
            self.assertEqual(before_projection, tampered, label)
            self.assertEqual([], failed_closed["claim_ledger"], label)
            self.assertEqual("undefined", failed_closed["data_state"], label)
            self.assertEqual(0, failed_closed["row_count"], label)
            self.assertEqual(
                "EVIDENCE_INTEGRITY_INVALID",
                failed_closed["error"]["code"],
                label,
            )
            self.assertNotIn("allowed_reasoning_topics", failed_closed, label)

        partially_tampered = result_fixture()
        second_claim = json.loads(
            json.dumps(partially_tampered["claim_ledger"][0])
        )
        second_claim["dimensions"] = [{"label": "客户", "value": "B"}]
        tools.evidence.seal_claim(second_claim)
        partially_tampered["claim_ledger"].append(second_claim)
        partially_tampered["row_count"] = 2
        partially_tampered["claim_ledger"][0]["facts"]["metric_value"] = (
            "999.00"
        )
        partial_wire = tools._model_wire_result(partially_tampered)
        self.assertEqual([second_claim], partial_wire["claim_ledger"])
        self.assertEqual("incomplete", partial_wire["data_state"])
        self.assertEqual(1, partial_wire["row_count"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            partial_wire["error"]["code"],
        )

        disclosure_tamper = result_fixture()
        disclosure_tamper["disclosure_ledger"][0]["disclosure_seal"] = (
            "sha256_" + "0" * 64
        )
        disclosure_wire = tools._model_wire_result(disclosure_tamper)
        self.assertEqual([], disclosure_wire["claim_ledger"])
        self.assertEqual([], disclosure_wire["disclosure_ledger"])
        self.assertEqual("undefined", disclosure_wire["data_state"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            disclosure_wire["error"]["code"],
        )

    def test_governed_calculation_rejects_forged_claim_seal(self) -> None:
        left = self._scalar_calculation_result(
            "left",
            "12.00",
            period=("2026-02-01", "2026-03-01"),
        )
        right = self._scalar_calculation_result(
            "right",
            "10.00",
            period=("2026-01-01", "2026-02-01"),
        )
        left["claim_ledger"][0]["claim_seal"] = "sha256_" + "0" * 64
        right["claim_ledger"][0]["claim_seal"] = "sha256_" + "1" * 64

        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]

        self.assertEqual("failed", calculation["status"])
        self.assertEqual(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            calculation["error"]["code"],
        )
        self.assertNotIn("calculation_seal", calculation)
        public_results = [
            tools._model_wire_result(left),
            tools._model_wire_result(right),
        ]
        projected = tools._model_wire_calculations(
            [calculation],
            public_results,
        )
        self.assertEqual(1, len(projected))
        self.assertEqual("failed", projected[0]["status"])
        self.assertEqual(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            projected[0]["error"]["code"],
        )
        self.assertNotIn("operands", projected[0])
        self.assertNotIn("calculation_seal", projected[0])

    def test_governed_calculation_rejects_resealed_result_binding_mismatch(
        self,
    ) -> None:
        left = self._scalar_calculation_result(
            "left",
            "12.00",
            period=("2026-02-01", "2026-03-01"),
        )
        right = self._scalar_calculation_result(
            "right",
            "10.00",
            period=("2026-01-01", "2026-02-01"),
        )
        left_claim = left["claim_ledger"][0]
        left_claim["scope_fingerprint"] = "scope_other"
        tools.evidence.seal_claim(left_claim)

        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]

        self.assertEqual("failed", calculation["status"])
        self.assertEqual(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            calculation["error"]["code"],
        )
        self.assertNotIn("calculation_seal", calculation)

    def test_model_wire_replaces_calculation_when_one_operand_loses_integrity(
        self,
    ) -> None:
        left = self._scalar_calculation_result(
            "left",
            "12.00",
            period=("2026-02-01", "2026-03-01"),
        )
        right = self._scalar_calculation_result(
            "right",
            "10.00",
            period=("2026-01-01", "2026-02-01"),
        )
        calculation = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        self.assertEqual("success", calculation["status"])

        right["claim_ledger"][0]["facts"]["metric_value"] = "999.00"
        public_results = [
            tools._model_wire_result(left),
            tools._model_wire_result(right),
        ]
        self.assertEqual([], public_results[1]["claim_ledger"])
        projected = tools._model_wire_calculations(
            [calculation],
            public_results,
        )
        self.assertEqual(1, len(projected))
        self.assertEqual("failed", projected[0]["status"])
        self.assertEqual(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            projected[0]["error"]["code"],
        )
        self.assertNotIn("operands", projected[0])
        self.assertNotIn("calculation_seal", projected[0])

    def test_public_query_redacts_calculation_when_one_operand_is_invalid(
        self,
    ) -> None:
        requests = [
            {
                "request_id": "left",
                "domain": "delivery",
                "mode": "metric",
                "purpose": "calculation integrity integration test",
                "metric": "delivery_amount",
                "dimensions": [],
                "time_range": {
                    "start": "2026-02-01",
                    "end": "2026-03-01",
                },
            },
            {
                "request_id": "right",
                "domain": "delivery",
                "mode": "metric",
                "purpose": "calculation integrity integration test",
                "metric": "delivery_amount",
                "dimensions": [],
                "time_range": {
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                },
            },
        ]
        calculations = [
            {
                "calculation_id": "delta",
                "operation": "difference",
                "left_request_id": "left",
                "right_request_id": "right",
            }
        ]
        original_seal_claim_ids = tools._seal_claim_ids

        def seal_then_tamper(results: list[dict[str, object]]) -> None:
            original_seal_claim_ids(results)
            results[0]["claim_ledger"][0]["claim_seal"] = (
                "sha256_" + "0" * 64
            )

        with (
            mock.patch.object(
                runtime_health,
                "query_readiness_status",
                return_value={"ready": True},
            ),
            mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=[
                    (
                        [{"metric_value": "12.00"}],
                        False,
                        self._read_only_source_evidence(),
                    ),
                    (
                        [{"metric_value": "10.00"}],
                        False,
                        self._read_only_source_evidence(),
                    ),
                ],
            ),
            mock.patch.object(
                tools,
                "_seal_claim_ids",
                side_effect=seal_then_tamper,
            ),
        ):
            payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {
                        "requests": requests,
                        "calculations": calculations,
                    }
                )
            )

        self.assertEqual("partial", payload["status"])
        self.assertEqual(1, payload["calculation_count"])
        self.assertEqual("failed", payload["calculations"][0]["status"])
        self.assertEqual(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            payload["calculations"][0]["error"]["code"],
        )
        self.assertNotIn("operands", payload["calculations"][0])
        self.assertEqual([], payload["results"][0]["claim_ledger"])
        self.assertEqual(
            "EVIDENCE_INTEGRITY_INVALID",
            payload["results"][0]["error"]["code"],
        )
        self.assertNotIn("calculation_seal", json.dumps(payload))
        source_reference = payload["source_evidence_ref"]
        self.assertEqual(
            {
                "schema",
                "source_ref_sha256",
            },
            set(source_reference),
        )
        self.assertEqual(
            "datasage-query-model-source-reference/v1",
            source_reference["schema"],
        )
        self.assertRegex(
            source_reference["source_ref_sha256"],
            r"^[0-9a-f]{64}$",
        )
        serialized_source_reference = json.dumps(source_reference)
        for private_field in (
            "identity_sha256",
            "transport_mode",
            "grant_policy",
            "read_only",
            "source_commitment_sha256",
            "security_evidence_sha256",
        ):
            self.assertNotIn(private_field, serialized_source_reference)

    def test_governed_calculation_normal_path_remains_sealed_and_visible(
        self,
    ) -> None:
        left = self._scalar_calculation_result(
            "left",
            "12.00",
            period=("2026-02-01", "2026-03-01"),
        )
        right = self._scalar_calculation_result(
            "right",
            "10.00",
            period=("2026-01-01", "2026-02-01"),
        )
        share_numerator = self._scalar_calculation_result(
            "share_numerator",
            "2.00",
            period=("2026-02-01", "2026-03-01"),
            filter_scope={"customer": "A"},
            share_partition_dimensions=("customer",),
        )
        share_denominator = self._scalar_calculation_result(
            "share_denominator",
            "10.00",
            period=("2026-02-01", "2026-03-01"),
            share_partition_dimensions=("customer",),
        )
        calculations = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                },
                {
                    "calculation_id": "ratio",
                    "operation": "ratio",
                    "left_request_id": "left",
                    "right_request_id": "right",
                },
                {
                    "calculation_id": "share",
                    "operation": "share",
                    "left_request_id": "share_numerator",
                    "right_request_id": "share_denominator",
                },
            ],
            [left, right, share_numerator, share_denominator],
        )
        public_results = [
            tools._model_wire_result(left),
            tools._model_wire_result(right),
            tools._model_wire_result(share_numerator),
            tools._model_wire_result(share_denominator),
        ]
        projected = tools._model_wire_calculations(
            calculations,
            public_results,
        )

        self.assertEqual(
            ["success", "success", "success"],
            [calculation["status"] for calculation in calculations],
        )
        self.assertEqual(
            ["2.00", "1.2", "0.2"],
            [calculation["value"] for calculation in calculations],
        )
        self.assertTrue(
            all(
                tools._calculation_has_valid_seal(calculation)
                for calculation in calculations
            )
        )
        self.assertEqual(calculations, projected)
        for calculation in projected:
            for operand in calculation["operands"]:
                self.assertRegex(
                    operand["claim_seal"],
                    r"^sha256_[0-9a-f]{64}$",
                )

    def test_canary_source_reference_digest_supports_new_and_legacy_formats(
        self,
    ) -> None:
        digest = "a" * 64
        public_reference = {
            "schema": "datasage-query-model-source-reference/v1",
            "source_ref_sha256": digest,
        }
        self.assertEqual(
            digest,
            canary_transcript_adapter._business_database_source_digest(
                public_reference
            ),
        )

        legacy_reference = self._read_only_source_evidence()
        self.assertEqual(
            canary_transcript_adapter._sha256(legacy_reference),
            canary_transcript_adapter._business_database_source_digest(
                legacy_reference
            ),
        )

        privileged_reference = self._read_only_source_evidence()
        privileged_reference.update(
            {
                "grant_policy": "user_accepted_canary_privileged_account",
                "configured_port": 3306,
                "observed_server_port": 3002,
                "canary_source_port_mismatch_exception": True,
            }
        )
        sealed = {
            key: value
            for key, value in privileged_reference.items()
            if key != "security_evidence_sha256"
        }
        privileged_reference["security_evidence_sha256"] = hashlib.sha256(
            b"datasage-query-source-evidence/v1\x00"
            + json.dumps(
                sealed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            canary_transcript_adapter._sha256(privileged_reference),
            canary_transcript_adapter._business_database_source_digest(
                privileged_reference
            ),
        )

        invalid_identity_cases = {}
        extra_identity = json.loads(json.dumps(privileged_reference))
        extra_identity["private_source"] = "must-not-be-accepted"
        invalid_identity_cases["extra_identity_field"] = extra_identity
        bad_port = json.loads(json.dumps(privileged_reference))
        bad_port["configured_port"] = 0
        invalid_identity_cases["bad_port"] = bad_port
        bad_boolean = json.loads(json.dumps(privileged_reference))
        bad_boolean["canary_source_port_mismatch_exception"] = 1
        invalid_identity_cases["bad_boolean"] = bad_boolean
        inconsistent = json.loads(json.dumps(privileged_reference))
        inconsistent["observed_server_port"] = 3306
        invalid_identity_cases["inconsistent_mismatch"] = inconsistent
        for label, reference in invalid_identity_cases.items():
            with self.subTest(privileged_case=label):
                with self.assertRaises(ValueError):
                    canary_transcript_adapter._business_database_source_digest(
                        reference
                    )

        legacy_tamper_cases: dict[str, object] = {}
        extra_field = json.loads(json.dumps(legacy_reference))
        extra_field["private_source"] = "must-not-be-accepted"
        legacy_tamper_cases["extra_field"] = extra_field
        missing_field = json.loads(json.dumps(legacy_reference))
        missing_field.pop("identity_sha256")
        legacy_tamper_cases["missing_field"] = missing_field
        wrong_seal = json.loads(json.dumps(legacy_reference))
        wrong_seal["security_evidence_sha256"] = "0" * 64
        legacy_tamper_cases["wrong_seal"] = wrong_seal
        wrong_digest_type = json.loads(json.dumps(legacy_reference))
        wrong_digest_type["identity_sha256"] = 1
        legacy_tamper_cases["wrong_digest_type"] = wrong_digest_type
        wrong_boolean_type = json.loads(json.dumps(legacy_reference))
        wrong_boolean_type["connection_verified"] = 1
        legacy_tamper_cases["wrong_boolean_type"] = wrong_boolean_type
        wrong_transport = json.loads(json.dumps(legacy_reference))
        wrong_transport["transport_mode"] = "ssh"
        legacy_tamper_cases["wrong_transport"] = wrong_transport
        wrong_grant = json.loads(json.dumps(legacy_reference))
        wrong_grant["grant_policy"] = "all_privileges"
        legacy_tamper_cases["wrong_grant"] = wrong_grant
        writable = json.loads(json.dumps(legacy_reference))
        writable["read_only"] = False
        legacy_tamper_cases["writable"] = writable
        for label, reference in legacy_tamper_cases.items():
            with self.subTest(legacy_case=label):
                with self.assertRaises(ValueError):
                    canary_transcript_adapter._business_database_source_digest(
                        reference
                    )

        malformed_references = (
            None,
            {"schema": "unknown", "source_ref_sha256": digest},
            {
                "schema": "datasage-query-model-source-reference/v1",
                "source_ref_sha256": digest.upper(),
            },
            {
                "schema": "datasage-query-model-source-reference/v1",
                "source_ref_sha256": digest,
                "private_source": "must-not-be-accepted",
            },
        )
        for reference in malformed_references:
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    canary_transcript_adapter._business_database_source_digest(
                        reference
                    )

    def test_model_wire_calculations_remove_unregistered_fields(self) -> None:
        left = self._scalar_calculation_result(
            "left",
            "12.00",
            period=("2026-02-01", "2026-03-01"),
        )
        right = self._scalar_calculation_result(
            "right",
            "10.00",
            period=("2026-01-01", "2026-02-01"),
        )
        success = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "delta",
                    "operation": "difference",
                    "left_request_id": "left",
                    "right_request_id": "right",
                }
            ],
            [left, right],
        )[0]
        success["private_sql"] = "SELECT secret FROM private_table"
        success["operands"][0]["private_physical_field"] = "customer_name"
        success["relation_semantics"]["private_reasoning"] = "secret"
        success["scope_compatibility"]["private_scope"] = "secret"
        success_body = {
            key: value
            for key, value in success.items()
            if key != "calculation_seal"
        }
        success["calculation_seal"] = "sha256_" + hashlib.sha256(
            json.dumps(
                success_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        public_results = [
            tools._model_wire_result(left),
            tools._model_wire_result(right),
        ]
        projected_success = tools._model_wire_calculations(
            [success],
            public_results,
        )[0]
        self.assertEqual("success", projected_success["status"])
        self.assertTrue(tools._calculation_has_valid_seal(projected_success))
        serialized_success = json.dumps(projected_success, ensure_ascii=False)
        for private_value in (
            "private_sql",
            "private_table",
            "private_physical_field",
            "customer_name",
            "private_reasoning",
            "private_scope",
        ):
            self.assertNotIn(private_value, serialized_success)

        ordinary_failure = tools._calculation_failure(
            {
                "calculation_id": "ratio",
                "operation": "ratio",
                "left_request_id": "left",
                "right_request_id": "right",
            },
            tools.QueryFailure("CALCULATION_ZERO_DENOMINATOR", "不能除以零。"),
        )
        ordinary_failure["private_sql"] = "SELECT secret"
        ordinary_failure["operands"][0]["private_operand"] = "secret"
        ordinary_failure["error"]["private_trace"] = "secret"
        projected_failure = tools._model_wire_calculations(
            [ordinary_failure],
            public_results,
        )[0]
        self.assertEqual("failed", projected_failure["status"])
        self.assertNotIn(
            "private",
            json.dumps(projected_failure, ensure_ascii=False),
        )

        integrity_failure = {
            **ordinary_failure,
            "error": {
                "code": "CALCULATION_SOURCE_INTEGRITY_INVALID",
                "message": "forged internal detail",
                "private_trace": "secret",
            },
        }
        projected_integrity = tools._model_wire_calculations(
            [integrity_failure],
            public_results,
        )[0]
        self.assertEqual(
            {
                "calculation_id",
                "operation",
                "status",
                "error",
            },
            set(projected_integrity),
        )
        self.assertNotIn(
            "private",
            json.dumps(projected_integrity, ensure_ascii=False),
        )

    def test_shared_dimension_labels_are_business_specific(self) -> None:
        for domain in ("delivery", "receivable"):
            projection = contracts._domain_contract(domain, "planner")["planner"]
            labels = {
                dimension["code"]: dimension["label"]
                for dimension in projection["dimensions"]
            }
            self.assertEqual(
                "默认归属部门" if domain == "delivery" else "客户部门",
                labels["department"],
            )
            self.assertEqual("业务组织", labels["organization"])

    def test_plugin_skill_and_host_versions_are_documented_consistently(self) -> None:
        plugin_manifest = yaml.safe_load(
            (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        architecture = (PROFILE_ROOT / "ARCHITECTURE.md").read_text(
            encoding="utf-8"
        )
        main_skill = _main_skill()

        version = str(plugin_manifest["version"])
        hermes_version = "0.21.1"
        self.assertEqual(version, str(plugin_manifest["version"]))
        self.assertIn(f"version: {version}", main_skill)
        self.assertIn(f"`{version}`", architecture)
        self.assertIn(f"`{hermes_version}`", architecture)

    def test_skill_keeps_adaptive_planning_and_evidence_boundaries(self) -> None:
        main_skill = _main_skill()
        normalized = " ".join(main_skill.split())
        answer_boundary = " ".join(_answer_boundary().split())

        self.assertIn("Choose the route adaptively", normalized)
        self.assertIn(
            "If one branch fails, preserve valid independent evidence",
            answer_boundary,
        )
        for forbidden in (
            "must query exactly",
            "fixed metric count",
            "fixed call order",
            "answer template",
        ):
            self.assertNotIn(forbidden, normalized.casefold())
        self.assertTrue(SKILL_PATH.is_file())

        _, registration = probe_registration(
            PLUGIN_ROOT,
            package_name="datasage_business_contract_registration",
        )
        self.assertEqual(
            {
                schemas.DATASAGE_CATALOG["name"],
                schemas.DATASAGE_ENTITY_RESOLVE["name"],
                schemas.DATASAGE_QUERY["name"],
            },
            {entry["name"] for entry in registration.tools},
        )
        self.assertEqual([], registration.hooks)
        self.assertEqual([], registration.prompt_sections)

    def test_period_evidence_distinguishes_calendar_progress_from_freshness(self) -> None:
        observed_on = date(2026, 8, 25)
        current = tools._annotate_period_evidence(
            {"start": "2026-08-01", "end": "2026-09-01", "source": "explicit"},
            observed_on,
        )
        completed = tools._annotate_period_evidence(
            {"start": "2026-07-01", "end": "2026-08-01", "source": "explicit"},
            observed_on,
        )
        future = tools._annotate_period_evidence(
            {"start": "2026-09-01", "end": "2026-10-01", "source": "explicit"},
            observed_on,
        )

        self.assertNotIn("period_state", current)
        self.assertNotIn("coverage", current)
        self.assertEqual(
            "in_progress", current["calendar_evidence"]["period_state"]
        )
        self.assertEqual(
            "not_proven", current["calendar_evidence"]["source_freshness"]
        )
        self.assertEqual(
            "completed", completed["calendar_evidence"]["period_state"]
        )
        self.assertEqual(
            "not_started", future["calendar_evidence"]["period_state"]
        )
        self.assertIn("期间进行中", tools._scope_texts(current)[0])
        self.assertIn("数据新鲜度未证明", tools._scope_texts(current)[0])

    def test_evidence_projects_dynamic_relations_only_from_valid_claim_seals(self) -> None:
        result = self._scalar_calculation_result(
            "causal_relation", "90", period=("2026-07-01", "2026-08-01")
        )
        claim = result["claim_ledger"][0]
        claim["allowed_relations"] = ["observation", "causal_conclusion"]
        tools.evidence.seal_claim(claim)
        request = {"request_id": "causal_relation"}

        bundle = tools.evidence.build_evidence_bundle([request], [result])
        self.assertIn("causal_conclusion", bundle["items"][0]["supports"])
        self.assertNotIn("answer_guardrails", bundle)

        tampered = copy.deepcopy(result)
        tampered["claim_ledger"][0]["allowed_relations"].append("forged_relation")
        tampered_bundle = tools.evidence.build_evidence_bundle([request], [tampered])
        self.assertEqual([], tampered_bundle["items"][0]["supports"])

    def test_coverage_mismatch_keeps_values_without_period_comparison_authority(self) -> None:
        observed_on = date(2026, 8, 25)
        mismatched_period = tools._annotate_period_evidence(
            {
                "current": {
                    "start": "2026-08-01",
                    "end": "2026-09-01",
                    "source": "explicit",
                },
                "comparison": {
                    "start": "2026-07-01",
                    "end": "2026-08-01",
                    "source": "explicit",
                },
            },
            observed_on,
        )
        self.assertEqual(
            "coverage_mismatch",
            mismatched_period["comparison_compatibility"]["status"],
        )
        claims = tools._claim_ledger(
            "period_mismatch",
            "metric_calculation_fixture",
            "计算测试指标",
            "人民币元",
            [],
            mismatched_period,
            "scope_period_mismatch",
            "projection_period_mismatch",
            False,
            [
                {
                    "metric_value": "90",
                    "comparison_value": "100",
                    "delta_value": "-10",
                    "change_rate": "-0.1",
                }
            ],
        )
        self.assertEqual("-10", claims[0]["facts"]["delta_value"])
        self.assertNotIn("period_comparison", claims[0]["allowed_relations"])
        tools.evidence.seal_claim(claims[0])
        result = self._scalar_calculation_result(
            "period_mismatch",
            "90",
            period=("2026-08-01", "2026-09-01"),
        )
        result["applied_time_range"] = mismatched_period
        result["claim_ledger"] = claims
        self.assertTrue(tools.evidence.claim_is_valid_for_result(claims[0], result))
        projected_result = tools._model_wire_result(result)
        projected_facts = projected_result["claim_ledger"][0]["facts"]
        self.assertEqual("90", projected_facts["metric_value"])
        self.assertEqual("100", projected_facts["comparison_value"])
        self.assertNotIn("delta_value", projected_facts)
        self.assertNotIn("change_rate", projected_facts)
        self.assertNotIn(
            "period_comparison",
            projected_result["claim_ledger"][0]["allowed_relations"],
        )
        tampered = copy.deepcopy(claims[0])
        tampered["period"]["current"]["calendar_evidence"][
            "period_state"
        ] = "completed"
        tools.evidence.seal_claim(tampered)
        self.assertFalse(tools.evidence.claim_is_valid_for_result(tampered, result))

    def test_governed_arithmetic_preserves_value_and_reports_period_compatibility(self) -> None:
        observed_on = date(2026, 8, 25)
        left = self._scalar_calculation_result(
            "current_open", "90", period=("2026-08-01", "2026-09-01")
        )
        right = self._scalar_calculation_result(
            "prior_closed", "100", period=("2026-07-01", "2026-08-01")
        )
        for result in (left, right):
            annotated = tools._annotate_period_evidence(
                result["applied_time_range"], observed_on
            )
            result["applied_time_range"] = annotated
            result["claim_ledger"][0]["period"] = copy.deepcopy(annotated)
            tools.evidence.seal_claim(result["claim_ledger"][0])
        calculations = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "open_vs_closed",
                    "operation": "difference",
                    "left_request_id": "current_open",
                    "right_request_id": "prior_closed",
                }
            ],
            [left, right],
            observed_on=observed_on,
        )
        calculation = calculations[0]
        self.assertEqual("success", calculation["status"])
        self.assertEqual("-10", calculation["value"])
        self.assertEqual(
            "coverage_mismatch",
            calculation["period_compatibility"]["status"],
        )
        self.assertIn("PERIOD_COVERAGE_MISMATCH", calculation["limitations"])
        projected = tools._model_wire_calculations(calculations, [left, right])[0]
        self.assertEqual("failed", projected["status"])
        self.assertEqual("PERIOD_COVERAGE_MISMATCH", projected["error"]["code"])
        self.assertEqual([], projected["allowed_relations"])
        self.assertNotIn("value", projected)
        self.assertNotIn("calculation_seal", projected)
        self.assertEqual(
            [{"request_id": "current_open"}, {"request_id": "prior_closed"}],
            projected["operands"],
        )

    def test_compact_wire_retains_period_scope_and_typed_limitation(self) -> None:
        observed_on = date(2026, 8, 25)
        result = self._scalar_calculation_result(
            "open_period", "90", period=("2026-08-01", "2026-09-01")
        )
        annotated = tools._annotate_period_evidence(
            result["applied_time_range"], observed_on
        )
        result["applied_time_range"] = annotated
        result["claim_ledger"][0]["period"] = copy.deepcopy(annotated)
        tools.evidence.seal_claim(result["claim_ledger"][0])
        request = {"request_id": "open_period"}
        payload = {
            "status": "success",
            "metric_contexts": [{"business_metric_ref": "metric_calculation_fixture"}],
            "results": [result],
            "evidence_bundle": tools.evidence.build_evidence_bundle(
                [request], [result]
            ),
        }
        compact = wire.compact_query_payload(payload)
        self.assertEqual(
            "in_progress",
            compact["results"][0]["applied_time_range"]["calendar_evidence"][
                "period_state"
            ],
        )
        self.assertNotIn("answer_constraints", compact)
        self.assertIn(
            "PERIOD_IN_PROGRESS",
            compact["evidence_bundle"]["items"][0]["limitations"],
        )

    def test_debt_balance_trend_reuses_required_month_bucket_and_rejects_conflict(
        self,
    ) -> None:
        detail = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": "receivable", "metric": "debt_balance_trend"}]}
            )
        )["results"][0]
        self.assertEqual("month", detail["metric"]["required_time_bucket"])
        self.assertEqual([], detail["metric"]["comparison_kinds"])
        raw_request = {
            "request_id": "debt_trend_required_bucket",
            "domain": "receivable",
            "mode": "metric",
            "purpose": "contract-required monthly debt trend",
            "metric": "debt_balance_trend",
            "dimensions": [],
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        request = tools._validate_request(raw_request)
        datasets, semantics = tools._contracts("receivable")
        request = tools._validate_metric_contract(request, semantics)
        self.assertEqual("month", request["time_bucket"])
        sql, params, scope = tools._build_metric_query(
            request,
            datasets,
            semantics,
            tools._metric_query_limit(request),
            observed_on=date(2026, 8, 25),
        )
        self.assertIn("AS `period`", sql)
        self.assertIn("GROUP BY `f`.`bill_date`", sql)
        self.assertEqual("period", scope["dimension_outputs"][0])
        self.assertIn("2026-01", params)
        self.assertIn("2026-09", params)

        conflicting = tools._validate_request(
            {**raw_request, "request_id": "debt_trend_conflict", "time_bucket": "day"}
        )
        with self.assertRaises(tools.QueryFailure) as caught:
            tools._validate_metric_contract(conflicting, semantics)
        self.assertEqual("INVALID_PLAN", caught.exception.code)

    def test_debt_balance_trend_success_requires_period_in_every_returned_row(
        self,
    ) -> None:
        request = {
            "request_id": "debt_trend_period_proof",
            "domain": "receivable",
            "mode": "metric",
            "purpose": "monthly debt trend period proof",
            "metric": "debt_balance_trend",
            "dimensions": [],
            "time_range": {"start": "2026-07-01", "end": "2026-09-01"},
        }

        def execute_good(_sql, _params, _limit, **_kwargs):
            return (
                [
                    {
                        "period": "2026-07",
                        "metric_value": "100.00",
                        tools._INTERNAL_MATCH_COUNT: 1,
                    },
                    {
                        "period": "2026-08",
                        "metric_value": "90.00",
                        tools._INTERNAL_MATCH_COUNT: 1,
                    },
                ],
                False,
                self._read_only_source_evidence(),
            )

        with mock.patch.object(tools, "_execute_with_source", side_effect=execute_good):
            good = json.loads(tools.datasage_query({"requests": [request]}))
        self.assertEqual("success", good["status"])
        period_values = [
            dimension["value"]
            for row in good["results"][0]["claim_ledger"]
            for dimension in row["dimensions"]
            if dimension["label"] == "期间"
        ]
        self.assertEqual(["2026-07", "2026-08"], period_values)

        def execute_bad(_sql, _params, _limit, **_kwargs):
            return (
                [{"metric_value": "190.00", tools._INTERNAL_MATCH_COUNT: 2}],
                False,
                self._read_only_source_evidence(),
            )

        bad_request = {**request, "request_id": "debt_trend_missing_period"}
        with mock.patch.object(tools, "_execute_with_source", side_effect=execute_bad):
            bad = json.loads(tools.datasage_query({"requests": [bad_request]}))
        self.assertEqual("failed", bad["status"])
        self.assertEqual("CONTRACT_UNAVAILABLE", bad["results"][0]["error"]["code"])

    def test_one_observed_on_controls_target_plan_and_calendar_evidence(self) -> None:
        observed_on = date(2031, 12, 31)
        raw_request = {
            "request_id": "target_frozen_observed_on",
            "domain": "target",
            "mode": "metric",
            "purpose": "frozen batch clock proof",
            "metric": "delivery_target_completion",
            "dimensions": [],
            "attribution_mode": "transaction_detail",
            "time_bucket": "month",
            "time_range": {"start": "2031-12-01", "end": "2032-02-01"},
        }
        request = tools._validate_request(raw_request)
        datasets, semantics = tools._contracts("target")
        request = tools._validate_metric_contract(request, semantics)
        sql, _params, scope = tools._build_metric_query(
            request,
            datasets,
            semantics,
            tools._metric_query_limit(request),
            observed_on=observed_on,
        )
        self.assertIn("k.`period` > '2031-12'", sql)
        self.assertIn("AS period_state", sql)
        period = tools._annotate_period_evidence(scope["time_range"], observed_on)
        self.assertEqual(
            "2031-12-31", period["calendar_evidence"]["observed_on"]
        )
        self.assertEqual(
            "in_progress", period["calendar_evidence"]["period_state"]
        )

    def test_unresolved_snapshot_period_is_not_calendar_evidence_and_stays_wire_safe(
        self,
    ) -> None:
        observed_on = date(2026, 8, 25)
        periods = (
            {"source": "latest_snapshot", "resolution_state": "evidence_unavailable"},
            {
                "source": "latest_snapshot_offset",
                "months_before": 1,
                "resolution_state": "evidence_unavailable",
            },
        )
        self.assertNotIn(
            "calendar_evidence",
            tools._annotate_period_evidence(periods[0], observed_on),
        )
        self.assertEqual(
            "not_assessable",
            tools.assess_period_compatibility(*periods)["status"],
        )
        left = self._scalar_calculation_result(
            "snapshot_left", "90", period=("2026-08-01", "2026-09-01")
        )
        right = self._scalar_calculation_result(
            "snapshot_right", "100", period=("2026-07-01", "2026-08-01")
        )
        for result, period in zip((left, right), periods):
            result["applied_time_range"] = copy.deepcopy(period)
            result["claim_ledger"][0]["period"] = copy.deepcopy(period)
            tools.evidence.seal_claim(result["claim_ledger"][0])
        calculations = tools._build_governed_calculations(
            [
                {
                    "calculation_id": "unresolved_snapshot_delta",
                    "operation": "difference",
                    "left_request_id": "snapshot_left",
                    "right_request_id": "snapshot_right",
                }
            ],
            [left, right],
            observed_on=observed_on,
        )
        self.assertEqual(
            "not_assessable",
            calculations[0]["period_compatibility"]["status"],
        )
        projected = tools._model_wire_calculations(calculations, [left, right])[0]
        self.assertEqual("failed", projected["status"])
        self.assertEqual(
            "PERIOD_COMPARABILITY_NOT_ASSESSABLE",
            projected["error"]["code"],
        )
        self.assertNotIn("value", projected)

    def test_partial_ytd_entity_arithmetic_is_not_a_model_visible_yoy_claim(
        self,
    ) -> None:
        observed_on = date(2026, 8, 25)
        result_pairs: list[tuple[dict[str, object], dict[str, object]]] = []
        calculations: list[dict[str, str]] = []
        for entity_id, filter_scope, values in (
            ("vietnam", {"country": "Vietnam"}, ("120", "100")),
            ("thai_kim", {"customer": "Thai Kim"}, ("90", "75")),
        ):
            current = self._scalar_calculation_result(
                f"{entity_id}_2026_ytd",
                values[0],
                period=("2026-01-01", "2026-09-01"),
                filter_scope=filter_scope,
            )
            prior = self._scalar_calculation_result(
                f"{entity_id}_2025_full",
                values[1],
                period=("2025-01-01", "2025-09-01"),
                filter_scope=filter_scope,
            )
            for result in (current, prior):
                annotated = tools._annotate_period_evidence(
                    result["applied_time_range"], observed_on
                )
                result["applied_time_range"] = annotated
                result["claim_ledger"][0]["period"] = copy.deepcopy(annotated)
                tools.evidence.seal_claim(result["claim_ledger"][0])
            result_pairs.append((current, prior))
            calculations.append(
                {
                    "calculation_id": f"{entity_id}_returned_value_ratio",
                    "operation": "ratio",
                    "left_request_id": str(current["request_id"]),
                    "right_request_id": str(prior["request_id"]),
                }
            )

        results = [result for pair in result_pairs for result in pair]
        derived = tools._build_governed_calculations(
            calculations,
            results,
            observed_on=observed_on,
        )
        self.assertEqual(["success", "success"], [item["status"] for item in derived])
        self.assertTrue(all(item["value"] is not None for item in derived))
        self.assertTrue(all(item["allowed_relations"] == [] for item in derived))
        self.assertTrue(
            all(
                item["period_compatibility"]["status"] == "coverage_mismatch"
                for item in derived
            )
        )

        public_results = [tools._model_wire_result(result) for result in results]
        projected = tools._model_wire_calculations(derived, public_results)
        self.assertEqual(["failed", "failed"], [item["status"] for item in projected])
        self.assertTrue(
            all(item["error"]["code"] == "PERIOD_COVERAGE_MISMATCH" for item in projected)
        )
        self.assertTrue(all("value" not in item for item in projected))
        self.assertEqual(
            ["120", "100", "90", "75"],
            [
                result["claim_ledger"][0]["facts"]["metric_value"]
                for result in public_results
            ],
        )
        current_evidence = public_results[0]["applied_time_range"]["calendar_evidence"]
        self.assertEqual("in_progress", current_evidence["period_state"])
        self.assertEqual("not_proven", current_evidence["source_freshness"])

    def test_public_time_range_uses_date_boundaries_across_runtime_paths(self) -> None:
        base = {
            "request_id": "month_boundary_not_public",
            "domain": "delivery",
            "metric": "delivery_amount",
            "dimensions": [],
            "time_range": {"start": "2026-01", "end": "2026-09"},
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"requests": [base]},
                schemas.DATASAGE_QUERY["parameters"],
            )

        for domain, metric in (
            ("delivery", "delivery_amount"),
            ("receipt", "net_receipt_amount"),
            ("target", "delivery_target_completion"),
        ):
            with self.subTest(domain=domain):
                request = {**base, "domain": domain, "metric": metric}
                if domain == "target":
                    request["attribution_mode"] = "transaction_detail"
                with self.assertRaises(tools.QueryFailure) as caught:
                    tools._validate_request(request)
                self.assertEqual("INVALID_INPUT", caught.exception.code)

        accepted = {
            **base,
            "request_id": "date_boundary_public",
            "time_range": {"start": "2026-01-01", "end": "2026-09-01"},
        }
        jsonschema.validate(
            {"requests": [accepted]},
            schemas.DATASAGE_QUERY["parameters"],
        )
        self.assertEqual(
            accepted["time_range"],
            tools._validate_request(accepted)["time_range"],
        )
        for domain, metric in (
            ("delivery", "delivery_amount"),
            ("receipt", "net_receipt_amount"),
            ("target", "delivery_target_completion"),
        ):
            with self.subTest(compiled_domain=domain):
                request = {
                    **accepted,
                    "request_id": f"{domain}_date_boundary_public",
                    "domain": domain,
                    "metric": metric,
                }
                if domain == "target":
                    request["attribution_mode"] = "transaction_detail"
                normalized = tools._validate_request(request)
                datasets, semantics = tools._contracts(domain)
                normalized = tools._validate_metric_contract(normalized, semantics)
                _sql, _params, scope = tools._build_metric_query(
                    normalized,
                    datasets,
                    semantics,
                    tools._metric_query_limit(normalized),
                    observed_on=date(2026, 8, 25),
                )
                self.assertEqual(
                    accepted["time_range"],
                    {
                        "start": scope["time_range"]["start"],
                        "end": scope["time_range"]["end"],
                    },
                )

    def test_live_host_resolves_only_the_bare_datasage_skill_name(self) -> None:
        host_root = PROFILE_ROOT.parent.parent / "hermes-agent"
        host_python = host_root / "venv" / "Scripts" / "python.exe"
        if not host_python.is_file():
            self.skipTest("Hermes host runtime is unavailable")
        code = (
            "import json; from tools.skills_tool import skill_view; "
            "files=['references/answer-boundary.md','references/delivery-analysis.md',"
            "'references/entity-guidance.md','references/query-rules.md']; "
            "print(json.dumps({'bare':json.loads(skill_view('datasage', preprocess=False)), "
            "'qualified':json.loads(skill_view('datasage:datasage', preprocess=False)), "
            "'references':[json.loads(skill_view('datasage', file_path=f, preprocess=False)) "
            "for f in files]}, ensure_ascii=False))"
        )
        environment = dict(os.environ)
        environment.update(
            {
                "HERMES_HOME": str(PROFILE_ROOT),
                "PYTHONPATH": str(host_root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        completed = subprocess.run(
            [str(host_python), "-B", "-c", code],
            cwd=host_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        resolved = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(resolved["bare"].get("success"), resolved)
        self.assertIn("# DataSage", resolved["bare"].get("content", ""))
        self.assertFalse(resolved["qualified"].get("success"), resolved)
        self.assertTrue(
            all(item.get("success") is True for item in resolved["references"]),
            resolved,
        )
        main_skill = _main_skill()
        normalized = " ".join(main_skill.split())
        self.assertIn("skill_view(name=\"datasage\", file_path=", normalized)

    def test_main_skill_keeps_transient_analysis_out_of_memory_without_host_loop_rules(
        self,
    ) -> None:
        main_skill = _main_skill()
        normalized = " ".join(main_skill.split())
        self.assertIn("Conversation history, not persistent memory", normalized)
        self.assertIn("temporary or candidate entity mappings", normalized)
        self.assertIn("stable cross-session preference", normalized)
        self.assertNotIn("contains `tool_calls` is interim", normalized)
        self.assertNotIn("tool-free assistant message", normalized)

    def test_answer_policy_owns_the_governed_calculation_batch_boundary(self) -> None:
        answer_boundary = " ".join(_answer_boundary().split())
        self.assertIn("same `datasage_query` call", answer_boundary)
        self.assertIn("compatible scalar evidence already returned", answer_boundary)
        self.assertIn("explicitly labeled transparent arithmetic", answer_boundary)

    def test_unknown_geography_never_auto_binds_a_governed_filter_value(self) -> None:
        for token in ("泰国", "未注册地域名称"):
            with self.subTest(token=token), mock.patch.object(
                tools, "_execute", return_value=([], False)
            ):
                payload = json.loads(
                    entities.datasage_entity_resolve({"token": token})
                )
            self.assertEqual("not_found", payload["status"])
            self.assertTrue(payload["must_stop_business_query"])
            self.assertEqual([], payload["candidates"])

    def test_registered_bkk_remains_an_exact_department_alias(self) -> None:
        stable_payload = json.loads(
            entities.datasage_entity_resolve(
                {"token": "BKK", "entity_types": ["department"]}
            )
        )
        self.assertEqual("resolved", stable_payload["status"])
        self.assertEqual("registered_exact", stable_payload["resolution_path"])

    def test_dimension_labels_cannot_become_mapping_or_durable_memory_facts(
        self,
    ) -> None:
        guidance = (
            PROFILE_ROOT
            / "skills"
            / "business-analytics"
            / "datasage"
            / "references"
            / "entity-guidance.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(guidance.split())
        self.assertIn("dimension breakdown only enumerates observed labels", normalized)
        self.assertIn("does not prove an alias or create a candidate mapping", normalized)
        self.assertIn("must not become a durable Memory fact", normalized)


if __name__ == "__main__":
    unittest.main()
