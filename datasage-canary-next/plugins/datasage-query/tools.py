"""Guarded query planning and execution for DataSage Expert.

The module intentionally exposes one handler and no lifecycle hooks. Business
semantics live in the plugin's versioned governed contracts; Skill references
only guide model workflow and answers. This module enforces the tool boundary
and returns structured evidence.
"""

from __future__ import annotations

import copy
import json
import hashlib
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .analytical_queries import AnalysisQueryError, build_analytical_metric_query, validate_frozen_pool_rows
from .query_errors import QueryFailure
from . import currency_basis
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
    _quote_identifier,
    _quote_table,
    _qualified_identifier,
)
from .query_builders import (
    _DATABASE_CURRENT_DATE_EVIDENCE,
    _DATABASE_QUERY_DATE_OBSERVATION,
    _INTERNAL_AS_OF_DATE,
    _INTERNAL_COMPARISON_SNAPSHOT_MONTH,
    _INTERNAL_PARTITION_COMPARISON,
    _INTERNAL_PARTITION_COMPARISON_KNOWN,
    _INTERNAL_PARTITION_COMPARISON_MISSING,
    _INTERNAL_PARTITION_CURRENT,
    _INTERNAL_PARTITION_CURRENT_KNOWN,
    _INTERNAL_PARTITION_CURRENT_MISSING,
    _INTERNAL_PARTITION_DELTA,
    _INTERNAL_PARTITION_ROW_COUNT,
    _INTERNAL_RESULT_FIELDS,
    _INTERNAL_SNAPSHOT_MONTH,
    _MATCHED_ELAPSED_COMPARISON_VERSION,
    _MYSQL_DAY_FORMAT,
    _MYSQL_MONTH_FORMAT,
    _SNAPSHOT_TIME_SOURCES,
    _add_months,
    _build_comparison_metric_query,
    _build_composite_metric_core,
    _build_metric_core,
    _build_metric_query,
    _build_ratio_metric_core,
    _business_today,
    _comparison_period,
    _default_time_range,
    _effective_dimensions,
    _ensure_metric_available,
    _ensure_metric_tree_available,
    _fixed_filter_refines,
    _governed_join,
    _max_group_dimensions,
    _max_metric_range_days,
    _merge_records,
    _metric_dimension_definition,
    _metric_order_clause,
    _metric_time_bounds,
    _metric_time_value_format,
    _next_month_start,
    _parse_time_boundary,
    _register_governed_join,
    _shift_calendar_year,
    _system_filter_record,
    _validate_governed_request_time_range,
    _validate_time_bounds,
    _year_over_year_matched_elapsed_ranges,
)
from .query_validation import (
    _calendar_month_time_range,
    _target_gap_contract,
    _validate_request,
)
from .query_execution import (
    _ConsistentSnapshotExecutor,
    _bounded_int,
    _connect,
    _database_query_failure,
    _execute_with_source,
    _json_value,
)
from .result_projection import (
    _FORMAL_DSO_ATTESTATION_GUARDS,
    _FORMAL_DSO_ATTESTATION_VERSION,
    _FORMAL_DSO_ATTESTED_FACTS,
    _FORMAL_DSO_AUTHORIZED_COMPONENTS,
    _FORMAL_DSO_COVERAGE_DISCLOSURE,
    _FORMAL_DSO_EXTERNAL_SCOPE_DISCLOSURE,
    _FORMAL_DSO_FORMULA_DISCLOSURE,
    _FORMAL_DSO_GROSS_DELIVERY_FACT,
    _FORMAL_DSO_PROJECTION_UNDEFINED_REASONS,
    _MODEL_DISCLOSURE_PROJECTION_VERSION,
    _MODEL_WIRE_CALCULATION_ERROR_FIELDS,
    _MODEL_WIRE_CALCULATION_FIELDS,
    _MODEL_WIRE_CALCULATION_OPERAND_FIELDS,
    _MODEL_WIRE_EVIDENCE_INTEGRITY_ERROR,
    _MODEL_WIRE_OPTIONAL_METRIC_CONTEXT_FIELDS,
    _MODEL_WIRE_RECONCILIATION_FIELD_ALIASES,
    _MODEL_WIRE_RESULT_FIELDS,
    _MODEL_WIRE_SCOPE_COMPATIBILITY_FIELDS,
    _calculation_has_valid_seal,
    _disclosure_is_valid_for_result,
    _disclosure_item_is_valid_for_result,
    _disclosure_ledger_has_valid_seal,
    _fail_closed_formal_dso_model_wire,
    _fail_closed_period_comparison_model_wire,
    _fail_closed_structural_model_wire,
    _fail_closed_target_gap_model_wire,
    _filter_model_wire_evidence,
    _finite_decimal_present,
    _formal_dso_attestation_seal,
    _formal_dso_attestation_state,
    _formal_dso_attested_components_are_valid,
    _formal_dso_window_requirements,
    _mark_model_wire_evidence_integrity_failure,
    _model_wire_calculation_integrity_failure,
    _model_wire_calculation_period_failure,
    _model_wire_calculation_projection,
    _model_wire_calculations,
    _model_wire_change_reconciliation,
    _model_wire_evidence_bundle_results,
    _model_wire_metric_contexts,
    _model_wire_result,
    _reseal_model_disclosure_ledger,
    _sealed_disclosure_ids,
)
from .capability_contract import (
    AvailabilityContractError,
    CapabilityContractError,
    MATCHED_ELAPSED_COVERAGE,
    PHYSICAL_EXECUTION_BUDGET,
    PREVIOUS_PERIOD_COMPARISON,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
    YEAR_OVER_YEAR_COMPARISON,
    business_today,
    ensure_available,
    physical_request_cost,
)
from .db_security import (
    confirm_mysql_read_only_transaction,
    DatabaseSecurityError,
    validate_mysql_source_evidence,
)
from . import (
    analytical_handlers,
    capability_contract,
    contract_store,
    contracts,
    db_executor,
    db_runtime,
    entities,
    evidence,
    request_contract,
    settings,
    sql_identifiers,
)


def _consistent_snapshot_executor(
    *, deadline_at: float | None = None
) -> _ConsistentSnapshotExecutor:
    """Compatibility factory that resolves the current tools facade class."""

    return _ConsistentSnapshotExecutor(deadline_at=deadline_at)


logger = logging.getLogger(__name__)
_CAPABILITY_LOGGER = logging.getLogger(f"{__name__}.capabilities")
_QUERY_CONCURRENCY_LOCK = threading.Lock()
_ACTIVE_QUERY_CALLS = 0

def _validated_query_envelope(args: Any) -> request_contract.ValidatedQueryEnvelope:
    try:
        return request_contract.validate_query_envelope(args)
    except request_contract.RequestContractError as exc:
        raise QueryFailure(
            exc.code,
            exc.message,
            path=exc.path,
            hint=exc.hint,
        ) from exc


def _at_stage(failure: QueryFailure, stage: str) -> QueryFailure:
    """Attach the first trustworthy failure stage without hiding the cause."""

    if failure.stage is None:
        failure.stage = stage
    return failure








def _metric_allowed_dimensions(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    semantics: Mapping[str, Any] | None = None,
) -> set[str]:
    """Resolve the executable dimension capability for this metric path.

    Allocated amount metrics intentionally inherit their dimensions from the
    canonical target-completion path instead of copying that list into the
    metric declaration.  Keep this preflight resolver aligned with the
    catalog and entity resolvers so a published inherited capability is not
    rejected before the analytical builder gets a chance to compile it.
    """

    allowed = metric.get("allowed_dimensions")
    if isinstance(allowed, list) and all(isinstance(code, str) for code in allowed):
        return set(allowed)
    paths = metric.get("paths")
    attribution_mode = request.get("attribution_mode")
    path = paths.get(attribution_mode) if isinstance(paths, Mapping) else None
    path_allowed = path.get("allowed_dimensions") if isinstance(path, Mapping) else None
    if isinstance(path_allowed, list) and all(
        isinstance(code, str) for code in path_allowed
    ):
        return set(path_allowed)
    if allowed is None and paths is None:
        source_metric = metric.get("source_completion_metric")
        source_path = metric.get("source_path")
        if source_metric is None and source_path is None:
            return set()
        metrics = semantics.get("metrics") if isinstance(semantics, Mapping) else None
        source = (
            metrics.get(source_metric)
            if isinstance(metrics, Mapping) and isinstance(source_metric, str)
            else None
        )
        source_paths = source.get("paths") if isinstance(source, Mapping) else None
        source_definition = (
            source_paths.get(source_path)
            if isinstance(source_paths, Mapping) and isinstance(source_path, str)
            else None
        )
        source_allowed = (
            source_definition.get("allowed_dimensions")
            if isinstance(source_definition, Mapping)
            else None
        )
        if isinstance(source_allowed, list) and all(
            isinstance(code, str) for code in source_allowed
        ):
            return set(source_allowed)
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标引用的来源维度能力合同无效。",
            stage="contract_load",
        )
    raise QueryFailure(
        "CONTRACT_UNAVAILABLE",
        "指标维度能力合同无效。",
        stage="contract_load",
    )


def _validate_detail_request_capabilities(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    semantics: Mapping[str, Any] | None = None,
) -> None:
    """Fail before entity or business-data access when detail cannot authorize a request."""

    requested_dimensions = request.get("dimensions") or []
    requested_filters = request.get("metric_filters") or {}
    if not isinstance(requested_dimensions, list) or any(
        not isinstance(code, str) for code in requested_dimensions
    ):
        raise QueryFailure("INVALID_PLAN", "维度列表无效。")
    if len(requested_dimensions) != len(set(requested_dimensions)):
        raise QueryFailure("INVALID_PLAN", "维度列表无效。")
    if len(requested_dimensions) > _max_group_dimensions(metric):
        raise QueryFailure("INVALID_PLAN", "请求的分组维度数量超过该指标发布的上限。")
    if not isinstance(requested_filters, Mapping):
        raise QueryFailure("INVALID_PLAN", "过滤条件格式无效。")
    allowed_dimensions = _metric_allowed_dimensions(request, metric, semantics)
    try:
        grouping=capability_contract.metric_grouping(metric)
    except capability_contract.CapabilityContractError as exc:
        raise QueryFailure(exc.code,exc.message) from exc
    if grouping and requested_dimensions and not set(grouping['required'])<=set(requested_dimensions)<=set(grouping['allowed']):
        raise QueryFailure('UNSUPPORTED_DIMENSION','该指标分组组合不受支持；可筛选维度不等于可分组维度。',stage='input_validation',path='dimensions',hint='必需分组：'+', '.join(grouping['required'])+'；允许分组：'+', '.join(grouping['allowed'])+'。')
    if not {*requested_dimensions, *requested_filters}.issubset(allowed_dimensions):
        raise QueryFailure(
            "UNSUPPORTED_DIMENSION",
            "该指标详情不支持请求中的某个维度或过滤条件。",
            stage="input_validation",
        )

    decomposition = request.get("complete_change_decomposition")
    if isinstance(decomposition, Mapping):
        capability = metric.get("change_decomposition")
        capability_dimensions = (
            capability.get("dimensions")
            if isinstance(capability, Mapping)
            and capability.get("mode") == "additive_partition"
            else None
        )
        if (
            not isinstance(capability_dimensions, list)
            or decomposition.get("dimension") not in capability_dimensions
        ):
            raise QueryFailure(
                "UNSUPPORTED_CHANGE_DECOMPOSITION",
                "该指标详情不支持请求中的完整变化分解。",
                stage="input_validation",
            )

    try:
        _validate_governed_request_time_range(request)
    except QueryFailure as failure:
        if failure.stage is None:
            failure.stage = "input_validation"
        raise


def _validate_metric_contract(
    request: Mapping[str, Any], semantics: Mapping[str, Any]
) -> dict[str, Any]:
    """Load the current metric contract and fail closed before database access."""

    metrics = semantics.get("metrics")
    metric_code = request.get("metric")
    metric = metrics.get(metric_code) if isinstance(metrics, Mapping) else None
    if not isinstance(metric_code, str) or not isinstance(metric, Mapping):
        raise QueryFailure("UNSUPPORTED_METRIC", "该指标尚未进入受控指标定义。")
    try:
        capability_contract.validate_metric_execution_contract(metric)
    except CapabilityContractError as exc:
        raise QueryFailure(exc.code, exc.message) from exc
    _ensure_metric_available(metric)
    _ensure_metric_tree_available(metric_code, semantics)
    _validate_detail_request_capabilities(request, metric, semantics)
    normalized = dict(request)
    required_time_bucket = metric.get("required_time_bucket")
    if required_time_bucket is not None:
        if (
            required_time_bucket not in {"day", "month"}
            or not isinstance(metric.get("time_field"), str)
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标的必需时间分组合同无效。",
                stage="contract_load",
            )
        requested_time_bucket = normalized.get("time_bucket")
        if requested_time_bucket is None:
            normalized["time_bucket"] = required_time_bucket
        elif requested_time_bucket != required_time_bucket:
            raise QueryFailure(
                "INVALID_PLAN",
                "请求的时间分组与指标合同不一致。",
                stage="input_validation",
            )
    return normalized


def _read_yaml(relative_path: str) -> dict[str, Any]:
    try:
        return contract_store.read_yaml(relative_path)
    except contract_store.ContractStoreError as exc:
        if "escapes" in exc.message:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE", "语义文件路径不安全。"
            ) from exc
        if "mapping" in exc.message:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "业务语义格式无效。") from exc
        raise QueryFailure("CONTRACT_UNAVAILABLE", "暂时无法读取业务语义。") from exc








def _contracts(domain: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        datasets, semantics = contracts.execution_contracts(domain)
    except contracts.ContractFailure as exc:
        raise QueryFailure(exc.code, exc.message, stage="contract_load") from exc
    _validate_value_contract_definitions(semantics)
    return datasets, semantics


def _parsed_value_contract(
    definition: Mapping[str, Any],
) -> capability_contract.ValueContract:
    """Adapt the shared value-contract parser to the query error boundary."""

    try:
        return capability_contract.parse_value_contract(
            definition.get("value_contract"),
            filterable_default=definition.get("filterable", True),
        )
    except capability_contract.CapabilityContractError as exc:
        raise QueryFailure(
            exc.code,
            exc.message,
            stage=(
                "input_validation"
                if exc.code in {"INVALID_INPUT", "FILTER_VALUE_NOT_ALLOWED"}
                else "contract_load"
            ),
        ) from exc


def _normalize_value_contract_filter(
    value_contract: capability_contract.ValueContract,
    raw_value: Any,
) -> Any:
    """Adapt shared filter normalization errors to the query boundary."""

    try:
        return value_contract.normalize_filter_value(raw_value)
    except capability_contract.CapabilityContractError as exc:
        raise QueryFailure(
            exc.code,
            exc.message,
            stage="input_validation",
        ) from exc


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
        value_contract = _parsed_value_contract(raw_definition)
        if value_contract.kind == "entity_exact" and not isinstance(
            raw_definition.get("identity_filter"), Mapping
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "实体维度缺少稳定身份过滤定义。",
                stage="contract_load",
            )
        if (
            value_contract.kind == "source_exact"
            and raw_definition.get("normalization") is not None
        ):
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
        if metric.get("dimension_overrides"):
            effective = _effective_dimensions(semantics, metric)
            for code in metric["dimension_overrides"]:
                _parsed_value_contract(effective[code])
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
    dimensions = _effective_dimensions(semantics, request.get("metric"))
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
        value_contract = _parsed_value_contract(definition)
        if not value_contract.filterable:
            raise QueryFailure(
                "UNSUPPORTED_DIMENSION_FILTER",
                "该维度仅支持分组展示，不能作为筛选条件。",
                stage="input_validation",
            )
        if value_contract.kind == "closed":
            normalized_filters[code] = _normalize_value_contract_filter(
                value_contract, raw_value
            )
        elif value_contract.kind == "source_exact":
            _normalize_value_contract_filter(value_contract, raw_value)
            # Exact source values pass through byte-for-byte; this layer never
            # normalizes, expands, translates, or guesses a replacement.
            continue
        elif value_contract.kind == "entity_exact":
            _normalize_value_contract_filter(value_contract, raw_value)
            if code not in (request.get("_entity_bindings") or {}):
                raise QueryFailure(
                    "ENTITY_IDENTITY_UNAVAILABLE",
                    "实体筛选未完成稳定身份绑定。",
                    stage="entity_preflight",
                )
    normalized_request["metric_filters"] = normalized_filters
    return normalized_request




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










_PERIOD_EVIDENCE_VERSION = "calendar-period-evidence/v2"
_PERIOD_OBSERVATION_BASIS = "business_clock_query_observation"


def _period_boundary_date(value: Any) -> date | None:
    """Parse a governed half-open date or month boundary."""

    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return datetime.strptime(value, "%Y-%m").date()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return None


def _calendar_period_evidence(
    start: Any,
    end: Any,
    observed_on: date,
) -> dict[str, Any] | None:
    """Describe calendar-window progress without claiming source freshness."""

    start_date = _period_boundary_date(start)
    end_date = _period_boundary_date(end)
    if start_date is None or end_date is None or start_date >= end_date:
        return None
    if end_date <= observed_on:
        period_state = "completed"
    elif start_date > observed_on:
        period_state = "not_started"
    else:
        period_state = "in_progress"
    return {
        "version": _PERIOD_EVIDENCE_VERSION,
        "observed_on": observed_on.isoformat(),
        "observation_basis": _PERIOD_OBSERVATION_BASIS,
        "period_state": period_state,
        "source_freshness": "not_proven",
    }


def _period_state(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    calendar_evidence = value.get("calendar_evidence")
    evidence = (
        calendar_evidence
        if isinstance(calendar_evidence, Mapping)
        else value
    )
    state = evidence.get("period_state")
    return state if state in {"completed", "in_progress", "not_started"} else None


def _is_snapshot_period(value: Mapping[str, Any]) -> bool:
    return value.get("source") in _SNAPSHOT_TIME_SOURCES


def assess_period_compatibility(
    left: Any,
    right: Any,
    *,
    comparison_alignment: Any = None,
) -> dict[str, Any] | None:
    """Assess period evidence only; metric and population checks stay separate."""

    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return None
    left_snapshot = _is_snapshot_period(left)
    right_snapshot = _is_snapshot_period(right)
    if left_snapshot or right_snapshot:
        if not (left_snapshot and right_snapshot):
            return {
                "status": "not_assessable",
                "reason_codes": ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"],
            }
        left_resolved = (
            left.get("resolution_state") == "resolved"
            and isinstance(left.get("snapshot_month"), str)
        )
        right_resolved = (
            right.get("resolution_state") == "resolved"
            and isinstance(right.get("snapshot_month"), str)
        )
        if left_resolved and right_resolved:
            return {"status": "compatible", "reason_codes": []}
        return {
            "status": "not_assessable",
            "reason_codes": ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"],
        }

    if isinstance(comparison_alignment, Mapping):
        required_alignment_fields = {
            "version",
            "kind",
            "coverage",
            "observed_on",
            "requested_current_start",
            "requested_current_end",
            "effective_current_end",
            "current_was_clipped",
        }
        observed_date = _period_boundary_date(
            comparison_alignment.get("observed_on")
        )
        requested_start = _period_boundary_date(
            comparison_alignment.get("requested_current_start")
        )
        requested_end = _period_boundary_date(
            comparison_alignment.get("requested_current_end")
        )
        effective_end = _period_boundary_date(
            comparison_alignment.get("effective_current_end")
        )
        left_start = _period_boundary_date(left.get("start"))
        left_end = _period_boundary_date(left.get("end"))
        right_start = _period_boundary_date(right.get("start"))
        right_end = _period_boundary_date(right.get("end"))
        left_calendar = left.get("calendar_evidence")
        right_calendar = right.get("calendar_evidence")
        clipped = comparison_alignment.get("current_was_clipped")
        aligned = False
        if (
            set(comparison_alignment) == required_alignment_fields
            and comparison_alignment.get("version")
            == _MATCHED_ELAPSED_COMPARISON_VERSION
            and comparison_alignment.get("kind") == YEAR_OVER_YEAR_COMPARISON
            and comparison_alignment.get("coverage")
            == MATCHED_ELAPSED_COVERAGE
            and isinstance(clipped, bool)
            and observed_date is not None
            and requested_start is not None
            and requested_end is not None
            and effective_end is not None
            and requested_start < effective_end <= requested_end
            and effective_end <= observed_date + timedelta(days=1)
            and clipped == (effective_end < requested_end)
            and left_start == requested_start
            and left_end == effective_end
            and isinstance(left_calendar, Mapping)
            and isinstance(right_calendar, Mapping)
            and left_calendar.get("observed_on")
            == comparison_alignment.get("observed_on")
            and right_calendar.get("observed_on")
            == comparison_alignment.get("observed_on")
        ):
            try:
                aligned = (
                    right_start == _shift_calendar_year(left_start, -1)
                    and (
                        right_end - right_start == left_end - left_start
                        or (
                            left_start.day == 1
                            and left_end.day == 1
                            and right_start.day == 1
                            and right_end.day == 1
                            and comparison_alignment.get("current_was_clipped") is False
                            and right_end == _shift_calendar_year(left_end, -1)
                        )
                    )
                )
            except (ValueError, OverflowError):
                aligned = False
        if aligned:
            return {"status": "compatible", "reason_codes": []}
        return {
            "status": "not_assessable",
            "reason_codes": ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"],
        }

    if left == right:
        return {"status": "compatible", "reason_codes": []}

    left_state = _period_state(left)
    right_state = _period_state(right)
    if left_state is None and right_state is None:
        return None
    if left_state == right_state == "completed":
        status = "compatible"
        reasons: list[str] = []
    elif "not_started" in {left_state, right_state} or None in {
        left_state,
        right_state,
    }:
        status = "not_assessable"
        reasons = ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"]
    else:
        status = "coverage_mismatch"
        reasons = ["PERIOD_COVERAGE_MISMATCH"]
    return {"status": status, "reason_codes": reasons}


def _annotate_period_evidence(value: Any, observed_on: date) -> Any:
    """Attach one batch-frozen calendar observation to every flow range."""

    if not isinstance(value, Mapping):
        return value
    evidence = _calendar_period_evidence(
        value.get("start"), value.get("end"), observed_on
    )
    if evidence is not None:
        return {**dict(value), "calendar_evidence": evidence}
    annotated = {
        key: _annotate_period_evidence(item, observed_on)
        if isinstance(item, Mapping)
        else copy.deepcopy(item)
        for key, item in value.items()
    }
    compatibility = assess_period_compatibility(
        annotated.get("current"),
        annotated.get("comparison"),
        comparison_alignment=annotated.get("comparison_alignment"),
    )
    if compatibility is not None:
        annotated["comparison_compatibility"] = compatibility
    return annotated


def _period_comparison_authorized(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    compatibility = value.get("comparison_compatibility")
    return (
        not isinstance(compatibility, Mapping)
        or compatibility.get("status") == "compatible"
    )




















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
    *,
    reserved_request_ids: Sequence[str] = (),
) -> tuple[list[Any], dict[str, str]]:
    """Expand only explicit semantic operations into governed request pairs."""

    used_request_ids = set(reserved_request_ids) | {
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
            "limit": 20,
        }
        direction = operation.get("direction", "increase")
        partition_request["order_by"] = {
            "field": "absolute_delta_value" if direction == "absolute" else "delta_value",
            "direction": "asc" if direction == "decrease" else "desc",
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
    *,
    reserved_request_ids: Sequence[str] = (),
) -> tuple[list[Any], dict[str, str]]:
    """Expand an explicit target-gap operation into overall and partition queries."""

    used_request_ids = set(reserved_request_ids) | {
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


def _allocate_public_request_branches(
    requests: Sequence[Any],
    *,
    physical_budget: int = PHYSICAL_EXECUTION_BUDGET,
    preflight_failures: Mapping[str, QueryFailure] | None = None,
) -> tuple[list[Any], dict[str, QueryFailure]]:
    """Admit public branches independently into the physical execution budget."""

    admitted: list[Any] = []
    local_failures: dict[str, QueryFailure] = dict(preflight_failures or {})
    remaining = physical_budget
    for raw_request in requests:
        request_id = (
            str(raw_request.get("request_id"))
            if isinstance(raw_request, Mapping)
            else ""
        )
        if request_id in local_failures:
            continue
        try:
            # Complete-operation expansion calls the structural validator. Do
            # the same validation per public branch first so one malformed
            # operation cannot escape into the batch-level exception handler.
            _validate_request(raw_request)
            needs_currency_probe = False
            if raw_request.get("currency_basis") == "auto":
                _, budget_semantics = _contracts(raw_request["domain"])
                basis_info = currency_basis.capability(raw_request["metric"], budget_semantics)
                needs_currency_probe = bool(basis_info and set(basis_info["counterparts"]) == {"rmb", "original"})
            cost = physical_request_cost(raw_request, currency_probe=needs_currency_probe)
        except CapabilityContractError as exc:
            local_failures[request_id] = QueryFailure(
                exc.code,
                exc.message,
                stage="input_validation",
            )
            continue
        except QueryFailure as exc:
            local_failures[request_id] = _at_stage(exc, "input_validation")
            continue
        if cost > remaining:
            local_failures[request_id] = QueryFailure(
                "EXECUTION_BUDGET_EXCEEDED",
                "This request branch exceeds the remaining physical execution budget; retry it separately.",
                stage="query_planning",
            )
            continue
        admitted.append(raw_request)
        remaining -= cost
    return admitted, local_failures


def _order_public_branch_artifacts(
    public_requests: Sequence[Any],
    expanded_requests: Sequence[Any],
    results: Sequence[Mapping[str, Any]],
    local_failures: Mapping[str, QueryFailure],
    operation_partitions: Mapping[str, str],
    target_gap_partitions: Mapping[str, str],
    *,
    elapsed_ms: int,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Project one result per public branch after validating physical pairs."""

    request_by_id = {
        str(request.get("request_id")): request
        for request in expanded_requests
        if isinstance(request, Mapping) and isinstance(request.get("request_id"), str)
    }
    result_by_id = {
        str(result.get("request_id")): dict(result)
        for result in results
        if isinstance(result, Mapping) and isinstance(result.get("request_id"), str)
    }
    ordered_requests: list[Mapping[str, Any]] = []
    ordered_results: list[dict[str, Any]] = []
    for raw_request in public_requests:
        if not isinstance(raw_request, Mapping):
            continue
        request_id = raw_request.get("request_id")
        if not isinstance(request_id, str):
            continue
        failure = local_failures.get(request_id)
        if failure is not None:
            ordered_requests.append(raw_request)
            ordered_results.append(
                _failure_result(
                    request_id,
                    failure,
                    elapsed_ms,
                    business_metric_ref=(
                        str(raw_request.get("metric"))
                        if isinstance(raw_request.get("metric"), str)
                        else None
                    ),
                    business_sql_attempted_count=0,
                    business_sql_confirmed_count=0,
                )
            )
            continue
        overall_id = operation_partitions.get(request_id)
        if overall_id is None:
            overall_id = target_gap_partitions.get(request_id)
        physical_ids = (request_id,)
        if isinstance(overall_id, str):
            # The overall query is an internal reconciliation operand. Require
            # both physical artifacts to exist, but expose only the public
            # partition branch whose ID was supplied by Hermes.
            physical_ids = (overall_id, request_id)
        for physical_id in physical_ids:
            request = request_by_id.get(physical_id)
            result = result_by_id.get(physical_id)
            if request is None or result is None:
                raise QueryFailure(
                    "INTERNAL_ERROR",
                    "The physical execution plan did not produce a result for every admitted branch.",
                    stage="result_validation",
                )
        public_request = request_by_id.get(request_id)
        public_result = result_by_id.get(request_id)
        if public_request is None or public_result is None:
            raise QueryFailure(
                "INTERNAL_ERROR",
                "The physical execution plan did not produce its public branch result.",
                stage="result_validation",
            )
        ordered_requests.append(public_request)
        ordered_results.append(public_result)
    ordered_ids = [str(result.get("request_id")) for result in ordered_results]
    if (
        len(ordered_requests) != len(public_requests)
        or len(ordered_results) != len(public_requests)
        or len(set(ordered_ids)) != len(ordered_ids)
    ):
        raise QueryFailure(
            "INTERNAL_ERROR",
            "The public branch execution plan could not be reconciled.",
            stage="result_validation",
        )
    return ordered_requests, ordered_results


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
    contract = _target_gap_contract()
    if (
        not isinstance(request, Mapping)
        or request.get("domain") != "target"
        or request.get("metric") not in contract.metrics
        or request.get("attribution_mode") != contract.attribution_mode
        or dimension not in contract.dimensions
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


def _validate_delivery_metric_scope(
    request: Mapping[str, Any], semantics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the selected metric's declared scope without reading natural language."""
    normalized = dict(request)
    if normalized.get("domain") != "delivery" or normalized.get("mode") != "metric":
        return normalized
    if semantics is None:
        _datasets, semantics = _contracts("delivery")
    metrics = semantics.get("metrics")
    definition = metrics.get(normalized.get("metric")) if isinstance(metrics, Mapping) else None
    if not isinstance(definition, Mapping):
        raise QueryFailure("UNSUPPORTED_METRIC", "该指标尚未进入受控指标定义。")
    try:
        policy = contracts.delivery_scope_policy(definition)
    except contracts.ContractFailure as exc:
        raise QueryFailure(exc.code, exc.message) from exc
    scope = normalized.get("delivery_scope")
    valid = (scope is None and not policy["required"]) or scope in policy["allowed_scopes"]
    if not valid:
        # Registered error classes are retained; selection follows policy,
        # not the spelling of the metric identifier.
        if policy["required"]:
            raise QueryFailure("GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST",
                "毛出库指标必须通过结构化字段确认用户明确要求毛口径或下单出库对照。")
        raise QueryFailure("INVALID_INPUT", "当前指标与 delivery_scope 不一致。")
    if scope is None and policy["default"] not in {None, "default_net"}:
        normalized["delivery_scope"] = policy["default"]
    filters = normalized.get("metric_filters") or {}
    if isinstance(filters, dict) and "ready_goods" in filters:
        value = filters["ready_goods"]
        values = value if isinstance(value, list) else [value]
        if not values or any(item not in {"y", "n"} for item in values):
            raise QueryFailure("INVALID_PLAN", "备货筛选只接受结构化值 y 或 n。")
    return normalized


def _validate_inventory_metric_scope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the structured current-inventory scope without reading free text."""

    normalized = dict(request)
    scope = normalized.get("inventory_scope")
    if normalized.get("domain") != "inventory" or normalized.get("mode") != "metric":
        return normalized
    if scope is not None and scope not in capability_contract.INVENTORY_SCOPES:
        raise QueryFailure("INVALID_INPUT", "库存范围不受支持。")
    return normalized




















def _metric_query_limit(request: Mapping[str, Any]) -> int:
    """Validate the public row limit and apply only the documented environment cap."""

    environment_cap = _bounded_int(
        "max_rows",
        request_contract.PUBLIC_ROW_LIMIT_MAX,
        request_contract.PUBLIC_ROW_LIMIT_MIN,
        request_contract.PUBLIC_ROW_LIMIT_MAX,
    )
    requested_limit = request.get("limit", environment_cap)
    if not request_contract.valid_public_row_limit(requested_limit):
        raise QueryFailure(
            "INVALID_INPUT",
            (
                "limit 必须是 "
                f"{request_contract.PUBLIC_ROW_LIMIT_MIN} 到 "
                f"{request_contract.PUBLIC_ROW_LIMIT_MAX} 的整数。"
            ),
        )
    return min(environment_cap, requested_limit)


def _period_additive_fields(metric, scope, datasets, semantics):
    if metric.get("query_kind") == "target_completion":
        return ["target_amount_rmb", "actual_amount_rmb", "gap_amount_rmb"]
    if metric.get("time_policy") in {"current_snapshot", "latest_snapshot", "latest_non_null_snapshot"}:
        return []
    sum_kinds = {"sum", "sum_positive", "sum_product", "sum_product_many"}
    components = metric.get("components") or []
    component_metrics = [(semantics.get("metrics") or {}).get(c.get("metric"), {}) for c in components]
    component_additivity = bool(component_metrics) and all(
        c.get("aggregation") in sum_kinds
        and c.get("time_policy") not in {"current_snapshot", "latest_snapshot", "latest_non_null_snapshot"}
        for c in component_metrics
    )
    additive = metric.get("aggregation") in sum_kinds or component_additivity or metric.get("query_kind") == "allocated_amount"
    sources = scope.get("source_datasets") or [scope.get("dataset")]
    catalog = datasets.get("datasets", {})
    if additive and sources and all(catalog.get(name, {}).get("kind") in {"fact", "actual_fact", "monthly_flow", "monthly_target_fact", "lifecycle_fact"} for name in sources):
        return ["metric_value"]
    return []


def _validate_pre_entity_metric_plan(
    request: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    *,
    observed_on: date | None = None,
) -> None:
    """Compile the governed plan before entity lookup so pure failures are DB-free."""

    _sql, _params, scope = _build_metric_query(
        request,
        datasets_contract,
        semantics,
        _metric_query_limit(request),
        observed_on=observed_on,
    )

    check = request.get("period_summary")
    if check and check["field"] not in _period_additive_fields(semantics["metrics"][request["metric"]], scope, datasets_contract, semantics):
        raise QueryFailure("NONADDITIVE_PERIOD_FIELD", "该字段不能跨月份求和。", stage="input_validation")
    if check:
        period = scope.get("time_range") or {}
        start, end = _period_boundary_date(period.get("start")), _period_boundary_date(period.get("end"))
        if start is not None and end is not None:
            if start.day != 1 or end.day != 1:
                raise QueryFailure("FULL_MONTH_WINDOW_REQUIRED", "期间合计需要完整自然月边界。", stage="input_validation")
            if any(not start <= _period_boundary_date(month) < end for month in check["periods"]):
                raise QueryFailure("PERIOD_OUTSIDE_QUERY", "选定月份不在查询期间内。", stage="input_validation")












def _evidence_rows_and_state(
    rows: Sequence[Mapping[str, Any]], truncated: bool
) -> tuple[list[dict[str, Any]], str]:
    public_rows = [
        {
            str(key): value
            for key, value in row.items()
            if key not in _INTERNAL_RESULT_FIELDS and not str(key).startswith("__distribution_")
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
            # Target-completion aggregates deliberately retain one structural
            # row when both sides have no matching facts.  Keep that row so
            # target_data_state/period_state can tell the model "missing" or
            # "not_set_for_future"; never turn it into a numeric zero.
            if any(
                isinstance(row, Mapping)
                and (
                    "target_data_state" in row
                    or "period_state" in row
                )
                for row in rows
            ):
                return public_rows, "undefined"
            return [], "empty"
    metric_states = [row.get("metric_data_state") for row in rows if "metric_data_state" in row]
    if metric_states:
        if any(state == "incomplete" for state in metric_states):
            return public_rows, "incomplete"
        if all(state == "missing" for state in metric_states):
            return public_rows, "undefined"
        if any(state == "missing" for state in metric_states):
            return public_rows, "incomplete"
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


def _validate_required_time_bucket_rows(
    metric: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    """Fail closed when a contract-required series loses its period grain."""

    required_time_bucket = metric.get("required_time_bucket")
    if required_time_bucket is None or not rows:
        return
    if required_time_bucket not in {"day", "month"}:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "指标的必需时间分组合同无效。",
            stage="result_validation",
        )
    if any(_safe_display_value(row.get("period")) is None for row in rows):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "时间序列结果缺少合同要求的期间维度。",
            stage="result_validation",
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


# Composition-root bridge: entities reuse the governed executor without a
# lower-level module importing this orchestration module.  The lambda resolves
# ``_execute`` at call time so existing monkeypatch-based compatibility tests
# continue to intercept the boundary.
db_runtime.install_query_executor(
    lambda sql, params, limit, *, deadline_at=None: _execute(
        sql, params, limit, deadline_at=deadline_at
    )
)








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


def _caller_retry_metadata(failure: QueryFailure) -> dict[str, Any]:
    """Describe whether a caller may safely submit the same request again."""

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
            f"调用方至少等待 {retry_after_seconds} 秒后最多重新提交一次；重新提交必须保持同一 request_id、"
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


def _public_error(failure: QueryFailure) -> dict[str, Any]:
    error = {
        "code": failure.code,
        "message": failure.message,
        **_caller_retry_metadata(failure),
    }
    if failure.path is not None:
        error["path"] = failure.path
    if failure.hint is not None:
        error["hint"] = failure.hint
    return error


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


# Public result-field names live in a low-dependency module so the read-only
# catalog projection can read them without importing this execution center
# (F07).  The alias keeps existing references in this module unchanged.
from .public_fields import PUBLIC_FACT_FIELDS as _PUBLIC_FACT_FIELDS

_PUBLIC_STATE_FIELDS = {
    "net_flow_state",
    "pool_movement_state",
    "metric_data_state",
    "current_metric_data_state",
    "comparison_metric_data_state",
    "target_data_state",
    "actual_data_state",
    "period_state",
    "cost_turnover_state",
    "ddp_turnover_state",
    "unclassified_amount_state",
}
_SCOPE_PRESENTATION_KEYS = {
    "request_id",
    "dimensions",
    "order_by",
    "limit",
    "decomposition_of_request_id",
    "_target_gap_of_request_id",
    "period_summary",
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
    text = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in text
    )
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
    metric: str | None = None,
) -> list[dict[str, Any]]:
    """Project governed filter identity without leaking execution identifiers."""

    dimensions = _effective_dimensions(semantics, metric)
    if not isinstance(dimensions, Mapping):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "业务域缺少维度语义。")
    public: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
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
        identity_filter = (
            definition.get("identity_filter")
            if isinstance(definition, Mapping)
            else None
        )
        identity_capable = (
            isinstance(identity_filter, Mapping)
            and identity_filter.get("entity_type") == resolved.get("entity_type")
            and isinstance(resolved.get("entity_type"), str)
            and isinstance(identity_filter.get("column"), str)
            and resolved.get("entity_type") in entities._registry()["candidate_sources"]
        )
        identity_metadata: dict[str, Any] = {}
        entity_refs: list[str] = []
        if identity_capable:
            stable_values: list[str] = []
            identity_keys = ["canonical_ids", "canonical_codes"]
            if resolved.get("resolution_path") == "registered_exact":
                # Registered aliases already carry canonical source values in
                # filter_values; other paths must expose an explicit canonical
                # ID or code before a re-callable reference can be issued.
                identity_keys.append("filter_values")
            for key in identity_keys:
                raw_values = resolved.get(key)
                if not isinstance(raw_values, list):
                    continue
                stable_values = [
                    str(value)
                    for value in raw_values
                    if value is not None and not isinstance(value, bool) and str(value) != ""
                ]
                if stable_values:
                    break
            entity_refs = [
                entities.remember_opaque_entity_ref(
                    role,
                    value,
                )
                for value in dict.fromkeys(stable_values)
            ]
            identity_metadata = {
                "identity_state": "identified" if entity_refs else "identity_missing",
                "entity_ref_kind": "opaque_reference_non_filter_token",
            }
            if len(entity_refs) == 1:
                identity_metadata["entity_ref"] = entity_refs[0]
            elif entity_refs:
                identity_metadata["entity_refs"] = entity_refs
        execution_values = {
            str(value)
            for key in ("canonical_ids", "filter_values")
            for value in (resolved.get(key) if isinstance(resolved.get(key), list) else [])
            if value is not None
        }
        for raw_name in raw_names:
            display_name = _safe_display_value(raw_name)
            name_values = {str(raw_name), display_name}
            if (
                safe_label is None
                or display_name is None
                or (
                    bool(name_values.intersection(internal_values))
                    and not (
                        not name_values.intersection(execution_values)
                        and entities.is_governed_display_name(raw_name, resolved, definition)
                    )
                )
            ):
                continue
            key = (role, display_name, tuple(entity_refs))
            if key not in seen:
                public.append(
                    {
                        "role": role,
                        "label": safe_label,
                        "display_name": display_name,
                        **identity_metadata,
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


def _public_currency_scope(
    request: Mapping[str, Any],
    metric_definition: Mapping[str, Any],
) -> dict[str, Any] | None:
    policy = metric_definition.get("currency_policy")
    if not (
        isinstance(policy, Mapping)
        and policy.get("mode") == "original_currency"
        and policy.get("require_filter_or_group") is True
    ):
        return None
    dimensions = request.get("dimensions") or []
    filters = request.get("metric_filters") or {}
    if "currency" in dimensions:
        return {"mode": "grouped"}
    if "currency" not in filters:
        return None
    raw_value = filters.get("currency")
    values = raw_value if isinstance(raw_value, list) else [raw_value]
    public_values = [
        _safe_display_value(value)
        for value in values
        if _safe_display_value(value) is not None
    ]
    if len(public_values) != len(values):
        return {"mode": "filtered", "state": "unknown"}
    return {
        "mode": "filtered",
        "value": public_values[0] if len(public_values) == 1 else public_values,
    }


def _claim_entity_identity(
    binding: Mapping[str, Any],
    row: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    fields = binding.get("identity_fields")
    if not isinstance(fields, list) or not fields:
        return None, None
    for field in fields:
        if not isinstance(field, str):
            continue
        raw_value = row.get(field)
        if raw_value is None or isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        return entities.remember_opaque_entity_ref(
            str(binding.get("dimension") or "entity"),
            raw_value,
        ), "identified"
    return None, "identity_missing"


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
        try:
            default_scope = contracts.delivery_scope_policy(metric_definition)["default"]
        except contracts.ContractFailure as exc:
            raise QueryFailure(exc.code, exc.message) from exc
        applied_request["delivery_scope"] = request.get("delivery_scope") or default_scope
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
                "identity_outputs": scope.get("identity_outputs") or {},
                **({"ranking_plan": scope["ranking_plan"]} if scope.get("ranking_plan") else {}),
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


# Numeric parsing and bounded comparison are owned by evidence.py so query
# execution and evidence validation cannot silently drift apart.
_finite_decimal = evidence._finite_decimal
_decimal_close = evidence._decimal_close
_target_amounts_consistent = evidence._target_amounts_consistent


def _exact_nonnegative_int(value: Any) -> int | None:
    parsed = _finite_decimal(value)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _validated_embedded_partition_proof(
    rows: Sequence[Mapping[str, Any]],
    *,
    truncated: bool,
    returned_row_count: int,
    requires_completeness_proof: bool = False,
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
    proof = {
        "version": "same-statement-window-partition-proof/v1",
        "metric_value": _json_value(current),
        "comparison_value": _json_value(comparison),
        "delta_value": _json_value(delta),
        "full_partition_row_count": full_count,
    }
    if requires_completeness_proof:
        coverage_fields = {
            _INTERNAL_PARTITION_CURRENT_MISSING: "current_missing_value_count",
            _INTERNAL_PARTITION_CURRENT_KNOWN: "current_known_value_count",
            _INTERNAL_PARTITION_COMPARISON_MISSING: (
                "comparison_missing_value_count"
            ),
            _INTERNAL_PARTITION_COMPARISON_KNOWN: "comparison_known_value_count",
        }
        for internal_field, public_field in coverage_fields.items():
            count = _exact_nonnegative_int(first.get(internal_field))
            if count is None or any(
                _exact_nonnegative_int(row.get(internal_field)) != count
                for row in rows[1:]
            ):
                raise QueryFailure(
                    "PARTITION_PROOF_INVALID",
                    "完整分区覆盖计数证明缺失、无效或不一致。",
                    stage="result_validation",
                )
            proof[public_field] = count
    distribution = evidence.distribution_from_window_rows(rows, full_count, delta)
    if distribution is not None:
        proof["change_distribution"] = distribution
    return proof


def _comparison_is_complete(
    facts: Mapping[str, Any],
    states: Mapping[str, str],
    *,
    require_state_evidence: bool = False,
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
    coverage_absence_fields = {
        "current_metric_data_state": (
            "current_missing_value_count",
            "current_known_value_count",
        ),
        "comparison_metric_data_state": (
            "comparison_missing_value_count",
            "comparison_known_value_count",
        ),
    }
    has_paired_states = {
        "current_metric_data_state",
        "comparison_metric_data_state",
    } <= set(states)
    has_legacy_comparison_state = "comparison_state" in states
    has_complete_coverage_counts = all(
        _exact_nonnegative_int(facts.get(field)) is not None
        for fields in coverage_absence_fields.values()
        for field in fields
    )
    if require_state_evidence and not (
        has_paired_states
        or has_legacy_comparison_state
        or has_complete_coverage_counts
    ):
        return False
    return (
        values[2] == values[0] - values[1]
        and all(
            str(value).casefold() in complete_states
            or (
                key in coverage_absence_fields
                and str(value).casefold() == "not_present"
                and all(
                    _exact_nonnegative_int(facts.get(field)) == 0
                    for field in coverage_absence_fields[key]
                )
            )
            for key, value in states.items()
        )
    )


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
            (target is not None or target_state in {"missing", "incomplete", "not_set_for_future"})
            and actual is None
            and gap is None
            and completion is None
            and metric_value is None
        )
    if str(states.get("actual_data_state") or "").casefold() not in {
        "reported",
        "set",
    }:
        return False
    if target_state == "missing":
        return (
            completion is None
            and metric_value is None
            and gap is None
            and target in {None, Decimal("0")}
        )
    if target_state == "incomplete":
        return completion is None and metric_value is None and gap is None
    if target_state == "zero":
        return (
            target == 0
            and _target_amounts_consistent(target, actual, gap)
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
            _target_amounts_consistent(target, actual, gap)
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
    fact_units: Mapping[str, str] | None = None,
    currency_scope: Mapping[str, Any] | None = None,
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
        dimensions: list[dict[str, Any]] = []
        currency_binding_missing = False
        for binding in dimension_bindings:
            dimension_start = len(dimensions)
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
            approved_display = set(binding.get("public_display_fields") or ())
            for field in fields:
                normalized_field = field.casefold()
                if (
                    normalized_field in {"id", "no"}
                    or normalized_field.startswith("_")
                    or (
                        normalized_field.endswith(("_id", "_no"))
                        and normalized_field != "currency_no"
                        and field not in approved_display
                    )
                ):
                    continue
                display_value = _safe_display_value(row.get(field))
                if display_value is None:
                    continue
                dimension_value: dict[str, Any] = {"label": label, "value": display_value}
                raw_value = row.get(field)
                if field in approved_display and isinstance(raw_value, str) and display_value != raw_value:
                    # Existing bounded/sanitized rendering is not an exact input token.
                    dimension_value["display_only"] = True
                dimensions.append(dimension_value)
                break
            if (
                binding.get("dimension") == "currency"
                and len(dimensions) == dimension_start
            ):
                currency_binding_missing = True
            entity_ref, identity_state = _claim_entity_identity(binding, row)
            if identity_state is not None:
                identity_metadata = {
                    "identity_state": identity_state,
                    "entity_ref_kind": "opaque_reference_non_filter_token",
                }
                if isinstance(binding.get("entity_ref_scope"), str):
                    identity_metadata["entity_ref_scope"] = binding["entity_ref_scope"]
                if isinstance(binding.get("source_group_identity"), str):
                    identity_metadata["source_group_identity"] = binding[
                        "source_group_identity"
                    ]
                if isinstance(binding.get("source_group_ref_field"), str):
                    identity_metadata["source_group_ref_field"] = binding[
                        "source_group_ref_field"
                    ]
                if entity_ref is not None:
                    identity_metadata["entity_ref"] = entity_ref
                if len(dimensions) > dimension_start:
                    dimensions[dimension_start].update(identity_metadata)
                elif isinstance(label, str):
                    dimensions.append(
                        {
                            "label": label,
                            "value": "未知",
                            "display_only": True,
                            "display_name_missing": True,
                            **identity_metadata,
                        }
                    )
        currency_value: str | None = None
        currency_state: str | None = None
        currency_placeholder_added = False
        if currency_binding_missing:
            currency_label = next(
                (
                    str(binding.get("label"))
                    for binding in dimension_bindings
                    if binding.get("dimension") == "currency"
                    and isinstance(binding.get("label"), str)
                ),
                "币种",
            )
            dimensions.append(
                {
                    "label": currency_label,
                    "value": "未知币种",
                    "display_only": True,
                    "display_name_missing": True,
                    "identity_state": "unknown",
                }
            )
            currency_placeholder_added = True
        if isinstance(currency_scope, Mapping):
            if currency_scope.get("mode") == "filtered":
                raw_value = currency_scope.get("value")
                currency_value = (
                    _safe_display_value(raw_value)
                    if not isinstance(raw_value, list)
                    else None
                )
                currency_state = "identified" if currency_value is not None else "unknown"
            elif currency_scope.get("mode") == "grouped":
                currency_value = _safe_display_value(row.get("currency_no"))
                currency_state = "identified" if currency_value is not None else "unknown"
                if currency_value is None and not currency_placeholder_added:
                    currency_label = next(
                        (
                            str(binding.get("label"))
                            for binding in dimension_bindings
                            if binding.get("dimension") == "currency"
                            and isinstance(binding.get("label"), str)
                        ),
                        "币种",
                    )
                    dimensions.append(
                        {
                            "label": currency_label,
                            "value": "未知币种",
                            "display_only": True,
                            "display_name_missing": True,
                            "identity_state": "unknown",
                        }
                    )
        elif not currency_binding_missing:
            currency_value = (
                "CNY"
                if not fact_units and (
                    any(field.endswith("_rmb") for field in facts)
                    or metric_unit in {"人民币元", "元"}
                )
                else None
            )
        period_value = _safe_display_value(row.get("period"))
        if period_value is not None and not any(
            dimension["value"] == period_value for dimension in dimensions
        ):
            dimensions.append({"label": "\u671f\u95f4", "value": period_value})
        relations = ["observation"]
        if (
            _comparison_is_complete(facts, states)
            and _period_comparison_authorized(period)
        ):
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
                **({"fact_units": {
                    field: unit for field, unit in fact_units.items() if field in facts
                }} if fact_units else {}),
                "currency": currency_value,
                **({"currency_state": currency_state} if currency_state is not None else {}),
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
    known_dimension_codes: set[str],
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
        "any_request_dimension_or_filter_present",
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
    if "any_request_dimension_or_filter_present" in condition:
        codes = condition["any_request_dimension_or_filter_present"]
        if (
            not isinstance(codes, list)
            or not codes
            or len(codes) > 12
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None
                for code in codes
            )
            or len(set(codes)) != len(codes)
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露条件无效。",
                stage="contract_load",
            )
        if not set(codes) <= known_dimension_codes:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露条件引用了未注册维度。",
                stage="contract_load",
            )
        dimensions = request.get("dimensions") or []
        metric_filters = request.get("metric_filters") or {}
        if not isinstance(dimensions, list) or not isinstance(
            metric_filters, Mapping
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "指标披露条件无效。",
                stage="contract_load",
            )
        checks.append(
            any(
                code in dimensions or code in metric_filters
                for code in codes
            )
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
    known_dimension_codes: set[str],
    inherited_disclosures: Sequence[Mapping[str, Any]] = (),
    inventory_scope: str | None = None,
    applied_time_range: Mapping[str, Any] | None = None,
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
    if not isinstance(known_dimension_codes, set) or any(
        not isinstance(code, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None
        for code in known_dimension_codes
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "业务域维度注册表无效。",
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
        if disclosure_id == _FORMAL_DSO_COVERAGE_DISCLOSURE:
            months, days = _formal_dso_window_requirements(applied_time_range)
            if months is not None:
                public_text += (
                    f" 本次期间为{applied_time_range['start']}至{applied_time_range['end']}（不含结束日），"
                    f"共{months}个完整自然月、{days}个自然日；"
                    f"要求覆盖期初至期末连续{months + 1}个月末余额，真实零余额参与计算，缺失余额不自动补零。"
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
                known_dimension_codes=known_dimension_codes,
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
















def _formal_dso_calculation_attestation(
    *,
    request: Mapping[str, Any],
    row: Mapping[str, Any],
    applied_time_range: Mapping[str, Any],
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
    disclosure_ledger: Sequence[Mapping[str, Any]],
    disclosure_ledger_seal: str,
) -> dict[str, Any] | None:
    """Seal canonical public component values and guards for formal DSO."""

    if (
        request.get("domain") != "receivable"
        or request.get("metric") != "formal_receivable_turnover_days"
    ):
        return None
    expected_months, expected_period_days = _formal_dso_window_requirements(
        applied_time_range
    )
    period_days = row.get("period_natural_days")
    period_days_match = (
        isinstance(period_days, int)
        and not isinstance(period_days, bool)
        and expected_period_days is not None
        and period_days == expected_period_days
    )
    denominator = row.get("delivery_amount_rmb")
    denominator_decimal = _finite_decimal(denominator)
    denominator_present = denominator_decimal is not None
    denominator_positive = (
        denominator_decimal is not None and denominator_decimal > 0
    )
    effective_month_count = _finite_decimal(row.get("effective_month_count"))
    sealed_disclosures = _sealed_disclosure_ids(
        disclosure_ledger,
        disclosure_ledger_seal,
        request_id=str(request["request_id"]),
        metric_ref=metric_ref,
        scope_fingerprint=scope_fingerprint,
        projection_fingerprint=projection_fingerprint,
    )
    guards = {
        "metric_value_present": _finite_decimal_present(row.get("metric_value")),
        "average_net_debt_present": _finite_decimal_present(
            row.get("average_net_debt_rmb")
        ),
        "gross_delivery_denominator_present": denominator_present,
        "gross_delivery_denominator_positive": denominator_positive,
        "period_natural_days_present": isinstance(period_days, int)
        and not isinstance(period_days, bool),
        "period_matches_complete_window": period_days_match,
        "complete_natural_month_window": expected_months is not None,
        "complete_month_end_snapshots": (
            expected_months is not None
            and row.get("snapshot_month_count") == expected_months + 1
        ),
        "effective_month_count_within_window": (
            expected_months is not None
            and effective_month_count is not None
            and effective_month_count == effective_month_count.to_integral_value()
            and 1 <= effective_month_count <= expected_months
        ),
        "coverage_disclosure_sealed": _FORMAL_DSO_COVERAGE_DISCLOSURE
        in sealed_disclosures,
        "formula_disclosure_sealed": _FORMAL_DSO_FORMULA_DISCLOSURE
        in sealed_disclosures,
        "both_external_customer_scopes_disclosed": (
            _FORMAL_DSO_EXTERNAL_SCOPE_DISCLOSURE in sealed_disclosures
        ),
    }
    reason_codes = [
        key.upper() for key, passed in guards.items() if passed is not True
    ]
    component_values = (
        {
            "metric_value": _json_value(row.get("metric_value")),
            "average_net_debt_rmb": _json_value(
                row.get("average_net_debt_rmb")
            ),
            _FORMAL_DSO_GROSS_DELIVERY_FACT: _json_value(denominator),
            "period_natural_days": _json_value(period_days),
            "snapshot_month_count": _json_value(
                row.get("snapshot_month_count")
            ),
            "effective_month_count": _json_value(
                row.get("effective_month_count")
            ),
        }
        if not reason_codes
        else {}
    )
    attestation: dict[str, Any] = {
        "contract_version": _FORMAL_DSO_ATTESTATION_VERSION,
        "status": "verified" if not reason_codes else "undefined",
        "guards": guards,
        "authorized_components": (
            [
                "formal_receivable_turnover_value",
                "average_net_debt",
                "same_period_gross_delivery_amount",
                "gross_delivery_denominator_semantics",
                "period_natural_days",
                "snapshot_month_count",
                "effective_month_count",
            ]
            if not reason_codes
            else []
        ),
        "component_values": component_values,
        "undefined_reason_codes": reason_codes,
        "request_id": str(request["request_id"]),
        "metric_ref": metric_ref,
        "scope_fingerprint": scope_fingerprint,
        "projection_fingerprint": projection_fingerprint,
    }
    canonical = json.dumps(
        attestation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    attestation["attestation_seal"] = (
        "sha256_" + hashlib.sha256(canonical).hexdigest()
    )
    return attestation


def _attach_formal_dso_calculation_attestations(
    *,
    request: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    applied_time_range: Mapping[str, Any],
    metric_ref: str | None,
    scope_fingerprint: str,
    projection_fingerprint: str,
    disclosure_ledger: Sequence[Mapping[str, Any]],
    disclosure_ledger_seal: str,
) -> None:
    if (
        request.get("domain") != "receivable"
        or request.get("metric") != "formal_receivable_turnover_days"
    ):
        return
    for row, claim in zip(rows, claims):
        if not isinstance(row, Mapping) or not isinstance(claim, dict):
            continue
        attestation = _formal_dso_calculation_attestation(
            request=request,
            row=row,
            applied_time_range=applied_time_range,
            metric_ref=metric_ref,
            scope_fingerprint=scope_fingerprint,
            projection_fingerprint=projection_fingerprint,
            disclosure_ledger=disclosure_ledger,
            disclosure_ledger_seal=disclosure_ledger_seal,
        )
        facts = claim.get("facts")
        if attestation is not None and isinstance(facts, dict):
            component_values = attestation.get("component_values")
            if attestation.get("status") == "verified" and isinstance(
                component_values,
                Mapping,
            ):
                for fact_name in _FORMAL_DSO_ATTESTED_FACTS:
                    facts[fact_name] = copy.deepcopy(component_values[fact_name])
            facts["calculation_attestation"] = attestation


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
        "requires_completeness_proof": (
            isinstance(capability, Mapping)
            and capability.get("mode") == "additive_partition"
        ),
    }


def _claim_triplet(claim: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
    facts = claim.get("facts")
    states = claim.get("states")
    if not isinstance(facts, Mapping) or not isinstance(states, Mapping):
        return None
    if not _comparison_is_complete(
        facts,
        states,
        require_state_evidence=True,
    ):
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


_COMPARISON_COMPLETENESS_COUNT_FIELDS = (
    "current_missing_value_count",
    "current_known_value_count",
    "comparison_missing_value_count",
    "comparison_known_value_count",
)


def _comparison_completeness_evidence(
    claim: Mapping[str, Any],
    *,
    allow_not_present: bool,
) -> tuple[str, dict[str, int] | None]:
    facts = claim.get("facts")
    states = claim.get("states")
    if not isinstance(facts, Mapping) or not isinstance(states, Mapping):
        return "unavailable", None
    counts = {
        field: _exact_nonnegative_int(facts.get(field))
        for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
    }
    if any(value is None for value in counts.values()):
        return "invalid", None
    normalized_counts = {field: int(value) for field, value in counts.items()}
    for prefix in ("current", "comparison"):
        state = str(states.get(f"{prefix}_metric_data_state") or "").casefold()
        missing = normalized_counts[f"{prefix}_missing_value_count"]
        known = normalized_counts[f"{prefix}_known_value_count"]
        if state == "complete" and missing == 0 and known > 0:
            continue
        if allow_not_present and state == "not_present" and missing == known == 0:
            continue
        if state in {"missing", "incomplete"}:
            return "incomplete", normalized_counts
        return "invalid", normalized_counts
    return "complete", normalized_counts


def _summed_completeness_counts(
    evidence_items: Sequence[Mapping[str, int]],
) -> dict[str, int]:
    return {
        field: sum(item[field] for item in evidence_items)
        for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
    }


def _partition_proof_completeness_counts(
    proof: Mapping[str, Any] | None,
) -> dict[str, int] | None:
    if not isinstance(proof, Mapping):
        return None
    counts = {
        field: _exact_nonnegative_int(proof.get(field))
        for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
    }
    if any(value is None for value in counts.values()):
        return None
    return {field: int(value) for field, value in counts.items()}


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
        requires_completeness_proof = (
            context.get("requires_completeness_proof") is True
        )
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
            or requires_completeness_proof
            != (overall_context.get("requires_completeness_proof") is True)
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
            and (
                not requires_completeness_proof
                or _partition_proof_completeness_counts(partition_proof)
                is not None
            )
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
        completeness_reconciliation: dict[str, Any] | None = None
        if requires_completeness_proof:
            overall_coverage_status, overall_coverage = (
                _comparison_completeness_evidence(
                    overall_claims[0],
                    allow_not_present=False,
                )
            )
            driver_coverage = [
                _comparison_completeness_evidence(
                    claim,
                    allow_not_present=True,
                )
                for claim in driver_claims
            ]
            if (
                overall_coverage_status != "complete"
                or overall_coverage is None
                or any(status != "complete" or counts is None for status, counts in driver_coverage)
            ):
                continue
            returned_coverage = _summed_completeness_counts(
                [
                    counts
                    for _status, counts in driver_coverage
                    if counts is not None
                ]
            )
            partition_coverage = (
                _partition_proof_completeness_counts(partition_proof)
                if bounded_proof_valid
                else returned_coverage
            )
            if partition_coverage != overall_coverage:
                continue
            completeness_reconciliation = {
                "policy": "exact_integer_counts_both_periods",
                "overall": overall_coverage,
                "partition_totals": partition_coverage,
                "returned_partition_totals": returned_coverage,
            }
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
        distribution = (
            partition_proof.get("change_distribution") if bounded_proof_valid
            else evidence.distribution_from_deltas([item[2] for item in complete_driver_triplets])
        )
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
        if distribution is not None:
            driver["_change_reconciliation_pending"]["change_distribution"] = distribution
        if completeness_reconciliation is not None:
            driver["_change_reconciliation_pending"][
                "completeness_proof"
            ] = completeness_reconciliation


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
    *,
    requires_completeness_proof: bool = False,
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
        or not isinstance(overall.get("business_metric_ref"), str)
        or not overall.get("business_metric_ref")
        or overall.get("business_metric_ref")
        != partition.get("business_metric_ref")
        or not isinstance(overall.get("business_metric_unit"), str)
        or not overall.get("business_metric_unit")
        or overall.get("business_metric_unit")
        != partition.get("business_metric_unit")
        or not isinstance(overall.get("applied_time_range"), Mapping)
        or overall.get("applied_time_range")
        != partition.get("applied_time_range")
    ):
        return "SCOPE_MISMATCH"
    overall_claims = overall.get("claim_ledger")
    partition_claims = partition.get("claim_ledger")
    if requires_completeness_proof:
        if not isinstance(overall_claims, list) or len(overall_claims) != 1:
            return "DATA_COVERAGE_PROOF_UNAVAILABLE"
        overall_status, overall_coverage = _comparison_completeness_evidence(
            overall_claims[0],
            allow_not_present=False,
        )
        if overall_status == "incomplete":
            return "DATA_COVERAGE_INCOMPLETE"
        if overall_status != "complete" or overall_coverage is None:
            return "DATA_COVERAGE_PROOF_INVALID"
        if not isinstance(partition_claims, list) or not partition_claims:
            return "DATA_COVERAGE_PROOF_UNAVAILABLE"
        partition_coverage_items = [
            _comparison_completeness_evidence(
                claim,
                allow_not_present=True,
            )
            for claim in partition_claims
        ]
        if any(status == "incomplete" for status, _counts in partition_coverage_items):
            return "DATA_COVERAGE_INCOMPLETE"
        if any(
            status != "complete" or counts is None
            for status, counts in partition_coverage_items
        ):
            return "DATA_COVERAGE_PROOF_INVALID"
        returned_coverage = _summed_completeness_counts(
            [
                counts
                for _status, counts in partition_coverage_items
                if counts is not None
            ]
        )
        if partition.get("truncated") is True:
            partition_proof = partition.get("complete_partition_proof")
            if not isinstance(partition_proof, Mapping) or any(
                field not in partition_proof
                for field in _COMPARISON_COMPLETENESS_COUNT_FIELDS
            ):
                return "DATA_COVERAGE_PROOF_UNAVAILABLE"
            partition_coverage = _partition_proof_completeness_counts(
                partition_proof
            )
            if partition_coverage is None:
                return "DATA_COVERAGE_PROOF_INVALID"
        else:
            partition_coverage = returned_coverage
        if partition_coverage != overall_coverage:
            return "DATA_COVERAGE_PROOF_MISMATCH"
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
    contexts: Sequence[Mapping[str, Any] | None] = (),
) -> None:
    """Expose a typed fail-closed outcome only for the new explicit operation."""

    result_by_id = {
        str(result.get("request_id")): result
        for result in results
        if isinstance(result, dict) and isinstance(result.get("request_id"), str)
    }
    context_by_id = {
        str(context["request"]["request_id"]): context
        for context in contexts
        if isinstance(context, Mapping)
        and isinstance(context.get("request"), Mapping)
        and isinstance(context["request"].get("request_id"), str)
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
                requires_completeness_proof=(
                    context_by_id.get(partition_id, {}).get(
                        "requires_completeness_proof"
                    )
                    is True
                ),
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
        or states.get("actual_data_state") not in {"reported", "set"}
        or states.get("period_state") in {"not_started", "includes_future"}
        or not _target_status_is_coherent(facts, states)
    ):
        return None
    target = _finite_decimal(facts.get("target_amount_rmb"))
    actual = _finite_decimal(facts.get("actual_amount_rmb"))
    gap = _finite_decimal(facts.get("gap_amount_rmb"))
    completion = _finite_decimal(facts.get("completion_rate"))
    metric_value = _finite_decimal(facts.get("metric_value"))
    if not _target_amounts_consistent(target, actual, gap):
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
    contract: capability_contract.TargetGapContract,
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
        not in contract.valid_target_data_states
        or claim["states"].get("actual_data_state") not in {"reported", "set"}
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

    if not operation_partitions:
        return
    contract = _target_gap_contract()
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
        reason = _target_gap_failure_reason(overall, partition, contract)
        if reason != "RECONCILIATION_NOT_ESTABLISHED" or not isinstance(overall, Mapping):
            partition["target_gap_reconciliation"] = {
                "status": "not_reconciled",
                "operation": contract.receipt_operation,
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
            "version": contract.receipt_version,
            "status": "reconciled",
            "operation": contract.receipt_operation,
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
            "interpretation_boundary": contract.receipt_interpretation_code,
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
    dimensions = _effective_dimensions(semantics, request.get("metric"))
    if not isinstance(dimensions, Mapping):
        return []
    labels: list[str] = []
    for dimension in requested:
        definition = dimensions.get(dimension)
        label = definition.get("label") if isinstance(definition, Mapping) else None
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def _effective_dimension_request(
    request: Mapping[str, Any], scope: Mapping[str, Any], semantics: Mapping[str, Any]
) -> dict[str, Any]:
    """Use the compiler's final logical grain at every public projection boundary."""
    chosen = scope.get("effective_dimensions", request.get("dimensions") or [])
    definitions = semantics.get("dimensions")
    if (
        not isinstance(chosen, list)
        or any(not isinstance(code, str) for code in chosen)
        or len(chosen) != len(set(chosen))
        or not isinstance(definitions, Mapping)
        or any(code not in definitions for code in chosen)
    ):
        raise QueryFailure("CONTRACT_UNAVAILABLE", "查询的有效分组维度元信息无效。")
    return {**request, "dimensions": list(chosen)}


def _business_dimension_bindings(
    request: Mapping[str, Any],
    scope: Mapping[str, Any],
    semantics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind each logical dimension to ordered, business-safe display fields."""

    requested = request.get("dimensions") or []
    outputs = scope.get("dimension_outputs") or []
    dimensions = _effective_dimensions(semantics, request.get("metric"))
    if (
        not isinstance(requested, list)
        or not isinstance(outputs, list)
        or not isinstance(dimensions, Mapping)
    ):
        return []
    public_outputs = {
        output for output in outputs if isinstance(output, str) and output
    }
    identity_outputs = scope.get("identity_outputs") or {}
    if not isinstance(identity_outputs, Mapping):
        identity_outputs = {}
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
        identity_fields: list[str] = []
        identity_filter = definition.get("identity_filter")
        if isinstance(identity_filter, Mapping):
            identity_column = identity_filter.get("column")
            if isinstance(identity_column, str) and identity_column in declared_outputs:
                identity_fields.append(identity_column)
            else:
                identity_output = identity_outputs.get(dimension)
                if isinstance(identity_output, str) and identity_output:
                    identity_fields.append(identity_output)
        approved_display = definition.get("public_display_fields", [])
        declared_fields = {output for _column, output in _dimension_columns(definition)}
        if (
            not isinstance(approved_display, list)
            or any(not isinstance(field, str) or field.startswith("_") or field.casefold() in {"id", "no"} for field in approved_display)
            or not set(approved_display) <= declared_fields
        ):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "业务标识展示合同无效。")
        approved_display = [field for field in approved_display if field in public_outputs]
        safe_outputs = [
            output
            for output in declared_outputs
            if output.casefold() != "id"
            and (not output.casefold().endswith("_id") or output in approved_display)
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
            binding_metadata: dict[str, Any] = {}
            if (
                dimension == "executor"
                and identity_fields
                and isinstance(identity_filter, Mapping)
                and identity_filter.get("column") == "executor_erp_id"
            ):
                binding_metadata = {
                    "entity_ref_scope": "filter_identity",
                    "source_group_identity": "source_executor_id",
                    "source_group_ref_field": "pattern_executor_ref",
                }
            bindings.append(
                {
                    "dimension": str(dimension),
                    "fields": candidates,
                    "label": label.strip(),
                    **({"identity_fields": identity_fields} if identity_fields else {}),
                    **binding_metadata,
                    **({"public_display_fields": approved_display} if approved_display else {}),
                }
            )
    return bindings


def _public_comparison_alignment(value: Any) -> dict[str, Any]:
    fields = (
        "version",
        "kind",
        "coverage",
        "observed_on",
        "requested_current_start",
        "requested_current_end",
        "effective_current_end",
        "current_was_clipped",
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != set(fields)
        or value.get("version") != _MATCHED_ELAPSED_COMPARISON_VERSION
        or value.get("kind") != YEAR_OVER_YEAR_COMPARISON
        or value.get("coverage") != MATCHED_ELAPSED_COVERAGE
        or not isinstance(value.get("current_was_clipped"), bool)
        or any(
            _period_boundary_date(value.get(field)) is None
            for field in (
                "observed_on",
                "requested_current_start",
                "requested_current_end",
                "effective_current_end",
            )
        )
    ):
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "同比匹配覆盖证据无效。",
        )
    return {field: copy.deepcopy(value[field]) for field in fields}


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
    handler = analytical_handlers.handler_for_time(source)
    if handler is not None:
        return handler.resolve("time_projection")(value)
    if source == "monthly_slow_pool_observation":
        return {k:v for k,v in value.items() if k in {"source","closing_basis","whitelist_basis"} or k.startswith("monthly_")}
    if source == "frozen_baseline_recorded_window":
        return {k: v for k, v in value.items() if k in {
            "source", "baseline_week", "frozen_at", "read_at", "read_utc_at",
            "observed_db_utc_offset_seconds", "window_start", "window_end",
            "requested_window_end", "window_coverage",
        }}
    if source == "fabric_source_observation":
        return {k: v for k, v in value.items() if k in {
            "source", "basis", "window_start", "window_end", "inventory_scope",
            "fabric_read_at", "fabric_read_utc_at", "fabric_etl_min", "fabric_etl_max",
            "fabric_etl_time_count", "fabric_missing_etl", "fabric_scope_rows",
        }}
    if source == "pattern_current_observation":
        return {k:v for k,v in value.items() if k in {"source","basis","window_start","window_end","pattern_read_at","pattern_read_utc_at","pattern_task_modified_max","pattern_execute_modified_max"}}
    if source == "frozen_baseline_to_current":
        return {k: v for k, v in value.items() if k in {"source", "baseline_week", "frozen_at", "read_at", "read_utc_at", "observed_db_utc_offset_seconds"}}
    if source in {"current_snapshot", "latest_snapshot", "latest_non_null_snapshot"}:
        return {"source": source}
    if source in {"latest_complete_accounting_months", "latest_available_accounting_months"} and isinstance(value.get("months"), int):
        return {"source": source, "months": value["months"]}
    if source == "latest_snapshot_offset" and isinstance(value.get("months_before"), int):
        return {"source": source, "months_before": value["months_before"]}
    nested: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "查询时间范围结构无效。")
        nested[key] = (
            _public_comparison_alignment(item)
            if key == "comparison_alignment"
            else _public_time_range(item)
        )
    if nested:
        return nested
    raise QueryFailure("CONTRACT_UNAVAILABLE", "查询结果缺少明确的时间或快照范围。")


def _normalized_snapshot_month(value: Any) -> str:
    text = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}", text):
            parsed = datetime.strptime(text, "%Y-%m")
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "业务快照月份证据无效。",
            stage="result_validation",
        )
    return parsed.strftime("%Y-%m")


def _resolve_snapshot_time_evidence(
    value: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    data_state: str,
    *,
    evidence_field: str = _INTERNAL_SNAPSHOT_MONTH,
) -> tuple[dict[str, Any], str]:
    """Bind model-visible snapshot scope to an internal same-query month."""

    source = value.get("source")
    if source == "latest_available_accounting_months":
        periods = {
            (_normalized_snapshot_month(row["effective_start_month"]),
             _normalized_snapshot_month(row["operating_end_month"]))
            for row in rows
            if row.get("effective_start_month") and row.get("operating_end_month")
        }
        if not periods:
            return {**value, "resolution_state": "unavailable"}, data_state
        if len(periods) != 1:
            raise QueryFailure("CONTRACT_UNAVAILABLE", "库存周转期间证据不一致。", stage="result_validation")
        start_month, end_month = next(iter(periods))
        start = _calendar_month_time_range(start_month)["start"]
        end = _calendar_month_time_range(end_month)["end"]
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        months = (last.year - first.year) * 12 + last.month - first.month
        if months != value.get("months") or any(row.get("period_natural_days") != (last-first).days for row in rows):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "库存周转月份数或自然日证据不一致。", stage="result_validation")
        return {"start": start, "end": end, "source": source}, data_state
    if source in _SNAPSHOT_TIME_SOURCES:
        observed_field = any(evidence_field in row for row in rows)
        months: set[str] = set()
        for row in rows:
            raw_month = row.get(evidence_field)
            if raw_month is None or raw_month == "":
                continue
            months.add(_normalized_snapshot_month(raw_month))
        if len(months) > 1:
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "同一查询返回了不一致的业务快照月份。",
                stage="result_validation",
            )
        public = {key: item for key, item in value.items() if key != "required_measure"}
        if months:
            public.update(
                {
                    "snapshot_month": next(iter(months)),
                    "resolution_state": "resolved",
                }
            )
            return public, data_state
        if not rows:
            public["resolution_state"] = "no_matching_data"
            return public, data_state
        if not observed_field:
            public["resolution_state"] = "evidence_unavailable"
            return public, data_state
        if source == "latest_non_null_snapshot":
            public["resolution_state"] = "required_value_unavailable"
            return public, "undefined"
        public["resolution_state"] = "no_snapshot_data"
        return public, data_state

    if "start" in value or "end" in value or source is not None:
        return dict(value), data_state

    resolved: dict[str, Any] = {}
    resolved_state = data_state
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "查询时间范围结构无效。",
                stage="result_validation",
            )
        if key == "comparison_alignment":
            resolved[key] = _public_comparison_alignment(item)
            continue
        field = (
            _INTERNAL_COMPARISON_SNAPSHOT_MONTH
            if key == "comparison"
            else _INTERNAL_SNAPSHOT_MONTH
        )
        resolved[key], resolved_state = _resolve_snapshot_time_evidence(
            item,
            rows,
            resolved_state,
            evidence_field=field,
        )
    return resolved, resolved_state


def _normalized_as_of_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("invalid as-of date")
    return date.fromisoformat(value).isoformat()


def _resolve_current_as_of_date_evidence(
    value: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    data_state: str,
) -> tuple[dict[str, Any], str, bool]:
    """Bind one contract-selected current snapshot to its same-query date."""

    if value.get("source") != "current_snapshot":
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "当前快照日期证据与时间范围不一致。",
            stage="result_validation",
        )
    public = dict(value)
    as_of_basis = public.get("as_of_basis")
    if as_of_basis not in {None, _DATABASE_QUERY_DATE_OBSERVATION}:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            "当前快照观察日期口径无效。",
            stage="result_validation",
        )
    dates: set[str] = set()
    missing = not rows
    invalid = False
    for row in rows:
        raw_date = row.get(_INTERNAL_AS_OF_DATE)
        if _INTERNAL_AS_OF_DATE not in row or raw_date is None or raw_date == "":
            missing = True
            continue
        try:
            dates.add(_normalized_as_of_date(raw_date))
        except (TypeError, ValueError):
            invalid = True
    if invalid:
        public["resolution_state"] = "as_of_date_invalid"
        return public, "undefined", True
    if len(dates) > 1:
        public["resolution_state"] = "as_of_date_conflicting"
        return public, "undefined", True
    if missing or not dates:
        public["resolution_state"] = "as_of_date_unavailable"
        return public, "undefined", True
    public.update(
        {
            "as_of_date": next(iter(dates)),
            "resolution_state": "resolved",
        }
    )
    return public, data_state, False


def _scope_texts(value: Any) -> list[str]:
    """Render validated time metadata as business language, preserving order."""
    if not isinstance(value, Mapping):
        return []
    start, end = value.get("start"), value.get("end")
    if isinstance(start, str) and isinstance(end, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}", end):
                inclusive_end = datetime.strptime(end, "%Y-%m") - timedelta(days=1)
                rendered = f"{start} 至 {inclusive_end.strftime('%Y-%m')}"
            else:
                start_date = datetime.strptime(start, "%Y-%m-%d")
                inclusive_end = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)
                rendered = (
                    f"{start_date.strftime('%Y-%m-%d')} 至 "
                    f"{inclusive_end.strftime('%Y-%m-%d')}"
                )
            calendar_evidence = value.get("calendar_evidence")
            calendar_evidence = (
                calendar_evidence
                if isinstance(calendar_evidence, Mapping)
                else {}
            )
            observed_on = calendar_evidence.get("observed_on")
            if calendar_evidence.get("period_state") == "in_progress" and isinstance(
                observed_on, str
            ):
                rendered += (
                    f"（查询日 {observed_on}，期间进行中；数据新鲜度未证明）"
                )
            elif calendar_evidence.get("period_state") == "not_started":
                rendered += "（期间尚未开始）"
            return [rendered]
        except ValueError:
            return []
    source = value.get("source")
    as_of_date = value.get("as_of_date")
    handler = analytical_handlers.handler_for_time(source)
    if handler is not None:
        return handler.resolve("time_description")(value)
    if source == "monthly_slow_pool_observation":
        closing = "本次当前登记池" if value.get("closing_basis")=="current_ods" else f"{value.get('monthly_month')}物理月末快照"
        return [f"独立月报{value.get('monthly_month')}；期初{value.get('monthly_opening_month')}物理快照、期末{closing}；两端使用本次同一库存单位白名单组合；流水{value.get('monthly_window_start')}至{value.get('monthly_window_end')}（结束不含），读取{value.get('monthly_read_at')}；当月不代表完整月结"]
    if source == "frozen_baseline_recorded_window":
        partial = f"；原请求结束{value.get('requested_window_end')}，期间未完，仅截至本次读取" if value.get("window_coverage") == "partial_to_read" else ""
        return [f"基线{value.get('baseline_week')}（记录冻结{value.get('frozen_at')}）；已记录流水{value.get('window_start')}至{value.get('window_end')}（结束不含），读取{value.get('read_at')}，均为库端时间{partial}；不表示历史期末库存"]
    if source == "fabric_source_observation":
        basis = "源出库已记录事件" if value.get("basis") == "recorded_delivery_history" else "源库存快照（" + ("可确认在仓" if value.get("inventory_scope") == "on_hand" else "完整源范围，含在途") + "）"
        window = f"，{value.get('window_start')}至{value.get('window_end')}（结束不含）" if value.get("window_start") else ""
        return [f"{basis}{window}；本次库端读取{value.get('fabric_read_at')}，ETL时点不代表完整批次"]
    if source == "pattern_current_observation":
        basis = {"current_observation":"当前全范围观察","task_created":"任务创建期间队列","execution_completed":"执行完成期间记录","linked_delivery":"已关联出库实际发生期间"}.get(value.get("basis"),"找版观察")
        window = f"，{value.get('window_start')}至{value.get('window_end')}（不含结束日）" if value.get("window_start") else ""
        return [f"{basis}{window}，读取时点{value.get('pattern_read_at')}（库端原值）"]
    if source == "frozen_baseline_to_current":
        return [f"基线{value.get('baseline_week')}，记录冻结{value.get('frozen_at')}至本次读取{value.get('read_at')}（库端时间原值）"]
    if source == "current_snapshot" and isinstance(as_of_date, str):
        if value.get("as_of_basis") == _DATABASE_QUERY_DATE_OBSERVATION:
            return [f"截至 {as_of_date} 查询时观察到的当前库存快照"]
        return [f"截至 {as_of_date} 的当前业务快照"]
    snapshot_month = value.get("snapshot_month")
    if isinstance(snapshot_month, str):
        if source == "latest_non_null_snapshot":
            return [f"{snapshot_month} 最新有值业务月度快照"]
        if source == "latest_snapshot_offset" and isinstance(
            value.get("months_before"), int
        ):
            return [
                f"{snapshot_month} 业务月度快照"
                f"（距最新快照 {value['months_before']} 个月）"
            ]
        if source == "latest_snapshot":
            return [f"{snapshot_month} 业务月度快照"]
    if source == "current_snapshot":
        if value.get("resolution_state") in {
            "as_of_date_unavailable",
            "as_of_date_invalid",
            "as_of_date_conflicting",
        }:
            return ["当前业务快照截至日期证据不可用"]
        return ["当前业务快照"]
    if source == "latest_snapshot":
        return ["业务月度快照月份证据不可用"]
    if source == "latest_non_null_snapshot":
        if value.get("resolution_state") == "required_value_unavailable":
            return ["最新有值业务快照不可用"]
        return ["最新有值业务快照月份证据不可用"]
    if source in {"latest_complete_accounting_months", "latest_available_accounting_months"} and isinstance(value.get("months"), int):
        return [f"最近 {value['months']} 个完整会计月"]
    if source == "latest_snapshot_offset" and isinstance(value.get("months_before"), int):
        return [f"业务月度快照月份证据不可用（偏移 {value['months_before']} 个月）"]
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
    error = _public_error(failure)
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
                exc.code,
                str(exc),
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
        canonical = json.dumps(
            reference,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        payload["source_evidence_ref"] = {
            "schema": "datasage-query-model-source-reference/v1",
            "source_ref_sha256": hashlib.sha256(canonical).hexdigest(),
        }






































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
            **_caller_retry_metadata(failure),
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
        if not values or any(
            not capability_contract.is_value_scalar(item) for item in values
        ):
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
        | {"time_range", "comparison", "metric_filters", "_entity_bindings", "currency_basis", "_currency_basis_plan"}
    }
    calculation_definition = dict(metric_definition)
    if "disclosures" in calculation_definition:
        calculation_definition["disclosures"] = [d for d in (metric_definition.get("disclosures") or []) if d.get("id") != "currency.basis.selection"]
    basis = {
        "request": non_temporal_request,
        "metric_contract": calculation_definition,
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
        "group_dimensions": list(scope.get("effective_dimensions", request.get("dimensions") or [])),
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
    if not evidence.claim_is_valid_for_result(claim, result):
        raise QueryFailure(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算引用的查询证据未通过完整性校验。",
        )
    projected_result = _model_wire_result(result)
    projected_claims = projected_result.get("claim_ledger")
    if (
        projected_result.get("status") != "success"
        or not isinstance(projected_claims, list)
        or len(projected_claims) != 1
        or projected_claims[0] != claim
    ):
        raise QueryFailure(
            "CALCULATION_SOURCE_INTEGRITY_INVALID",
            "计算引用的查询证据未通过模型边界完整性校验。",
        )
    claim = projected_claims[0]
    dimensions = claim.get("dimensions")
    scope_entities = claim.get("scope_entities")
    period = claim.get("period")
    facts = claim.get("facts")
    unit = claim.get("unit")
    claim_id = claim.get("claim_id")
    claim_seal = claim.get("claim_seal")
    scope_fingerprint = claim.get("scope_fingerprint")
    projection_fingerprint = claim.get("projection_fingerprint")
    calculation_scope = result.get("_calculation_scope")
    if (
        dimensions != [] or not isinstance(scope_entities, list)
        or (isinstance(calculation_scope, Mapping) and calculation_scope.get("group_dimensions"))
    ):
        raise QueryFailure(
            "CALCULATION_REQUIRES_SCALAR",
            "带分组维度的结果不能作为标量计算操作数。",
        )
    if (
        not isinstance(period, Mapping)
        or not isinstance(facts, Mapping)
        or not isinstance(claim_id, str)
        or not isinstance(claim_seal, str)
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
        "claim_seal": claim_seal,
        "metric_ref": claim.get("metric_ref"),
        "value": value,
        "unit": unit,
        "currency": claim.get("currency"),
        "currency_state": claim.get("currency_state"),
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
    *,
    observed_on: date | None = None,
) -> list[dict[str, Any]]:
    calculation_observed_on = observed_on or _business_today()
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
            if (left.get("currency") != right.get("currency")
                    or left.get("currency_state") == "unknown" or right.get("currency_state") == "unknown"
                    or ("原币" in left["unit"] and not left.get("currency"))):
                raise QueryFailure("CALCULATION_CURRENCY_MISMATCH", "计算两侧币种不兼容或未知；跨币种联合计算须将两侧改用 currency_basis=rmb 重新查询，保留各自有效独立观察。")
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
            left_period_evidence = _annotate_period_evidence(
                left["period"], calculation_observed_on
            )
            right_period_evidence = _annotate_period_evidence(
                right["period"], calculation_observed_on
            )
            period_compatibility = assess_period_compatibility(
                left_period_evidence,
                right_period_evidence,
            ) or {
                "status": "not_assessable",
                "reason_codes": ["PERIOD_COMPARABILITY_NOT_ASSESSABLE"],
            }
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
                    "claim_seal": operand["claim_seal"],
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
                "allowed_relations": (
                    ["derived_observation"]
                    if period_compatibility.get("status") == "compatible"
                    else []
                ),
                "relation_semantics": {
                    "derived_observation": (
                        "arithmetic_not_registered_metric_or_causal_evidence"
                    )
                },
                "operands": operands,
                "value": _json_value(value),
                "unit": output_unit,
                "scope_compatibility": scope_compatibility,
                "period_compatibility": period_compatibility,
                "limitations": [
                    "NOT_A_REGISTERED_METRIC",
                    "NOT_STRUCTURAL_CONTRIBUTION",
                    "NOT_CAUSAL_EVIDENCE",
                    *period_compatibility["reason_codes"],
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














def _validate_request_plan_without_entities(
    raw_request: Any,
    *,
    observed_on: date | None = None,
    request_path: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate one branch through the last database-free planning boundary."""

    try:
        request = _validate_inventory_metric_scope(
            _validate_request(raw_request, request_path=request_path)
        )
    except QueryFailure as exc:
        raise _at_stage(exc, "input_validation")
    try:
        datasets, semantics = _contracts(request["domain"])
    except QueryFailure as exc:
        raise _at_stage(exc, "contract_load")
    try:
        request = currency_basis.prepare_request(request, semantics)
        request = _validate_delivery_metric_scope(request, semantics)
        request = _validate_metric_contract(request, semantics)
        _validate_pre_entity_metric_plan(
            request,
            datasets,
            semantics,
            observed_on=observed_on,
        )
    except QueryFailure as exc:
        raise _at_stage(exc, "input_validation")
    return request, datasets, semantics


def _validate_query_dispatch(
    args: Any,
    *,
    observed_on: date,
    branch_failures: dict[str, QueryFailure] | None = None,
) -> request_contract.ValidatedQueryEnvelope:
    """Validate the envelope globally; optionally retain attributable branch failures."""

    envelope = _validated_query_envelope(args)
    for index, raw_request in enumerate(envelope.requests):
        request_path = f"requests[{index}]"
        try:
            if any(str(key).startswith("_") for key in raw_request):
                raise QueryFailure("INVALID_INPUT", "调用方不能提供内部执行字段。", stage="input_validation")
            request, _datasets, semantics = _validate_request_plan_without_entities(
                raw_request,
                observed_on=observed_on,
                request_path=request_path,
            )
            change_operation = request.get("complete_change_decomposition")
            if isinstance(change_operation, Mapping):
                _validate_complete_decomposition_capability(
                    {"request": request, "semantics": semantics},
                    str(change_operation.get("dimension") or ""),
                )
            target_operation = request.get("complete_target_gap_decomposition")
            if isinstance(target_operation, Mapping):
                _validate_target_gap_decomposition_capability(
                    {"request": request, "semantics": semantics},
                    str(target_operation.get("dimension") or ""),
                )
        except QueryFailure as exc:
            if exc.path is None:
                exc.path = (
                    f"{request_path}.metric"
                    if exc.code
                    in {
                        "METRIC_NOT_REGISTERED",
                        "UNSUPPORTED_METRIC",
                        "METRIC_NOT_AVAILABLE",
                    }
                    else request_path
                )
            if exc.hint is None and exc.code in {
                "METRIC_NOT_REGISTERED",
                "UNSUPPORTED_METRIC",
            }:
                exc.hint = "Use an exact metric code returned by datasage_catalog."
            if branch_failures is None:
                raise
            branch_failures[str(raw_request["request_id"])] = exc
    return envelope


def _prepare_one(
    raw_request: Any,
    *,
    deadline_at: float | None,
    resolution_cache: dict[tuple[str, str], Any],
    max_unique_lookups: int,
    preflight_stats: dict[str, Any],
    period_observed_on: date | None = None,
) -> dict[str, Any]:
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise QueryFailure(
            "BATCH_DEADLINE_EXCEEDED",
            "本次批量查询已达到总时限。",
            timeout=True,
            stage="batch_deadline",
        )
    request, datasets, semantics = _validate_request_plan_without_entities(
        raw_request,
        observed_on=period_observed_on,
    )
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
    period_observed_on: date | None = None,
    preflight_source_evidence_refs: Sequence[Any] = (),
) -> dict[str, Any]:
    started = started_at if started_at is not None else time.monotonic()
    query_id = f"dq_{uuid.uuid4().hex[:16]}"
    request_id = raw_request.get("request_id", "unknown") if isinstance(raw_request, dict) else "unknown"
    domain = raw_request.get("domain") if isinstance(raw_request, dict) else None
    mode = raw_request.get("mode") if isinstance(raw_request, dict) else None
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
    if (isinstance(prepared, Mapping)
            and isinstance(prepared.get("request"), Mapping)
            and (prepared["request"].get("_currency_basis_plan") or {}).get("requires_probe")):
        business_metric_ref, failure_metric_label = _failure_metric_identity(
            raw_request, {"request": raw_request, "semantics": prepared.get("semantics")},
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
        basis_plan = request.get("_currency_basis_plan")
        if isinstance(basis_plan, Mapping) and basis_plan.get("requires_probe"):
            currency_basis.validate_pair_bindings(request, semantics)
            if execute_query is None:
                # Discovery and the selected metric share one read-only snapshot.
                with _ConsistentSnapshotExecutor(deadline_at=deadline_at) as snapshot:
                    return _run_one(
                        raw_request, deadline_at=deadline_at, audit_context=audit_context,
                        prepared=prepared, preflight_failure=preflight_failure,
                        preflight_db_call_count=preflight_db_call_count,
                        entity_preflight_elapsed_ms=entity_preflight_elapsed_ms,
                        started_at=started, execute_query=snapshot.execute,
                        snapshot_group_marker=snapshot_group_marker or snapshot.marker,
                        period_observed_on=period_observed_on,
                        preflight_source_evidence_refs=preflight_source_evidence_refs,
                    )
            current_stage = "currency_scope"
            probe_sql, probe_params = currency_basis.build_probe(
                request, datasets, semantics, _build_metric_query,
                observed_on=period_observed_on,
            )
            _check_call_deadline(deadline_at)
            business_sql_attempted_count += 1
            probe_rows, probe_truncated, probe_source = execute_query(
                probe_sql, probe_params, 1, deadline_at=deadline_at,
            )
            business_sql_confirmed_count += 1
            preflight_source_evidence_refs = (*preflight_source_evidence_refs, probe_source)
            source_evidence_ref = _consistent_source_evidence_ref(preflight_source_evidence_refs)
            request = currency_basis.resolve_probe(request, probe_rows, probe_truncated)
            business_metric_ref, failure_metric_label = _failure_metric_identity(request, {"request": request, "semantics": semantics})
            request = _validate_metric_contract(request, semantics)
            request = _validate_metric_filter_value_contracts(request, semantics)
        semantics = currency_basis.with_disclosure(request, semantics)
        current_stage = "query_planning"
        limit = _metric_query_limit(request)
        sql, params, scope = _build_metric_query(
            request,
            datasets,
            semantics,
            limit,
            observed_on=period_observed_on,
        )
        private_time_range = scope.get("time_range")
        applied_time_range = _public_time_range(private_time_range)
        current_snapshot_evidence = (
            private_time_range.get("current_snapshot_evidence")
            if isinstance(private_time_range, Mapping)
            else None
        )
        requires_current_as_of_date = (
            isinstance(private_time_range, Mapping)
            and private_time_range.get("source") == "current_snapshot"
            and current_snapshot_evidence
            in {
                _DATABASE_CURRENT_DATE_EVIDENCE,
                _DATABASE_QUERY_DATE_OBSERVATION,
            }
        )
        if current_snapshot_evidence == _DATABASE_QUERY_DATE_OBSERVATION:
            applied_time_range["as_of_basis"] = (
                _DATABASE_QUERY_DATE_OBSERVATION
            )
        _check_call_deadline(deadline_at)
        current_stage = "business_sql"
        business_sql_attempted_count += 1
        executor = execute_query or _execute_with_source
        handler = analytical_handlers.get_handler(semantics["metrics"][request["metric"]].get("query_kind"))
        if (scope.get("_validate_frozen_pool") is True or scope.get("_validate_monthly_pool") is True or scope.get("_validate_pattern_observation") is True or (handler is not None and handler.consistent_snapshot)) and execute_query is None:
            with _ConsistentSnapshotExecutor(deadline_at=deadline_at) as snapshot:
                rows, truncated, business_source_evidence_ref = snapshot.execute(
                    sql, params, limit, deadline_at=deadline_at
                )
        else:
            rows, truncated, business_source_evidence_ref = executor(
                sql, params, limit, deadline_at=deadline_at
            )
        source_evidence_ref = _consistent_source_evidence_ref(
            [*preflight_source_evidence_refs, business_source_evidence_ref]
        )
        business_sql_confirmed_count += 1
        proof_plan = scope.get("embedded_complete_partition_proof")
        if truncated and isinstance(proof_plan, Mapping):
            try:
                complete_partition_proof = _validated_embedded_partition_proof(
                    rows,
                    truncated=truncated,
                    returned_row_count=len(rows),
                    requires_completeness_proof=(
                        proof_plan.get("requires_completeness_proof") is True
                    ),
                )
            except QueryFailure as proof_failure:
                complete_partition_proof_failure = proof_failure.code
        _check_call_deadline(deadline_at)
        current_stage = "result_validation"
        if scope.get("_validate_frozen_pool") is True or scope.get("_validate_monthly_pool") is True:
            try:
                if scope.get("_validate_monthly_pool"):
                    from .monthly_slow_pool import validate_monthly_rows
                    applied_time_range = validate_monthly_rows(rows,flow=bool(scope.get("_monthly_flow")))
                else:
                    applied_time_range = validate_frozen_pool_rows(rows)
                if request.get("metric") == "registered_slow_pool_baseline_net_outbound" or scope.get("_monthly_flow"):
                    for row in rows:
                        if not row.get("__matched_row_count") and int(row.get("outbound_unknown_rows") or 0) + int(row.get("returns_unknown_rows") or 0) > 0:
                            raise QueryFailure("FLOW_SCOPE_UNASSESSABLE", "存在无法确认范围的流水，不能将没有可返回销售组解释为空流水。", stage="result_validation")
                        if "sales_id" in row and row.get("__matched_row_count"):
                            sales_id = row["sales_id"]
                            row["sales_identity_ref"] = ("unattributed" if sales_id is None else "sales_" + hashlib.sha256(f"flow-salesperson:{sales_id}".encode("utf-8")).hexdigest()[:16])
                scope["time_range"] = applied_time_range
            except AnalysisQueryError as error:
                raise QueryFailure(error.code, error.message, stage="baseline_validation") from error
        if handler is not None and handler.observer is not None:
            try:
                applied_time_range = handler.resolve("observer")(rows)
                scope["time_range"] = applied_time_range
            except AnalysisQueryError as error:
                raise QueryFailure(error.code,error.message,stage="result_validation") from error
        if scope.get("_validate_pattern_observation") is True:
            from .pattern_queries import pattern_observation
            try:
                applied_time_range = pattern_observation(rows, scope["time_range"])
                scope["time_range"] = applied_time_range
                for row in rows:
                    for role, column in (("task","task_id"),("customer","customer_id"),("salesperson","sales_id"),("executor","executor_id")):
                        if column in row and row.get("__matched_row_count"):
                            value = row[column]
                            row[f"pattern_{role}_ref"] = "unattributed" if value is None else role + "_" + hashlib.sha256(f"pattern-{role}:{value}".encode("utf-8")).hexdigest()[:16]
            except AnalysisQueryError as error:
                raise QueryFailure(error.code,error.message,stage="result_validation") from error
        if scope.get("_fabric_observation"):
            from .fabric_source_queries import fabric_observation
            try:
                applied_time_range = fabric_observation(rows, scope["time_range"])
            except AnalysisQueryError as error:
                raise QueryFailure(error.code, error.message, stage="result_validation") from error
            scope["time_range"] = applied_time_range
        public_rows, data_state = _evidence_rows_and_state(rows, truncated)
        applied_time_range, data_state = _resolve_snapshot_time_evidence(
            applied_time_range,
            rows,
            data_state,
        )
        if requires_current_as_of_date:
            applied_time_range, data_state, as_of_evidence_failed = (
                _resolve_current_as_of_date_evidence(
                    applied_time_range,
                    rows,
                    data_state,
                )
            )
            if as_of_evidence_failed:
                public_rows = [{"metric_value": None}]
                truncated = False
        applied_time_range = _annotate_period_evidence(
            applied_time_range,
            period_observed_on or _business_today(),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _check_call_deadline(deadline_at)
        metric_ref = _business_metric_ref(request)
        metric_label = _business_metric_label(scope, semantics)
        metric_context = _business_metric_context(scope, semantics, datasets)
        dimension_request = _effective_dimension_request(request, scope, semantics)
        dimension_labels = _business_dimension_labels(dimension_request, semantics)
        dimension_bindings = _business_dimension_bindings(dimension_request, scope, semantics)
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
        currency_scope = _public_currency_scope(request, metric_definition)
        result_fact_units = metric_definition.get("result_fact_units") or (
            capability_contract.TARGET_COMPLETION_FACT_UNITS
            if metric_definition.get("query_kind") == "target_completion"
            else None
        )
        if isinstance(currency_scope, Mapping):
            result_fact_units = {
                **(result_fact_units if isinstance(result_fact_units, Mapping) else {}),
                "unclassified_source_amount": "原币金额（币种未知，仅原始值证据，不可比较）",
                "unclassified_source_amount_min": "原币金额下界（币种未知，不可合计）",
                "unclassified_source_amount_max": "原币金额上界（币种未知，不可合计）",
                "unclassified_source_row_count": "币种缺失源记录数",
            }
        _validate_required_time_bucket_rows(metric_definition, public_rows)
        domain_dimensions = semantics.get("dimensions")
        if not isinstance(domain_dimensions, Mapping) or any(
            not isinstance(code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None
            for code in domain_dimensions
        ):
            raise QueryFailure(
                "CONTRACT_UNAVAILABLE",
                "业务域维度注册表无效。",
                stage="contract_load",
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
            request.get("metric"),
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
            fact_units=result_fact_units,
            currency_scope=currency_scope,
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
            known_dimension_codes=set(domain_dimensions),
            inventory_scope=scope.get("inventory_scope"),
            applied_time_range=applied_time_range,
        )
        _attach_formal_dso_calculation_attestations(
            request=request,
            rows=public_rows,
            claims=claim_ledger,
            applied_time_range=applied_time_range,
            metric_ref=metric_ref,
            scope_fingerprint=scope_fingerprint,
            projection_fingerprint=projection_fingerprint,
            disclosure_ledger=disclosure_ledger,
            disclosure_ledger_seal=disclosure_ledger_seal,
        )
        result = {
            "request_id": request["request_id"],
            "_period_additive_fields": _period_additive_fields(metric_definition, scope, datasets, semantics),
            "_ranking_plan": scope.get("ranking_plan"),
            "_resolved_currency_context": _decomposition_context({"request": request, "semantics": semantics}) if request.get("_currency_basis_plan") else None,
            "_resolved_currency_request": dict(request) if request.get("_currency_basis_plan") else None,
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
            "change_reconciliation": None,
            "complete_partition_proof": complete_partition_proof,
            "complete_partition_proof_failure": complete_partition_proof_failure,
            "row_count": len(public_rows),
            "truncated": truncated,
            "requested_limit": int(request.get("limit", _bounded_int("max_rows", 100, 1, 100))),
            "effective_limit": limit,
            "has_more": bool(truncated),
            "applied_time_range": applied_time_range,
            "error": None,
            "elapsed_ms": elapsed_ms,
            "entity_resolution_db_call_count": int(
                prepared.get("entity_resolution_db_call_count") or 0
            ),
            "entity_preflight_elapsed_ms": entity_preflight_elapsed_ms,
            "business_sql_attempted_count": business_sql_attempted_count,
            "business_sql_confirmed_count": business_sql_confirmed_count,
            "source_evidence_ref": source_evidence_ref,
        }
        if currency_scope is not None:
            result["currency_scope"] = copy.deepcopy(currency_scope)
        if isinstance(snapshot_group_marker, str) and snapshot_group_marker:
            result["_snapshot_group_marker"] = snapshot_group_marker
        _check_call_deadline(deadline_at)
        if deadline_at is not None:
            _seal_claim_ids([result])
            fallback = copy.deepcopy(_model_wire_result(result, request=request))
            _check_call_deadline(deadline_at)
            result["_deadline_fallback"] = fallback
    except QueryFailure as failure:
        source_evidence_ref = _consistent_source_evidence_ref(
            [
                *preflight_source_evidence_refs,
                *(
                    [source_evidence_ref]
                    if source_evidence_ref is not None
                    else []
                ),
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
            entity_resolution_db_call_count=preflight_db_call_count,
            entity_preflight_elapsed_ms=entity_preflight_elapsed_ms,
            business_sql_attempted_count=business_sql_attempted_count,
            business_sql_confirmed_count=business_sql_confirmed_count,
            source_evidence_ref=source_evidence_ref,
        )
    except Exception:
        logger.exception("datasage_query unexpected request failure query_id=%s", query_id)
        source_evidence_ref = _consistent_source_evidence_ref(
            [
                *preflight_source_evidence_refs,
                *(
                    [source_evidence_ref]
                    if source_evidence_ref is not None
                    else []
                ),
            ]
        )
        failure_stage = current_stage
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result = _failure_result(
            str(request_id),
            QueryFailure("INTERNAL_ERROR", "当前子查询暂时不可用。"),
            elapsed_ms,
            business_metric_ref=business_metric_ref,
            business_metric_label=failure_metric_label,
            entity_resolution_db_call_count=preflight_db_call_count,
            entity_preflight_elapsed_ms=entity_preflight_elapsed_ms,
            business_sql_attempted_count=business_sql_attempted_count,
            business_sql_confirmed_count=business_sql_confirmed_count,
            source_evidence_ref=source_evidence_ref,
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


def _call_deadline(candidate=None):
    deadline_at = time.monotonic() + _bounded_int("call_timeout_seconds", 60, 1, 300)
    for value in (candidate, db_executor.current_deadline()):
        if value is not None:
            deadline_at = min(deadline_at, value)
    return deadline_at


def _check_call_deadline(deadline_at):
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise QueryFailure("BATCH_DEADLINE_EXCEEDED", "本次查询已达到调用总时限。", timeout=True, stage="batch_deadline")


def _deadline_payload(args, results=()):
    """Retain immutable pre-expiry evidence and explicit outcomes for every input."""
    requests = args.get("requests") if isinstance(args, Mapping) else None
    requests = requests if isinstance(requests, list) else []
    public_ids = [str(item.get("request_id")) for item in requests if isinstance(item, Mapping)]
    by_id = {str(result.get("request_id")): result for result in results if isinstance(result, Mapping)}
    error = {"code": "BATCH_DEADLINE_EXCEEDED", "message": "本次查询已达到总时限；仅保留期限内已验证的独立证据，其余分支与计算未完成。", "retryable": True}
    public_results = []
    for request_id in public_ids:
        result = by_id.get(request_id, {})
        fallback = result.get("_deadline_fallback")
        if isinstance(fallback, dict):
            public_results.append(fallback)
        else:
            branch_error = result.get("error") if result.get("status") == "failed" else None
            public_results.append({"request_id": request_id, "status": "failed", "rows": [],
                "claim_ledger": [], "error": branch_error or error})
    return {
        "status": "partial" if any(result.get("status") in {"success", "partial"} for result in public_results) else "failed",
        "request_count": len(public_ids), "metric_contexts": [], "results": public_results, "error": error,
    }


def _datasage_query_with_slot(args: dict[str, Any], **_kwargs: Any) -> str:
    """Validate, execute, and return structured evidence for one to ten requests."""
    batch_started = time.monotonic()
    deadline_at = _call_deadline(_kwargs.pop("_deadline_at", None))
    results = []
    period_observed_on = _kwargs.pop("_period_observed_on", None)
    if not isinstance(period_observed_on, date):
        period_observed_on = _business_today()
    query_slot_owned = bool(_kwargs.pop("_query_slot_owned", False))
    try:
        validated_envelope = _kwargs.pop("_validated_query_envelope", None)
        if validated_envelope is None:
            validated_envelope = _validated_query_envelope(args)
        elif not isinstance(
            validated_envelope, request_contract.ValidatedQueryEnvelope
        ):
            raise QueryFailure("INTERNAL_ERROR", "查询工具暂时不可用。")
        requests = list(validated_envelope.requests)
        request_ids = list(validated_envelope.request_ids)
        calculations = [
            dict(calculation) for calculation in validated_envelope.calculations
        ]
        public_requests = list(requests)
        preflight_failures = _kwargs.pop("_branch_preflight_failures", {})
        requests, local_branch_failures = _allocate_public_request_branches(
            public_requests, preflight_failures=preflight_failures
        )
        requests, operation_partitions = _expand_complete_change_decompositions(
            requests,
            reserved_request_ids=[str(request_id) for request_id in request_ids],
        )
        requests, target_gap_partitions = (
            _expand_complete_target_gap_decompositions(
                requests,
                reserved_request_ids=[str(request_id) for request_id in request_ids],
            )
        )
        _check_call_deadline(deadline_at)
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
                    period_observed_on=period_observed_on,
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
                    period_observed_on=period_observed_on,
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
                        period_observed_on=period_observed_on,
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
                    period_observed_on=period_observed_on,
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
                            period_observed_on=period_observed_on,
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
                        period_observed_on=period_observed_on,
                    )
                )
        _check_call_deadline(deadline_at)
        prepared_contexts = [result.get("_resolved_currency_context") or context for context, result in zip(prepared_contexts, results)]
        _authorize_change_decompositions(prepared_contexts, results)
        _check_call_deadline(deadline_at)
        _tag_complete_decomposition_reconciliations(
            results,
            operation_partitions,
        )
        _check_call_deadline(deadline_at)
        _seal_claim_ids(results)
        _seal_change_reconciliations(results)
        _check_call_deadline(deadline_at)
        _finalize_complete_decomposition_outcomes(
            results,
            operation_partitions,
            prepared_contexts,
        )
        _check_call_deadline(deadline_at)
        _finalize_target_gap_decompositions(
            prepared_contexts,
            results,
            target_gap_partitions,
        )
        physical_results = list(results)
        requests, results = _order_public_branch_artifacts(
            public_requests,
            requests,
            results,
            local_branch_failures,
            operation_partitions,
            target_gap_partitions,
            elapsed_ms=int((time.monotonic() - batch_started) * 1000),
        )
        resolved_currency_requests = {
            str(result["request_id"]): result["_resolved_currency_request"]
            for result in results
            if isinstance(result.get("_resolved_currency_request"), Mapping)
        }
        requests = [resolved_currency_requests.get(str(request.get("request_id")), request) for request in requests]
        _check_call_deadline(deadline_at)
        calculation_results = _build_governed_calculations(
            calculations,
            results,
            observed_on=period_observed_on,
        )
        request_by_id = {
            str(request.get("request_id")): request
            for request in requests
            if isinstance(request, Mapping)
            and isinstance(request.get("request_id"), str)
        }
        result_by_id = {
            str(result.get("request_id")): result
            for result in physical_results
            if isinstance(result, Mapping)
            and isinstance(result.get("request_id"), str)
        }

        def project_result(result: Mapping[str, Any]) -> dict[str, Any]:
            reconciliation = result.get("target_gap_reconciliation")
            overall_result = (
                result_by_id.get(str(reconciliation.get("overall_request_id")))
                if isinstance(reconciliation, Mapping)
                and isinstance(reconciliation.get("overall_request_id"), str)
                else None
            )
            return _model_wire_result(
                result,
                request=request_by_id.get(str(result.get("request_id"))),
                overall_result=overall_result,
            )

        public_results = []
        for result in results:
            _check_call_deadline(deadline_at)
            projected = project_result(result)
            _check_call_deadline(deadline_at)
            public_results.append(projected)
        _check_call_deadline(deadline_at)
        public_calculation_results = _model_wire_calculations(
            calculation_results,
            public_results,
        )
        _check_call_deadline(deadline_at)
        evidence_results = _model_wire_evidence_bundle_results(
            results,
            public_results,
        )
        usable = any(r.get("status") in {"success", "partial"} for r in public_results)
        complete = (
            all(r.get("status") == "success" for r in public_results)
            and all(c.get("status") == "success" for c in public_calculation_results)
        )
        overall = "success" if complete else "partial" if usable else "failed"
        payload = {
            "status": overall,
            "request_count": len(results),
            "answer_scope_line": _answer_scope_line(results),
            "metric_contexts": _model_wire_metric_contexts(results),
            "evidence_bundle": evidence.build_evidence_bundle(
                requests,
                [*evidence_results, *[r for r in physical_results if r.get("request_id") not in request_by_id]],
            ),
            "results": public_results,
        }
        _check_call_deadline(deadline_at)
        _attach_batch_source_evidence(payload, results)
        if preflight_failures and len(preflight_failures) == len(public_requests):
            payload["error"] = _public_error(preflight_failures[request_ids[0]])
        if calculations:
            payload["calculation_count"] = len(public_calculation_results)
            payload["calculations"] = public_calculation_results
        _audit_batch_capabilities(args, requests, results)
    except QueryFailure as failure:
        if failure.code == "BATCH_DEADLINE_EXCEEDED":
            return json.dumps(_deadline_payload(args, results), ensure_ascii=False, separators=(",", ":"))
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {
                "code": failure.code,
                "message": failure.message,
                **_caller_retry_metadata(failure),
            },
        }
    except Exception:
        logger.exception("datasage_query unexpected handler failure")
        failure = QueryFailure("INTERNAL_ERROR", "查询工具暂时不可用。")
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": {
                "code": failure.code,
                "message": failure.message,
                **_caller_retry_metadata(failure),
            },
        }
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if time.monotonic() >= deadline_at:
        return json.dumps(_deadline_payload(args, results), ensure_ascii=False, separators=(",", ":"))
    return rendered


def datasage_entity_resolve(args: dict[str, Any], **kwargs: Any) -> str:
    """Public entity calls share query capacity; internal prefetch keeps its lease."""
    deadline_at = _call_deadline(kwargs.get("_deadline_at"))
    if not _try_acquire_query_slot():
        return json.dumps({
            "status": "failed", "must_stop_business_query": True,
            "error": {"code": "QUERY_CONCURRENCY_LIMIT", "retryable": True,
                      "message": "当前并行业务查询已达到安全上限，请稍后重试。"},
        }, ensure_ascii=False, separators=(",", ":"))
    try:
        result = entities.datasage_entity_resolve(args, **{**kwargs, "deadline_at": deadline_at})
        if time.monotonic() >= deadline_at:
            return json.dumps({
                "status": "failed", "must_stop_business_query": True,
                "error": {"code": "BATCH_DEADLINE_EXCEEDED", "retryable": True,
                          "message": "实体解析已超过调用总时限。"},
            }, ensure_ascii=False, separators=(",", ":"))
        return result
    finally:
        _release_query_slot()


def datasage_query(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute a bounded query call, failing fast when capacity is exhausted."""

    kwargs["_deadline_at"] = _call_deadline(kwargs.get("_deadline_at"))
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
                **_caller_retry_metadata(failure),
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


def entitlement_guarded_datasage_query(
    args: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Public composition root with coarse authorization before business validation."""

    # Keep unauthorized callers outside the governed capability/metric oracle.
    # This check inspects only trusted principal plus raw tool/domain/metric
    # scope; the normalized row/metric authorization is repeated below.
    from . import entitlements

    kwargs["_deadline_at"] = _call_deadline(kwargs.get("_deadline_at"))

    if not entitlements.coarse_authorized("datasage_query", args):
        return entitlements.denied_response()

    observed_on = _business_today()
    branch_failures: dict[str, QueryFailure] = {}
    try:
        validated_envelope = _validate_query_dispatch(
            args,
            observed_on=observed_on,
            branch_failures=branch_failures,
        )
        _check_call_deadline(kwargs["_deadline_at"])
    except QueryFailure as failure:
        return json.dumps(
            {
                "status": "failed",
                "request_count": 0,
                "metric_contexts": [],
                "results": [],
                "error": _public_error(failure),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if not entitlements.authorized(
        "datasage_query",
        args,
        validated_requests=validated_envelope.requests,
    ):
        return entitlements.denied_response()
    return runtime_guarded_datasage_query(
        args,
        _validated_query_envelope=validated_envelope,
        _period_observed_on=observed_on,
        _branch_preflight_failures=branch_failures,
        **kwargs,
    )


def runtime_guarded_datasage_query(
    args: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Official runtime facade: keep the schema visible, then fail closed."""

    started = time.monotonic()
    kwargs["_deadline_at"] = _call_deadline(kwargs.get("_deadline_at"))
    period_observed_on = kwargs.pop("_period_observed_on", None)
    if not isinstance(period_observed_on, date):
        period_observed_on = _business_today()
    try:
        validated_envelope = kwargs.pop("_validated_query_envelope", None)
        if validated_envelope is None:
            # Internal compatibility entrypoint: structural envelope checks
            # remain here.  The model-visible composition root above performs
            # the complete database-free business preflight before entitlement.
            validated_envelope = _validated_query_envelope(args)
        elif not isinstance(
            validated_envelope,
            request_contract.ValidatedQueryEnvelope,
        ):
            raise QueryFailure("INTERNAL_ERROR", "查询工具暂时不可用。")
        requests = list(validated_envelope.requests)
        request_ids = list(validated_envelope.request_ids)
        calculations = [
            dict(calculation) for calculation in validated_envelope.calculations
        ]
        from . import runtime_health

        _check_call_deadline(kwargs["_deadline_at"])
        preflight_failures = kwargs.get("_branch_preflight_failures", {})
        all_branches_invalid = bool(requests) and len(preflight_failures) == len(requests)
        readiness = {"ready": True} if all_branches_invalid else runtime_health.query_readiness_status()
        _check_call_deadline(kwargs["_deadline_at"])
        if readiness.get("ready"):
            return datasage_query(
                args,
                _validated_query_envelope=validated_envelope,
                _period_observed_on=period_observed_on,
                **kwargs,
            )

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
        public_requests = list(requests)
        guarded_requests, local_branch_failures = (
            _allocate_public_request_branches(
                public_requests, preflight_failures=preflight_failures
            )
        )
        guarded_requests, guarded_operations = (
            _expand_complete_change_decompositions(
                guarded_requests,
                reserved_request_ids=[str(request_id) for request_id in request_ids],
            )
        )
        guarded_requests, guarded_target_gap_operations = (
            _expand_complete_target_gap_decompositions(
                guarded_requests,
                reserved_request_ids=[str(request_id) for request_id in request_ids],
            )
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        results = []
        for request in guarded_requests:
            branch_failure = failure
            try:
                _validate_request_plan_without_entities(
                    request,
                    observed_on=period_observed_on,
                )
            except QueryFailure as exc:
                branch_failure = exc
            results.append(
                _failure_result(
                    str(request.get("request_id")),
                    branch_failure,
                    elapsed_ms,
                    business_metric_ref=(
                        _business_metric_ref(request)
                        if isinstance(request, Mapping)
                        else None
                    ),
                    business_sql_attempted_count=0,
                    business_sql_confirmed_count=0,
                )
            )
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
        guarded_requests, results = _order_public_branch_artifacts(
            public_requests,
            guarded_requests,
            results,
            local_branch_failures,
            guarded_operations,
            guarded_target_gap_operations,
            elapsed_ms=elapsed_ms,
        )
        calculation_results = _build_governed_calculations(
            calculations,
            results,
            observed_on=period_observed_on,
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
        public_calculation_results = _model_wire_calculations(
            calculation_results,
            public_results,
        )
        evidence_results = _model_wire_evidence_bundle_results(
            results,
            public_results,
        )
        payload = {
            "status": "failed",
            "request_count": len(results),
            "answer_scope_line": None,
            "metric_contexts": [],
            "evidence_bundle": evidence.build_evidence_bundle(
                guarded_requests,
                evidence_results,
            ),
            "results": public_results,
        }
        if calculations:
            payload["calculation_count"] = len(public_calculation_results)
            payload["calculations"] = public_calculation_results
    except QueryFailure as failure:
        payload = {
            "status": "failed",
            "request_count": 0,
            "metric_contexts": [],
            "results": [],
            "error": _public_error(failure),
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
                **_caller_retry_metadata(failure),
            },
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
