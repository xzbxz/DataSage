from __future__ import annotations

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
        tools._seal_claim_ids(results)
        tools._seal_change_reconciliations(results)
        tools._finalize_complete_decomposition_outcomes(
            results,
            operation_partitions,
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
                                "dimensions": [],
                                "calendar_month": "2026-07",
                            }
                        ]
                    }
                )
            )

        self.assertEqual("success", query_payload["status"])
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
        disclosures = {
            item["disclosure_id"]: item
            for item in result["disclosure_ledger"]
        }
        self.assertEqual(
            {"receipt.domain.scope", "receipt.net.scope"},
            set(disclosures),
        )
        expected_disclosure_texts = {
            "receipt.domain.scope": (
                "收款及退款域指标均包含内部客户并排除A状态；"
                "涉及用途时以本次实际筛选范围为准。"
            ),
            "receipt.net.scope": (
                "净收款为收款人民币金额减退款人民币金额；"
                "收款和退款范围均包含内部客户并排除A状态。"
            ),
        }
        self.assertEqual(
            expected_disclosure_texts,
            {key: item["text"] for key, item in disclosures.items()},
        )
        for disclosure in disclosures.values():
            self.assertIs(disclosure["applies"], True)
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
                "contract_version": "metric-disclosure-ledger/v1",
                "request_id": result["request_id"],
                "metric_ref": result["business_metric_ref"],
                "scope_fingerprint": result["scope_fingerprint"],
                "projection_fingerprint": result["projection_fingerprint"],
                "ledger": result["disclosure_ledger"],
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
        self.assertIn("Present every sealed `disclosure_ledger` item", normalized_hook)
        self.assertIn("summarization must not drop", normalized_hook)

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
