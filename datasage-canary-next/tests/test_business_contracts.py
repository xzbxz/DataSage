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
            "平均净经营欠款",
            "期间自然日数",
            "月末欠款快照月数",
            "有效出库月份数",
            "排除内部客户的双侧范围",
            "密封有效",
            "未定义而不是错误",
        ):
            self.assertIn(proposition, answer_contract_text)
        self.assertNotIn(
            "平均月末净欠款除以同期毛出库金额再乘期间自然日数",
            answer_contract_text,
        )
        formula_contract = answer_contract[1]
        for condition in (
            "同一次 datasage_query",
            "formal-receivable-turnover-calculation-attestation/v1",
            "状态为 verified",
            "attestation_seal 与外层 claim_seal 均有效",
            "公式披露与双侧客户范围披露均适用且密封有效",
            "同一查询已密封的正式公式披露",
        ):
            self.assertIn(condition, formula_contract)
        self.assertIn(
            "attestation 缺失、无效或状态为 undefined 时",
            formula_contract,
        )
        self.assertIn(
            "不得依据本 catalog 合同直接陈述正式公式或正式周转数值",
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
                "gross_delivery_denominator_semantics",
                "period_natural_days",
                "snapshot_month_count",
                "effective_month_count",
            ],
            attestation["authorized_components"],
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
                "period_natural_days",
                "snapshot_month_count",
                "effective_month_count",
                "calculation_attestation",
            },
            set(dso_facts),
        )
        serialized_attestation = json.dumps(attestation, ensure_ascii=False)
        for private_value in ("42.00", "100.00", "900.00", "365"):
            self.assertNotIn(private_value, serialized_attestation)
        for implementation_token in ("SELECT ", "vk_dw", "vk_dwd", "`d`", "`s`"):
            self.assertNotIn(implementation_token, serialized_attestation)
        serialized_wire = json.dumps(dso_wire, ensure_ascii=False)
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
            self.assertNotIn(private_token, serialized_wire)

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
            self.assertIn(reason, case_attestation["undefined_reason_codes"], case_name)
            self.assertNotIn(
                "metric_value",
                result["claim_ledger"][0]["facts"],
                case_name,
            )
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
