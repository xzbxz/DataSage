"""Bind finite analysis conditions and coverage to the existing result evidence."""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from . import capability_contract
from .query_errors import QueryFailure


_OP_LABELS = {"eq": "等于", "gt": "大于", "gte": "不小于", "lt": "小于", "lte": "不大于"}
_COUNT_KEYS = ("population", "match", "unknown", "excluded")
_TIME_KEYS = ("net_debt_snapshot_month", "open_items_observation_date")


def selected_rows(scope: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Do not bind parent-population quantities to a filtered-slice claim."""
    if not isinstance(scope.get("analysis"), Mapping) or scope["analysis"].get("operation") != "inventory_flow":
        return rows
    selected = set(scope.get("analysis_selected_fields") or ()) | {
        "return_rolls", "return_quantity", "analysis_known_gross_rolls",
        "analysis_known_gross_quantity",
    }
    references = set(scope.get("analysis_reference_fields") or ())
    return [
        {key: value for key, value in row.items()
         if key not in references and not key.startswith("reference_")
         and (not key.endswith(("_rolls", "_quantity")) or key in selected)}
        for row in rows
    ]


def with_disclosures(request: Mapping[str, Any], semantics: Mapping[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
    """Use the existing disclosure ledger, rather than an unbound answer note."""
    if "analysis" not in request:
        return semantics
    if not isinstance(scope.get("analysis"), Mapping):
        raise QueryFailure("ANALYSIS_SCOPE_UNAVAILABLE", "本次分析没有已编译的范围证据。")
    metric_code = request.get("metric")
    metric = semantics.get("metrics", {}).get(metric_code)
    if not isinstance(metric, Mapping):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "分析指标合同不可用。")
    registry = capability_contract.metric_analysis_fields(metric)
    descriptions = []
    for stage, title in (("row_filters", "行级条件"), ("group_filters", "分组后条件")):
        allowed = registry.get(stage) or registry.get("row" if stage == "row_filters" else "group") or {}
        terms = []
        for condition in request["analysis"].get(stage, []):
            definition = allowed.get(condition["field"])
            if not isinstance(definition, Mapping):
                raise QueryFailure("ANALYSIS_SCOPE_UNAVAILABLE", "分析字段缺少已登记的表达信息。")
            terms.append(f"{definition['label']}{_OP_LABELS[condition['op']]}{condition['value']}（{definition['unit']}）")
        if terms:
            descriptions.append(title + "：" + "且".join(terms))
    text = "本次为受控分析切片，" + "；".join(descriptions) + "。条件按且组合；组级筛选先于最终排序和截断。未知匹配保持未知，不能当零、当不匹配或当完整名单。"
    operation = scope["analysis"].get("operation")
    if operation == "inventory_flow":
        text += "价格条件使用同一历史出库行的成交价与DDP；毛量不等于净量，无法归入价格档的退货不从该档毛量任意扣减。原75%高折KPI默认定义不变；本分析不以当前库存促销价代替历史成交价。"
    if operation == "current_debt":
        text += "金额来自客户净欠款账，不是未结清应收合计。月度余额与当前未结项未证明同一业务时点时，联合匹配保持未知；当前逾期信息只能作为另一个时点的观察。"
    if operation == "open_receivable":
        text += "行级逾期条件先筛选未结项，再汇总客户金额；存在性条件在同一入选项目范围中判断。该金额仍是未结清应收项目合计，不是客户净欠款快照余额。"
    updated = dict(semantics)
    updated["metrics"] = dict(semantics["metrics"])
    changed_metric = dict(metric)
    changed_metric["disclosures"] = [
        copy.deepcopy(item) for item in metric.get("disclosures", [])
        if item.get("id") != "analysis.scope"
    ] + [{"id": "analysis.scope", "mode": "required_always", "text": text}]
    updated["metrics"][metric_code] = changed_metric
    return updated


def _count(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析覆盖计数无效。")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析覆盖计数无效。") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析覆盖计数不是非负整数。")
    return int(number)


def public_context(request: Mapping[str, Any], scope: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                   *, period: Mapping[str, Any], scope_fingerprint: str, projection_fingerprint: str,
                   source_filters: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    if "analysis" not in request:
        return None
    plan = scope.get("analysis")
    if not isinstance(plan, Mapping):
        raise QueryFailure("ANALYSIS_SCOPE_UNAVAILABLE", "本次分析没有已编译的范围证据。")
    context = {
        "version": "registered-analysis-evidence/v1",
        "protocol_version": plan.get("protocol_version"),
        "conditions": copy.deepcopy(request["analysis"]),
        "source_filters": copy.deepcopy(list(source_filters)),
        "field_contracts": [
            {key: copy.deepcopy(condition[key]) for key in
             ("field", "stage", "unit", "currency", "source_scope", "unknown_policy", "operation")
             if key in condition}
            for stage in ("row_filters", "group_filters")
            for condition in plan.get(stage, []) if isinstance(condition, Mapping)
        ],
        "combination": "and",
        "operation": plan.get("operation"),
        "scope_fingerprint": scope_fingerprint,
        "projection_fingerprint": projection_fingerprint,
        "period": copy.deepcopy(period),
        "unknown_policy": "preserve_and_report",
        "source_scopes": sorted({
            condition["source_scope"]
            for stage in ("row_filters", "group_filters")
            for condition in plan.get(stage, [])
            if isinstance(condition, Mapping) and isinstance(condition.get("source_scope"), str)
        }),
        "counts_available": False,
    }
    count_fields = scope.get("analysis_counts")
    if rows and isinstance(count_fields, Mapping):
        counts = {}
        for key in _COUNT_KEYS:
            field = count_fields.get(key)
            if not isinstance(field, str) or not field.startswith("analysis_"):
                raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析覆盖计数绑定无效。")
            values = {_count(row.get(field)) for row in rows}
            if len(values) != 1:
                raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "重复展示的分析总体计数不一致。")
            counts[key] = values.pop()
        if counts["population"] != sum(counts[key] for key in ("match", "unknown", "excluded")):
            raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析匹配、未知与排除计数未与总体对账。")
        context.update(counts_available=True, counts=counts,
                       count_grain=scope.get("analysis_count_grain", "registered_analysis_objects"))
    time_fields = scope.get("analysis_source_times")
    if isinstance(time_fields, Mapping):
        dates = {}
        for key in _TIME_KEYS:
            field = time_fields.get(key)
            if not isinstance(field, str):
                continue
            values = {str(row[field]) for row in rows if row.get(field) is not None}
            if len(values) > 1:
                raise QueryFailure("ANALYSIS_EVIDENCE_INVALID", "分析来源时点证据不一致。")
            dates[key] = next(iter(values)) if values else None
        context["source_observations"] = dates
    for name in ("alignment_state", "temporal_state", "parent_population"):
        if plan.get(name) is not None:
            context[name] = plan[name]
    context["evidence_seal"] = _context_seal(context)
    return context


def _context_seal(context: Mapping[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in context.items() if k != "evidence_seal"},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def context_is_valid(context: Any, result: Mapping[str, Any]) -> bool:
    """Bind auxiliary analysis evidence to the same visible result and facts."""
    if not isinstance(context, Mapping) or context.get("version") != "registered-analysis-evidence/v1":
        return False
    if context.get("evidence_seal") != _context_seal(context):
        return False
    if any(context.get(key) != result.get(key) for key in ("scope_fingerprint", "projection_fingerprint")):
        return False
    if context.get("period") != result.get("applied_time_range"):
        return False
    if context.get("counts_available") is True:
        counts = context.get("counts")
        if not isinstance(counts, Mapping) or set(counts) != set(_COUNT_KEYS):
            return False
        try:
            normalized = {key: _count(counts[key]) for key in _COUNT_KEYS}
            if normalized["population"] != sum(normalized[key] for key in ("match", "unknown", "excluded")):
                return False
            for claim in result.get("claim_ledger", []):
                facts = claim.get("facts", {})
                if any(_count(facts.get("analysis_" + key + "_count")) != normalized[key] for key in _COUNT_KEYS):
                    return False
        except (QueryFailure, TypeError, AttributeError):
            return False
    return True
