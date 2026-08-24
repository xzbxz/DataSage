"""Final model-wire constraints shared by every public DataSage tool."""

from __future__ import annotations

from functools import wraps
import json
from typing import Any, Callable, Mapping

from . import settings


DEFAULT_TOOL_RESULT_CHAR_LIMIT = 90_000
HARD_TOOL_RESULT_CHAR_LIMIT = 95_000


def tool_result_char_limit() -> int:
    """Stay below Hermes' approximately 100k-character executor ceiling."""

    return settings.get_int(
        "max_tool_result_chars",
        DEFAULT_TOOL_RESULT_CHAR_LIMIT,
        4_096,
        HARD_TOOL_RESULT_CHAR_LIMIT,
    )


def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_CATALOG_METRIC_FIELDS = (
    "code",
    "label",
    "business_definition",
    "time_policy",
    "unit",
    "unit_policy",
    "currency_policy",
    "required_attribution_mode",
    "allowed_attribution_modes",
    "available_inventory_scopes",
    "default_inventory_scope",
    "max_group_dimensions",
    "exact_default_lookup_supported",
    "comparison_kinds",
    "supports_dimensions",
    "supports_change_decomposition",
    "operation_summary",
    "requires_metric_detail",
)


def _compact_dimension(
    dimension: Mapping[str, Any],
    value_policies: dict[str, str],
) -> dict[str, Any]:
    compact = {
        key: dimension[key]
        for key in ("code", "label", "answer_note")
        if key in dimension
    }
    contract = dimension.get("value_contract")
    if isinstance(contract, Mapping):
        kind = contract.get("kind")
        if isinstance(kind, str) and kind:
            compact["value_kind"] = kind
            rule = contract.get("model_rule")
            if isinstance(rule, str) and rule:
                value_policies.setdefault(kind, rule)
        for key in ("allowed_values", "business_meanings"):
            if key in contract:
                compact[key] = contract[key]
    return compact


def _metric_operation_summary(metric: Mapping[str, Any]) -> list[str]:
    operations = ["direct_fact"]
    if metric.get("comparison_kinds"):
        operations.append("returned_comparison")
    if metric.get("allowed_dimensions"):
        operations.append("dimension_breakdown")
    if metric.get("change_decomposition_dimensions"):
        operations.append("complete_change_decomposition")
    if metric.get("target_gap_decomposition"):
        operations.append("complete_target_gap_decomposition")
    return operations


def _compact_metric_detail(
    detail: Mapping[str, Any],
    *,
    detail_receipt: Any = None,
    value_policies: dict[str, str],
) -> dict[str, Any]:
    metric = detail.get("metric")
    metric = metric if isinstance(metric, Mapping) else {}
    compact_metric = {
        key: metric[key]
        for key in _CATALOG_METRIC_FIELDS
        if key in metric
    }
    compact_metric["operation_summary"] = _metric_operation_summary(metric)
    for key in (
        "allowed_dimensions",
        "change_decomposition_dimensions",
        "reasoning_topics",
        "target_gap_decomposition",
        "dimensions_by_attribution_mode",
    ):
        if key in metric:
            compact_metric[key] = metric[key]
    limitations = metric.get("answer_contract")
    compact = {
        "domain": detail.get("domain"),
        "level": "metric",
        "metric": compact_metric,
        "dimensions": [
            _compact_dimension(item, value_policies)
            for item in detail.get("dimensions", [])
            if isinstance(item, Mapping)
        ],
    }
    if limitations:
        compact["limitations"] = limitations
    if isinstance(detail_receipt, str) and detail_receipt:
        compact["detail_receipt"] = detail_receipt
    return compact


def _compact_catalog_result(
    result: Mapping[str, Any],
    value_policies: dict[str, str],
) -> dict[str, Any]:
    if result.get("projection_mode") == "audit_full":
        return dict(result)
    level = result.get("level")
    if level == "metric":
        return _compact_metric_detail(
            result,
            detail_receipt=result.get("detail_receipt"),
            value_policies=value_policies,
        )
    if level == "performance_scorecard":
        compact_details: list[dict[str, Any]] = []
        for item in result.get("metric_details", []):
            if not isinstance(item, Mapping) or not isinstance(
                item.get("detail"), Mapping
            ):
                continue
            compact_detail = _compact_metric_detail(
                item["detail"],
                detail_receipt=item.get("detail_receipt"),
                value_policies=value_policies,
            )
            for key in ("lens", "time_binding"):
                if key in item:
                    compact_detail[key] = item[key]
            compact_details.append(compact_detail)
        recommended_bundle = [
            {
                key: item[key]
                for key in ("lens", "time_binding", "request_template")
                if key in item
            }
            for item in result.get("recommended_bundle", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("request_template"), Mapping)
        ]
        return {
            "level": "performance_scorecard",
            "recipe": result.get("recipe"),
            "recommended_bundle": recommended_bundle,
            "metric_count": len(compact_details),
            "metric_details": compact_details,
        }
    metrics = []
    for metric in result.get("metrics", []):
        if not isinstance(metric, Mapping):
            continue
        compact_metric = {
            key: metric[key] for key in _CATALOG_METRIC_FIELDS if key in metric
        }
        if "operation_summary" not in compact_metric:
            compact_metric["operation_summary"] = _metric_operation_summary(metric)
        if metric.get("limitations"):
            compact_metric["limitations"] = metric["limitations"]
        metrics.append(compact_metric)
    return {
        key: value
        for key, value in {
            "domain": result.get("domain"),
            "level": level,
            "metric_count": result.get("metric_count", len(metrics)),
            "metrics": metrics,
            "metric_selection_boundary": result.get("metric_selection_boundary"),
        }.items()
        if value is not None
    }


def compact_catalog_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project full internal catalog contracts to a compact model surface."""

    if payload.get("model_wire_version") == "datasage-catalog-model-wire/v2":
        return payload
    results = payload.get("results")
    if not isinstance(results, list):
        return payload
    value_policies: dict[str, str] = {}
    compact_results = [
        _compact_catalog_result(result, value_policies)
        for result in results
        if isinstance(result, Mapping)
    ]
    compact = {
        key: value
        for key, value in payload.items()
        if key not in {"results"}
    }
    compact["model_wire_version"] = "datasage-catalog-model-wire/v2"
    compact["results"] = compact_results
    if value_policies:
        compact["dimension_value_policies"] = value_policies
    return compact


def _compact_query_row(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: claim[key]
        for key in (
            "claim_id",
            "dimensions",
            "facts",
            "states",
            "allowed_relations",
            "unit",
            "currency",
        )
        if key in claim
    }


def _compact_evidence_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, Mapping):
        return {}
    items: list[dict[str, Any]] = []
    for item in bundle.get("items", []):
        if not isinstance(item, Mapping):
            continue
        items.append(
            {
                key: item[key]
                for key in (
                    "request_id",
                    "status",
                    "data_state",
                    "completeness",
                    "reconciliation",
                    "supports",
                    "limitations",
                    "analysis_intent",
                    "evidence_role",
                )
                if key in item
            }
        )
    return {
        key: value
        for key, value in {
            "version": bundle.get("version"),
            "coverage": bundle.get("coverage"),
            "items": items,
            "evidence_gaps": bundle.get("evidence_gaps", []),
            "answer_guardrails": bundle.get("answer_guardrails", {}),
        }.items()
        if value not in (None, {}, [])
    }


def _query_answer_constraints(
    results: list[Mapping[str, Any]],
    evidence_bundle: Mapping[str, Any],
    metric_contexts: Any,
) -> dict[str, Any]:
    items = {
        item.get("request_id"): item
        for item in evidence_bundle.get("items", [])
        if isinstance(item, Mapping) and isinstance(item.get("request_id"), str)
    }
    truncated = [
        str(result.get("request_id"))
        for result in results
        if result.get("truncated") is True
    ]
    reconciliation_missing: list[str] = []
    benchmark_request_ids: list[str] = []
    for request_id, item in items.items():
        limitations = item.get("limitations")
        limitations = limitations if isinstance(limitations, list) else []
        if any("RECONCIL" in str(value) for value in limitations):
            reconciliation_missing.append(request_id)
        supports = item.get("supports")
        supports = supports if isinstance(supports, list) else []
        # A period/change comparison supports a neutral relative fact, but it
        # does not authorize words such as healthy, normal, controllable, or
        # on-target.  Only an explicitly governed benchmark/target does.
        if item.get("evidence_role") == "benchmark" or any(
            value in {"target_status", "benchmark"} for value in supports
        ):
            benchmark_request_ids.append(request_id)
    successful_request_ids = [
        str(result.get("request_id"))
        for result in results
        if result.get("status") == "success"
        and isinstance(result.get("request_id"), str)
    ]
    benchmark_missing_request_ids = [
        request_id
        for request_id in successful_request_ids
        if request_id not in benchmark_request_ids
    ]
    metric_count = len(metric_contexts) if isinstance(metric_contexts, list) else 0
    return {
        "truncated_population": {
            "request_ids": truncated,
            "rule": (
                "Returned Top-N rows authorize only the returned ranking, never a "
                "complete-population or concentration statement."
            ),
        },
        "benchmark_missing": {
            "value": bool(benchmark_missing_request_ids),
            "request_ids": benchmark_missing_request_ids,
            "benchmark_request_ids": benchmark_request_ids,
            "rule": "Normative judgments require a compatible governed benchmark.",
        },
        "reconciliation_missing": {
            "request_ids": reconciliation_missing,
            "rule": (
                "Do not claim structural contribution, offset, lift, or drag for "
                "these requests without a reconciled receipt."
            ),
        },
        "scope_compatibility": {
            "status": (
                "not_proven_across_metrics"
                if metric_count > 1
                else "single_metric_or_not_applicable"
            ),
            "rule": (
                "Cross-metric comparisons require compatible period, population, "
                "unit, currency, filters, and business scope."
            ),
        },
    }


def compact_query_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove repeated proof envelopes after their integrity has been checked."""

    if payload.get("model_wire_version") == "datasage-query-model-wire/v2":
        return payload
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or not any(
        isinstance(result, Mapping) and "claim_ledger" in result
        for result in raw_results
    ):
        return payload
    disclosures: dict[tuple[str, str], dict[str, Any]] = {}
    compact_results: list[dict[str, Any]] = []
    evidence_bundle = _compact_evidence_bundle(payload.get("evidence_bundle"))
    evidence_items = {
        item.get("request_id"): item
        for item in evidence_bundle.get("items", [])
        if isinstance(item, Mapping)
    }
    for result in raw_results:
        if not isinstance(result, Mapping):
            continue
        claims = result.get("claim_ledger")
        claims = claims if isinstance(claims, list) else []
        compact_result = {
            key: result[key]
            for key in (
                "request_id",
                "status",
                "data_state",
                "business_metric_ref",
                "business_metric_label",
                "business_dimension_labels",
                "allowed_reasoning_topics",
                "change_reconciliation",
                "target_gap_reconciliation",
                "row_count",
                "truncated",
                "requested_limit",
                "effective_limit",
                "has_more",
                "applied_time_range",
                "error",
            )
            if key in result
        }
        compact_rows = [
            _compact_query_row(claim)
            for claim in claims
            if isinstance(claim, Mapping)
        ]
        compact_result["rows"] = compact_rows
        scope_entities = {
            json.dumps(claim.get("scope_entities", []), ensure_ascii=False, sort_keys=True): claim.get("scope_entities", [])
            for claim in claims
            if isinstance(claim, Mapping)
        }
        if len(scope_entities) == 1:
            compact_result["scope_entities"] = next(iter(scope_entities.values()))
        elif len(scope_entities) > 1:
            for compact_row, claim in zip(compact_rows, claims):
                if isinstance(claim, Mapping) and claim.get("scope_entities"):
                    compact_row["scope_entities"] = claim["scope_entities"]
        item = evidence_items.get(result.get("request_id"))
        if isinstance(item, Mapping) and item.get("limitations"):
            compact_result["limitations"] = item["limitations"]
        disclosure_refs: list[str] = []
        for disclosure in result.get("disclosure_ledger", []):
            if not isinstance(disclosure, Mapping) or disclosure.get("applies") is not True:
                continue
            disclosure_id = disclosure.get("disclosure_id")
            text = disclosure.get("text")
            if not isinstance(disclosure_id, str) or not isinstance(text, str):
                continue
            disclosure_refs.append(disclosure_id)
            key = (disclosure_id, text)
            shared = disclosures.setdefault(
                key,
                {
                    "disclosure_id": disclosure_id,
                    "text": text,
                    "request_ids": [],
                },
            )
            request_id = result.get("request_id")
            if request_id not in shared["request_ids"]:
                shared["request_ids"].append(request_id)
        if disclosure_refs:
            compact_result["disclosure_refs"] = disclosure_refs
        compact_results.append(compact_result)
    compact = {
        key: value
        for key, value in payload.items()
        if key not in {"results", "evidence_bundle"}
    }
    compact["model_wire_version"] = "datasage-query-model-wire/v2"
    compact["evidence_bundle"] = evidence_bundle
    compact["answer_constraints"] = _query_answer_constraints(
        [result for result in raw_results if isinstance(result, Mapping)],
        evidence_bundle,
        payload.get("metric_contexts"),
    )
    compact["disclosures"] = list(disclosures.values())
    compact["results"] = compact_results
    return compact


def _partial_results_payload(
    payload: dict[str, Any], limit: int
) -> str | None:
    """Keep a useful prefix of successful batch results within the wire budget."""

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    partial = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "results",
            "status",
            "error",
            "evidence_bundle",
            "source_evidence_ref",
            "semantic_coverage_receipts",
            "disclosures",
            "answer_constraints",
            "metric_contexts",
            "calculations",
            "calculation_count",
        }
    }
    partial.update({"status": "partial", "results": []})

    def candidate(selected: list[Any]) -> dict[str, Any]:
        request_ids = {
            item.get("request_id")
            for item in selected
            if isinstance(item, Mapping) and isinstance(item.get("request_id"), str)
        }
        metric_refs = {
            item.get("business_metric_ref")
            for item in selected
            if isinstance(item, Mapping)
            and isinstance(item.get("business_metric_ref"), str)
        }
        current = dict(partial)
        current.update(
            {
                "results": list(selected),
                "truncated": True,
                "omitted_result_count": len(results) - len(selected),
                "error": {
                    "code": "OUTPUT_TRUNCATED",
                    "message": (
                        "The successful batch exceeded the model-safe wire budget. "
                        "A complete prefix is returned; narrow the omitted branches or retry them separately."
                    ),
                    "retryable": True,
                },
            }
        )
        contexts = payload.get("metric_contexts")
        if isinstance(contexts, list):
            current["metric_contexts"] = [
                item
                for item in contexts
                if isinstance(item, Mapping)
                and item.get("business_metric_ref") in metric_refs
            ]
        bundle = payload.get("evidence_bundle")
        if isinstance(bundle, Mapping):
            filtered_bundle = {
                key: bundle[key]
                for key in ("version", "coverage", "answer_guardrails")
                if key in bundle
            }
            for field in ("items", "evidence_gaps"):
                values = bundle.get(field)
                if isinstance(values, list):
                    filtered_bundle[field] = [
                        item
                        for item in values
                        if isinstance(item, Mapping)
                        and item.get("request_id") in request_ids
                    ]
            current["evidence_bundle"] = filtered_bundle
        constraints = payload.get("answer_constraints")
        if isinstance(constraints, Mapping):
            filtered_constraints = json.loads(json.dumps(constraints))
            for value in filtered_constraints.values():
                if isinstance(value, dict) and isinstance(value.get("request_ids"), list):
                    value["request_ids"] = [
                        request_id
                        for request_id in value["request_ids"]
                        if request_id in request_ids
                    ]
                if isinstance(value, dict) and isinstance(
                    value.get("benchmark_request_ids"), list
                ):
                    value["benchmark_request_ids"] = [
                        request_id
                        for request_id in value["benchmark_request_ids"]
                        if request_id in request_ids
                    ]
            benchmark = filtered_constraints.get("benchmark_missing")
            if isinstance(benchmark, dict):
                benchmark["value"] = bool(benchmark.get("request_ids"))
            compatibility = filtered_constraints.get("scope_compatibility")
            if isinstance(compatibility, dict):
                compatibility["status"] = (
                    "not_proven_across_metrics"
                    if len(metric_refs) > 1
                    else "single_metric_or_not_applicable"
                )
            current["answer_constraints"] = filtered_constraints
        disclosures = payload.get("disclosures")
        if isinstance(disclosures, list):
            filtered_disclosures = []
            for disclosure in disclosures:
                if not isinstance(disclosure, Mapping):
                    continue
                retained = [
                    request_id
                    for request_id in disclosure.get("request_ids", [])
                    if request_id in request_ids
                ]
                if retained:
                    filtered_disclosures.append(
                        {**dict(disclosure), "request_ids": retained}
                    )
            current["disclosures"] = filtered_disclosures
        if "source_evidence_ref" in payload:
            current["source_evidence_ref"] = payload["source_evidence_ref"]
        calculations = payload.get("calculations")
        if isinstance(calculations, list):
            retained_calculations = []
            for calculation in calculations:
                if not isinstance(calculation, Mapping):
                    continue
                operands = calculation.get("operands")
                operand_ids = {
                    operand.get("request_id")
                    for operand in operands
                    if isinstance(operand, Mapping)
                } if isinstance(operands, list) else set()
                if operand_ids <= request_ids:
                    retained_calculations.append(calculation)
            if retained_calculations:
                current["calculation_count"] = len(retained_calculations)
                current["calculations"] = retained_calculations
        return current

    for item in results:
        partial["results"].append(item)
        if len(_compact_json(candidate(partial["results"]))) > limit:
            partial["results"].pop()
            break
    if not partial["results"]:
        return None
    rendered = _compact_json(candidate(partial["results"]))
    return rendered if len(rendered) <= limit else None


def enforce_tool_result_budget(tool_name: str, result: Any) -> str:
    """Return valid JSON within budget, replacing invalid/oversized output."""

    limit = tool_result_char_limit()
    if isinstance(result, str):
        rendered = result
    else:
        try:
            rendered = _compact_json(result)
        except (TypeError, ValueError):
            rendered = ""
    try:
        decoded = json.loads(rendered)
    except (TypeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict):
        if tool_name == "datasage_catalog":
            decoded = compact_catalog_payload(decoded)
        elif tool_name == "datasage_query":
            decoded = compact_query_payload(decoded)
        rendered = _compact_json(decoded)
    if isinstance(decoded, dict) and len(rendered) <= limit:
        return rendered

    if isinstance(decoded, dict):
        partial = _partial_results_payload(decoded, limit)
        if partial is not None:
            return partial

    reason = "OUTPUT_TOO_LARGE" if isinstance(decoded, dict) else "INVALID_TOOL_RESULT"
    replacement = _compact_json(
        {
            "status": "failed",
            "results": [],
            "error": {
                "code": reason,
                "message": (
                    "Tool result exceeds the model-safe context budget; "
                    "narrow the request or reduce detail."
                    if reason == "OUTPUT_TOO_LARGE"
                    else "The tool did not produce a valid JSON object."
                ),
                "retryable": reason == "OUTPUT_TOO_LARGE",
            },
            "tool": tool_name,
            "result_char_limit": limit,
        }
    )
    if len(replacement) > limit:  # Defensive for an operator-set tiny limit.
        replacement = '{"status":"failed","error":{"code":"OUTPUT_TOO_LARGE"}}'
    return replacement


def bounded_json_handler(tool_name: str, handler: Callable[..., Any]):
    """Wrap a public handler at its last boundary before Hermes dispatch."""

    @wraps(handler)
    def invoke(args: dict[str, Any], **kwargs: Any) -> str:
        return enforce_tool_result_budget(tool_name, handler(args, **kwargs))

    invoke.__datasage_result_budget__ = True
    return invoke
