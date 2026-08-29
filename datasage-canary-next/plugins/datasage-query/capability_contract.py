"""Pure cross-domain capability facts shared by Schema and runtime validation.

This module is deliberately small.  Metric semantics and physical adapters stay
in the versioned semantics files; Hermes remains responsible for planning and
business judgement.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from . import request_contract


DOMAIN_SOURCES: dict[str, dict[str, str]] = {
    "delivery": {
        "semantics": "plugins/datasage-query/contracts/delivery-semantics.yaml",
    },
    "receipt": {
        "semantics": "plugins/datasage-query/contracts/receipt-semantics.yaml",
    },
    "receivable": {
        "semantics": "plugins/datasage-query/contracts/receivable-semantics.yaml",
    },
    "target": {
        "semantics": "plugins/datasage-query/contracts/target-semantics.yaml",
    },
    "customer_risk": {
        "semantics": "plugins/datasage-query/contracts/customer_risk-semantics.yaml",
    },
    "inventory": {
        "semantics": "plugins/datasage-query/contracts/inventory-semantics.yaml",
    },
}

SUPPORTED_DOMAINS = tuple(DOMAIN_SOURCES)
ATTRIBUTION_MODES = ("transaction_detail", "salesperson_allocation")
ENTITY_TYPES = (
    "department",
    "customer",
    "salesperson",
    "product",
    "warehouse",
    "supplier",
)
ENTITY_RESOLVE_DEFAULT_LIMIT = 5
ENTITY_RESOLVE_HARD_LIMIT = 10
ENTITY_NORMALIZATION_STEPS = (
    "unicode_nfkc",
    "trim",
    "collapse_whitespace",
    "casefold",
)
# Runtime safety behavior is code-owned.  The same keys remain in the legacy
# YAML registry during migration and are checked as an exact compatibility
# mirror; they are not independently interpreted as executable policy.
ENTITY_RUNTIME_POLICY = {
    "explicit_type_wins": True,
    "exact_before_candidates": True,
    "registered_aliases_skip_lookup_when_entity_type_explicit": True,
    "fuzzy_candidates_never_auto_bind": True,
    "entity_only_stops_after_resolution": True,
    "default_max_candidates": ENTITY_RESOLVE_DEFAULT_LIMIT,
    "hard_max_candidates": ENTITY_RESOLVE_HARD_LIMIT,
}
BUSINESS_TIME_ZONE = timezone(timedelta(hours=8))
DELIVERY_SCOPES = ("default_net", "explicit_gross", "order_delivery_alignment")
INVENTORY_SCOPES = ("total", "on_hand", "available", "allocated", "in_transit")
# Compatibility export; request_contract is the sole owner of public limits.
PUBLIC_REQUEST_LIMIT = request_contract.PUBLIC_REQUEST_LIMIT
PHYSICAL_EXECUTION_BUDGET = 10
PREVIOUS_PERIOD_COMPARISON = "previous_period"
YEAR_OVER_YEAR_COMPARISON = "year_over_year"
SNAPSHOT_MONTHS_BEFORE_COMPARISON = "snapshot_months_before"
MATCHED_ELAPSED_COVERAGE = "matched_elapsed"
FLOW_COMPARISON_KINDS = (
    PREVIOUS_PERIOD_COMPARISON,
    YEAR_OVER_YEAR_COMPARISON,
)
PUBLIC_COMPARISON_KINDS = (
    *FLOW_COMPARISON_KINDS,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
)


class CapabilityContractError(ValueError):
    """A stable, non-sensitive capability-contract validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AvailabilityContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_CONTRACT_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIXED_FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_CALENDAR_MONTH = re.compile(
    r"^(?!0000-)(?!9999-12$)[0-9]{4}-(?:0[1-9]|1[0-2])$"
)


def _metric_group_dimension_limit(metric: Mapping[str, Any]) -> int:
    value = metric.get(
        "max_group_dimensions", request_contract.MAX_GROUP_DIMENSIONS
    )
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= request_contract.MAX_GROUP_DIMENSIONS
        or (
            metric.get("query_kind") is not None
            and "max_group_dimensions" not in metric
        )
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "分析指标缺少有效的分组维度上限。",
        )
    return value


def _fixed_filter_spec(spec: Mapping[str, Any]) -> tuple[str, Any]:
    if not isinstance(spec, Mapping):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "固定过滤规则格式无效。"
        )
    op = str(spec.get("op", "eq")).lower()
    value = spec.get("value")
    if op == "in":
        if (
            isinstance(value, list)
            and 1 <= len(value) <= request_contract.MAX_FILTER_VALUES
        ):
            return op, value
    elif op in _FIXED_FILTER_OPERATORS and not isinstance(value, (list, dict)):
        return op, value
    raise CapabilityContractError(
        "CONTRACT_UNAVAILABLE", "固定过滤规则不受支持。"
    )


def _dimension_columns(
    definition: Mapping[str, Any],
) -> list[tuple[str, str]]:
    raw_columns = definition.get("columns") or []
    if not isinstance(raw_columns, list) or not raw_columns:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "维度定义缺少有效字段。"
        )
    result: list[tuple[str, str]] = []
    for item in raw_columns:
        if isinstance(item, str):
            column, output = item, item
        elif isinstance(item, dict):
            column = item.get("column")
            output = item.get("alias") or column
        else:
            raise CapabilityContractError(
                "CONTRACT_UNAVAILABLE", "维度字段定义格式无效。"
            )
        if (
            not isinstance(column, str)
            or _CONTRACT_IDENTIFIER.fullmatch(column) is None
            or not isinstance(output, str)
            or _CONTRACT_IDENTIFIER.fullmatch(output) is None
        ):
            raise CapabilityContractError(
                "CONTRACT_UNAVAILABLE", "维度字段或输出名称无效。"
            )
        result.append((column, output))
    return result


def _entity_bindings(request: Mapping[str, Any]) -> Mapping[str, Any]:
    bindings = request.get("_entity_bindings") or {}
    if not isinstance(bindings, Mapping):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "实体绑定结构无效。"
        )
    return bindings


def _bound_entity_value(
    bindings: Mapping[str, Any],
    code: str,
    fallback_value: Any,
) -> tuple[Mapping[str, Any] | None, Any]:
    binding = bindings.get(code)
    if not isinstance(binding, Mapping):
        return None, fallback_value
    values = binding.get("filter_values")
    if not isinstance(values, list) or not values:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "实体绑定缺少稳定身份值。"
        )
    value: Any = values
    if not isinstance(fallback_value, list) and len(values) == 1:
        value = values[0]
    return binding, value


def _entity_filter_column(
    definition: Mapping[str, Any], binding: Mapping[str, Any] | None
) -> str:
    if binding is None:
        column = definition.get("filter_column")
        if not isinstance(column, str):
            raise CapabilityContractError(
                "CONTRACT_UNAVAILABLE", "维度过滤定义无效。"
            )
        return column
    identity_filter = definition.get("identity_filter")
    if (
        not isinstance(identity_filter, Mapping)
        or identity_filter.get("entity_type") != binding.get("entity_type")
        or identity_filter.get("value_field") != binding.get("value_field")
        or not isinstance(identity_filter.get("column"), str)
        or identity_filter.get("column")
        not in set(binding.get("identity_columns") or [])
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "实体稳定身份定义不一致。"
        )
    return str(identity_filter["column"])


def _bound_entity_filter(
    bindings: Mapping[str, Any],
    code: str,
    definition: Mapping[str, Any],
    fallback_value: Any,
) -> tuple[str, Any]:
    binding, value = _bound_entity_value(bindings, code, fallback_value)
    return _entity_filter_column(definition, binding), value


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _calendar_month_time_range(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or _CALENDAR_MONTH.fullmatch(value) is None:
        raise ValueError("calendar_month must use zero-padded YYYY-MM")
    start = date.fromisoformat(f"{value}-01")
    end = _shift_months(start, 1)
    return {"start": start.isoformat(), "end": end.isoformat()}


def business_today() -> date:
    """Return one Asia/Shanghai business-clock observation date."""

    return datetime.now(BUSINESS_TIME_ZONE).date()


def ensure_available(definition: Mapping[str, Any]) -> None:
    """Validate the cross-domain governed availability shape."""

    availability = definition.get("availability")
    if availability is None:
        return
    if not isinstance(availability, dict):
        raise AvailabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "指标可用性定义无效。",
        )
    status = str(availability.get("status") or "available")
    if status == "available":
        return
    if status not in {"blocked", "pending_validation"}:
        raise AvailabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "指标包含未知可用性状态。",
        )
    code = availability.get("error_code")
    message = availability.get("message")
    if (
        not isinstance(code, str)
        or not code
        or not isinstance(message, str)
        or not message
    ):
        raise AvailabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "不可用指标缺少结构化错误定义。",
        )
    # Operator-only reason text must not cross the model-visible boundary.
    raise AvailabilityContractError(code, "该指标当前不可用于回答。")


def query_request_schema_conditions() -> list[dict]:
    """Return JSON Schema conditions generated from cross-domain field facts."""

    conditions = [
        {
            "if": {"properties": {"domain": {"const": "target"}}},
            "then": {
                "required": ["attribution_mode"],
                "properties": {
                    "attribution_mode": {"enum": list(ATTRIBUTION_MODES)}
                },
            },
            "else": {"not": {"required": ["attribution_mode"]}},
        },
        {
            "if": {"required": ["delivery_scope"]},
            "then": {
                "properties": {
                    "domain": {"const": "delivery"},
                    "delivery_scope": {"enum": list(DELIVERY_SCOPES)},
                }
            },
        },
        {
            "if": {"required": ["inventory_scope"]},
            "then": {
                "properties": {
                    "domain": {"const": "inventory"},
                    "inventory_scope": {"enum": list(INVENTORY_SCOPES)},
                }
            },
        },
    ]
    return deepcopy(conditions)


def validate_request_field_contract(request: Mapping[str, Any]) -> None:
    """Validate only cross-domain field ownership and allowed values."""

    if not isinstance(request, Mapping):
        raise CapabilityContractError("INVALID_INPUT", "Request must be an object.")

    domain = request.get("domain")
    if domain not in SUPPORTED_DOMAINS:
        raise CapabilityContractError("INVALID_INPUT", "Unsupported request domain.")

    attribution_mode = request.get("attribution_mode")
    if domain == "target":
        if attribution_mode is None:
            raise CapabilityContractError(
                "ATTRIBUTION_MODE_REQUIRED",
                "Target requests require attribution_mode.",
            )
        if attribution_mode not in ATTRIBUTION_MODES:
            raise CapabilityContractError(
                "INVALID_INPUT", "Unsupported target attribution_mode."
            )
    elif "attribution_mode" in request:
        raise CapabilityContractError(
            "INVALID_INPUT", "attribution_mode is owned by the target domain."
        )

    if "delivery_scope" in request:
        if domain != "delivery":
            raise CapabilityContractError(
                "INVALID_INPUT", "delivery_scope is owned by the delivery domain."
            )
        if request.get("delivery_scope") not in DELIVERY_SCOPES:
            raise CapabilityContractError("INVALID_INPUT", "Unsupported delivery_scope.")

    if "inventory_scope" in request:
        if domain != "inventory":
            raise CapabilityContractError(
                "INVALID_INPUT", "inventory_scope is owned by the inventory domain."
            )
        if request.get("inventory_scope") not in INVENTORY_SCOPES:
            raise CapabilityContractError("INVALID_INPUT", "Unsupported inventory_scope.")


def physical_request_cost(request: Mapping[str, Any]) -> int:
    """Return the physical-slot cost of one public request branch."""

    if not isinstance(request, Mapping):
        raise CapabilityContractError("INVALID_INPUT", "Request must be an object.")
    complete_change = "complete_change_decomposition" in request
    complete_target_gap = "complete_target_gap_decomposition" in request
    if complete_change and complete_target_gap:
        raise CapabilityContractError(
            "INVALID_INPUT", "A request cannot contain both complete operations."
        )
    return 2 if complete_change or complete_target_gap else 1


_FORBIDDEN_CAPABILITY_KEYS = frozenset(
    {
        "triggers",
        "phrases",
        "question_types",
        "intent_routes",
        "route",
        "routes",
        "routing",
        "routing_precedence",
        "metric_selection",
        "metric_selection_boundary",
        "planning_rules",
        "tool_planning",
        "workflow",
        "plan",
        "plans",
        "fixed_plan",
        "recipes",
        "analysis_recipes",
        "bundle",
        "metric_bundle",
        "recommended_bundle",
        "required_metrics",
        "call_order",
        "sequence",
        "fixed_sequence",
        "max_tool_calls",
        "first_call",
        "second_call",
        "fallback",
        "answer_template",
        "template",
        "answer_shape",
        "output_order",
        "narrative_label",
        "threshold",
        "thresholds",
        "causal_explanation",
        "cause",
        "causes",
        "hypothesis",
        "hypotheses",
        "recommendation",
        "recommendations",
    }
)


def assert_capability_boundary(value: Any) -> None:
    """Reject planner judgement embedded in a capability-fact structure."""

    def visit(current: Any, path: tuple[str, ...]) -> None:
        if isinstance(current, Mapping):
            for raw_key, child in current.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                child_path = (*path, str(raw_key))
                if key in _FORBIDDEN_CAPABILITY_KEYS:
                    raise CapabilityContractError(
                        "CAPABILITY_BOUNDARY_VIOLATION",
                        f"Planner-owned field is not a capability fact: {'.'.join(child_path)}",
                    )
                visit(child, child_path)
        elif isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                visit(child, (*path, str(index)))

    visit(value, ())
