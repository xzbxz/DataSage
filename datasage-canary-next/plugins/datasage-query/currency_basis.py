"""Contract-backed currency selection, without a second set of formulas.

Exact metric requests remain exact. The explicit ``auto`` selection used for
ordinary monetary questions resolves to an existing, named metric. All source
scope discovery is compiled by the ordinary governed query builder.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .query_errors import QueryFailure

BASIS_CHOICES = ("auto", "rmb", "original")


def capability(code: str, semantics: Mapping[str, Any]) -> dict[str, Any] | None:
    metrics = semantics.get("metrics", {})
    definition = metrics.get(code)
    if not isinstance(definition, Mapping):
        return None
    pairs = semantics.get("currency_basis_pairs", [])
    if not isinstance(pairs, list):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "币种对应指标合同无效。")
    used: set[str] = set()
    selected = None
    for pair in pairs:
        if (not isinstance(pair, Mapping) or set(pair) != {"rmb", "original"}
                or any(not isinstance(pair.get(k), str) or pair[k] not in metrics
                       for k in ("rmb", "original"))
                or pair["rmb"] == pair["original"]
                or used.intersection(pair.values())):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "币种对应指标引用无效或重复。")
        used.update(pair.values())
        if code in pair.values():
            selected = dict(pair)
    if selected is not None:
        return {"supported": list(BASIS_CHOICES), "counterparts": selected,
                "ordinary_question_default": "auto",
                "exact_metric_basis": "rmb" if code == selected["rmb"] else "original",
                "scope": "complete_filtered_request_including_components_and_comparison_periods"}
    policy = definition.get("currency_policy") or {}
    policy = policy if isinstance(policy, Mapping) else {}
    unit_text = str(definition.get("unit", "")) + str(definition.get("unit_policy", ""))
    if policy.get("mode") == "original_currency" or "原币" in unit_text:
        return {"supported": ["auto", "original"], "counterparts": {"original": code},
                "ordinary_question_default": "auto", "exact_metric_basis": "original",
                "limitation": "仅有已批准原币来源；跨币种只能分别展示，不能统一换算人民币。"}
    rmb_operand_kind = definition.get("query_kind") in {
        "target_completion", "inventory_turnover_days", "formal_dso",
        "paired_amounts", "fabric_delivery_source_summary", "fabric_inventory_source_summary",
    }
    rmb_operand_kind = rmb_operand_kind or any(
        isinstance(value, str) and value.endswith("_rmb")
        for key, value in definition.items()
        if key == "measure" or key.endswith("_measure")
    ) or any("人民币" in str(value) for value in (definition.get("result_fact_units") or {}).values())
    ratio = definition.get("ratio")
    if isinstance(ratio, Mapping):
        rmb_operand_kind = rmb_operand_kind or any(
            isinstance(ratio.get(k), str)
            and "人民币" in str(metrics.get(ratio[k], {}).get("unit", ""))
            for k in ("numerator", "denominator")
        )
    if "人民币" in unit_text or rmb_operand_kind:
        return {"supported": ["auto", "rmb"], "counterparts": {"rmb": code},
                "ordinary_question_default": "auto", "exact_metric_basis": "rmb",
                "limitation": "当前仅有已批准人民币来源或完整人民币计算口径；原币金额/币种映射不足，保留人民币。"}
    return None


def _original_scope(request: dict[str, Any]) -> dict[str, Any]:
    filters = request.get("metric_filters") or {}
    values = filters.get("currency")
    values = values if isinstance(values, list) else ([] if values is None else [values])
    dimensions = list(request.get("dimensions") or [])
    if len(values) != 1 and "currency" not in dimensions:
        request["dimensions"] = [*dimensions, "currency"]
    return request


def prepare_request(request: Mapping[str, Any], semantics: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(request)
    if "currency_basis" not in result:
        return result
    requested = result["currency_basis"]
    if not isinstance(requested, str) or requested not in BASIS_CHOICES:
        raise QueryFailure("INVALID_INPUT", "currency_basis 只接受 auto、rmb 或 original。")
    definition = semantics.get("metrics", {}).get(result.get("metric"), {})
    availability = definition.get("availability") if isinstance(definition, Mapping) else None
    if isinstance(availability, Mapping) and availability.get("status") in {"pending_validation", "blocked"}:
        # The existing availability gate owns its more specific failure.
        return result
    info = capability(str(result.get("metric")), semantics)
    if info is None:
        raise QueryFailure("CURRENCY_BASIS_UNAVAILABLE", "该指标不接受金额币种选择。")
    if requested not in info["supported"]:
        raise QueryFailure("CURRENCY_BASIS_UNAVAILABLE", info.get("limitation", "该币种口径没有批准来源。"))
    refs = info["counterparts"]
    probe = requested == "auto" and set(refs) == {"rmb", "original"}
    selected = ("rmb" if "rmb" in refs else "original") if requested == "auto" else requested
    plan = {"requested_basis": requested, "requested_metric": result["metric"],
            "resolved_basis": selected, "counterparts": dict(refs), "requires_probe": probe,
            "reason": "scope_pending" if probe else ("explicit_selection" if requested != "auto" else "only_governed_basis"),
            "limitation": info.get("limitation")}
    result["metric"] = refs[selected]
    order = result.get("order_by")
    if isinstance(order, Mapping) and order.get("field") == plan["requested_metric"]:
        result["order_by"] = {**order, "field": "metric_value"}
    result["_currency_basis_plan"] = plan
    if selected == "original" and not probe:
        _original_scope(result)
    return result


def build_probe(request, datasets, semantics, builder, *, observed_on=None):
    plan = request["_currency_basis_plan"]
    probe = dict(request)
    for key in ("currency_basis", "_currency_basis_plan", "order_by", "period_summary",
                "complete_change_decomposition", "decomposition_of_request_id",
                "complete_target_gap_decomposition", "_target_gap_of_request_id"):
        probe.pop(key, None)
    probe.update(metric=plan["counterparts"]["original"], dimensions=["currency"],
                 _currency_scope_probe=True)
    # The builder suppresses only its outer pagination for this private plan.
    # No SQL text is trimmed and no user-supplied SQL is accepted.
    sql, params, scope = builder(probe, datasets, semantics, 1, observed_on=observed_on)
    if "currency_no" not in scope.get("dimension_outputs", []):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "原币范围查询未提供受治理币种分组。")
    # Blank detection is not normalization: source_exact currency identifiers
    # must be passed back to the selected query without trimming or guessing.
    currency = "CASE WHEN NULLIF(TRIM(q.`currency_no`), '') IS NULL THEN NULL ELSE q.`currency_no` END"
    return (
        "SELECT COUNT(DISTINCT " + currency + ") AS currency_count, "
        "MIN(" + currency + ") AS single_currency, "
        "COALESCE(SUM(CASE WHEN " + currency + " IS NULL THEN 1 ELSE 0 END), 0) AS unknown_currency_groups "
        "FROM (" + sql + ") AS q WHERE COALESCE(q.`__matched_row_count`, 0) > 0",
        params,
    )


def validate_pair_bindings(request, semantics):
    """Do not carry an entity identity across different counterpart contracts."""
    from . import capability_contract
    bindings = request.get("_entity_bindings") or {}
    if not bindings:
        return
    refs = request["_currency_basis_plan"]["counterparts"]
    if set(refs) != {"rmb", "original"}:
        return
    try:
        left = capability_contract.effective_dimension_definitions(semantics, refs["rmb"])
        right = capability_contract.effective_dimension_definitions(semantics, refs["original"])
    except capability_contract.CapabilityContractError as exc:
        raise QueryFailure(exc.code, exc.message) from exc
    if any(left.get(code) != right.get(code) for code in bindings):
        raise QueryFailure("CURRENCY_COUNTERPART_SCOPE_MISMATCH", "币种对应指标的实体范围定义不同，不能复用绑定；请明确选择精确指标重新查询。")


def resolve_probe(request, rows, truncated):
    if truncated or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise QueryFailure("CURRENCY_SCOPE_UNVERIFIED", "币种范围证据不完整。")
    row = rows[0]
    def count(key):
        value = row.get(key)
        if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdecimal()):
            raise QueryFailure("CURRENCY_SCOPE_UNVERIFIED", "币种范围计数无效。")
        value = int(value)
        if value < 0:
            raise QueryFailure("CURRENCY_SCOPE_UNVERIFIED", "币种范围计数无效。")
        return value
    n = count("currency_count")
    unknown = count("unknown_currency_groups")
    if unknown:
        raise QueryFailure("CURRENCY_SCOPE_UNKNOWN", "本次范围含未知币种，不能确认单币种原币口径；可明确选择已批准人民币指标，或核实币种。")
    result = deepcopy(dict(request))
    plan = result["_currency_basis_plan"]
    selected = "original" if n <= 1 else "rmb"
    if n == 1:
        value = row.get("single_currency")
        if (isinstance(value, bool) or not isinstance(value, (str, int))
                or not str(value).strip() or len(str(value)) > 80):
            raise QueryFailure("CURRENCY_SCOPE_UNVERIFIED", "单币种标识无效。")
        # Restrict all components/periods to the proved currency, retaining
        # required grouping and every prior governed population filter.
        result["metric_filters"] = {**(result.get("metric_filters") or {}), "currency": value}
        plan["currency"] = value
    elif n == 0:
        _original_scope(result)
    result["metric"] = plan["counterparts"][selected]
    plan.update(resolved_basis=selected, requires_probe=False, currency_count=n,
                reason="empty_scope" if n == 0 else "single_currency_scope" if n == 1 else "cross_currency_scope")
    return result


def with_disclosure(request, semantics):
    plan = request.get("_currency_basis_plan")
    if not isinstance(plan, Mapping):
        return semantics
    note = {
        "single_currency_scope": "本次完整筛选范围及比较期间仅有一种币种，按该币种原币计算和展示。",
        "cross_currency_scope": "本次完整筛选范围或比较期间包含多种币种，使用已批准人民币口径统一计算和展示。",
        "empty_scope": "本次范围没有可识别的金额源记录，不推断币种或虚构金额。",
        "explicit_selection": "按明确选择的币种口径计算；币种筛选限制业务范围，不是自行换汇。",
        "only_governed_basis": plan.get("limitation") or "使用当前唯一已批准的币种口径。",
    }[plan["reason"]]
    result = dict(semantics)
    metrics = dict(result["metrics"])
    definition = dict(metrics[request["metric"]])
    definition["disclosures"] = [*(definition.get("disclosures") or []),
        {"id": "currency.basis.selection", "mode": "required_always", "text": note}]
    metrics[request["metric"]] = definition
    result["metrics"] = metrics
    return result
