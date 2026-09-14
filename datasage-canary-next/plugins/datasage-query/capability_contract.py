"""Pure cross-domain capability facts shared by Schema and runtime validation.

This module is deliberately small.  Metric semantics and physical adapters stay
in the versioned semantics files; Hermes remains responsible for planning and
business judgement.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
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
    "inventory": {
        "semantics": "plugins/datasage-query/contracts/inventory-semantics.yaml",
    },
    "pattern_matching": {
        "semantics": "plugins/datasage-query/contracts/pattern-matching-semantics.yaml",
    },
    "profit": {
        "semantics": "plugins/datasage-query/contracts/profit-semantics.yaml",
    },
}

CHANGE_DIRECTIONS = ("decrease", "increase", "absolute")
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
DELIVERY_SCOPES = ("default_net", "explicit_gross", "order_delivery_alignment", "source_recorded")
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
QUERY_POLICY_PATH = "plugins/datasage-query/contracts/query-policy.yaml"
TARGET_GAP_CONTRACT_PATH = (
    "plugins/datasage-query/contracts/target-gap-decomposition.yaml"
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
_VALUE_CONTRACT_KINDS = frozenset({"closed", "source_exact", "entity_exact"})
_VALUE_SCALAR_TYPES = (str, int, float, bool)


@dataclass(frozen=True)
class QueryPolicy:
    """Validated query-policy facts shared by planner and executor."""

    version: str
    start_inclusive: bool
    end_exclusive: bool
    max_days: int
    wider_analysis: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "governed_metric_time_range": {
                "start_inclusive": self.start_inclusive,
                "end_exclusive": self.end_exclusive,
                "max_days": self.max_days,
                "wider_analysis": self.wider_analysis,
            },
        }


@dataclass(frozen=True)
class ValueContract:
    """One validated dimension-value contract and its exact projection."""

    kind: str
    filterable: bool
    allowed_values: tuple[Any, ...] = ()
    canonical_aliases: tuple[tuple[Any, Any], ...] = ()
    business_meanings: tuple[tuple[Any, str], ...] = ()
    _projection: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def as_mapping(self) -> dict[str, Any]:
        return deepcopy(self._projection)

    def normalize_filter_value(self, raw_value: Any) -> Any:
        """Validate and normalize one scalar or non-empty scalar list."""

        is_list = isinstance(raw_value, list)
        values = raw_value if is_list else [raw_value]
        if not values or any(not is_value_scalar(value) for value in values):
            raise CapabilityContractError(
                "INVALID_INPUT",
                "Dimension filter values must be non-null finite scalars.",
            )
        if self.kind != "closed":
            return deepcopy(raw_value)
        allowed = {_typed_scalar_key(value) for value in self.allowed_values}
        aliases = {
            _typed_scalar_key(alias): canonical
            for alias, canonical in self.canonical_aliases
        }
        normalized = [
            aliases.get(_typed_scalar_key(value), value) for value in values
        ]
        if any(_typed_scalar_key(value) not in allowed for value in normalized):
            raise CapabilityContractError(
                "FILTER_VALUE_NOT_ALLOWED",
                "Dimension filter value is outside the governed closed set.",
            )
        return normalized if is_list else normalized[0]


@dataclass(frozen=True)
class TargetGapContract:
    """Minimal executable facts from the target-gap YAML authority."""

    version: str
    metrics: tuple[str, ...]
    attribution_mode: str
    dimensions: tuple[str, ...]
    valid_target_data_states: tuple[str, ...]
    fail_closed_target_data_states: tuple[str, ...]
    receipt_version: str
    receipt_operation: str
    receipt_interpretation_code: str


def parse_query_policy(raw: Any) -> QueryPolicy:
    """Parse the sole supported version of the common query policy."""

    if not isinstance(raw, Mapping):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Common query policy must be a mapping."
        )
    time_range = raw.get("governed_metric_time_range")
    if (
        raw.get("version") != "datasage-query-policy/v1"
        or set(raw) != {"version", "governed_metric_time_range"}
        or not isinstance(time_range, Mapping)
        or set(time_range)
        != {"start_inclusive", "end_exclusive", "max_days", "wider_analysis"}
        or time_range.get("start_inclusive") is not True
        or time_range.get("end_exclusive") is not True
        or not isinstance(time_range.get("max_days"), int)
        or isinstance(time_range.get("max_days"), bool)
        or time_range["max_days"] < 1
        or time_range.get("wider_analysis")
        != "split_into_independently_bounded_periods"
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Common query policy is invalid."
        )
    return QueryPolicy(
        version=str(raw["version"]),
        start_inclusive=True,
        end_exclusive=True,
        max_days=int(time_range["max_days"]),
        wider_analysis=str(time_range["wider_analysis"]),
    )


def is_value_scalar(value: Any) -> bool:
    return isinstance(value, _VALUE_SCALAR_TYPES) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _typed_scalar_key(value: Any) -> tuple[type[Any], Any]:
    return type(value), value


def parse_value_contract(
    raw: Any,
    *,
    filterable_default: bool = True,
) -> ValueContract:
    """Validate one value contract, including typed uniqueness and aliases."""

    if not isinstance(raw, Mapping):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Dimension value contract must be a mapping."
        )
    kind = raw.get("kind")
    if kind not in _VALUE_CONTRACT_KINDS:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Dimension value contract kind is invalid."
        )
    allowed_keys = {"kind", "filterable", "model_rule"}
    if kind == "closed":
        allowed_keys.update(
            {"allowed_values", "canonical_aliases", "business_meanings"}
        )
    if set(raw) - allowed_keys:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Dimension value contract has unknown fields."
        )
    model_rule = raw.get("model_rule")
    if model_rule is not None and (
        not isinstance(model_rule, str) or not model_rule.strip()
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Dimension model rule is invalid."
        )
    filterable = raw.get("filterable", filterable_default)
    if not isinstance(filterable, bool):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Dimension filterability must be boolean."
        )

    allowed_values: tuple[Any, ...] = ()
    canonical_aliases: tuple[tuple[Any, Any], ...] = ()
    business_meanings: tuple[tuple[Any, str], ...] = ()
    if kind == "closed":
        allowed = raw.get("allowed_values")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(not is_value_scalar(value) for value in allowed)
        ):
            raise CapabilityContractError(
                "CONTRACT_UNAVAILABLE",
                "Closed value contract requires non-null finite scalar values.",
            )
        typed_allowed = {_typed_scalar_key(value) for value in allowed}
        if len(typed_allowed) != len(allowed):
            raise CapabilityContractError(
                "CONTRACT_UNAVAILABLE", "Closed value contract contains duplicates."
            )
        allowed_values = tuple(allowed)

        aliases = raw.get("canonical_aliases")
        if aliases is not None:
            if not isinstance(aliases, Mapping):
                raise CapabilityContractError(
                    "CONTRACT_UNAVAILABLE", "Canonical aliases must be a mapping."
                )
            alias_keys = {
                _typed_scalar_key(alias)
                for alias in aliases
                if is_value_scalar(alias)
            }
            if len(alias_keys) != len(aliases):
                raise CapabilityContractError(
                    "CONTRACT_UNAVAILABLE", "Canonical aliases contain invalid keys."
                )
            for alias, canonical in aliases.items():
                alias_key = _typed_scalar_key(alias)
                canonical_key = (
                    _typed_scalar_key(canonical)
                    if is_value_scalar(canonical)
                    else None
                )
                if (
                    canonical_key not in typed_allowed
                    or alias_key in typed_allowed
                    or canonical_key in alias_keys
                ):
                    raise CapabilityContractError(
                        "CONTRACT_UNAVAILABLE",
                        "Canonical alias must map directly to a distinct allowed value.",
                    )
            canonical_aliases = tuple(aliases.items())

        meanings = raw.get("business_meanings")
        if meanings is not None:
            if not isinstance(meanings, Mapping) or any(
                not is_value_scalar(key)
                or not isinstance(value, str)
                or not value.strip()
                for key, value in meanings.items()
            ):
                raise CapabilityContractError(
                    "CONTRACT_UNAVAILABLE", "Business meanings are invalid."
                )
            business_meanings = tuple(meanings.items())
    elif "canonical_aliases" in raw or "allowed_values" in raw:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "Only a closed value contract may declare allowed values or aliases.",
        )

    return ValueContract(
        kind=str(kind),
        filterable=filterable,
        allowed_values=allowed_values,
        canonical_aliases=canonical_aliases,
        business_meanings=business_meanings,
        _projection=deepcopy(dict(raw)),
    )


def _unique_nonempty_strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", f"Target-gap {field_name} is invalid."
        )
    return tuple(value)


def parse_target_gap_contract(raw: Any) -> TargetGapContract:
    """Parse the executable target-gap facts consumed across all boundaries."""

    if not isinstance(raw, Mapping):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Target-gap contract must be a mapping."
        )
    applicability = raw.get("applicability")
    receipt = raw.get("receipt")
    rollout = raw.get("rollout")
    if (
        set(raw)
        != {
            "version",
            "status",
            "applicability",
            "valid_target_data_states",
            "fail_closed_target_data_states",
            "receipt",
            "rollout",
        }
        or raw.get("version") != "datasage-target-gap-decomposition/v1"
        or raw.get("status") != "active"
        or not isinstance(applicability, Mapping)
        or set(applicability) != {"metrics", "attribution_mode", "dimensions"}
        or not isinstance(receipt, Mapping)
        or set(receipt) != {"version", "operation", "interpretation_code"}
        or not isinstance(rollout, Mapping)
        or set(rollout) != {"status", "model_visible_operation"}
        or rollout.get("status") != "active"
        or rollout.get("model_visible_operation") is not True
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Target-gap capability is not active."
        )
    metrics = _unique_nonempty_strings(
        applicability.get("metrics"), field_name="metrics"
    )
    dimensions = _unique_nonempty_strings(
        applicability.get("dimensions"), field_name="dimensions"
    )
    attribution_mode = applicability.get("attribution_mode")
    if not isinstance(attribution_mode, str) or not attribution_mode:
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Target-gap attribution mode is invalid."
        )
    valid_states = _unique_nonempty_strings(
        raw.get("valid_target_data_states"), field_name="valid target states"
    )
    failed_states = _unique_nonempty_strings(
        raw.get("fail_closed_target_data_states"),
        field_name="fail-closed target states",
    )
    if set(valid_states) & set(failed_states):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Target-gap target states overlap."
        )
    receipt_version = receipt.get("version")
    receipt_operation = receipt.get("operation")
    interpretation_code = receipt.get("interpretation_code")
    if (
        not isinstance(receipt_version, str)
        or not receipt_version
        or receipt_operation != "complete_target_gap_decomposition"
        or not isinstance(interpretation_code, str)
        or not interpretation_code
    ):
        raise CapabilityContractError(
            "CONTRACT_UNAVAILABLE", "Target-gap receipt contract is invalid."
        )
    return TargetGapContract(
        version=str(raw["version"]),
        metrics=metrics,
        attribution_mode=attribution_mode,
        dimensions=dimensions,
        valid_target_data_states=valid_states,
        fail_closed_target_data_states=failed_states,
        receipt_version=receipt_version,
        receipt_operation=str(receipt_operation),
        receipt_interpretation_code=interpretation_code,
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


TARGET_COMPLETION_UNIT = "比例"
TARGET_COMPLETION_FACT_UNITS = {
    "metric_value": TARGET_COMPLETION_UNIT,
    "completion_rate": TARGET_COMPLETION_UNIT,
    "target_amount_rmb": "人民币元",
    "actual_amount_rmb": "人民币元",
    "gap_amount_rmb": "人民币元",
}


def effective_dimension_definitions(
    semantics: Mapping[str, Any], metric: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Resolve metric-specific definitions without inheriting a different SQL source.

    Existing overrides replace physical mappings. Only omitted descriptive/value
    metadata inherits the domain contract; joins, normalization, identity columns
    and public display exceptions must remain explicitly declared by the override.
    """
    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise CapabilityContractError("CONTRACT_UNAVAILABLE", "Dimension definitions are invalid.")
    if isinstance(metric, str):
        metrics = semantics.get("metrics")
        metric = metrics.get(metric) if isinstance(metrics, Mapping) else None
        if not isinstance(metric, Mapping):
            raise CapabilityContractError("CONTRACT_UNAVAILABLE", "Metric dimension owner is invalid.")
    overrides = metric.get("dimension_overrides", {}) if isinstance(metric, Mapping) else {}
    if not isinstance(overrides, Mapping) or not set(overrides) <= set(dimensions):
        raise CapabilityContractError("CONTRACT_UNAVAILABLE", "Metric dimension overrides are invalid.")
    effective = dict(dimensions)
    metadata = {"label", "business_definition", "semantics", "value_contract", "filterable", "unit", "time_semantics"}
    for code, override in overrides.items():
        base = dimensions[code]
        if not isinstance(base, Mapping) or not isinstance(override, Mapping):
            raise CapabilityContractError("CONTRACT_UNAVAILABLE", "Metric dimension override is invalid.")
        effective[code] = {**{k: v for k, v in base.items() if k in metadata}, **override}
    return effective


MONTHLY_SLOW_FORBIDDEN_PARAMETERS = ('baseline_week', 'comparison', 'time_bucket', 'order_by')
HISTORY_FORBIDDEN_PARAMETERS = ('time_range','calendar_month','time_bucket','comparison','order_by','movement_state','inventory_scope')


def analytical_time_buckets(metric: Mapping[str, Any]) -> list[str] | None:
    """Shared analytical-builder time-bucket gate; None leaves ordinary metrics alone."""
    kind = metric.get("query_kind")
    if kind is None:
        return None
    if kind == "fabric_source" and metric.get("fabric_side") != "delivery":
        return []
    return ["month"] if kind in {"target_completion", "allocated_amount", "pattern_matching", "fabric_source"} else []


def metric_grouping(metric: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """One grouping contract for both catalog and pre-I/O validation.
    allowed_dimensions continues to include independently legal filters.
    """
    raw=metric.get('grouping')
    if raw is None:return None
    if not isinstance(raw,Mapping) or set(raw)!={'allowed','required','default'}:
        raise CapabilityContractError('CONTRACT_UNAVAILABLE','指标分组定义无效。')
    for value in raw.values():
        if not isinstance(value,list) or any(not isinstance(x,str) for x in value) or len(set(value))!=len(value):
            raise CapabilityContractError('CONTRACT_UNAVAILABLE','指标分组定义无效。')
    if not set(raw['required'])<=set(raw['default'])<=set(raw['allowed'])<=set(metric.get('allowed_dimensions') or []):
        raise CapabilityContractError('CONTRACT_UNAVAILABLE','指标分组与过滤维度不一致。')
    return {k:list(v) for k,v in raw.items()}


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

    status = validate_availability(definition)
    if status == "available":
        return
    availability = definition["availability"]
    raise AvailabilityContractError(
        str(availability["error_code"]), "该指标当前不可用于回答。"
    )


def validate_availability(definition: Mapping[str, Any]) -> str:
    """Validate availability metadata and return its normalized status.

    Catalog compilation needs to validate unavailable metrics without treating a
    well-formed pending/blocked metric as an execution request.  The existing
    ``ensure_available`` function intentionally raises for those statuses, so
    this helper owns the shape validation while leaving execution denial to the
    caller.
    """

    availability = definition.get("availability")
    if availability is None:
        return "available"
    if not isinstance(availability, dict):
        raise AvailabilityContractError(
            "CONTRACT_UNAVAILABLE",
            "指标可用性定义无效。",
        )
    status = str(availability.get("status") or "available")
    if status == "available":
        return status
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
    return status


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
