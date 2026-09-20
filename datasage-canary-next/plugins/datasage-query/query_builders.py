"""Governed metric SQL planning and construction.

This module owns the complete metric builder closure. It has no database
execution or entity lookup side effects; tools.py keeps compatibility aliases
for callers and retains execution, evidence, and public handlers.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from .analytical_queries import (
    AnalysisQueryError,
    build_analytical_metric_query,
)
from .capability_contract import (
    AvailabilityContractError,
    CapabilityContractError,
    MATCHED_ELAPSED_COVERAGE,
    PREVIOUS_PERIOD_COMPARISON,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
    YEAR_OVER_YEAR_COMPARISON,
    business_today,
    ensure_available,
)
from .query_errors import QueryFailure
from .query_sql import (
    _INTERNAL_MATCH_COUNT,
    _approved_column,
    _blocked_columns,
    _bound_entity_filter,
    _currency_missing_input_sql,
    _dimension_columns,
    _dimension_expression,
    _filter_clause,
    _integrity_columns,
    _latest_snapshot_resolver_sql,
    _metric_aggregation_sql,
    _metric_missing_input_sql,
    _metric_unclassified_source_value_sql,
    _normalized_dimension_value,
    _overdue_integrity_columns,
    _qualified_identifier,
    _quote_identifier,
    _quote_table,
)
from . import (
    analytical_handlers,
    capability_contract,
    contract_store,
    contracts,
    evidence,
    request_contract,
)

_MYSQL_MONTH_FORMAT = "%%Y-%%m"
_MYSQL_DAY_FORMAT = "%%Y-%%m-%%d"
_SNAPSHOT_TIME_SOURCES = {
    "latest_snapshot",
    "latest_non_null_snapshot",
    "latest_snapshot_offset",
}
_INTERNAL_SNAPSHOT_MONTH = "__snapshot_month"
_INTERNAL_COMPARISON_SNAPSHOT_MONTH = "__comparison_snapshot_month"
_INTERNAL_AS_OF_DATE = "__as_of_date"
_INTERNAL_PARTITION_CURRENT = "__full_partition_metric_value"
_INTERNAL_PARTITION_COMPARISON = "__full_partition_comparison_value"
_INTERNAL_PARTITION_DELTA = "__full_partition_delta_value"
_INTERNAL_PARTITION_ROW_COUNT = "__full_partition_row_count"
_INTERNAL_PARTITION_CURRENT_MISSING = "__full_partition_current_missing_value_count"
_INTERNAL_PARTITION_CURRENT_KNOWN = "__full_partition_current_known_value_count"
_INTERNAL_PARTITION_COMPARISON_MISSING = (
    "__full_partition_comparison_missing_value_count"
)
_INTERNAL_PARTITION_COMPARISON_KNOWN = (
    "__full_partition_comparison_known_value_count"
)
_INTERNAL_RESULT_FIELDS = {
    _INTERNAL_MATCH_COUNT,
    _INTERNAL_SNAPSHOT_MONTH,
    _INTERNAL_COMPARISON_SNAPSHOT_MONTH,
    _INTERNAL_AS_OF_DATE,
    _INTERNAL_PARTITION_CURRENT,
    _INTERNAL_PARTITION_COMPARISON,
    _INTERNAL_PARTITION_DELTA,
    _INTERNAL_PARTITION_ROW_COUNT,
    _INTERNAL_PARTITION_CURRENT_MISSING,
    _INTERNAL_PARTITION_CURRENT_KNOWN,
    _INTERNAL_PARTITION_COMPARISON_MISSING,
    _INTERNAL_PARTITION_COMPARISON_KNOWN,
}
_DATABASE_CURRENT_DATE_EVIDENCE = "database_current_date"
_DATABASE_QUERY_DATE_OBSERVATION = "database_query_date_observation"
_MATCHED_ELAPSED_COMPARISON_VERSION = "matched-elapsed-comparison/v1"

def _ensure_metric_available(metric: Mapping[str, Any]) -> None:
    try:
        ensure_available(metric)
    except AvailabilityContractError as exc:
        raise QueryFailure(exc.code, exc.message) from exc

def _ensure_metric_tree_available(
    metric_code: Any,
    semantics: Mapping[str, Any],
    *,
    _visiting: set[str] | None = None,
    _checked: set[str] | None = None,
) -> None:
    """Check availability for every governed operand before compilation.

    Derived metrics reference other metrics through ``components``, ``ratio``,
    or ``source_completion_metric``.  Availability is a property of each
    operand, not only the public wrapper.  Keep this traversal bounded and fail
    closed if a malformed contract introduces a reference cycle.
    """

    if not isinstance(metric_code, str) or not metric_code:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标来源引用必须是非空字符串。",
            stage="contract_load",
        )
    if not isinstance(semantics, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标来源合同格式无效。",
            stage="contract_load",
        )
    metrics = semantics.get("metrics")
    if not isinstance(metrics, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标来源合同缺少指标注册表。",
            stage="contract_load",
        )
    metric = metrics.get(metric_code)
    if not isinstance(metric, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标来源引用了未注册指标。",
            stage="contract_load",
        )
    visiting = _visiting if _visiting is not None else set()
    checked = _checked if _checked is not None else set()
    if metric_code in checked:
        return
    if metric_code in visiting:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标来源合同存在循环引用。",
            stage="contract_load",
        )
    visiting.add(metric_code)
    try:
        _ensure_metric_available(metric)
        references: list[Any] = []
        components = metric.get("components")
        if components is not None:
            if not isinstance(components, list) or not components:
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "复合指标组成合同格式无效。",
                    stage="contract_load",
                )
            for component in components:
                if not isinstance(component, Mapping):
                    raise QueryFailure(
                        "CONTRACT_UNAVAILABLE",
                        "复合指标组成引用格式无效。",
                        stage="contract_load",
                    )
                references.append(component.get("metric"))
        ratio = metric.get("ratio")
        if ratio is not None:
            if not isinstance(ratio, Mapping):
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "比例指标来源合同格式无效。",
                    stage="contract_load",
                )
            references.extend((ratio.get("numerator"), ratio.get("denominator")))
        if "source_completion_metric" in metric:
            references.append(metric.get("source_completion_metric"))
        for reference in references:
            _ensure_metric_tree_available(
                reference,
                semantics,
                _visiting=visiting,
                _checked=checked,
            )
    finally:
        visiting.remove(metric_code)
    checked.add(metric_code)

def _max_group_dimensions(metric: Mapping[str, Any]) -> int:
    try:
        return capability_contract._metric_group_dimension_limit(metric)
    except CapabilityContractError as exc:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "分析指标缺少有效的分组维度上限。",
        ) from exc

def _max_metric_range_days() -> int:
    """Read the executor limit from the same authority exposed to planning."""

    try:
        policy = contract_store.read_query_policy()
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ) as exc:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "治理查询时间策略无效。",
            stage="contract_load",
        ) from exc
    return policy.max_days

def _validate_governed_request_time_range(request: Mapping[str, Any]) -> None:
    """Apply the common span policy before any metric compiler can branch."""

    supplied = request.get("time_range")
    if supplied is None:
        return
    if not isinstance(supplied, Mapping):
        raise QueryFailure("INVALID_PLAN", "时间范围格式无效。")
    start, end = supplied.get("start"), supplied.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise QueryFailure("INVALID_PLAN", "时间范围格式无效。")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", end
    ):
        granularity = "date"
    else:
        raise QueryFailure("INVALID_PLAN", "时间范围格式无效。")
    _validate_time_bounds(
        start,
        end,
        granularity,
        max_days=_max_metric_range_days(),
    )

def _effective_dimensions(
    semantics: Mapping[str, Any], metric: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    try:
        return capability_contract.effective_dimension_definitions(semantics, metric)
    except CapabilityContractError as exc:
        raise QueryFailure(exc.code, exc.message, stage="contract_load") from exc

def _next_month_start(today: date) -> date:
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    return date(year, month, 1)

def _business_today() -> date:
    """Return one business-clock observation date, never a source watermark."""

    return business_today()

def _default_time_range(
    policy: str, observed_on: date | None = None
) -> tuple[str, str] | None:
    if policy != "current_month":
        return None
    today = observed_on or _business_today()
    return date(today.year, today.month, 1).isoformat(), _next_month_start(today).isoformat()

def _metric_time_value_format(metric: Mapping[str, Any]) -> str:
    """Resolve the storage format for a month-granular metric."""

    if metric.get("time_granularity") != "month":
        return "date"
    value_format = metric.get("time_value_format", "month")
    if value_format not in {"month", "date"}:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "月粒度指标的时间值格式无效。")
    return str(value_format)

def _metric_time_bounds(metric: Mapping[str, Any], start: str, end: str) -> tuple[str, str]:
    value_format = _metric_time_value_format(metric)
    if metric.get("time_granularity") != "month":
        return start, end
    month_pattern = re.compile(r"^\d{4}-\d{2}$")
    boundary_pattern = re.compile(r"^\d{4}-\d{2}-01$")
    if value_format == "date":
        if boundary_pattern.fullmatch(start) and boundary_pattern.fullmatch(end):
            return start, end
        raise QueryFailure("INVALID_PLAN", "日期承载的月粒度指标必须使用自然月首日边界。")
    if month_pattern.fullmatch(start) and month_pattern.fullmatch(end):
        return start, end
    if boundary_pattern.fullmatch(start) and boundary_pattern.fullmatch(end):
        return start[:7], end[:7]
    raise QueryFailure("INVALID_PLAN", "月粒度指标必须使用 YYYY-MM 或自然月首日边界。")

def _add_months(value: date, months: int) -> date:
    try:
        return capability_contract._shift_months(value, months)
    except (ValueError, OverflowError) as exc:
        raise QueryFailure(
            "INVALID_PLAN", "期间比较超出支持的日期边界。"
        ) from exc

def _comparison_period(start: str, end: str) -> tuple[str, str]:
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QueryFailure("INVALID_PLAN", "期间比较必须使用 YYYY-MM-DD。") from exc
    if start_date >= end_date:
        raise QueryFailure("INVALID_PLAN", "期间比较范围无效。")
    try:
        if start_date.day == 1 and end_date.day == 1:
            months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
            if months > 0:
                return _add_months(start_date, -months).isoformat(), start_date.isoformat()
        duration = end_date - start_date
        return (start_date - duration).isoformat(), start_date.isoformat()
    except (ValueError, OverflowError) as exc:
        raise QueryFailure("INVALID_PLAN", "期间比较超出支持的日期边界。") from exc

def _shift_calendar_year(value: date, years: int) -> date:
    target_year = value.year + years
    return value.replace(
        year=target_year,
        day=min(value.day, calendar.monthrange(target_year, value.month)[1]),
    )

def _year_over_year_matched_elapsed_ranges(
    start: str,
    end: str,
    observed_on: date,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QueryFailure(
            "INVALID_PLAN",
            "同比期间必须使用 YYYY-MM-DD。",
        ) from exc
    if start_date >= end_date:
        raise QueryFailure("INVALID_PLAN", "同比期间范围无效。")
    effective_end = min(end_date, observed_on + timedelta(days=1))
    if effective_end <= start_date:
        raise QueryFailure("INVALID_PLAN", "同比当前期间尚未开始。")
    try:
        prior_start = _shift_calendar_year(start_date, -1)
        if (
            effective_end == end_date
            and start_date.day == 1
            and end_date.day == 1
        ):
            # Calendar-month requests retain their month boundary semantics;
            # elapsed-day windows (including clipped/partial leap-year windows)
            # derive the comparison end from the effective current duration.
            prior_end = _shift_calendar_year(effective_end, -1)
        else:
            prior_end = prior_start + (effective_end - start_date)
    except (ValueError, OverflowError) as exc:
        raise QueryFailure("INVALID_PLAN", "同比期间超出支持的日期边界。") from exc
    current = {"start": start_date.isoformat(), "end": effective_end.isoformat()}
    prior = {"start": prior_start.isoformat(), "end": prior_end.isoformat()}
    alignment = {
        "version": _MATCHED_ELAPSED_COMPARISON_VERSION,
        "kind": YEAR_OVER_YEAR_COMPARISON,
        "coverage": MATCHED_ELAPSED_COVERAGE,
        "observed_on": observed_on.isoformat(),
        "requested_current_start": start_date.isoformat(),
        "requested_current_end": end_date.isoformat(),
        "effective_current_end": effective_end.isoformat(),
        "current_was_clipped": effective_end < end_date,
    }
    return current, prior, alignment

def _parse_time_boundary(value: Any, value_format: str) -> date:
    if not isinstance(value, str):
        raise QueryFailure("INVALID_PLAN", "时间边界必须是字符串。")
    try:
        if value_format == "month":
            return datetime.strptime(value, "%Y-%m").date()
        if value_format == "date":
            return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        expected = "YYYY-MM" if value_format == "month" else "YYYY-MM-DD"
        raise QueryFailure("INVALID_PLAN", f"时间边界必须使用 {expected}。") from exc
    raise QueryFailure("CONTRACT_UNAVAILABLE", "数据集时间格式定义无效。")

def _validate_time_bounds(
    start: Any,
    end: Any,
    value_format: str,
    *,
    max_days: int,
) -> tuple[str, str]:
    start_value = _parse_time_boundary(start, value_format)
    end_value = _parse_time_boundary(end, value_format)
    if start_value >= end_value:
        raise QueryFailure("INVALID_PLAN", "时间范围起止值无效。")
    if (end_value - start_value).days > max_days:
        raise QueryFailure(
            "QUERY_RANGE_TOO_WIDE",
            f"单次查询时间范围不能超过 {max_days} 天，请拆分期间。",
        )
    return str(start), str(end)

def _fixed_filter_refines(inherited: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Return true when a metric fixed filter is a safe subset of a dataset filter."""

    inherited_op = str(inherited.get("op", "eq")).lower()
    candidate_op = str(candidate.get("op", "eq")).lower()
    inherited_value = inherited.get("value")
    candidate_value = candidate.get("value")
    if inherited_op != "in" or not isinstance(inherited_value, list):
        return False
    inherited_values = set(inherited_value)
    if candidate_op == "eq":
        return candidate_value in inherited_values
    if candidate_op == "in" and isinstance(candidate_value, list) and candidate_value:
        return set(candidate_value).issubset(inherited_values)
    return False

def _system_filter_record(
    dataset: str, column: str, spec: Mapping[str, Any], source: str
) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "column": column,
        "op": str(spec.get("op", "eq")).lower(),
        "value": spec.get("value"),
        "source": source,
    }

def _merge_records(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        for record in group:
            item = dict(record)
            if item not in merged:
                merged.append(item)
    return merged

def _governed_join(
    definition: Mapping[str, Any],
    base_table: str,
    base_dataset: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    join_states: dict[tuple[Any, ...], dict[str, Any]],
    *,
    purpose: str,
) -> tuple[str, Mapping[str, Any]]:
    source = definition.get("source") or {"type": "fact"}
    if not isinstance(source, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度来源定义无效。")
    source_type = source.get("type", "fact")
    if source_type == "fact":
        return "f", base_dataset
    if source_type == "barcode_fact":
        if not (
            base_dataset.get("grain") == "one_actual_outbound_piece_or_barcode"
            and base_dataset.get("metric_only") is True
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "物理条码维度只能用于已登记的条码事实指标。",
            )
        return "f", base_dataset
    if source_type != "governed_join":
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度来源类型不受支持。")

    return _register_governed_join(
        source,
        base_dataset,
        datasets_contract,
        join_states,
        purpose=purpose,
    )

def _register_governed_join(
    source: Mapping[str, Any],
    base_dataset: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    join_states: dict[tuple[Any, ...], dict[str, Any]],
    *,
    purpose: str,
) -> tuple[str, Mapping[str, Any]]:
    if purpose not in {"metric", "filter", "projection"}:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "受控关联用途无效。")
    target = source.get("dataset")
    local_columns = source.get("local_columns")
    remote_columns = source.get("remote_columns")
    if local_columns is None and source.get("local_column") is not None:
        local_columns = [source.get("local_column")]
    if remote_columns is None and source.get("remote_column") is not None:
        remote_columns = [source.get("remote_column")]
    cardinality = source.get("cardinality")
    dataset_map = datasets_contract.get("datasets") or {}
    target_dataset = dataset_map.get(target) if isinstance(dataset_map, dict) else None
    if (
        not isinstance(target, str)
        or not isinstance(target_dataset, dict)
        or not isinstance(local_columns, list)
        or not isinstance(remote_columns, list)
        or not local_columns
        or len(local_columns) != len(remote_columns)
        or any(not isinstance(column, str) for column in [*local_columns, *remote_columns])
        or cardinality != "many_to_one"
    ):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "受控关联定义无效。")

    approved_join = False
    for contract in base_dataset.get("joins") or []:
        if not isinstance(contract, dict):
            continue
        if (
            contract.get("target") == target
            and contract.get("local") == local_columns
            and contract.get("remote") == remote_columns
            and contract.get("cardinality") == "many_to_one"
        ):
            approved_join = True
            break
    if not approved_join:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度关联未进入数据集白名单。")

    base_allowed = {str(item) for item in base_dataset.get("allowed_columns") or []}
    target_allowed = {str(item) for item in target_dataset.get("allowed_columns") or []}
    for local in local_columns:
        _approved_column(local, base_allowed, _blocked_columns(datasets_contract, base_dataset))
    for remote in remote_columns:
        _approved_column(remote, target_allowed, _blocked_columns(datasets_contract, target_dataset))
    unique_key_sets = [target_dataset.get("primary_key") or []]
    unique_key_sets.extend(target_dataset.get("unique_key_sets") or [])
    if remote_columns not in unique_key_sets:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "受控关联目标键不是已声明唯一键。")

    key = (target, *local_columns, "=>", *remote_columns)
    if key not in join_states:
        join_states[key] = {
            "alias": f"j{len(join_states) + 1}",
            "table": target,
            "locals": list(local_columns),
            "remotes": list(remote_columns),
            "dataset": target_dataset,
            "cardinality": "many_to_one",
            "join_type": "left",
            "unique_target_key": True,
            "deduplication": "none_unique_target_key",
            "purposes": set(),
        }
    join_states[key]["purposes"].add(purpose)
    return str(join_states[key]["alias"]), target_dataset

def _metric_dimension_definition(
    dimensions: Mapping[str, Any], metric: Mapping[str, Any], code: str
) -> Mapping[str, Any]:
    definition = _effective_dimensions({"dimensions": dimensions}, metric).get(code)
    if not isinstance(definition, Mapping):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标维度定义无效。")
    return definition

def _build_metric_core(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    dimension_contract: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    sign: int = 1,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    if sign not in {-1, 1}:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标符号只能是 1 或 -1。")
    table = metric.get("table")
    dataset_map = datasets_contract.get("datasets", {})
    dataset = dataset_map.get(table) if isinstance(dataset_map, dict) else None
    if not isinstance(table, str) or not isinstance(dataset, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标引用了未批准的数据集。")
    base_allowed = {str(item) for item in dataset.get("allowed_columns") or []}
    base_blocked = _blocked_columns(datasets_contract, dataset)
    time_field = metric.get("time_field")

    requested_dimensions = request.get("dimensions") or []
    if (
        not isinstance(requested_dimensions, list)
        or len(requested_dimensions) > request_contract.MAX_GROUP_DIMENSIONS
        or any(not isinstance(code, str) for code in requested_dimensions)
        or len(requested_dimensions) != len(set(requested_dimensions))
    ):
        raise QueryFailure("INVALID_PLAN", "维度列表无效。")
    requested_filters = request.get("metric_filters") or {}
    if not isinstance(requested_filters, dict):
        raise QueryFailure("INVALID_PLAN", "过滤条件格式无效。")
    dimensions = semantics.get("dimensions") or {}
    if not isinstance(dimensions, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度语义格式无效。")
    allowed_dimensions = set(dimension_contract.get("allowed_dimensions") or [])
    component_dimensions = set(metric.get("allowed_dimensions") or [])

    dimension_codes: list[str] = []
    # Register population-changing filter joins before presentation-only joins
    # so compiler aliases remain stable when a projection is added or removed.
    for code in [*sorted(requested_filters), *requested_dimensions]:
        if (
            code not in allowed_dimensions
            or code not in component_dimensions
            or not isinstance(dimensions.get(code), dict)
        ):
            raise QueryFailure("UNSUPPORTED_DIMENSION", "该指标不支持请求中的某个维度或过滤条件。")
        if code not in dimension_codes:
            dimension_codes.append(str(code))
    currency_policy = metric.get("currency_policy")
    requires_currency_scope = currency_policy == "group_or_filter" or (
        isinstance(currency_policy, dict)
        and currency_policy.get("mode") == "original_currency"
        and currency_policy.get("require_filter_or_group") is True
    )
    if requires_currency_scope and not (
        "currency" in requested_dimensions or "currency" in requested_filters
    ):
        raise QueryFailure("CURRENCY_SCOPE_REQUIRED", "原币指标必须按币种分组或限定单一币种。")
    if (
        requires_currency_scope
        and "currency" not in requested_dimensions
        and "currency" in requested_filters
    ):
        currency_values = requested_filters["currency"]
        currency_values = (
            currency_values if isinstance(currency_values, list) else [currency_values]
        )
        if len(currency_values) != 1:
            raise QueryFailure(
                "CURRENCY_SCOPE_REQUIRED",
                "原币指标未按币种分组时只能限定一个币种。",
            )
    unit_policy = metric.get("unit_policy")
    requires_unit_scope = unit_policy == "group_or_filter" or (
        isinstance(unit_policy, Mapping)
        and unit_policy.get("mode") == "group_or_filter"
    )
    if requires_unit_scope and not (
        "unit" in requested_dimensions or "unit" in requested_filters
    ):
        raise QueryFailure("UNIT_SCOPE_REQUIRED", "数量指标必须按单位分组或限定单一单位。")
    if (
        requires_unit_scope
        and "unit" not in requested_dimensions
        and "unit" in requested_filters
    ):
        unit_values = requested_filters["unit"]
        unit_values = unit_values if isinstance(unit_values, list) else [unit_values]
        if len(unit_values) != 1:
            raise QueryFailure(
                "UNIT_SCOPE_REQUIRED",
                "数量指标未按单位分组时只能限定一个单位。",
            )

    join_states: dict[tuple[Any, ...], dict[str, Any]] = {}
    metric_join_alias: str | None = None
    metric_join_dataset: Mapping[str, Any] | None = None
    metric_join = metric.get("metric_join")
    if metric_join is not None:
        if not isinstance(metric_join, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "指标关联定义无效。")
        metric_join_alias, metric_join_dataset = _register_governed_join(
            metric_join,
            dataset,
            datasets_contract,
            join_states,
            purpose="metric",
        )
    resolved_dimensions: dict[str, tuple[str, Mapping[str, Any]]] = {}
    resolved_definitions: dict[str, Mapping[str, Any]] = {}
    warnings: list[str] = []
    for code in dimension_codes:
        definition = _metric_dimension_definition(dimensions, metric, code)
        resolved_definitions[code] = definition
        resolved_dimensions[code] = _governed_join(
            definition,
            table,
            dataset,
            datasets_contract,
            join_states,
            purpose=("filter" if code in requested_filters else "projection"),
        )
        note = definition.get("answer_note")
        if isinstance(note, str) and note not in warnings:
            warnings.append(note)

    select_columns: list[str] = []
    group_columns: list[str] = []
    output_names: list[str] = []
    for code in requested_dimensions:
        definition = resolved_definitions[code]
        alias, source_dataset = resolved_dimensions[code]
        source_allowed = {str(item) for item in source_dataset.get("allowed_columns") or []}
        source_blocked = _blocked_columns(datasets_contract, source_dataset)
        for column, output in _dimension_columns(definition):
            _approved_column(column, source_allowed, source_blocked)
            if output in output_names:
                raise QueryFailure("CONTRACT_UNAVAILABLE", "维度输出名称重复。")
            expression = _dimension_expression(definition, alias, column)
            select_columns.append(f"{expression} AS {_quote_identifier(output)}")
            group_columns.append(expression)
            output_names.append(output)

    time_bucket = request.get("time_bucket")
    if time_bucket is not None:
        if time_bucket not in {"day", "month"} or not isinstance(time_field, str):
            raise QueryFailure("INVALID_PLAN", "该指标不支持请求的时间分组。")
        if metric.get("time_granularity") == "month" and time_bucket != "month":
            raise QueryFailure("INVALID_PLAN", "月粒度指标只能按月分组。")
        _approved_column(time_field, base_allowed, base_blocked)
        if "period" in output_names:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "时间分组输出名称冲突。")
        qualified_time = _qualified_identifier("f", time_field)
        if time_bucket == "day":
            expression = f"DATE({qualified_time})"
        elif (
            metric.get("time_granularity") == "month"
            and _metric_time_value_format(metric) == "month"
        ):
            expression = qualified_time
        else:
            expression = f"DATE_FORMAT({qualified_time}, '{_MYSQL_MONTH_FORMAT}')"
        select_columns.append(f"{expression} AS {_quote_identifier('period')}")
        group_columns.append(expression)
        output_names.append("period")

    metric_sql = _metric_aggregation_sql(
        metric,
        dataset,
        datasets_contract,
        "f",
        joined_alias=metric_join_alias,
        joined_dataset=metric_join_dataset,
    )
    if sign == -1:
        metric_sql = f"-({metric_sql})"

    where_params: list[Any] = []
    where: list[str] = []
    system_filters: list[dict[str, Any]] = []
    inherited_filters: dict[str, dict[str, Any]] = {}
    snapshot_population_filters: list[tuple[str, Mapping[str, Any]]] = []
    dataset_required_filters = dataset.get("required_filters") or []
    if not isinstance(dataset_required_filters, list):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "数据集固定过滤定义无效。")
    for dataset_spec in dataset_required_filters:
        if not isinstance(dataset_spec, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "数据集固定过滤定义无效。")
        column = _approved_column(dataset_spec.get("column"), base_allowed, base_blocked)
        spec = {"op": str(dataset_spec.get("op", "eq")).lower(), "value": dataset_spec.get("value")}
        inherited_filters[column] = spec
        where.append(_filter_clause(column, spec, where_params, alias="f"))
        snapshot_population_filters.append((column, spec))
        system_filters.append(_system_filter_record(table, column, spec, "dataset_required"))
    for column, spec in (metric.get("required_filters") or {}).items():
        _approved_column(column, base_allowed, base_blocked)
        if not isinstance(spec, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "指标固定过滤定义无效。")
        normalized_spec = {"op": str(spec.get("op", "eq")).lower(), "value": spec.get("value")}
        if column in inherited_filters:
            if inherited_filters[column] == normalized_spec:
                continue
            if not _fixed_filter_refines(inherited_filters[column], normalized_spec):
                raise QueryFailure("CONTRACT_UNAVAILABLE", "指标固定过滤与数据集固定口径冲突。")
        where.append(_filter_clause(column, spec, where_params, alias="f"))
        system_filters.append(_system_filter_record(table, column, spec, "metric_required"))

    eligibility_evidence = None
    if metric_join_alias is not None and metric_join_dataset is not None:
        joined_allowed = {str(item) for item in metric_join_dataset.get("allowed_columns") or []}
        joined_blocked = _blocked_columns(datasets_contract, metric_join_dataset)
        joined_required_filters = metric_join.get("required_filters") or {}
        if not isinstance(joined_required_filters, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "指标关联过滤定义无效。")
        for column, spec in joined_required_filters.items():
            column = _approved_column(column, joined_allowed, joined_blocked)
            if not isinstance(spec, dict):
                raise QueryFailure("CONTRACT_UNAVAILABLE", "指标关联过滤定义无效。")
            where.append(_filter_clause(column, spec, where_params, alias=metric_join_alias))
            system_filters.append(
                _system_filter_record(
                    str(metric_join.get("dataset")), column, spec, "metric_join_required"
                )
            )
        condition = metric_join.get("condition")
        eligibility_nulls = []
        for column in metric_join.get("required_not_null") or []:
            column = _approved_column(column, joined_allowed, joined_blocked)
            qualified = _qualified_identifier(metric_join_alias, column)
            if isinstance(condition, dict) and condition.get("type") == "date_plus_days_before_today":
                eligibility_nulls.append(f"{qualified} IS NULL")
            else:
                where.append(f"{qualified} IS NOT NULL")
        missing_column = metric_join.get("required_missing")
        if missing_column is not None:
            missing_column = _approved_column(missing_column, joined_allowed, joined_blocked)
            where.append(f"{_qualified_identifier(metric_join_alias, missing_column)} IS NULL")
        condition = metric_join.get("condition")
        if condition is not None:
            if not isinstance(condition, dict) or condition.get("type") != "date_plus_days_before_today":
                raise QueryFailure("CONTRACT_UNAVAILABLE", "指标关联条件不受支持。")
            date_column = _approved_column(condition.get("date_column"), base_allowed, base_blocked)
            days_column = _approved_column(condition.get("days_column"), joined_allowed, joined_blocked)
            eligibility_evidence = (
                f"DATE_ADD({_qualified_identifier('f', date_column)}, "
                f"INTERVAL {_qualified_identifier(metric_join_alias, days_column)} DAY) < CURDATE()",
                f"{_qualified_identifier(metric_join_alias, days_column)} IS NULL",
                f"{_qualified_identifier('f', date_column)} IS NULL",
                eligibility_nulls,
            )

    time_policy = str(metric.get("time_policy") or "")
    current_snapshot_evidence = metric.get("current_snapshot_evidence")
    current_snapshot_evidence_supported = (
        time_policy == "current_snapshot"
        and (
            current_snapshot_evidence == _DATABASE_CURRENT_DATE_EVIDENCE
            or (
                current_snapshot_evidence == _DATABASE_QUERY_DATE_OBSERVATION
                and request.get("domain") == "inventory"
                and request.get("metric") == "current_inventory_amount_rmb"
            )
        )
    )
    if (
        current_snapshot_evidence is not None
        and not current_snapshot_evidence_supported
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "当前快照日期证据合同无效。",
        )
    time_range = request.get("time_range")
    applied_time: dict[str, Any] | None = None
    if isinstance(time_range, dict):
        start, end = time_range.get("start"), time_range.get("end")
        if not time_field or not isinstance(start, str) or not isinstance(end, str) or start >= end:
            raise QueryFailure("INVALID_PLAN", "时间范围无效或该指标不接受时间范围。")
        start, end = _metric_time_bounds(metric, start, end)
        start, end = _validate_time_bounds(
            start,
            end,
            _metric_time_value_format(metric),
            max_days=_max_metric_range_days(),
        )
        if time_policy in {"latest_snapshot", "latest_non_null_snapshot"}:
            try:
                snapshot_start = date.fromisoformat(
                    f"{start}-01" if _metric_time_value_format(metric) == "month" else start
                )
                snapshot_end = date.fromisoformat(
                    f"{end}-01" if _metric_time_value_format(metric) == "month" else end
                )
            except ValueError as exc:
                raise QueryFailure("INVALID_PLAN", "快照指标的显式期间无效。") from exc
            month_span = (
                (snapshot_end.year - snapshot_start.year) * 12
                + snapshot_end.month
                - snapshot_start.month
            )
            if (
                snapshot_start.day != 1
                or snapshot_end.day != 1
                or month_span != 1
            ):
                raise QueryFailure(
                    "INVALID_PLAN",
                    "快照指标一次只能选择一个自然月；多月变化请使用受治理的趋势指标。",
                )
        _approved_column(time_field, base_allowed, base_blocked)
        quoted_time = _qualified_identifier("f", time_field)
        where.extend([f"{quoted_time} >= %s", f"{quoted_time} < %s"])
        where_params.extend([start, end])
        applied_time = {"start": start, "end": end, "source": "explicit"}
    else:
        default_range = _default_time_range(time_policy, observed_on)
        if default_range and time_field:
            default_range = _metric_time_bounds(metric, default_range[0], default_range[1])
            default_range = _validate_time_bounds(
                default_range[0],
                default_range[1],
                _metric_time_value_format(metric),
                max_days=_max_metric_range_days(),
            )
            _approved_column(time_field, base_allowed, base_blocked)
            quoted_time = _qualified_identifier("f", time_field)
            where.extend([f"{quoted_time} >= %s", f"{quoted_time} < %s"])
            where_params.extend(default_range)
            applied_time = {"start": default_range[0], "end": default_range[1], "source": "default_current_month"}
        elif time_policy in {"latest_snapshot", "latest_non_null_snapshot"} and time_field:
            _approved_column(time_field, base_allowed, base_blocked)
            quoted_time = _qualified_identifier("f", time_field)
            snapshot_offset = request.get("_snapshot_offset_months", 0)
            if not isinstance(snapshot_offset, int) or not 0 <= snapshot_offset <= 24:
                raise QueryFailure("INVALID_PLAN", "快照比较月份偏移无效。")
            if snapshot_offset:
                if dataset.get("kind") != "monthly_snapshot":
                    raise QueryFailure("INVALID_PLAN", "该数据集不支持按月快照比较。")
                max_snapshot_sql = _latest_snapshot_resolver_sql(
                    table,
                    time_field,
                    snapshot_population_filters,
                    where_params,
                )
                where.append(
                    f"{quoted_time} = DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(({max_snapshot_sql}), '-01'), '{_MYSQL_DAY_FORMAT}'), "
                    f"INTERVAL %s MONTH), '{_MYSQL_MONTH_FORMAT}')"
                )
                where_params.append(snapshot_offset)
                applied_time = {"source": "latest_snapshot_offset", "months_before": snapshot_offset}
            elif time_policy == "latest_non_null_snapshot":
                required_measure = _approved_column(
                    metric.get("snapshot_required_non_null"), base_allowed, base_blocked
                )
                max_snapshot_sql = _latest_snapshot_resolver_sql(
                    table,
                    time_field,
                    snapshot_population_filters,
                    where_params,
                    required_non_null=required_measure,
                )
                where.append(
                    f"{quoted_time} = ({max_snapshot_sql})"
                )
                applied_time = {
                    "source": "latest_non_null_snapshot",
                    "required_measure": required_measure,
                }
            else:
                max_snapshot_sql = _latest_snapshot_resolver_sql(
                    table,
                    time_field,
                    snapshot_population_filters,
                    where_params,
                )
                where.append(f"{quoted_time} = ({max_snapshot_sql})")
                applied_time = {"source": "latest_snapshot"}
        elif time_policy == "current_snapshot":
            applied_time = {
                "source": "current_snapshot",
                **(
                    {"current_snapshot_evidence": current_snapshot_evidence}
                    if current_snapshot_evidence is not None
                    else {}
                ),
            }
        elif time_policy not in {"current_snapshot", ""}:
            raise QueryFailure("INVALID_PLAN", "该指标缺少必要时间范围。")

    applied_filters: dict[str, Any] = {}
    applied_filter_plan: list[dict[str, Any]] = []
    for code in sorted(requested_filters):
        value = requested_filters[code]
        definition = resolved_definitions[code]
        alias, source_dataset = resolved_dimensions[code]
        column, bound_value = _bound_entity_filter(request, code, definition, value)
        source_allowed = {str(item) for item in source_dataset.get("allowed_columns") or []}
        source_blocked = _blocked_columns(datasets_contract, source_dataset)
        _approved_column(column, source_allowed, source_blocked)
        quoted_filter = _dimension_expression(definition, alias, str(column))
        value = _normalized_dimension_value(definition, bound_value)
        if isinstance(value, list):
            if not value or len(value) > request_contract.MAX_FILTER_VALUES:
                raise QueryFailure("INVALID_PLAN", "过滤值列表无效。")
            where.append(f"{quoted_filter} IN ({', '.join(['%s'] * len(value))})")
            where_params.extend(value)
        else:
            where.append(f"{quoted_filter} = %s")
            where_params.append(value)
        applied_filters[code] = value
        source_table = table
        source_role = "fact"
        if alias != "f":
            source_state = next(
                (
                    state
                    for state in join_states.values()
                    if state.get("alias") == alias
                ),
                None,
            )
            if not isinstance(source_state, Mapping):
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "过滤条件的受控关联计划缺失。",
                )
            source_table = str(source_state["table"])
            source_role = "governed_join"
        applied_filter_plan.append(
            {
                "logical_dimension": str(code),
                "dataset": source_table,
                "sql_alias": alias,
                "source_role": source_role,
                "column": str(column),
                "operator": "in" if isinstance(value, list) else "eq",
                "normalized_value": value,
                "normalization": definition.get("normalization"),
                "purpose": "user_filter",
            }
        )

    missing_input = _metric_missing_input_sql(
        metric, dataset, datasets_contract, "f",
        joined_alias=metric_join_alias, joined_dataset=metric_join_dataset,
    )
    missing = f"COALESCE(SUM(CASE WHEN {missing_input} THEN 1 ELSE 0 END), 0)"
    known = f"COUNT(*) - ({missing})"
    evidence_columns = [
        *_integrity_columns(metric_sql, missing, known),
        f"COUNT(*) AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    if metric.get("include_known_subset") is True:
        if metric.get("aggregation") != "sum":
            raise QueryFailure("CONTRACT_UNAVAILABLE", "已知部分只适用于基础求和指标。")
        evidence_columns.append(
            f"CASE WHEN ({known}) > 0 THEN {metric_sql} ELSE NULL END AS known_subset_value"
        )
    if eligibility_evidence is not None:
        evidence_columns = _overdue_integrity_columns(
            metric, dataset, datasets_contract, metric_join_alias,
            metric_join_dataset, eligibility_evidence, missing_input, sign)
    currency_missing_input = _currency_missing_input_sql(
        metric,
        dataset,
        datasets_contract,
        "f",
    )
    if currency_missing_input is not None:
        unknown_count = (
            f"COALESCE(SUM(CASE WHEN {currency_missing_input} IS NULL "
            "THEN 1 ELSE 0 END), 0)"
        )
        evidence_columns.append(
            f"COALESCE(SUM(CASE WHEN {currency_missing_input} IS NULL "
            f"THEN 1 ELSE 0 END), 0) AS {_quote_identifier('currency_missing_rows')}"
        )
        evidence_columns.append(
            f"{unknown_count} AS {_quote_identifier('unclassified_source_row_count')}"
        )
        evidence_columns.append(
            f"CASE WHEN ({unknown_count}) = 0 THEN NULL "
            f"WHEN ({unknown_count}) = 1 THEN 'currency_unknown_source_value_not_comparable' "
            f"ELSE 'currency_unknown_source_range_not_comparable' END "
            f"AS {_quote_identifier('unclassified_amount_state')}"
        )
        source_value = _metric_unclassified_source_value_sql(
            metric,
            dataset,
            datasets_contract,
            "f",
            sign=sign,
        )
        if source_value is not None:
            unknown_min = (
                f"MIN(CASE WHEN {currency_missing_input} IS NULL "
                f"THEN {source_value} ELSE NULL END)"
            )
            unknown_max = (
                f"MAX(CASE WHEN {currency_missing_input} IS NULL "
                f"THEN {source_value} ELSE NULL END)"
            )
            evidence_columns.extend(
                [
                    f"CASE WHEN ({unknown_count}) = 1 THEN {unknown_min} ELSE NULL END "
                    f"AS {_quote_identifier('unclassified_source_amount')}",
                    f"{unknown_min} AS {_quote_identifier('unclassified_source_amount_min')}",
                    f"{unknown_max} AS {_quote_identifier('unclassified_source_amount_max')}",
                ]
            )
    if (
        isinstance(applied_time, Mapping)
        and applied_time.get("source") in _SNAPSHOT_TIME_SOURCES
        and isinstance(time_field, str)
    ):
        evidence_columns.append(
            f"MAX({_qualified_identifier('f', time_field)}) "
            f"AS {_quote_identifier(_INTERNAL_SNAPSHOT_MONTH)}"
        )
    if (
        isinstance(applied_time, Mapping)
        and applied_time.get("source") == "current_snapshot"
        and applied_time.get("current_snapshot_evidence")
        in {
            _DATABASE_CURRENT_DATE_EVIDENCE,
            _DATABASE_QUERY_DATE_OBSERVATION,
        }
    ):
        evidence_columns.append(
            f"CURDATE() AS {_quote_identifier(_INTERNAL_AS_OF_DATE)}"
        )
    select_sql = ", ".join(select_columns + evidence_columns)
    sql = f"SELECT {select_sql} FROM {_quote_table(table)} AS {_quote_identifier('f')}"
    join_params: list[Any] = []
    applied_joins: list[str] = []
    applied_join_plan: list[dict[str, Any]] = []
    for state in join_states.values():
        alias = str(state["alias"])
        target_dataset = state["dataset"]
        on = [
            f"{_qualified_identifier('f', str(local))} = "
            f"{_qualified_identifier(alias, str(remote))}"
            for local, remote in zip(state["locals"], state["remotes"])
        ]
        for spec in target_dataset.get("required_filters") or []:
            if not isinstance(spec, dict):
                raise QueryFailure("CONTRACT_UNAVAILABLE", "关联数据集过滤规则无效。")
            column = spec.get("column")
            target_allowed = {str(item) for item in target_dataset.get("allowed_columns") or []}
            _approved_column(column, target_allowed, _blocked_columns(datasets_contract, target_dataset))
            on.append(_filter_clause(column, spec, join_params, alias=alias))
            system_filters.append(
                _system_filter_record(
                    str(state["table"]), str(column), spec, "joined_dataset_required"
                )
            )
        sql += f" LEFT JOIN {_quote_table(str(state['table']))} AS {_quote_identifier(alias)} ON " + " AND ".join(on)
        applied_joins.append(str(state["table"]))
        applied_join_plan.append(
            {
                "target": str(state["table"]),
                "local_columns": list(state["locals"]),
                "remote_columns": list(state["remotes"]),
                "cardinality": state["cardinality"],
                "join_type": state["join_type"],
                "unique_target_key": state["unique_target_key"],
                "deduplication": state["deduplication"],
                "purposes": sorted(state["purposes"]),
            }
        )
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_columns:
        sql += " GROUP BY " + ", ".join(group_columns)
        if eligibility_evidence is not None:
            sql += f" HAVING {_quote_identifier(_INTERNAL_MATCH_COUNT)} > 0"
    return sql, [*join_params, *where_params], {
        "time_range": applied_time,
        "filters": applied_filters,
        "filter_plan": applied_filter_plan,
        "system_filters": system_filters,
        "dataset": table,
        "joins": applied_joins,
        "join_plan": applied_join_plan,
        "warnings": warnings,
        "dimension_outputs": output_names,
    }

def _build_comparison_metric_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    comparison = request.get("comparison")
    if not isinstance(comparison, dict) or request.get("time_bucket") is not None:
        raise QueryFailure("INVALID_PLAN", "期间比较不能与时间分组同时使用。")
    kind = comparison.get("kind")
    if kind not in contracts.metric_comparison_kinds(metric):
        raise QueryFailure("INVALID_PLAN", "该指标不支持请求中的比较类型。")
    current_request = dict(request)
    prior_request = dict(request)
    current_request.pop("comparison", None)
    prior_request.pop("comparison", None)
    comparison_alignment: dict[str, Any] | None = None
    if kind == PREVIOUS_PERIOD_COMPARISON:
        if set(comparison) != {"kind"}:
            raise QueryFailure(
                "INVALID_PLAN",
                "previous_period 比较只接受 kind。",
            )
        current_range = request.get("time_range")
        if not isinstance(current_range, dict):
            raise QueryFailure("INVALID_PLAN", "上期比较必须提供当前期间。")
        start, end = current_range.get("start"), current_range.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise QueryFailure("INVALID_PLAN", "上期比较时间范围无效。")
        prior_start, prior_end = _comparison_period(start, end)
        prior_request["time_range"] = {"start": prior_start, "end": prior_end}
    elif kind == YEAR_OVER_YEAR_COMPARISON:
        if (
            set(comparison) != {"kind", "coverage"}
            or comparison.get("coverage") != MATCHED_ELAPSED_COVERAGE
        ):
            raise QueryFailure(
                "INVALID_PLAN",
                "year_over_year 比较要求 coverage=matched_elapsed。",
            )
        current_range = request.get("time_range")
        if not isinstance(current_range, dict):
            raise QueryFailure("INVALID_PLAN", "同比必须提供当前期间。")
        start, end = current_range.get("start"), current_range.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise QueryFailure("INVALID_PLAN", "同比时间范围无效。")
        current_range, prior_range, comparison_alignment = (
            _year_over_year_matched_elapsed_ranges(
                start,
                end,
                observed_on or _business_today(),
            )
        )
        current_request["time_range"] = current_range
        prior_request["time_range"] = prior_range
    elif kind == SNAPSHOT_MONTHS_BEFORE_COMPARISON:
        if set(comparison) != {"kind", "months"}:
            raise QueryFailure(
                "INVALID_PLAN",
                "snapshot_months_before 比较必须且只能包含 kind 与 months。",
            )
        months = comparison.get("months")
        if (
            not isinstance(months, int)
            or isinstance(months, bool)
            or not 1 <= months <= 24
        ):
            raise QueryFailure("INVALID_PLAN", "快照比较必须提供 1 到 24 个月偏移。")
        if metric.get("time_policy") != "latest_snapshot" or request.get("time_range") is not None:
            raise QueryFailure("INVALID_PLAN", "该指标不支持最新快照月份比较。")
        current_request["_snapshot_offset_months"] = 0
        prior_request["_snapshot_offset_months"] = months
    else:
        raise QueryFailure("INVALID_PLAN", "比较类型不受支持。")

    if metric.get("ratio") is not None:
        current_sql, current_params, current_scope = _build_ratio_metric_core(
            current_request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
        prior_sql, prior_params, prior_scope = _build_ratio_metric_core(
            prior_request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
    elif metric.get("components") is not None:
        current_sql, current_params, current_scope = _build_composite_metric_core(
            current_request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
        prior_sql, prior_params, prior_scope = _build_composite_metric_core(
            prior_request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
    else:
        current_sql, current_params, current_scope = _build_metric_core(
            current_request,
            metric,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
        prior_sql, prior_params, prior_scope = _build_metric_core(
            prior_request,
            metric,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
    dimensions = current_scope["dimension_outputs"]
    if prior_scope["dimension_outputs"] != dimensions:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比较两期的维度输出不一致。")
    if dimensions:
        key_list = ", ".join(_quote_identifier(item) for item in dimensions)
        keys_sql = f"SELECT {key_list} FROM current_period UNION SELECT {key_list} FROM comparison_period"
        current_join = " AND ".join(
            f"k.{_quote_identifier(item)} <=> c.{_quote_identifier(item)}" for item in dimensions
        )
        prior_join = " AND ".join(
            f"k.{_quote_identifier(item)} <=> p.{_quote_identifier(item)}" for item in dimensions
        )
        output_dimensions = [
            f"COALESCE(c.{_quote_identifier(item)}, p.{_quote_identifier(item)}) AS {_quote_identifier(item)}"
            for item in dimensions
        ]
        ctes = (
            f"WITH current_period AS ({current_sql}), "
            f"comparison_period AS ({prior_sql}), keys_all AS ({keys_sql}) "
        )
        select_from = (
            "FROM keys_all AS k "
            f"LEFT JOIN current_period AS c ON {current_join} "
            f"LEFT JOIN comparison_period AS p ON {prior_join}"
        )
    else:
        output_dimensions = []
        ctes = f"WITH current_period AS ({current_sql}), comparison_period AS ({prior_sql}) "
        select_from = "FROM current_period AS c CROSS JOIN comparison_period AS p"
    if metric.get("ratio") is not None:
        comparison_values = [
            "c.metric_value AS metric_value",
            "p.metric_value AS comparison_value",
            "CASE WHEN c.metric_value IS NOT NULL AND p.metric_value IS NOT NULL "
            "THEN c.metric_value - p.metric_value ELSE NULL END AS delta_value",
            "CASE WHEN c.metric_value IS NOT NULL AND p.metric_value > 0 THEN "
            "(c.metric_value - p.metric_value) / p.metric_value ELSE NULL END AS change_rate",
        ]
    else:
        comparison_values = [
            "CASE WHEN COALESCE(c.missing_value_count, 0) > 0 THEN NULL ELSE COALESCE(c.metric_value, 0) END AS metric_value",
            "CASE WHEN COALESCE(p.missing_value_count, 0) > 0 THEN NULL ELSE COALESCE(p.metric_value, 0) END AS comparison_value",
            "CASE WHEN COALESCE(c.missing_value_count, 0) + COALESCE(p.missing_value_count, 0) > 0 THEN NULL ELSE COALESCE(c.metric_value, 0) - COALESCE(p.metric_value, 0) END AS delta_value",
            "CASE WHEN COALESCE(c.missing_value_count, 0) + COALESCE(p.missing_value_count, 0) = 0 AND COALESCE(p.metric_value, 0) > 0 THEN "
            "(COALESCE(c.metric_value, 0) - p.metric_value) / p.metric_value ELSE NULL END AS change_rate",
        ]
    select = [
        *output_dimensions,
        *comparison_values,
        f"COALESCE(c.{_INTERNAL_MATCH_COUNT}, 0) + COALESCE(p.{_INTERNAL_MATCH_COUNT}, 0) "
        f"AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    select.extend(_integrity_columns(
        "NULL", "COALESCE(c.missing_value_count, 0) + COALESCE(p.missing_value_count, 0)",
        "COALESCE(c.known_value_count, 0) + COALESCE(p.known_value_count, 0)",
    )[1:])
    change_decomposition = metric.get("change_decomposition")
    requires_completeness_proof = (
        isinstance(change_decomposition, Mapping)
        and change_decomposition.get("mode") == "additive_partition"
    )
    if requires_completeness_proof:
        select.extend(
            [
                "COALESCE(c.missing_value_count, 0) AS current_missing_value_count",
                "COALESCE(c.known_value_count, 0) AS current_known_value_count",
                "COALESCE(p.missing_value_count, 0) AS comparison_missing_value_count",
                "COALESCE(p.known_value_count, 0) AS comparison_known_value_count",
                "COALESCE(c.metric_data_state, 'not_present') AS current_metric_data_state",
                "COALESCE(p.metric_data_state, 'not_present') AS comparison_metric_data_state",
            ]
        )
    current_time = current_scope.get("time_range")
    comparison_time = prior_scope.get("time_range")
    if (
        isinstance(current_time, Mapping)
        and current_time.get("source") in _SNAPSHOT_TIME_SOURCES
    ):
        select.append(
            f"c.{_quote_identifier(_INTERNAL_SNAPSHOT_MONTH)} "
            f"AS {_quote_identifier(_INTERNAL_SNAPSHOT_MONTH)}"
        )
    if (
        isinstance(comparison_time, Mapping)
        and comparison_time.get("source") in _SNAPSHOT_TIME_SOURCES
    ):
        select.append(
            f"p.{_quote_identifier(_INTERNAL_SNAPSHOT_MONTH)} "
            f"AS {_quote_identifier(_INTERNAL_COMPARISON_SNAPSHOT_MONTH)}"
        )
    row_select = ", ".join(select)
    row_source_sql = f"SELECT {row_select} {select_from}"
    embedded_partition_proof = dimensions and isinstance(
        request.get("decomposition_of_request_id"), str
    )
    if embedded_partition_proof:
        proof_select = (
            "SUM(partition_rows.metric_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_CURRENT)}, "
            "SUM(partition_rows.comparison_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_COMPARISON)}, "
            "SUM(partition_rows.delta_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_DELTA)}, "
            "COUNT(*) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_ROW_COUNT)}"
        )
        for name, expression in evidence.CHANGE_DISTRIBUTION_SQL.items():
            proof_select += f", {expression} OVER () AS `__distribution_{name}`"
        if requires_completeness_proof:
            proof_select += (
                ", SUM(partition_rows.current_missing_value_count) OVER () AS "
                f"{_quote_identifier(_INTERNAL_PARTITION_CURRENT_MISSING)}, "
                "SUM(partition_rows.current_known_value_count) OVER () AS "
                f"{_quote_identifier(_INTERNAL_PARTITION_CURRENT_KNOWN)}, "
                "SUM(partition_rows.comparison_missing_value_count) OVER () AS "
                f"{_quote_identifier(_INTERNAL_PARTITION_COMPARISON_MISSING)}, "
                "SUM(partition_rows.comparison_known_value_count) OVER () AS "
                f"{_quote_identifier(_INTERNAL_PARTITION_COMPARISON_KNOWN)}"
            )
        sql = (
            f"{ctes}SELECT partition_rows.*, "
            f"{proof_select} "
            f"FROM ({row_source_sql}) AS partition_rows"
        )
    else:
        sql = f"{ctes}{row_source_sql}"
    order_by = request.get("order_by") or {"field": "delta_value", "direction": "desc"}
    if not isinstance(order_by, dict):
        raise QueryFailure("INVALID_PLAN", "比较排序定义无效。")
    field = order_by.get("field")
    direction = str(order_by.get("direction", "desc")).upper()
    if field not in {
        "metric_value",
        "comparison_value",
        "delta_value",
        "absolute_delta_value",
        "change_rate",
        *dimensions,
    } or direction not in {"ASC", "DESC"}:
        raise QueryFailure("INVALID_PLAN", "比较排序字段不受支持。")
    if dimensions:
        order_expression = "ABS(`delta_value`)" if field == "absolute_delta_value" else _quote_identifier(str(field))
        sql += f" ORDER BY {order_expression} {direction}"
    sql += " LIMIT %s"
    warnings = list(current_scope.get("warnings") or [])
    for warning in prior_scope.get("warnings") or []:
        if warning not in warnings:
            warnings.append(warning)
    note = metric.get("answer_note")
    if isinstance(note, str) and note not in warnings:
        warnings.append(note)
    system_filters = _merge_records(
        current_scope.get("system_filters") or [],
        prior_scope.get("system_filters") or [],
    )
    join_plan = _merge_records(
        current_scope.get("join_plan") or [],
        prior_scope.get("join_plan") or [],
    )
    filter_plan = _merge_records(
        current_scope.get("filter_plan") or [],
        prior_scope.get("filter_plan") or [],
    )
    scope = {
        "metric": request.get("metric"),
        "dataset": current_scope.get("dataset"),
        "source_datasets": current_scope.get("source_datasets") or [current_scope.get("dataset")],
        "time_range": {
            "current": current_scope.get("time_range"),
            "comparison": prior_scope.get("time_range"),
        },
        "filters": current_scope.get("filters") or {},
        "filter_plan": filter_plan,
        "system_filters": system_filters,
        "join_plan": join_plan,
        "warnings": warnings,
        "dimension_outputs": dimensions,
    }
    if comparison_alignment is not None:
        scope["time_range"]["comparison_alignment"] = comparison_alignment
    if embedded_partition_proof:
        scope["embedded_complete_partition_proof"] = {
            "version": "same-statement-window-partition-proof/v1",
            "requires_completeness_proof": requires_completeness_proof,
        }
    return sql, [*current_params, *prior_params, limit + 1], scope

def _metric_order_clause(request: Mapping[str, Any], dimension_outputs: Sequence[str]) -> str:
    if not dimension_outputs:
        if request.get("order_by") is not None:
            raise QueryFailure("INVALID_PLAN", "无分组指标不接受 order_by。")
        return ""
    order_by = request.get("order_by")
    if order_by is None:
        if "period" in dimension_outputs:
            return f" ORDER BY {_quote_identifier('period')} ASC"
        return " ORDER BY metric_value DESC"
    if not isinstance(order_by, dict):
        raise QueryFailure("INVALID_PLAN", "指标排序定义无效。")
    field = order_by.get("field")
    direction = str(order_by.get("direction") or "").lower()
    allowed_fields = {"metric_value", *dimension_outputs}
    if field not in allowed_fields or direction not in {"asc", "desc"}:
        raise QueryFailure("INVALID_PLAN", "指标排序字段或方向不受支持。")
    return f" ORDER BY {_quote_identifier(str(field))} {direction.upper()}"

def _build_metric_query(
    request: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    metric_code = request.get("metric")
    metrics = semantics.get("metrics")
    if not isinstance(metrics, dict) or metric_code not in metrics:
        raise QueryFailure("UNSUPPORTED_METRIC", "该指标尚未进入受控指标定义。")
    metric = metrics[metric_code]
    if not isinstance(metric, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标定义格式无效。")
    handler = analytical_handlers.get_handler(metric.get("query_kind"))
    if (request.get("baseline_week") is not None or request.get("movement_state") is not None) and not (handler and handler.baseline_parameters):
        raise QueryFailure("INVALID_PLAN", "该指标不接受基线周或变化状态参数。")
    if request.get("pattern_time_basis") is not None and metric.get("query_kind") != "pattern_matching":
        raise QueryFailure("INVALID_PLAN", "该指标不接受找版时间口径参数。")
    inventory_scope_filters = metric.get("inventory_scope_filters")
    requested_inventory_scope = request.get("inventory_scope")
    applied_inventory_scope: str | None = None
    if inventory_scope_filters is not None:
        if not isinstance(inventory_scope_filters, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "库存范围定义无效。")
        applied_inventory_scope = str(
            requested_inventory_scope or metric.get("default_inventory_scope") or "total"
        )
        selected_scope_filters = inventory_scope_filters.get(applied_inventory_scope)
        if not isinstance(selected_scope_filters, dict):
            raise QueryFailure("INVALID_PLAN", "该库存指标不支持请求中的库存范围。")
        metric = dict(metric)
        fixed_filters = metric.get("required_filters") or {}
        if not isinstance(fixed_filters, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "指标固定过滤定义无效。")
        overlap = set(fixed_filters).intersection(selected_scope_filters)
        if any(fixed_filters[key] != selected_scope_filters[key] for key in overlap):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "库存范围与指标固定过滤冲突。")
        metric["required_filters"] = {**fixed_filters, **selected_scope_filters}
    elif requested_inventory_scope is not None:
        raise QueryFailure("INVALID_PLAN", "该库存指标不接受 inventory_scope。")
    order_by = request.get("order_by")
    if isinstance(order_by, dict) and order_by.get("field") == metric_code:
        request = {
            **request,
            "order_by": {**order_by, "field": "metric_value"},
        }
    _ensure_metric_available(metric)
    _ensure_metric_tree_available(metric_code, semantics)
    requested_dimensions = request.get("dimensions") or []
    if (
        not isinstance(requested_dimensions, list)
        or len(requested_dimensions) > _max_group_dimensions(metric)
    ):
        raise QueryFailure(
            "INVALID_PLAN",
            "请求的分组维度数量超过该指标发布的上限。",
        )
    _validate_governed_request_time_range(request)
    attribution_mode = request.get("attribution_mode")
    required_mode = metric.get("required_attribution_mode")
    allowed_modes = metric.get("allowed_attribution_modes")
    if required_mode is not None and attribution_mode != required_mode:
        raise QueryFailure("ATTRIBUTION_MODE_MISMATCH", "该指标与请求的归属账本不一致。")
    if allowed_modes is not None:
        if not isinstance(allowed_modes, list) or attribution_mode not in set(allowed_modes):
            raise QueryFailure("ATTRIBUTION_MODE_MISMATCH", "该指标不支持请求的归属账本。")

    if metric.get("query_kind") is not None:
        if request.get("comparison") is not None:
            raise QueryFailure("INVALID_PLAN", "该分析指标暂不接受通用比较参数。")
        time_bucket = request.get("time_bucket")
        if time_bucket is not None and time_bucket not in capability_contract.analytical_time_buckets(metric):
            raise QueryFailure("INVALID_PLAN", "该分析指标不支持请求中的时间分组参数。",
                path="time_bucket", hint="核对该指标 allowed_time_buckets；空列表表示不接受 time_bucket。这次错误仅标识该字段；其他参数是否有效仍须分别验证。")
        try:
            sql, params, scope = build_analytical_metric_query(
                request,
                metric,
                datasets_contract,
                semantics,
                limit,
                observed_on=observed_on,
            )
            scope["inventory_scope"] = applied_inventory_scope
            return sql, params, scope
        except AnalysisQueryError as exc:
            raise QueryFailure(exc.code, exc.message, path=exc.path, hint=exc.hint) from exc

    if request.get("comparison") is not None:
        return _build_comparison_metric_query(
            request,
            metric,
            datasets_contract,
            semantics,
            limit,
            observed_on=observed_on,
        )

    ratio = metric.get("ratio")
    components = metric.get("components")
    if ratio is None and components is None:
        sql, params, scope = _build_metric_core(
            request,
            metric,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
        note = metric.get("answer_note")
        if isinstance(note, str) and note not in scope["warnings"]:
            scope["warnings"].append(note)
        sql += _metric_order_clause(request, scope["dimension_outputs"])
        sql += " LIMIT %s"
        params.append(limit + 1)
        scope.update({
            "metric": metric_code,
            "source_datasets": [scope["dataset"]],
            "inventory_scope": applied_inventory_scope,
        })
        return sql, params, scope

    if ratio is not None:
        sql, params, scope = _build_ratio_metric_core(
            request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
    else:
        sql, params, scope = _build_composite_metric_core(
            request,
            metric,
            datasets_contract,
            semantics,
            observed_on=observed_on,
        )
    sql += _metric_order_clause(request, scope["dimension_outputs"])
    sql += " LIMIT %s"
    params.append(limit + 1)
    scope["inventory_scope"] = applied_inventory_scope
    return sql, params, scope

def _build_composite_metric_core(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    metric_code = request.get("metric")
    metrics = semantics.get("metrics")
    components = metric.get("components")
    if not isinstance(metrics, dict) or not isinstance(components, list) or not 2 <= len(components) <= 5:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标必须包含两个到五个组成指标。")
    core_sql: list[str] = []
    params: list[Any] = []
    scopes: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标组成定义无效。")
        component_code = component.get("metric")
        component_metric = metrics.get(component_code)
        if not isinstance(component_metric, dict) or component_metric.get("components") is not None:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标引用了无效或嵌套指标。")
        _ensure_metric_available(component_metric)
        component_metric = dict(component_metric)
        inherited_overrides = metric.get("dimension_overrides") or {}
        if not isinstance(inherited_overrides, dict):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标维度覆盖定义无效。")
        if inherited_overrides:
            component_overrides = component_metric.get("dimension_overrides") or {}
            if not isinstance(component_overrides, dict):
                raise QueryFailure("CONTRACT_UNAVAILABLE", "组成指标维度覆盖定义无效。")
            component_metric["dimension_overrides"] = {
                **component_overrides,
                **inherited_overrides,
            }
        sql, component_params, scope = _build_metric_core(
            request,
            component_metric,
            metric,
            datasets_contract,
            semantics,
            sign=component.get("sign"),
            observed_on=observed_on,
        )
        core_sql.append(sql)
        params.extend(component_params)
        scopes.append(scope)

    dimension_outputs = scopes[0]["dimension_outputs"]
    if any(scope["dimension_outputs"] != dimension_outputs for scope in scopes[1:]):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标各组成部分的维度输出不一致。")
    applied_time = scopes[0]["time_range"]
    if any(scope["time_range"] != applied_time for scope in scopes[1:]):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "复合指标各组成部分的时间范围不一致。")

    outer_dimensions = [
        f"{_qualified_identifier('u', output)} AS {_quote_identifier(output)}"
        for output in dimension_outputs
    ]
    select_columns = outer_dimensions + [
        *_integrity_columns(
            f"COALESCE(SUM({_qualified_identifier('u', 'metric_value')}), 0)",
            "COALESCE(SUM(u.missing_value_count), 0)",
            "COALESCE(SUM(u.known_value_count), 0)",
        ),
        f"COALESCE(SUM({_qualified_identifier('u', _INTERNAL_MATCH_COUNT)}), 0) "
        f"AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    currency_policy = metric.get("currency_policy")
    if (
        isinstance(currency_policy, Mapping)
        and currency_policy.get("mode") == "original_currency"
        and currency_policy.get("require_filter_or_group") is True
    ):
        unknown_count = (
            "COALESCE(SUM(u.unclassified_source_row_count), 0)"
        )
        unknown_min = "MIN(u.unclassified_source_amount_min)"
        unknown_max = "MAX(u.unclassified_source_amount_max)"
        select_columns.extend(
            [
                f"{unknown_count} AS {_quote_identifier('currency_missing_rows')}",
                f"{unknown_count} AS {_quote_identifier('unclassified_source_row_count')}",
                f"CASE WHEN ({unknown_count}) = 0 THEN NULL "
                f"WHEN ({unknown_count}) = 1 THEN 'currency_unknown_source_value_not_comparable' "
                f"ELSE 'currency_unknown_source_range_not_comparable' END "
                f"AS {_quote_identifier('unclassified_amount_state')}",
                f"CASE WHEN ({unknown_count}) = 1 THEN {unknown_min} ELSE NULL END "
                f"AS {_quote_identifier('unclassified_source_amount')}",
                f"{unknown_min} AS {_quote_identifier('unclassified_source_amount_min')}",
                f"{unknown_max} AS {_quote_identifier('unclassified_source_amount_max')}",
            ]
        )
    select_sql = ", ".join(select_columns)
    sql = f"SELECT {select_sql} FROM ({' UNION ALL '.join(core_sql)}) AS {_quote_identifier('u')}"
    if dimension_outputs:
        sql += " GROUP BY " + ", ".join(
            _qualified_identifier("u", output) for output in dimension_outputs
        )
    warnings: list[str] = []
    joins: list[str] = []
    join_plan: list[dict[str, Any]] = []
    source_datasets: list[str] = []
    system_filters: list[dict[str, Any]] = []
    filter_plan: list[dict[str, Any]] = []
    for scope in scopes:
        for warning in scope["warnings"]:
            if warning not in warnings:
                warnings.append(warning)
        for join in scope["joins"]:
            if join not in joins:
                joins.append(join)
        if scope["dataset"] not in source_datasets:
            source_datasets.append(scope["dataset"])
        system_filters = _merge_records(system_filters, scope.get("system_filters") or [])
        filter_plan = _merge_records(filter_plan, scope.get("filter_plan") or [])
        join_plan = _merge_records(join_plan, scope.get("join_plan") or [])
    note = metric.get("answer_note")
    if isinstance(note, str) and note not in warnings:
        warnings.append(note)
    return sql, params, {
        "metric": metric_code,
        "time_range": applied_time,
        "filters": scopes[0]["filters"],
        "filter_plan": filter_plan,
        "system_filters": system_filters,
        "dataset": None,
        "source_datasets": source_datasets,
        "joins": joins,
        "join_plan": join_plan,
        "warnings": warnings,
        "dimension_outputs": dimension_outputs,
    }

def _build_ratio_metric_core(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    metric_code = request.get("metric")
    metrics = semantics.get("metrics")
    ratio = metric.get("ratio")
    if not isinstance(metrics, dict) or not isinstance(ratio, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例指标定义无效。")
    numerator_code = ratio.get("numerator")
    denominator_code = ratio.get("denominator")
    numerator_metric = metrics.get(numerator_code)
    denominator_metric = metrics.get(denominator_code)
    if (
        not isinstance(numerator_metric, dict)
        or not isinstance(denominator_metric, dict)
        or numerator_metric.get("components") is not None
        or denominator_metric.get("components") is not None
        or numerator_metric.get("ratio") is not None
        or denominator_metric.get("ratio") is not None
    ):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例指标只能引用两个基础指标。")
    _ensure_metric_available(numerator_metric)
    _ensure_metric_available(denominator_metric)
    inherited_overrides = metric.get("dimension_overrides") or {}
    if not isinstance(inherited_overrides, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例指标维度覆盖定义无效。")
    if inherited_overrides:
        numerator_metric = dict(numerator_metric)
        denominator_metric = dict(denominator_metric)
        for base_metric in (numerator_metric, denominator_metric):
            base_overrides = base_metric.get("dimension_overrides") or {}
            if not isinstance(base_overrides, dict):
                raise QueryFailure("CONTRACT_UNAVAILABLE", "比例组成指标维度覆盖定义无效。")
            base_metric["dimension_overrides"] = {**base_overrides, **inherited_overrides}

    numerator_sql, numerator_params, numerator_scope = _build_metric_core(
        request,
        numerator_metric,
        metric,
        datasets_contract,
        semantics,
        observed_on=observed_on,
    )
    denominator_sql, denominator_params, denominator_scope = _build_metric_core(
        request,
        denominator_metric,
        metric,
        datasets_contract,
        semantics,
        observed_on=observed_on,
    )
    dimensions = numerator_scope["dimension_outputs"]
    if denominator_scope["dimension_outputs"] != dimensions:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例指标分子分母维度输出不一致。")
    if denominator_scope["time_range"] != numerator_scope["time_range"]:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例指标分子分母时间范围不一致。")

    if dimensions:
        key_list = ", ".join(_quote_identifier(item) for item in dimensions)
        keys_sql = f"SELECT {key_list} FROM numerator UNION SELECT {key_list} FROM denominator"
        numerator_join = " AND ".join(
            f"k.{_quote_identifier(item)} <=> n.{_quote_identifier(item)}" for item in dimensions
        )
        denominator_join = " AND ".join(
            f"k.{_quote_identifier(item)} <=> d.{_quote_identifier(item)}" for item in dimensions
        )
        output_dimensions = [
            f"COALESCE(n.{_quote_identifier(item)}, d.{_quote_identifier(item)}) AS {_quote_identifier(item)}"
            for item in dimensions
        ]
        from_sql = (
            f"keys_all AS ({keys_sql}) SELECT {{select}} FROM keys_all AS k "
            f"LEFT JOIN numerator AS n ON {numerator_join} "
            f"LEFT JOIN denominator AS d ON {denominator_join}"
        )
        ctes = f"WITH numerator AS ({numerator_sql}), denominator AS ({denominator_sql}), "
    else:
        output_dimensions = []
        from_sql = "SELECT {select} FROM numerator AS n CROSS JOIN denominator AS d"
        ctes = f"WITH numerator AS ({numerator_sql}), denominator AS ({denominator_sql}) "

    denominator_policy = ratio.get("denominator_policy", "positive")
    if denominator_policy not in {"positive", "nonzero"}:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "比例分母政策无效。")
    denominator_operator = "<>" if denominator_policy == "nonzero" else ">"
    numerator_value = "CASE WHEN COALESCE(n.missing_value_count, 0) > 0 THEN NULL ELSE COALESCE(n.metric_value, 0) END"
    denominator_value = "CASE WHEN COALESCE(d.missing_value_count, 0) > 0 THEN NULL ELSE COALESCE(d.metric_value, 0) END"
    select = [
        *output_dimensions,
        *_integrity_columns(
            f"CASE WHEN ({denominator_value}) {denominator_operator} 0 THEN ({numerator_value}) / d.metric_value ELSE NULL END",
            "COALESCE(n.missing_value_count, 0) + COALESCE(d.missing_value_count, 0)",
            "COALESCE(n.known_value_count, 0) + COALESCE(d.known_value_count, 0)",
        ),
        f"{numerator_value} AS numerator_value",
        f"{denominator_value} AS denominator_value",
        f"COALESCE(n.{_INTERNAL_MATCH_COUNT}, 0) + COALESCE(d.{_INTERNAL_MATCH_COUNT}, 0) "
        f"AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    sql = ctes + from_sql.format(select=", ".join(select))
    warnings: list[str] = []
    joins: list[str] = []
    source_datasets: list[str] = []
    system_filters: list[dict[str, Any]] = []
    filter_plan: list[dict[str, Any]] = []
    join_plan: list[dict[str, Any]] = []
    for scope in (numerator_scope, denominator_scope):
        for warning in scope.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
        for join in scope.get("joins") or []:
            if join not in joins:
                joins.append(join)
        dataset = scope.get("dataset")
        if isinstance(dataset, str) and dataset not in source_datasets:
            source_datasets.append(dataset)
        system_filters = _merge_records(system_filters, scope.get("system_filters") or [])
        filter_plan = _merge_records(filter_plan, scope.get("filter_plan") or [])
        join_plan = _merge_records(join_plan, scope.get("join_plan") or [])
    note = metric.get("answer_note")
    if isinstance(note, str) and note not in warnings:
        warnings.append(note)
    return sql, [*numerator_params, *denominator_params], {
        "metric": metric_code,
        "time_range": numerator_scope["time_range"],
        "filters": numerator_scope["filters"],
        "filter_plan": filter_plan,
        "system_filters": system_filters,
        "join_plan": join_plan,
        "dataset": None,
        "source_datasets": source_datasets,
        "joins": joins,
        "warnings": warnings,
        "dimension_outputs": dimensions,
    }
