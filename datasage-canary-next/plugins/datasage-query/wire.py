"""Final model-wire constraints shared by every public DataSage tool."""

from __future__ import annotations

from functools import wraps
from collections import Counter
import json
import time
from . import db_executor, settings
from typing import Any, Callable, Mapping

def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_CATALOG_METRIC_FIELDS = (
    "grouping",
    "allowed_time_buckets",
    "required_time_bucket",
    "code",
    "label",
    "ordering",
    "period_summary_fields",
    "change_decomposition_orderings",
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
    "allowed_dimensions",
    "dimensions_by_attribution_mode",
    "target_gap_decomposition",
    "supports_dimensions",
    "supports_change_decomposition",
    "operation_summary",
    "operation_summary_by_attribution_mode",
    "requires_metric_detail",
    "delivery_scope_policy",
    "scope_flags",
    "answer_boundary_summary",
    "status",
    "reason",
    "error",
    "selectable",
    "activation_gate",
    "pending",
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
        for key in ("allowed_values", "canonical_aliases", "business_meanings"):
            if key in contract:
                compact[key] = contract[key]
    return compact


def _metric_operation_summary(metric: Mapping[str, Any]) -> list[str]:
    operations = ["direct_fact"]
    if metric.get("comparison_kinds"):
        operations.append("returned_comparison")
    if metric.get("allowed_dimensions") or metric.get("supports_dimensions"):
        operations.append("dimension_breakdown")
    if metric.get("change_decomposition_dimensions"):
        operations.append("complete_change_decomposition")
    if metric.get("target_gap_decomposition") or metric.get(
        "supports_target_gap_decomposition"
    ):
        operations.append("complete_target_gap_decomposition")
    raw_modes = metric.get("allowed_attribution_modes")
    modes = {
        str(mode)
        for mode in raw_modes
        if isinstance(mode, str) and mode
    } if isinstance(raw_modes, list) else set()
    by_mode = metric.get("dimensions_by_attribution_mode")
    if isinstance(by_mode, Mapping):
        modes.update(str(mode) for mode in by_mode if str(mode))
    if len(modes) <= 1:
        return operations
    mode_operations = metric.get("operation_summary_by_attribution_mode")
    if not isinstance(mode_operations, Mapping):
        return operations
    values = [
        {str(item) for item in items if isinstance(item, str)}
        for items in mode_operations.values()
        if isinstance(items, list)
    ]
    if len(values) != len(modes):
        return operations
    common = set.intersection(*values)
    return [operation for operation in operations if operation in common]


def _compact_metric_detail(
    detail: Mapping[str, Any],
    *,
    value_policies: dict[str, str],
) -> dict[str, Any]:
    metric = detail.get("metric")
    metric = metric if isinstance(metric, Mapping) else {}
    compact_metric = {
        key: metric[key]
        for key in _CATALOG_METRIC_FIELDS
        if key in metric
    }
    is_pending = metric.get("selectable") is False or metric.get("pending") is True
    compact_metric["operation_summary"] = (
        [] if is_pending else _metric_operation_summary(metric)
    )
    for key in (
        "allowed_dimensions",
        "change_decomposition_dimensions",
        "target_gap_decomposition",
        "dimensions_by_attribution_mode",
        "operation_summary_by_attribution_mode",
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
    pending_capability = detail.get("pending_capability")
    if isinstance(pending_capability, Mapping):
        compact["pending_capability"] = {
            key: pending_capability[key]
            for key in _CATALOG_METRIC_FIELDS
            if key in pending_capability
        }
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
            value_policies=value_policies,
        )
    if level == "performance_scorecard":
        compact_lenses: list[dict[str, Any]] = []
        metric_count = 0
        for raw_lens in result.get("candidate_lenses", []):
            if not isinstance(raw_lens, Mapping):
                continue
            compact_candidates: list[dict[str, Any]] = []
            for raw_candidate in raw_lens.get("candidates", []):
                if not isinstance(raw_candidate, Mapping) or not isinstance(
                    raw_candidate.get("detail"), Mapping
                ):
                    continue
                candidate = _compact_metric_detail(
                    raw_candidate["detail"],
                    value_policies=value_policies,
                )
                # The metric already publishes allowed dimension codes. Keep
                # shared value policies once at the catalog top level instead
                # of replaying full dimension metadata for every candidate.
                candidate.pop("dimensions", None)
                compact_candidates.append(candidate)
            metric_count += len(compact_candidates)
            compact_lens = {
                key: value
                for key, value in raw_lens.items()
                if key != "candidates"
            }
            compact_lens["candidates"] = compact_candidates
            compact_lenses.append(compact_lens)
        return {
            "level": "performance_scorecard",
            "version": result.get("version"),
            "kind": result.get("kind"),
            "candidate_lenses": compact_lenses,
            "metric_count": metric_count,
            "evidence_boundaries": result.get("evidence_boundaries", []),
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
    dimension_sets = None
    metric_defaults = None
    if level == "expert_index":
        from .contracts import _compress_metric_dimension_sets
        # Reuse the catalog's existing lossless set encoding, not another cache.
        metrics, dimension_sets = _compress_metric_dimension_sets([
            {**m, "allowed_dimensions": m.get("allowed_dimensions", [])} for m in metrics
        ])
        metric_defaults = {}
        shared_keys = set.intersection(*(set(m) for m in metrics)) if metrics else set()
        for key in sorted(shared_keys - {"code", "label", "business_definition"}):
            encoded = [json.dumps(m[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")) for m in metrics]
            value, frequency = Counter(encoded).most_common(1)[0]
            if frequency < 2:
                continue
            metric_defaults[key] = json.loads(value)
            for m, candidate in zip(metrics, encoded):
                if candidate == value:
                    m.pop(key)
    return {
        key: value
        for key, value in {
            "allowed_dimension_sets": dimension_sets,
            "metric_defaults": metric_defaults,
            "domain": result.get("domain"),
            "level": level,
            "metric_count": result.get("metric_count", len(metrics)),
            "metrics": metrics,
            "pending_capabilities": result.get("pending_capabilities"),
        }.items()
        if value is not None
    }


def compact_catalog_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project full internal catalog contracts to a compact model surface."""

    if payload.get("model_wire_version") in {"datasage-catalog-model-wire/v3", "datasage-catalog-model-wire/v4"}:
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
    compact["model_wire_version"] = "datasage-catalog-model-wire/v4"
    compact["results"] = compact_results
    if value_policies:
        compact["dimension_value_policies"] = value_policies
    return compact


def _compact_query_row(claim: Mapping[str, Any], *, has_ranking: bool = False) -> dict[str, Any]:
    compact = {
        key: claim[key]
        for key in (
            "claim_id",
            "dimensions",
            "facts",
            "states",
            "allowed_relations",
            "unit",
            "fact_units",
            "currency",
        )
        if key in claim
    }
    if has_ranking and isinstance(compact.get("facts"), Mapping):
        compact["facts"] = {k: v for k, v in compact["facts"].items()
                            if k not in {"rank_population_count", "rank_unknown_value_count"}}
    return compact


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
        }.items()
        if value not in (None, {}, [])
    }


def compact_query_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove repeated proof envelopes after their integrity has been checked."""

    if payload.get("model_wire_version") == "datasage-query-model-wire/v3":
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
                "change_reconciliation",
                "target_gap_reconciliation",
                "ranking_evidence",
                "numeric_evidence",
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
            _compact_query_row(claim, has_ranking=bool(result.get("ranking_evidence")))
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
    compact["model_wire_version"] = "datasage-query-model-wire/v3"
    compact["evidence_bundle"] = evidence_bundle
    compact["disclosures"] = list(disclosures.values())
    compact["results"] = compact_results
    return compact


def enforce_tool_result_budget(tool_name: str, result: Any) -> str:
    """Return compact valid JSON and leave host-size handling to Hermes."""

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
        return _compact_json(decoded)

    reason = "INVALID_TOOL_RESULT"
    replacement = _compact_json(
        {
            "status": "failed",
            "results": [],
            "error": {
                "code": reason,
                "message": "The tool did not produce a valid JSON object.",
                "retryable": False,
            },
            "tool": tool_name,
        }
    )
    return replacement


def bounded_json_handler(tool_name: str, handler: Callable[..., Any]):
    """Wrap a public handler at its last boundary before Hermes dispatch."""

    @wraps(handler)
    def invoke(args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name not in {"datasage_query", "datasage_entity_resolve"}:
            return enforce_tool_result_budget(tool_name, handler(args, **kwargs))
        deadline_at = time.monotonic() + settings.get_int("call_timeout_seconds", 60, 1, 300)
        with db_executor.deadline_scope(deadline_at) as deadline_at:
            raw = handler(args, **kwargs)
            if time.monotonic() >= deadline_at:
                # The query producer keeps only pre-expiry verified branches.
                try:
                    decoded = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    decoded = None
                error = decoded.get("error") if isinstance(decoded, dict) else None
                if isinstance(error, Mapping) and error.get("code") == "BATCH_DEADLINE_EXCEEDED":
                    return enforce_tool_result_budget(tool_name, decoded)
                if tool_name != "datasage_query":
                    return _compact_json({"status": "failed", "results": [],
                        "must_stop_business_query": True,
                        "error": {"code": "BATCH_DEADLINE_EXCEEDED", "message": "调用总时限已到。", "retryable": True}})
                # The registered query producer already projected and checked
                # its completed evidence before returning. Crossing the deadline
                # while releasing its lease does not invalidate those facts.
                # Use the same public compaction and timeout exit as below;
                # never return the intermediate producer payload directly.
            rendered = enforce_tool_result_budget(tool_name, raw)
            if time.monotonic() >= deadline_at:
                # Preserve completed facts in the same public projection as
                # normal replies; expiry does not authorize an internal payload.
                decoded = json.loads(rendered)
                decoded["status"] = "partial" if decoded.get("results") else "failed"
                decoded["error"] = {"code": "BATCH_DEADLINE_EXCEEDED", "message": "输出整理超过总时限；保留期限内完成的结果。", "retryable": True}
                return _compact_json(decoded)
            return rendered

    return invoke
