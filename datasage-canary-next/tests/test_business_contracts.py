from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import importlib
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import yaml


PROFILE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROFILE_ROOT / "plugins" / "datasage-query"
os.environ["HERMES_HOME"] = str(PROFILE_ROOT)
TEST_PACKAGE = "datasage_query_contract_tests"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[TEST_PACKAGE] = package

contracts = importlib.import_module(f"{TEST_PACKAGE}.contracts")
schemas = importlib.import_module(f"{TEST_PACKAGE}.schemas")
skill_prompt = importlib.import_module(f"{TEST_PACKAGE}.skill_prompt")
tools = importlib.import_module(f"{TEST_PACKAGE}.tools")
runtime_health = importlib.import_module(f"{TEST_PACKAGE}.runtime_health")


class BusinessContractTests(unittest.TestCase):
    @staticmethod
    def _metric_detail_receipt(domain: str, metric: str) -> str:
        payload = json.loads(
            contracts.datasage_catalog(
                {"requests": [{"domain": domain, "metric": metric}]}
            )
        )
        if payload.get("status") != "success":
            raise AssertionError(payload)
        return str(payload["content_hash"])

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

    def test_user_visible_datasage_skills_survive_tool_search_deferral(self) -> None:
        for relative_path in (
            "skills/datasage/SKILL.md",
            "skills/datasage/datasage-query-patterns/SKILL.md",
        ):
            content = (PROFILE_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("requires_toolsets: [datasage-query]", content)

        internal_foundation = (
            PROFILE_ROOT / "skills/common-data-foundation/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "requires_toolsets: [datasage-query]",
            internal_foundation,
        )

    def test_user_visible_skills_declare_the_official_tool_search_bridge(self) -> None:
        main_skill = (PROFILE_ROOT / "skills/datasage/SKILL.md").read_text(
            encoding="utf-8"
        )
        companion = (
            PROFILE_ROOT / "skills/datasage/datasage-query-patterns/SKILL.md"
        ).read_text(encoding="utf-8")

        for tool_name in ("`tool_search`", "`tool_describe`", "`tool_call`"):
            self.assertIn(tool_name, main_skill)
            self.assertIn(tool_name, companion)
        for boundary in (
            "authorization",
            "query preflight",
            "evidence boundaries",
            "non-causality rules",
        ):
            self.assertIn(boundary, main_skill)
        for content in (main_skill, companion):
            self.assertIn("Never guess a tool name or argument", content)

    def test_main_skill_top_n_disclosure_depends_on_returned_state(self) -> None:
        content = (PROFILE_ROOT / "skills/datasage/SKILL.md").read_text(
            encoding="utf-8"
        )
        finalization = content.split("Finalization:", 1)[1].split(
            "## Stop and failure behavior", 1
        )[0]
        top_n_rule = next(
            paragraph
            for paragraph in finalization.split("\n-")
            if "ranked or Top-N result" in paragraph
        )
        normalized = " ".join(top_n_rule.split())

        for required in (
            "ranked or Top-N result",
            "returned `datasage_query`",
            "`truncated` and `data_state`",
            "If `truncated: true` OR `data_state: truncated`",
            "only the requested Top N is returned",
            "source result was truncated",
            "never imply a complete ranking",
            "When `truncated` is not `true` AND `data_state` is not `truncated`",
            "do not claim or imply that the result is truncated",
        ):
            self.assertIn(required, normalized)
        self.assertNotIn(
            "`truncated: true` AND `data_state: truncated`",
            normalized,
        )

    def test_complete_change_finalization_reports_noncausal_structural_contribution(
        self,
    ) -> None:
        content = (PROFILE_ROOT / "skills/datasage/SKILL.md").read_text(
            encoding="utf-8"
        )
        finalization = content.split("Finalization:", 1)[1].split(
            "## Stop and failure behavior", 1
        )[0]
        contribution_rule = next(
            paragraph
            for paragraph in finalization.split("\n-")
            if "`operation` is `complete_change_decomposition`" in " ".join(
                paragraph.split()
            )
        )
        normalized = " ".join(contribution_rule.split())

        for required in (
            "only when `operation` is `complete_change_decomposition` AND the returned "
            "`change_reconciliation.status` is explicitly `reconciled`",
            "a general `status: success` does not authorize it",
            "“结构贡献” / “structural contribution”",
            "strictly equivalent non-causal accounting term",
            "returned overall delta",
            "using only that partition's returned `delta_value`",
            "cover all returned partitions",
            "response's reconciliation basis",
            "Call a partition a structural contributor and report a rate only when that "
            "same returned claim has `structural_contribution` in `allowed_relations`",
            "valid seal covers a returned `facts.net_change_contribution_rate`",
            "zero-delta partition or a claim without that relation is not a structural "
            "contributor and has no zero rate to fill",
            "Never describe structural contribution as a cause, driver, or causal explanation",
            "When reporting “结构贡献” for this authorized reconciled operation",
            "state in the same paragraph or adjacent sentence: "
            "“这是净变化的结构分解，不代表业务原因或驱动。”",
            "Outside that negated boundary, never name a partition with 原因, 驱动, "
            "导致, or causal equivalents",
            "Use each authorized returned decimal-string rate directly",
            "signed dimensionless fraction: `1` means `100%`",
            "negative values and absolute values greater than `1` are valid",
            "multiply by 100 exactly once",
            "one consistent display precision across partitions",
            "Preserve every nonzero direction",
            "show `0 < rate < threshold` for a positive rate or "
            "`-threshold < rate < 0` for a negative rate",
            "never show it as `0.00%` or `-0.00%`",
            "Never recompute a rate from visible amounts, scale it twice, take "
            "its absolute "
            "value, clamp it, normalize partition rates to 100%, or force them to sum "
            "to 100%",
            "do not calculate, infer, or invent it",
            "absence for a zero overall delta or zero partition delta is not a zero rate",
            "When the returned `change_reconciliation.status` is `not_reconciled`, "
            "or when `change_reconciliation` or its status is missing",
            "preserve the returned gap or local-result scope and never call it structural "
            "contribution",
        ):
            self.assertIn(required, normalized)
        self.assertNotIn(
            "successful `complete_change_decomposition`",
            normalized,
        )

        main_skill = skill_prompt.load_main_skill(PROFILE_ROOT)
        hook = skill_prompt.build_wecom_skill_hook(main_skill)
        projected = hook(platform="wecom", is_first_turn=True)
        self.assertIsInstance(projected, dict)
        context = projected["context"]
        projected_normalized = " ".join(context.split())
        self.assertIn('authority="git"', context)
        self.assertIn('immutable="process"', context)
        for required in (
            "only when `operation` is `complete_change_decomposition` AND the returned "
            "`change_reconciliation.status` is explicitly `reconciled`",
            "a general `status: success` does not authorize it",
            "using only that partition's returned `delta_value`",
            "Call a partition a structural contributor and report a rate only when that "
            "same returned claim has `structural_contribution` in `allowed_relations`",
            "zero-delta partition or a claim without that relation is not a structural "
            "contributor and has no zero rate to fill",
            "Never describe structural contribution as a cause, driver, or causal explanation",
            "When reporting “结构贡献” for this authorized reconciled operation",
            "state in the same paragraph or adjacent sentence: "
            "“这是净变化的结构分解，不代表业务原因或驱动。”",
            "Outside that negated boundary, never name a partition with 原因, 驱动, "
            "导致, or causal equivalents",
            "Use each authorized returned decimal-string rate directly",
            "multiply by 100 exactly once",
            "show `0 < rate < threshold` for a positive rate or "
            "`-threshold < rate < 0` for a negative rate",
            "never show it as `0.00%` or `-0.00%`",
            "Never recompute a rate from visible amounts, scale it twice, take its absolute "
            "value, clamp it, normalize partition rates to 100%, or force them to sum "
            "to 100%",
            "absence for a zero overall delta or zero partition delta is not a zero rate",
            "When the returned `change_reconciliation.status` is `not_reconciled`, "
            "or when `change_reconciliation` or its status is missing",
            "preserve the returned gap or local-result scope and never call it structural "
            "contribution",
        ):
            self.assertIn(required, projected_normalized)

        governance_rule = content.split(
            "The default governance path is", 1
        )[1].split("### Plan and bind scope", 1)[0]
        governance_normalized = " ".join(governance_rule.split())
        for required in (
            "Apply `expert_index -> metric_detail -> query` per newly selected metric "
            "branch, not per turn",
            "Run it for a new Hermes session, changed domain or metric, or no retained "
            "proof of selection plus successful detail",
            "In the same Hermes session, reuse the prior successful detail receipt only "
            "for that metric while runtime accepts it",
            "If rejected as stale or invalid, refresh only the selected detail",
            "refresh `expert_index` only if selection becomes unsupported or ambiguous",
            "A new turn alone never forces refresh",
        ):
            self.assertIn(required, governance_normalized)
            self.assertIn(required, projected_normalized)

        semantics = yaml.safe_load(
            (PLUGIN_ROOT / "contracts" / "delivery-semantics.yaml").read_text(
                encoding="utf-8"
            )
        )
        answer_contract_metrics = {
            metric_code
            for metric_code, definition in semantics["metrics"].items()
            if definition.get("answer_contract") is not None
        }
        self.assertEqual({"delivery_amount"}, answer_contract_metrics)
        expected_answer_contract = (
            "仅当返回的是完整变化分解且状态已对账时，才可称为结构贡献；凡实际报告结构贡献，"
            "必须在同段或紧邻句明确说明：这是净变化的结构分解，不代表业务原因或驱动。"
            "除该否定边界外，不得用原因、驱动或导致命名任何分区。"
        )
        self.assertEqual(
            [expected_answer_contract],
            semantics["metrics"]["delivery_amount"]["answer_contract"],
        )
        serialized_semantics = json.dumps(semantics, ensure_ascii=False)
        self.assertNotIn(
            "“原因”只允许指向查询证实的客户、产品、业务员、部门或组织结构变化",
            serialized_semantics,
        )
        self.assertIn(
            "结构变化只能称为结构观察；完整对账授权时可称为结构贡献，"
            "但均不得称为原因、驱动或导致",
            serialized_semantics,
        )

        detail = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {"domain": "delivery", "metric": "delivery_amount"}
                    ]
                }
            )
        )
        self.assertEqual("success", detail["status"])
        self.assertEqual(
            [expected_answer_contract],
            detail["results"][0]["metric"]["answer_contract"],
        )
        current_receipt = detail.pop("content_hash")
        stale_detail = json.loads(json.dumps(detail, ensure_ascii=False))
        stale_detail["results"][0]["metric"].pop("answer_contract")
        stale_receipt = hashlib.sha256(
            json.dumps(
                stale_detail,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(current_receipt, stale_receipt)
        stale_request = {
            "request_id": "delivery_old_answer_contract_receipt",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "verify prior delivery detail receipt is invalid",
            "metric": "delivery_amount",
            "dimensions": [],
            "time_range": {"start": "2026-07-01", "end": "2026-08-01"},
            "detail_receipt": stale_receipt,
        }
        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("stale receipt must fail before database"),
        ) as execute:
            stale_result = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [stale_request]}
                )
            )
        execute.assert_not_called()
        self.assertEqual("failed", stale_result["status"])
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", stale_result["error"]["code"]
        )

    def test_model_visible_contribution_rate_wire_contract_is_direct_use_only(
        self,
    ) -> None:
        payload = json.loads(
            contracts.datasage_catalog({"requests": [{"domain": "delivery"}]})
        )
        self.assertEqual("success", payload["status"])
        affordances = payload["results"][0]["analysis_affordances"]
        self.assertEqual("datasage-analysis-affordances/v8", affordances["version"])
        contract = affordances["claim_wire_contracts"][
            "net_change_contribution_rate"
        ]

        self.assertEqual(
            "results[].claim_ledger[].facts.net_change_contribution_rate",
            contract["field_path"],
        )
        self.assertEqual(
            [
                "same_result_change_reconciliation_operation_is_complete_change_decomposition",
                "same_result_change_reconciliation_status_is_reconciled",
                "claim_is_validly_sealed",
                "claim_allowed_relations_contains_structural_contribution",
                "returned_overall_delta_is_nonzero",
                "producer_returned_the_field",
            ],
            contract["consume_only_when"],
        )
        self.assertEqual(
            "result_status_success_alone_never_authorizes_consumption",
            contract["success_boundary"],
        )
        self.assertEqual("decimal_string", contract["wire_type"])
        self.assertEqual("signed_dimensionless_fraction", contract["semantic_type"])
        self.assertEqual("one_equals_one_hundred_percent", contract["scale"])
        self.assertEqual(
            "negative_and_absolute_value_greater_than_one_are_valid",
            contract["valid_range"],
        )
        self.assertEqual(
            "sealed_returned_value_direct_use_only",
            contract["provenance"],
        )
        self.assertEqual(
            "multiply_by_100_exactly_once_with_one_consistent_display_precision",
            contract["percentage_display"],
        )

        forbidden = set(contract["forbidden_transformations"])
        self.assertEqual(
            {
                "recompute_from_visible_amounts",
                "take_absolute_value",
                "clamp",
                "normalize_partition_rates_to_one_hundred_percent",
                "force_partition_rates_to_sum_to_one_hundred_percent",
                "invent_or_fill_when_absent",
            },
            forbidden,
        )
        self.assertEqual(
            {
                "zero_overall_delta": "field_absent_not_zero_rate",
                "zero_partition_delta": "field_absent_not_zero_rate",
                "producer_omission": "field_absent_never_infer_or_fill",
            },
            contract["absence_semantics"],
        )

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

    def test_domain_analysis_seeds_use_an_adaptive_soft_budget(self) -> None:
        for domain in ("receipt", "receivable", "inventory", "target"):
            content = (
                PROFILE_ROOT
                / "skills"
                / f"{domain}-query"
                / "references"
                / "planner-contract.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("自适应软预算", content)
            self.assertIn("不设固定查询、追问或轮数上限", content)
            self.assertNotIn("最多在首批结果暴露实质缺口时追加一次", content)

    def test_receipt_detail_gate_and_required_answer_scope_survive_model_wire(
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

        unique = expert_index["metric_selection_boundary"]["branches"][
            "unique_compatible"
        ]
        self.assertEqual(
            {
                "selected_metric.exact_default_lookup_supported": True,
                "explicit_qualifiers_present": False,
            },
            unique["direct_query_when_all"],
        )
        detail_conditions = unique["detail_first_when_any"]
        self.assertIn(
            {"selected_metric.exact_default_lookup_supported": False},
            detail_conditions,
        )
        self.assertIn(
            {"selected_metric.exact_default_lookup_supported": "missing"},
            detail_conditions,
        )
        qualifier_condition = next(
            condition
            for condition in detail_conditions
            if condition.get("explicit_qualifiers_present") is True
        )
        self.assertIs(qualifier_condition["explicit_qualifiers_present"], True)
        self.assertEqual(
            {
                "calendar_month",
                "time_range",
                "dimensions",
                "filters",
                "entity",
                "comparison",
                "decomposition",
                "ranking",
            },
            set(qualifier_condition["examples"]),
        )
        self.assertEqual(
            {"dimensions": []},
            qualifier_condition["empty_values_do_not_count_as_present"],
        )
        self.assertIs(
            unique["direct_query_when_all"]["explicit_qualifiers_present"],
            False,
        )
        self.assertEqual(
            "query_selected_metric_at_exact_governed_default",
            unique["actions"]["direct_query"],
        )
        self.assertEqual(
            "load_selected_metric_detail_before_query",
            unique["actions"]["detail_first"],
        )
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
                                "detail_receipt": self._metric_detail_receipt(
                                    "receipt", "net_receipt_amount"
                                ),
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
        self.assertNotIn(
            self._metric_detail_receipt("receipt", "net_receipt_amount"),
            json.dumps(query_payload, ensure_ascii=False),
        )
        self.assertIn("2026-07-01", captured["params"])
        self.assertIn("2026-08-01", captured["params"])
        result = query_payload["results"][0]
        self.assertEqual(
            {
                "start": "2026-07-01",
                "end": "2026-08-01",
                "source": "explicit",
            },
            result["applied_time_range"],
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
            "receipt.domain.scope": "收款及退款域指标均包含内部客户并排除A状态。",
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
            request = {
                **request,
                "detail_receipt": self._metric_detail_receipt(
                    str(request["domain"]), str(request["metric"])
                ),
            }
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
        self.assertEqual("datasage-mini-receipt-semantics/v9", receipt_contract["version"])
        self.assertIn(
            "本次实际查询的用途分组或筛选范围",
            "\n".join(receipt_contract["answer_contract"]),
        )

        query_description = schemas.DATASAGE_QUERY["description"]
        self.assertIn("exact_default_lookup_supported: true", query_description)
        self.assertIn("false or missing", query_description)
        self.assertIn("calendar_month", query_description)
        self.assertIn("time_range", query_description)
        self.assertIn("answer_scope_line", query_description)
        self.assertIn("Every sealed disclosure_ledger item", query_description)
        self.assertIn(
            "explicit, non-default qualifier",
            schemas.REQUEST["properties"]["calendar_month"]["description"],
        )
        self.assertIn(
            "explicit, non-default qualifier",
            schemas.REQUEST["properties"]["time_range"]["description"],
        )

        main_skill = skill_prompt.load_main_skill(PROFILE_ROOT)
        patterns = (
            PROFILE_ROOT / "skills/datasage/datasage-query-patterns/SKILL.md"
        ).read_text(encoding="utf-8")
        hook_context = skill_prompt.build_wecom_skill_hook(main_skill)(
            platform="wecom",
            is_first_turn=True,
        )["context"]
        for content in (main_skill, patterns, hook_context):
            normalized = " ".join(content.split())
            self.assertIn("`exact_default_lookup_supported: true`", normalized)
            self.assertIn("false or missing", normalized)
            self.assertIn("`calendar_month` and `time_range`", normalized)
        normalized_hook = " ".join(hook_context.split())
        self.assertIn("When `answer_scope_line` is non-empty", normalized_hook)
        self.assertIn("faithfully state its actual returned range", normalized_hook)
        self.assertIn("For every sealed `disclosure_ledger` item", normalized_hook)
        self.assertIn("whose `applies` value is `true`", normalized_hook)
        self.assertIn("fully cover all of its independent business propositions", normalized_hook)
        self.assertIn("Natural rewording and lossless merging", normalized_hook)
        self.assertIn("inclusion, exclusion, definition, or conditional scope", normalized_hook)
        self.assertIn("another request, metric, or domain", normalized_hook)
        self.assertIn("Semicolon-separated, coordinated, and conditional clauses", normalized_hook)
        self.assertIn("never start another catalog, detail, or query call", normalized_hook)
        for content in (main_skill, hook_context):
            self.assertNotIn("one by one", content)
            self.assertNotIn("must not drop or merge away", content)

    def test_runtime_metric_detail_receipt_gate_fails_closed_before_database(
        self,
    ) -> None:
        def validate(request: dict[str, object]) -> dict[str, object]:
            normalized = tools._validate_inventory_metric_scope(
                tools._validate_delivery_metric_scope(
                    tools._validate_request(request)
                )
            )
            _, semantics = tools._contracts(str(normalized["domain"]))
            return tools._validate_metric_detail_gate(normalized, semantics)

        base = {
            "request_id": "receipt_gate",
            "domain": "receipt",
            "mode": "metric",
            "purpose": "offline metric-detail receipt gate test",
            "metric": "net_receipt_amount",
            "dimensions": [],
        }
        correct = self._metric_detail_receipt("receipt", "net_receipt_amount")

        with self.assertRaises(tools.QueryFailure) as missing:
            validate(base)
        self.assertEqual("METRIC_DETAIL_REQUIRED", missing.exception.code)

        wrong_metric_receipt = self._metric_detail_receipt(
            "receipt", "receipt_amount"
        )
        with self.assertRaises(tools.QueryFailure) as wrong_metric:
            validate({**base, "detail_receipt": wrong_metric_receipt})
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", wrong_metric.exception.code
        )

        tampered = ("0" if correct[0] != "0" else "1") + correct[1:]
        with self.assertRaises(tools.QueryFailure) as tampered_error:
            validate({**base, "detail_receipt": tampered})
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", tampered_error.exception.code
        )

        stale_current = "f" * 64 if correct != "f" * 64 else "e" * 64
        with mock.patch.object(
            tools,
            "_current_metric_detail_receipt",
            return_value=stale_current,
        ):
            with self.assertRaises(tools.QueryFailure) as expired:
                validate({**base, "detail_receipt": correct})
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", expired.exception.code
        )

        with self.assertRaises(tools.QueryFailure) as dimension_overreach:
            validate(
                {
                    **base,
                    "detail_receipt": correct,
                    "dimensions": ["warehouse"],
                }
            )
        self.assertEqual("UNSUPPORTED_DIMENSION", dimension_overreach.exception.code)

        with self.assertRaises(tools.QueryFailure) as time_overreach:
            validate(
                {
                    **base,
                    "detail_receipt": correct,
                    "time_range": {
                        "start": "2020-01-01",
                        "end": "2026-01-01",
                    },
                }
            )
        self.assertEqual("QUERY_RANGE_TOO_WIDE", time_overreach.exception.code)

        exact_default = validate(
            {
                "request_id": "delivery_exact_default",
                "domain": "delivery",
                "mode": "metric",
                "purpose": "offline exact-default exception test",
                "metric": "delivery_amount",
                "dimensions": [],
            }
        )
        self.assertNotIn("detail_receipt", exact_default)

        with mock.patch.object(
            tools,
            "_execute_with_source",
            return_value=(
                [{"metric_value": "42.00"}],
                False,
                self._read_only_source_evidence(),
            ),
        ):
            exact_default_query = json.loads(
                tools.datasage_query(
                    {
                        "requests": [
                            {
                                "request_id": "delivery_exact_default_query",
                                "domain": "delivery",
                                "mode": "metric",
                                "purpose": "offline exact-default execution test",
                                "metric": "delivery_amount",
                                "dimensions": [],
                            }
                        ]
                    }
                )
            )
        self.assertEqual("success", exact_default_query["status"])
        self.assertEqual(
            "success", exact_default_query["results"][0]["status"]
        )

        self.assertIn("detail_receipt", schemas.REQUEST["properties"])
        self.assertNotIn("detail_receipt", schemas.REQUEST["required"])
        query_description = schemas.DATASAGE_QUERY["description"]
        self.assertIn("content_hash", query_description)
        self.assertIn("before any database access", query_description)
        for skill_path in (
            PROFILE_ROOT / "skills" / "datasage" / "SKILL.md",
            PROFILE_ROOT
            / "skills"
            / "datasage"
            / "datasage-query-patterns"
            / "SKILL.md",
        ):
            skill_content = skill_path.read_text(encoding="utf-8")
            self.assertIn("`content_hash`", skill_content)
            self.assertIn("`detail_receipt`", skill_content)
            self.assertIn("unchanged", skill_content)

        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("database access must not occur"),
        ):
            runtime_failure = json.loads(
                tools.runtime_guarded_datasage_query({"requests": [base]})
            )
        self.assertEqual("failed", runtime_failure["status"])
        self.assertEqual(
            "METRIC_DETAIL_REQUIRED", runtime_failure["error"]["code"]
        )

    def test_public_runtime_receipt_gate_covers_plan_batch_decomposition_and_wire(
        self,
    ) -> None:
        receipt = self._metric_detail_receipt("receipt", "net_receipt_amount")
        late_plan = {
            "request_id": "late_plan",
            "domain": "receipt",
            "mode": "metric",
            "purpose": "public pre-entity capability test",
            "metric": "net_receipt_amount",
            "detail_receipt": receipt,
            "dimensions": [],
            "metric_filters": {"customer": "X"},
            "order_by": {"field": "metric_value", "direction": "desc"},
        }
        with (
            mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=AssertionError("entity/database access must not occur"),
            ) as execute,
            mock.patch.object(
                runtime_health,
                "query_readiness_status",
                return_value={"ready": True},
            ),
        ):
            late_failure = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [late_plan]}
                )
            )
        self.assertEqual("failed", late_failure["status"])
        self.assertEqual("INVALID_PLAN", late_failure["error"]["code"])
        execute.assert_not_called()

        exact_default = {
            "request_id": "batch_exact_default",
            "domain": "delivery",
            "mode": "metric",
            "purpose": "public batch receipt test",
            "metric": "delivery_amount",
            "dimensions": [],
        }
        missing_receipt = {
            "request_id": "batch_missing_receipt",
            "domain": "receipt",
            "mode": "metric",
            "purpose": "public batch receipt test",
            "metric": "net_receipt_amount",
            "dimensions": [],
        }
        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("batch validation must be DB-free"),
        ) as execute:
            batch_failure = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [exact_default, missing_receipt]}
                )
            )
        self.assertEqual("METRIC_DETAIL_REQUIRED", batch_failure["error"]["code"])
        execute.assert_not_called()

        decomposition = {
            "request_id": "bad_decomposition_receipt",
            "domain": "receipt",
            "mode": "metric",
            "purpose": "public decomposition receipt test",
            "metric": "net_receipt_amount",
            "detail_receipt": ("0" if receipt[0] != "0" else "1") + receipt[1:],
            "time_range": {"start": "2026-01-01", "end": "2026-02-01"},
            "complete_change_decomposition": {"dimension": "customer"},
        }
        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("decomposition validation must be DB-free"),
        ) as execute:
            decomposition_failure = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [decomposition]}
                )
            )
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID",
            decomposition_failure["error"]["code"],
        )
        execute.assert_not_called()

        wire_request = {
            **exact_default,
            "request_id": "receipt_non_leak",
            "purpose": "public receipt non-leak test",
        }
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
            ),
        ):
            without_receipt = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [wire_request]}
                )
            )
            delivery_receipt = self._metric_detail_receipt(
                "delivery",
                "delivery_amount",
            )
            with_receipt = json.loads(
                tools.runtime_guarded_datasage_query(
                    {
                        "requests": [
                            {**wire_request, "detail_receipt": delivery_receipt}
                        ]
                    }
                )
            )
        self.assertEqual("success", without_receipt["status"])
        self.assertEqual("success", with_receipt["status"])
        self.assertEqual(
            without_receipt["results"],
            with_receipt["results"],
        )
        self.assertEqual(
            without_receipt["evidence_bundle"],
            with_receipt["evidence_bundle"],
        )
        self.assertNotIn(
            delivery_receipt,
            json.dumps(with_receipt, ensure_ascii=False),
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
            request = {
                **request,
                "detail_receipt": self._metric_detail_receipt(
                    str(request["domain"]), str(request["metric"])
                ),
            }
            calls: list[dict[str, object]] = []

            def execute_query(sql, params, limit, **_kwargs):
                calls.append({"sql": sql, "params": list(params), "limit": limit})
                return rows, False, self._read_only_source_evidence()

            with mock.patch.object(
                tools,
                "_execute_with_source",
                side_effect=execute_query,
            ):
                payload = json.loads(tools.datasage_query({"requests": [request]}))
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
            "customer-risk.formal-receivable-turnover.external-customer.scope"
        )
        formal_dso_coverage_id = (
            "customer-risk.formal-receivable-turnover.coverage"
        )
        formal_dso_text = "正式应收周转天数的月末净欠款和毛出库分母均固定排除内部客户。"
        formal_dso_formula_id = (
            "customer-risk.formal-receivable-turnover.formula"
        )
        formal_dso_formula_text = (
            "正式应收周转天数按平均月末净欠款除以同期毛出库金额，再乘期间自然日数计算。"
        )
        customer_risk_contract = yaml.safe_load(
            (
                PROFILE_ROOT
                / "plugins/datasage-query/contracts/customer_risk-semantics.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "datasage-mini-customer-risk-semantics/v7",
            customer_risk_contract["version"],
        )
        formal_dso_declarations = customer_risk_contract["metrics"][
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
        self.assertNotIn("default_disclosures", customer_risk_contract)
        detail = json.loads(
            contracts.datasage_catalog(
                {
                    "requests": [
                        {
                            "domain": "customer_risk",
                            "metric": "formal_receivable_turnover_days",
                        }
                    ]
                }
            )
        )
        self.assertEqual("success", detail["status"])
        answer_contract = detail["results"][0]["metric"]["answer_contract"]
        self.assertEqual(3, len(answer_contract))
        answer_contract_text = "\n".join(answer_contract)
        for proposition in (
            "正式应收周转天数",
            "未定义",
            "平均月末净经营欠款",
            "同期毛出库额",
            "期间自然日数",
            "月末欠款快照月数",
            "有效出库月份数",
            "排除内部客户的双侧范围",
            "密封有效",
            "未定义而不是错误",
            "typed undefined",
            "称组成项完整",
        ):
            self.assertIn(proposition, answer_contract_text)
        self.assertNotIn(
            "平均月末净欠款除以同期毛出库金额再乘期间自然日数",
            answer_contract_text,
        )
        formula_contract = answer_contract[1]
        for condition in (
            "同一已验证密封 claim",
            "同期毛出库额",
            "同一次 datasage_query",
            "formal-receivable-turnover-calculation-attestation/v1",
            "状态为 verified",
            "attestation_seal 与外层 claim_seal 均有效",
            "组成项均存在、有效、密封且与本次范围绑定",
            "公式披露与双侧客户范围披露均适用且密封有效",
            "同一查询已密封的正式公式披露",
            "同期毛出库额非正",
            "必须保持 typed undefined",
            "清除正式周转数值、公式与推理主题",
        ):
            self.assertIn(condition, formula_contract)
        self.assertIn(
            "attestation 缺失、无效或状态为 undefined 时",
            formula_contract,
        )

        dso_rows = [
            {
                "metric_value": "42.00",
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
            "domain": "customer_risk",
            "mode": "metric",
            "purpose": "synthetic formal DSO disclosure test",
            "metric": "formal_receivable_turnover_days",
            "dimensions": [],
        }
        captured_dso_raw: list[dict[str, object]] = []
        original_model_wire_result = tools._model_wire_result

        def capture_dso_raw(result):
            before_projection = json.loads(json.dumps(result))
            projected = original_model_wire_result(result)
            self.assertEqual(before_projection, result)
            captured_dso_raw.append(before_projection)
            return projected

        with mock.patch.object(
            tools,
            "_model_wire_result",
            side_effect=capture_dso_raw,
        ):
            dso_result, dso_calls = run_query(dso_request, dso_rows)
        self.assertEqual(1, len(captured_dso_raw))
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
            "formal-receivable-turnover-calculation-attestation/v1",
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
            "metric_value": "42.00",
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
            self.assertEqual([], projected["allowed_reasoning_topics"])
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
            "effective_month_count": "11",
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
                "COMPLETE_13_MONTH_END_SNAPSHOTS",
            ),
            (
                "effective_months_incomplete",
                {**dso_rows[0], "effective_month_count": "11"},
                "COMPLETE_12_EFFECTIVE_MONTHS",
            ),
            (
                "effective_months_fractional",
                {**dso_rows[0], "effective_month_count": "12.5"},
                "COMPLETE_12_EFFECTIVE_MONTHS",
            ),
            (
                "effective_months_boolean",
                {**dso_rows[0], "effective_month_count": True},
                "COMPLETE_12_EFFECTIVE_MONTHS",
            ),
        )
        for case_name, row, reason in undefined_cases:
            captured_undefined_raw: list[dict[str, object]] = []

            def capture_undefined_raw(raw_result):
                before_projection = json.loads(json.dumps(raw_result))
                projected = original_model_wire_result(raw_result)
                self.assertEqual(before_projection, raw_result)
                captured_undefined_raw.append(before_projection)
                return projected

            context = (
                mock.patch.object(
                    tools,
                    "_model_wire_result",
                    side_effect=capture_undefined_raw,
                )
                if case_name == "effective_months_incomplete"
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
            self.assertEqual([], result["allowed_reasoning_topics"], case_name)
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
            if case_name == "effective_months_incomplete":
                self.assertEqual(1, len(captured_undefined_raw))
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
        self.assertIn(
            "COMPLETE_12_NATURAL_MONTH_WINDOW",
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

        current_receipt = detail["content_hash"]
        stale_detail = json.loads(json.dumps(detail, ensure_ascii=False))
        stale_detail.pop("content_hash")
        stale_detail["results"][0]["metric"]["answer_contract"][1] = (
            "同时呈现已返回的平均净经营欠款、期间自然日数、月末欠款快照月数和有效出库月份数；"
            "只有同一次 datasage_query 返回的 "
            "formal-receivable-turnover-calculation-attestation/v1 状态为 verified、"
            "attestation_seal 与外层 claim_seal 均有效，且公式披露与双侧客户范围披露"
            "均适用且密封有效时，才可忠实呈现同一查询已密封的正式公式披露；"
            "attestation 缺失、无效或状态为 undefined 时，不得依据本 catalog 合同直接陈述"
            "正式公式或正式周转数值。"
        )
        stale_receipt = hashlib.sha256(
            json.dumps(
                stale_detail,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(current_receipt, stale_receipt)
        stale_request = {
            **dso_request,
            "request_id": "formal_dso_old_component_contract_receipt",
            "dimensions": ["department"],
            "detail_receipt": stale_receipt,
        }
        with mock.patch.object(
            tools.entities,
            "prefetch_metric_entities",
            side_effect=AssertionError("stale receipt must fail before entity work"),
        ) as prefetch, mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("stale receipt must fail before database"),
        ) as execute:
            stale_payload = json.loads(
                tools.runtime_guarded_datasage_query({"requests": [stale_request]})
            )
        prefetch.assert_not_called()
        execute.assert_not_called()
        self.assertEqual("failed", stale_payload["status"])
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", stale_payload["error"]["code"]
        )

        delivery_receipt_result, _ = run_query(
            {
                "request_id": "delivery_receipt_comparison",
                "domain": "customer_risk",
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
        self.assertIn("customer-risk.delivery-receipt.scope-asymmetry", delivery_receipt_ids)
        self.assertNotIn(formal_dso_disclosure_id, delivery_receipt_ids)

        main_skill = skill_prompt.load_main_skill(PROFILE_ROOT)
        hook = skill_prompt.build_wecom_skill_hook(main_skill)
        hook_context = hook(platform="wecom", is_first_turn=True)["context"]
        normalized_hook = " ".join(hook_context.split())
        for required in (
            "formal receivable turnover days or formal DSO",
            "`customer_risk` expert index",
            "ordinary net debt, aging, or overdue receivables",
            "`receivable` domain",
            "selected metric detail's `answer_contract`",
            "formal-receivable-turnover-calculation-attestation/v1",
            "valid `attestation_seal`, and a valid enclosing `claim_seal`",
            "both the formula disclosure and the two-sided external-customer-scope disclosure",
            "applicable and validly sealed",
            "never invent a denominator amount",
            "missing, invalid, or `status: undefined`",
            "do not make a formal turnover numeric or component-formula assertion",
            "`undefined` or `partial` is a governed result state, not a tool error",
            "independently sealed non-formula facts and disclosures",
            "Do not re-query merely to repair or restate this finalization contract",
        ):
            self.assertIn(required, normalized_hook)
        self.assertIsNone(hook(platform="cli", is_first_turn=True))

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
                }
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
                    "detail_receipt": self._metric_detail_receipt(
                        "delivery", metric_code
                    ),
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
        rules = delivery["answer_rules"]
        joined_rules = "\n".join(rules)
        self.assertNotIn("不设置固定最大查询跨度", joined_rules)
        self.assertNotIn("跨度较长”本身说成口径错误或拒绝理由", joined_rules)
        governance_rule = next(
            rule
            for rule in rules
            if "query-policy.governed_metric_time_range.max_days" in rule
        )
        self.assertIn("query-policy 的 wider_analysis 规则拆分", governance_rule)
        self.assertIn("单一权威治理", governance_rule)
        time_policy = policy["governed_metric_time_range"]
        self.assertEqual(
            "split_into_independently_bounded_periods",
            time_policy["wider_analysis"],
        )
        self.assertNotIn(str(time_policy["max_days"]), governance_rule)

    def test_snapshot_month_evidence_and_typed_states_are_model_safe(self) -> None:
        def run_query(
            request: dict[str, object],
            rows: list[dict[str, object]],
        ) -> tuple[dict[str, object], dict[str, object], str]:
            request = {
                **request,
                "detail_receipt": self._metric_detail_receipt(
                    str(request["domain"]),
                    str(request["metric"]),
                ),
            }
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
        self.assertEqual("查询范围：2026-07 月末业务快照", latest_payload["answer_scope_line"])
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
            detail_receipt: str | None = None,
        ) -> tuple[dict[str, object], dict[str, object], list[str]]:
            sql_calls: list[str] = []

            def execute_query(sql, _params, _limit, **_kwargs):
                sql_calls.append(sql)
                return rows, False, self._read_only_source_evidence()

            request = {
                **base_request,
                "request_id": request_id,
                "metric": metric,
                "detail_receipt": detail_receipt
                or self._metric_detail_receipt("receivable", metric),
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
        self.assertEqual("datasage-mini-receivable-semantics/v6", semantics["version"])
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

        current_catalog = json.loads(
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
        current_catalog.pop("content_hash")
        stale_catalog = json.loads(
            json.dumps(current_catalog, ensure_ascii=False).replace(
                "截至数据库查询日，当前正数未结清应收中超过适用授信天数的部分按治理汇率折算后的人民币金额。",
                "当前正数未结清应收中，超过适用授信天数的部分按治理汇率折算后的人民币金额。",
            )
        )
        self.assertNotEqual(current_catalog, stale_catalog)
        stale_receipt = hashlib.sha256(
            json.dumps(
                stale_catalog,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        stale_request = {
            **base_request,
            "request_id": "overdue_stale_receipt",
            "detail_receipt": stale_receipt,
        }
        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("stale receipt must fail before SQL"),
        ) as execute:
            stale_payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [stale_request]}
                )
            )
        execute.assert_not_called()
        self.assertEqual("failed", stale_payload["status"])
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID", stale_payload["error"]["code"]
        )
        self.assertNotIn("42.00", json.dumps(stale_payload))

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
        current_receipt = str(detail["content_hash"])

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
                "detail_receipt": self._metric_detail_receipt(
                    "inventory", requested_metric
                ),
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

        stale_detail = json.loads(json.dumps(detail, ensure_ascii=False))
        stale_detail.pop("content_hash")
        stale_detail["results"][0]["metric"].pop("answer_contract")
        stale_receipt = hashlib.sha256(
            json.dumps(
                stale_detail,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(current_receipt, stale_receipt)
        stale_request = {
            "request_id": "inventory_old_observation_contract_receipt",
            "domain": "inventory",
            "mode": "metric",
            "purpose": "verify prior inventory detail receipt is invalid",
            "metric": metric,
            "dimensions": [],
            "inventory_scope": "total",
            "detail_receipt": stale_receipt,
        }
        with mock.patch.object(
            tools,
            "_execute_with_source",
            side_effect=AssertionError("stale receipt must fail before SQL"),
        ) as execute:
            stale_payload = json.loads(
                tools.runtime_guarded_datasage_query(
                    {"requests": [stale_request]}
                )
            )
        execute.assert_not_called()
        self.assertEqual("failed", stale_payload["status"])
        self.assertEqual(
            "METRIC_DETAIL_RECEIPT_INVALID",
            stale_payload["error"]["code"],
        )

    def test_target_metric_ambiguity_requires_official_clarification(self) -> None:
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
        self.assertTrue(completion_metrics.issubset(metric_codes))

        boundary = expert_index["metric_selection_boundary"]
        branches = boundary["branches"]
        self.assertEqual(
            "candidate_index_only_no_match_classification",
            boundary["producer_scope"],
        )
        self.assertEqual(
            "call_official_clarify",
            branches["multiple_materially_distinct"]["next_step"],
        )
        self.assertEqual(
            {"metric_detail_calls": 0, "datasage_query_calls": 0},
            branches["multiple_materially_distinct"][
                "before_clarification_response"
            ],
        )
        self.assertEqual(
            "report_domain_local_gap_or_call_official_clarify",
            branches["zero_compatible"]["next_step"],
        )
        zero_branch = branches["zero_compatible"]
        self.assertEqual("current_returned_domain_only", zero_branch["scope"])
        self.assertEqual(
            {"metric_detail_calls": 0, "datasage_query_calls": 0},
            zero_branch["before_response"],
        )
        self.assertEqual(
            "user_semantics_explicitly_support_one_minimal_related_domain",
            zero_branch["cross_domain_check"]["allowed_only_when"],
        )
        self.assertEqual(
            "load_only_that_related_domain_expert_index",
            zero_branch["cross_domain_check"]["action"],
        )
        self.assertEqual(
            ["enumerate_all_domains", "claim_globally_unsupported"],
            zero_branch["forbidden"],
        )
        self.assertNotIn("next_step", expert_index)
        serialized_index = json.dumps(expert_index, ensure_ascii=False)
        self.assertNotIn('"exact_match"', serialized_index)
        self.assertNotIn('"ambiguity"', serialized_index)

        activation = (
            "user_explicitly_selected_both_or_original_question_explicitly_"
            "requests_both"
        )
        for metric in sorted(completion_metrics):
            detail = json.loads(
                contracts.datasage_catalog(
                    {"requests": [{"domain": "target", "metric": metric}]}
                )
            )
            self.assertEqual("success", detail["status"], metric)
            projected = detail["results"][0]
            self.assertEqual(metric, projected["metric"]["code"])
            self.assertNotIn(
                "return_delivery_and_receipt_together",
                json.dumps(detail, ensure_ascii=False),
            )
            guidance = projected["planning_guidance"]
            clarify_rules = [
                rule
                for rule in guidance["planning_rules"]
                if "Hermes 官方 clarify" in rule
            ]
            self.assertEqual(1, len(clarify_rules), metric)
            self.assertIn(
                "澄清答复前 metric detail 和 datasage_query 均为0",
                clarify_rules[0],
            )
            self.assertIn("用户明确选择两者", clarify_rules[0])
            exact_overview_policy = guidance["recipe_policy"][
                "exact_overview_bundle"
            ]
            self.assertIn("仅在用户明确选择出库与收款两者", exact_overview_policy)
            self.assertIn("原问题明确要求", exact_overview_policy)
            recipes = guidance["recipes"]
            overview = recipes["completion_overview"]
            self.assertEqual("exact_overview_bundle", overview["kind"])
            self.assertEqual(activation, overview["activation"])
            self.assertEqual(
                completion_metrics,
                {request["metric"] for request in overview["requests"]},
            )
            dual_query_recipes = []
            for recipe_name, recipe in recipes.items():
                requests = (
                    recipe.get("requests")
                    if isinstance(recipe, dict)
                    else None
                )
                if not isinstance(requests, list):
                    continue
                request_metrics = {
                    request.get("metric")
                    for request in requests
                    if isinstance(request, dict)
                }
                if completion_metrics.issubset(request_metrics):
                    dual_query_recipes.append(recipe_name)
                    self.assertEqual(activation, recipe.get("activation"))
            self.assertEqual(["completion_overview"], dual_query_recipes)

        planner_path = (
            PROFILE_ROOT / "skills/target-query/references/planner-contract.yaml"
        )
        semantics_path = (
            PROFILE_ROOT
            / "plugins/datasage-query/contracts/target-semantics.yaml"
        )
        planner = yaml.safe_load(planner_path.read_text(encoding="utf-8"))
        semantics = yaml.safe_load(semantics_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "clarify_before_metric_detail_and_query",
            planner["defaults"]["ambiguous_target_type"],
        )
        self.assertEqual(
            {"metric_detail_calls": 0, "datasage_query_calls": 0},
            planner["defaults"]["ambiguous_target_pre_response"],
        )
        self.assertEqual(
            activation,
            planner["recipes"]["completion_overview"]["activation"],
        )
        self.assertEqual("plugin_physical_execution", semantics["contract_role"])
        for session_policy_key in (
            "ambiguous_target_type",
            "ambiguous_target_pre_response",
            "completion_overview_activation",
        ):
            self.assertNotIn(session_policy_key, semantics["defaults"])
        semantics_recipe_status = semantics["analysis_recipes_status"]
        self.assertEqual(
            "reference_only_not_runtime_or_model_authority",
            semantics_recipe_status["status"],
        )
        self.assertEqual(
            "planner_contract",
            semantics_recipe_status["authoritative_source"],
        )
        self.assertIs(semantics_recipe_status["runtime_authority"], False)
        self.assertIs(semantics_recipe_status["user_intent_authority"], False)
        self.assertNotIn(
            "activation",
            semantics["analysis_recipes"]["completion_overview"],
        )
        for content in (
            planner_path.read_text(encoding="utf-8"),
            semantics_path.read_text(encoding="utf-8"),
        ):
            self.assertNotIn("return_delivery_and_receipt_together", content)

        main_skill = (PROFILE_ROOT / "skills/datasage/SKILL.md").read_text(
            encoding="utf-8"
        )
        patterns = (
            PROFILE_ROOT / "skills/datasage/datasage-query-patterns/SKILL.md"
        ).read_text(encoding="utf-8")
        normalized_main = " ".join(main_skill.split())
        normalized_patterns = " ".join(patterns.split())
        for normalized in (normalized_main, normalized_patterns):
            self.assertIn("`metric_selection_boundary`", normalized)
            self.assertIn("official Hermes `clarify`", normalized)
            self.assertIn("`datasage_query`", normalized)
        self.assertIn(
            "metric-detail calls and `datasage_query` calls must both be zero",
            normalized_main,
        )
        self.assertIn(
            "zero metric-detail and `datasage_query` calls before the clarification response",
            normalized_patterns,
        )
        for normalized in (normalized_main, normalized_patterns):
            self.assertIn("domain-local", normalized)
            self.assertIn("single minimal related domain", normalized)
            self.assertIn("Never enumerate every domain", normalized)

    def test_delivery_planner_has_no_dev1_acceptance_orphan(self) -> None:
        for relative_path in (
            "skills/delivery-query/references/planner-contract.yaml",
            "plugins/datasage-query/contracts/delivery-semantics.yaml",
        ):
            content = (PROFILE_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("phase_a_acceptance", content)
            self.assertNotIn("dev1_single_metric_vertical_slice", content)

    def test_all_model_catalog_details_avoid_legacy_driver_vocabulary(self) -> None:
        checked_metrics = 0
        structural_metrics = 0
        for domain in (
            "delivery",
            "receipt",
            "receivable",
            "target",
            "customer_risk",
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
                if metric["supports_change_decomposition"]:
                    self.assertIn(
                        "structural_contributor_count_semantics", serialized
                    )
                    structural_metrics += 1
                checked_metrics += 1
        self.assertGreater(checked_metrics, 100)
        self.assertGreater(structural_metrics, 0)

    def test_model_wire_renames_legacy_reconciliation_fields_without_mutation(self) -> None:
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
        public = projected["change_reconciliation"]
        self.assertEqual(["claim-1"], public["structural_contributor_claim_ids"])
        self.assertEqual(3, public["full_partition_row_count"])
        self.assertEqual(1, public["returned_nonzero_contributor_count"])
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
                "allowed_reasoning_topics": ["comparison"],
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
            self.assertEqual([], failed_closed["allowed_reasoning_topics"], label)

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

    def test_shared_dimension_labels_are_business_specific(self) -> None:
        for domain in ("delivery", "customer_risk"):
            projection = contracts._domain_contract(domain, "planner")["planner"]
            labels = {
                dimension["code"]: dimension["label"]
                for dimension in projection["dimensions"]
            }
            self.assertEqual("客户部门", labels["department"])
            self.assertEqual("业务组织", labels["organization"])


if __name__ == "__main__":
    unittest.main()
