"""Guarded query planning and execution for DataSage Mini.

The module intentionally exposes one handler and no lifecycle hooks. Business
semantics live in versioned skill references; this module enforces the tool
boundary and returns structured evidence.
"""

from __future__ import annotations

import json
import hashlib
import importlib
import logging
import math
import os
import re
import ssl
import sys
import threading
import time
import uuid
import calendar
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .analytical_queries import AnalysisQueryError, build_analytical_metric_query
from .db_security import (
    confirm_mysql_read_only_transaction,
    DatabaseSecurityError,
    mysql_tls_kwargs,
    validate_mysql_source_evidence,
    verify_mysql_read_only_grants,
    verify_mysql_source_identity,
    verify_mysql_tls,
)
from . import entities, evidence, settings


logger = logging.getLogger(__name__)
_CAPABILITY_LOGGER = logging.getLogger(f"{__name__}.capabilities")
_PYMYSQL_IMPORT_LOCK = threading.RLock()
_QUERY_CONCURRENCY_LOCK = threading.Lock()
_ACTIVE_QUERY_CALLS = 0

_DOMAINS = {
    "delivery",
    "receipt",
    "receivable",
    "target",
    "customer_risk",
    "inventory",
}
_SEMANTIC_PATHS = {
    "delivery": "plugins/datasage-query/contracts/delivery-semantics.yaml",
    "receipt": "plugins/datasage-query/contracts/receipt-semantics.yaml",
    "receivable": "plugins/datasage-query/contracts/receivable-semantics.yaml",
    "target": "plugins/datasage-query/contracts/target-semantics.yaml",
    "customer_risk": "plugins/datasage-query/contracts/customer_risk-semantics.yaml",
    "inventory": "plugins/datasage-query/contracts/inventory-semantics.yaml",
}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
_MYSQL_MONTH_FORMAT = "%%Y-%%m"
_MYSQL_DAY_FORMAT = "%%Y-%%m-%%d"
_QUERY_POLICY_PATH = (
    "plugins/datasage-query/contracts/query-policy.yaml"
)
_FILTER_OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_COMMON_REQUEST_FIELDS = {
    "request_id",
    "domain",
    "mode",
    "purpose",
    "analysis_intent",
    "evidence_role",
    "time_range",
    "calendar_month",
    "order_by",
    "limit",
}
_METRIC_REQUEST_FIELDS = _COMMON_REQUEST_FIELDS | {
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
}
_GROSS_DELIVERY_SCOPES = {"explicit_gross", "order_delivery_alignment"}
_ORDER_DELIVERY_ALIGNMENT_METRICS = {
    "order_amount",
    "order_quantity",
    "order_roll_count",
    "placed_order_count",
    "delivery_order_count",
}
_INVENTORY_SCOPES = {"total", "on_hand", "available", "allocated", "in_transit"}
_MAX_METRIC_FILTERS = 12
_MAX_FILTER_VALUES = 50
_INTERNAL_MATCH_COUNT = "__matched_row_count"
_INTERNAL_PARTITION_CURRENT = "__full_partition_metric_value"
_INTERNAL_PARTITION_COMPARISON = "__full_partition_comparison_value"
_INTERNAL_PARTITION_DELTA = "__full_partition_delta_value"
_INTERNAL_PARTITION_ROW_COUNT = "__full_partition_row_count"
_INTERNAL_RESULT_FIELDS = {
    _INTERNAL_MATCH_COUNT,
    _INTERNAL_PARTITION_CURRENT,
    _INTERNAL_PARTITION_COMPARISON,
    _INTERNAL_PARTITION_DELTA,
    _INTERNAL_PARTITION_ROW_COUNT,
}
_BUSINESS_TIME_ZONE = timezone(timedelta(hours=8))
_EVIDENCE_INTERPRETATION = (
    "查询结果只能直接证明返回的事实、对比和关联；对于为什么、驱动因素或原因分析，"
    "未被证据直接验证的原因必须标为可能、相关或未知/待验证，不得写成已确认因果。"
)


class QueryFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        timeout: bool = False,
        stage: str | None = None,
        retryable: bool | None = None,
        source_evidence_ref: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.timeout = timeout
        self.stage = stage
        self.retryable = retryable
        self.source_evidence_ref = (
            dict(source_evidence_ref)
            if isinstance(source_evidence_ref, Mapping)
            else None
        )


def _at_stage(failure: QueryFailure, stage: str) -> QueryFailure:
    """Attach the first trustworthy failure stage without hiding the cause."""

    if failure.stage is None:
        failure.stage = stage
    return failure


def _ensure_metric_available(metric: Mapping[str, Any]) -> None:
    availability = metric.get("availability")
    if availability is None:
        return
    if not isinstance(availability, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标可用性定义无效。")
    status = str(availability.get("status") or "available")
    if status == "available":
        return
    if status not in {"blocked", "pending_validation"}:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标包含未知可用性状态。")
    code = availability.get("error_code")
    message = availability.get("message")
    if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "不可用指标缺少结构化错误定义。")
    # Keep the operational reason in the private execution contract.  A caller
    # that guesses a non-projected metric must not learn its pending rollout
    # state, source wording, or business label through the model-visible error.
    raise QueryFailure(code, "该指标当前不可用于回答。")


def _max_group_dimensions(metric: Mapping[str, Any]) -> int:
    value = metric.get("max_group_dimensions", 5)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 5
        or (metric.get("query_kind") is not None and "max_group_dimensions" not in metric)
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "分析指标缺少有效的分组维度上限。",
        )
    return value


def _profile_root() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=64)
def _parse_yaml_cached(path_text: str, modified_ns: int, size: int) -> dict[str, Any]:
    del modified_ns, size
    value = yaml.safe_load(Path(path_text).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "业务语义格式无效。")
    return value


def _read_yaml(relative_path: str) -> dict[str, Any]:
    root = _profile_root()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "语义文件路径不安全。") from exc
    try:
        stat = path.stat()
        value = _parse_yaml_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, yaml.YAMLError) as exc:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "暂时无法读取业务语义。") from exc
    return value


def _max_metric_range_days() -> int:
    """Read the executor limit from the same authority exposed to planning."""

    policy = _read_yaml(_QUERY_POLICY_PATH)
    time_range = policy.get("governed_metric_time_range")
    if (
        policy.get("version") != "datasage-query-policy/v1"
        or set(policy) != {"version", "governed_metric_time_range"}
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
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "治理查询时间策略无效。",
            stage="contract_load",
        )
    return int(time_range["max_days"])


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
    if re.fullmatch(r"\d{4}-\d{2}", start) and re.fullmatch(
        r"\d{4}-\d{2}", end
    ):
        granularity = "month"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(
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


def _contracts(domain: str) -> tuple[dict[str, Any], dict[str, Any]]:
    datasets = _read_yaml("plugins/datasage-query/contracts/datasets.yaml")
    semantics = _read_yaml(_SEMANTIC_PATHS[domain])
    _validate_value_contract_definitions(semantics)
    return datasets, semantics


_VALUE_CONTRACT_KINDS = {"closed", "source_exact", "entity_exact"}
_VALUE_SCALAR_TYPES = (str, int, float, bool)


def _is_value_scalar(value: Any) -> bool:
    return isinstance(value, _VALUE_SCALAR_TYPES) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _validate_value_contract_definitions(semantics: Mapping[str, Any]) -> None:
    """Fail closed when a dimension's sole value source is malformed."""

    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "业务域缺少维度取值合同。",
            stage="contract_load",
        )
    for code, raw_definition in dimensions.items():
        if not isinstance(code, str) or not isinstance(raw_definition, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "维度取值合同格式无效。",
                stage="contract_load",
            )
        value_contract = raw_definition.get("value_contract")
        if not isinstance(value_contract, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "维度缺少唯一取值合同。",
                stage="contract_load",
            )
        kind = value_contract.get("kind")
        if kind not in _VALUE_CONTRACT_KINDS:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "维度取值合同类型无效。",
                stage="contract_load",
            )
        filterable = value_contract.get("filterable", raw_definition.get("filterable", True))
        if not isinstance(filterable, bool):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "维度过滤能力定义无效。",
                stage="contract_load",
            )
        if kind == "closed":
            allowed_values = value_contract.get("allowed_values")
            if (
                not isinstance(allowed_values, list)
                or not allowed_values
                or any(not _is_value_scalar(value) for value in allowed_values)
            ):
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "封闭维度缺少有效允许值。",
                    stage="contract_load",
                )
            typed_values = {(type(value), value) for value in allowed_values}
            if len(typed_values) != len(allowed_values):
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "封闭维度允许值存在重复。",
                    stage="contract_load",
                )
            canonical_aliases = value_contract.get("canonical_aliases")
            if canonical_aliases is not None:
                if not isinstance(canonical_aliases, Mapping) or any(
                    not _is_value_scalar(alias)
                    or not _is_value_scalar(canonical)
                    or (type(canonical), canonical) not in typed_values
                    for alias, canonical in canonical_aliases.items()
                ):
                    raise QueryFailure(
                        "CONTRACT_UNAVAILABLE",
                        "封闭维度规范别名定义无效。",
                        stage="contract_load",
                    )
            meanings = value_contract.get("business_meanings")
            if meanings is not None and (
                not isinstance(meanings, Mapping)
                or any(
                    not _is_value_scalar(key)
                    or not isinstance(value, str)
                    or not value.strip()
                    for key, value in meanings.items()
                )
            ):
                raise QueryFailure(
                    "CONTRACT_UNAVAILABLE",
                    "封闭维度业务含义定义无效。",
                    stage="contract_load",
                )
        if kind == "entity_exact" and not isinstance(
            raw_definition.get("identity_filter"), Mapping
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "实体维度缺少稳定身份过滤定义。",
                stage="contract_load",
            )
        if kind == "source_exact" and raw_definition.get("normalization") is not None:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "原值精确维度不能同时声明运行时改写规则。",
                stage="contract_load",
            )
    metrics = semantics.get("metrics")
    if metrics is None:
        return
    if not isinstance(metrics, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "业务域缺少指标合同。",
            stage="contract_load",
        )
    known_dimensions = {str(code) for code in dimensions}
    for metric_code, metric in metrics.items():
        if not isinstance(metric_code, str) or not isinstance(metric, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标合同格式无效。",
                stage="contract_load",
            )
        capability = metric.get("change_decomposition")
        if capability is None:
            continue
        allowed = metric.get("allowed_dimensions")
        if (
            not isinstance(capability, Mapping)
            or capability.get("mode") != "additive_partition"
            or not isinstance(capability.get("dimensions"), list)
            or not capability["dimensions"]
            or any(not isinstance(code, str) for code in capability["dimensions"])
            or len(set(capability["dimensions"])) != len(capability["dimensions"])
            or not set(capability["dimensions"]) <= known_dimensions
            or not isinstance(allowed, list)
            or not set(capability["dimensions"]) <= set(allowed)
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标变化分解合同无效。",
                stage="contract_load",
            )


def _validate_metric_filter_value_contracts(
    request: Mapping[str, Any], semantics: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate normalized metric-filter values without guessing or rewriting."""

    filters = request.get("metric_filters")
    if not isinstance(filters, Mapping) or not filters:
        return dict(request)
    normalized_request = dict(request)
    normalized_filters = dict(filters)
    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "业务域缺少维度取值合同。",
            stage="contract_load",
        )
    for code, raw_value in filters.items():
        definition = dimensions.get(code)
        if not isinstance(definition, Mapping):
            # The governed metric validator will report an unsupported role.
            continue
        value_contract = definition.get("value_contract")
        if not isinstance(value_contract, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "过滤维度缺少唯一取值合同。",
                stage="contract_load",
            )
        if value_contract.get("filterable", definition.get("filterable", True)) is False:
            raise QueryFailure(
                "UNSUPPORTED_DIMENSION_FILTER",
                "该维度仅支持分组展示，不能作为筛选条件。",
                stage="input_validation",
            )
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        if not values or any(not _is_value_scalar(value) for value in values):
            raise QueryFailure(
                "INVALID_INPUT",
                "维度筛选值必须是非空标量或非空标量列表。",
                stage="input_validation",
            )
        kind = value_contract.get("kind")
        if kind == "closed":
            allowed = {
                (type(value), value) for value in value_contract.get("allowed_values", [])
            }
            aliases = {
                (type(alias), alias): canonical
                for alias, canonical in (value_contract.get("canonical_aliases") or {}).items()
            }
            canonical_values = [
                aliases.get((type(value), value), value) for value in values
            ]
            if any((type(value), value) not in allowed for value in canonical_values):
                raise QueryFailure(
                    "FILTER_VALUE_NOT_ALLOWED",
                    "筛选值不在该维度的受控允许值中。",
                    stage="input_validation",
                )
            normalized_filters[code] = (
                canonical_values if isinstance(raw_value, list) else canonical_values[0]
            )
        elif kind == "source_exact":
            # Exact source values pass through byte-for-byte; this layer never
            # normalizes, expands, translates, or guesses a replacement.
            continue
        elif kind == "entity_exact":
            if code not in (request.get("_entity_bindings") or {}):
                raise QueryFailure(
                    "ENTITY_IDENTITY_UNAVAILABLE",
                    "实体筛选未完成稳定身份绑定。",
                    stage="entity_preflight",
                )
        else:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "过滤维度取值合同类型无效。",
                stage="contract_load",
            )
    normalized_request["metric_filters"] = normalized_filters
    return normalized_request


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    return settings.get_int(name, default, minimum, maximum)


def _try_acquire_query_slot() -> bool:
    """Reserve one bounded query call without queueing Hermes workers."""

    global _ACTIVE_QUERY_CALLS
    capacity = _bounded_int("max_concurrent_queries", 4, 1, 16)
    with _QUERY_CONCURRENCY_LOCK:
        if _ACTIVE_QUERY_CALLS >= capacity:
            return False
        _ACTIVE_QUERY_CALLS += 1
        return True


def _release_query_slot() -> None:
    global _ACTIVE_QUERY_CALLS
    with _QUERY_CONCURRENCY_LOCK:
        if _ACTIVE_QUERY_CALLS <= 0:
            logger.error("datasage_query concurrency_slot_underflow")
            _ACTIVE_QUERY_CALLS = 0
            return
        _ACTIVE_QUERY_CALLS -= 1


def _connection_port() -> int:
    """Read the connection endpoint from env without making it policy.

    Fail-closed: an unset port defaults to 3306, but a non-numeric or
    out-of-range value raises instead of silently falling back.
    """

    raw = os.environ.get("DATA_QUERY_MYSQL_PORT", "").strip()
    if not raw:
        return 3306
    try:
        value = int(raw)
    except ValueError as exc:
        raise QueryFailure(
            "INVALID_INPUT",
            "DATA_QUERY_MYSQL_PORT 不是有效端口号，请检查配置。",
        ) from exc
    if not 1 <= value <= 65535:
        raise QueryFailure(
            "INVALID_INPUT",
            "DATA_QUERY_MYSQL_PORT 超出有效端口范围（1-65535）。",
        )
    return value


def _quote_identifier(value: str) -> str:
    if not isinstance(value, str) or _COLUMN_IDENTIFIER.fullmatch(value) is None:
        raise QueryFailure("INVALID_PLAN", "查询包含无效字段标识。")
    return f"`{value}`"


def _quote_table(value: str) -> str:
    if not isinstance(value, str) or _TABLE_IDENTIFIER.fullmatch(value) is None:
        raise QueryFailure("INVALID_PLAN", "查询包含无效数据表标识。")
    return ".".join(_quote_identifier(part) for part in value.split("."))


def _qualified_identifier(alias: str, column: str) -> str:
    return f"{_quote_identifier(alias)}.{_quote_identifier(column)}"


def _next_month_start(today: date) -> date:
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    return date(year, month, 1)


def _calendar_month_time_range(value: Any) -> dict[str, str]:
    """Expand a typed calendar month into canonical half-open day bounds."""

    if not isinstance(value, str) or re.fullmatch(
        r"^(?!0000-)(?!9999-12$)[0-9]{4}-(?:0[1-9]|1[0-2])$", value
    ) is None:
        raise QueryFailure(
            "INVALID_INPUT",
            "calendar_month must use a zero-padded YYYY-MM value.",
        )
    try:
        start = date.fromisoformat(f"{value}-01")
        end = _next_month_start(start)
    except (ValueError, OverflowError) as exc:
        raise QueryFailure(
            "INVALID_INPUT",
            "calendar_month contains an invalid or unsupported year-month.",
        ) from exc
    return {"start": start.isoformat(), "end": end.isoformat()}


def _default_time_range(policy: str) -> tuple[str, str] | None:
    if policy != "current_month":
        return None
    today = datetime.now(_BUSINESS_TIME_ZONE).date()
    return date(today.year, today.month, 1).isoformat(), _next_month_start(today).isoformat()


def _metric_time_bounds(metric: Mapping[str, Any], start: str, end: str) -> tuple[str, str]:
    if metric.get("time_granularity") != "month":
        return start, end
    month_pattern = re.compile(r"^\d{4}-\d{2}$")
    boundary_pattern = re.compile(r"^\d{4}-\d{2}-01$")
    if month_pattern.fullmatch(start) and month_pattern.fullmatch(end):
        return start, end
    if boundary_pattern.fullmatch(start) and boundary_pattern.fullmatch(end):
        return start[:7], end[:7]
    raise QueryFailure("INVALID_PLAN", "月粒度指标必须使用 YYYY-MM 或自然月首日边界。")


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


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


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise QueryFailure("INVALID_INPUT", "每个查询请求必须是对象。")
    request = dict(request)
    request_id = request.get("request_id")
    domain = request.get("domain")
    mode = request.get("mode")
    purpose = request.get("purpose")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 64:
        raise QueryFailure("INVALID_INPUT", "request_id 无效。")
    if domain not in _DOMAINS:
        raise QueryFailure("INVALID_INPUT", "业务域不受支持。")
    if mode == "dataset":
        raise QueryFailure(
            "DETAIL_CONTRACT_UNAVAILABLE",
            "当前发布版本尚未开放语义明细合同，请使用已登记指标和维度。",
        )
    if mode != "metric":
        raise QueryFailure("INVALID_INPUT", "查询模式不受支持。")
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 300:
        raise QueryFailure("INVALID_INPUT", "查询目的无效。")
    if set(request) - _METRIC_REQUEST_FIELDS:
        raise QueryFailure("INVALID_INPUT", "请求包含当前模式不接受的字段。")
    if not isinstance(request.get("metric"), str):
        raise QueryFailure("INVALID_INPUT", "指标模式缺少 metric。")
    metric_filters = request.get("metric_filters")
    if metric_filters is not None:
        if (
            not isinstance(metric_filters, dict)
            or len(metric_filters) > _MAX_METRIC_FILTERS
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                f"metric_filters 最多接受 {_MAX_METRIC_FILTERS} 个受控筛选。",
            )
        for raw_value in metric_filters.values():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if (
                not values
                or len(values) > _MAX_FILTER_VALUES
                or any(not _is_value_scalar(value) for value in values)
            ):
                raise QueryFailure(
                    "INVALID_INPUT",
                    f"每个指标筛选必须包含一到 {_MAX_FILTER_VALUES} 个标量值。",
                )
    analysis_intent = request.get("analysis_intent")
    if analysis_intent is not None and analysis_intent not in evidence.ANALYSIS_INTENTS:
        raise QueryFailure("INVALID_INPUT", "analysis_intent 不受支持。")
    evidence_role = request.get("evidence_role")
    if evidence_role is not None and evidence_role not in evidence.EVIDENCE_ROLES:
        raise QueryFailure("INVALID_INPUT", "evidence_role 不受支持。")
    decomposition_of = request.get("decomposition_of_request_id")
    if decomposition_of is not None and (
        not isinstance(decomposition_of, str)
        or not decomposition_of.strip()
        or len(decomposition_of) > 64
    ):
        raise QueryFailure(
            "INVALID_INPUT",
            "decomposition_of_request_id 必须引用非空的同批整体请求 ID。",
        )
    target_gap_of = request.get("_target_gap_of_request_id")
    if target_gap_of is not None and (
        not isinstance(target_gap_of, str)
        or not target_gap_of.strip()
        or len(target_gap_of) > 64
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
            raise QueryFailure("INVALID_INPUT", "time_range 结构无效。")
        start, end = time_range.get("start"), time_range.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise QueryFailure("INVALID_INPUT", "time_range 边界必须是字符串。")
        if re.fullmatch(r"\d{4}-\d{2}", start) and re.fullmatch(
            r"\d{4}-\d{2}", end
        ):
            time_format = "%Y-%m"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(
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
            raise QueryFailure("INVALID_INPUT", "comparison 结构无效。")
        kind = comparison.get("kind")
        if kind == "previous_period":
            if set(comparison) != {"kind"} or time_range is None:
                raise QueryFailure(
                    "INVALID_INPUT",
                    "previous_period 必须且只能提供 kind，并同时提供 time_range。",
                )
        elif kind == "snapshot_months_before":
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
            or set(complete_decomposition) != {"dimension"}
            or not isinstance(dimension, str)
            or not dimension.strip()
            or len(dimension) > 80
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_change_decomposition must contain exactly one non-empty dimension.",
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
            request["comparison"] = {"kind": "previous_period"}
        elif (
            not isinstance(comparison, Mapping)
            or set(comparison) != {"kind"}
            or comparison.get("kind") != "previous_period"
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_change_decomposition only supports previous_period comparison.",
            )
    complete_target_gap = request.get("complete_target_gap_decomposition")
    if complete_target_gap is not None:
        dimension = (
            complete_target_gap.get("dimension")
            if isinstance(complete_target_gap, Mapping)
            else None
        )
        if (
            not isinstance(complete_target_gap, Mapping)
            or set(complete_target_gap) != {"dimension"}
            or dimension not in {"customer", "department", "organization"}
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
            "decomposition_of_request_id",
            "complete_change_decomposition",
            "_target_gap_of_request_id",
        }
        if conflicts.intersection(request):
            raise QueryFailure(
                "INVALID_INPUT",
                "complete_target_gap_decomposition cannot be combined with dimensions, ordering, limits, comparisons, or another decomposition link.",
            )
        if (
            domain != "target"
            or request.get("metric")
            not in {
                "delivery_target_completion",
                "receipt_target_completion",
            }
            or request.get("attribution_mode") != "transaction_detail"
        ):
            raise QueryFailure(
                "UNSUPPORTED_TARGET_GAP_DECOMPOSITION",
                "The selected metric, ledger, or dimension does not authorize complete target-gap decomposition.",
                stage="query_planning",
            )
    if isinstance(time_range, dict) and "field" in time_range:
        raise QueryFailure("INVALID_INPUT", "指标模式的 time_range 不接受物理字段。")
    attribution_mode = request.get("attribution_mode")
    if domain == "target" and mode == "metric":
        if attribution_mode not in {"transaction_detail", "salesperson_allocation"}:
            raise QueryFailure("ATTRIBUTION_MODE_REQUIRED", "目标指标必须明确交易事实账或业务员分摊账。")
    elif attribution_mode is not None:
        raise QueryFailure("INVALID_INPUT", "只有目标域指标可以指定 attribution_mode。")
    return request


def _complete_decomposition_overall_id(
    request_id: str,
    used_request_ids: set[str],
) -> str:
    """Build a deterministic, collision-free internal ID within the wire limit."""

    for nonce in range(1000):
        digest = hashlib.sha256(
            f"{request_id}|complete_change_decomposition|overall|{nonce}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        suffix = f"__overall_{digest}"
        candidate = f"{request_id[: 64 - len(suffix)]}{suffix}"
        if candidate not in used_request_ids:
            return candidate
    raise QueryFailure(
        "INVALID_INPUT",
        "Unable to allocate a unique request ID for complete change decomposition.",
    )


def _expand_complete_change_decompositions(
    requests: Sequence[Any],
) -> tuple[list[Any], dict[str, str]]:
    """Expand only explicit semantic operations into governed request pairs."""

    used_request_ids = {
        str(request.get("request_id"))
        for request in requests
        if isinstance(request, Mapping)
        and isinstance(request.get("request_id"), str)
    }
    expanded: list[Any] = []
    operation_partitions: dict[str, str] = {}
    for raw_request in requests:
        operation = (
            raw_request.get("complete_change_decomposition")
            if isinstance(raw_request, Mapping)
            else None
        )
        if operation is None:
            expanded.append(raw_request)
            continue
        request = _validate_request(raw_request)
        dimension = str(operation["dimension"]).strip()
        partition_id = str(request["request_id"])
        overall_id = _complete_decomposition_overall_id(
            partition_id,
            used_request_ids,
        )
        used_request_ids.add(overall_id)
        base = dict(request)
        base.pop("complete_change_decomposition", None)
        overall_request = {
            **base,
            "request_id": overall_id,
        }
        partition_request = {
            **base,
            "request_id": partition_id,
            "dimensions": [dimension],
            "decomposition_of_request_id": overall_id,
        }
        expanded.extend((overall_request, partition_request))
        operation_partitions[partition_id] = overall_id
    return expanded, operation_partitions


def _validate_complete_decomposition_capability(
    prepared: Mapping[str, Any],
    dimension: str,
) -> None:
    """Authorize the selected metric and dimension before business SQL runs."""

    request = prepared.get("request")
    semantics = prepared.get("semantics")
    metrics = semantics.get("metrics") if isinstance(semantics, Mapping) else None
    metric = (
        metrics.get(request.get("metric"))
        if isinstance(metrics, Mapping) and isinstance(request, Mapping)
        else None
    )
    capability = (
        metric.get("change_decomposition")
        if isinstance(metric, Mapping)
        else None
    )
    dimensions = (
        capability.get("dimensions")
        if isinstance(capability, Mapping)
        else None
    )
    if (
        not isinstance(capability, Mapping)
        or capability.get("mode") != "additive_partition"
        or not isinstance(dimensions, list)
        or dimension not in dimensions
    ):
        raise QueryFailure(
            "UNSUPPORTED_CHANGE_DECOMPOSITION",
            "The selected metric and dimension do not authorize a complete change decomposition.",
            stage="query_planning",
        )


def _target_gap_overall_id(
    request_id: str,
    used_request_ids: set[str],
) -> str:
    """Build a deterministic internal overall ID for a target-gap operation."""

    for nonce in range(1000):
        digest = hashlib.sha256(
            f"{request_id}|complete_target_gap_decomposition|overall|{nonce}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        suffix = f"__gapall_{digest}"
        candidate = f"{request_id[: 64 - len(suffix)]}{suffix}"
        if candidate not in used_request_ids:
            return candidate
    raise QueryFailure(
        "INVALID_INPUT",
        "Unable to allocate a unique request ID for complete target-gap decomposition.",
    )


def _expand_complete_target_gap_decompositions(
    requests: Sequence[Any],
) -> tuple[list[Any], dict[str, str]]:
    """Expand an explicit target-gap operation into overall and partition queries."""

    used_request_ids = {
        str(request.get("request_id"))
        for request in requests
        if isinstance(request, Mapping)
        and isinstance(request.get("request_id"), str)
    }
    expanded: list[Any] = []
    operation_partitions: dict[str, str] = {}
    for raw_request in requests:
        operation = (
            raw_request.get("complete_target_gap_decomposition")
            if isinstance(raw_request, Mapping)
            else None
        )
        if operation is None:
            expanded.append(raw_request)
            continue
        request = _validate_request(raw_request)
        dimension = str(operation["dimension"])
        partition_id = str(request["request_id"])
        overall_id = _target_gap_overall_id(partition_id, used_request_ids)
        used_request_ids.add(overall_id)
        base = dict(request)
        base.pop("complete_target_gap_decomposition", None)
        overall_request = {**base, "request_id": overall_id}
        partition_request = {
            **base,
            "request_id": partition_id,
            "dimensions": [dimension],
            "_target_gap_of_request_id": overall_id,
        }
        expanded.extend((overall_request, partition_request))
        operation_partitions[partition_id] = overall_id
    return expanded, operation_partitions


def _validate_target_gap_decomposition_capability(
    prepared: Mapping[str, Any],
    dimension: str,
) -> None:
    """Authorize only the versioned target transaction-detail gap contract."""

    request = prepared.get("request")
    semantics = prepared.get("semantics")
    metrics = semantics.get("metrics") if isinstance(semantics, Mapping) else None
    metric = (
        metrics.get(request.get("metric"))
        if isinstance(metrics, Mapping) and isinstance(request, Mapping)
        else None
    )
    contract = _read_yaml(
        "plugins/datasage-query/contracts/target-gap-decomposition.yaml"
    )
    applicability = contract.get("applicability")
    if (
        contract.get("version") != "datasage-target-gap-decomposition/v1"
        or contract.get("status") != "active"
        or not isinstance(applicability, Mapping)
        or not isinstance(request, Mapping)
        or request.get("domain") != "target"
        or request.get("metric") not in applicability.get("metrics", [])
        or request.get("attribution_mode") != applicability.get("attribution_mode")
        or dimension not in applicability.get("dimensions", [])
        or not isinstance(metric, Mapping)
        or metric.get("query_kind") != "target_completion"
    ):
        raise QueryFailure(
            "UNSUPPORTED_TARGET_GAP_DECOMPOSITION",
            "The selected metric, ledger, or dimension does not authorize complete target-gap decomposition.",
            stage="query_planning",
        )
    paths = metric.get("paths")
    path = paths.get("transaction_detail") if isinstance(paths, Mapping) else None
    if (
        not isinstance(path, Mapping)
        or path.get("ledger") != "transaction_detail"
        or dimension not in path.get("allowed_dimensions", [])
    ):
        raise QueryFailure(
            "UNSUPPORTED_TARGET_GAP_DECOMPOSITION",
            "The selected target path does not authorize this complete gap partition.",
            stage="query_planning",
        )


def _validate_delivery_metric_scope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit delivery scope fields without re-reading natural language."""

    normalized = dict(request)
    scope = normalized.get("delivery_scope")
    if normalized.get("domain") != "delivery" or normalized.get("mode") != "metric":
        if scope is not None:
            raise QueryFailure("INVALID_INPUT", "delivery_scope 只适用于出库域指标。")
        return normalized

    metric = str(normalized.get("metric") or "")
    if "gross_delivery" in metric:
        if scope not in _GROSS_DELIVERY_SCOPES:
            raise QueryFailure(
                "GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST",
                "毛出库指标必须通过结构化字段确认用户明确要求毛口径或下单出库对照。",
            )
    elif scope == "order_delivery_alignment" and metric in _ORDER_DELIVERY_ALIGNMENT_METRICS:
        pass
    elif scope not in {None, "default_net"}:
        raise QueryFailure("INVALID_INPUT", "当前指标与 delivery_scope 不一致。")

    filters = normalized.get("metric_filters") or {}
    if isinstance(filters, dict) and "ready_goods" in filters:
        value = filters["ready_goods"]
        values = value if isinstance(value, list) else [value]
        if not values or any(item not in {"y", "n"} for item in values):
            raise QueryFailure("INVALID_PLAN", "备货筛选只接受结构化值 y 或 n。")
    return normalized


def _validate_inventory_metric_scope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the structured current-inventory scope without reading purpose text."""

    normalized = dict(request)
    scope = normalized.get("inventory_scope")
    if normalized.get("domain") != "inventory" or normalized.get("mode") != "metric":
        if scope is not None:
            raise QueryFailure("INVALID_INPUT", "inventory_scope 只适用于库存域指标。")
        return normalized
    if scope is not None and scope not in _INVENTORY_SCOPES:
        raise QueryFailure("INVALID_INPUT", "库存范围不受支持。")
    return normalized


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


def _filter_clause(
    column: str, spec: Mapping[str, Any], params: list[Any], *, alias: str | None = None
) -> str:
    op = str(spec.get("op", "eq")).lower()
    value = spec.get("value")
    quoted = _qualified_identifier(alias, column) if alias else _quote_identifier(column)
    if op == "eq":
        params.append(value)
        return f"{quoted} = %s"
    if op == "ne":
        params.append(value)
        return f"{quoted} <> %s"
    if op == "in" and isinstance(value, list) and value:
        params.extend(value)
        return f"{quoted} IN ({', '.join(['%s'] * len(value))})"
    if op in _FILTER_OPERATORS and op not in {"eq", "ne"} and not isinstance(value, (list, dict)):
        params.append(value)
        return f"{quoted} {_FILTER_OPERATORS[op]} %s"
    raise QueryFailure("INVALID_PLAN", "指标定义包含不支持的过滤规则。")


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


def _blocked_columns(
    datasets_contract: Mapping[str, Any], dataset: Mapping[str, Any]
) -> set[str]:
    global_blocked = datasets_contract.get("defaults", {}).get("blocked_columns") or []
    local_blocked = dataset.get("forbidden_columns") or []
    return {str(item).lower() for item in [*global_blocked, *local_blocked]}


def _dimension_columns(definition: Mapping[str, Any]) -> list[tuple[str, str]]:
    raw_columns = definition.get("columns") or []
    if not isinstance(raw_columns, list) or not raw_columns:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度定义缺少有效字段。")
    result: list[tuple[str, str]] = []
    for item in raw_columns:
        if isinstance(item, str):
            column, output = item, item
        elif isinstance(item, dict):
            column, output = item.get("column"), item.get("alias") or item.get("column")
        else:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "维度字段定义格式无效。")
        if (
            not isinstance(column, str)
            or _IDENTIFIER.fullmatch(column) is None
            or not isinstance(output, str)
            or _IDENTIFIER.fullmatch(output) is None
        ):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "维度字段或输出名称无效。")
        result.append((column, output))
    return result


def _dimension_expression(definition: Mapping[str, Any], alias: str, column: str) -> str:
    expression = _qualified_identifier(alias, column)
    normalization = definition.get("normalization")
    if normalization is None:
        return expression
    if normalization == "meter_case":
        return f"CASE WHEN LOWER({expression}) = 'm' THEN 'm' ELSE {expression} END"
    raise QueryFailure("CONTRACT_UNAVAILABLE", "维度标准化规则不受支持。")


def _normalized_dimension_value(definition: Mapping[str, Any], value: Any) -> Any:
    normalization = definition.get("normalization")
    if normalization is None:
        return value
    if normalization != "meter_case":
        raise QueryFailure("CONTRACT_UNAVAILABLE", "维度标准化规则不受支持。")
    if isinstance(value, str):
        return "m" if value.lower() == "m" else value
    if isinstance(value, list):
        return [
            "m" if isinstance(item, str) and item.lower() == "m" else item
            for item in value
        ]
    return value


def _bound_entity_filter(
    request: Mapping[str, Any],
    code: str,
    definition: Mapping[str, Any],
    fallback_value: Any,
) -> tuple[str, Any]:
    bindings = request.get("_entity_bindings") or {}
    binding = bindings.get(code) if isinstance(bindings, Mapping) else None
    if not isinstance(binding, Mapping):
        column = definition.get("filter_column")
        return str(column) if isinstance(column, str) else "", fallback_value
    identity_filter = definition.get("identity_filter")
    if (
        not isinstance(identity_filter, Mapping)
        or identity_filter.get("entity_type") != binding.get("entity_type")
        or identity_filter.get("value_field") != binding.get("value_field")
        or not isinstance(identity_filter.get("column"), str)
        or identity_filter.get("column") not in set(binding.get("identity_columns") or [])
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "实体绑定与指标的稳定身份过滤定义不一致。",
        )
    values = binding.get("filter_values")
    if not isinstance(values, list) or not values:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "实体绑定缺少稳定身份值。")
    value: Any = values
    if not isinstance(fallback_value, list) and len(values) == 1:
        value = values[0]
    return str(identity_filter["column"]), value


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


def _metric_aggregation_sql(
    metric: Mapping[str, Any],
    dataset: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    alias: str,
    *,
    joined_alias: str | None = None,
    joined_dataset: Mapping[str, Any] | None = None,
) -> str:
    aggregation = metric.get("aggregation")
    allowed = {str(item) for item in dataset.get("allowed_columns") or []}
    blocked = _blocked_columns(datasets_contract, dataset)
    if aggregation == "sum_product":
        columns = metric.get("measure_columns")
        if not isinstance(columns, list) or len(columns) != 2:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "乘积求和指标必须声明两个字段。")
        approved = [_approved_column(column, allowed, blocked) for column in columns]
        expression = " * ".join(_qualified_identifier(alias, column) for column in approved)
        return f"COALESCE(SUM({expression}), 0)"
    if aggregation == "sum_product_many":
        columns = metric.get("measure_columns")
        multiplier = metric.get("multiplier")
        if not isinstance(columns, list) or not columns or not isinstance(multiplier, str):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "多字段乘积求和指标定义无效。")
        approved = [_approved_column(column, allowed, blocked) for column in columns]
        multiplier = _approved_column(multiplier, allowed, blocked)
        additive = " + ".join(_qualified_identifier(alias, column) for column in approved)
        return f"COALESCE(SUM(({additive}) * {_qualified_identifier(alias, multiplier)}), 0)"

    measure = metric.get("measure")
    measure = _approved_column(measure, allowed, blocked)
    quoted_measure = _qualified_identifier(alias, measure)
    if aggregation == "sum":
        return f"COALESCE(SUM({quoted_measure}), 0)"
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {quoted_measure})"
    if aggregation == "sum_positive":
        return f"COALESCE(SUM(CASE WHEN {quoted_measure} > 0 THEN {quoted_measure} ELSE 0 END), 0)"
    if aggregation == "min":
        return f"MIN({quoted_measure})"
    if aggregation == "max":
        return f"MAX({quoted_measure})"
    if aggregation == "days_since_min":
        return f"DATEDIFF(CURDATE(), MIN({quoted_measure}))"
    if aggregation in {"max_days_over", "sum_positive_difference", "sum_positive_difference_product"}:
        if joined_alias is None or not isinstance(joined_dataset, Mapping):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "派生指标缺少受控关联。")
        joined_allowed = {str(item) for item in joined_dataset.get("allowed_columns") or []}
        joined_blocked = _blocked_columns(datasets_contract, joined_dataset)
        subtract_measure = _approved_column(metric.get("subtract_measure"), joined_allowed, joined_blocked)
        joined_expression = _qualified_identifier(joined_alias, subtract_measure)
        if aggregation == "max_days_over":
            return f"GREATEST(COALESCE(MAX(DATEDIFF(CURDATE(), {quoted_measure}) - {joined_expression}), 0), 0)"
        difference = f"GREATEST({quoted_measure} - {joined_expression}, 0)"
        if aggregation == "sum_positive_difference":
            return f"COALESCE(SUM({difference}), 0)"
        multiplier = _approved_column(metric.get("multiplier"), allowed, blocked)
        return f"COALESCE(SUM({difference} * {_qualified_identifier(alias, multiplier)}), 0)"
    raise QueryFailure("CONTRACT_UNAVAILABLE", "指标聚合方式不受支持。")


def _metric_dimension_definition(
    dimensions: Mapping[str, Any], metric: Mapping[str, Any], code: str
) -> Mapping[str, Any]:
    overrides = metric.get("dimension_overrides") or {}
    if not isinstance(overrides, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标维度覆盖定义无效。")
    definition = overrides.get(code, dimensions.get(code))
    if not isinstance(definition, dict):
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
        or len(requested_dimensions) > 5
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
    unit_policy = metric.get("unit_policy")
    requires_unit_scope = unit_policy == "group_or_filter" or (
        isinstance(unit_policy, Mapping)
        and unit_policy.get("mode") == "group_or_filter"
    )
    if requires_unit_scope and not (
        "unit" in requested_dimensions or "unit" in requested_filters
    ):
        raise QueryFailure("UNIT_SCOPE_REQUIRED", "数量指标必须按单位分组或限定单一单位。")

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
        _approved_column(time_field, base_allowed, base_blocked)
        if "period" in output_names:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "时间分组输出名称冲突。")
        qualified_time = _qualified_identifier("f", time_field)
        if time_bucket == "day":
            expression = f"DATE({qualified_time})"
        elif metric.get("time_granularity") == "month":
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
        for column in metric_join.get("required_not_null") or []:
            column = _approved_column(column, joined_allowed, joined_blocked)
            where.append(f"{_qualified_identifier(metric_join_alias, column)} IS NOT NULL")
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
            where.append(
                f"DATE_ADD({_qualified_identifier('f', date_column)}, "
                f"INTERVAL {_qualified_identifier(metric_join_alias, days_column)} DAY) < CURDATE()"
            )

    time_policy = str(metric.get("time_policy") or "")
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
            "month" if metric.get("time_granularity") == "month" else "date",
            max_days=_max_metric_range_days(),
        )
        _approved_column(time_field, base_allowed, base_blocked)
        quoted_time = _qualified_identifier("f", time_field)
        where.extend([f"{quoted_time} >= %s", f"{quoted_time} < %s"])
        where_params.extend([start, end])
        applied_time = {"start": start, "end": end, "source": "explicit"}
    else:
        default_range = _default_time_range(time_policy)
        if default_range and time_field:
            default_range = _metric_time_bounds(metric, default_range[0], default_range[1])
            default_range = _validate_time_bounds(
                default_range[0],
                default_range[1],
                "month" if metric.get("time_granularity") == "month" else "date",
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
            quoted_table = _quote_table(table)
            subquery_time = _quote_identifier(time_field)
            snapshot_offset = request.get("_snapshot_offset_months", 0)
            if not isinstance(snapshot_offset, int) or not 0 <= snapshot_offset <= 24:
                raise QueryFailure("INVALID_PLAN", "快照比较月份偏移无效。")
            if snapshot_offset:
                if dataset.get("kind") != "monthly_snapshot":
                    raise QueryFailure("INVALID_PLAN", "该数据集不支持按月快照比较。")
                where.append(
                    f"{quoted_time} = DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT((SELECT MAX({subquery_time}) "
                    f"FROM {quoted_table}), '-01'), '{_MYSQL_DAY_FORMAT}'), "
                    f"INTERVAL %s MONTH), '{_MYSQL_MONTH_FORMAT}')"
                )
                where_params.append(snapshot_offset)
                applied_time = {"source": "latest_snapshot_offset", "months_before": snapshot_offset}
            elif time_policy == "latest_non_null_snapshot":
                required_measure = _approved_column(
                    metric.get("snapshot_required_non_null"), base_allowed, base_blocked
                )
                where.append(
                    f"{quoted_time} = (SELECT MAX({subquery_time}) FROM {quoted_table} "
                    f"WHERE {_quote_identifier(required_measure)} IS NOT NULL)"
                )
                applied_time = {
                    "source": "latest_non_null_snapshot",
                    "required_measure": required_measure,
                }
            else:
                where.append(f"{quoted_time} = (SELECT MAX({subquery_time}) FROM {quoted_table})")
                applied_time = {"source": "latest_snapshot"}
        elif time_policy == "current_snapshot":
            applied_time = {"source": "current_snapshot"}
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
            if not value or len(value) > 50:
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

    evidence_columns = [
        f"{metric_sql} AS metric_value",
        f"COUNT(*) AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    completeness_measure = metric.get("completeness_measure")
    if completeness_measure is not None:
        completeness_measure = _approved_column(completeness_measure, base_allowed, base_blocked)
        completeness = _qualified_identifier("f", completeness_measure)
        missing = f"SUM(CASE WHEN {completeness} IS NULL THEN 1 ELSE 0 END)"
        known = f"SUM(CASE WHEN {completeness} IS NOT NULL THEN 1 ELSE 0 END)"
        evidence_columns.extend([
            f"{missing} AS missing_value_count",
            f"{known} AS known_value_count",
            f"CASE WHEN COUNT(*) > 0 THEN {known} / COUNT(*) ELSE NULL END AS value_coverage_rate",
            "CASE WHEN COUNT(*) = 0 THEN 'missing' "
            f"WHEN {missing} = 0 THEN 'complete' "
            f"WHEN {known} = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
        ])
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
) -> tuple[str, list[Any], dict[str, Any]]:
    comparison = request.get("comparison")
    if not isinstance(comparison, dict) or request.get("time_bucket") is not None:
        raise QueryFailure("INVALID_PLAN", "期间比较不能与时间分组同时使用。")
    kind = comparison.get("kind")
    current_request = dict(request)
    prior_request = dict(request)
    current_request.pop("comparison", None)
    prior_request.pop("comparison", None)
    if kind == "previous_period":
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
    elif kind == "snapshot_months_before":
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
            current_request, metric, datasets_contract, semantics
        )
        prior_sql, prior_params, prior_scope = _build_ratio_metric_core(
            prior_request, metric, datasets_contract, semantics
        )
    elif metric.get("components") is not None:
        current_sql, current_params, current_scope = _build_composite_metric_core(
            current_request, metric, datasets_contract, semantics
        )
        prior_sql, prior_params, prior_scope = _build_composite_metric_core(
            prior_request, metric, datasets_contract, semantics
        )
    else:
        current_sql, current_params, current_scope = _build_metric_core(
            current_request, metric, metric, datasets_contract, semantics
        )
        prior_sql, prior_params, prior_scope = _build_metric_core(
            prior_request, metric, metric, datasets_contract, semantics
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
    select = [
        *output_dimensions,
        "COALESCE(c.metric_value, 0) AS metric_value",
        "COALESCE(p.metric_value, 0) AS comparison_value",
        "COALESCE(c.metric_value, 0) - COALESCE(p.metric_value, 0) AS delta_value",
        "CASE WHEN COALESCE(p.metric_value, 0) > 0 THEN "
        "(COALESCE(c.metric_value, 0) - p.metric_value) / p.metric_value ELSE NULL END AS change_rate",
        f"COALESCE(c.{_INTERNAL_MATCH_COUNT}, 0) + COALESCE(p.{_INTERNAL_MATCH_COUNT}, 0) "
        f"AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
    row_select = ", ".join(select)
    row_source_sql = f"SELECT {row_select} {select_from}"
    embedded_partition_proof = dimensions and isinstance(
        request.get("decomposition_of_request_id"), str
    )
    if embedded_partition_proof:
        sql = (
            f"{ctes}SELECT partition_rows.*, "
            "SUM(partition_rows.metric_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_CURRENT)}, "
            "SUM(partition_rows.comparison_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_COMPARISON)}, "
            "SUM(partition_rows.delta_value) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_DELTA)}, "
            "COUNT(*) OVER () AS "
            f"{_quote_identifier(_INTERNAL_PARTITION_ROW_COUNT)} "
            f"FROM ({row_source_sql}) AS partition_rows"
        )
    else:
        sql = f"{ctes}{row_source_sql}"
    order_by = request.get("order_by") or {"field": "delta_value", "direction": "desc"}
    if not isinstance(order_by, dict):
        raise QueryFailure("INVALID_PLAN", "比较排序定义无效。")
    field = order_by.get("field")
    direction = str(order_by.get("direction", "desc")).upper()
    if field not in {"metric_value", "comparison_value", "delta_value", "change_rate"} or direction not in {"ASC", "DESC"}:
        raise QueryFailure("INVALID_PLAN", "比较排序字段不受支持。")
    if dimensions:
        sql += f" ORDER BY {_quote_identifier(str(field))} {direction}"
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
    if embedded_partition_proof:
        scope["embedded_complete_partition_proof"] = {
            "version": "same-statement-window-partition-proof/v1",
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
    request: Mapping[str, Any], datasets_contract: Mapping[str, Any], semantics: Mapping[str, Any], limit: int
) -> tuple[str, list[Any], dict[str, Any]]:
    metric_code = request.get("metric")
    metrics = semantics.get("metrics")
    if not isinstance(metrics, dict) or metric_code not in metrics:
        raise QueryFailure("UNSUPPORTED_METRIC", "该指标尚未进入受控指标定义。")
    metric = metrics[metric_code]
    if not isinstance(metric, dict):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "指标定义格式无效。")
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
        if time_bucket is not None and (
            metric.get("query_kind") not in {"target_completion", "allocated_amount"}
            or time_bucket != "month"
        ):
            raise QueryFailure("INVALID_PLAN", "该分析指标不支持请求中的时间分组参数。")
        try:
            sql, params, scope = build_analytical_metric_query(
                request, metric, datasets_contract, semantics, limit
            )
            scope["inventory_scope"] = applied_inventory_scope
            return sql, params, scope
        except AnalysisQueryError as exc:
            raise QueryFailure(exc.code, exc.message) from exc

    if request.get("comparison") is not None:
        return _build_comparison_metric_query(request, metric, datasets_contract, semantics, limit)

    ratio = metric.get("ratio")
    components = metric.get("components")
    if ratio is None and components is None:
        sql, params, scope = _build_metric_core(
            request, metric, metric, datasets_contract, semantics
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
            request, metric, datasets_contract, semantics
        )
    else:
        sql, params, scope = _build_composite_metric_core(
            request, metric, datasets_contract, semantics
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
    select_sql = ", ".join(
        outer_dimensions
        + [
            f"COALESCE(SUM({_qualified_identifier('u', 'metric_value')}), 0) AS metric_value",
            f"COALESCE(SUM({_qualified_identifier('u', _INTERNAL_MATCH_COUNT)}), 0) "
            f"AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
        ]
    )
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
        request, numerator_metric, metric, datasets_contract, semantics
    )
    denominator_sql, denominator_params, denominator_scope = _build_metric_core(
        request, denominator_metric, metric, datasets_contract, semantics
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

    numerator_value = "COALESCE(n.metric_value, 0)"
    denominator_value = "COALESCE(d.metric_value, 0)"
    select = [
        *output_dimensions,
        f"CASE WHEN {denominator_value} > 0 THEN {numerator_value} / d.metric_value ELSE NULL END AS metric_value",
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


def _approved_column(column: Any, allowed: set[str], blocked: set[str]) -> str:
    if not isinstance(column, str) or _COLUMN_IDENTIFIER.fullmatch(column) is None:
        raise QueryFailure("INVALID_PLAN", "字段标识无效。")
    if column not in allowed or column.lower() in blocked:
        raise QueryFailure("COLUMN_NOT_ALLOWED", "查询引用了未批准或敏感字段。")
    return column


def _validate_pymysql_module(module: Any, vendor_root: Path):
    origin_text = str(getattr(module, "__file__", "") or "")
    if not origin_text:
        raise QueryFailure(
            "DEPENDENCY_UNTRUSTED",
            "PyMySQL 模块缺少可验证的制品来源。",
        )
    origin = Path(origin_text).resolve()
    version = tuple(getattr(module, "VERSION", ())[:3])
    connect = getattr(module, "connect", None)
    cursors = getattr(module, "cursors", None)
    if (
        not origin.is_relative_to(vendor_root)
        or version != (1, 2, 0)
        or not callable(connect)
        or cursors is None
        or not hasattr(cursors, "SSDictCursor")
    ):
        raise QueryFailure(
            "DEPENDENCY_UNTRUSTED",
            "PyMySQL 来源、版本或模块完整性与制品清单不一致。",
        )
    return module


def _load_pymysql():
    vendor_root = (Path(__file__).resolve().parent / "vendor").resolve()
    package_root = vendor_root / "pymysql"
    if not package_root.is_dir():
        raise QueryFailure(
            "DEPENDENCY_UNAVAILABLE",
            "查询组件缺少随制品分发的 PyMySQL，当前未连接数据库。",
        )
    with _PYMYSQL_IMPORT_LOCK:
        loaded = sys.modules.get("pymysql")
        if loaded is not None:
            return _validate_pymysql_module(loaded, vendor_root)
        sys.path.insert(0, str(vendor_root))
        try:
            pymysql = importlib.import_module("pymysql")
        except ImportError as exc:
            raise QueryFailure(
                "DEPENDENCY_UNAVAILABLE",
                "随制品分发的 PyMySQL 无法加载，当前未连接数据库。",
            ) from exc
        finally:
            try:
                sys.path.remove(str(vendor_root))
            except ValueError:
                pass
        return _validate_pymysql_module(pymysql, vendor_root)


def _connect(
    *,
    connect_timeout_seconds: int | None = None,
    read_timeout_seconds: int | None = None,
    timeout_seconds: int | None = None,
):
    pymysql = _load_pymysql()

    required = {
        "host": os.environ.get("DATA_QUERY_MYSQL_HOST", "").strip(),
        "database": os.environ.get("DATA_QUERY_MYSQL_DATABASE", "").strip(),
        "user": os.environ.get("DATA_QUERY_MYSQL_USER", "").strip(),
        "password": os.environ.get("DATA_QUERY_MYSQL_PASSWORD", ""),
    }
    if not all(required.values()):
        raise QueryFailure("CONFIGURATION_MISSING", "数据库连接配置不完整。")
    connect_timeout = _bounded_int("mysql_connect_timeout_seconds", 8, 1, 60)
    query_timeout = _bounded_int("mysql_query_timeout_seconds", 30, 1, 300)
    # Backward-compatible alias for source tests. Runtime call sites use the
    # independent connect/read budgets below.
    if timeout_seconds is not None:
        connect_timeout = min(connect_timeout, timeout_seconds)
        query_timeout = min(query_timeout, timeout_seconds)
    if connect_timeout_seconds is not None:
        connect_timeout = min(connect_timeout, connect_timeout_seconds)
    if read_timeout_seconds is not None:
        query_timeout = read_timeout_seconds
    try:
        connection = pymysql.connect(
            host=required["host"],
            port=_connection_port(),
            database=required["database"],
            user=required["user"],
            password=required["password"],
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=connect_timeout,
            read_timeout=query_timeout,
            write_timeout=query_timeout,
            cursorclass=pymysql.cursors.SSDictCursor,
            **mysql_tls_kwargs(),
        )
        tls_evidence = verify_mysql_tls(connection)
        grant_evidence = verify_mysql_read_only_grants(connection)
        connection._datasage_security_evidence = {
            **tls_evidence,
            **grant_evidence,
            "live_connection_verified": True,
        }
        verify_mysql_source_identity(
            connection,
            tls_evidence=tls_evidence,
            grant_evidence=grant_evidence,
            configured_identity={
                "host": required["host"],
                "port": _connection_port(),
                "database": required["database"],
                "user": required["user"],
            },
        )
        if tls_evidence["transport_mode"] == "plaintext":
            logger.warning(
                "datasage_database_plaintext_transport "
                "production_mode=false tls_verified=false"
            )
        logger.info(
            "datasage_database_security "
            "transport_mode=%s tls_required=%s tls_configured=%s "
            "tls_verified=%s tls_protocol=%s tls_cipher=%s "
            "grants_verified=%s grant_policy=%s observed_privileges=%s",
            tls_evidence["transport_mode"],
            tls_evidence["tls_required"],
            tls_evidence["tls_configured"],
            tls_evidence["tls_verified"],
            tls_evidence["tls_protocol"],
            tls_evidence["tls_cipher"],
            grant_evidence["grants_verified"],
            grant_evidence.get("grant_policy", "strict_object_read_only"),
            ",".join(
                grant_evidence.get(
                    "observed_privileges",
                    grant_evidence["read_only_privileges"],
                )
            ),
        )
        return connection
    except DatabaseSecurityError as exc:
        if "connection" in locals():
            connection.close()
        raise QueryFailure(exc.code, str(exc), stage="database_security") from exc
    except Exception as exc:
        if "connection" in locals():
            try:
                connection.close()
            except Exception:
                pass
        if isinstance(exc, ssl.SSLCertVerificationError):
            detail = str(
                getattr(exc, "verify_message", "") or exc
            ).casefold()
            code = (
                "DATABASE_TLS_IDENTITY_INVALID"
                if "hostname" in detail
                or "ip address mismatch" in detail
                or "doesn't match" in detail
                else "DATABASE_TLS_CERTIFICATE_INVALID"
            )
            raise QueryFailure(
                code,
                "数据库 TLS 证书或服务端身份验证失败。",
                stage="database_security",
            ) from exc
        errno = exc.args[0] if getattr(exc, "args", ()) else None
        if errno == 2026:
            raise QueryFailure(
                "DATABASE_TLS_NEGOTIATION_FAILED",
                "数据库服务端未完成强制 TLS 协商。",
                stage="database_security",
            ) from exc
        raise


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _bounded_int("max_cell_chars", 2000, 100, 20000):
            raise QueryFailure("OUTPUT_TOO_LARGE", "查询结果包含超长文本，已停止向模型传递。")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "查询结果包含非有限数值，已停止生成公开证据。",
                stage="result_validation",
            )
        return value
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "查询结果包含非有限数值，已停止生成公开证据。",
                stage="result_validation",
            )
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return _json_value(value.decode("utf-8", errors="replace"))
    return _json_value(str(value))


def _evidence_rows_and_state(
    rows: Sequence[Mapping[str, Any]], truncated: bool
) -> tuple[list[dict[str, Any]], str]:
    public_rows = [
        {
            str(key): value
            for key, value in row.items()
            if key not in _INTERNAL_RESULT_FIELDS
        }
        for row in rows
    ]
    if truncated:
        return public_rows, "truncated"
    if not rows:
        return [], "empty"
    matched_counts = [row.get(_INTERNAL_MATCH_COUNT) for row in rows if _INTERNAL_MATCH_COUNT in row]
    if matched_counts:
        try:
            matched = sum(int(value or 0) for value in matched_counts)
        except (TypeError, ValueError, OverflowError) as exc:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询证据覆盖计数无效。") from exc
        if matched == 0:
            return [], "empty"
    metric_states = [row.get("metric_data_state") for row in rows if "metric_data_state" in row]
    if metric_states:
        if any(state == "incomplete" for state in metric_states):
            return public_rows, "incomplete"
        if all(state == "missing" for state in metric_states):
            return public_rows, "undefined"
    if len(rows) == 1:
        if "metric_value" in rows[0] and rows[0]["metric_value"] is None:
            return public_rows, "undefined"
        values = list(public_rows[0].values())
        if len(values) == 1 and isinstance(values[0], (int, float, Decimal)) and values[0] == 0:
            return public_rows, "zero"
        if "metric_value" in rows[0]:
            metric_value = rows[0]["metric_value"]
            if isinstance(metric_value, (int, float, Decimal)) and not isinstance(metric_value, bool):
                if metric_value == 0:
                    return public_rows, "zero"
            elif isinstance(metric_value, str):
                try:
                    if Decimal(metric_value) == 0:
                        return public_rows, "zero"
                except Exception:
                    pass
    return public_rows, "rows"


def _execute_with_source(
    sql: str,
    params: Sequence[Any],
    limit: int,
    *,
    deadline_at: float | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    connection = None
    source_evidence_ref: dict[str, Any] | None = None
    expected_timeout = False
    try:
        remaining = None if deadline_at is None else deadline_at - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise QueryFailure("BATCH_DEADLINE_EXCEEDED", "本次批量查询已达到总时限。", timeout=True)
        configured_timeout = _bounded_int("mysql_query_timeout_seconds", 30, 1, 300)
        connect_budget = configured_timeout if remaining is None else min(
            configured_timeout, max(1, math.floor(remaining))
        )
        connection = _connect(
            connect_timeout_seconds=connect_budget,
            read_timeout_seconds=connect_budget,
        )
        remaining = None if deadline_at is None else deadline_at - time.monotonic()
        if remaining is not None and remaining <= 1:
            raise QueryFailure("BATCH_DEADLINE_EXCEEDED", "本次批量查询已达到总时限。", timeout=True)
        query_timeout = configured_timeout if remaining is None else min(
            configured_timeout, max(1, math.floor(remaining))
        )
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone = '+08:00'")
            cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (query_timeout * 1000,))
            cursor.execute("START TRANSACTION READ ONLY")
            source_evidence_ref = confirm_mysql_read_only_transaction(connection)
            cursor.execute(sql, tuple(params))
            raw_rows = cursor.fetchmany(limit + 1)
            if deadline_at is not None and time.monotonic() > deadline_at:
                raise QueryFailure("BATCH_DEADLINE_EXCEEDED", "本次批量查询已达到总时限。", timeout=True)
            truncated = len(raw_rows) > limit
            rows = raw_rows[:limit]
            return (
                [
                    {str(key): _json_value(value) for key, value in row.items()}
                    for row in rows
                ],
                truncated,
                source_evidence_ref,
            )
    except DatabaseSecurityError as exc:
        raise QueryFailure(
            "DATABASE_IDENTITY_CHANGED",
            "Database source identity changed during query execution.",
            stage="database_security",
            source_evidence_ref=source_evidence_ref,
        ) from exc
    except QueryFailure as failure:
        expected_timeout = failure.timeout
        if source_evidence_ref is not None:
            failure.source_evidence_ref = dict(source_evidence_ref)
        raise
    except Exception as exc:
        text = str(exc).lower()
        error_code = exc.args[0] if getattr(exc, "args", ()) else None
        server_timeout = error_code == 3024 or "maximum statement execution time exceeded" in text
        timeout = server_timeout or isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text
        expected_timeout = timeout
        failure = QueryFailure(
            "SERVER_STATEMENT_TIMEOUT" if server_timeout else "QUERY_TIMEOUT" if timeout else "QUERY_FAILED",
            "查询超时。" if timeout else "数据库查询失败。",
            timeout=timeout,
            source_evidence_ref=source_evidence_ref,
        )
        raise failure from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception as exc:
                logger.warning(
                    "datasage_query rollback_failed after_timeout=%s error_type=%s",
                    expected_timeout,
                    type(exc).__name__,
                )
            try:
                connection.close()
            except Exception as exc:
                logger.warning(
                    "datasage_query connection_close_failed after_timeout=%s error_type=%s",
                    expected_timeout,
                    type(exc).__name__,
                )


def _execute(
    sql: str,
    params: Sequence[Any],
    limit: int,
    *,
    deadline_at: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Compatibility facade for entity resolution and existing internal callers."""

    rows, truncated, _source_ref = _execute_with_source(
        sql, params, limit, deadline_at=deadline_at
    )
    return rows, truncated


def _database_query_failure(exc: Exception) -> QueryFailure:
    """Map driver failures without changing the public retry taxonomy."""

    text = str(exc).lower()
    error_code = exc.args[0] if getattr(exc, "args", ()) else None
    server_timeout = (
        error_code == 3024
        or "maximum statement execution time exceeded" in text
    )
    timeout = (
        server_timeout
        or isinstance(exc, TimeoutError)
        or "timeout" in text
        or "timed out" in text
    )
    return QueryFailure(
        (
            "SERVER_STATEMENT_TIMEOUT"
            if server_timeout
            else "QUERY_TIMEOUT"
            if timeout
            else "QUERY_FAILED"
        ),
        "Query timed out." if timeout else "Database query failed.",
        timeout=timeout,
    )


class _ConsistentSnapshotExecutor:
    """Execute a linked evidence group on one read-only consistent snapshot."""

    def __init__(self, *, deadline_at: float | None = None) -> None:
        self.deadline_at = deadline_at
        self.connection: Any = None
        self.marker: str | None = None
        self.expected_timeout = False
        self.closed = False
        self.poisoned_failure: QueryFailure | None = None
        self.source_evidence_ref: dict[str, Any] | None = None

    def __enter__(self) -> "_ConsistentSnapshotExecutor":
        try:
            remaining = (
                None
                if self.deadline_at is None
                else self.deadline_at - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                raise QueryFailure(
                    "BATCH_DEADLINE_EXCEEDED",
                    "The batch query deadline has been exceeded.",
                    timeout=True,
                )
            configured_timeout = _bounded_int(
                "mysql_query_timeout_seconds", 30, 1, 300
            )
            connect_budget = (
                configured_timeout
                if remaining is None
                else min(configured_timeout, max(1, math.floor(remaining)))
            )
            self.connection = _connect(
                connect_timeout_seconds=connect_budget,
                read_timeout_seconds=connect_budget,
            )
            remaining = (
                None
                if self.deadline_at is None
                else self.deadline_at - time.monotonic()
            )
            if remaining is not None and remaining <= 1:
                raise QueryFailure(
                    "BATCH_DEADLINE_EXCEEDED",
                    "The batch query deadline has been exceeded.",
                    timeout=True,
                )
            with self.connection.cursor() as cursor:
                cursor.execute("SET SESSION time_zone = '+08:00'")
                cursor.execute(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                cursor.execute(
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
                )
            self.source_evidence_ref = confirm_mysql_read_only_transaction(
                self.connection
            )
            self.marker = f"snapshot_group_{uuid.uuid4().hex}"
            return self
        except DatabaseSecurityError as exc:
            raise QueryFailure(
                "DATABASE_IDENTITY_CHANGED",
                "Database source identity changed during query execution.",
                stage="database_security",
                source_evidence_ref=self.source_evidence_ref,
            ) from exc
        except QueryFailure as failure:
            self.expected_timeout = failure.timeout
            self.close()
            raise
        except Exception as exc:
            failure = _database_query_failure(exc)
            self.expected_timeout = failure.timeout
            self.close()
            raise failure from exc

    def execute(
        self,
        sql: str,
        params: Sequence[Any],
        limit: int,
        *,
        deadline_at: float | None = None,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        effective_deadline = (
            deadline_at if deadline_at is not None else self.deadline_at
        )
        try:
            if self.poisoned_failure is not None:
                poisoned = self.poisoned_failure
                raise QueryFailure(
                    poisoned.code,
                    poisoned.message,
                    timeout=poisoned.timeout,
                    stage=poisoned.stage,
                    retryable=poisoned.retryable,
                    source_evidence_ref=self.source_evidence_ref,
                )
            if (
                self.connection is None
                or self.closed
                or self.marker is None
                or self.source_evidence_ref is None
            ):
                raise QueryFailure(
                    "QUERY_FAILED",
                    "Database query failed.",
                )
            remaining = (
                None
                if effective_deadline is None
                else effective_deadline - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                raise QueryFailure(
                    "BATCH_DEADLINE_EXCEEDED",
                    "The batch query deadline has been exceeded.",
                    timeout=True,
                )
            configured_timeout = _bounded_int(
                "mysql_query_timeout_seconds", 30, 1, 300
            )
            query_timeout = (
                configured_timeout
                if remaining is None
                else min(configured_timeout, max(1, math.floor(remaining)))
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s",
                    (query_timeout * 1000,),
                )
                cursor.execute(sql, tuple(params))
                raw_rows = cursor.fetchmany(limit + 1)
            if (
                effective_deadline is not None
                and time.monotonic() > effective_deadline
            ):
                raise QueryFailure(
                    "BATCH_DEADLINE_EXCEEDED",
                    "The batch query deadline has been exceeded.",
                    timeout=True,
                )
            truncated = len(raw_rows) > limit
            return (
                [
                    {
                        str(key): _json_value(value)
                        for key, value in row.items()
                    }
                    for row in raw_rows[:limit]
                ],
                truncated,
                dict(self.source_evidence_ref),
            )
        except QueryFailure as failure:
            self.expected_timeout = failure.timeout
            if self.source_evidence_ref is not None:
                failure.source_evidence_ref = dict(self.source_evidence_ref)
            if self.poisoned_failure is None:
                self.poisoned_failure = QueryFailure(
                    failure.code,
                    failure.message,
                    timeout=failure.timeout,
                    stage=failure.stage,
                    retryable=failure.retryable,
                    source_evidence_ref=failure.source_evidence_ref,
                )
            raise
        except Exception as exc:
            failure = _database_query_failure(exc)
            if self.source_evidence_ref is not None:
                failure.source_evidence_ref = dict(self.source_evidence_ref)
            self.expected_timeout = failure.timeout
            if self.poisoned_failure is None:
                self.poisoned_failure = QueryFailure(
                    failure.code,
                    failure.message,
                    timeout=failure.timeout,
                    stage=failure.stage,
                    retryable=failure.retryable,
                    source_evidence_ref=failure.source_evidence_ref,
                )
            raise failure from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.connection is None:
            return
        try:
            self.connection.rollback()
        except Exception as exc:
            logger.warning(
                "datasage_query rollback_failed after_timeout=%s error_type=%s",
                self.expected_timeout,
                type(exc).__name__,
            )
        try:
            self.connection.close()
        except Exception as exc:
            logger.warning(
                "datasage_query connection_close_failed after_timeout=%s error_type=%s",
                self.expected_timeout,
                type(exc).__name__,
            )

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _consistent_snapshot_executor(
    *, deadline_at: float | None = None
) -> _ConsistentSnapshotExecutor:
    return _ConsistentSnapshotExecutor(deadline_at=deadline_at)


def _audit(event: Mapping[str, Any]) -> None:
    safe = {
        key: event.get(key)
        for key in (
            "query_id",
            "request_id",
            "domain",
            "mode",
            "status",
            "data_state",
            "row_count",
            "elapsed_ms",
            "execution_retry_count",
            "preflight_status",
            "failure_stage",
            "error_code",
            "entity_preflight_elapsed_ms",
            "entity_resolution_db_call_count",
            "business_sql_count",
            "business_sql_attempted_count",
            "business_sql_confirmed_count",
            "session_ref",
            "task_ref",
        )
    }
    logger.info("datasage_query %s", json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def _audit_batch_capabilities(
    args: Mapping[str, Any],
    requests: Sequence[Any],
    results: Sequence[Mapping[str, Any]],
) -> None:
    """Record governed capability outcomes without business values or entities."""

    successful = sum(result.get("status") == "success" for result in results)
    failed = len(results) - successful
    linked = sum(
        isinstance(request, Mapping)
        and isinstance(request.get("decomposition_of_request_id"), str)
        for request in requests
    )
    reconciled = sum(
        isinstance(result.get("change_reconciliation"), Mapping)
        and result["change_reconciliation"].get("status") == "reconciled"
        for result in results
    )
    topics = {
        topic
        for result in results
        for topic in (
            result.get("allowed_reasoning_topics")
            if isinstance(result.get("allowed_reasoning_topics"), list)
            else []
        )
        if isinstance(topic, str)
    }
    error_codes = sorted({
        str(error["code"])
        for result in results
        if isinstance((error := result.get("error")), Mapping)
        and isinstance(error.get("code"), str)
    })
    safe = {
        "request_count": len(results),
        "success_count": successful,
        "failed_count": failed,
        "partial": 0 < successful < len(results),
        "linked_decomposition_count": linked,
        "reconciled_count": reconciled,
        "reasoning_topic_count": len(topics),
        "error_codes": error_codes,
    }
    _CAPABILITY_LOGGER.info(
        "datasage_query_batch %s",
        json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
    )


def _audit_ref(value: Any) -> str | None:
    """Return a stable, non-reversible reference for Hermes runtime context."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _retry_metadata(failure: QueryFailure) -> dict[str, Any]:
    retryable = (
        failure.retryable
        if failure.retryable is not None
        else failure.code
        in {
            "QUERY_TIMEOUT",
            "SERVER_STATEMENT_TIMEOUT",
            "BATCH_DEADLINE_EXCEEDED",
            "QUERY_CONCURRENCY_LIMIT",
            "DATABASE_UNAVAILABLE",
        }
    )
    if retryable:
        retry_after_seconds = (
            10
            if failure.code == "DATABASE_UNAVAILABLE"
            else 2
            if failure.code == "QUERY_CONCURRENCY_LIMIT"
            else 1
        )
        advice = (
            f"至少等待 {retry_after_seconds} 秒后最多重试一次；重试必须保持同一 request_id、"
            "指标、维度、筛选、时间和比较语义，不得缩减问题或替换指标。"
        )
    else:
        retry_after_seconds = None
        advice = "不可盲目重试；先根据错误修正请求，无法修正时直接说明失败。"
    metadata: dict[str, Any] = {
        "retryable": retryable,
        "max_retry_attempts": 1 if retryable else 0,
        "retry_advice": advice,
    }
    if retry_after_seconds is not None:
        metadata["retry_after_seconds"] = retry_after_seconds
    return metadata


def _business_metric_label(
    scope: Mapping[str, Any], semantics: Mapping[str, Any]
) -> str | None:
    """Return a model-usable label without exposing the internal metric code."""
    metric = scope.get("metric")
    if metric is None:
        return None
    metrics = semantics.get("metrics")
    definition = metrics.get(metric) if isinstance(metrics, Mapping) else None
    label = definition.get("label") if isinstance(definition, Mapping) else None
    if not isinstance(label, str) or not label.strip():
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少业务指标名称。")
    return label.strip()


def _business_metric_ref(request: Mapping[str, Any]) -> str | None:
    """Return a stable opaque metric identity without exposing contract codes."""
    domain = request.get("domain")
    metric = request.get("metric")
    if not isinstance(domain, str) or not isinstance(metric, str):
        return None
    return "metric_" + hashlib.sha256(
        f"{domain}\0{metric}".encode("utf-8")
    ).hexdigest()[:16]


_PUBLIC_FACT_FIELDS = {
    "metric_value",
    "comparison_value",
    "delta_value",
    "change_rate",
    "target_amount_rmb",
    "actual_amount_rmb",
    "gap_amount_rmb",
    "completion_rate",
    "known_value_count",
    "missing_value_count",
    "value_coverage_rate",
    "cost_turnover_days",
    "ddp_turnover_days",
    "avg_inventory_cost_rmb",
    "avg_inventory_ddp_rmb",
    "net_delivery_rmb",
    "period_natural_days",
    "snapshot_month_count",
    "receipt_coverage",
    "average_net_debt_rmb",
    "effective_month_count",
    "excluded_negative_bill_count",
    "excluded_open_balance_bill_count",
}
_PUBLIC_STATE_FIELDS = {
    "metric_data_state",
    "target_data_state",
    "actual_data_state",
    "period_state",
    "cost_turnover_state",
    "ddp_turnover_state",
}
_SCOPE_PRESENTATION_KEYS = {
    "request_id",
    "purpose",
    "analysis_intent",
    "evidence_role",
    "dimensions",
    "order_by",
    "limit",
    "decomposition_of_request_id",
    "_target_gap_of_request_id",
}
_KNOWN_REASONING_TOPICS = {
    "collection_timing",
    "credit_or_settlement",
    "customer_mix",
    "demand_timing",
    "delivery_execution",
    "external_event",
    "price_or_terms",
    "product_mix",
    "return_activity",
    "supply_availability",
    "target_or_plan_change",
}


def _semantic_request_fingerprint(
    request: Mapping[str, Any],
    *,
    scope_fingerprint: str | None = None,
) -> str:
    """Compatibility wrapper around the evidence-owned canonical contract."""

    return evidence.semantic_request_fingerprint(
        request,
        scope_fingerprint=scope_fingerprint,
    )


def _allowed_reasoning_topics(
    metric_definition: Mapping[str, Any],
) -> list[str]:
    """Return only versioned, metric-owned reasoning capabilities."""

    topics = metric_definition.get("reasoning_topics")
    decomposition = metric_definition.get("change_decomposition")
    if topics is None:
        return []
    if (
        not isinstance(topics, list)
        or not topics
        or any(not isinstance(topic, str) for topic in topics)
        or len(set(topics)) != len(topics)
        or not set(topics) <= _KNOWN_REASONING_TOPICS
        or not isinstance(decomposition, Mapping)
        or decomposition.get("mode") != "additive_partition"
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标原因推理能力合同无效。",
        )
    return list(topics)


def _safe_display_value(value: Any) -> str | None:
    """Project a dimension value to one bounded, non-deceptive display line."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    limit = _bounded_int("max_dimension_display_chars", 80, 16, 80)
    if len(text) <= limit:
        return text
    digest_length = min(16, max(1, limit - 1))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:digest_length]
    suffix = f"…{digest}"
    return text[: limit - len(suffix)] + suffix


def _public_scope_entities(
    resolved_entities: Sequence[Mapping[str, Any]],
    semantics: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Project governed filter identity without leaking execution identifiers."""

    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "业务域缺少维度语义。")
    public: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for resolved in resolved_entities:
        if not isinstance(resolved, Mapping):
            raise QueryFailure(
                "ENTITY_PUBLIC_IDENTITY_UNAVAILABLE",
                "实体筛选缺少可安全展示的治理身份。",
                stage="entity_preflight",
            )
        role = resolved.get("filter_role")
        definition = dimensions.get(role) if isinstance(role, str) else None
        label = definition.get("label") if isinstance(definition, Mapping) else None
        if (
            not isinstance(role, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role) is None
            or role == "id"
            or role.endswith(("_id", "_no", "_code", "_number"))
            or not isinstance(label, str)
        ):
            raise QueryFailure(
                "ENTITY_PUBLIC_IDENTITY_UNAVAILABLE",
                "实体筛选缺少可安全展示的治理角色。",
                stage="entity_preflight",
            )
        safe_label = _safe_display_value(label)
        raw_names = resolved.get("display_names")
        if not isinstance(raw_names, list) or not raw_names:
            raise QueryFailure(
                "ENTITY_PUBLIC_IDENTITY_UNAVAILABLE",
                "实体筛选缺少可安全展示的治理名称。",
                stage="entity_preflight",
            )
        internal_values = {
            str(value)
            for key in (
                "canonical_ids",
                "canonical_codes",
                "filter_values",
            )
            for value in (
                resolved.get(key)
                if isinstance(resolved.get(key), list)
                else []
            )
            if value is not None
        }
        accepted = 0
        for raw_name in raw_names:
            display_name = _safe_display_value(raw_name)
            if (
                safe_label is None
                or display_name is None
                or display_name in internal_values
            ):
                continue
            key = (role, display_name)
            if key not in seen:
                public.append(
                    {
                        "role": role,
                        "label": safe_label,
                        "display_name": display_name,
                    }
                )
                seen.add(key)
            accepted += 1
        if accepted == 0:
            raise QueryFailure(
                "ENTITY_PUBLIC_IDENTITY_UNAVAILABLE",
                "实体筛选仅有内部标识，不能进入公开证据。",
                stage="entity_preflight",
            )
    return public


def _scope_fingerprints(
    request: Mapping[str, Any],
    scope: Mapping[str, Any],
    metric_definition: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
) -> tuple[str, str]:
    """Hash the base population and the actual compiled projection plan."""

    join_plan = scope.get("join_plan") or []
    if not isinstance(join_plan, list):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询关联计划无效。")
    population_joins = [
        item
        for item in join_plan
        if isinstance(item, Mapping)
        and set(item.get("purposes") or []) & {"metric", "filter"}
    ]
    population_source_names = {
        str(value)
        for value in [
            scope.get("dataset"),
            *(scope.get("source_datasets") or []),
            *(
                item.get("target")
                for item in population_joins
                if isinstance(item, Mapping)
            ),
        ]
        if isinstance(value, str) and value
    }
    projection_source_names = {
        *population_source_names,
        *(
            str(item.get("target"))
            for item in join_plan
            if isinstance(item, Mapping)
            and isinstance(item.get("target"), str)
            and item.get("target")
        ),
    }
    dataset_catalog = datasets_contract.get("datasets")
    population_datasets = (
        {
            name: dataset_catalog[name]
            for name in sorted(population_source_names)
            if name in dataset_catalog
        }
        if isinstance(dataset_catalog, Mapping)
        else {}
    )
    projection_datasets = (
        {
            name: dataset_catalog[name]
            for name in sorted(projection_source_names)
            if name in dataset_catalog
        }
        if isinstance(dataset_catalog, Mapping)
        else {}
    )
    system_filters = scope.get("system_filters") or []
    if not isinstance(system_filters, list):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询系统过滤计划无效。")
    filter_plan = scope.get("filter_plan") or []
    if not isinstance(filter_plan, list) or any(
        not isinstance(item, Mapping) for item in filter_plan
    ):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询用户过滤计划无效。")
    population_system_filters = [
        item
        for item in system_filters
        if isinstance(item, Mapping)
        and item.get("dataset") in population_source_names
    ]
    applied_request = {
        str(key): value
        for key, value in request.items()
        if str(key) not in _SCOPE_PRESENTATION_KEYS
    }
    if request.get("domain") == "delivery" and request.get("mode") == "metric":
        applied_request["delivery_scope"] = request.get("delivery_scope") or "default_net"
    if request.get("domain") == "inventory" and request.get("mode") == "metric":
        applied_inventory_scope = scope.get("inventory_scope")
        if isinstance(applied_inventory_scope, str) and applied_inventory_scope:
            applied_request["inventory_scope"] = applied_inventory_scope
    population_projection = {
        "request": applied_request,
        "applied": {
            "time_range": scope.get("time_range"),
            "filters": scope.get("filters"),
            "filter_plan": filter_plan,
            "system_filters": population_system_filters,
        },
        "metric_contract": metric_definition,
        "selected_dataset_contracts": population_datasets,
        "sources": sorted(population_source_names),
        "population_joins": population_joins,
    }
    population_digest = hashlib.sha256(
        json.dumps(
            population_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    projection_digest = hashlib.sha256(
        json.dumps(
            {
                "population": population_projection,
                "dimension_outputs": scope.get("dimension_outputs") or [],
                "join_plan": join_plan,
                "selected_dataset_contracts": projection_datasets,
                "sources": sorted(projection_source_names),
                "system_filters": system_filters,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return (
        f"scope_{population_digest[:24]}",
        f"projection_{projection_digest[:24]}",
    )


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _validated_embedded_partition_proof(
    rows: Sequence[Mapping[str, Any]],
    *,
    truncated: bool,
    returned_row_count: int,
) -> dict[str, Any]:
    if not truncated or not rows or any(not isinstance(row, Mapping) for row in rows):
        raise QueryFailure(
            "PARTITION_PROOF_INVALID",
            "截断分区没有返回同语句窗口证明。",
            stage="result_validation",
        )
    first = rows[0]
    current = _finite_decimal(first.get(_INTERNAL_PARTITION_CURRENT))
    comparison = _finite_decimal(first.get(_INTERNAL_PARTITION_COMPARISON))
    delta = _finite_decimal(first.get(_INTERNAL_PARTITION_DELTA))
    raw_count = first.get(_INTERNAL_PARTITION_ROW_COUNT)
    try:
        full_count = int(raw_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QueryFailure(
            "PARTITION_PROOF_INVALID",
            "完整分区聚合证明缺少有效行数。",
            stage="result_validation",
        ) from exc
    for row in rows[1:]:
        if (
            _finite_decimal(row.get(_INTERNAL_PARTITION_CURRENT)) != current
            or _finite_decimal(row.get(_INTERNAL_PARTITION_COMPARISON))
            != comparison
            or _finite_decimal(row.get(_INTERNAL_PARTITION_DELTA)) != delta
        ):
            raise QueryFailure(
                "PARTITION_PROOF_INVALID",
                "同一结果集中的完整分区窗口证明不一致。",
                stage="result_validation",
            )
        try:
            row_count = int(row.get(_INTERNAL_PARTITION_ROW_COUNT))
        except (TypeError, ValueError, OverflowError) as exc:
            raise QueryFailure(
                "PARTITION_PROOF_INVALID",
                "同一结果集中的完整分区行数证明无效。",
                stage="result_validation",
            ) from exc
        if row_count != full_count:
            raise QueryFailure(
                "PARTITION_PROOF_INVALID",
                "同一结果集中的完整分区行数证明不一致。",
                stage="result_validation",
            )
    if (
        current is None
        or comparison is None
        or delta is None
        or delta != current - comparison
        or full_count <= returned_row_count
    ):
        raise QueryFailure(
            "PARTITION_PROOF_INVALID",
            "完整分区聚合证明与返回分区不一致。",
            stage="result_validation",
        )
    return {
        "version": "same-statement-window-partition-proof/v1",
        "metric_value": _json_value(current),
        "comparison_value": _json_value(comparison),
        "delta_value": _json_value(delta),
        "full_partition_row_count": full_count,
    }


def _comparison_is_complete(
    facts: Mapping[str, Any], states: Mapping[str, str]
) -> bool:
    values = [
        _finite_decimal(facts.get(field))
        for field in ("metric_value", "comparison_value", "delta_value")
    ]
    if any(value is None for value in values):
        return False
    complete_states = {
        "available",
        "complete",
        "current",
        "known",
        "non_empty",
        "valid",
    }
    return (
        values[2] == values[0] - values[1]
        and all(
        str(value).casefold() in complete_states for value in states.values()
        )
    )


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= scale * Decimal("0.000000001")


def _target_status_is_coherent(
    facts: Mapping[str, Any], states: Mapping[str, str]
) -> bool:
    target = _finite_decimal(facts.get("target_amount_rmb"))
    actual = _finite_decimal(facts.get("actual_amount_rmb"))
    gap = _finite_decimal(facts.get("gap_amount_rmb"))
    completion = _finite_decimal(facts.get("completion_rate"))
    metric_value = _finite_decimal(facts.get("metric_value"))
    target_state = str(states.get("target_data_state") or "").casefold()
    period_state = str(states.get("period_state") or "").casefold()
    if period_state in {"not_started", "includes_future", "future"}:
        return (
            target is not None
            and actual is None
            and gap is None
            and completion is None
            and metric_value is None
        )
    if target_state == "missing":
        return completion is None and metric_value is None and target in {None, Decimal("0")}
    if target_state == "incomplete":
        return completion is None and metric_value is None and gap is None
    if target_state == "zero":
        return (
            target == 0
            and actual is not None
            and gap is not None
            and _decimal_close(gap, -actual)
            and completion is None
            and metric_value is None
        )
    if target_state in {"set", "complete"}:
        if (
            target is None
            or target == 0
            or actual is None
            or gap is None
            or completion is None
        ):
            return False
        return (
            _decimal_close(gap, target - actual)
            and _decimal_close(completion, actual / target)
            and (
                metric_value is None
                or _decimal_close(metric_value, completion)
            )
        )
    return False


def _claim_ledger(
    request_id: str,
    metric_ref: str | None,
    metric_label: str,
    metric_unit: str | None,
    dimension_bindings: Sequence[Mapping[str, Any]],
    period: Mapping[str, Any],
    scope_fingerprint: str,
    projection_fingerprint: str,
    truncated: bool,
    rows: Sequence[Mapping[str, Any]],
    scope_entities: Sequence[Mapping[str, str]] = (),
) -> list[dict[str, Any]]:
    """Build canonical public evidence claims; renderers never infer raw rows."""

    ledger: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        facts = {
            field: _json_value(row[field])
            for field in _PUBLIC_FACT_FIELDS
            if field in row
        }
        states = {
            field: str(row[field])
            for field in _PUBLIC_STATE_FIELDS
            if field in row and row[field] is not None
        }
        dimensions: list[dict[str, str]] = []
        for binding in dimension_bindings:
            label = binding.get("label")
            fields = binding.get("fields")
            if isinstance(binding.get("field"), str) and not isinstance(fields, list):
                fields = [binding["field"]]
            if (
                not isinstance(label, str)
                or not isinstance(fields, list)
                or any(not isinstance(field, str) for field in fields)
            ):
                continue
            for field in fields:
                normalized_field = field.casefold()
                if (
                    normalized_field in {"id", "no"}
                    or normalized_field.startswith("_")
                    or normalized_field.endswith(("_id", "_no"))
                ):
                    continue
                display_value = _safe_display_value(row.get(field))
                if display_value is None:
                    continue
                dimensions.append({"label": label, "value": display_value})
                break
        period_value = _safe_display_value(row.get("period"))
        if period_value is not None and not any(
            dimension["value"] == period_value for dimension in dimensions
        ):
            dimensions.append({"label": "\u671f\u95f4", "value": period_value})
        relations = ["observation"]
        if _comparison_is_complete(facts, states):
            relations.append("period_comparison")
        if (
            ("completion_rate" in facts or "target_amount_rmb" in facts)
            and _target_status_is_coherent(facts, states)
        ):
            relations.append("target_status")
        if dimensions:
            relations.append("dimension_breakdown")
        ledger.append(
            {
                "claim_id": f"claim_unsealed_{index}",
                "claim_seal": "unsealed",
                "request_id": request_id,
                "metric_ref": metric_ref,
                "metric_label": metric_label,
                "dimensions": dimensions,
                "scope_entities": [dict(entity) for entity in scope_entities],
                "period": dict(period),
                "scope_fingerprint": scope_fingerprint,
                "projection_fingerprint": projection_fingerprint,
                "unit": metric_unit,
                "currency": (
                    "CNY"
                    if any(field.endswith("_rmb") for field in facts)
                    or metric_unit in {"人民币元", "元"}
                    else None
                ),
                "facts": facts,
                "states": states,
                "source_truncated": bool(truncated),
                "allowed_relations": relations,
            }
        )
    return ledger


def _disclosure_applies(
    disclosure: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    inventory_scope: str | None,
    data_state: str,
    truncated: bool,
) -> bool:
    """Evaluate only declared, structured result state; never inspect question text."""

    mode = disclosure.get("mode")
    if mode == "required_always":
        return True
    if mode == "contract_only":
        return False
    condition = disclosure.get("when")
    if mode != "required_when" or not isinstance(condition, Mapping):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标披露条件无效。",
            stage="contract_load",
        )
    allowed = {
        "data_state",
        "truncated",
        "dimension_present",
        "inventory_scope",
    }
    if set(condition) - allowed or not condition:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标披露条件超出治理范围。",
            stage="contract_load",
        )
    checks: list[bool] = []
    if "data_state" in condition:
        checks.append(condition["data_state"] == data_state)
    if "truncated" in condition:
        checks.append(
            isinstance(condition["truncated"], bool)
            and condition["truncated"] is truncated
        )
    if "dimension_present" in condition:
        dimension = condition["dimension_present"]
        dimensions = request.get("dimensions") or []
        checks.append(
            isinstance(dimension, str)
            and isinstance(dimensions, list)
            and dimension in dimensions
        )
    if "inventory_scope" in condition:
        expected_scope = condition["inventory_scope"]
        checks.append(
            isinstance(expected_scope, str)
            and inventory_scope == expected_scope
        )
    return bool(checks) and all(checks)


def _disclosure_ledger(
    *,
    request: Mapping[str, Any],
    metric_ref: str | None,
    metric_definition: Mapping[str, Any],
    datasets: Mapping[str, Any],
    scope_fingerprint: str,
    projection_fingerprint: str,
    data_state: str,
    truncated: bool,
    inherited_disclosures: Sequence[Mapping[str, Any]] = (),
    inventory_scope: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Build a typed, request-bound disclosure contract and seal its completeness."""

    metric_declarations = metric_definition.get("disclosures")
    if not isinstance(metric_declarations, list) or not isinstance(
        inherited_disclosures, Sequence
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标缺少显式披露分类。",
            stage="contract_load",
        )
    declarations = [*inherited_disclosures, *metric_declarations]
    # All declarations are validated here, but only public, potentially
    # required clauses are allowed to cross the model wire.
    ledger: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for order, declaration in enumerate(declarations):
        if not isinstance(declaration, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露声明无效。",
                stage="contract_load",
            )
        mode = declaration.get("mode")
        expected = {"id", "mode", "text"}
        if mode == "required_when":
            expected.add("when")
        if set(declaration) != expected or mode not in {
            "required_always",
            "required_when",
            "contract_only",
        }:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露声明无效。",
                stage="contract_load",
            )
        disclosure_id = declaration.get("id")
        text = declaration.get("text")
        if (
            not isinstance(disclosure_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", disclosure_id) is None
            or disclosure_id in identifiers
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露标识或内容无效。",
                stage="contract_load",
            )
        identifiers.add(disclosure_id)
        if mode == "contract_only":
            # Private clauses may name physical columns or SQL expressions.
            # The metric definition is already bound into the projection
            # fingerprint, so no private text is needed in the public seal.
            continue
        public_text = _business_safe_semantic_text(text, datasets)
        if public_text is None:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "公开指标披露包含实现层内容。",
                stage="contract_load",
            )
        item = {
            "disclosure_id": disclosure_id,
            "disclosure_seal": "unsealed",
            "contract_version": "metric-disclosure/v1",
            "request_id": str(request["request_id"]),
            "metric_ref": metric_ref,
            "scope_fingerprint": scope_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "mode": mode,
            "order": len(ledger),
            "text": public_text,
            "applies": _disclosure_applies(
                declaration,
                request=request,
                inventory_scope=inventory_scope,
                data_state=data_state,
                truncated=truncated,
            ),
        }
        seal = hashlib.sha256(
            json.dumps(
                {key: value for key, value in item.items() if key != "disclosure_seal"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        item["disclosure_seal"] = f"sha256_{seal}"
        ledger.append(item)
    ledger_seal = hashlib.sha256(
        json.dumps(
            {
                "contract_version": "metric-disclosure-ledger/v1",
                "request_id": str(request["request_id"]),
                "metric_ref": metric_ref,
                "scope_fingerprint": scope_fingerprint,
                "projection_fingerprint": projection_fingerprint,
                "ledger": ledger,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return ledger, f"sha256_{ledger_seal}"


def _decomposition_context(
    prepared: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(prepared, Mapping):
        return None
    request = prepared.get("request")
    semantics = prepared.get("semantics")
    if not isinstance(request, Mapping) or not isinstance(semantics, Mapping):
        return None
    metrics = semantics.get("metrics")
    metric = (
        metrics.get(request.get("metric"))
        if isinstance(metrics, Mapping)
        else None
    )
    if not isinstance(metric, Mapping):
        return None
    capability = metric.get("change_decomposition")
    if not isinstance(capability, Mapping):
        capability = {}
    dimensions = request.get("dimensions") or []
    return {
        "request": request,
        "dimensions": list(dimensions) if isinstance(dimensions, list) else [],
        "capability": dict(capability),
    }


def _claim_triplet(claim: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
    facts = claim.get("facts")
    states = claim.get("states")
    if not isinstance(facts, Mapping) or not isinstance(states, Mapping):
        return None
    if not _comparison_is_complete(facts, states):
        return None
    current = _finite_decimal(facts.get("metric_value"))
    comparison = _finite_decimal(facts.get("comparison_value"))
    delta = _finite_decimal(facts.get("delta_value"))
    if (
        current is None
        or comparison is None
        or delta is None
        or delta != current - comparison
    ):
        return None
    return current, comparison, delta


def _reasoning_evidence_eligible(claim: Mapping[str, Any]) -> bool:
    triplet = _claim_triplet(claim)
    relations = claim.get("allowed_relations")
    return (
        triplet is not None
        and triplet[2] != 0
        and claim.get("source_truncated") is False
        and isinstance(relations, list)
        and "period_comparison" in relations
    )


def _authorize_change_decompositions(
    contexts: Sequence[Mapping[str, Any] | None],
    results: list[dict[str, Any]],
) -> None:
    """Authorize only complete, same-scope, fully reconciled decompositions."""

    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result.get("request_id"), str)
    }
    context_by_id: dict[str, Mapping[str, Any]] = {}
    for context in contexts:
        if not isinstance(context, Mapping):
            continue
        request = context.get("request")
        if isinstance(request, Mapping) and isinstance(request.get("request_id"), str):
            context_by_id[request["request_id"]] = context
    for driver_id, context in context_by_id.items():
        request = context["request"]
        overall_id = request.get("decomposition_of_request_id")
        if not isinstance(overall_id, str) or not isinstance(driver_id, str):
            continue
        overall_context = context_by_id.get(overall_id)
        if not isinstance(overall_context, Mapping):
            continue
        overall_request = overall_context.get("request")
        overall_dimensions = overall_context.get("dimensions")
        driver_dimensions = context.get("dimensions")
        capability = context.get("capability")
        overall_capability = overall_context.get("capability")
        allowed_dimensions = (
            capability.get("dimensions")
            if isinstance(capability, Mapping)
            else None
        )
        if (
            driver_id == overall_id
            or not isinstance(overall_request, Mapping)
            or overall_request.get("decomposition_of_request_id") is not None
            or overall_dimensions != []
            or not isinstance(driver_dimensions, list)
            or len(driver_dimensions) != 1
            or not isinstance(capability, Mapping)
            or capability.get("mode") != "additive_partition"
            or capability != overall_capability
            or not isinstance(allowed_dimensions, list)
            or driver_dimensions[0] not in allowed_dimensions
        ):
            continue
        overall = result_by_id.get(overall_id)
        driver = result_by_id.get(driver_id)
        if (
            not isinstance(overall, dict)
            or not isinstance(driver, dict)
            or overall.get("status") != "success"
            or driver.get("status") != "success"
            or not isinstance(overall.get("_snapshot_group_marker"), str)
            or overall.get("_snapshot_group_marker")
            != driver.get("_snapshot_group_marker")
            or overall.get("truncated") is True
            or overall.get("scope_fingerprint")
            != driver.get("scope_fingerprint")
            or not isinstance(overall.get("projection_fingerprint"), str)
            or not isinstance(driver.get("projection_fingerprint"), str)
            or overall.get("business_metric_ref")
            != driver.get("business_metric_ref")
            or overall.get("applied_time_range")
            != driver.get("applied_time_range")
            or overall.get("business_metric_unit")
            != driver.get("business_metric_unit")
        ):
            continue
        overall_claims = overall.get("claim_ledger")
        driver_claims = driver.get("claim_ledger")
        partition_proof = driver.get("complete_partition_proof")
        bounded_partition = driver.get("truncated") is True
        proof_current = (
            _finite_decimal(partition_proof.get("metric_value"))
            if isinstance(partition_proof, Mapping)
            else None
        )
        proof_comparison = (
            _finite_decimal(partition_proof.get("comparison_value"))
            if isinstance(partition_proof, Mapping)
            else None
        )
        proof_delta = (
            _finite_decimal(partition_proof.get("delta_value"))
            if isinstance(partition_proof, Mapping)
            else None
        )
        proof_row_count = (
            partition_proof.get("full_partition_row_count")
            if isinstance(partition_proof, Mapping)
            else None
        )
        bounded_proof_valid = (
            bounded_partition
            and isinstance(partition_proof, Mapping)
            and partition_proof.get("version")
            == "same-statement-window-partition-proof/v1"
            and proof_current is not None
            and proof_comparison is not None
            and proof_delta is not None
            and proof_delta == proof_current - proof_comparison
            and isinstance(proof_row_count, int)
            and isinstance(driver_claims, list)
            and proof_row_count > len(driver_claims)
        )
        if bounded_partition and not bounded_proof_valid:
            continue
        if (
            not isinstance(overall_claims, list)
            or len(overall_claims) != 1
            or not isinstance(driver_claims, list)
            or not driver_claims
            or overall_claims[0].get("dimensions")
            or any(not claim.get("dimensions") for claim in driver_claims)
            or overall_claims[0].get("source_truncated") is True
            or (
                not bounded_proof_valid
                and any(
                    claim.get("source_truncated") is True
                    for claim in driver_claims
                )
            )
            or "period_comparison"
            not in overall_claims[0].get("allowed_relations", [])
            or any(
                "period_comparison" not in claim.get("allowed_relations", [])
                for claim in driver_claims
            )
        ):
            continue
        overall_triplet = _claim_triplet(overall_claims[0])
        driver_triplets = [_claim_triplet(claim) for claim in driver_claims]
        if overall_triplet is None or any(item is None for item in driver_triplets):
            continue
        overall_current, overall_comparison, overall_delta = overall_triplet
        if overall_delta == 0:
            continue
        complete_driver_triplets = [
            item for item in driver_triplets if item is not None
        ]
        returned_driver_current = sum(
            (item[0] for item in complete_driver_triplets), Decimal("0")
        )
        returned_driver_comparison = sum(
            (item[1] for item in complete_driver_triplets), Decimal("0")
        )
        returned_driver_delta = sum(
            (item[2] for item in complete_driver_triplets), Decimal("0")
        )
        driver_current = (
            proof_current if bounded_proof_valid else returned_driver_current
        )
        driver_comparison = (
            proof_comparison
            if bounded_proof_valid
            else returned_driver_comparison
        )
        driver_delta = proof_delta if bounded_proof_valid else returned_driver_delta
        if (
            driver_current != overall_current
            or driver_comparison != overall_comparison
            or driver_delta != overall_delta
        ):
            continue
        for claim, triplet in zip(driver_claims, complete_driver_triplets):
            delta = triplet[2]
            if delta != 0:
                claim["allowed_relations"].append("structural_contribution")
                claim["relation_semantics"] = {
                    "structural_contribution": "structural_not_causal"
                }
                claim["facts"]["net_change_contribution_rate"] = _json_value(
                    delta / overall_delta
                )
        driver["_change_reconciliation_pending"] = {
            "overall_request_id": overall_id,
            "snapshot_consistency": (
                "same_connection_repeatable_read_consistent_snapshot"
            ),
            "population_fingerprint": overall.get("scope_fingerprint"),
            "overall_projection_fingerprint": overall.get(
                "projection_fingerprint"
            ),
            "driver_projection_fingerprint": driver.get(
                "projection_fingerprint"
            ),
            "overall_current": _json_value(overall_current),
            "overall_comparison": _json_value(overall_comparison),
            "overall_delta": _json_value(overall_delta),
            "driver_current_sum": _json_value(driver_current),
            "driver_comparison_sum": _json_value(driver_comparison),
            "driver_delta_sum": _json_value(driver_delta),
            "proof_mode": (
                "same_statement_window_full_partition"
                if bounded_proof_valid
                else "returned_full_partition"
            ),
            "full_partition_row_count": (
                int(proof_row_count)
                if bounded_proof_valid
                else len(driver_claims)
            ),
            "returned_driver_row_count": len(driver_claims),
            "unreturned_driver_row_count": (
                int(proof_row_count) - len(driver_claims)
                if bounded_proof_valid
                else 0
            ),
            "complete_population_claims_returned": not bounded_proof_valid,
            "unreturned_current": _json_value(
                driver_current - returned_driver_current
            ),
            "unreturned_comparison": _json_value(
                driver_comparison - returned_driver_comparison
            ),
            "unreturned_delta": _json_value(
                driver_delta - returned_driver_delta
            ),
            "reconciliation_policy": (
                "same_statement_window_exact_three_column_additive_partition"
                if bounded_proof_valid
                else "exact_three_column_additive_partition"
            ),
        }


def _seal_claim_ids(results: Sequence[Mapping[str, Any]]) -> None:
    """Bind every final public claim field, including authorized relations."""

    for result in results:
        ledger = result.get("claim_ledger")
        if not isinstance(ledger, list):
            continue
        for claim in ledger:
            if not isinstance(claim, dict):
                continue
            evidence.seal_claim(claim)


def _tag_complete_decomposition_reconciliations(
    results: Sequence[Mapping[str, Any]],
    operation_partitions: Mapping[str, str],
) -> None:
    """Bind the explicit operation into a successful reconciliation seal."""

    for result in results:
        if (
            not isinstance(result, dict)
            or result.get("request_id") not in operation_partitions
        ):
            continue
        pending = result.get("_change_reconciliation_pending")
        if isinstance(pending, dict):
            pending["operation"] = "complete_change_decomposition"


def _seal_change_reconciliations(results: Sequence[Mapping[str, Any]]) -> None:
    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("request_id"), str)
    }
    for result in results:
        if not isinstance(result, dict):
            continue
        pending = result.pop("_change_reconciliation_pending", None)
        if not isinstance(pending, Mapping):
            continue
        overall = result_by_id.get(str(pending.get("overall_request_id")))
        overall_claims = overall.get("claim_ledger") if isinstance(overall, dict) else None
        driver_claims = result.get("claim_ledger")
        if (
            not isinstance(overall_claims, list)
            or len(overall_claims) != 1
            or not isinstance(driver_claims, list)
        ):
            continue
        authorized_driver_ids = [
            claim["claim_id"]
            for claim in driver_claims
            if isinstance(claim, Mapping)
            and "structural_contribution" in claim.get("allowed_relations", [])
        ]
        partition_claim_ids = [
            claim["claim_id"]
            for claim in driver_claims
            if isinstance(claim, Mapping)
        ]
        if not authorized_driver_ids:
            continue
        reconciliation = {
            "status": "reconciled",
            **dict(pending),
            "overall_claim_id": overall_claims[0]["claim_id"],
            "partition_claim_ids": partition_claim_ids,
            "driver_claim_ids": authorized_driver_ids,
            "driver_row_count": int(
                pending.get("full_partition_row_count") or len(driver_claims)
            ),
            "returned_driver_row_count": len(driver_claims),
            "returned_nonzero_driver_count": len(authorized_driver_ids),
            "nonzero_driver_count_scope": "returned_rows_only",
            # Compatibility alias. Its explicit scope prevents consumers from
            # interpreting it as a count over an unreturned proof-backed tail.
            "nonzero_driver_count": len(authorized_driver_ids),
        }
        evidence.seal_reconciliation(reconciliation)
        result["change_reconciliation"] = reconciliation


def _complete_decomposition_failure_reason(
    overall: Mapping[str, Any] | None,
    partition: Mapping[str, Any],
) -> str:
    partition_error = partition.get("error")
    if isinstance(partition_error, Mapping) and partition_error.get("code") == (
        "UNSUPPORTED_CHANGE_DECOMPOSITION"
    ):
        return "OPERATION_NOT_AUTHORIZED"
    if not isinstance(overall, Mapping) or overall.get("status") != "success":
        return "OVERALL_QUERY_UNAVAILABLE"
    if partition.get("status") != "success":
        return "PARTITION_QUERY_UNAVAILABLE"
    if (
        not isinstance(overall.get("_snapshot_group_marker"), str)
        or overall.get("_snapshot_group_marker")
        != partition.get("_snapshot_group_marker")
    ):
        return "SNAPSHOT_CONSISTENCY_UNPROVEN"
    if overall.get("truncated") is True:
        return "OVERALL_TRUNCATED"
    if (
        overall.get("scope_fingerprint") != partition.get("scope_fingerprint")
        or overall.get("business_metric_ref")
        != partition.get("business_metric_ref")
        or overall.get("applied_time_range")
        != partition.get("applied_time_range")
        or overall.get("business_metric_unit")
        != partition.get("business_metric_unit")
    ):
        return "SCOPE_MISMATCH"
    overall_claims = overall.get("claim_ledger")
    partition_claims = partition.get("claim_ledger")
    if partition.get("truncated") is True:
        if partition.get("complete_partition_proof_failure") is not None:
            return "PARTITION_PROOF_UNAVAILABLE"
        partition_proof = partition.get("complete_partition_proof")
        proof_row_count = (
            partition_proof.get("full_partition_row_count")
            if isinstance(partition_proof, Mapping)
            else None
        )
        proof_triplet = (
            (
                _finite_decimal(partition_proof.get("metric_value")),
                _finite_decimal(partition_proof.get("comparison_value")),
                _finite_decimal(partition_proof.get("delta_value")),
            )
            if isinstance(partition_proof, Mapping)
            and partition_proof.get("version")
            == "same-statement-window-partition-proof/v1"
            else None
        )
        valid_proof = (
            proof_triplet is not None
            and all(value is not None for value in proof_triplet)
            and proof_triplet[2] == proof_triplet[0] - proof_triplet[1]
            and isinstance(proof_row_count, int)
            and isinstance(partition_claims, list)
            and proof_row_count > len(partition_claims)
        )
        if not valid_proof:
            return "PARTITION_TRUNCATED"
        if not isinstance(overall_claims, list) or len(overall_claims) != 1:
            return "EVIDENCE_INCOMPLETE"
        overall_triplet = _claim_triplet(overall_claims[0])
        if overall_triplet is None:
            return "EVIDENCE_INCOMPLETE"
        if overall_triplet[2] == 0:
            return "OVERALL_CHANGE_ZERO"
        if proof_triplet != overall_triplet:
            return "PARTITION_DOES_NOT_RECONCILE"
        return "RECONCILIATION_NOT_ESTABLISHED"
    if (
        not isinstance(overall_claims, list)
        or len(overall_claims) != 1
        or not isinstance(partition_claims, list)
        or not partition_claims
    ):
        return "EVIDENCE_INCOMPLETE"
    overall_triplet = _claim_triplet(overall_claims[0])
    partition_triplets = [_claim_triplet(claim) for claim in partition_claims]
    if overall_triplet is None or any(item is None for item in partition_triplets):
        return "EVIDENCE_INCOMPLETE"
    if overall_triplet[2] == 0:
        return "OVERALL_CHANGE_ZERO"
    complete_triplets = [item for item in partition_triplets if item is not None]
    sums = tuple(
        sum((item[index] for item in complete_triplets), Decimal("0"))
        for index in range(3)
    )
    if sums != overall_triplet:
        return "PARTITION_DOES_NOT_RECONCILE"
    return "RECONCILIATION_NOT_ESTABLISHED"


def _finalize_complete_decomposition_outcomes(
    results: Sequence[Mapping[str, Any]],
    operation_partitions: Mapping[str, str],
) -> None:
    """Expose a typed fail-closed outcome only for the new explicit operation."""

    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("request_id"), str)
    }
    for partition_id, overall_id in operation_partitions.items():
        partition = result_by_id.get(partition_id)
        if not isinstance(partition, dict):
            continue
        reconciliation = partition.get("change_reconciliation")
        if (
            isinstance(reconciliation, Mapping)
            and reconciliation.get("status") == "reconciled"
        ):
            continue
        partition["change_reconciliation"] = {
            "status": "not_reconciled",
            "operation": "complete_change_decomposition",
            "reason_code": _complete_decomposition_failure_reason(
                result_by_id.get(overall_id),
                partition,
            ),
            "overall_request_id": overall_id,
        }


def _target_gap_claim_amounts(
    claim: Mapping[str, Any],
) -> tuple[Decimal, Decimal, Decimal, Decimal | None] | None:
    """Validate one target-status claim without aggregating its completion rate."""

    facts = claim.get("facts")
    states = claim.get("states")
    relations = claim.get("allowed_relations")
    if (
        not isinstance(facts, Mapping)
        or not isinstance(states, Mapping)
        or not isinstance(relations, list)
        or "target_status" not in relations
        or claim.get("source_truncated") is True
        or states.get("target_data_state") not in {"set", "zero"}
        or states.get("period_state") in {"not_started", "includes_future"}
        or not _target_status_is_coherent(facts, states)
    ):
        return None
    target = _finite_decimal(facts.get("target_amount_rmb"))
    actual = _finite_decimal(facts.get("actual_amount_rmb"))
    gap = _finite_decimal(facts.get("gap_amount_rmb"))
    completion = _finite_decimal(facts.get("completion_rate"))
    metric_value = _finite_decimal(facts.get("metric_value"))
    if (
        target is None
        or actual is None
        or gap is None
        or not _decimal_close(gap, target - actual)
    ):
        return None
    if target == 0:
        if facts.get("completion_rate") is not None or facts.get("metric_value") is not None:
            return None
        completion = None
    elif (
        completion is None
        or metric_value is None
        or not _decimal_close(metric_value, completion)
        or not _decimal_close(completion, actual / target)
    ):
        return None
    return target, actual, gap, completion


def _target_gap_failure_reason(
    overall: Mapping[str, Any] | None,
    partition: Mapping[str, Any],
) -> str:
    error = partition.get("error")
    if isinstance(error, Mapping) and error.get("code") == (
        "UNSUPPORTED_TARGET_GAP_DECOMPOSITION"
    ):
        return "OPERATION_NOT_AUTHORIZED"
    if not isinstance(overall, Mapping) or overall.get("status") != "success":
        return "OVERALL_QUERY_UNAVAILABLE"
    if partition.get("status") != "success":
        return "PARTITION_QUERY_UNAVAILABLE"
    if (
        not isinstance(overall.get("_snapshot_group_marker"), str)
        or overall.get("_snapshot_group_marker")
        != partition.get("_snapshot_group_marker")
    ):
        return "SNAPSHOT_CONSISTENCY_UNPROVEN"
    if overall.get("truncated") is True:
        return "OVERALL_TRUNCATED"
    if partition.get("truncated") is True:
        return "PARTITION_PROOF_UNAVAILABLE"
    if (
        overall.get("scope_fingerprint") != partition.get("scope_fingerprint")
        or overall.get("business_metric_ref")
        != partition.get("business_metric_ref")
        or overall.get("applied_time_range")
        != partition.get("applied_time_range")
        or overall.get("business_metric_unit")
        != partition.get("business_metric_unit")
    ):
        return "SCOPE_MISMATCH"
    overall_claims = overall.get("claim_ledger")
    partition_claims = partition.get("claim_ledger")
    if (
        not isinstance(overall_claims, list)
        or len(overall_claims) != 1
        or not isinstance(partition_claims, list)
        or not partition_claims
        or overall_claims[0].get("dimensions")
        or any(not claim.get("dimensions") for claim in partition_claims)
    ):
        return "EVIDENCE_INCOMPLETE"
    all_claims = [overall_claims[0], *partition_claims]
    if any(
        not isinstance(claim.get("states"), Mapping)
        or claim["states"].get("target_data_state")
        not in {"set", "zero"}
        or claim["states"].get("period_state")
        in {"not_started", "includes_future"}
        for claim in all_claims
    ):
        return "TARGET_STATE_INCOMPLETE"
    overall_amounts = _target_gap_claim_amounts(overall_claims[0])
    partition_amounts = [
        _target_gap_claim_amounts(claim) for claim in partition_claims
    ]
    if overall_amounts is None or any(item is None for item in partition_amounts):
        return "EVIDENCE_INCOMPLETE"
    complete = [item for item in partition_amounts if item is not None]
    sums = tuple(
        sum((item[index] for item in complete), Decimal("0"))
        for index in range(3)
    )
    if sums != overall_amounts[:3]:
        return "PARTITION_DOES_NOT_RECONCILE"
    return "RECONCILIATION_NOT_ESTABLISHED"


def _finalize_target_gap_decompositions(
    contexts: Sequence[Mapping[str, Any] | None],
    results: Sequence[Mapping[str, Any]],
    operation_partitions: Mapping[str, str],
) -> None:
    """Emit an independent amount reconciliation; never authorize causality."""

    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("request_id"), str)
    }
    request_by_id: dict[str, Mapping[str, Any]] = {}
    for context in contexts:
        request = context.get("request") if isinstance(context, Mapping) else None
        if isinstance(request, Mapping) and isinstance(request.get("request_id"), str):
            request_by_id[request["request_id"]] = request
    for partition_id, overall_id in operation_partitions.items():
        partition = result_by_id.get(partition_id)
        overall = result_by_id.get(overall_id)
        if not isinstance(partition, dict):
            continue
        reason = _target_gap_failure_reason(overall, partition)
        if reason != "RECONCILIATION_NOT_ESTABLISHED" or not isinstance(overall, Mapping):
            partition["target_gap_reconciliation"] = {
                "status": "not_reconciled",
                "operation": "complete_target_gap_decomposition",
                "reason_code": reason,
                "overall_request_id": overall_id,
                "causal_attribution_authorized": False,
            }
            continue
        overall_claim = overall["claim_ledger"][0]
        partition_claims = partition["claim_ledger"]
        overall_amounts = _target_gap_claim_amounts(overall_claim)
        partition_amounts = [
            _target_gap_claim_amounts(claim) for claim in partition_claims
        ]
        if overall_amounts is None or any(item is None for item in partition_amounts):
            continue
        complete = [item for item in partition_amounts if item is not None]
        sums = tuple(
            sum((item[index] for item in complete), Decimal("0"))
            for index in range(3)
        )
        request = request_by_id.get(partition_id)
        dimension = None
        if isinstance(request, Mapping):
            dimensions = request.get("dimensions")
            if isinstance(dimensions, list) and len(dimensions) == 1:
                dimension = dimensions[0]
        receipt = {
            "version": "datasage-target-gap-reconciliation/v1",
            "status": "reconciled",
            "operation": "complete_target_gap_decomposition",
            "overall_request_id": overall_id,
            "partition_request_id": partition_id,
            "dimension": dimension,
            "snapshot_consistency": "same_connection_repeatable_read_consistent_snapshot",
            "population_fingerprint": overall.get("scope_fingerprint"),
            "overall_projection_fingerprint": overall.get("projection_fingerprint"),
            "partition_projection_fingerprint": partition.get("projection_fingerprint"),
            "overall_target_amount_rmb": _json_value(overall_amounts[0]),
            "partition_target_sum_rmb": _json_value(sums[0]),
            "overall_actual_amount_rmb": _json_value(overall_amounts[1]),
            "partition_actual_sum_rmb": _json_value(sums[1]),
            "overall_gap_amount_rmb": _json_value(overall_amounts[2]),
            "partition_gap_sum_rmb": _json_value(sums[2]),
            "overall_completion_rate": _json_value(overall_amounts[3]),
            "completion_rate_basis": "overall_actual_amount_rmb / overall_target_amount_rmb",
            "completion_rate_aggregated": False,
            "full_partition_row_count": len(partition_claims),
            "proof_mode": "returned_full_partition",
            "overall_claim_id": overall_claim.get("claim_id"),
            "partition_claim_ids": [
                claim.get("claim_id") for claim in partition_claims
            ],
            "causal_attribution_authorized": False,
            "interpretation_boundary": "additive_gap_composition_not_causal",
        }
        evidence.seal_reconciliation(receipt)
        partition["target_gap_reconciliation"] = receipt


def _failure_metric_identity(
    raw_request: Any,
    prepared: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    request = (
        prepared.get("request")
        if isinstance(prepared, Mapping)
        and isinstance(prepared.get("request"), Mapping)
        else raw_request
    )
    if not isinstance(request, Mapping):
        return None, None
    metric_ref = _business_metric_ref(request)
    semantics = (
        prepared.get("semantics")
        if isinstance(prepared, Mapping)
        and isinstance(prepared.get("semantics"), Mapping)
        else None
    )
    if semantics is None and isinstance(request.get("domain"), str):
        try:
            _, semantics = _contracts(request["domain"])
        except (QueryFailure, OSError, ValueError, TypeError):
            semantics = None
    if not isinstance(semantics, Mapping):
        return metric_ref, None
    try:
        return metric_ref, _business_metric_label(
            {"metric": request.get("metric")}, semantics
        )
    except QueryFailure:
        return metric_ref, None


def _physical_contract_identifiers(datasets: Mapping[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    catalog = datasets.get("datasets")
    if not isinstance(catalog, Mapping):
        return identifiers
    for table, definition in catalog.items():
        if isinstance(table, str):
            identifiers.add(table)
            identifiers.update(table.split("."))
        if not isinstance(definition, Mapping):
            continue
        for key in (
            "allowed_columns",
            "forbidden_columns",
            "primary_key",
            "unique_key",
            "unique_keys",
        ):
            values = definition.get(key)
            if isinstance(values, list):
                identifiers.update(str(value) for value in values if isinstance(value, str))
    return {value for value in identifiers if value}


def _business_safe_semantic_text(value: Any, datasets: Mapping[str, Any]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Snake-case identifiers and SQL function syntax are implementation
    # evidence even when a newly added column was omitted from datasets.yaml.
    if re.search(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b", text) or re.search(
        r"\b(?:SUM|COUNT|AVG|MIN|MAX|DATEDIFF|CURDATE|CASE|COALESCE)\s*\(",
        text,
        flags=re.IGNORECASE,
    ):
        return None
    for identifier in _physical_contract_identifiers(datasets):
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        ):
            return None
    return text


def _business_safe_policy(
    value: Any,
    subject: str,
    datasets: Mapping[str, Any],
) -> str | None:
    if value == "group_or_filter":
        return f"必须按{subject}分组或限定单一{subject}。"
    if isinstance(value, str):
        policy = value.strip()
        if not policy or re.fullmatch(r"[a-z][a-z0-9_]*", policy) is not None:
            return None
        return _business_safe_semantic_text(policy, datasets)
    if isinstance(value, Mapping):
        business_rule = value.get("business_rule")
        if isinstance(business_rule, str):
            business_rule = business_rule.strip()
            # A policy identifier is implementation vocabulary, not an
            # explanation suitable for the answering model.
            if business_rule and re.fullmatch(r"[a-z][a-z0-9_]*", business_rule) is None:
                safe_business_rule = _business_safe_semantic_text(
                    business_rule,
                    datasets,
                )
                if safe_business_rule is not None:
                    return safe_business_rule
        if value.get("require_filter_or_group") is True:
            suffix = "，且不同币种不得直接相加。" if value.get("never_sum_mixed_currency") is True else "。"
            return f"必须按{subject}分组或限定单一{subject}{suffix}"
    return None


def _business_metric_context(
    scope: Mapping[str, Any],
    semantics: Mapping[str, Any],
    datasets: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only business-safe meaning; never formula, table, join, or measure."""

    metric = scope.get("metric")
    metrics = semantics.get("metrics")
    definition = metrics.get(metric) if isinstance(metrics, Mapping) else None
    if not isinstance(definition, Mapping):
        return {
            "business_metric_definition": None,
            "business_metric_unit": None,
            "business_metric_unit_policy": None,
            "business_metric_currency_policy": None,
            "business_metric_answer_note": None,
        }
    return {
        "business_metric_definition": _business_safe_semantic_text(
            definition.get("business_definition"), datasets
        ),
        "business_metric_unit": _business_safe_semantic_text(
            definition.get("unit"), datasets
        ),
        "business_metric_unit_policy": _business_safe_policy(
            definition.get("unit_policy"), "单位", datasets
        ),
        "business_metric_currency_policy": _business_safe_policy(
            definition.get("currency_policy"), "币种", datasets
        ),
        "business_metric_answer_note": _business_safe_semantic_text(
            definition.get("answer_note"), datasets
        ),
    }


def _business_dimension_labels(
    request: Mapping[str, Any], semantics: Mapping[str, Any]
) -> list[str]:
    """Translate validated dimension codes to business labels for the model."""
    requested = request.get("dimensions") or []
    if not requested:
        return []
    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return []
    labels: list[str] = []
    for dimension in requested:
        definition = dimensions.get(dimension)
        label = definition.get("label") if isinstance(definition, Mapping) else None
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def _business_dimension_bindings(
    request: Mapping[str, Any],
    scope: Mapping[str, Any],
    semantics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind each logical dimension to ordered, business-safe display fields."""

    requested = request.get("dimensions") or []
    outputs = scope.get("dimension_outputs") or []
    dimensions = semantics.get("dimensions")
    if (
        not isinstance(requested, list)
        or not isinstance(outputs, list)
        or not isinstance(dimensions, Mapping)
    ):
        return []
    public_outputs = {
        output for output in outputs if isinstance(output, str) and output
    }
    bindings: list[dict[str, Any]] = []
    for dimension in requested:
        definition = dimensions.get(dimension)
        label = definition.get("label") if isinstance(definition, Mapping) else None
        if not isinstance(definition, Mapping) or not isinstance(label, str):
            continue
        declared_outputs = [
            output
            for _column, output in _dimension_columns(definition)
            if output in public_outputs
        ]
        safe_outputs = [
            output
            for output in declared_outputs
            if output.casefold() != "id"
            and not output.casefold().endswith("_id")
        ]

        def display_priority(output: str) -> tuple[int, int]:
            lowered = output.casefold()
            if lowered.endswith(("_name", "_label", "_full_name")):
                rank = 0
            elif lowered.endswith(("_no", "_code", "_number")):
                rank = 1
            else:
                rank = 2
            return rank, declared_outputs.index(output)

        candidates = sorted(safe_outputs, key=display_priority)
        if candidates and label.strip():
            bindings.append(
                {
                    "dimension": str(dimension),
                    "fields": candidates,
                    "label": label.strip(),
                }
            )
    return bindings


def _public_time_range(value: Any) -> dict[str, Any]:
    """Validate and remove implementation-only fields from time evidence."""
    if not isinstance(value, Mapping):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少明确的时间或快照范围。")
    start, end = value.get("start"), value.get("end")
    if isinstance(start, str) and isinstance(end, str) and start < end:
        source = value.get("source")
        if not isinstance(source, str) or not source:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询时间范围来源无效。")
        return {"start": start, "end": end, "source": source}
    source = value.get("source")
    if source in {"current_snapshot", "latest_snapshot"}:
        return {"source": source}
    if source == "latest_non_null_snapshot" and isinstance(value.get("required_measure"), str):
        return {"source": source, "required_measure": value["required_measure"]}
    if source == "latest_complete_accounting_months" and isinstance(value.get("months"), int):
        return {"source": source, "months": value["months"]}
    if source == "latest_snapshot_offset" and isinstance(value.get("months_before"), int):
        return {"source": source, "months_before": value["months_before"]}
    nested: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询时间范围结构无效。")
        nested[key] = _public_time_range(item)
    if nested:
        return nested
    raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少明确的时间或快照范围。")


def _scope_texts(value: Any) -> list[str]:
    """Render validated time metadata as business language, preserving order."""
    if not isinstance(value, Mapping):
        return []
    start, end = value.get("start"), value.get("end")
    if isinstance(start, str) and isinstance(end, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}", end):
                inclusive_end = datetime.strptime(end, "%Y-%m") - timedelta(days=1)
                return [f"{start} 至 {inclusive_end.strftime('%Y-%m')}"]
            start_date = datetime.strptime(start, "%Y-%m-%d")
            inclusive_end = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)
            return [
                f"{start_date.strftime('%Y-%m-%d')} 至 "
                f"{inclusive_end.strftime('%Y-%m-%d')}"
            ]
        except ValueError:
            return []
    source = value.get("source")
    if source == "current_snapshot":
        return ["当前业务快照"]
    if source == "latest_snapshot":
        return ["最新可用月末快照"]
    if source == "latest_non_null_snapshot":
        return ["最新有值快照"]
    if source == "latest_complete_accounting_months" and isinstance(value.get("months"), int):
        return [f"最近 {value['months']} 个完整会计月"]
    if source == "latest_snapshot_offset" and isinstance(value.get("months_before"), int):
        return [f"最新可用月末快照前 {value['months_before']} 个月"]
    rendered: list[str] = []
    for nested in value.values():
        if not isinstance(nested, Mapping):
            continue
        for text in _scope_texts(nested):
            if text not in rendered:
                rendered.append(text)
    return rendered


def _answer_scope_line(results: Sequence[Mapping[str, Any]]) -> str | None:
    """Build one canonical scope declaration for all successful batch evidence."""
    rendered: list[str] = []
    for result in results:
        if result.get("status") != "success":
            continue
        for text in _scope_texts(result.get("applied_time_range")):
            if text not in rendered:
                rendered.append(text)
    return "查询范围：" + "；".join(rendered) if rendered else None


def _failure_result(
    request_id: str,
    failure: QueryFailure,
    elapsed_ms: int,
    *,
    business_metric_ref: str | None = None,
    business_metric_label: str | None = None,
    execution_retry_count: int = 0,
    entity_resolution_db_call_count: int = 0,
    entity_preflight_elapsed_ms: int = 0,
    business_sql_attempted_count: int = 0,
    business_sql_confirmed_count: int = 0,
    source_evidence_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    private_availability_failure = failure.code in {
        "DATA_RECONCILIATION_REQUIRED",
        "SEMANTIC_UNIT_RECONCILIATION_REQUIRED",
    }
    error = {"code": failure.code, "message": failure.message, **_retry_metadata(failure)}
    result = {
        "request_id": request_id,
        "status": "timeout" if failure.timeout else "failed",
        "data_state": None,
        "business_metric_ref": None if private_availability_failure else business_metric_ref,
        "business_metric_label": None if private_availability_failure else business_metric_label,
        "business_metric_definition": None,
        "business_metric_unit": None,
        "business_metric_unit_policy": None,
        "business_metric_currency_policy": None,
        "business_metric_answer_note": None,
        "business_dimension_labels": [],
        "resolved_entities": [],
        "rows": None,
        "claim_ledger": [],
        "disclosure_contract_version": None,
        "disclosure_ledger": [],
        "disclosure_ledger_seal": None,
        "row_count": None,
        "truncated": False,
        "applied_time_range": None,
        "error": error,
        "elapsed_ms": elapsed_ms,
        "execution_retry_count": execution_retry_count,
        "entity_resolution_db_call_count": entity_resolution_db_call_count,
        "entity_preflight_elapsed_ms": entity_preflight_elapsed_ms,
        "business_sql_attempted_count": business_sql_attempted_count,
        "business_sql_confirmed_count": business_sql_confirmed_count,
    }
    if source_evidence_ref is not None:
        result["source_evidence_ref"] = dict(source_evidence_ref)
    return result


def _consistent_source_evidence_ref(
    references: Sequence[Any],
) -> dict[str, Any] | None:
    validated: list[dict[str, Any]] = []
    for reference in references:
        try:
            validated.append(
                validate_mysql_source_evidence(reference, require_read_only=True)
            )
        except DatabaseSecurityError as exc:
            raise QueryFailure(
                "DATABASE_IDENTITY_CHANGED",
                "Database source identity changed during query execution.",
                stage="database_security",
            ) from exc
    if not validated:
        return None
    first = validated[0]
    if any(reference != first for reference in validated[1:]):
        raise QueryFailure(
            "DATABASE_IDENTITY_CHANGED",
            "Database source identity changed during query execution.",
            stage="database_security",
        )
    return first


def _batch_source_evidence_ref(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return one verified source ref or fail closed on execution drift."""

    executed = [
        result
        for result in results
        if int(result.get("business_sql_confirmed_count") or 0) > 0
        or result.get("source_evidence_ref") is not None
    ]
    if not executed:
        return None
    return _consistent_source_evidence_ref(
        [result.get("source_evidence_ref") for result in executed]
    )


def _attach_batch_source_evidence(
    payload: dict[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> None:
    reference = _batch_source_evidence_ref(results)
    if reference is not None:
        payload["source_evidence_ref"] = reference


_MODEL_WIRE_RESULT_FIELDS = (
    "request_id",
    "status",
    "data_state",
    "business_metric_ref",
    "business_metric_label",
    "business_dimension_labels",
    "scope_fingerprint",
    "projection_fingerprint",
    "claim_ledger",
    "disclosure_contract_version",
    "disclosure_ledger",
    "disclosure_ledger_seal",
    # Governed proof capabilities consumed by the typed-answer validator.
    "allowed_reasoning_topics",
    "change_reconciliation",
    "target_gap_reconciliation",
    "row_count",
    "truncated",
    "applied_time_range",
    "error",
)


_MODEL_WIRE_RECONCILIATION_FIELD_ALIASES = {
    "driver_projection_fingerprint": "contributor_projection_fingerprint",
    "driver_current_sum": "contributor_current_sum",
    "driver_comparison_sum": "contributor_comparison_sum",
    "driver_delta_sum": "contributor_delta_sum",
    "returned_driver_row_count": "returned_partition_row_count",
    "unreturned_driver_row_count": "unreturned_partition_row_count",
    "driver_claim_ids": "structural_contributor_claim_ids",
    "driver_row_count": "full_partition_row_count",
    "returned_nonzero_driver_count": "returned_nonzero_contributor_count",
    "nonzero_driver_count_scope": "nonzero_contributor_count_scope",
    "nonzero_driver_count": "nonzero_contributor_count",
}


def _model_wire_change_reconciliation(value: Any) -> Any:
    """Rename legacy reconciliation vocabulary only at the model boundary."""

    if not isinstance(value, Mapping):
        return value
    return {
        _MODEL_WIRE_RECONCILIATION_FIELD_ALIASES.get(key, key): item
        for key, item in value.items()
    }


def _model_wire_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project private execution state to the minimal model-visible result."""

    projected = {
        field: result.get(field)
        for field in _MODEL_WIRE_RESULT_FIELDS
        if field in result
    }
    if "change_reconciliation" in projected:
        projected["change_reconciliation"] = _model_wire_change_reconciliation(
            projected["change_reconciliation"]
        )
    return projected


_MODEL_WIRE_OPTIONAL_METRIC_CONTEXT_FIELDS = {
    "unit_policy": "business_metric_unit_policy",
    "currency_policy": "business_metric_currency_policy",
    "answer_note": "business_metric_answer_note",
}


def _model_wire_metric_contexts(
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate safe business meaning without repeating it in every result."""

    contexts_by_ref: dict[str, dict[str, Any]] = {}
    ordered_refs: list[str] = []
    for result in results:
        if result.get("status") != "success":
            continue
        metric_ref = result.get("business_metric_ref")
        label = result.get("business_metric_label")
        if not isinstance(metric_ref, str) or not metric_ref.strip():
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少业务指标引用。")
        if not isinstance(label, str) or not label.strip():
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少业务指标名称。")
        metric_ref = metric_ref.strip()
        context: dict[str, Any] = {
            "business_metric_ref": metric_ref,
            "label": label.strip(),
            "definition": (
                result["business_metric_definition"].strip()
                if isinstance(result.get("business_metric_definition"), str)
                and result["business_metric_definition"].strip()
                else None
            ),
            "unit": (
                result["business_metric_unit"].strip()
                if isinstance(result.get("business_metric_unit"), str)
                and result["business_metric_unit"].strip()
                else None
            ),
        }
        for public_field, private_field in (
            _MODEL_WIRE_OPTIONAL_METRIC_CONTEXT_FIELDS.items()
        ):
            value = result.get(private_field)
            if isinstance(value, str) and value.strip():
                context[public_field] = value.strip()
        previous = contexts_by_ref.get(metric_ref)
        if previous is None:
            contexts_by_ref[metric_ref] = context
            ordered_refs.append(metric_ref)
        elif previous != context:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "同一业务指标引用的语义上下文不一致。",
            )
    return [contexts_by_ref[metric_ref] for metric_ref in ordered_refs]


_CALCULATION_OPERATIONS = {"difference", "ratio", "share"}
_CALCULATION_FIELDS = {
    "calculation_id",
    "operation",
    "left_request_id",
    "right_request_id",
}


def _validate_calculations(
    raw_calculations: Any,
    request_ids: Sequence[str],
) -> list[dict[str, str]]:
    if raw_calculations is None:
        return []
    if not isinstance(raw_calculations, list) or not 1 <= len(raw_calculations) <= 10:
        raise QueryFailure(
            "INVALID_INPUT",
            "calculations 必须包含 1 到 10 个受治理计算。",
        )
    known_request_ids = set(request_ids)
    normalized: list[dict[str, str]] = []
    calculation_ids: set[str] = set()
    for raw in raw_calculations:
        if not isinstance(raw, Mapping) or set(raw) != _CALCULATION_FIELDS:
            raise QueryFailure(
                "INVALID_INPUT",
                "每个 calculation 只接受固定操作和两个 request_id 引用。",
            )
        calculation = {key: raw.get(key) for key in _CALCULATION_FIELDS}
        if any(
            not isinstance(value, str) or not 1 <= len(value) <= 64
            for value in calculation.values()
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "calculation 字段必须是 1 到 64 个字符的字符串。",
            )
        calculation_id = calculation["calculation_id"]
        operation = calculation["operation"]
        if operation not in _CALCULATION_OPERATIONS:
            raise QueryFailure("INVALID_INPUT", "不支持该受治理计算操作。")
        if calculation_id in calculation_ids:
            raise QueryFailure("INVALID_INPUT", "calculation_id 必须唯一。")
        if (
            calculation["left_request_id"] not in known_request_ids
            or calculation["right_request_id"] not in known_request_ids
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "计算操作数必须引用本批次中的 request_id。",
            )
        calculation_ids.add(calculation_id)
        normalized.append(calculation)
    return normalized


def _calculation_failure(
    calculation: Mapping[str, str],
    failure: QueryFailure,
) -> dict[str, Any]:
    return {
        "calculation_id": calculation["calculation_id"],
        "operation": calculation["operation"],
        "status": "failed",
        "allowed_relations": [],
        "operands": [
            {"request_id": calculation["left_request_id"]},
            {"request_id": calculation["right_request_id"]},
        ],
        "value": None,
        "unit": None,
        "error": {
            "code": failure.code,
            "message": failure.message,
            **_retry_metadata(failure),
        },
    }


def _normalized_calculation_filter_scope(value: Any) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise QueryFailure(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数缺少可验证的筛选范围。",
        )
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key:
            raise QueryFailure(
                "CALCULATION_SCOPE_UNVERIFIED",
                "计算操作数包含无法验证的筛选字段。",
            )
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        if not values or any(not _is_value_scalar(item) for item in values):
            raise QueryFailure(
                "CALCULATION_SCOPE_UNVERIFIED",
                "计算操作数包含无法验证的筛选值。",
            )
        tokens = {
            json.dumps(
                {
                    "type": type(item).__name__,
                    "value": _json_value(item),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in values
        }
        normalized[raw_key] = tuple(sorted(tokens))
    return dict(sorted(normalized.items()))


def _calculation_filter_fingerprint(
    filter_scope: Mapping[str, Sequence[str]],
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            filter_scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"filter_{digest[:24]}"


def _calculation_scope_contract(
    request: Mapping[str, Any],
    scope: Mapping[str, Any],
    metric_definition: Mapping[str, Any],
) -> dict[str, Any]:
    non_temporal_request = {
        str(key): value
        for key, value in request.items()
        if str(key)
        not in _SCOPE_PRESENTATION_KEYS
        | {"time_range", "comparison", "metric_filters", "_entity_bindings"}
    }
    basis = {
        "request": non_temporal_request,
        "metric_contract": metric_definition,
        "execution_contract": {
            key: scope.get(key)
            for key in (
                "dataset",
                "source_datasets",
                "join_plan",
                "system_filters",
                "inventory_scope",
            )
            if scope.get(key) is not None
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    change_decomposition = metric_definition.get("change_decomposition")
    share_dimensions = (
        change_decomposition.get("dimensions")
        if isinstance(change_decomposition, Mapping)
        and change_decomposition.get("mode") == "additive_partition"
        else []
    )
    return {
        "version": "governed-calculation-scope/v1",
        "metric_basis_fingerprint": f"basis_{digest[:24]}",
        "filter_scope": dict(request.get("metric_filters") or {}),
        "share_partition_dimensions": [
            str(dimension)
            for dimension in share_dimensions
            if isinstance(dimension, str)
        ],
    }


def _strict_subset_filter_dimensions(
    numerator: Mapping[str, Sequence[str]],
    denominator: Mapping[str, Sequence[str]],
) -> set[str] | None:
    strict_dimensions: set[str] = set()
    for key, denominator_values in denominator.items():
        numerator_values = numerator.get(key)
        if numerator_values is None:
            return None
        numerator_set = set(numerator_values)
        denominator_set = set(denominator_values)
        if not numerator_set.issubset(denominator_set):
            return None
        if numerator_set != denominator_set:
            strict_dimensions.add(key)
    strict_dimensions.update(set(numerator) - set(denominator))
    return strict_dimensions or None


def _calculation_operand(
    request_id: str,
    result_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = result_by_id.get(request_id)
    if not isinstance(result, Mapping) or result.get("status") != "success":
        raise QueryFailure(
            "CALCULATION_SOURCE_UNAVAILABLE",
            "计算引用的查询没有成功返回。",
        )
    claims = result.get("claim_ledger")
    if (
        not isinstance(claims, list)
        or len(claims) != 1
        or not isinstance(claims[0], Mapping)
        or claims[0].get("source_truncated") is not False
    ):
        raise QueryFailure(
            "CALCULATION_REQUIRES_SCALAR",
            "计算只接受一个未截断的标量 claim。",
        )
    claim = claims[0]
    dimensions = claim.get("dimensions")
    scope_entities = claim.get("scope_entities")
    period = claim.get("period")
    facts = claim.get("facts")
    unit = claim.get("unit")
    claim_id = claim.get("claim_id")
    scope_fingerprint = claim.get("scope_fingerprint")
    projection_fingerprint = claim.get("projection_fingerprint")
    calculation_scope = result.get("_calculation_scope")
    if dimensions != [] or not isinstance(scope_entities, list):
        raise QueryFailure(
            "CALCULATION_REQUIRES_SCALAR",
            "带分组维度的结果不能作为标量计算操作数。",
        )
    if (
        not isinstance(period, Mapping)
        or not isinstance(facts, Mapping)
        or not isinstance(claim_id, str)
        or not isinstance(unit, str)
        or not unit
    ):
        raise QueryFailure(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算操作数缺少完整的值、单位或范围证据。",
        )
    if (
        not isinstance(calculation_scope, Mapping)
        or calculation_scope.get("version")
        != "governed-calculation-scope/v1"
        or not isinstance(
            calculation_scope.get("metric_basis_fingerprint"), str
        )
        or not isinstance(scope_fingerprint, str)
        or not isinstance(projection_fingerprint, str)
        or not isinstance(
            calculation_scope.get("share_partition_dimensions"), list
        )
        or any(
            not isinstance(dimension, str)
            for dimension in calculation_scope.get(
                "share_partition_dimensions", []
            )
        )
    ):
        raise QueryFailure(
            "CALCULATION_SCOPE_UNVERIFIED",
            "计算操作数没有完整的受治理范围证明。",
        )
    filter_scope = _normalized_calculation_filter_scope(
        calculation_scope.get("filter_scope")
    )
    value = _finite_decimal(facts.get("metric_value"))
    if value is None:
        raise QueryFailure(
            "CALCULATION_VALUE_UNAVAILABLE",
            "计算操作数没有有限标量值。",
        )
    return {
        "request_id": request_id,
        "claim_id": claim_id,
        "metric_ref": claim.get("metric_ref"),
        "value": value,
        "unit": unit,
        "scope_fingerprint": scope_fingerprint,
        "projection_fingerprint": projection_fingerprint,
        "metric_basis_fingerprint": calculation_scope[
            "metric_basis_fingerprint"
        ],
        "filter_scope": filter_scope,
        "filter_fingerprint": _calculation_filter_fingerprint(filter_scope),
        "share_partition_dimensions": set(
            calculation_scope["share_partition_dimensions"]
        ),
        "period": dict(period),
        "scope_entities": [dict(item) for item in scope_entities],
    }


def _build_governed_calculations(
    calculations: Sequence[Mapping[str, str]],
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, Mapping)
        and isinstance(result.get("request_id"), str)
    }
    derived: list[dict[str, Any]] = []
    for calculation in calculations:
        try:
            left = _calculation_operand(
                calculation["left_request_id"], result_by_id
            )
            right = _calculation_operand(
                calculation["right_request_id"], result_by_id
            )
            same_period = left["period"] == right["period"]
            same_metric_basis = (
                left["metric_ref"] == right["metric_ref"]
                and left["metric_basis_fingerprint"]
                == right["metric_basis_fingerprint"]
            )
            same_filter_scope = left["filter_scope"] == right["filter_scope"]
            if left["unit"] != right["unit"]:
                raise QueryFailure(
                    "CALCULATION_UNIT_MISMATCH",
                    "未登记单位代数时，计算操作数必须使用相同单位。",
                )
            operation = calculation["operation"]
            if operation in {"difference", "ratio"}:
                if not (same_metric_basis and same_filter_scope):
                    raise QueryFailure(
                        "CALCULATION_SCOPE_MISMATCH",
                        "跨期差额或比率要求相同指标合同和完全相同的非时间筛选范围。",
                    )
                scope_compatibility = {
                    "rule": "same_metric_and_non_temporal_scope",
                    "same_metric_basis": True,
                    "same_filter_scope": True,
                    "period_relation": "same" if same_period else "different",
                    "same_unit": True,
                    "scalar_untruncated_operands": True,
                }
            else:
                if not (same_metric_basis and same_period):
                    raise QueryFailure(
                        "CALCULATION_SCOPE_MISMATCH",
                        "占比要求相同指标合同和相同期间。",
                    )
                subset_dimensions = _strict_subset_filter_dimensions(
                    left["filter_scope"], right["filter_scope"]
                )
                authorized_share_dimensions = (
                    left["share_partition_dimensions"]
                    & right["share_partition_dimensions"]
                )
                if (
                    not subset_dimensions
                    or not subset_dimensions.issubset(
                        authorized_share_dimensions
                    )
                ):
                    raise QueryFailure(
                        "CALCULATION_SUBSET_NOT_PROVEN",
                        "占比分子不是分母在已登记可加分区维度上的可证明严格子集。",
                    )
                scope_compatibility = {
                    "rule": "same_metric_period_and_proven_filter_subset",
                    "same_metric_basis": True,
                    "same_period": True,
                    "numerator_strict_subset_of_denominator": True,
                    "subset_dimensions": sorted(subset_dimensions),
                    "same_unit": True,
                    "scalar_untruncated_operands": True,
                }
            if operation == "difference":
                value = left["value"] - right["value"]
                output_unit = left["unit"]
            else:
                if operation == "share" and (
                    right["value"] <= 0
                    or left["value"] < 0
                    or left["value"] > right["value"]
                ):
                    raise QueryFailure(
                        "CALCULATION_SHARE_OUT_OF_RANGE",
                        "占比要求分母大于零，且分子必须在零到分母之间。",
                    )
                if right["value"] == 0:
                    raise QueryFailure(
                        "CALCULATION_DIVISION_BY_ZERO",
                        "比率或占比的分母不能为零。",
                    )
                value = left["value"] / right["value"]
                output_unit = "ratio" if operation == "ratio" else "proportion"
            operands = [
                {
                    "request_id": operand["request_id"],
                    "claim_id": operand["claim_id"],
                    "metric_ref": operand["metric_ref"],
                    "value": _json_value(operand["value"]),
                    "unit": operand["unit"],
                    "period": operand["period"],
                    "scope_entities": operand["scope_entities"],
                    "scope_fingerprint": operand["scope_fingerprint"],
                    "projection_fingerprint": operand[
                        "projection_fingerprint"
                    ],
                    "metric_basis_fingerprint": operand[
                        "metric_basis_fingerprint"
                    ],
                    "filter_fingerprint": operand["filter_fingerprint"],
                }
                for operand in (left, right)
            ]
            observation = {
                "calculation_id": calculation["calculation_id"],
                "operation": operation,
                "status": "success",
                "allowed_relations": ["derived_observation"],
                "relation_semantics": {
                    "derived_observation": (
                        "arithmetic_not_registered_metric_or_causal_evidence"
                    )
                },
                "operands": operands,
                "value": _json_value(value),
                "unit": output_unit,
                "scope_compatibility": scope_compatibility,
                "limitations": [
                    "NOT_A_REGISTERED_METRIC",
                    "NOT_STRUCTURAL_CONTRIBUTION",
                    "NOT_CAUSAL_EVIDENCE",
                ],
                "error": None,
            }
            digest = hashlib.sha256(
                json.dumps(
                    observation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            observation["calculation_seal"] = f"sha256_{digest}"
            derived.append(observation)
        except QueryFailure as failure:
            derived.append(_calculation_failure(calculation, failure))
    return derived


def _prepare_one(
    raw_request: Any,
    *,
    deadline_at: float | None,
    resolution_cache: dict[tuple[str, str], Any],
    max_unique_lookups: int,
    preflight_stats: dict[str, Any],
) -> dict[str, Any]:
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise QueryFailure(
            "BATCH_DEADLINE_EXCEEDED",
            "本次批量查询已达到总时限。",
            timeout=True,
            stage="batch_deadline",
        )
    try:
        request = _validate_inventory_metric_scope(
            _validate_delivery_metric_scope(_validate_request(raw_request))
        )
    except QueryFailure as exc:
        raise _at_stage(exc, "input_validation")
    try:
        datasets, semantics = _contracts(request["domain"])
    except QueryFailure as exc:
        raise _at_stage(exc, "contract_load")
    resolved_entities: list[dict[str, Any]] = []
    if request["mode"] == "metric":
        def exact_lookup(sql: str, params: Sequence[Any], limit: int):
            preflight_stats["entity_resolution_db_call_count"] = (
                preflight_stats.get("entity_resolution_db_call_count", 0) + 1
            )
            try:
                rows, truncated, source_ref = _execute_with_source(
                    sql, params, limit, deadline_at=deadline_at
                )
            except QueryFailure as failure:
                if failure.source_evidence_ref is not None:
                    source_refs = preflight_stats.setdefault(
                        "source_evidence_refs", []
                    )
                    source_refs.append(failure.source_evidence_ref)
                    _consistent_source_evidence_ref(source_refs)
                raise
            source_refs = preflight_stats.setdefault("source_evidence_refs", [])
            source_refs.append(source_ref)
            _consistent_source_evidence_ref(source_refs)
            return rows, truncated

        entity_started = time.monotonic()
        try:
            entities.prefetch_metric_entities(
                request,
                semantics,
                exact_lookup=exact_lookup,
                resolution_cache=resolution_cache,
                max_unique_lookups=max_unique_lookups,
            )
            request, resolved_entities = entities.canonicalize_metric_request(
                request,
                semantics,
                exact_lookup=exact_lookup,
                resolution_cache=resolution_cache,
                max_unique_lookups=max_unique_lookups,
            )
            request = _validate_metric_filter_value_contracts(request, semantics)
        except entities.EntityFailure as exc:
            stage = "contract_load" if exc.code == "CONTRACT_UNAVAILABLE" else "entity_preflight"
            raise QueryFailure(exc.code, exc.message, stage=stage) from exc
        except QueryFailure as exc:
            if exc.stage is None:
                exc.stage = (
                    "contract_load"
                    if exc.code == "CONTRACT_UNAVAILABLE"
                    else "entity_preflight"
                )
            raise
        finally:
            preflight_stats["entity_preflight_elapsed_ms"] = int(
                (time.monotonic() - entity_started) * 1000
            )
    return {
        "request": request,
        "datasets": datasets,
        "semantics": semantics,
        "resolved_entities": resolved_entities,
        "entity_resolution_db_call_count": preflight_stats.get(
            "entity_resolution_db_call_count", 0
        ),
    }


def _run_one(
    raw_request: Any,
    *,
    deadline_at: float | None = None,
    audit_context: Mapping[str, Any] | None = None,
    prepared: Mapping[str, Any] | None = None,
    preflight_failure: QueryFailure | None = None,
    preflight_db_call_count: int = 0,
    entity_preflight_elapsed_ms: int = 0,
    started_at: float | None = None,
    execute_query: Callable[
        ..., tuple[list[dict[str, Any]], bool, dict[str, Any]]
    ] | None = None,
    snapshot_group_marker: str | None = None,
    preflight_source_evidence_refs: Sequence[Any] = (),
) -> dict[str, Any]:
    started = started_at if started_at is not None else time.monotonic()
    query_id = f"dq_{uuid.uuid4().hex[:16]}"
    request_id = raw_request.get("request_id", "unknown") if isinstance(raw_request, dict) else "unknown"
    domain = raw_request.get("domain") if isinstance(raw_request, dict) else None
    mode = raw_request.get("mode") if isinstance(raw_request, dict) else None
    execution_retry_count = 0
    business_sql_attempted_count = 0
    business_sql_confirmed_count = 0
    source_evidence_ref: dict[str, Any] | None = None
    complete_partition_proof: dict[str, Any] | None = None
    complete_partition_proof_failure: str | None = None
    preflight_status = "passed" if preflight_failure is None else "failed"
    failure_stage = preflight_failure.stage if preflight_failure is not None else None
    current_stage = failure_stage or "input_validation"
    business_metric_ref, failure_metric_label = _failure_metric_identity(
        raw_request, prepared
    )
    try:
        source_evidence_ref = _consistent_source_evidence_ref(
            preflight_source_evidence_refs
        )
        if preflight_failure is not None:
            # Preserve the concrete validation/contract/entity failure that
            # just occurred; an expired shared deadline must not overwrite it.
            raise preflight_failure
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise QueryFailure(
                "BATCH_DEADLINE_EXCEEDED",
                "本次批量查询已达到总时限。",
                timeout=True,
                stage="batch_deadline",
            )
        if not isinstance(prepared, Mapping):
            raise QueryFailure("INTERNAL_ERROR", "查询预检结果不可用。", stage="contract_load")
        request = prepared["request"]
        datasets = prepared["datasets"]
        semantics = prepared["semantics"]
        resolved_entities = list(prepared.get("resolved_entities") or [])
        current_stage = "query_planning"
        environment_cap = _bounded_int("max_rows", 100, 1, 100)
        requested_limit = request.get("limit", environment_cap)
        if not isinstance(requested_limit, int):
            raise QueryFailure("INVALID_INPUT", "limit 必须是整数。")
        ranking_cap = 10 if request.get("order_by") is not None else environment_cap
        limit = max(1, min(ranking_cap, environment_cap, requested_limit))
        sql, params, scope = _build_metric_query(
            request, datasets, semantics, limit
        )
        applied_time_range = _public_time_range(scope.get("time_range"))
        current_stage = "business_sql"
        business_sql_attempted_count = 1
        executor = execute_query or _execute_with_source
        rows, truncated, business_source_evidence_ref = executor(
            sql, params, limit, deadline_at=deadline_at
        )
        source_evidence_ref = _consistent_source_evidence_ref(
            [*preflight_source_evidence_refs, business_source_evidence_ref]
        )
        business_sql_confirmed_count = 1
        proof_plan = scope.get("embedded_complete_partition_proof")
        if truncated and isinstance(proof_plan, Mapping):
            try:
                complete_partition_proof = _validated_embedded_partition_proof(
                    rows,
                    truncated=truncated,
                    returned_row_count=len(rows),
                )
            except QueryFailure as proof_failure:
                complete_partition_proof_failure = proof_failure.code
        current_stage = "result_validation"
        public_rows, data_state = _evidence_rows_and_state(rows, truncated)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        metric_ref = _business_metric_ref(request)
        metric_label = _business_metric_label(scope, semantics)
        metric_context = _business_metric_context(scope, semantics, datasets)
        dimension_labels = _business_dimension_labels(request, semantics)
        dimension_bindings = _business_dimension_bindings(request, scope, semantics)
        metrics = semantics.get("metrics")
        metric_definition = (
            metrics.get(request.get("metric"))
            if isinstance(metrics, Mapping)
            else None
        )
        if not isinstance(metric_definition, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标合同不可用。",
            )
        scope_fingerprint, projection_fingerprint = _scope_fingerprints(
            request,
            scope,
            metric_definition,
            datasets,
        )
        semantic_request_fingerprint = _semantic_request_fingerprint(
            request,
            scope_fingerprint=scope_fingerprint,
        )
        public_scope_entities = _public_scope_entities(
            resolved_entities,
            semantics,
        )
        claim_ledger = _claim_ledger(
            request["request_id"],
            metric_ref,
            metric_label,
            metric_context.get("business_metric_unit"),
            dimension_bindings,
            applied_time_range,
            scope_fingerprint,
            projection_fingerprint,
            truncated,
            public_rows,
            public_scope_entities,
        )
        disclosure_ledger, disclosure_ledger_seal = _disclosure_ledger(
            request=request,
            metric_ref=metric_ref,
            metric_definition=metric_definition,
            datasets=datasets,
            scope_fingerprint=scope_fingerprint,
            projection_fingerprint=projection_fingerprint,
            data_state=data_state,
            truncated=truncated,
            inherited_disclosures=semantics.get("default_disclosures") or (),
            inventory_scope=scope.get("inventory_scope"),
        )
        reasoning_topics = (
            _allowed_reasoning_topics(metric_definition)
            if any(
                _reasoning_evidence_eligible(claim)
                for claim in claim_ledger
            )
            else []
        )
        result = {
            "request_id": request["request_id"],
            "_calculation_scope": _calculation_scope_contract(
                request,
                scope,
                metric_definition,
            ),
            "status": "success",
            "data_state": data_state,
            "_semantic_request_fingerprint": semantic_request_fingerprint,
            "business_metric_ref": metric_ref,
            "business_metric_label": metric_label,
            **metric_context,
            "business_dimension_labels": dimension_labels,
            "business_dimension_bindings": dimension_bindings,
            "scope_fingerprint": scope_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "resolved_entities": resolved_entities,
            "rows": public_rows,
            "claim_ledger": claim_ledger,
            "disclosure_contract_version": "metric-disclosure-ledger/v1",
            "disclosure_ledger": disclosure_ledger,
            "disclosure_ledger_seal": disclosure_ledger_seal,
            "allowed_reasoning_topics": reasoning_topics,
            "change_reconciliation": None,
            "complete_partition_proof": complete_partition_proof,
            "complete_partition_proof_failure": complete_partition_proof_failure,
            "row_count": len(public_rows),
            "truncated": truncated,
            "applied_time_range": applied_time_range,
            "error": None,
            "elapsed_ms": elapsed_ms,
            "execution_retry_count": execution_retry_count,
            "entity_resolution_db_call_count": int(
                prepared.get("entity_resolution_db_call_count") or 0
            ),
            "entity_preflight_elapsed_ms": entity_preflight_elapsed_ms,
            "business_sql_attempted_count": business_sql_attempted_count,
            "business_sql_confirmed_count": business_sql_confirmed_count,
            "source_evidence_ref": source_evidence_ref,
        }
        if isinstance(snapshot_group_marker, str) and snapshot_group_marker:
            result["_snapshot_group_marker"] = snapshot_group_marker
        encoded_size = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > _bounded_int(
            "max_result_bytes", 262144, 16384, 1048576
        ):
            raise QueryFailure("OUTPUT_TOO_LARGE", "查询结果超过安全上下文上限，请缩小范围或减少明细列。")
    except QueryFailure as failure:
        source_evidence_ref = _consistent_source_evidence_ref(
            [
                *preflight_source_evidence_refs,
                *(
                    [failure.source_evidence_ref]
                    if failure.source_evidence_ref is not None
                    else []
                ),
            ]
        )
        if failure.stage is None:
            if failure.code == "CONTRACT_UNAVAILABLE":
                failure.stage = "contract_load"
            elif failure.code == "INVALID_INPUT":
                failure.stage = "input_validation"
            else:
                failure.stage = current_stage
        failure_stage = failure.stage
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = _failure_result(
            str(request_id),
            failure,
            elapsed_ms,
            business_metric_ref=business_metric_ref,
            business_metric_label=failure_metric_label,
            execution_retry_count=execution_retry_count,
            entity_resolution_db_call_count=preflight_db_call_count,
            entity_preflight_elapsed_ms=entity_preflight_elapsed_ms,
            business_sql_attempted_count=business_sql_attempted_count,
            business_sql_confirmed_count=business_sql_confirmed_count,
            source_evidence_ref=source_evidence_ref,
        )
    except Exception:
        logger.exception("datasage_query unexpected request failure query_id=%s", query_id)
        failure_stage = current_stage
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = _failure_result(
            str(request_id),
            QueryFailure("INTERNAL_ERROR", "当前子查询暂时不可用。"),
            elapsed_ms,
            business_metric_ref=business_metric_ref,
            business_metric_label=failure_metric_label,
            execution_retry_count=execution_retry_count,
            entity_resolution_db_call_count=preflight_db_call_count,
            entity_preflight_elapsed_ms=entity_preflight_elapsed_ms,
            business_sql_attempted_count=business_sql_attempted_count,
            business_sql_confirmed_count=business_sql_confirmed_count,
        )
    _audit(
        {
            "query_id": query_id,
            "request_id": request_id,
            "domain": domain,
            "mode": mode,
            "status": result["status"],
            "data_state": result["data_state"],
            "row_count": result["row_count"],
            "elapsed_ms": result["elapsed_ms"],
            "execution_retry_count": result["execution_retry_count"],
            "preflight_status": preflight_status,
            "failure_stage": failure_stage,
            "error_code": (
                result.get("error", {}).get("code")
                if isinstance(result.get("error"), dict)
                else None
            ),
            "entity_preflight_elapsed_ms": result.get("entity_preflight_elapsed_ms", 0),
            "entity_resolution_db_call_count": result.get("entity_resolution_db_call_count", 0),
            # Backward-compatible count now means a query completed its DB
            # execution/fetch boundary. Attempted and confirmed are explicit.
            "business_sql_count": business_sql_confirmed_count,
            "business_sql_attempted_count": business_sql_attempted_count,
            "business_sql_confirmed_count": business_sql_confirmed_count,
            "session_ref": (audit_context or {}).get("session_ref"),
            "task_ref": (audit_context or {}).get("task_ref"),
        }
    )
    return result


def _datasage_query_with_slot(args: dict[str, Any], **_kwargs: Any) -> str:
    """Validate, execute, and return structured evidence for one to ten requests."""
    batch_started = time.monotonic()
    query_slot_owned = bool(_kwargs.pop("_query_slot_owned", False))
    try:
        if (
            not isinstance(args, dict)
            or "requests" not in args
            or set(args) - {"requests", "calculations"}
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "查询参数只接受 requests 和可选 calculations。",
            )
        requests = args.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 10:
            raise QueryFailure("INVALID_INPUT", "requests 必须包含 1 到 10 个查询。")
        request_ids = [
            request.get("request_id") if isinstance(request, dict) else None
            for request in requests
        ]
        if any(
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 64
            for request_id in request_ids
        ):
            raise QueryFailure("INVALID_INPUT", "每个 request_id 必须是 1 到 64 个字符的字符串。")
        if len(set(request_ids)) != len(request_ids):
            raise QueryFailure("INVALID_INPUT", "同一次调用中的 request_id 必须唯一。")
        calculations = _validate_calculations(
            args.get("calculations"),
            [str(request_id) for request_id in request_ids],
        )
        requests, operation_partitions = _expand_complete_change_decompositions(
            requests
        )
        requests, target_gap_partitions = (
            _expand_complete_target_gap_decompositions(requests)
        )
        if len(requests) > 10:
            raise QueryFailure(
                "INVALID_INPUT",
                "Expanded complete decompositions exceed the ten-request execution budget.",
            )
        call_timeout = _bounded_int("call_timeout_seconds", 60, 1, 300)
        deadline_at = batch_started + call_timeout
        audit_context = {
            "session_ref": _audit_ref(_kwargs.get("session_id")),
            "task_ref": _audit_ref(_kwargs.get("task_id")),
        }
        resolution_cache: dict[tuple[str, str], Any] = {}
        max_unique_lookups = _bounded_int(
            "entity_preflight_max_lookups", 10, 1, 50
        )
        results = []
        prepared_contexts: list[dict[str, Any] | None] = []
        expanded_by_id = {
            str(request.get("request_id")): request
            for request in requests
            if isinstance(request, Mapping)
            and isinstance(request.get("request_id"), str)
        }
        linked_partitions = {
            str(request["request_id"]): str(
                request["decomposition_of_request_id"]
            )
            for request in requests
            if isinstance(request, Mapping)
            and isinstance(request.get("request_id"), str)
            and isinstance(request.get("decomposition_of_request_id"), str)
        }
        linked_partitions.update(
            {
                str(request["request_id"]): str(
                    request["_target_gap_of_request_id"]
                )
                for request in requests
                if isinstance(request, Mapping)
                and isinstance(request.get("request_id"), str)
                and isinstance(request.get("_target_gap_of_request_id"), str)
            }
        )
        complete_dimensions_by_overall = {
            overall_id: str(expanded_by_id[partition_id]["dimensions"][0])
            for partition_id, overall_id in operation_partitions.items()
        }
        complete_partition_by_overall = {
            overall_id: partition_id
            for partition_id, overall_id in operation_partitions.items()
        }
        target_gap_dimensions_by_overall = {
            overall_id: str(expanded_by_id[partition_id]["dimensions"][0])
            for partition_id, overall_id in target_gap_partitions.items()
        }
        target_gap_partition_by_overall = {
            overall_id: partition_id
            for partition_id, overall_id in target_gap_partitions.items()
        }
        operation_preflight_failures: dict[str, QueryFailure] = {}

        def execute_independent_request(
            request: Any,
        ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
            request_started = time.monotonic()
            stats = {
                "entity_resolution_db_call_count": 0,
                "entity_preflight_elapsed_ms": 0,
            }
            prepared: dict[str, Any] | None = None
            failure: QueryFailure | None = None
            try:
                prepared = _prepare_one(
                    request,
                    deadline_at=deadline_at,
                    resolution_cache=resolution_cache,
                    max_unique_lookups=max_unique_lookups,
                    preflight_stats=stats,
                )
            except QueryFailure as exc:
                failure = exc
            except Exception:
                logger.exception("datasage_query unexpected preflight failure")
                failure = QueryFailure(
                    "INTERNAL_ERROR",
                    "查询预检暂时不可用。",
                    stage="entity_preflight",
                )
            return (
                _decomposition_context(prepared),
                _run_one(
                    request,
                    deadline_at=deadline_at,
                    audit_context=audit_context,
                    prepared=prepared,
                    preflight_failure=failure,
                    preflight_db_call_count=stats["entity_resolution_db_call_count"],
                    entity_preflight_elapsed_ms=stats["entity_preflight_elapsed_ms"],
                    preflight_source_evidence_refs=stats.get(
                        "source_evidence_refs", ()
                    ),
                    started_at=request_started,
                ),
            )

        parallel_safe = (
            query_slot_owned
            and len(requests) > 1
            and not linked_partitions
            and all(
                isinstance(request, Mapping)
                and not request.get("metric_filters")
                for request in requests
            )
        )
        leased_slots = 0
        if parallel_safe:
            for _ in range(len(requests) - 1):
                if not _try_acquire_query_slot():
                    break
                leased_slots += 1
        if linked_partitions:
            entries: list[dict[str, Any]] = []
            entry_by_id: dict[str, dict[str, Any]] = {}
            for request in requests:
                request_started = time.monotonic()
                stats = {
                    "entity_resolution_db_call_count": 0,
                    "entity_preflight_elapsed_ms": 0,
                }
                prepared: dict[str, Any] | None = None
                failure: QueryFailure | None = None
                request_id = str(request.get("request_id"))
                try:
                    prepared = _prepare_one(
                        request,
                        deadline_at=deadline_at,
                        resolution_cache=resolution_cache,
                        max_unique_lookups=max_unique_lookups,
                        preflight_stats=stats,
                    )
                    if request_id in complete_dimensions_by_overall:
                        _validate_complete_decomposition_capability(
                            prepared,
                            complete_dimensions_by_overall[request_id],
                        )
                    if request_id in target_gap_dimensions_by_overall:
                        _validate_target_gap_decomposition_capability(
                            prepared,
                            target_gap_dimensions_by_overall[request_id],
                        )
                except QueryFailure as exc:
                    failure = exc
                except Exception:
                    logger.exception(
                        "datasage_query unexpected preflight failure"
                    )
                    failure = QueryFailure(
                        "INTERNAL_ERROR",
                        "Query preflight is temporarily unavailable.",
                        stage="entity_preflight",
                    )
                entry = {
                    "request": request,
                    "request_id": request_id,
                    "started_at": request_started,
                    "stats": stats,
                    "prepared": prepared,
                    "failure": failure,
                }
                entries.append(entry)
                entry_by_id[request_id] = entry

            # The explicit operation has an additional capability contract. If
            # its overall request cannot satisfy that contract, its generated
            # partition is blocked before any business statement is attempted.
            for partition_id, overall_id in operation_partitions.items():
                overall_entry = entry_by_id.get(overall_id)
                partition_entry = entry_by_id.get(partition_id)
                if (
                    overall_entry is not None
                    and partition_entry is not None
                    and overall_entry.get("failure") is not None
                ):
                    partition_entry["failure"] = overall_entry["failure"]
            for partition_id, overall_id in target_gap_partitions.items():
                overall_entry = entry_by_id.get(overall_id)
                partition_entry = entry_by_id.get(partition_id)
                if (
                    overall_entry is not None
                    and partition_entry is not None
                    and overall_entry.get("failure") is not None
                ):
                    partition_entry["failure"] = overall_entry["failure"]

            prepared_contexts.extend(
                _decomposition_context(entry.get("prepared"))
                for entry in entries
            )

            def run_prepared_entry(
                entry: Mapping[str, Any],
                *,
                execute_query: Callable[
                    ..., tuple[list[dict[str, Any]], bool, dict[str, Any]]
                ]
                | None = None,
                snapshot_group_marker: str | None = None,
                failure_override: QueryFailure | None = None,
            ) -> dict[str, Any]:
                stats = entry["stats"]
                return _run_one(
                    entry["request"],
                    deadline_at=deadline_at,
                    audit_context=audit_context,
                    prepared=entry.get("prepared"),
                    preflight_failure=(
                        failure_override or entry.get("failure")
                    ),
                    preflight_db_call_count=stats[
                        "entity_resolution_db_call_count"
                    ],
                    entity_preflight_elapsed_ms=stats[
                        "entity_preflight_elapsed_ms"
                    ],
                    preflight_source_evidence_refs=stats.get(
                        "source_evidence_refs", ()
                    ),
                    started_at=entry["started_at"],
                    execute_query=execute_query,
                    snapshot_group_marker=snapshot_group_marker,
                )

            partitions_by_overall: dict[str, list[str]] = {}
            for partition_id, overall_id in linked_partitions.items():
                if overall_id in entry_by_id:
                    partitions_by_overall.setdefault(overall_id, []).append(
                        partition_id
                    )
            result_by_request_id: dict[str, dict[str, Any]] = {}
            consumed: set[str] = set()

            # Process roots first so a manually supplied partition may precede
            # its overall request in the caller's list without changing the
            # required overall-then-partition statement order.
            root_ids = [
                entry["request_id"]
                for entry in entries
                if entry["request_id"] not in linked_partitions
            ]
            for root_id in root_ids:
                if root_id in consumed:
                    continue
                member_ids = [root_id, *partitions_by_overall.get(root_id, [])]
                member_entries = [
                    entry_by_id[member_id]
                    for member_id in member_ids
                    if member_id in entry_by_id and member_id not in consumed
                ]
                is_linked_group = len(member_entries) > 1
                has_preflight_failure = any(
                    entry.get("failure") is not None
                    for entry in member_entries
                )
                if not is_linked_group or has_preflight_failure:
                    for entry in member_entries:
                        result_by_request_id[entry["request_id"]] = (
                            run_prepared_entry(entry)
                        )
                        consumed.add(entry["request_id"])
                    continue
                try:
                    with _consistent_snapshot_executor(
                        deadline_at=deadline_at
                    ) as snapshot:
                        for entry in member_entries:
                            result_by_request_id[entry["request_id"]] = (
                                run_prepared_entry(
                                    entry,
                                    execute_query=snapshot.execute,
                                    snapshot_group_marker=snapshot.marker,
                                )
                            )
                            consumed.add(entry["request_id"])
                except QueryFailure as failure:
                    for entry in member_entries:
                        result_by_request_id[entry["request_id"]] = (
                            run_prepared_entry(
                                entry, failure_override=failure
                            )
                        )
                        consumed.add(entry["request_id"])

            # Invalid or nested manual links are deliberately not authorized;
            # still return their independent evidence/failure in input order.
            for entry in entries:
                request_id = entry["request_id"]
                if request_id not in consumed:
                    result_by_request_id[request_id] = run_prepared_entry(entry)
                    consumed.add(request_id)
            results.extend(
                result_by_request_id[entry["request_id"]]
                for entry in entries
            )
        elif leased_slots:
            try:
                with ThreadPoolExecutor(max_workers=leased_slots + 1) as executor:
                    executed = list(
                        executor.map(execute_independent_request, requests)
                    )
            finally:
                for _ in range(leased_slots):
                    _release_query_slot()
            prepared_contexts.extend(item[0] for item in executed)
            results.extend(item[1] for item in executed)
        else:
            for request in requests:
                # Complete operations stay sequential so an overall
                # capability failure deterministically blocks its partition.
                request_started = time.monotonic()
                stats = {
                    "entity_resolution_db_call_count": 0,
                    "entity_preflight_elapsed_ms": 0,
                }
                prepared: dict[str, Any] | None = None
                failure: QueryFailure | None = None
                request_id = (
                    str(request.get("request_id"))
                    if isinstance(request, Mapping)
                    else ""
                )
                try:
                    if request_id in operation_preflight_failures:
                        raise operation_preflight_failures[request_id]
                    prepared = _prepare_one(
                        request,
                        deadline_at=deadline_at,
                        resolution_cache=resolution_cache,
                        max_unique_lookups=max_unique_lookups,
                        preflight_stats=stats,
                    )
                    if request_id in complete_dimensions_by_overall:
                        _validate_complete_decomposition_capability(
                            prepared,
                            complete_dimensions_by_overall[request_id],
                        )
                    if request_id in target_gap_dimensions_by_overall:
                        _validate_target_gap_decomposition_capability(
                            prepared,
                            target_gap_dimensions_by_overall[request_id],
                        )
                except QueryFailure as exc:
                    failure = exc
                    partition_id = complete_partition_by_overall.get(request_id)
                    if partition_id is not None:
                        operation_preflight_failures[partition_id] = exc
                    target_partition_id = target_gap_partition_by_overall.get(
                        request_id
                    )
                    if target_partition_id is not None:
                        operation_preflight_failures[target_partition_id] = exc
                except Exception:
                    logger.exception("datasage_query unexpected preflight failure")
                    failure = QueryFailure(
                        "INTERNAL_ERROR",
                        "查询预检暂时不可用。",
                        stage="entity_preflight",
                    )
                prepared_contexts.append(_decomposition_context(prepared))
                results.append(
                    _run_one(
                        request,
                        deadline_at=deadline_at,
                        audit_context=audit_context,
                        prepared=prepared,
                        preflight_failure=failure,
                        preflight_db_call_count=stats[
                            "entity_resolution_db_call_count"
                        ],
                        entity_preflight_elapsed_ms=stats[
                            "entity_preflight_elapsed_ms"
                        ],
                        preflight_source_evidence_refs=stats.get(
                            "source_evidence_refs", ()
                        ),
                        started_at=request_started,
                    )
                )
        _authorize_change_decompositions(prepared_contexts, results)
        _tag_complete_decomposition_reconciliations(
            results,
            operation_partitions,
        )
        _seal_claim_ids(results)
        _seal_change_reconciliations(results)
        _finalize_complete_decomposition_outcomes(
            results,
            operation_partitions,
        )
        _finalize_target_gap_decompositions(
            prepared_contexts,
            results,
            target_gap_partitions,
        )
        calculation_results = _build_governed_calculations(
            calculations,
            results,
        )
        successful = sum(1 for result in results if result["status"] == "success")
        failed_calculations = sum(
            calculation.get("status") != "success"
            for calculation in calculation_results
        )
        overall = (
            "success"
            if successful == len(results) and failed_calculations == 0
            else "partial"
            if successful
            else "failed"
        )
        public_results = [_model_wire_result(result) for result in results]
        payload = {
            "status": overall,
            "request_count": len(results),
            "answer_scope_line": _answer_scope_line(results),
            "metric_contexts": _model_wire_metric_contexts(results),
            "evidence_bundle": evidence.build_evidence_bundle(requests, results),
            "results": public_results,
        }
        _attach_batch_source_evidence(payload, results)
        if calculations:
            payload["calculation_count"] = len(calculation_results)
            payload["calculations"] = calculation_results
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > _bounded_int(
            "max_batch_bytes", 524288, 65536, 2097152
        ):
            raise QueryFailure(
                "OUTPUT_TOO_LARGE",
                "本次批量证据超过安全上下文上限，请减少子问题或缩小明细范围。",
            )
        _audit_batch_capabilities(args, requests, results)
    except QueryFailure as failure:
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {"code": failure.code, "message": failure.message, **_retry_metadata(failure)},
        }
    except Exception:
        logger.exception("datasage_query unexpected handler failure")
        failure = QueryFailure("INTERNAL_ERROR", "查询工具暂时不可用。")
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {"code": failure.code, "message": failure.message, **_retry_metadata(failure)},
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def datasage_query(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute a bounded query call, failing fast when capacity is exhausted."""

    if not _try_acquire_query_slot():
        failure = QueryFailure(
            "QUERY_CONCURRENCY_LIMIT",
            "当前并行业务查询已达到安全上限，请等待正在执行的查询结束后再试。",
            stage="concurrency_guard",
        )
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {
                "code": failure.code,
                "message": failure.message,
                **_retry_metadata(failure),
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        internal_kwargs = dict(kwargs)
        internal_kwargs["_query_slot_owned"] = True
        return _datasage_query_with_slot(args, **internal_kwargs)
    finally:
        _release_query_slot()


def _public_readiness_code(reason_code: Any) -> str:
    """Collapse private readiness detail into a stable model-facing taxonomy."""

    internal = str(reason_code or "")
    if internal.startswith("HERMES_IDENTITY_"):
        return "HERMES_IDENTITY_UNVERIFIED"
    if internal == "DATABASE_CONFIGURATION_MISSING":
        return internal
    if internal.startswith("DATABASE_"):
        return "DATABASE_UNAVAILABLE"
    if internal.startswith("DEPENDENCY_"):
        return "DEPENDENCY_UNAVAILABLE"
    return "DATABASE_UNAVAILABLE"


def _readiness_retryable(reason_code: Any) -> bool:
    """Only connection availability is transient at the readiness boundary."""

    return str(reason_code or "") in {
        "DATABASE_CONNECTION_FAILED",
        "DATABASE_UNAVAILABLE",
    }


def runtime_guarded_datasage_query(
    args: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Official runtime facade: keep the schema visible, then fail closed."""

    started = time.monotonic()
    try:
        if (
            not isinstance(args, dict)
            or "requests" not in args
            or set(args) - {"requests", "calculations"}
        ):
            raise QueryFailure(
                "INVALID_INPUT",
                "查询参数只接受 requests 和可选 calculations。",
            )
        requests = args.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 10:
            raise QueryFailure("INVALID_INPUT", "requests 必须包含 1 到 10 个查询。")
        request_ids = [
            request.get("request_id") if isinstance(request, dict) else None
            for request in requests
        ]
        if any(
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 64
            for request_id in request_ids
        ):
            raise QueryFailure("INVALID_INPUT", "每个 request_id 必须是 1 到 64 个字符的字符串。")
        if len(set(request_ids)) != len(request_ids):
            raise QueryFailure("INVALID_INPUT", "同一次调用中的 request_id 必须唯一。")
        calculations = _validate_calculations(
            args.get("calculations"),
            [str(request_id) for request_id in request_ids],
        )
        for request in requests:
            _validate_inventory_metric_scope(
                _validate_delivery_metric_scope(_validate_request(request))
            )
        guarded_requests, guarded_operations = (
            _expand_complete_change_decompositions(requests)
        )
        guarded_requests, guarded_target_gap_operations = (
            _expand_complete_target_gap_decompositions(guarded_requests)
        )
        if len(guarded_requests) > 10:
            raise QueryFailure(
                "INVALID_INPUT",
                "Expanded complete decompositions exceed the ten-request execution budget.",
            )

        from . import runtime_health

        readiness = runtime_health.query_readiness_status()
        if readiness.get("ready"):
            return datasage_query(args, **kwargs)

        internal_reason_code = str(
            readiness.get("reason_code") or "DATABASE_UNAVAILABLE"
        )
        reason_code = _public_readiness_code(internal_reason_code)
        safe_messages = {
            "DATABASE_CONFIGURATION_MISSING": "业务数据库连接配置尚未完成。",
            "DATABASE_GRANTS_UNVERIFIED": "业务数据库只读权限尚未通过校验。",
            "DATABASE_SECURITY_EVIDENCE_MISSING": "业务数据库安全状态尚未通过校验。",
            "DATABASE_TLS_NEGOTIATION_FAILED": "业务数据库安全连接未能建立。",
            "DATABASE_TLS_REQUIRED": "业务数据库需要经过验证的安全连接。",
            "DEPENDENCY_UNAVAILABLE": "业务查询运行依赖暂时不可用。",
            "HERMES_IDENTITY_MISMATCH": "当前 Profile 运行身份未通过校验。",
            "HERMES_IDENTITY_UNVERIFIED": "当前 Profile 运行身份尚未通过校验。",
        }
        failure = QueryFailure(
            reason_code,
            safe_messages.get(
                reason_code,
                "受治理的业务查询当前暂时不可用。",
            ),
            stage="runtime_readiness",
            retryable=_readiness_retryable(internal_reason_code),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        results = [
            _failure_result(
                str(request.get("request_id")),
                failure,
                elapsed_ms,
                business_metric_ref=(
                    str(request.get("metric"))
                    if isinstance(request, Mapping)
                    and isinstance(request.get("metric"), str)
                    else None
                ),
                business_sql_attempted_count=0,
                business_sql_confirmed_count=0,
            )
            for request in guarded_requests
        ]
        _finalize_complete_decomposition_outcomes(
            results,
            guarded_operations,
        )
        _finalize_target_gap_decompositions(
            [
                {"request": request}
                for request in guarded_requests
                if isinstance(request, Mapping)
            ],
            results,
            guarded_target_gap_operations,
        )
        calculation_results = _build_governed_calculations(
            calculations,
            results,
        )
        logger.warning(
            "datasage_query readiness_blocked reason_code=%s "
            "public_reason_code=%s request_count=%d "
            "business_sql_attempted_count=0",
            internal_reason_code,
            reason_code,
            len(results),
        )
        public_results = [_model_wire_result(result) for result in results]
        payload = {
            "status": "failed",
            "request_count": len(results),
            "answer_scope_line": None,
            "metric_contexts": [],
            "evidence_bundle": evidence.build_evidence_bundle(
                guarded_requests, results
            ),
            "results": public_results,
        }
        if calculations:
            payload["calculation_count"] = len(calculation_results)
            payload["calculations"] = calculation_results
    except QueryFailure as failure:
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {
                "code": failure.code,
                "message": failure.message,
                **_retry_metadata(failure),
            },
        }
    except Exception:
        logger.exception("datasage_query runtime guard failed")
        failure = QueryFailure("INTERNAL_ERROR", "查询工具暂时不可用。")
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {
                "code": failure.code,
                "message": failure.message,
                **_retry_metadata(failure),
            },
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
