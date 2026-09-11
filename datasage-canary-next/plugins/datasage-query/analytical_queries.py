"""Semantics-driven analytical query builders for DataSage Expert.

The ordinary metric builder handles one fact and one aggregation.  This module
owns analytical shapes requiring coordinated aggregates or different grains:
document-level settlement, formal DSO, paired facts, inventory turnover, and
current registered-pool classifications from one observation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

from . import capability_contract, request_contract, sql_identifiers

_OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_MYSQL_MONTH_FORMAT = "%%Y-%%m"
_PERIOD_KEY = "__period__"


def _business_today() -> date:
    return capability_contract.business_today()


class AnalysisQueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _max_group_dimensions(metric: Mapping[str, Any]) -> int:
    try:
        return capability_contract._metric_group_dimension_limit(metric)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError(
            "CONTRACT_UNAVAILABLE",
            "分析指标缺少有效的分组维度上限。",
        ) from exc


def _ensure_available(definition: Mapping[str, Any]) -> None:
    try:
        capability_contract.ensure_available(definition)
    except capability_contract.AvailabilityContractError as exc:
        raise AnalysisQueryError(exc.code, exc.message) from exc


def _quote_column(value: Any) -> str:
    try:
        return sql_identifiers.quote_identifier(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise AnalysisQueryError(
            "CONTRACT_UNAVAILABLE", "分析指标包含无效字段。"
        ) from exc


def _qualified(alias: str, column: Any) -> str:
    try:
        return sql_identifiers.qualified_identifier(alias, column)
    except sql_identifiers.SqlIdentifierError as exc:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "分析指标包含无效字段。") from exc


def _quote_table(value: Any) -> str:
    try:
        return sql_identifiers.quote_table(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "分析指标包含无效数据集。")


def _dataset(table: Any, datasets_contract: Mapping[str, Any]) -> Mapping[str, Any]:
    datasets = datasets_contract.get("datasets")
    definition = datasets.get(table) if isinstance(datasets, dict) else None
    if not isinstance(definition, dict):
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "分析指标引用了未批准的数据集。")
    return definition


def _approved(column: Any, dataset: Mapping[str, Any]) -> str:
    if not isinstance(column, str) or column not in set(dataset.get("allowed_columns") or []):
        raise AnalysisQueryError("COLUMN_NOT_ALLOWED", "分析指标引用了未批准字段。")
    if column.lower() in {
        str(item).lower() for item in dataset.get("forbidden_columns") or []
    }:
        raise AnalysisQueryError("COLUMN_NOT_ALLOWED", "分析指标引用了敏感字段。")
    return column


def _add_months(value: date, months: int) -> date:
    try:
        return capability_contract._shift_months(value, months)
    except (ValueError, OverflowError) as exc:
        raise AnalysisQueryError("INVALID_PLAN", "分析期间超出支持的日期边界。") from exc


def _time_window(
    request: Mapping[str, Any],
    policy: str,
    observed_on: date | None = None,
) -> tuple[str, str, dict[str, Any]]:
    supplied = request.get("time_range")
    if isinstance(supplied, dict):
        start, end = supplied.get("start"), supplied.get("end")
        try:
            start_date = datetime.strptime(str(start), "%Y-%m-%d").date()
            end_date = datetime.strptime(str(end), "%Y-%m-%d").date()
        except ValueError as exc:
            raise AnalysisQueryError("INVALID_PLAN", "分析指标时间必须使用 YYYY-MM-DD。") from exc
        if start_date >= end_date:
            raise AnalysisQueryError("INVALID_PLAN", "分析指标时间范围无效。")
        return start_date.isoformat(), end_date.isoformat(), {
            "start": start_date.isoformat(), "end": end_date.isoformat(), "source": "explicit"
        }
    current_month = (observed_on or _business_today()).replace(day=1)
    if policy == "current_month":
        start_date, end_date = current_month, _add_months(current_month, 1)
    elif policy == "last_12_completed_months":
        start_date, end_date = _add_months(current_month, -12), current_month
    elif policy == "last_3_completed_months":
        start_date, end_date = _add_months(current_month, -3), current_month
    else:
        raise AnalysisQueryError("INVALID_PLAN", "分析指标缺少明确时间范围。")
    return start_date.isoformat(), end_date.isoformat(), {
        "start": start_date.isoformat(), "end": end_date.isoformat(), "source": policy
    }


def _require_completed_month_window(
    start: str,
    end: str,
    observed_on: date | None,
) -> None:
    """Reject analytical windows that include the current or a future month."""

    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise AnalysisQueryError(
            "INVALID_PLAN", "完整月份分析期间必须使用有效的日期边界。"
        ) from exc
    completed_boundary = (observed_on or _business_today()).replace(day=1)
    if start_date >= end_date or end_date > completed_boundary:
        raise AnalysisQueryError(
            "INVALID_PLAN",
            "该分析指标只接受截至当前业务月之前的完整自然月。",
        )


def _filter_clause(alias: str, column: str, spec: Mapping[str, Any], params: list[Any]) -> str:
    try:
        op, value = capability_contract._fixed_filter_spec(spec)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError(
            "CONTRACT_UNAVAILABLE", "分析指标包含不支持的固定过滤规则。"
        ) from exc
    quoted = _qualified(alias, column)
    if op == "in":
        params.extend(value)
        return f"{quoted} IN ({', '.join(['%s'] * len(value))})"
    params.append(value)
    return f"{quoted} {_OPS[op]} %s"


def _value_filter(alias: str, column: str, value: Any, params: list[Any]) -> str:
    quoted = _qualified(alias, column)
    if isinstance(value, list):
        if not 1 <= len(value) <= request_contract.MAX_FILTER_VALUES:
            raise AnalysisQueryError("INVALID_PLAN", "分析过滤值数量无效。")
        params.extend(value)
        return f"{quoted} IN ({', '.join(['%s'] * len(value))})"
    if isinstance(value, (str, int, float, bool)):
        params.append(value)
        return f"{quoted} = %s"
    raise AnalysisQueryError("INVALID_PLAN", "分析过滤值无效。")


def _entity_bindings(request: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        return capability_contract._entity_bindings(request)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError(
            "CONTRACT_UNAVAILABLE", "实体绑定结构无效。"
        ) from exc


def _bound_value(
    bindings: Mapping[str, Any],
    code: str,
    fallback: Any,
) -> tuple[Mapping[str, Any] | None, Any]:
    try:
        return capability_contract._bound_entity_value(bindings, code, fallback)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", exc.message) from exc


def _dimension_filter(
    definition: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
) -> str:
    try:
        return capability_contract._entity_filter_column(definition, binding)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", exc.message) from exc


def _bound_mapping_column(binding: Mapping[str, Any], column: Any) -> str:
    allowed = binding.get("identity_columns")
    if (
        not isinstance(column, str)
        or not isinstance(allowed, list)
        or column not in allowed
    ):
        raise AnalysisQueryError(
            "CONTRACT_UNAVAILABLE",
            "分析映射字段与实体稳定身份定义不一致。",
        )
    return column


def _dimension_columns(definition: Mapping[str, Any]) -> list[tuple[str, str]]:
    try:
        return capability_contract._dimension_columns(definition)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", exc.message) from exc


def _order_clause(
    request: Mapping[str, Any],
    dimension_outputs: list[str],
    *,
    allowed_value_fields: set[str],
    default_field: str = "metric_value",
    default_direction: str = "DESC",
) -> str:
    order_by = request.get("order_by")
    if not dimension_outputs:
        if order_by is not None:
            raise AnalysisQueryError("INVALID_PLAN", "无分组分析指标不接受 order_by。")
        return ""
    if order_by is None:
        return f" ORDER BY {_quote_column(default_field)} {default_direction}"
    if not isinstance(order_by, dict):
        raise AnalysisQueryError("INVALID_PLAN", "分析指标排序定义无效。")
    field = order_by.get("field")
    direction = str(order_by.get("direction") or "").upper()
    allowed = {*allowed_value_fields, *dimension_outputs}
    if field not in allowed or direction not in {"ASC", "DESC"}:
        raise AnalysisQueryError("INVALID_PLAN", "分析指标排序字段或方向不受支持。")
    return f" ORDER BY {_quote_column(str(field))} {direction}"


def _inventory_turnover_period(
    request: Mapping[str, Any],
    default_months: int,
    *,
    valid_measure: str = "cost_amount_rmb",
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    supplied = request.get("time_range")
    if supplied is None:
        observed_month = (observed_on or _business_today()).replace(day=1)
        cutoff_month = observed_month.strftime("%Y-%m")
        inventory_present = f"({_quote_column(valid_measure)} IS NOT NULL AND {_quote_column(valid_measure)} <> 0)"
        bounds_sql = (
            "latest_available AS ("
            f"SELECT MAX(CASE WHEN ({inventory_present}) "
            f"AND {_quote_column('bill_date')} < %s "
            "THEN `bill_date` END) AS `operating_end_month` FROM {table}), "
            "bounds AS (SELECT "
            f"DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(`operating_end_month`, '-01'), '%%Y-%%m-%%d'), "
            f"INTERVAL {default_months - 1} MONTH), '{_MYSQL_MONTH_FORMAT}') AS `operating_start_month`, "
            "`operating_end_month`, "
            f"DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(`operating_end_month`, '-01'), '%%Y-%%m-%%d'), "
            f"INTERVAL {default_months} MONTH), '{_MYSQL_MONTH_FORMAT}') AS `opening_month` "
            "FROM latest_available)"
        )
        return bounds_sql, [cutoff_month], {
            "source": "latest_available_accounting_months",
            "months": default_months,
        }
    if not isinstance(supplied, dict):
        raise AnalysisQueryError("INVALID_PLAN", "库存周转期间格式无效。")
    start, end = supplied.get("start"), supplied.get("end")
    try:
        start_date = datetime.strptime(str(start), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(end), "%Y-%m-%d").date()
    except ValueError as exc:
        raise AnalysisQueryError("INVALID_PLAN", "库存周转期间必须使用自然月首日边界。") from exc
    if start_date.day != 1 or end_date.day != 1 or start_date >= end_date:
        raise AnalysisQueryError("INVALID_PLAN", "库存周转期间必须使用有效的自然月首日边界。")
    _require_completed_month_window(
        start_date.isoformat(), end_date.isoformat(), observed_on
    )
    operating_end = _add_months(end_date, -1)
    opening = _add_months(start_date, -1)
    bounds_sql = (
        "bounds AS (SELECT %s AS `operating_start_month`, %s AS `operating_end_month`, "
        "%s AS `opening_month`)"
    )
    return bounds_sql, [
        start_date.strftime("%Y-%m"),
        operating_end.strftime("%Y-%m"),
        opening.strftime("%Y-%m"),
    ], {"start": start_date.isoformat(), "end": end_date.isoformat(), "source": "explicit"}


def _effective_dimensions(semantics, metric):
    try:
        return capability_contract.effective_dimension_definitions(semantics, metric)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisQueryError(exc.code, exc.message) from exc


def _inventory_turnover_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    table = metric.get("table")
    dataset = _dataset(table, datasets_contract)
    dimensions = _effective_dimensions(semantics, metric)
    if not isinstance(dimensions, dict):
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "库存维度语义无效。")
    requested_dimensions = request.get("dimensions") or []
    requested_filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    if (
        not isinstance(requested_dimensions, list)
        or len(requested_dimensions) > _max_group_dimensions(metric)
        or len(requested_dimensions) != len(set(requested_dimensions))
        or any(not isinstance(item, str) for item in requested_dimensions)
        or not isinstance(requested_filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "库存周转最多支持两个分组维度。")
    allowed_dimensions = set(metric.get("allowed_dimensions") or [])
    dimension_codes: list[str] = []
    for code in [*requested_dimensions, *requested_filters.keys()]:
        definition = dimensions.get(code)
        source = definition.get("source") if isinstance(definition, dict) else None
        if (
            code not in allowed_dimensions
            or not isinstance(definition, dict)
            or (isinstance(source, dict) and source.get("type", "fact") != "fact")
        ):
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "库存周转不支持请求中的某个维度。")
        if code not in dimension_codes:
            dimension_codes.append(code)

    output_columns: list[tuple[str, str]] = []
    output_names: list[str] = []
    for code in requested_dimensions:
        for column, output in _dimension_columns(dimensions[code]):
            _approved(column, dataset)
            if output in output_names:
                raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "库存周转维度输出名称重复。")
            output_columns.append((column, output))
            output_names.append(output)

    params: list[Any] = []
    where: list[str] = []
    system_filters: list[dict[str, Any]] = []
    for spec in dataset.get("required_filters") or []:
        if not isinstance(spec, dict):
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "库存周转数据集固定过滤无效。")
        column = _approved(spec.get("column"), dataset)
        where.append(_filter_clause("s", column, spec, params))
        system_filters.append({
            "dataset": str(table), "column": column, "op": str(spec.get("op", "eq")),
            "value": spec.get("value"), "source": "dataset_required",
        })
    for column, spec in (metric.get("required_filters") or {}).items():
        if not isinstance(spec, dict):
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "库存周转指标固定过滤无效。")
        column = _approved(column, dataset)
        where.append(_filter_clause("s", column, spec, params))
        system_filters.append({
            "dataset": str(table), "column": column, "op": str(spec.get("op", "eq")),
            "value": spec.get("value"), "source": "metric_required",
        })
    applied_filters: dict[str, Any] = {}
    for code, value in requested_filters.items():
        definition = dimensions[code]
        binding, bound_value = _bound_value(bindings, code, value)
        column = _approved(_dimension_filter(definition, binding), dataset)
        where.append(_value_filter("s", column, bound_value, params))
        applied_filters[code] = bound_value

    default_months = metric.get("default_complete_months", 12)
    if not isinstance(default_months, int) or not 1 <= default_months <= 120:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "库存周转默认月份定义无效。")
    cost_measure = _approved(metric.get("cost_measure"), dataset)
    ddp_measure = _approved(metric.get("ddp_measure"), dataset)
    net_measure = _approved(metric.get("net_delivery_measure"), dataset)
    period_measure = _approved(metric.get("time_field"), dataset)
    bounds_sql, bounds_params, applied_time = _inventory_turnover_period(
        request,
        default_months,
        valid_measure=cost_measure,
        observed_on=observed_on,
    )
    quoted_table = _quote_table(table)
    bounds_sql = bounds_sql.format(table=quoted_table)

    monthly_dimension_select = [
        f"{_qualified('s', column)} AS {_quote_column(output)}" for column, output in output_columns
    ]
    monthly_dimension_group = [_qualified("s", column) for column, _ in output_columns]
    entity_dimension_select = [_quote_column(output) for output in output_names]
    entity_group = ", ".join(entity_dimension_select)
    dimension_prefix = (", ".join(monthly_dimension_select) + ", ") if monthly_dimension_select else ""
    monthly_group = ", " + ", ".join(monthly_dimension_group) if monthly_dimension_group else ""
    where_sql = " AND " + " AND ".join(where) if where else ""
    entity_prefix = (", ".join(entity_dimension_select) + ", ") if entity_dimension_select else ""
    matrix_dimension_prefix = (
        ", ".join(f"eb.{_quote_column(name)}" for name in output_names) + ", "
        if output_names else ""
    )
    entity_group_sql = f" GROUP BY {entity_group}" if entity_group else ""
    dimension_join = " AND ".join(
        f"md.{_quote_column(name)} <=> eb.{_quote_column(name)}" for name in output_names
    )
    dimension_join = (" AND " + dimension_join) if dimension_join else ""
    summary_group = f" GROUP BY {', '.join('m.' + _quote_column(name) for name in output_names)}" if output_names else ""
    summary_dimensions = ", ".join(f"m.{_quote_column(name)}" for name in output_names)
    summary_prefix = (summary_dimensions + ", ") if summary_dimensions else ""
    final_dimensions = ", ".join(_quote_column(name) for name in output_names)
    final_prefix = (final_dimensions + ", ") if final_dimensions else ""

    day_format = "%%Y-%%m-%%d"
    sql = f"""WITH RECURSIVE {bounds_sql},
expected_months AS (
  SELECT `opening_month` AS `snapshot_month` FROM bounds
  UNION ALL
  SELECT DATE_FORMAT(DATE_ADD(STR_TO_DATE(CONCAT(`snapshot_month`, '-01'), '{day_format}'), INTERVAL 1 MONTH), '{_MYSQL_MONTH_FORMAT}')
  FROM expected_months CROSS JOIN bounds
  WHERE `snapshot_month` < `operating_end_month`
),
accounted_months AS (
  SELECT DISTINCT s.{_quote_column(period_measure)} AS `snapshot_month`
  FROM {quoted_table} AS s CROSS JOIN bounds AS b
  WHERE s.{_quote_column(period_measure)} BETWEEN b.`opening_month` AND b.`operating_end_month`
    AND s.{_quote_column(cost_measure)} IS NOT NULL
    AND s.{_quote_column(cost_measure)} <> 0
),
monthly_data AS (
  SELECT {dimension_prefix}s.{_quote_column(period_measure)} AS `snapshot_month`,
         SUM(s.{_quote_column(cost_measure)}) AS `inventory_cost_rmb`,
         SUM(s.{_quote_column(ddp_measure)}) AS `inventory_ddp_rmb`,
         SUM(s.{_quote_column(net_measure)}) AS `net_delivery_rmb`,
         SUM(CASE WHEN s.{_quote_column(cost_measure)} IS NULL THEN 1 ELSE 0 END) AS `cost_nulls`,
         SUM(CASE WHEN s.{_quote_column(ddp_measure)} IS NULL THEN 1 ELSE 0 END) AS `ddp_nulls`,
         SUM(CASE WHEN s.{_quote_column(net_measure)} IS NULL THEN 1 ELSE 0 END) AS `net_nulls`,
         COUNT(*) AS `source_rows`
  FROM {quoted_table} AS s CROSS JOIN bounds AS b
  WHERE s.{_quote_column(period_measure)} BETWEEN b.`opening_month` AND b.`operating_end_month`{where_sql}
  GROUP BY s.{_quote_column(period_measure)}{monthly_group}
),
entities AS (
  SELECT {entity_prefix}MIN(`snapshot_month`) AS `first_observed_month`
  FROM monthly_data{entity_group_sql}
  HAVING COUNT(*) > 0
),
entity_bounds AS (
  SELECT {entity_prefix}b.`operating_start_month` AS `effective_start_month`,
         b.`opening_month` AS `effective_opening_month`, b.`operating_end_month`
  FROM entities AS e CROSS JOIN bounds AS b
),
matrix AS (
  SELECT {matrix_dimension_prefix}eb.`effective_start_month`, eb.`effective_opening_month`, eb.`operating_end_month`,
         em.`snapshot_month`, md.`inventory_cost_rmb`, md.`inventory_ddp_rmb`, md.`net_delivery_rmb`,
         COALESCE(md.`source_rows`, 0) AS `source_rows`,
         COALESCE(md.`cost_nulls`, 0) AS `cost_nulls`,
         COALESCE(md.`ddp_nulls`, 0) AS `ddp_nulls`,
         COALESCE(md.`net_nulls`, 0) AS `net_nulls`,
         CASE WHEN md.`source_rows` IS NULL THEN 0 ELSE 1 END AS `entity_snapshot_present`,
         CASE WHEN am.`snapshot_month` IS NULL THEN 0 ELSE 1 END AS `accounting_ready`
  FROM entity_bounds AS eb
  JOIN expected_months AS em ON em.`snapshot_month` BETWEEN eb.`effective_opening_month` AND eb.`operating_end_month`
  LEFT JOIN monthly_data AS md ON md.`snapshot_month` = em.`snapshot_month`{dimension_join}
  LEFT JOIN accounted_months AS am ON am.`snapshot_month` = em.`snapshot_month`
),
summary_raw AS (
  SELECT {summary_prefix}MIN(m.`effective_start_month`) AS `effective_start_month`,
         MIN(m.`effective_opening_month`) AS `effective_opening_month`,
         MIN(m.`operating_end_month`) AS `operating_end_month`,
         TIMESTAMPDIFF(MONTH,
           STR_TO_DATE(CONCAT(MIN(m.`effective_start_month`), '-01'), '{day_format}'),
           STR_TO_DATE(CONCAT(MIN(m.`operating_end_month`), '-01'), '{day_format}')) + 1 AS `effective_operating_months`,
         DATEDIFF(
           DATE_ADD(STR_TO_DATE(CONCAT(MIN(m.`operating_end_month`), '-01'), '{day_format}'), INTERVAL 1 MONTH),
           STR_TO_DATE(CONCAT(MIN(m.`effective_start_month`), '-01'), '{day_format}')) AS `period_natural_days`,
         COUNT(*) AS `expected_snapshot_count`,
         SUM(m.`entity_snapshot_present`) AS `actual_snapshot_count`,
         SUM(CASE WHEN m.`accounting_ready` = 0 THEN 1 ELSE 0 END) AS `unready_accounting_month_count`,
         GROUP_CONCAT(CASE WHEN m.`accounting_ready` = 0 THEN m.`snapshot_month` END) AS `unready_accounting_months`,
         SUM(m.`source_rows`) AS `entity_source_hit_count`,
         SUM(m.`cost_nulls`) AS `cost_missing_value_count`,
         SUM(m.`ddp_nulls`) AS `ddp_missing_value_count`,
         SUM(CASE WHEN m.`snapshot_month` >= m.`effective_start_month` THEN m.`net_nulls` ELSE 0 END) AS `net_delivery_missing_value_count`,
         SUM(CASE WHEN m.`snapshot_month` >= m.`effective_start_month` AND m.`entity_snapshot_present` = 0 THEN 1 ELSE 0 END) AS `missing_flow_months`,
         SUM(CASE WHEN m.`snapshot_month` IN (m.`effective_opening_month`, m.`operating_end_month`)
             THEN m.`inventory_cost_rmb` / 2 ELSE m.`inventory_cost_rmb` END) AS `weighted_cost`,
         SUM(CASE WHEN m.`snapshot_month` IN (m.`effective_opening_month`, m.`operating_end_month`)
             THEN m.`inventory_ddp_rmb` / 2 ELSE m.`inventory_ddp_rmb` END) AS `weighted_ddp`,
         SUM(CASE WHEN m.`snapshot_month` >= m.`effective_start_month` THEN m.`net_delivery_rmb` ELSE 0 END) AS `recorded_net_delivery`
  FROM matrix AS m{summary_group}
),
summary AS (
  SELECT *,
         CASE WHEN `unready_accounting_month_count` > 0 OR `actual_snapshot_count` < `expected_snapshot_count` OR `cost_missing_value_count` > 0
              THEN NULL ELSE `weighted_cost` / `effective_operating_months` END AS `avg_inventory_cost_rmb`,
         CASE WHEN `unready_accounting_month_count` > 0 OR `actual_snapshot_count` < `expected_snapshot_count` OR `ddp_missing_value_count` > 0
              THEN NULL ELSE `weighted_ddp` / `effective_operating_months` END AS `avg_inventory_ddp_rmb`,
         CASE WHEN `missing_flow_months` > 0 OR `net_delivery_missing_value_count` > 0
              THEN NULL ELSE `recorded_net_delivery` END AS `net_delivery_rmb`
  FROM summary_raw
),
turnover_values AS (
  SELECT *,
    CASE WHEN `net_delivery_rmb` IS NULL OR `net_delivery_rmb` = 0 OR `avg_inventory_cost_rmb` IS NULL
      THEN NULL ELSE `avg_inventory_cost_rmb` * `period_natural_days` / `net_delivery_rmb` END AS `cost_turnover_days`,
    CASE WHEN `net_delivery_rmb` IS NULL OR `net_delivery_rmb` = 0 OR `avg_inventory_ddp_rmb` IS NULL
      THEN NULL ELSE `avg_inventory_ddp_rmb` * `period_natural_days` / `net_delivery_rmb` END AS `ddp_turnover_days`
  FROM summary
)
SELECT {final_prefix}`cost_turnover_days` AS `metric_value`, `cost_turnover_days`, `ddp_turnover_days`,
       `avg_inventory_cost_rmb`, `avg_inventory_ddp_rmb`, `net_delivery_rmb`, `period_natural_days`,
       `effective_operating_months`, `expected_snapshot_count`, `actual_snapshot_count`,
       `unready_accounting_month_count`, `unready_accounting_months`,
       `cost_missing_value_count`, `ddp_missing_value_count`, `net_delivery_missing_value_count`,
       `entity_source_hit_count`, `effective_opening_month`, `effective_start_month`, `operating_end_month`,
       CASE WHEN `unready_accounting_month_count` > 0 THEN 'inventory_cost_accounting_not_ready'
            WHEN `actual_snapshot_count` < `expected_snapshot_count` THEN 'missing_entity_snapshot'
            WHEN `cost_missing_value_count` > 0 THEN 'missing_cost_value'
            WHEN `net_delivery_rmb` IS NULL THEN 'missing_net_delivery_value'
            WHEN `net_delivery_rmb` = 0 THEN 'zero_net_delivery'
            ELSE 'available' END AS `cost_turnover_state`,
       CASE WHEN `unready_accounting_month_count` > 0 THEN 'inventory_cost_accounting_not_ready'
            WHEN `actual_snapshot_count` < `expected_snapshot_count` THEN 'missing_entity_snapshot'
            WHEN `ddp_missing_value_count` > 0 THEN 'missing_ddp_value'
            WHEN `net_delivery_rmb` IS NULL THEN 'missing_net_delivery_value'
            WHEN `net_delivery_rmb` = 0 THEN 'zero_net_delivery'
            ELSE 'available' END AS `ddp_turnover_state`,
       CASE WHEN `cost_turnover_days` IS NULL THEN 'missing' ELSE 'complete' END AS `metric_data_state`,
       `entity_source_hit_count` AS `__matched_row_count`
FROM turnover_values"""

    order_by = request.get("order_by")
    value_fields = {
        "metric_value", "cost_turnover_days", "ddp_turnover_days", "avg_inventory_cost_rmb",
        "avg_inventory_ddp_rmb", "net_delivery_rmb", "period_natural_days",
    }
    if not output_names:
        if order_by is not None:
            raise AnalysisQueryError("INVALID_PLAN", "无分组库存周转不接受 order_by。")
    else:
        field, direction = "cost_turnover_days", "DESC"
        if order_by is not None:
            if not isinstance(order_by, dict):
                raise AnalysisQueryError("INVALID_PLAN", "库存周转排序定义无效。")
            field = str(order_by.get("field") or "")
            direction = str(order_by.get("direction") or "").upper()
            if field not in {*value_fields, *output_names} or direction not in {"ASC", "DESC"}:
                raise AnalysisQueryError("INVALID_PLAN", "库存周转排序字段或方向不受支持。")
        sql += f" ORDER BY ({_quote_column(field)} IS NULL) ASC, {_quote_column(field)} {direction}"
    sql += " LIMIT %s"
    params = [*bounds_params, *params, limit + 1]
    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, params, {
        "metric": request.get("metric"),
        "dataset": str(table),
        "source_datasets": [str(table)],
        "time_range": applied_time,
        "filters": applied_filters,
        "system_filters": system_filters,
        "warnings": warnings,
        "dimension_outputs": output_names,
    }


def _settlement_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    table = metric.get("table")
    dataset = _dataset(table, datasets_contract)
    document_key = _approved(metric.get("document_key"), dataset)
    bill_time = _approved(metric.get("bill_time_field"), dataset)
    completion_time = _approved(metric.get("completion_time_field"), dataset)
    open_amount = _approved(metric.get("open_amount_field"), dataset)
    dimensions = _effective_dimensions(semantics, metric)
    allowed_dimensions = set(metric.get("allowed_dimensions") or [])
    selected = request.get("dimensions") or []
    filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    if (
        not isinstance(selected, list)
        or len(selected) > _max_group_dimensions(metric)
        or not isinstance(filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "结算行为维度或过滤条件无效。")
    codes: list[str] = []
    for code in [*selected, *filters.keys()]:
        if code not in allowed_dimensions or not isinstance(dimensions.get(code), dict):
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "结算行为不支持请求中的维度。")
        if code not in codes:
            codes.append(code)

    params: list[Any] = []
    where: list[str] = []
    for column, spec in (metric.get("required_filters") or {}).items():
        where.append(_filter_clause("f", _approved(column, dataset), spec, params))
    where.append(f"{_qualified('f', completion_time)} IS NOT NULL")
    start, end, applied_time = _time_window(
        request, str(metric.get("time_policy") or ""), observed_on
    )
    where.extend([f"{_qualified('f', completion_time)} >= %s", f"{_qualified('f', completion_time)} < %s"])
    params.extend([start, end])

    selected_columns: list[tuple[str, str]] = []
    for code in codes:
        definition = dimensions[code]
        for column, output in _dimension_columns(definition):
            _approved(column, dataset)
            if code in selected:
                selected_columns.append((column, output))
        if code in filters:
            binding, bound_value = _bound_value(bindings, code, filters[code])
            filter_column = _approved(_dimension_filter(definition, binding), dataset)
            where.append(_value_filter("f", filter_column, bound_value, params))

    inner_dimension_select = [
        f"{_qualified('f', column)} AS {_quote_column(output)}" for column, output in selected_columns
    ]
    inner_group = [_qualified("f", document_key), *[_qualified("f", column) for column, _ in selected_columns]]
    inner_select = ", ".join([
        f"{_qualified('f', document_key)} AS {_quote_column('document_id')}",
        *inner_dimension_select,
        f"DATEDIFF(MIN({_qualified('f', completion_time)}), MIN({_qualified('f', bill_time)})) AS {_quote_column('settlement_days')}",
        f"MAX(CASE WHEN {_qualified('f', open_amount)} > 0 THEN 1 ELSE 0 END) AS {_quote_column('has_open_balance')}",
        f"MAX(CASE WHEN {_qualified('f', open_amount)} IS NULL THEN 1 ELSE 0 END) AS {_quote_column('missing_balance_flag')}",
        f"MAX(CASE WHEN {_qualified('f', bill_time)} IS NULL THEN 1 ELSE 0 END) "
        f"AS {_quote_column('missing_bill_time_flag')}",
    ])
    per_bill = (
        f"SELECT {inner_select} FROM {_quote_table(table)} AS {_quote_column('f')}"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY " + ", ".join(inner_group)
    )
    outer_dimensions = [_quote_column(output) for _, output in selected_columns]
    quality_columns = (
        "SUM(settlement_days < 0) AS excluded_negative_bill_count, "
        "SUM(has_open_balance = 1) AS excluded_open_balance_bill_count, "
        "SUM(CASE WHEN (missing_bill_time_flag = 1 OR missing_balance_flag = 1) AND has_open_balance = 0 "
        "THEN 1 ELSE 0 END) AS missing_eligibility_count"
    )
    statistic = metric.get("statistic")
    if statistic == "average":
        value_sql = "AVG(e.settlement_days)"
    elif statistic == "maximum":
        value_sql = "MAX(e.settlement_days)"
    elif statistic == "distribution":
        value_sql = "COUNT(e.document_id)"
    else:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "结算行为统计方式无效。")

    eligible = (
        "SELECT * FROM per_bill WHERE settlement_days >= 0 "
        "AND has_open_balance = 0 AND missing_bill_time_flag = 0 AND missing_balance_flag = 0"
    )
    if outer_dimensions:
        quality = (
            f"SELECT {', '.join(outer_dimensions)}, {quality_columns} FROM per_bill "
            f"GROUP BY {', '.join(outer_dimensions)}"
        )
        quality_join = " AND ".join(f"e.{column} <=> q.{column}" for column in outer_dimensions)
        quality_from = f"quality AS q LEFT JOIN eligible AS e ON {quality_join}"
    else:
        quality = f"SELECT {quality_columns} FROM per_bill"
        quality_from = "quality AS q LEFT JOIN eligible AS e ON TRUE"
    select_parts = [*[f"q.{column}" for column in outer_dimensions]]
    group_parts = [f"q.{column}" for column in outer_dimensions]
    if statistic == "distribution":
        band = (
            "CASE WHEN e.document_id IS NULL THEN NULL "
            "WHEN e.settlement_days <= 30 THEN '0-30天' "
            "WHEN e.settlement_days <= 60 THEN '31-60天' "
            "WHEN e.settlement_days <= 90 THEN '61-90天' ELSE '90天以上' END"
        )
        select_parts.append(f"{band} AS {_quote_column('settlement_band')}")
        group_parts.append(band)
    missing_dates = f"COALESCE(q.{_quote_column('missing_eligibility_count')}, 0)"
    known_values = "COUNT(e.document_id)"
    matched_rows = (
        f"({known_values} + COALESCE(q.{_quote_column('excluded_negative_bill_count')}, 0) + "
        f"COALESCE(q.{_quote_column('excluded_open_balance_bill_count')}, 0) + {missing_dates})"
    )
    select_parts.extend([
        f"CASE WHEN {missing_dates} > 0 OR {known_values} = 0 THEN NULL ELSE {value_sql} END AS {_quote_column('metric_value')}",
        f"CASE WHEN {known_values} > 0 THEN {value_sql} ELSE NULL END AS {_quote_column('known_subset_value')}",
        f"COUNT(e.document_id) AS {_quote_column('sample_bill_count')}",
        f"COALESCE(q.{_quote_column('excluded_negative_bill_count')}, 0) "
        f"AS {_quote_column('excluded_negative_bill_count')}",
        f"COALESCE(q.{_quote_column('excluded_open_balance_bill_count')}, 0) "
        f"AS {_quote_column('excluded_open_balance_bill_count')}",
        f"{missing_dates} AS {_quote_column('missing_value_count')}",
        f"{known_values} AS {_quote_column('known_value_count')}",
        f"CASE WHEN ({known_values}) + ({missing_dates}) > 0 THEN "
        f"1.0 * ({known_values}) / (({known_values}) + ({missing_dates})) ELSE NULL END "
        f"AS {_quote_column('value_coverage_rate')}",
        f"CASE WHEN {matched_rows} = 0 THEN 'missing' "
        f"WHEN {missing_dates} > 0 THEN CASE WHEN {known_values} = 0 THEN 'missing' ELSE 'incomplete' END "
        f"WHEN {known_values} = 0 THEN 'missing' ELSE 'complete' END "
        f"AS {_quote_column('metric_data_state')}",
        f"{matched_rows} AS {_quote_column('__matched_row_count')}",
    ])
    sql = (
        f"WITH per_bill AS ({per_bill}), eligible AS ({eligible}), quality AS ({quality}) "
        f"SELECT {', '.join(select_parts)} FROM {quality_from}"
    )
    if group_parts:
        sql += " GROUP BY " + ", ".join([
            *group_parts,
            "q.excluded_negative_bill_count",
            "q.excluded_open_balance_bill_count",
            "q.missing_eligibility_count",
        ])
    dimension_outputs = [output for _, output in selected_columns]
    if statistic == "distribution" and request.get("order_by") is None:
        sql += " ORDER BY " + (", ".join(group_parts[:-1]) + ", " if len(group_parts) > 1 else "") + (
            "FIELD(settlement_band, '0-30天', '31-60天', '61-90天', '90天以上')"
        )
    else:
        sortable_outputs = [*dimension_outputs]
        if statistic == "distribution":
            sortable_outputs.append("settlement_band")
        sql += _order_clause(
            request,
            sortable_outputs,
            allowed_value_fields={"metric_value", "sample_bill_count"},
        )
    sql += " LIMIT %s"
    params.append(limit + 1)
    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, params, {
        "metric": request.get("metric"),
        "dataset": table,
        "source_datasets": [table],
        "time_range": applied_time,
        "filters": filters,
        "warnings": warnings,
        "dimension_outputs": dimension_outputs,
    }


def _mapping_filter_clauses(
    alias: str,
    request_filters: Mapping[str, Any],
    mappings: Mapping[str, Any],
    dataset: Mapping[str, Any],
    params: list[Any],
    bindings: Mapping[str, Any],
    fields: tuple[str, str],
) -> list[str]:
    bound_field, unbound_field = fields
    where: list[str] = []
    for code, value in request_filters.items():
        mapping = mappings.get(code)
        binding, bound_value = _bound_value(bindings, code, value)
        column = (
            mapping.get(bound_field)
            if binding is not None and isinstance(mapping, dict)
            else mapping.get(unbound_field) if isinstance(mapping, dict) else None
        )
        if binding is not None:
            column = _bound_mapping_column(binding, column)
        where.append(_value_filter(alias, _approved(column, dataset), bound_value, params))
    return where


def _mapping_filters(
    alias: str,
    side: str,
    request_filters: Mapping[str, Any],
    mappings: Mapping[str, Any],
    dataset: Mapping[str, Any],
    params: list[Any],
    bindings: Mapping[str, Any],
) -> list[str]:
    return _mapping_filter_clauses(
        alias,
        request_filters,
        mappings,
        dataset,
        params,
        bindings,
        (f"{side}_key", f"{side}_filter"),
    )


def _mapping_parts_for_fields(
    selected: list[str],
    mappings: Mapping[str, Any],
    dataset: Mapping[str, Any],
    fields: tuple[str, str],
    messages: tuple[str, str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    key_field, outputs_field = fields
    unsupported_message, output_error_message = messages
    keys: list[tuple[str, str]] = []
    outputs: list[tuple[str, str]] = []
    for index, code in enumerate(selected):
        mapping = mappings.get(code)
        if not isinstance(mapping, dict):
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", unsupported_message)
        key = _approved(mapping.get(key_field), dataset)
        keys.append((key, f"key_{index + 1}"))
        for item in mapping.get(outputs_field) or []:
            if not isinstance(item, dict):
                raise AnalysisQueryError("CONTRACT_UNAVAILABLE", output_error_message)
            column = _approved(item.get("column"), dataset)
            outputs.append((column, str(item.get("alias") or column)))
    return keys, outputs


def _mapping_parts(
    selected: list[str], mappings: Mapping[str, Any], side: str, dataset: Mapping[str, Any]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    return _mapping_parts_for_fields(
        selected,
        mappings,
        dataset,
        (f"{side}_key", f"{side}_outputs"),
        ("分析指标不支持请求的维度。", "分析维度输出定义无效。"),
    )


def _fixed_filters(alias: str, specs: Mapping[str, Any], dataset: Mapping[str, Any], params: list[Any]) -> list[str]:
    return [_filter_clause(alias, _approved(column, dataset), spec, params) for column, spec in specs.items()]


def _component_mapping_parts(
    selected: list[str], mappings: Mapping[str, Any], dataset: Mapping[str, Any]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Resolve the governed grain for one independently aggregated component."""
    return _mapping_parts_for_fields(
        selected,
        mappings,
        dataset,
        ("key", "outputs"),
        ("分析组件不支持请求中的维度。", "分析组件维度输出定义无效。"),
    )


def _component_mapping_filters(
    alias: str,
    request_filters: Mapping[str, Any],
    mappings: Mapping[str, Any],
    dataset: Mapping[str, Any],
    params: list[Any],
    bindings: Mapping[str, Any],
) -> list[str]:
    return _mapping_filter_clauses(
        alias,
        request_filters,
        mappings,
        dataset,
        params,
        bindings,
        ("key", "filter"),
    )


def _aggregate_components(
    source: Mapping[str, Any],
    selected: list[str],
    request_filters: Mapping[str, Any],
    start: str,
    end: str,
    datasets_contract: Mapping[str, Any],
    value_alias: str,
    time_bucket: str | None = None,
    bindings: Mapping[str, Any] | None = None,
    include_null_count: bool = False,
) -> tuple[str, list[Any], list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Aggregate several signed facts at an identical governed grain.

    Each fact is grouped independently before UNION ALL.  This is the key
    protection against multiplying rows when net delivery or net receipt spans
    separate gross and reversal facts.
    """

    components = source.get("components")
    if not isinstance(components, list) or not components:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "组合分析指标缺少数据组件。")

    component_sql: list[str] = []
    params: list[Any] = []
    expected_keys: list[tuple[str, str]] | None = None
    expected_outputs: list[tuple[str, str]] | None = None
    source_tables: list[str] = []
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "组合分析指标组件定义无效。")
        table = component.get("table")
        dataset = _dataset(table, datasets_contract)
        measure = _approved(component.get("measure"), dataset)
        time_field = _approved(component.get("time_field"), dataset)
        sign = component.get("sign", 1)
        if sign not in {-1, 1}:
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "组合分析指标符号只能是 1 或 -1。")
        mappings = component.get("dimension_mappings") or {}
        if not isinstance(mappings, dict):
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "组合分析指标维度映射无效。")
        keys, outputs = _component_mapping_parts(selected, mappings, dataset)
        if time_bucket == "month":
            keys = [(_PERIOD_KEY, "period"), *keys]
        elif time_bucket is not None:
            raise AnalysisQueryError("INVALID_PLAN", "组合分析指标只支持按月趋势。")
        key_aliases = [alias for _, alias in keys]
        output_aliases = [alias for _, alias in outputs]
        if expected_keys is None:
            expected_keys, expected_outputs = keys, outputs
        elif (
            key_aliases != [alias for _, alias in expected_keys]
            or output_aliases != [alias for _, alias in expected_outputs or []]
        ):
            raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "组合分析指标各组件的对齐粒度不一致。")

        alias = f"c{index + 1}"
        component_params: list[Any] = []
        where = _fixed_filters(
            alias, component.get("required_filters") or {}, dataset, component_params
        )
        where += _component_mapping_filters(
            alias,
            request_filters,
            mappings,
            dataset,
            component_params,
            bindings or {},
        )
        where += [
            f"{_qualified(alias, time_field)} >= %s",
            f"{_qualified(alias, time_field)} < %s",
        ]
        component_params += [start, end]
        measure_sql = f"COALESCE(SUM({_qualified(alias, measure)}), 0)"
        if sign == -1:
            measure_sql = f"-({measure_sql})"
        key_expressions = [
            (
                f"DATE_FORMAT({_qualified(alias, time_field)}, '{_MYSQL_MONTH_FORMAT}')"
                if column == _PERIOD_KEY
                else _qualified(alias, column)
            )
            for column, _ in keys
        ]
        select = [
            *[
                f"{expression} AS {_quote_column(output)}"
                for expression, (_, output) in zip(key_expressions, keys)
            ],
            *[
                f"MAX({_qualified(alias, column)}) AS {_quote_column(output)}"
                for column, output in outputs
            ],
            f"{measure_sql} AS {_quote_column('_component_value')}",
            f"COUNT(*) AS {_quote_column('_component_count')}",
        ]
        if include_null_count:
            select.append(
                f"SUM(CASE WHEN {_qualified(alias, measure)} IS NULL THEN 1 ELSE 0 END) "
                f"AS {_quote_column('_component_null_count')}"
            )
        group = key_expressions
        sql = (
            f"SELECT {', '.join(select)} FROM {_quote_table(table)} AS {_quote_column(alias)}"
            + (" WHERE " + " AND ".join(where) if where else "")
            + (" GROUP BY " + ", ".join(group) if group else "")
        )
        component_sql.append(sql)
        params.extend(component_params)
        if str(table) not in source_tables:
            source_tables.append(str(table))

    assert expected_keys is not None and expected_outputs is not None
    key_aliases = [alias for _, alias in expected_keys]
    output_aliases = [alias for _, alias in expected_outputs]
    outer_select = [
        *[_quote_column(alias) for alias in key_aliases],
        *[f"MAX({_quote_column(alias)}) AS {_quote_column(alias)}" for alias in output_aliases],
        f"SUM({_quote_column('_component_value')}) AS {_quote_column(value_alias)}",
        f"SUM({_quote_column('_component_count')}) AS {_quote_column('__matched_row_count')}",
    ]
    if include_null_count:
        outer_select.append(
            f"SUM({_quote_column('_component_null_count')}) AS "
            f"{_quote_column('__actual_null_count')}"
        )
    union_sql = " UNION ALL ".join(component_sql)
    sql = f"SELECT {', '.join(outer_select)} FROM ({union_sql}) AS {_quote_column('components')}"
    if key_aliases:
        sql += " GROUP BY " + ", ".join(_quote_column(alias) for alias in key_aliases)
    return sql, params, expected_keys, expected_outputs, source_tables


def _formal_dso_query(
    request: Mapping[str, Any], metric: Mapping[str, Any], datasets_contract: Mapping[str, Any], limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    debt_table, delivery_table = metric.get("debt_table"), metric.get("delivery_table")
    debt_dataset, delivery_dataset = _dataset(debt_table, datasets_contract), _dataset(delivery_table, datasets_contract)
    debt_time = _approved(metric.get("debt_time_field"), debt_dataset)
    debt_measure = _approved(metric.get("debt_measure"), debt_dataset)
    delivery_time = _approved(metric.get("delivery_time_field"), delivery_dataset)
    delivery_measure = _approved(metric.get("delivery_measure"), delivery_dataset)
    selected = request.get("dimensions") or []
    request_filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    mappings = metric.get("dimension_mappings") or {}
    if (
        not isinstance(selected, list)
        or len(selected) > _max_group_dimensions(metric)
        or not isinstance(request_filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "正式周转天数一次最多按一个维度分析。")
    allowed = set(metric.get("allowed_dimensions") or [])
    for code in [*selected, *request_filters.keys()]:
        if code not in allowed or code not in mappings:
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "正式周转天数不支持请求中的维度。")
    start, end, applied_time = _time_window(
        request, str(metric.get("time_policy") or ""), observed_on
    )
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if start_date.day != 1 or end_date.day != 1:
        raise AnalysisQueryError("INVALID_PLAN", "正式周转天数必须使用完整自然月。")
    period_months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
    if period_months < 1:
        raise AnalysisQueryError("INVALID_PLAN", "正式周转天数期间不足一个月。")
    _require_completed_month_window(start, end, observed_on)
    opening_month = _add_months(start_date, -1).strftime("%Y-%m")
    ending_month = _add_months(end_date, -1).strftime("%Y-%m")
    debt_end = end_date.strftime("%Y-%m")
    expected_snapshots = period_months + 1
    period_days = (end_date - start_date).days

    debt_keys, debt_outputs = _mapping_parts(selected, mappings, "debt", debt_dataset)
    delivery_keys, _ = _mapping_parts(selected, mappings, "delivery", delivery_dataset)
    debt_params: list[Any] = []
    debt_where = _fixed_filters("d", metric.get("debt_required_filters") or {}, debt_dataset, debt_params)
    debt_where += _mapping_filters(
        "d", "debt", request_filters, mappings, debt_dataset, debt_params, bindings
    )
    debt_where += [f"{_qualified('d', debt_time)} >= %s", f"{_qualified('d', debt_time)} < %s"]
    debt_params += [opening_month, debt_end]
    delivery_params: list[Any] = []
    delivery_where = _fixed_filters(
        "s", metric.get("delivery_required_filters") or {}, delivery_dataset, delivery_params
    )
    delivery_where += _mapping_filters(
        "s",
        "delivery",
        request_filters,
        mappings,
        delivery_dataset,
        delivery_params,
        bindings,
    )
    delivery_where += [f"{_qualified('s', delivery_time)} >= %s", f"{_qualified('s', delivery_time)} < %s"]
    delivery_params += [start, end]

    debt_key_select = [f"{_qualified('d', column)} AS {_quote_column(alias)}" for column, alias in debt_keys]
    debt_output_select = [f"MAX({_qualified('d', column)}) AS {_quote_column(alias)}" for column, alias in debt_outputs]
    debt_group = [*[_qualified("d", column) for column, _ in debt_keys], _qualified("d", debt_time)]
    debt_monthly_select = [
        *debt_key_select,
        *debt_output_select,
        f"{_qualified('d', debt_time)} AS bill_month",
        f"SUM({_qualified('d', debt_measure)}) AS partial_monthly_debt_rmb",
        "COUNT(*) AS debt_source_row_count",
        f"SUM(CASE WHEN {_qualified('d', debt_measure)} IS NULL THEN 1 ELSE 0 END) "
        f"AS debt_null_count",
    ]
    debt_monthly = (
        f"SELECT {', '.join(debt_monthly_select)} "
        f"FROM {_quote_table(debt_table)} AS d WHERE {' AND '.join(debt_where)} GROUP BY {', '.join(debt_group)}"
    )
    avg_key_select = [f"{_quote_column(alias)}" for _, alias in debt_keys]
    avg_output_select = [f"MAX({_quote_column(alias)}) AS {_quote_column(alias)}" for _, alias in debt_outputs]
    avg_group = [f"{_quote_column(alias)}" for _, alias in debt_keys]
    debt_avg_select = [
        *avg_key_select,
        *avg_output_select,
        f"CASE WHEN COUNT(DISTINCT bill_month) = {expected_snapshots} "
        f"THEN SUM(CASE WHEN bill_month IN (%s, %s) THEN partial_monthly_debt_rmb / 2 ELSE partial_monthly_debt_rmb END) / {period_months} "
        "ELSE NULL END AS partial_average_net_debt_rmb",
        "COUNT(DISTINCT bill_month) AS snapshot_month_count",
        "SUM(debt_null_count) AS debt_null_count",
        "SUM(debt_source_row_count) AS debt_source_row_count",
    ]
    debt_avg = (
        f"SELECT {', '.join(debt_avg_select)} "
        "FROM debt_monthly"
        + (" GROUP BY " + ", ".join(avg_group) if avg_group else "")
    )

    delivery_key_select = [f"{_qualified('s', column)} AS {_quote_column(alias)}" for column, alias in delivery_keys]
    delivery_monthly_group = [
        *[_qualified("s", column) for column, _ in delivery_keys],
        f"DATE_FORMAT({_qualified('s', delivery_time)}, '{_MYSQL_MONTH_FORMAT}')",
    ]
    delivery_monthly_select = [
        *delivery_key_select,
        f"DATE_FORMAT({_qualified('s', delivery_time)}, '{_MYSQL_MONTH_FORMAT}') AS delivery_month",
        f"SUM({_qualified('s', delivery_measure)}) AS partial_monthly_delivery_rmb",
        "COUNT(*) AS delivery_source_row_count",
        f"SUM(CASE WHEN {_qualified('s', delivery_measure)} IS NULL THEN 1 ELSE 0 END) "
        f"AS delivery_null_count",
    ]
    delivery_monthly = (
        f"SELECT {', '.join(delivery_monthly_select)} "
        f"FROM {_quote_table(delivery_table)} AS s WHERE {' AND '.join(delivery_where)} GROUP BY {', '.join(delivery_monthly_group)}"
    )
    delivery_agg_keys = [f"{_quote_column(alias)}" for _, alias in delivery_keys]
    delivery_agg = (
        f"SELECT {', '.join([*delivery_agg_keys, 'SUM(partial_monthly_delivery_rmb) AS partial_delivery_amount_rmb', 'SUM(partial_monthly_delivery_rmb > 0) AS effective_month_count', 'SUM(delivery_null_count) AS delivery_null_count', 'SUM(delivery_source_row_count) AS delivery_source_row_count'])} FROM delivery_monthly"
        + (" GROUP BY " + ", ".join(delivery_agg_keys) if delivery_agg_keys else "")
    )
    join = " AND ".join(
        f"a.{_quote_column(debt_alias)} <=> v.{_quote_column(delivery_alias)}"
        for (_, debt_alias), (_, delivery_alias) in zip(debt_keys, delivery_keys)
    ) or "1 = 1"
    output_dimensions = [f"a.{_quote_column(alias)}" for _, alias in debt_outputs]
    missing_inputs = (
        "COALESCE(a.debt_null_count, 0) + COALESCE(v.delivery_null_count, 0)"
    )
    source_rows = (
        "COALESCE(a.debt_source_row_count, 0) + COALESCE(v.delivery_source_row_count, 0)"
    )
    known_inputs = f"({source_rows} - ({missing_inputs}))"
    partial_average = "a.partial_average_net_debt_rmb"
    partial_delivery = "COALESCE(v.partial_delivery_amount_rmb, 0)"
    full_average = (
        f"CASE WHEN COALESCE(a.debt_null_count, 0) > 0 THEN NULL "
        f"ELSE {partial_average} END"
    )
    full_delivery = (
        f"CASE WHEN COALESCE(v.delivery_source_row_count, 0) = 0 OR COALESCE(v.delivery_null_count, 0) > 0 THEN NULL "
        f"ELSE {partial_delivery} END"
    )
    full_metric = (
        f"CASE WHEN ({missing_inputs}) > 0 THEN NULL "
        f"WHEN ({full_delivery}) > 0 THEN ({full_average}) * {period_days} / ({full_delivery}) "
        f"ELSE NULL END"
    )
    select = [
        *output_dimensions,
        f"{full_metric} AS metric_value",
        f"{full_average} AS average_net_debt_rmb",
        f"{full_delivery} AS delivery_amount_rmb",
        f"{period_days} AS period_natural_days",
        "a.snapshot_month_count",
        "COALESCE(v.effective_month_count, 0) AS effective_month_count",
        f"({missing_inputs}) AS missing_value_count",
        f"({known_inputs}) AS known_value_count",
        f"CASE WHEN COALESCE(a.debt_source_row_count, 0) > 0 AND COALESCE(v.delivery_source_row_count, 0) > 0 "
        f"THEN 1.0 * ({known_inputs}) / ({source_rows}) ELSE NULL END AS value_coverage_rate",
        f"CASE WHEN ({source_rows}) = 0 THEN 'missing' "
        f"WHEN ({missing_inputs}) = 0 AND ({full_metric}) IS NOT NULL THEN 'complete' "
        f"WHEN ({full_metric}) IS NULL THEN 'missing' "
        f"WHEN ({known_inputs}) = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
        f"{source_rows} AS `__matched_row_count`",
    ]
    output_aliases = [alias for _, alias in debt_outputs]
    sql = (
        f"WITH debt_monthly AS ({debt_monthly}), debt_avg AS ({debt_avg}), "
        f"delivery_monthly AS ({delivery_monthly}), delivery_agg AS ({delivery_agg}) "
        f"SELECT {', '.join(select)} FROM debt_avg AS a LEFT JOIN delivery_agg AS v ON {join}"
    )
    sql += _order_clause(
        request,
        output_aliases,
        allowed_value_fields={
            "metric_value",
            "average_net_debt_rmb",
            "delivery_amount_rmb",
            "snapshot_month_count",
            "effective_month_count",
        },
    )
    sql += " LIMIT %s"
    params = [*debt_params, opening_month, ending_month, *delivery_params, limit + 1]
    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, params, {
        "metric": request.get("metric"),
        "dataset": None,
        "source_datasets": [debt_table, delivery_table],
        "time_range": applied_time,
        "filters": request_filters,
        "warnings": warnings,
        "dimension_outputs": output_aliases,
    }


def _paired_amounts_query(
    request: Mapping[str, Any], metric: Mapping[str, Any], datasets_contract: Mapping[str, Any], limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    left, right = metric.get("left") or {}, metric.get("right") or {}
    selected = request.get("dimensions") or []
    request_filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    mappings = metric.get("dimension_mappings") or {}
    if (
        not isinstance(selected, list)
        or len(selected) > _max_group_dimensions(metric)
        or not isinstance(request_filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "出库收款对照一次最多按一个维度分析。")
    allowed = set(metric.get("allowed_dimensions") or [])
    for code in [*selected, *request_filters.keys()]:
        if code not in allowed or code not in mappings:
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "出库收款对照不支持请求中的维度。")
    start, end, applied_time = _time_window(
        request, str(metric.get("time_policy") or ""), observed_on
    )

    def aggregate(
        alias: str,
        table: str,
        measure: str,
        keys: list[tuple[str, str]],
        outputs: list[tuple[str, str]],
        where: list[str],
        value_alias: str,
    ) -> str:
        select = [
            *[f"{_qualified(alias, column)} AS {_quote_column(output)}" for column, output in keys],
            *[f"MAX({_qualified(alias, column)}) AS {_quote_column(output)}" for column, output in outputs],
            f"SUM({_qualified(alias, measure)}) AS {_quote_column(value_alias)}",
            f"COUNT(*) AS {_quote_column('__matched_row_count')}",
            f"SUM(CASE WHEN {_qualified(alias, measure)} IS NULL THEN 1 ELSE 0 END) "
            f"AS {_quote_column('__actual_null_count')}",
        ]
        group = [_qualified(alias, column) for column, _ in keys]
        return (
            f"SELECT {', '.join(select)} FROM {_quote_table(table)} AS {_quote_column(alias)} "
            f"WHERE {' AND '.join(where)}" + (" GROUP BY " + ", ".join(group) if group else "")
        )

    def one_or_components(
        source: Mapping[str, Any], side: str, alias: str, value_alias: str
    ) -> tuple[str, list[Any], list[tuple[str, str]], list[tuple[str, str]], list[str]]:
        if source.get("components") is not None:
            return _aggregate_components(
                source,
                selected,
                request_filters,
                start,
                end,
                datasets_contract,
                value_alias,
                bindings=bindings,
                include_null_count=True,
            )
        table = source.get("table")
        dataset = _dataset(table, datasets_contract)
        measure = _approved(source.get("measure"), dataset)
        time_field = _approved(source.get("time_field"), dataset)
        keys, outputs = _mapping_parts(selected, mappings, side, dataset)
        params: list[Any] = []
        where = _fixed_filters(alias, source.get("required_filters") or {}, dataset, params)
        where += _mapping_filters(
            alias, side, request_filters, mappings, dataset, params, bindings
        )
        where += [
            f"{_qualified(alias, time_field)} >= %s",
            f"{_qualified(alias, time_field)} < %s",
        ]
        params += [start, end]
        sql = aggregate(alias, str(table), measure, keys, outputs, where, value_alias)
        return sql, params, keys, outputs, [str(table)]

    left_sql, left_params, left_keys, left_outputs, left_tables = one_or_components(
        left, "left", "l", "left_amount"
    )
    right_sql, right_params, right_keys, right_outputs, right_tables = one_or_components(
        right, "right", "r", "right_amount"
    )
    if [alias for _, alias in left_keys] != [alias for _, alias in right_keys] or [
        alias for _, alias in left_outputs
    ] != [alias for _, alias in right_outputs]:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "出库收款两侧的对齐粒度不一致。")
    if left_keys:
        key_columns = [alias for _, alias in left_keys]
        keys_sql = " UNION ".join([
            "SELECT " + ", ".join(_quote_column(alias) for alias in key_columns) + " FROM left_agg",
            "SELECT " + ", ".join(_quote_column(alias) for _, alias in right_keys) + " FROM right_agg",
        ])
        left_join = " AND ".join(f"k.{_quote_column(alias)} <=> l.{_quote_column(alias)}" for alias in key_columns)
        right_join = " AND ".join(
            f"k.{_quote_column(left_alias)} <=> r.{_quote_column(right_alias)}"
            for (_, left_alias), (_, right_alias) in zip(left_keys, right_keys)
        )
        output_aliases = [alias for _, alias in left_outputs]
        output_dimensions = [
            f"COALESCE(l.{_quote_column(left_alias)}, r.{_quote_column(right_alias)}) AS {_quote_column(left_alias)}"
            for (_, left_alias), (_, right_alias) in zip(left_outputs, right_outputs)
        ]
        from_sql = f"keys_all AS ({keys_sql}) SELECT {{select}} FROM keys_all AS k LEFT JOIN left_agg AS l ON {left_join} LEFT JOIN right_agg AS r ON {right_join}"
    else:
        output_aliases, output_dimensions = [], []
        from_sql = "SELECT {select} FROM left_agg AS l CROSS JOIN right_agg AS r"
    left_rows = "COALESCE(l.__matched_row_count, 0)"
    right_rows = "COALESCE(r.__matched_row_count, 0)"
    left_nulls = "COALESCE(l.__actual_null_count, 0)"
    right_nulls = "COALESCE(r.__actual_null_count, 0)"
    total_rows = f"({left_rows} + {right_rows})"
    unmatched_side = (
        f"CASE WHEN {left_rows} = 0 THEN 1 ELSE 0 END + "
        f"CASE WHEN {right_rows} = 0 THEN 1 ELSE 0 END"
    )
    missing_inputs = f"({left_nulls} + {right_nulls})"
    known_inputs = f"({total_rows} - {left_nulls} - {right_nulls})"
    left_value = (
        f"CASE WHEN {left_rows} > 0 AND {left_nulls} = 0 "
        f"THEN l.left_amount ELSE NULL END"
    )
    right_value = (
        f"CASE WHEN {right_rows} > 0 AND {right_nulls} = 0 "
        f"THEN r.right_amount ELSE NULL END"
    )
    metric_value = (
        f"CASE WHEN {missing_inputs} > 0 OR ({unmatched_side}) > 0 THEN NULL ELSE "
        f"COALESCE(l.left_amount, 0) - COALESCE(r.right_amount, 0) END"
    )
    select = [
        *output_dimensions,
        f"{metric_value} AS metric_value",
        f"{left_value} AS net_delivery_amount_rmb",
        f"{right_value} AS net_receipt_amount_rmb",
        f"CASE WHEN {missing_inputs} = 0 AND ({unmatched_side}) = 0 AND COALESCE(l.left_amount, 0) > 0 "
        f"THEN COALESCE(r.right_amount, 0) / l.left_amount ELSE NULL END AS receipt_coverage",
        f"{missing_inputs} AS missing_value_count",
        f"{known_inputs} AS known_value_count",
        f"CASE WHEN ({unmatched_side}) = 0 AND ({known_inputs}) + ({missing_inputs}) > 0 THEN "
        f"1.0 * ({known_inputs}) / (({known_inputs}) + ({missing_inputs})) ELSE NULL END AS value_coverage_rate",
        f"CASE WHEN {total_rows} = 0 THEN 'missing' "
        f"WHEN {missing_inputs} = 0 AND ({unmatched_side}) = 0 THEN 'complete' "
        f"WHEN {known_inputs} = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
        f"{total_rows} AS `__matched_row_count`",
    ]
    ctes = f"WITH left_agg AS ({left_sql}), right_agg AS ({right_sql}), " if left_keys else f"WITH left_agg AS ({left_sql}), right_agg AS ({right_sql}) "
    if left_keys:
        sql = ctes + from_sql.format(select=", ".join(select))
    else:
        sql = ctes + from_sql.format(select=", ".join(select))
    sql += _order_clause(
        request,
        output_aliases,
        allowed_value_fields={
            "metric_value",
            "net_delivery_amount_rmb",
            "net_receipt_amount_rmb",
            "receipt_coverage",
        },
    )
    sql += " LIMIT %s"
    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, [*left_params, *right_params, limit + 1], {
        "metric": request.get("metric"),
        "dataset": None,
        "source_datasets": [*left_tables, *[table for table in right_tables if table not in left_tables]],
        "time_range": applied_time,
        "filters": request_filters,
        "warnings": warnings,
        "dimension_outputs": output_aliases,
    }


def _allocated_amount_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    selected = request.get("dimensions") or []
    request_filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    if (
        not isinstance(selected, list)
        or len(selected) > _max_group_dimensions(metric)
        or not isinstance(request_filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "分摊净额一次最多按两个业务维度展开。")
    time_bucket = request.get("time_bucket")

    source_code = metric.get("source_completion_metric")
    source_path = metric.get("source_path")
    metrics = semantics.get("metrics") or {}
    source_metric = metrics.get(source_code) if isinstance(metrics, dict) else None
    if isinstance(source_metric, dict):
        _ensure_available(source_metric)
    paths = source_metric.get("paths") if isinstance(source_metric, dict) else None
    path = paths.get(source_path) if isinstance(paths, dict) else None
    if not isinstance(path, dict) or path.get("ledger") != "salesperson_allocation":
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "分摊净额缺少业务员分摊来源。")
    _ensure_available(path)

    allowed = set(path.get("allowed_dimensions") or [])
    requested_codes = [*selected, *request_filters.keys()]
    if any(code not in allowed for code in requested_codes):
        raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "分摊净额不支持请求中的维度。")

    start, end, applied_time = _time_window(
        request, str(metric.get("time_policy") or ""), observed_on
    )
    actual = path.get("actual") or {}
    sql, params, _keys, outputs, source_tables = _aggregate_components(
        actual,
        selected,
        request_filters,
        start,
        end,
        datasets_contract,
        "metric_value",
        time_bucket,
        bindings,
        include_null_count=True,
    )
    missing = "COALESCE(__actual_null_count, 0)"
    known = f"__matched_row_count - ({missing})"
    projection = [*[_quote_column(alias) for _, alias in [*_keys, *outputs]],
        f"CASE WHEN {missing} > 0 THEN NULL ELSE metric_value END AS metric_value",
        "__matched_row_count",
        f"{missing} AS missing_value_count", f"{known} AS known_value_count",
        f"CASE WHEN __matched_row_count > 0 THEN 1.0 * ({known}) / __matched_row_count ELSE NULL END AS value_coverage_rate",
        f"CASE WHEN __matched_row_count = 0 THEN 'missing' WHEN {missing} = 0 THEN 'complete' WHEN {known} = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
    ]
    # Keys and display outputs can share an alias; expose each only once.
    projection = list(dict.fromkeys(projection))
    sql = f"SELECT {', '.join(projection)} FROM ({sql}) AS allocated"
    output_aliases = (["period"] if time_bucket == "month" else []) + [alias for _, alias in outputs]
    sql += _order_clause(
        request,
        output_aliases,
        allowed_value_fields={"metric_value"},
        default_field="period" if time_bucket == "month" else "metric_value",
        default_direction="ASC" if time_bucket == "month" else "DESC",
    )
    sql += " LIMIT %s"
    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, [*params, limit + 1], {
        "metric": request.get("metric"),
        "dataset": None,
        "source_datasets": source_tables,
        "time_range": applied_time,
        "filters": request_filters,
        "warnings": warnings,
        "dimension_outputs": output_aliases,
    }


def _target_completion_query(
    request: Mapping[str, Any], metric: Mapping[str, Any], datasets_contract: Mapping[str, Any], limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    query_observed_on = observed_on or _business_today()
    selected = request.get("dimensions") or []
    request_filters = request.get("metric_filters") or {}
    bindings = _entity_bindings(request)
    if (
        not isinstance(selected, list)
        or len(selected) > _max_group_dimensions(metric)
        or not isinstance(request_filters, dict)
    ):
        raise AnalysisQueryError("INVALID_PLAN", "目标完成分析一次最多按两个业务维度展开。")
    time_bucket = request.get("time_bucket")

    requested_codes = [*selected, *request_filters.keys()]
    path_code = request.get("attribution_mode")
    paths = metric.get("paths") or {}
    path = paths.get(path_code) if isinstance(paths, dict) else None
    if not isinstance(path, dict):
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "目标完成指标缺少受控取数路径。")
    _ensure_available(path)
    allowed = set(path.get("allowed_dimensions") or [])
    mappings = path.get("dimension_mappings") or {}
    for code in requested_codes:
        if code not in allowed or code not in mappings:
            raise AnalysisQueryError("UNSUPPORTED_DIMENSION", "目标完成指标不支持请求中的维度。")

    target, actual = path.get("target") or {}, path.get("actual") or {}
    target_table = target.get("table")
    target_dataset = _dataset(target_table, datasets_contract)
    target_measure = _approved(target.get("measure"), target_dataset)
    target_time = _approved(target.get("time_field"), target_dataset)
    target_time_value_format = target.get("time_value_format", "month")
    if target.get("time_granularity") == "month" and target_time_value_format not in {
        "month",
        "date",
    }:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "目标时间值格式无效。")

    if time_bucket == "month" and request.get("time_range") is None:
        current_month = query_observed_on.replace(day=1)
        trend_start = _add_months(current_month, -5)
        trend_end = _add_months(current_month, 1)
        start, end = trend_start.isoformat(), trend_end.isoformat()
        applied_time = {
            "start": start,
            "end": end,
            "source": "latest_6_natural_months_including_current",
        }
    else:
        start, end, applied_time = _time_window(
            request, str(metric.get("time_policy") or ""), query_observed_on
        )
    if time_bucket is None:
        current_month = query_observed_on.replace(day=1)
        next_month = _add_months(current_month, 1)
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        if start_date < next_month and end_date > next_month:
            raise AnalysisQueryError(
                "TARGET_MIXED_PERIOD_REQUIRES_MONTHLY_BUCKET",
                "同时包含已发生期间和未来月份的目标查询必须按月展示。",
            )
    target_start, target_end = start, end
    if target.get("time_granularity") == "month":
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        if start_date.day != 1 or end_date.day != 1:
            raise AnalysisQueryError("INVALID_PLAN", "月粒度目标必须使用自然月首日边界。")
        if target_time_value_format == "month":
            target_start, target_end = start_date.strftime("%Y-%m"), end_date.strftime("%Y-%m")
        else:
            target_start, target_end = start, end

    actual_start = start
    actual_end = min(
        date.fromisoformat(end), query_observed_on + timedelta(days=1)
    ).isoformat()

    target_keys, target_outputs = _mapping_parts(selected, mappings, "target", target_dataset)
    if time_bucket == "month":
        target_keys = [(_PERIOD_KEY, "period"), *target_keys]
    target_params: list[Any] = []
    target_where = _fixed_filters(
        "t", target.get("required_filters") or {}, target_dataset, target_params
    )
    target_where += _mapping_filters(
        "t",
        "target",
        request_filters,
        mappings,
        target_dataset,
        target_params,
        bindings,
    )
    target_where += [f"{_qualified('t', target_time)} >= %s", f"{_qualified('t', target_time)} < %s"]
    target_params += [target_start, target_end]

    def aggregate(
        alias: str,
        table: str,
        measure: str,
        keys: list[tuple[str, str]],
        outputs: list[tuple[str, str]],
        where: list[str],
        value_alias: str,
        *,
        time_field: str,
        monthly_source: bool = False,
        target_measure_with_null_state: bool = False,
        actual_measure_with_null_state: bool = False,
    ) -> str:
        period_expression = (
            _qualified(alias, time_field)
            if monthly_source
            else f"DATE_FORMAT({_qualified(alias, time_field)}, '{_MYSQL_MONTH_FORMAT}')"
        )
        key_expressions = [
            period_expression if column == _PERIOD_KEY else _qualified(alias, column)
            for column, _ in keys
        ]
        select = [
            *[
                f"{expression} AS {_quote_column(output)}"
                for expression, (_, output) in zip(key_expressions, keys)
            ],
            *[f"MAX({_qualified(alias, column)}) AS {_quote_column(output)}" for column, output in outputs],
            f"SUM({_qualified(alias, measure)}) AS {_quote_column(value_alias)}",
            f"COUNT(*) AS {_quote_column('__matched_row_count')}",
        ]
        if target_measure_with_null_state:
            select.append(
                f"SUM(CASE WHEN {_qualified(alias, measure)} IS NULL THEN 1 ELSE 0 END) "
                f"AS {_quote_column('__target_null_count')}"
            )
        if actual_measure_with_null_state:
            select.append(
                f"SUM(CASE WHEN {_qualified(alias, measure)} IS NULL THEN 1 ELSE 0 END) "
                f"AS {_quote_column('__actual_null_count')}"
            )
        group = key_expressions
        return (
            f"SELECT {', '.join(select)} FROM {_quote_table(table)} AS {_quote_column(alias)} "
            f"WHERE {' AND '.join(where)}" + (" GROUP BY " + ", ".join(group) if group else "")
        )

    target_sql = aggregate(
        "t",
        target_table,
        target_measure,
        target_keys,
        target_outputs,
        target_where,
        "target_amount_rmb",
        time_field=target_time,
        monthly_source=(
            target.get("time_granularity") == "month"
            and target_time_value_format == "month"
        ),
        target_measure_with_null_state=True,
    )
    if actual.get("components") is not None:
        actual_sql, actual_params, actual_keys, actual_outputs, actual_tables = _aggregate_components(
            actual,
            selected,
            request_filters,
            actual_start,
            actual_end,
            datasets_contract,
            "actual_amount_rmb",
            time_bucket,
            bindings,
            include_null_count=True,
        )
    else:
        actual_table = actual.get("table")
        actual_dataset = _dataset(actual_table, datasets_contract)
        actual_measure = _approved(actual.get("measure"), actual_dataset)
        actual_time = _approved(actual.get("time_field"), actual_dataset)
        actual_keys, actual_outputs = _mapping_parts(selected, mappings, "actual", actual_dataset)
        if time_bucket == "month":
            actual_keys = [(_PERIOD_KEY, "period"), *actual_keys]
        actual_params = []
        actual_where = _fixed_filters(
            "a", actual.get("required_filters") or {}, actual_dataset, actual_params
        )
        actual_where += _mapping_filters(
            "a",
            "actual",
            request_filters,
            mappings,
            actual_dataset,
            actual_params,
            bindings,
        )
        actual_where += [
            f"{_qualified('a', actual_time)} >= %s",
            f"{_qualified('a', actual_time)} < %s",
        ]
        actual_params += [actual_start, actual_end]
        actual_sql = aggregate(
            "a",
            str(actual_table),
            actual_measure,
            actual_keys,
            actual_outputs,
            actual_where,
            "actual_amount_rmb",
            time_field=actual_time,
            actual_measure_with_null_state=True,
        )
        actual_tables = [str(actual_table)]
    if [alias for _, alias in target_keys] != [alias for _, alias in actual_keys] or [
        alias for _, alias in target_outputs
    ] != [alias for _, alias in actual_outputs]:
        raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "目标与实际的对齐粒度不一致。")

    if target_keys:
        target_key_aliases = [alias for _, alias in target_keys]
        keys_sql = " UNION ".join([
            "SELECT " + ", ".join(_quote_column(alias) for alias in target_key_aliases) + " FROM target_agg",
            "SELECT " + ", ".join(_quote_column(alias) for _, alias in actual_keys) + " FROM actual_agg",
        ])
        target_join = " AND ".join(
            f"k.{_quote_column(alias)} <=> t.{_quote_column(alias)}" for alias in target_key_aliases
        )
        actual_join = " AND ".join(
            f"k.{_quote_column(target_alias)} <=> a.{_quote_column(actual_alias)}"
            for (_, target_alias), (_, actual_alias) in zip(target_keys, actual_keys)
        )
        output_aliases = (["period"] if time_bucket == "month" else []) + [
            alias for _, alias in target_outputs
        ]
        output_dimensions = [
            *([f"k.{_quote_column('period')} AS {_quote_column('period')}"] if time_bucket == "month" else []),
            *[
                f"COALESCE(t.{_quote_column(target_alias)}, a.{_quote_column(actual_alias)}) AS {_quote_column(target_alias)}"
                for (_, target_alias), (_, actual_alias) in zip(target_outputs, actual_outputs)
            ],
        ]
        from_sql = (
            f"keys_all AS ({keys_sql}) SELECT {{select}} FROM keys_all AS k "
            f"LEFT JOIN target_agg AS t ON {target_join} LEFT JOIN actual_agg AS a ON {actual_join}"
        )
        ctes = f"WITH target_agg AS ({target_sql}), actual_agg AS ({actual_sql}), "
    else:
        output_aliases, output_dimensions = [], []
        from_sql = "SELECT {select} FROM target_agg AS t CROSS JOIN actual_agg AS a"
        ctes = f"WITH target_agg AS ({target_sql}), actual_agg AS ({actual_sql}) "

    target_value = "COALESCE(t.target_amount_rmb, 0)"
    actual_value = "COALESCE(a.actual_amount_rmb, 0)"
    target_rows = "COALESCE(t.__matched_row_count, 0)"
    actual_rows = "COALESCE(a.__matched_row_count, 0)"
    target_nulls = "COALESCE(t.__target_null_count, 0)"
    actual_nulls = "COALESCE(a.__actual_null_count, 0)"
    if time_bucket == "month":
        current_period = query_observed_on.strftime("%Y-%m")
        period_state = (
            f"CASE WHEN k.`period` > '{current_period}' THEN 'not_started' "
            f"WHEN k.`period` = '{current_period}' THEN 'in_progress' "
            "ELSE 'completed' END"
        )
    else:
        current_month = query_observed_on.replace(day=1)
        next_month = _add_months(current_month, 1)
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        if start_date >= next_month:
            state = "not_started"
        elif end_date > next_month:
            state = "includes_future"
        elif start_date >= current_month and end_date <= next_month:
            state = "in_progress"
        elif end_date <= current_month:
            state = "completed"
        else:
            state = "includes_in_progress"
        period_state = f"'{state}'"
    target_state = (
        f"CASE WHEN {target_rows} = 0 AND {period_state} IN ('not_started', 'includes_future') "
        f"THEN 'not_set_for_future' "
        f"WHEN {target_rows} = 0 THEN 'missing' "
        f"WHEN {target_nulls} > 0 THEN 'incomplete' "
        f"WHEN {target_value} = 0 THEN 'zero' ELSE 'set' END"
    )
    actual_state = (
        f"CASE WHEN {period_state} IN ('not_started', 'includes_future') THEN 'not_started' "
        f"WHEN {actual_rows} = 0 THEN 'missing' "
        f"WHEN {actual_nulls} > 0 THEN 'incomplete' ELSE 'reported' END"
    )
    target_output = (
        f"CASE WHEN {target_rows} = 0 OR {target_nulls} > 0 "
        f"THEN NULL ELSE {target_value} END"
    )
    actual_output = (
        f"CASE WHEN {period_state} IN ('not_started', 'includes_future') "
        f"OR {actual_rows} = 0 OR {actual_nulls} > 0 THEN NULL ELSE {actual_value} END"
    )
    completion = (
        f"CASE WHEN {period_state} IN ('not_started', 'includes_future') "
        f"OR {target_rows} = 0 OR {actual_rows} = 0 OR {target_nulls} > 0 OR {actual_nulls} > 0 "
        f"OR {target_value} = 0 THEN NULL "
        f"ELSE {actual_value} / {target_value} END"
    )
    gap = (
        f"CASE WHEN {period_state} IN ('not_started', 'includes_future') "
        f"OR {target_rows} = 0 OR {actual_rows} = 0 OR {target_nulls} > 0 OR {actual_nulls} > 0 "
        f"THEN NULL ELSE {target_value} - {actual_value} END"
    )
    select = [
        *output_dimensions,
        f"{completion} AS metric_value",
        f"{completion} AS completion_rate",
        f"{target_output} AS target_amount_rmb",
        f"{actual_output} AS actual_amount_rmb",
        f"{gap} AS gap_amount_rmb",
        f"{target_state} AS target_data_state",
        f"{target_nulls} AS target_missing_count",
        f"{actual_state} AS actual_data_state",
        f"{period_state} AS period_state",
        "COALESCE(t.__matched_row_count, 0) + COALESCE(a.__matched_row_count, 0) AS `__matched_row_count`",
    ]
    sql = ctes + from_sql.format(select=", ".join(select))
    value_fields = {
        "metric_value",
        "completion_rate",
        "target_amount_rmb",
        "actual_amount_rmb",
        "gap_amount_rmb",
    }
    order_by = request.get("order_by")
    ranking_plan = None
    if not output_aliases:
        if order_by is not None:
            raise AnalysisQueryError("INVALID_PLAN", "无分组目标完成指标不接受 order_by。")
    elif order_by is None and time_bucket == "month":
        sql += " ORDER BY `period` ASC"
    else:
        field = "completion_rate"
        direction = "DESC"
        if order_by is not None:
            if not isinstance(order_by, dict):
                raise AnalysisQueryError("INVALID_PLAN", "目标完成排序定义无效。")
            field = str(order_by.get("field") or "")
            direction = str(order_by.get("direction") or "").upper()
            if field not in {*value_fields, *output_aliases} or direction not in {"ASC", "DESC"}:
                raise AnalysisQueryError("INVALID_PLAN", "目标完成排序字段或方向不受支持。")
        value_order = f"{_quote_column(field)} {direction}"
        if field in value_fields:
            sql = (f"SELECT ranked.*,COUNT(*) OVER () AS rank_population_count, "
                   f"SUM(CASE WHEN {_quote_column(field)} IS NULL THEN 1 ELSE 0 END) OVER () AS rank_unknown_value_count, "
                   f"RANK() OVER (ORDER BY ({_quote_column(field)} IS NULL) ASC,{value_order}) AS query_rank, "
                   f"COUNT(*) OVER (PARTITION BY {_quote_column(field)}) AS rank_tie_count FROM ({sql}) AS ranked")
            ranking_plan = {"field": field, "direction": direction.lower()}
        if field in {"metric_value", "completion_rate"}:
            sql += f" ORDER BY ({_quote_column(field)} IS NULL) ASC, {_quote_column(field)} {direction}"
        else:
            sql += f" ORDER BY ({_quote_column(field)} IS NULL) ASC, {_quote_column(field)} {direction}"
    sql += " LIMIT %s"

    warnings = [str(metric.get("answer_note"))] if metric.get("answer_note") else []
    return sql, [*target_params, *actual_params, limit + 1], {
        "metric": request.get("metric"),
        "dataset": None,
        "source_datasets": [str(target_table), *[table for table in actual_tables if table != target_table]],
        "time_range": applied_time,
        "filters": request_filters,
        "warnings": warnings,
        "dimension_outputs": output_aliases,
        "ranking_plan": ranking_plan,
    }


def _registered_slow_pool_query(request, metric, datasets_contract, semantics, limit, *, observed_on=None):
    """Current registered records only; no baseline, writes, price output or unit fallback."""
    if any(request.get(k) is not None for k in ('time_range', 'calendar_month', 'comparison', 'time_bucket')):
        raise AnalysisQueryError('INVALID_PLAN', '当前登记池只回答本次读取状态，不接受历史期间或基线比较。')
    table = metric.get('table')
    dataset = _dataset(table, datasets_contract)
    dimensions = _effective_dimensions(semantics, metric)
    chosen = request.get('dimensions') or []
    filters = request.get('metric_filters') or {}
    if not isinstance(chosen, list) or len(chosen) > _max_group_dimensions(metric) or len(set(chosen)) != len(chosen) or not isinstance(filters, dict):
        raise AnalysisQueryError('INVALID_PLAN', '登记池分组或筛选无效。')
    allowed = set(metric.get('allowed_dimensions') or [])
    if any(k not in allowed for k in [*chosen, *filters]):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION', '登记池不支持该维度。')
    statistic = metric.get('pool_statistic')
    statistics = {'quantity', 'rolls', 'entries', 'products', 'skus', 'discount_rolls', 'priced_rolls', 'overlap_rolls', 'unknown_label_rolls', 'unpriced_rolls', 'classification_rolls'}
    if statistic not in statistics:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '登记池统计口径未登记。')
    if statistic == 'quantity' and 'unit' not in chosen:
        values = filters.get('unit')
        if values is None or (isinstance(values, list) and len(values) != 1):
            raise AnalysisQueryError('UNIT_SCOPE_REQUIRED', '登记池数量必须按库存单位分组或限定单一库存单位。')
    def column(name):
        return _qualified('s', _approved(name, dataset))
    unit = f"CASE WHEN LOWER({column('source_unit')}) = 'm' THEN 'm' ELSE NULLIF(TRIM({column('source_unit')}), '') END"
    output_names = []
    selections = []
    for name in chosen:
        definition = dimensions[name]
        if definition.get('source'):
            raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '登记池不连接未经确认的主数据。')
        for field, alias in _dimension_columns(definition):
            if alias in output_names:
                raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '登记池维度重复。')
            selections.append(f"{unit if name == 'unit' else column(field)} AS {_quote_column(alias)}")
            output_names.append(alias)
    policy = metric.get('classification_policy') or {}
    labels = policy.get('known_labels')
    discount = policy.get('discountable_label')
    units = dimensions['unit'].get('value_contract', {}).get('allowed_values')
    if not isinstance(labels, list) or not labels or discount not in labels or not isinstance(units, list) or not units:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '登记池分类或库存单位定义无效。')
    params = [*labels, discount, *units]
    label = column('slow_label')
    selections.extend([
        f"{column('id')} AS pool_id", f"{column('goods_id')} AS pool_product_id",
        f"{column('goods_sku_id')} AS pool_sku_id", f"{column('goods_num')} AS pool_quantity",
        f"{column('piece_num')} AS pool_rolls",
        f"CASE WHEN {label} IN ({','.join(['%s']*len(labels))}) THEN CASE WHEN {label} = %s THEN 1 ELSE 0 END ELSE NULL END AS discount_flag",
        f"CASE WHEN {unit} IN ({','.join(['%s']*len(units))}) THEN 0 ELSE 1 END AS unknown_unit",
        f"CASE WHEN {column('promotion_price')} > 0 THEN 1 ELSE 0 END AS positive_price_flag",
        f"CASE WHEN {column('promotion_price')} IS NULL THEN 1 ELSE 0 END AS missing_price_flag",
    ])
    where = []
    for field, spec in (metric.get('required_filters') or {}).items():
        _approved(field, dataset)
        where.append(_filter_clause('s', field, spec, params))
    bindings = _entity_bindings(request)
    for name, value in filters.items():
        definition = dimensions[name]
        binding, value = _bound_value(bindings, name, value)
        field = _dimension_filter(definition, binding)
        _approved(field, dataset)
        if name == 'pool_sku':
            values = value if isinstance(value, list) else [value]
            if any(isinstance(v, bool) or not (isinstance(v, int) and v >= 0 or isinstance(v, str) and v.isascii() and v.isdecimal()) for v in values):
                raise AnalysisQueryError('INVALID_PLAN', '规格标识必须是明确的整数。')
        clause = _value_filter('s', field, value, params)
        if name == 'unit':clause = clause.replace(_qualified('s', field), unit)
        where.append(clause)
    cte = f"WITH pool AS (SELECT {', '.join(selections)} FROM {_quote_table(table)} AS s"
    if where:cte += ' WHERE ' + ' AND '.join(where)
    cte += ') '
    if statistic in {'entries','products','skus'}:
        field = {'entries':'pool_id','products':'pool_product_id','skus':'pool_sku_id'}[statistic]
        value = f"COUNT({'DISTINCT ' if statistic != 'entries' else ''}{field})"
        missing_row = f'{field} IS NULL'
    elif statistic == 'quantity':
        value = 'COALESCE(SUM(CASE WHEN unknown_unit=0 THEN pool_quantity ELSE NULL END),0)'
        missing_row = 'pool_quantity IS NULL OR unknown_unit=1'
    else:
        condition, unknown = {
            'rolls':('1=1','1=0'),
            'classification_rolls':('1=1','1=0'),
            'discount_rolls':('discount_flag=1','discount_flag IS NULL'),
            'priced_rolls':('positive_price_flag=1','1=0'),
            'overlap_rolls':('discount_flag=1 AND positive_price_flag=1','discount_flag IS NULL AND positive_price_flag=1'),
            'unknown_label_rolls':('discount_flag IS NULL','1=0'),
            'unpriced_rolls':('missing_price_flag=1','1=0'),
        }[statistic]
        value = f'COALESCE(SUM(CASE WHEN {condition} THEN pool_rolls ELSE 0 END),0)'
        missing_row = f'({unknown}) OR (({condition}) AND pool_rolls IS NULL)'
    missing = f'COALESCE(SUM(CASE WHEN {missing_row} THEN 1 ELSE 0 END),0)'
    known = f'COUNT(*)-({missing})'
    select = [*[_quote_column(k) for k in output_names],
        f'CASE WHEN {missing}>0 THEN NULL ELSE {value} END AS metric_value',
        f'CASE WHEN {known}>0 THEN {value} ELSE NULL END AS known_subset_value',
        f'{missing} AS missing_value_count', f'{known} AS known_value_count',
        f'CASE WHEN COUNT(*)>0 THEN 1.0*({known})/COUNT(*) ELSE NULL END AS value_coverage_rate',
        f"CASE WHEN COUNT(*)=0 THEN 'missing' WHEN {missing}=0 THEN 'complete' WHEN {known}=0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
        'COUNT(*) AS __matched_row_count',
    ]
    if statistic == 'classification_rolls':
        for field, condition, unknown in [
            ('pool_discountable_rolls','discount_flag=1','discount_flag IS NULL'),
            ('pool_priced_rolls','positive_price_flag=1','1=0'),
            ('pool_overlap_rolls','discount_flag=1 AND positive_price_flag=1','discount_flag IS NULL AND positive_price_flag=1'),
            ('pool_unknown_label_rolls','discount_flag IS NULL','1=0'),
            ('pool_unpriced_rolls','missing_price_flag=1','1=0'),
        ]:
            miss = f'COALESCE(SUM(CASE WHEN ({unknown}) OR (({condition}) AND pool_rolls IS NULL) THEN 1 ELSE 0 END),0)'
            val = f'COALESCE(SUM(CASE WHEN {condition} THEN pool_rolls ELSE 0 END),0)'
            select.append(f'CASE WHEN {miss}>0 THEN NULL ELSE {val} END AS {field}')
            if field in {'pool_discountable_rolls','pool_overlap_rolls'}:
                select.append(f'CASE WHEN COUNT(*)-({miss})>0 THEN {val} ELSE NULL END AS pool_known_{field[5:]}')
    sql = cte + 'SELECT ' + ', '.join(select) + ' FROM pool'
    if output_names:sql += ' GROUP BY '+','.join(_quote_column(k) for k in output_names)
    sql += _order_clause(request, output_names, allowed_value_fields={'metric_value'})+' LIMIT %s'
    params.append(limit+1)
    return sql, params, {'metric':request.get('metric'),'dataset':table,'source_datasets':[table],
        'time_range':{'source':'current_snapshot','as_of_date':(observed_on or _business_today()).isoformat()},
        'filters':filters,'dimension_outputs':output_names,'warnings':[metric.get('answer_note','')]}


def _frozen_pool_comparison_query(request, metric, datasets_contract, semantics, limit, *, observed_on=None):
    """Compare recorded baselines with current records, never physical-batch disposal."""
    import re
    if any(request.get(k) is not None for k in ('time_range','calendar_month','comparison','time_bucket')):
        raise AnalysisQueryError('INVALID_PLAN', '仅支持已有冻结时点到本次读取，不用当前池回填历史期末。')
    week = request.get('baseline_week')
    today = observed_on or _business_today()
    iso = today.isocalendar()
    current_week = f'{iso.year:04d}-W{iso.week:02d}'
    if week is not None:
        try:
            if not isinstance(week,str) or not re.fullmatch(r'[0-9]{4}-W[0-9]{2}',week):raise ValueError()
            date.fromisocalendar(int(week[:4]),int(week[6:]),1)
            if week > current_week:raise ValueError()
        except ValueError:
            raise AnalysisQueryError('INVALID_PLAN','基线周必须是当前或过去的合法周标签。')
    mode = metric.get('baseline_view')
    if mode not in {'groups','summary'}:raise AnalysisQueryError('CONTRACT_UNAVAILABLE','基线比较视图未登记。')
    movement = request.get('movement_state')
    states = ('New','Exited','Reduced','No Change','Increased','Unassessable')
    if movement is not None and (mode != 'groups' or movement not in states):
        raise AnalysisQueryError('INVALID_PLAN','变化状态筛选仅适用于比较明细。')
    chosen = request.get('dimensions') or []
    if chosen:
        expected={'product','pool_sku','warehouse_department','unit'} if mode=='groups' else {'unit','warehouse_department'}
        if mode=='groups' and set(chosen)!=expected or mode=='summary' and (not set(chosen)<=expected or 'unit' not in chosen):
            raise AnalysisQueryError('UNSUPPORTED_DIMENSION','明细保持产品、规格、部门、单位粒度；汇总按单位，可附加仓库部门。')
    grouping=['unit','whse_dept'] if mode=='summary' and 'warehouse_department' in chosen else ['unit']
    effective_dimensions = chosen or (['product','pool_sku','warehouse_department','unit'] if mode=='groups' else ['unit'])
    base_table=metric.get('baseline_table');current_table=metric.get('table')
    base_ds=_dataset(base_table,datasets_contract);current_ds=_dataset(current_table,datasets_contract)
    scale=metric.get('quantity_scale')
    if not isinstance(scale,int) or isinstance(scale,bool) or scale!=4:
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE','基线比较只接受已核验的四位小数数量精度。')
    def source(table,ds,alias,baseline=False):
        def col(k):return _qualified(alias,_approved(k,ds))
        qty=col('total_qty' if baseline else 'goods_num')
        rolls=col('total_piece' if baseline else 'piece_num')
        unit=f"(CASE WHEN LOWER(TRIM({col('source_unit')}))='m' THEN 'm' ELSE NULLIF(TRIM({col('source_unit')}),'') END) COLLATE utf8mb4_bin"
        dept=f"NULLIF({col('whse_dept')},'') COLLATE utf8mb4_bin"
        whitelist_unknown='0' if baseline else f"CASE WHEN {col('is_whitelist')} IS NULL OR {col('is_whitelist')} NOT IN ('n','y') THEN 1 ELSE 0 END"
        membership='1'
        if not baseline:
            criteria=metric.get('required_filters') or {}
            if set(criteria)!={'goods_num','is_whitelist'}:
                raise AnalysisQueryError('CONTRACT_UNAVAILABLE','当前池纳入条件定义不完整。')
            criterion_params=[]
            quantity_condition=_filter_clause(alias,'goods_num',criteria['goods_num'],criterion_params)
            whitelist_condition=_filter_clause(alias,'is_whitelist',criteria['is_whitelist'],criterion_params)
            predicate=f"({quantity_condition} AND CASE WHEN {col('is_whitelist')} IN ('n','y') THEN ({whitelist_condition}) ELSE NULL END)"
            membership=f"CASE WHEN {predicate} THEN 1 WHEN NOT({predicate}) THEN 0 ELSE -1 END"
            params.extend(criterion_params*2)
        return f"SELECT {col('source_row_id' if baseline else 'id')} AS source_id,{col('goods_id')} AS goods_id,{col('goods_sku_id')} AS goods_sku_id,{col('goods_name')} AS goods_name,{dept} AS whse_dept,{unit} AS unit,{qty} AS qty,{rolls} AS rolls,{col('slow_label')} AS slow_label,{membership} AS membership,{whitelist_unknown} AS unknown_whitelist FROM {_quote_table(table)} AS {alias}" + (f" WHERE {col('week_label')}=(SELECT baseline_week FROM clock)" if baseline else '')
    params=[week if week is not None else current_week]
    choice='%s' if week is not None else f"(SELECT MAX({_quote_column('week_label')}) FROM {_quote_table(base_table)} WHERE week_label<=%s AND week_label REGEXP '^[0-9]{{4}}-W[0-9]{{2}}$')"
    ctes=[f"clock AS (SELECT {choice} AS baseline_week,NOW(6) AS closing_read_at,UTC_TIMESTAMP(6) AS closing_utc_at,TIMESTAMPDIFF(SECOND,UTC_TIMESTAMP(6),NOW(6)) AS observed_clock_offset_seconds)"]
    for k in ('week_label','source_row_id','frozen_at','baseline_version','source_table'):_approved(k,base_ds)
    ctes.append(f"meta AS (SELECT COUNT(*) AS __baseline_rows,COUNT(DISTINCT source_row_id) AS __baseline_ids,COUNT(frozen_at) AS __baseline_timed_rows,COUNT(DISTINCT frozen_at) AS __baseline_times,MIN(frozen_at) AS baseline_frozen_at,COALESCE(SUM(CASE WHEN baseline_version=2 AND source_table=%s THEN 0 ELSE 1 END),0) AS __baseline_bad_source FROM {_quote_table(base_table)} WHERE week_label=(SELECT baseline_week FROM clock))")
    params.append(current_table)
    ctes.extend(['b_raw AS ('+source(base_table,base_ds,'b',True)+')','c_raw AS ('+source(current_table,current_ds,'c')+')'])
    filters=request.get('metric_filters') or {}
    if not isinstance(filters,dict) or any(k not in {'product','pool_sku','warehouse_department','unit'} for k in filters):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION','基线比较仅支持产品、规格、仓库部门和库存单位筛选。')
    bindings=_entity_bindings(request)
    filter_specs=[]
    for name,value in filters.items():
        _,value=_bound_value(bindings,name,value)
        column={'product':'goods_id','pool_sku':'goods_sku_id','warehouse_department':'whse_dept','unit':'unit'}[name]
        if name=='pool_sku':
            vv=value if isinstance(value,list) else [value]
            if any(isinstance(v,bool) or not (isinstance(v,int) and v>=0 or isinstance(v,str) and v.isascii() and v.isdecimal()) for v in vv):
                raise AnalysisQueryError('INVALID_PLAN','规格标识必须是明确整数。')
        filter_specs.append((column,value))
    valid="goods_id IS NOT NULL AND goods_sku_id IS NOT NULL AND whse_dept IS NOT NULL AND unit IN ('m','y','kg','Pcs')"
    for side in ('b','c'):
        clauses=[]
        for column,value in filter_specs:
            fparams=[];expr=_value_filter('r',column,value,fparams);params.extend(fparams)
            unknown=f'r.{column} IS NULL' if column!='unit' else "r.unit IS NULL OR r.unit NOT IN ('m','y','kg','Pcs')"
            clauses.append(f'({expr} OR ({unknown}))')
        if side=='c':clauses.append('membership<>0')
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        ctes.append(f"{side} AS (SELECT r.*,CASE WHEN {valid} THEN 1 ELSE 0 END AS key_valid,CASE WHEN {valid} THEN '' ELSE CONCAT('{side}:',source_id) END COLLATE utf8mb4_bin AS uncertainty_key FROM {side}_raw r{where})")
    keys=['goods_id','goods_sku_id','whse_dept','unit','uncertainty_key'];ks=','.join(keys)
    for side in ('b','c'):
        ctes.append(f"{side}g AS (SELECT {ks},MAX(goods_name) AS goods_name,MIN(key_valid) AS key_valid,SUM(CASE WHEN membership=1 THEN 1 ELSE 0 END) AS n,SUM(CASE WHEN membership=-1 THEN 1 ELSE 0 END) AS uncertain_membership_rows,ROUND(SUM(CASE WHEN membership=1 THEN qty ELSE NULL END),4) AS qty,ROUND(SUM(CASE WHEN membership=1 THEN rolls ELSE NULL END),4) AS rolls,SUM(CASE WHEN qty IS NULL THEN 1 ELSE 0 END) AS missing_qty,SUM(CASE WHEN membership=1 AND rolls IS NULL THEN 1 ELSE 0 END) AS missing_rolls,SUM(CASE WHEN slow_label IS NULL OR slow_label NOT IN ('deprice','discountable','handing') THEN 1 ELSE 0 END) AS unknown_class_rows,SUM(CASE WHEN key_valid=0 THEN 1 ELSE 0 END) AS unknown_key_rows,SUM(CASE WHEN unit IS NULL OR unit NOT IN ('m','y','kg','Pcs') THEN 1 ELSE 0 END) AS unknown_unit_rows,SUM(unknown_whitelist) AS unknown_whitelist_rows FROM {side} GROUP BY {ks})")
    ctes.append(f'keys_all AS (SELECT {ks} FROM bg UNION SELECT {ks} FROM cg)')
    def join(alias):return ' AND '.join(f'k.{key} <=> {alias}.{key}' for key in keys)
    def uncertain(side):
        conditions=[f'(u.{key} IS NULL OR u.{key}=k.{key})' for key in keys[:3]]
        conditions.append("(u.unit IS NULL OR u.unit NOT IN ('m','y','kg','Pcs') OR u.unit=k.unit)")
        return f"EXISTS (SELECT 1 FROM {side} u WHERE u.key_valid=0 AND {' AND '.join(conditions)})"
    ctes.append(f"paired AS (SELECT k.*,COALESCE(c.goods_name,b.goods_name) AS goods_name,COALESCE(b.n,0) AS opening_source_rows,COALESCE(c.n,0) AS closing_source_rows,b.qty AS oq,c.qty AS cq,b.rolls AS opening_rolls,c.rolls AS closing_rolls,COALESCE(b.missing_qty,0) AS opening_missing_quantity_rows,COALESCE(c.missing_qty,0) AS closing_missing_quantity_rows,COALESCE(b.missing_rolls,0) AS opening_missing_roll_rows,COALESCE(c.missing_rolls,0) AS closing_missing_roll_rows,COALESCE(b.unknown_class_rows,0) AS opening_unknown_class_rows,COALESCE(c.unknown_class_rows,0) AS closing_unknown_class_rows,COALESCE(c.uncertain_membership_rows,0) AS closing_uncertain_membership_rows,COALESCE(b.unknown_key_rows,0) AS opening_unknown_key_rows,COALESCE(c.unknown_key_rows,0) AS closing_unknown_key_rows,COALESCE(b.unknown_unit_rows,0) AS opening_unknown_unit_rows,COALESCE(c.unknown_unit_rows,0) AS closing_unknown_unit_rows,COALESCE(c.unknown_whitelist_rows,0) AS closing_unknown_whitelist_rows,CASE WHEN k.uncertainty_key<>'' OR {uncertain('b')} OR {uncertain('c')} THEN 1 ELSE 0 END AS identity_uncertain FROM keys_all k LEFT JOIN bg b ON {join('b')} LEFT JOIN cg c ON {join('c')})")
    ctes.append("classified AS (SELECT p.*,CASE WHEN identity_uncertain=1 OR closing_uncertain_membership_rows>0 THEN 'Unassessable' WHEN opening_source_rows=0 AND closing_source_rows>0 THEN 'New' WHEN opening_source_rows>0 AND closing_source_rows=0 THEN 'Exited' WHEN opening_missing_quantity_rows>0 OR closing_missing_quantity_rows>0 THEN 'Unassessable' WHEN ROUND(cq-oq,4)<0 THEN 'Reduced' WHEN ROUND(cq-oq,4)>0 THEN 'Increased' ELSE 'No Change' END AS pool_movement_state,CASE WHEN identity_uncertain=0 AND unit IN ('m','y','kg','Pcs') AND opening_missing_quantity_rows=0 THEN oq ELSE NULL END AS opening_quantity,CASE WHEN unit IN ('m','y','kg','Pcs') THEN oq ELSE NULL END AS opening_known_quantity,CASE WHEN identity_uncertain=0 AND unit IN ('m','y','kg','Pcs') AND closing_missing_quantity_rows=0 AND closing_uncertain_membership_rows=0 THEN cq ELSE NULL END AS closing_quantity,CASE WHEN unit IN ('m','y','kg','Pcs') THEN cq ELSE NULL END AS closing_known_quantity FROM paired p)")
    count_names=['new','exited','reduced','unchanged','increased','unassessable']
    counts=[f"SUM(CASE WHEN pool_movement_state='{state}' THEN 1 ELSE 0 END) AS {name}_group_count" for state,name in zip(states,count_names)]
    ctes.append('totals AS (SELECT COUNT(*) AS population_union_groups,'+','.join(c.replace(' AS ',' AS population_') for c in counts)+' FROM classified)')
    if mode=='groups':
        projection="goods_id,goods_name,goods_sku_id,whse_dept,unit,1 AS metric_value,pool_movement_state,opening_source_rows,closing_source_rows,opening_quantity,closing_quantity,opening_known_quantity,closing_known_quantity,opening_rolls AS opening_known_rolls,closing_rolls AS closing_known_rolls,CASE WHEN pool_movement_state IN ('Reduced','No Change','Increased') THEN ROUND(cq-oq,4) ELSE NULL END AS comparable_quantity_delta,CASE WHEN identity_uncertain=0 AND opening_missing_roll_rows=0 THEN opening_rolls ELSE NULL END AS opening_rolls,CASE WHEN identity_uncertain=0 AND closing_uncertain_membership_rows=0 AND closing_missing_roll_rows=0 THEN closing_rolls ELSE NULL END AS closing_rolls,opening_missing_quantity_rows,closing_missing_quantity_rows,opening_missing_roll_rows,closing_missing_roll_rows,opening_unknown_class_rows,closing_unknown_class_rows,closing_uncertain_membership_rows,opening_unknown_key_rows,closing_unknown_key_rows,opening_unknown_unit_rows,closing_unknown_unit_rows,closing_unknown_whitelist_rows,identity_uncertain"
        tail=''
        if movement is not None:tail=' WHERE pool_movement_state=%s';params.append(movement)
        ctes.append('display_rows AS (SELECT '+projection+',1 AS __matched_row_count FROM classified'+tail+')')
        outputs=['goods_id','goods_name','goods_sku_id','whse_dept','unit']
        ordering=' ORDER BY d.whse_dept,d.goods_id,d.goods_sku_id,d.unit'
    else:
        cols=','.join(grouping)
        aggregates=[f'{cols},COUNT(*) AS metric_value',*counts,
            'SUM(opening_known_quantity) AS opening_known_quantity','SUM(closing_known_quantity) AS closing_known_quantity','SUM(opening_rolls) AS opening_known_rolls','SUM(closing_rolls) AS closing_known_rolls','SUM(opening_source_rows) AS opening_source_rows','SUM(closing_source_rows) AS closing_source_rows',
            'COUNT(DISTINCT CASE WHEN opening_source_rows>0 THEN goods_id ELSE NULL END) AS opening_product_id_count','COUNT(DISTINCT CASE WHEN closing_source_rows>0 THEN goods_id ELSE NULL END) AS closing_product_id_count',
            'COUNT(DISTINCT CASE WHEN opening_source_rows>0 THEN goods_sku_id ELSE NULL END) AS opening_sku_id_count','COUNT(DISTINCT CASE WHEN closing_source_rows>0 THEN goods_sku_id ELSE NULL END) AS closing_sku_id_count',
            "CASE WHEN SUM(opening_source_rows)>0 AND SUM(CASE WHEN opening_source_rows>0 AND opening_quantity IS NULL THEN 1 ELSE 0 END)=0 THEN SUM(opening_quantity) ELSE NULL END AS opening_quantity",
            "CASE WHEN SUM(closing_source_rows)>0 AND SUM(closing_uncertain_membership_rows)=0 AND SUM(CASE WHEN closing_source_rows>0 AND closing_quantity IS NULL THEN 1 ELSE 0 END)=0 THEN SUM(closing_quantity) ELSE NULL END AS closing_quantity",
            "SUM(CASE WHEN pool_movement_state IN ('Reduced','No Change','Increased') THEN ROUND(cq-oq,4) ELSE NULL END) AS comparable_quantity_delta",
            'CASE WHEN SUM(identity_uncertain)=0 AND SUM(opening_missing_roll_rows)=0 THEN SUM(opening_rolls) ELSE NULL END AS opening_rolls',
            'CASE WHEN SUM(identity_uncertain)=0 AND SUM(closing_missing_roll_rows)=0 AND SUM(closing_uncertain_membership_rows)=0 THEN SUM(closing_rolls) ELSE NULL END AS closing_rolls',
        ]
        for k in ['opening_missing_quantity_rows','closing_missing_quantity_rows','opening_missing_roll_rows','closing_missing_roll_rows','opening_unknown_class_rows','closing_unknown_class_rows','closing_uncertain_membership_rows','opening_unknown_key_rows','closing_unknown_key_rows','opening_unknown_unit_rows','closing_unknown_unit_rows','closing_unknown_whitelist_rows','identity_uncertain']:aggregates.append(f'SUM({k}) AS {k}')
        ctes.append('display_rows AS (SELECT '+','.join(aggregates)+',COUNT(*) AS __matched_row_count FROM classified GROUP BY '+cols+')')
        outputs=grouping;ordering=' ORDER BY '+','.join('d.'+k for k in grouping)
    if request.get('order_by') is not None:raise AnalysisQueryError('INVALID_PLAN','基线比较使用稳定键排序，不跨单位按数量排名。')
    sql='WITH '+',\n'.join(ctes)+' SELECT d.*,m.*,clock.*,totals.*,0 AS missing_value_count,COALESCE(d.metric_value,0) AS known_value_count,1 AS value_coverage_rate FROM meta m CROSS JOIN clock CROSS JOIN totals LEFT JOIN display_rows d ON TRUE'+ordering+' LIMIT %s'
    params.append(limit+1)
    return sql,params,{'metric':request.get('metric'),'dataset':None,'source_datasets':[base_table,current_table],'dimension_outputs':outputs,'effective_dimensions':effective_dimensions,'filters':filters,'time_range':{'source':'frozen_baseline_to_current'},'warnings':[metric.get('answer_note','')],'_validate_frozen_pool':True}


def validate_frozen_pool_rows(rows):
    """Check global baseline integrity even when the user's display is empty or truncated."""
    if not rows:raise AnalysisQueryError('BASELINE_EVIDENCE_MISSING','没有返回可核验的基线元信息。')
    first = rows[0]
    metadata_keys = ['baseline_week','baseline_frozen_at','closing_read_at','closing_utc_at','observed_clock_offset_seconds','__baseline_rows','__baseline_ids','__baseline_timed_rows','__baseline_times','__baseline_bad_source']
    for row in rows:
        if any(row.get(k) != first.get(k) for k in metadata_keys):
            raise AnalysisQueryError('BASELINE_EVIDENCE_MISSING','基线元信息在返回行间不一致。')
        try:
            count=int(row['__baseline_rows']);ids=int(row['__baseline_ids']);timed=int(row['__baseline_timed_rows']);times=int(row['__baseline_times']);bad=int(row['__baseline_bad_source'])
        except (KeyError,TypeError,ValueError):raise AnalysisQueryError('BASELINE_EVIDENCE_MISSING','基线元信息不完整。')
        if row.get('__baseline_bad_keys') is not None and int(row['__baseline_bad_keys']) != 0:
            raise AnalysisQueryError('BASELINE_IDENTITY_INCOMPLETE','基线产品、规格、部门或库存单位不完整，不能定义可靠净出库范围。')
        if count==0:raise AnalysisQueryError('BASELINE_NOT_FOUND','没有符合所选周范围的现存基线；不会为查询自动冻结。')
        if count<0 or ids!=count or bad!=0:raise AnalysisQueryError('BASELINE_INCOMPLETE','基线来源记录重复、缺失或版本不兼容，不能可靠比较。')
        if timed!=count or times!=1:raise AnalysisQueryError('BASELINE_TIME_AMBIGUOUS','基线缺少唯一记录冻结时间，不能合并多个时点。')
        try:
            start=datetime.fromisoformat(str(row['baseline_frozen_at']));end=datetime.fromisoformat(str(row['closing_read_at']))
            if start.tzinfo is not None or end.tzinfo is not None or start>end:raise ValueError()
        except (KeyError,TypeError,ValueError):raise AnalysisQueryError('BASELINE_TIME_INVALID','记录冻结时间与本次库端读取时间不兼容。')
        if row.get('__matched_row_count') is None:row['__matched_row_count']=0
    metadata = {'source':'frozen_baseline_to_current','baseline_week':first['baseline_week'],'frozen_at':first['baseline_frozen_at'],'read_at':first['closing_read_at'],'read_utc_at':first['closing_utc_at'],'observed_db_utc_offset_seconds':first['observed_clock_offset_seconds']}
    try:
        w=metadata['baseline_week'];date.fromisocalendar(int(w[:4]),int(w[6:]),1)
        if len(w)!=8 or w[4:6]!='-W':raise ValueError()
    except (ValueError,TypeError):raise AnalysisQueryError('BASELINE_WEEK_INVALID','记录基线周标签无效。')
    for row in rows:
        for key in list(row):
            if key.startswith('__baseline_'):row.pop(key)
    return metadata


def _frozen_pool_net_outbound_query(request, metric, datasets_contract, semantics, limit, *, observed_on=None):
    """Recorded flows in unique frozen business keys; never frozen physical-batch disposal."""
    import re
    if any(request.get(k) is not None for k in ('time_range', 'calendar_month', 'comparison', 'time_bucket', 'movement_state', 'order_by')):
        raise AnalysisQueryError('INVALID_PLAN', '基线产品范围净出库仅支持记录冻结时间至本次读取，按稳定键排序。')
    week = request.get('baseline_week')
    iso = (observed_on or _business_today()).isocalendar()
    current_week = f'{iso.year:04d}-W{iso.week:02d}'
    if week is not None:
        try:
            if not isinstance(week, str) or not re.fullmatch(r'[0-9]{4}-W[0-9]{2}', week): raise ValueError()
            date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
            if week > current_week: raise ValueError()
        except ValueError:
            raise AnalysisQueryError('INVALID_PLAN', '基线周必须是当前或过去的合法周标签。')
    allowed = {'product': 'goods_id', 'pool_sku': 'goods_sku_id', 'warehouse_department': 'whse_dept', 'unit': 'unit', 'salesperson': 'sales_id'}
    chosen = request.get('dimensions') or ['unit']
    if not set(chosen) <= set(allowed) or 'unit' not in chosen:
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION', '净出库必须按库存单位分别汇总，可附加产品、规格、仓库部门和流水销售归属。')
    grouping = [allowed[k] for k in chosen]
    by_sales = 'salesperson' in chosen
    sales_filter = filters_sales = None
    filters = request.get('metric_filters') or {}
    if not isinstance(filters, dict) or not set(filters) <= set(allowed):
        raise AnalysisQueryError('UNSUPPORTED_DIMENSION', '仅支持冻结范围筛选及流水销售身份筛选。')
    tables = metric.get('flow_sources') or {}
    if set(tables) != {'outbound','returns','sales','warehouses'} or metric.get('document_scope') != 'legacy_ht_suffix_or_bulk':
        raise AnalysisQueryError('CONTRACT_UNAVAILABLE', '净出库来源或已确认单据范围缺失。')
    base = metric.get('table')
    base_ds = _dataset(base, datasets_contract)
    for field in ('week_label','source_row_id','frozen_at','baseline_version','source_table','goods_id','goods_sku_id','whse_dept','source_unit'):
        _approved(field, base_ds)
    source_columns = {
        'outbound': ['goods_id','goods_sku_id','whse_dept','unit','goods_num','piece_num','delivery_time','whse_id','bill_type','is_inner_cus','sale_bill_goods_id'],
        'returns': ['goods_id','goods_sku_id','whse_dept','unit','return_goods_num','return_piece_num','statement_time','in_whse_id','sale_bill_type','is_inner_cus','status','complnt_type','channel_type'],
        'sales': ['goods_detail_id','bill_status'], 'warehouses': ['whse_id','dept_name'],
    }
    for name, columns in source_columns.items():
        ds = _dataset(tables[name], datasets_contract)
        for field in columns: _approved(field, ds)
        if name in {'outbound','returns'}:
            for field in ('sales_id','sales_name'): _approved(field,ds)
    qt = {k: _quote_table(v) for k,v in tables.items()}
    qb = _quote_table(base)
    params = [week if week is not None else current_week]
    choice = '%s' if week is not None else f"(SELECT MAX(week_label) FROM {qb} WHERE week_label<=%s AND week_label REGEXP '^[0-9]{{4}}-W[0-9]{{2}}$')"
    unit = lambda col: f"(CASE WHEN LOWER(TRIM({col}))='m' THEN 'm' ELSE NULLIF(TRIM({col}),'') END) COLLATE utf8mb4_bin"
    ctes = [f"clock AS (SELECT {choice} AS baseline_week,NOW(6) AS closing_read_at,UTC_TIMESTAMP(6) AS closing_utc_at,TIMESTAMPDIFF(SECOND,UTC_TIMESTAMP(6),NOW(6)) AS observed_clock_offset_seconds)"]
    ctes.append(f"base_raw AS (SELECT week_label,source_row_id,frozen_at,baseline_version,source_table,goods_id,goods_sku_id,whse_dept,{unit('source_unit')} AS normalized_unit FROM {qb} WHERE week_label=(SELECT baseline_week FROM clock))")
    ctes.append("meta AS (SELECT COUNT(*) AS __baseline_rows,COUNT(DISTINCT source_row_id) AS __baseline_ids,COUNT(frozen_at) AS __baseline_timed_rows,COUNT(DISTINCT frozen_at) AS __baseline_times,MIN(frozen_at) AS baseline_frozen_at,COALESCE(SUM(CASE WHEN baseline_version=2 AND source_table=%s THEN 0 ELSE 1 END),0) AS __baseline_bad_source,COALESCE(SUM(CASE WHEN goods_id IS NOT NULL AND goods_sku_id IS NOT NULL AND NULLIF(whse_dept,'') IS NOT NULL AND normalized_unit IN ('m','y','kg','Pcs') THEN 0 ELSE 1 END),0) AS __baseline_bad_keys FROM base_raw)")
    params.append(metric.get('baseline_source_table'))
    ctes.append("base_keys AS (SELECT DISTINCT goods_id,goods_sku_id,whse_dept COLLATE utf8mb4_bin AS whse_dept,normalized_unit AS unit FROM base_raw)")
    filter_sql = []
    bindings = _entity_bindings(request)
    for key, value in filters.items():
        binding, value = _bound_value(bindings, key, value)
        if key == 'salesperson':
            definition = semantics['dimensions']['salesperson']
            # The public pipeline compiles a name-based draft before entity resolution;
            # final execution requires the existing entity_exact binding validation.
            sales_filter_column = _dimension_filter(definition,binding)
            filters_sales = value
            continue
        if key == 'pool_sku':
            values = value if isinstance(value,list) else [value]
            if any(isinstance(v,bool) or not (isinstance(v,int) and v>=0 or isinstance(v,str) and v.isascii() and v.isdecimal()) for v in values):
                raise AnalysisQueryError('INVALID_PLAN','规格标识必须是明确整数。')
        filter_sql.append(_value_filter('k', allowed[key], value, params))
    ctes.append('keys_b AS (SELECT * FROM base_keys k'+(' WHERE '+' AND '.join(filter_sql) if filter_sql else '')+')')
    for side in ('outbound','returns'):
        outgoing = side == 'outbound'
        time_field,whse,bill,qty,rolls = ('delivery_time','whse_id','bill_type','goods_num','piece_num') if outgoing else ('statement_time','in_whse_id','sale_bill_type','return_goods_num','return_piece_num')
        # Keep the legacy predicate on source collation: HT is unrestricted, including NULL types.
        legacy = f"(f.whse_dept LIKE %s OR f.{bill}=%s)"
        params.extend(['%-HT','bulk'])
        valid = f"(SELECT CASE WHEN COUNT(*)=1 AND COUNT(bill_status)=1 THEN MAX(bill_status)=6 ELSE NULL END FROM {qt['sales']} s WHERE s.goods_detail_id=f.sale_bill_goods_id)" if outgoing else '(f.status=4 AND f.complnt_type=1 AND f.channel_type=1)'
        predicate = f"(f.is_inner_cus='n' AND {valid})"
        candidate = "EXISTS(SELECT 1 FROM keys_b k WHERE (f.goods_id IS NULL OR f.goods_id=k.goods_id) AND (f.goods_sku_id IS NULL OR f.goods_sku_id=k.goods_sku_id))"
        sales_condition = ''
        if filters_sales is not None:
            filter_params = []
            sales_filter = _value_filter('f',sales_filter_column,filters_sales,filter_params)
            # The SQL placeholders occur after the legacy predicate in SELECT.
            sales_condition = f' AND ({sales_filter} OR f.sales_id IS NULL)'
        warehouse_ok = f"(SELECT CASE WHEN COUNT(*)=1 AND COUNT(dept_name)=1 THEN MAX(dept_name)=f.whse_dept ELSE NULL END FROM {qt['warehouses']} w WHERE w.whse_id=f.{whse})"
        ctes.append(f"{side}_raw AS (SELECT f.sales_id,NULLIF(f.sales_name,'') COLLATE utf8mb4_bin AS sales_name,f.goods_id,f.goods_sku_id,NULLIF(f.whse_dept,'') COLLATE utf8mb4_bin AS whse_dept,{unit('f.unit')} AS unit,f.{qty} AS qty,f.{rolls} AS rolls,f.{time_field} AS event_at,{legacy} AS document_ok,{predicate} AS valid_ok,{warehouse_ok} AS warehouse_ok,CASE WHEN f.{bill} IS NULL OR f.{bill} NOT IN ('bulk','sq') THEN 1 ELSE 0 END AS unknown_document FROM {qt[side]} f WHERE {candidate} AND (f.{time_field} IS NULL OR (f.{time_field}>=(SELECT baseline_frozen_at FROM meta) AND f.{time_field}<(SELECT closing_read_at FROM clock))){sales_condition})")
        if filters_sales is not None: params.extend(filter_params)
        match = ' AND '.join(f'k.{key}=r.{key}' for key in ('goods_id','goods_sku_id','whse_dept','unit'))
        # Reliable different keys are out of scope, unknown keys cannot be silently excluded.
        unknown = "goods_id IS NULL OR goods_sku_id IS NULL OR whse_dept IS NULL OR unit IS NULL OR unit NOT IN ('m','y','kg','Pcs') OR warehouse_ok IS NULL OR warehouse_ok=0 OR document_ok IS NULL OR valid_ok IS NULL OR event_at IS NULL"
        if filters_sales is not None: unknown += ' OR sales_id IS NULL'
        ctes.append(f"{side}_classified AS (SELECT r.*,CASE WHEN valid_ok=0 OR document_ok=0 THEN 'excluded' WHEN {unknown} THEN 'unknown' WHEN EXISTS(SELECT 1 FROM keys_b k WHERE {match}) THEN 'matched' ELSE 'unmatched' END AS match_state FROM {side}_raw r)")
        ctes.append(f"{side}_totals AS (SELECT COUNT(*) AS {side}_candidate_rows,COALESCE(SUM(CASE WHEN match_state='matched' THEN 1 ELSE 0 END),0) AS {side}_matched_rows,COALESCE(SUM(CASE WHEN match_state='unmatched' THEN 1 ELSE 0 END),0) AS {side}_unmatched_rows,COALESCE(SUM(CASE WHEN match_state='excluded' THEN 1 ELSE 0 END),0) AS {side}_excluded_rows,COALESCE(SUM(CASE WHEN match_state='unknown' THEN 1 ELSE 0 END),0) AS {side}_unknown_rows,COALESCE(SUM(unknown_document),0) AS {side}_unknown_document_rows,COALESCE(SUM(CASE WHEN event_at IS NULL THEN 1 ELSE 0 END),0) AS {side}_missing_time_rows,COALESCE(SUM(CASE WHEN match_state='matched' AND sales_id IS NULL THEN 1 ELSE 0 END),0) AS {side}_unattributed_sales_rows FROM {side}_classified)")
        sales_key = ',sales_id' if by_sales else ''
        ctes.append(f"{side}_agg AS (SELECT goods_id,goods_sku_id,whse_dept,unit{sales_key},COUNT(*) AS n,SUM(qty) AS qty,SUM(rolls) AS rolls,SUM(CASE WHEN qty IS NULL THEN 1 ELSE 0 END) AS missing_qty,SUM(CASE WHEN rolls IS NULL THEN 1 ELSE 0 END) AS missing_rolls FROM {side}_classified WHERE match_state='matched' GROUP BY goods_id,goods_sku_id,whse_dept,unit{sales_key})")
    key_columns = ['goods_id','goods_sku_id','whse_dept','unit'] + (['sales_id'] if by_sales else [])
    if by_sales:
        # Only observed flow identities create sales groups; no employee roster or task assignment.
        columns = ','.join(key_columns)
        ctes.append(f'flow_keys AS (SELECT {columns} FROM outbound_agg UNION SELECT {columns} FROM returns_agg)')
        ctes.append("sales_names AS (SELECT sales_id,sales_name FROM outbound_classified WHERE match_state='matched' UNION ALL SELECT sales_id,sales_name FROM returns_classified WHERE match_state='matched')")
        ctes.append("sales_labels AS (SELECT sales_id,CASE WHEN COUNT(DISTINCT sales_name)=1 THEN MAX(sales_name) ELSE NULL END AS sales_name,COUNT(DISTINCT sales_name) AS sales_name_variant_count,SUM(CASE WHEN sales_name IS NULL THEN 1 ELSE 0 END) AS sales_missing_name_rows FROM sales_names GROUP BY sales_id)")
    key_table = 'flow_keys' if by_sales else 'keys_b'
    join = lambda alias: ' AND '.join(f'k.{key} <=> {alias}.{key}' for key in key_columns)
    # Empty recorded sets contribute zero. Actual NULL measures remain counted as missing.
    ctes.append(f"paired AS (SELECT k.*,COALESCE(o.n,0) AS gross_flow_rows,COALESCE(r.n,0) AS return_flow_rows,COALESCE(o.qty,0) AS gross_known_quantity,COALESCE(r.qty,0) AS return_known_quantity,COALESCE(o.rolls,0) AS gross_known_rolls,COALESCE(r.rolls,0) AS return_known_rolls,COALESCE(o.missing_qty,0) AS gross_missing_quantity_rows,COALESCE(r.missing_qty,0) AS return_missing_quantity_rows,COALESCE(o.missing_rolls,0) AS gross_missing_roll_rows,COALESCE(r.missing_rolls,0) AS return_missing_roll_rows FROM {key_table} k LEFT JOIN outbound_agg o ON {join('o')} LEFT JOIN returns_agg r ON {join('r')})")
    sums = ['gross_flow_rows','return_flow_rows','gross_known_quantity','return_known_quantity','gross_known_rolls','return_known_rolls','gross_missing_quantity_rows','return_missing_quantity_rows','gross_missing_roll_rows','return_missing_roll_rows']
    aggregates = ','.join(f'SUM({field}) AS {field}' for field in sums)
    baseline_count = 'NULL' if by_sales else 'COUNT(*)'
    ctes.append('grouped AS (SELECT '+','.join(grouping)+f',{baseline_count} AS baseline_scope_groups,COUNT(*) AS grouped_key_count,SUM(CASE WHEN gross_flow_rows+return_flow_rows>0 THEN 1 ELSE 0 END) AS matched_product_groups,'+aggregates+' FROM paired GROUP BY '+','.join(grouping)+')')
    ctes.append('unit_totals AS (SELECT unit,'+aggregates+' FROM paired GROUP BY unit)')
    ctes.append('population AS (SELECT COUNT(*) AS population_display_groups FROM grouped)')
    complete = 'outbound_unknown_rows=0 AND returns_unknown_rows=0 AND g.gross_missing_quantity_rows=0 AND g.return_missing_quantity_rows=0'
    complete_rolls = 'outbound_unknown_rows=0 AND returns_unknown_rows=0 AND g.gross_missing_roll_rows=0 AND g.return_missing_roll_rows=0'
    known_net = 'g.gross_known_quantity-g.return_known_quantity'
    known_net_rolls = 'g.gross_known_rolls-g.return_known_rolls'
    net = f'CASE WHEN {complete} THEN {known_net} ELSE NULL END'
    unit_fields = ','.join(f'u.{field} AS unit_{field}' for field in sums)
    unit_complete = 'outbound_unknown_rows=0 AND returns_unknown_rows=0 AND u.gross_missing_quantity_rows=0 AND u.return_missing_quantity_rows=0'
    label_fields = ',sl.sales_name,sl.sales_name_variant_count,sl.sales_missing_name_rows' if by_sales else ''
    label_join = ' LEFT JOIN sales_labels sl ON sl.sales_id <=> g.sales_id' if by_sales else ''
    sql = 'WITH '+',\n'.join(ctes)+f" SELECT g.*{label_fields},m.*,clock.*,ot.*,rt.*,population.*,{unit_fields},(SELECT COUNT(*) FROM keys_b kb WHERE kb.unit=g.unit) AS unit_baseline_scope_groups,(SELECT COUNT(*) FROM grouped gg WHERE gg.unit=g.unit) AS unit_display_groups,{net} AS metric_value,{known_net} AS known_subset_value,{known_net_rolls} AS known_net_rolls,CASE WHEN {complete_rolls} THEN {known_net_rolls} ELSE NULL END AS net_rolls,CASE WHEN outbound_unknown_rows=0 AND g.gross_missing_quantity_rows=0 THEN g.gross_known_quantity ELSE NULL END AS gross_quantity,CASE WHEN returns_unknown_rows=0 AND g.return_missing_quantity_rows=0 THEN g.return_known_quantity ELSE NULL END AS return_quantity,CASE WHEN outbound_unknown_rows=0 AND g.gross_missing_roll_rows=0 THEN g.gross_known_rolls ELSE NULL END AS gross_rolls,CASE WHEN returns_unknown_rows=0 AND g.return_missing_roll_rows=0 THEN g.return_known_rolls ELSE NULL END AS return_rolls,CASE WHEN {unit_complete} THEN u.gross_known_quantity-u.return_known_quantity ELSE NULL END AS unit_net_quantity,u.gross_known_quantity-u.return_known_quantity AS unit_known_net_quantity,CASE WHEN NOT({complete}) THEN 'partial_unknown' WHEN g.gross_flow_rows=0 AND g.return_flow_rows=0 THEN 'no_recorded_flow' WHEN g.gross_flow_rows=0 OR g.return_flow_rows=0 THEN 'one_sided_recorded_flow' ELSE 'both_sides_recorded' END AS net_flow_state,COALESCE(g.grouped_key_count,0) AS __matched_row_count,COALESCE(g.gross_missing_quantity_rows+g.return_missing_quantity_rows,0)+outbound_unknown_rows+returns_unknown_rows AS missing_value_count,COALESCE(g.gross_flow_rows+g.return_flow_rows,0) AS known_value_count FROM meta m CROSS JOIN clock CROSS JOIN outbound_totals ot CROSS JOIN returns_totals rt CROSS JOIN population LEFT JOIN grouped g ON TRUE LEFT JOIN unit_totals u ON u.unit=g.unit"+label_join+' ORDER BY '+','.join('g.'+k for k in grouping)+' LIMIT %s'
    params.append(limit+1)
    outputs = grouping + (['sales_name'] if by_sales else [])
    return sql,params,{'metric':request.get('metric'),'dataset':None,'source_datasets':[base,*tables.values()],'dimension_outputs':outputs,'effective_dimensions':chosen,'filters':filters,'time_range':{'source':'frozen_baseline_to_current'},'warnings':[metric.get('answer_note','')],'_validate_frozen_pool':True}


def build_analytical_metric_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: date | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    query_observed_on = observed_on or _business_today()
    kind = metric.get("query_kind")
    _ensure_available(metric)
    if kind == "pattern_matching":
        from .pattern_queries import build_pattern_query
        return build_pattern_query(request, metric, datasets_contract, semantics, limit, observed_on=query_observed_on)
    if kind == "frozen_pool_net_outbound":
        return _frozen_pool_net_outbound_query(request, metric, datasets_contract, semantics, limit, observed_on=query_observed_on)
    if kind == "frozen_pool_comparison":
        return _frozen_pool_comparison_query(request, metric, datasets_contract, semantics, limit, observed_on=query_observed_on)
    if kind == "registered_slow_pool":
        return _registered_slow_pool_query(request, metric, datasets_contract, semantics, limit, observed_on=query_observed_on)
    if kind == "settlement_days":
        return _settlement_query(
            request,
            metric,
            datasets_contract,
            semantics,
            limit,
            observed_on=query_observed_on,
        )
    if kind == "formal_dso":
        return _formal_dso_query(
            request,
            metric,
            datasets_contract,
            limit,
            observed_on=query_observed_on,
        )
    if kind == "paired_amounts":
        return _paired_amounts_query(
            request,
            metric,
            datasets_contract,
            limit,
            observed_on=query_observed_on,
        )
    if kind == "allocated_amount":
        return _allocated_amount_query(
            request,
            metric,
            datasets_contract,
            semantics,
            limit,
            observed_on=query_observed_on,
        )
    if kind == "target_completion":
        return _target_completion_query(
            request,
            metric,
            datasets_contract,
            limit,
            observed_on=query_observed_on,
        )
    if kind == "inventory_turnover_days":
        return _inventory_turnover_query(
            request,
            metric,
            datasets_contract,
            semantics,
            limit,
            observed_on=query_observed_on,
        )
    raise AnalysisQueryError("CONTRACT_UNAVAILABLE", "分析指标类型不受支持。")
