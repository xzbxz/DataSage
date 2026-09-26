"""Validate public query branches without importing execution or operator code.

Metric semantics remain in the existing contracts. This module owns request
field/type checks and translates their failures into the shared query error.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

from . import capability_contract, contract_store, request_contract
from .capability_contract import (
    CapabilityContractError,
    MATCHED_ELAPSED_COVERAGE,
    PREVIOUS_PERIOD_COMPARISON,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
    SUPPORTED_DOMAINS,
    YEAR_OVER_YEAR_COMPARISON,
    validate_request_field_contract,
)
from .query_errors import QueryFailure


_DOMAINS = set(SUPPORTED_DOMAINS)

_COMMON_REQUEST_FIELDS = {
    "request_id",
    "domain",
    "time_range",
    "calendar_month",
    "order_by",
    "limit",
}

_METRIC_REQUEST_FIELDS = _COMMON_REQUEST_FIELDS | {
    "pattern_time_basis",
    "period_summary",
    "baseline_week",
    "movement_state",
    "metric",
    "dimensions",
    "metric_filters",
    "time_bucket",
    "comparison",
    "decomposition_of_request_id",
    "complete_change_decomposition",
    "complete_target_gap_decomposition",
    "_target_gap_of_request_id",
    "attribution_mode",
    "delivery_scope",
    "inventory_scope",
    "currency_basis",
}

def _target_gap_contract() -> capability_contract.TargetGapContract:
    """Read the single typed target-gap contract at the query boundary."""

    try:
        return contract_store.read_target_gap_contract()
    except (
        contract_store.ContractStoreError,
        capability_contract.CapabilityContractError,
    ) as exc:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "目标差距分解合同无效。",
            stage="contract_load",
        ) from exc

def _calendar_month_time_range(value: Any) -> dict[str, str]:
    """Expand a typed calendar month into canonical half-open day bounds."""

    try:
        return capability_contract._calendar_month_time_range(value)
    except ValueError as exc:
        raise QueryFailure(
            "INVALID_INPUT",
            "calendar_month must use a zero-padded YYYY-MM value.",
        ) from exc
    except OverflowError as exc:
        raise QueryFailure(
            "INVALID_INPUT",
            "calendar_month contains an invalid or unsupported year-month.",
        ) from exc

def _validate_request(
    request: Any,
    *,
    request_path: str | None = None,
) -> dict[str, Any]:
    def field_path(field: str | None = None) -> str | None:
        if request_path is None:
            return None
        return request_path if field is None else f"{request_path}.{field}"

    if not isinstance(request, dict):
        raise QueryFailure(
            "INVALID_INPUT",
            "每个查询请求必须是对象。",
            path=field_path(),
            hint="Replace this value with a request object.",
        )
    request = dict(request)
    request_id = request.get("request_id")
    domain = request.get("domain")
    legacy_mode = request.get("mode")
    if not request_contract.valid_string(request_id, request_contract.REQUEST_ID):
        raise QueryFailure(
            "INVALID_INPUT",
            "request_id 无效。",
            path=field_path("request_id"),
            hint="Use a unique non-blank request_id of at most 64 characters.",
        )
    if not isinstance(domain, str) or domain not in _DOMAINS:
        raise QueryFailure(
            "INVALID_INPUT",
            "业务域不受支持。",
            path=field_path("domain"),
            hint=f"Use one of: {', '.join(SUPPORTED_DOMAINS)}.",
        )
    if legacy_mode == "dataset":
        raise QueryFailure(
            "DETAIL_CONTRACT_UNAVAILABLE",
            "当前发布版本尚未开放语义明细合同，请使用已登记指标和维度。",
        )
    if legacy_mode is not None and legacy_mode != "metric":
        raise QueryFailure(
            "INVALID_INPUT",
            "查询模式不受支持。",
            path=field_path("mode"),
            hint="Use mode='metric'.",
        )
    request.pop("purpose", None)
    request.pop("mode", None)
    unexpected_fields = sorted(set(request) - _METRIC_REQUEST_FIELDS)
    if unexpected_fields:
        unexpected = unexpected_fields[0]
        raise QueryFailure(
            "INVALID_INPUT",
            "请求包含当前模式不接受的字段。",
            path=field_path(unexpected),
            hint=(
                "Move calculations to the top level beside requests."
                if unexpected == "calculations"
                else f"Remove unsupported request field '{unexpected}'."
            ),
        )
    request["mode"] = "metric"
    if "currency_basis" in request:
        from .currency_basis import BASIS_CHOICES
        basis = request["currency_basis"]
        if not isinstance(basis, str) or basis not in BASIS_CHOICES:
            raise QueryFailure("INVALID_INPUT", "currency_basis must be auto, rmb or original.", path=field_path("currency_basis"))
    try:
        validate_request_field_contract(request)
    except CapabilityContractError as exc:
        raise QueryFailure(exc.code, exc.message) from exc
    if not request_contract.valid_string(
        request.get("metric"), request_contract.METRIC_CODE
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            "指标模式缺少 metric。",
            path=field_path("metric"),
            hint="Use an exact metric code returned by datasage_catalog.",
        )
    dimensions = request.get("dimensions")
    if dimensions is not None and (
        not isinstance(dimensions, list)
        or len(dimensions) > request_contract.MAX_GROUP_DIMENSIONS
        or any(
            not request_contract.valid_string(
                dimension, request_contract.DIMENSION_CODE
            )
            for dimension in dimensions
        )
        or len(dimensions) != len(set(dimensions))
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            "维度列表无效。",
            path=field_path("dimensions"),
            hint=(
                f"Use at most {request_contract.MAX_GROUP_DIMENSIONS} unique governed dimension codes."
            ),
        )
    period_summary = request.get("period_summary")
    if period_summary is not None:
        if (not isinstance(period_summary, Mapping) or set(period_summary) != {"field", "periods"}
            or period_summary.get("field") not in {"metric_value", "target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb"}
            or request.get("time_bucket") != "month" or request.get("dimensions") or request.get("comparison") is not None):
            raise QueryFailure("INVALID_INPUT", "期间合计需要无实体分组的按月序列及明确的可加字段。")
        periods = period_summary.get("periods")
        if (not isinstance(periods, list) or not 1 <= len(periods) <= 36
            or any(not isinstance(p, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}", p) is None for p in periods)
            or len(set(periods)) != len(periods)):
            raise QueryFailure("INVALID_INPUT", "期间合计的月份列表无效。")
        for period in periods:
            _calendar_month_time_range(period)
    order_by = request.get("order_by")
    if order_by is not None:
        supplied_fields = set(order_by) if isinstance(order_by, Mapping) else set()
        if (
            not isinstance(order_by, Mapping)
            or supplied_fields != set(request_contract.ORDER_BY_FIELDS)
            or not isinstance(order_by.get("field"), str)
            or re.fullmatch(
                request_contract.ORDER_BY_FIELD_PATTERN,
                order_by.get("field", ""),
            )
            is None
            or order_by.get("direction") not in request_contract.ORDER_BY_DIRECTIONS
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "order_by 必须且只能包含有效的 field 和 direction。",
                path=field_path("order_by"),
                hint="Use exactly {'field': '<governed output>', 'direction': 'asc|desc'}.",
            )
    requested_limit = request.get("limit")
    if requested_limit is not None and not request_contract.valid_public_row_limit(
        requested_limit
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            (
                "limit 必须是 "
                f"{request_contract.PUBLIC_ROW_LIMIT_MIN} 到 "
                f"{request_contract.PUBLIC_ROW_LIMIT_MAX} 的整数。"
            ),
            path=field_path("limit"),
            hint=(
                f"Use an integer between {request_contract.PUBLIC_ROW_LIMIT_MIN} "
                f"and {request_contract.PUBLIC_ROW_LIMIT_MAX}."
            ),
        )
    metric_filters = request.get("metric_filters")
    if metric_filters is not None:
        if (
            not isinstance(metric_filters, dict)
            or len(metric_filters) > request_contract.MAX_METRIC_FILTERS
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                f"metric_filters 最多接受 {request_contract.MAX_METRIC_FILTERS} 个受控筛选。",
                path=field_path("metric_filters"),
                hint="Use a JSON object containing only governed dimension filters.",
            )
        for filter_name, raw_value in metric_filters.items():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if (
                not values
                or len(values) > request_contract.MAX_FILTER_VALUES
                or any(
                    not capability_contract.is_value_scalar(value)
                    for value in values
                )
            ):
                raise QueryFailure(
                    "INVALID_INPUT",
                    f"每个指标筛选必须包含一到 {request_contract.MAX_FILTER_VALUES} 个标量值。",
                    path=field_path(f"metric_filters.{filter_name}"),
                    hint="Use one scalar or a non-empty bounded scalar array.",
                )
    decomposition_of = request.get("decomposition_of_request_id")
    if decomposition_of is not None and not request_contract.valid_string(
        decomposition_of, request_contract.REQUEST_ID
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            "decomposition_of_request_id 必须引用非空的同批整体请求 ID。",
        )
    target_gap_of = request.get("_target_gap_of_request_id")
    if target_gap_of is not None and not request_contract.valid_string(
        target_gap_of, request_contract.REQUEST_ID
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            "Internal target-gap linkage must reference a non-empty same-batch overall request ID.",
        )
    time_range = request.get("time_range")
    calendar_month = request.get("calendar_month")
    if time_range is not None and calendar_month is not None:
        raise QueryFailure(
            "INVALID_INPUT",
            "calendar_month and time_range are mutually exclusive.",
        )
    if calendar_month is not None:
        time_range = _calendar_month_time_range(calendar_month)
        request.pop("calendar_month")
        request["time_range"] = time_range
    if time_range is not None:
        if not isinstance(time_range, Mapping) or set(time_range) != {
            "start",
            "end",
        }:
            raise QueryFailure(
                "INVALID_INPUT",
                "time_range 结构无效。",
                path=field_path("time_range"),
                hint="Use exactly {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}.",
            )
        start, end = time_range.get("start"), time_range.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise QueryFailure("INVALID_INPUT", "time_range 边界必须是字符串。")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", end
        ):
            time_format = "%Y-%m-%d"
        else:
            raise QueryFailure("INVALID_INPUT", "time_range 格式或粒度不一致。")
        try:
            start_value = datetime.strptime(start, time_format)
            end_value = datetime.strptime(end, time_format)
        except ValueError as exc:
            raise QueryFailure("INVALID_INPUT", "time_range 包含无效日期。") from exc
        if start_value >= end_value:
            raise QueryFailure("INVALID_INPUT", "time_range 起止值无效。")
    comparison = request.get("comparison")
    if comparison is not None:
        if not isinstance(comparison, Mapping):
            raise QueryFailure(
                "INVALID_INPUT",
                "comparison 结构无效。",
                path=field_path("comparison"),
                hint="Use a documented comparison object.",
            )
        kind = comparison.get("kind")
        if kind == PREVIOUS_PERIOD_COMPARISON:
            if set(comparison) != {"kind"} or time_range is None:
                raise QueryFailure(
                    "INVALID_INPUT",
                    "previous_period 必须且只能提供 kind，并同时提供 time_range。",
                )
        elif kind == YEAR_OVER_YEAR_COMPARISON:
            if (
                set(comparison) != {"kind", "coverage"}
                or comparison.get("coverage") != MATCHED_ELAPSED_COVERAGE
                or time_range is None
            ):
                raise QueryFailure(
                    "INVALID_INPUT",
                    "year_over_year 必须提供 coverage=matched_elapsed，并同时提供 time_range。",
                )
        elif kind == SNAPSHOT_MONTHS_BEFORE_COMPARISON:
            months = comparison.get("months")
            if (
                set(comparison) != {"kind", "months"}
                or not isinstance(months, int)
                or isinstance(months, bool)
                or not 1 <= months <= 24
                or time_range is not None
            ):
                raise QueryFailure(
                    "INVALID_INPUT",
                    "snapshot_months_before 必须提供 1 到 24 的整数 months，且不接受 time_range。",
                )
        else:
            raise QueryFailure("INVALID_INPUT", "comparison.kind 不受支持。")
    complete_decomposition = request.get("complete_change_decomposition")
    if complete_decomposition is not None:
        dimension = (
            complete_decomposition.get("dimension")
            if isinstance(complete_decomposition, Mapping)
            else None
        )
        if (
            not isinstance(complete_decomposition, Mapping)
            or set(complete_decomposition) - {"dimension", "direction"}
            or complete_decomposition.get("direction", "increase") not in capability_contract.CHANGE_DIRECTIONS
            or not request_contract.valid_string(
                dimension, request_contract.DIMENSION_CODE
            )
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_change_decomposition requires a dimension and optional governed direction.",
            )
        conflicts = {
            "dimensions",
            "order_by",
            "limit",
            "decomposition_of_request_id",
        }
        if conflicts.intersection(request):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_change_decomposition cannot be combined with dimensions, order_by, limit, or decomposition_of_request_id.",
            )
        comparison = request.get("comparison")
        if comparison is None:
            if time_range is None:
                raise QueryFailure(
                    "INVALID_INPUT",
                    "complete_change_decomposition requires time_range or calendar_month.",
                )
            request["comparison"] = {"kind": PREVIOUS_PERIOD_COMPARISON}
        elif (
            not isinstance(comparison, Mapping)
            or (
                comparison.get("kind") == PREVIOUS_PERIOD_COMPARISON
                and set(comparison) != {"kind"}
            )
            or (
                comparison.get("kind") == YEAR_OVER_YEAR_COMPARISON
                and (
                    set(comparison) != {"kind", "coverage"}
                    or comparison.get("coverage") != MATCHED_ELAPSED_COVERAGE
                )
            )
            or (
                comparison.get("kind") == SNAPSHOT_MONTHS_BEFORE_COMPARISON
                and set(comparison) != {"kind", "months"}
            )
            or comparison.get("kind")
            not in {
                PREVIOUS_PERIOD_COMPARISON,
                YEAR_OVER_YEAR_COMPARISON,
                SNAPSHOT_MONTHS_BEFORE_COMPARISON,
            }
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_change_decomposition comparison contract is invalid.",
            )
    complete_target_gap = request.get("complete_target_gap_decomposition")
    if complete_target_gap is not None:
        target_gap_contract = _target_gap_contract()
        dimension = (
            complete_target_gap.get("dimension")
            if isinstance(complete_target_gap, Mapping)
            else None
        )
        if (
            not isinstance(complete_target_gap, Mapping)
            or set(complete_target_gap) != {"dimension"}
            or dimension not in target_gap_contract.dimensions
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_target_gap_decomposition requires one governed target dimension.",
            )
        conflicts = {
            "dimensions",
            "order_by",
            "limit",
            "comparison",
            "time_bucket",
            "decomposition_of_request_id",
            "complete_change_decomposition",
            "_target_gap_of_request_id",
        }
        if conflicts.intersection(request):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_target_gap_decomposition cannot be combined with dimensions, ordering, limits, comparisons, time_bucket, or another decomposition link.",
            )
        if (
            domain != "target"
            or request.get("metric") not in target_gap_contract.metrics
            or request.get("attribution_mode")
            != target_gap_contract.attribution_mode
        ):
            raise QueryFailure(
                "UNSUPPORTED_TARGET_GAP_DECOMPOSITION",
                "The selected metric, ledger, or dimension does not authorize complete target-gap decomposition.",
                stage="query_planning",
            )
    if isinstance(time_range, dict) and "field" in time_range:
        raise QueryFailure("INVALID_INPUT", "指标模式的 time_range 不接受物理字段。")
    return request
