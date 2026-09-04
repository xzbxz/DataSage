"""Semantics-driven analytical query builders for DataSage Expert.

The ordinary metric builder handles one fact and one aggregation.  This module
owns the small set of analytical shapes that genuinely require a different
grain: document-level settlement behavior, formal DSO, and two independently
aggregated facts compared at a governed dimension.
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
        bounds_sql = (
            "latest_complete AS ("
            f"SELECT MAX(CASE WHEN {_quote_column(valid_measure)} IS NOT NULL "
            f"AND {_quote_column(valid_measure)} <> 0 "
            f"AND {_quote_column('bill_date')} < %s "
            "THEN `bill_date` END) AS `operating_end_month` FROM {table}), "
            "bounds AS (SELECT "
            f"DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(`operating_end_month`, '-01'), '%%Y-%%m-%%d'), "
            f"INTERVAL {default_months - 1} MONTH), '{_MYSQL_MONTH_FORMAT}') AS `operating_start_month`, "
            "`operating_end_month`, "
            f"DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(`operating_end_month`, '-01'), '%%Y-%%m-%%d'), "
            f"INTERVAL {default_months} MONTH), '{_MYSQL_MONTH_FORMAT}') AS `opening_month` "
            "FROM latest_complete)"
        )
        return bounds_sql, [cutoff_month], {
            "source": "latest_complete_accounting_months",
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
    dimensions = semantics.get("dimensions")
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
global_months AS (
  SELECT DISTINCT s.{_quote_column(period_measure)} AS `snapshot_month`
  FROM {quoted_table} AS s CROSS JOIN bounds AS b
  WHERE s.{_quote_column(period_measure)} BETWEEN b.`opening_month` AND b.`operating_end_month`
    AND s.{_quote_column(cost_measure)} IS NOT NULL
    AND s.{_quote_column(cost_measure)} <> 0
),
monthly_data AS (
  SELECT {dimension_prefix}s.{_quote_column(period_measure)} AS `snapshot_month`,
         COALESCE(SUM(s.{_quote_column(cost_measure)}), 0) AS `inventory_cost_rmb`,
         COALESCE(SUM(s.{_quote_column(ddp_measure)}), 0) AS `inventory_ddp_rmb`,
         COALESCE(SUM(s.{_quote_column(net_measure)}), 0) AS `net_delivery_rmb`,
         SUM(CASE WHEN COALESCE(s.{_quote_column(cost_measure)}, 0) <> 0
                    OR COALESCE(s.{_quote_column(ddp_measure)}, 0) <> 0
                    OR COALESCE(s.{_quote_column(net_measure)}, 0) <> 0
                  THEN 1 ELSE 0 END) AS `active_rows`,
         COUNT(*) AS `source_rows`
  FROM {quoted_table} AS s CROSS JOIN bounds AS b
  WHERE s.{_quote_column(period_measure)} BETWEEN b.`opening_month` AND b.`operating_end_month`{where_sql}
  GROUP BY s.{_quote_column(period_measure)}{monthly_group}
),
entities AS (
  SELECT {entity_prefix}MIN(CASE WHEN `active_rows` > 0
         THEN `snapshot_month` END) AS `first_active_month`
  FROM monthly_data{entity_group_sql}
  HAVING `first_active_month` IS NOT NULL
),
entity_bounds AS (
  SELECT {entity_prefix}GREATEST(b.`operating_start_month`, e.`first_active_month`) AS `effective_start_month`,
         DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(GREATEST(b.`operating_start_month`, e.`first_active_month`), '-01'), '{day_format}'), INTERVAL 1 MONTH), '{_MYSQL_MONTH_FORMAT}') AS `effective_opening_month`,
         b.`operating_end_month`
  FROM entities AS e CROSS JOIN bounds AS b
),
matrix AS (
  SELECT {matrix_dimension_prefix}eb.`effective_start_month`, eb.`effective_opening_month`, eb.`operating_end_month`,
         em.`snapshot_month`,
         COALESCE(md.`inventory_cost_rmb`, 0) AS `inventory_cost_rmb`,
         COALESCE(md.`inventory_ddp_rmb`, 0) AS `inventory_ddp_rmb`,
         COALESCE(md.`net_delivery_rmb`, 0) AS `net_delivery_rmb`,
         COALESCE(md.`source_rows`, 0) AS `source_rows`,
         CASE WHEN gm.`snapshot_month` IS NULL THEN 0 ELSE 1 END AS `global_snapshot_present`
  FROM entity_bounds AS eb
  JOIN expected_months AS em ON em.`snapshot_month` BETWEEN eb.`effective_opening_month` AND eb.`operating_end_month`
  LEFT JOIN monthly_data AS md ON md.`snapshot_month` = em.`snapshot_month`{dimension_join}
  LEFT JOIN global_months AS gm ON gm.`snapshot_month` = em.`snapshot_month`
),
summary AS (
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
         SUM(m.`global_snapshot_present`) AS `actual_snapshot_count`,
         SUM(m.`source_rows`) AS `entity_source_hit_count`,
         SUM(CASE WHEN m.`snapshot_month` IN (m.`effective_opening_month`, m.`operating_end_month`)
             THEN m.`inventory_cost_rmb` / 2 ELSE m.`inventory_cost_rmb` END) /
           (TIMESTAMPDIFF(MONTH,
             STR_TO_DATE(CONCAT(MIN(m.`effective_start_month`), '-01'), '{day_format}'),
             STR_TO_DATE(CONCAT(MIN(m.`operating_end_month`), '-01'), '{day_format}')) + 1) AS `avg_inventory_cost_rmb`,
         SUM(CASE WHEN m.`snapshot_month` IN (m.`effective_opening_month`, m.`operating_end_month`)
             THEN m.`inventory_ddp_rmb` / 2 ELSE m.`inventory_ddp_rmb` END) /
           (TIMESTAMPDIFF(MONTH,
             STR_TO_DATE(CONCAT(MIN(m.`effective_start_month`), '-01'), '{day_format}'),
             STR_TO_DATE(CONCAT(MIN(m.`operating_end_month`), '-01'), '{day_format}')) + 1) AS `avg_inventory_ddp_rmb`,
         SUM(CASE WHEN m.`snapshot_month` >= m.`effective_start_month` THEN m.`net_delivery_rmb` ELSE 0 END) AS `net_delivery_rmb`
  FROM matrix AS m{summary_group}
),
turnover_values AS (
  SELECT *,
    CASE WHEN `actual_snapshot_count` < `expected_snapshot_count` OR `net_delivery_rmb` <= 0 OR `avg_inventory_cost_rmb` <= 0
      THEN NULL ELSE `avg_inventory_cost_rmb` * `period_natural_days` / `net_delivery_rmb` END AS `cost_turnover_days`,
    CASE WHEN `actual_snapshot_count` < `expected_snapshot_count` OR `net_delivery_rmb` <= 0 OR `avg_inventory_ddp_rmb` <= 0
      THEN NULL ELSE `avg_inventory_ddp_rmb` * `period_natural_days` / `net_delivery_rmb` END AS `ddp_turnover_days`
  FROM summary
)
SELECT {final_prefix}`cost_turnover_days` AS `metric_value`, `cost_turnover_days`, `ddp_turnover_days`,
       `avg_inventory_cost_rmb`, `avg_inventory_ddp_rmb`, `net_delivery_rmb`, `period_natural_days`,
       `effective_operating_months`, `expected_snapshot_count`, `actual_snapshot_count`,
       `entity_source_hit_count`, `effective_opening_month`, `effective_start_month`, `operating_end_month`,
       CASE WHEN `actual_snapshot_count` < `expected_snapshot_count` THEN 'missing_global_snapshot'
            WHEN `net_delivery_rmb` <= 0 THEN 'non_positive_net_delivery'
            WHEN `avg_inventory_cost_rmb` <= 0 THEN 'non_positive_average_cost'
            ELSE 'available' END AS `cost_turnover_state`,
       CASE WHEN `actual_snapshot_count` < `expected_snapshot_count` THEN 'missing_global_snapshot'
            WHEN `net_delivery_rmb` <= 0 THEN 'non_positive_net_delivery'
            WHEN `avg_inventory_ddp_rmb` <= 0 THEN 'non_positive_average_ddp'
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
    dimensions = semantics.get("dimensions") or {}
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
    ])
    per_bill = (
        f"SELECT {inner_select} FROM {_quote_table(table)} AS {_quote_column('f')}"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY " + ", ".join(inner_group)
    )
    outer_dimensions = [_quote_column(output) for _, output in selected_columns]
    quality_columns = (
        "SUM(settlement_days < 0) AS excluded_negative_bill_count, "
        "SUM(has_open_balance = 1) AS excluded_open_balance_bill_count"
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

    eligible = "SELECT * FROM per_bill WHERE settlement_days >= 0 AND has_open_balance = 0"
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
    select_parts.extend([
        f"{value_sql} AS {_quote_column('metric_value')}",
        f"COUNT(e.document_id) AS {_quote_column('sample_bill_count')}",
        f"COALESCE(q.{_quote_column('excluded_negative_bill_count')}, 0) "
        f"AS {_quote_column('excluded_negative_bill_count')}",
        f"COALESCE(q.{_quote_column('excluded_open_balance_bill_count')}, 0) "
        f"AS {_quote_column('excluded_open_balance_bill_count')}",
        f"(COUNT(e.document_id) + "
        f"COALESCE(q.{_quote_column('excluded_negative_bill_count')}, 0) + "
        f"COALESCE(q.{_quote_column('excluded_open_balance_bill_count')}, 0)) "
        f"AS {_quote_column('__matched_row_count')}",
    ])
    sql = (
        f"WITH per_bill AS ({per_bill}), eligible AS ({eligible}), quality AS ({quality}) "
        f"SELECT {', '.join(select_parts)} FROM {quality_from}"
    )
    if group_parts:
        sql += " GROUP BY " + ", ".join([*group_parts, "q.excluded_negative_bill_count", "q.excluded_open_balance_bill_count"])
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
        f"SUM({_qualified('d', debt_measure)}) AS monthly_debt_rmb",
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
        f"THEN SUM(CASE WHEN bill_month IN (%s, %s) THEN monthly_debt_rmb / 2 ELSE monthly_debt_rmb END) / {period_months} "
        "ELSE NULL END AS average_net_debt_rmb",
        "COUNT(DISTINCT bill_month) AS snapshot_month_count",
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
        f"SUM({_qualified('s', delivery_measure)}) AS monthly_delivery_rmb",
    ]
    delivery_monthly = (
        f"SELECT {', '.join(delivery_monthly_select)} "
        f"FROM {_quote_table(delivery_table)} AS s WHERE {' AND '.join(delivery_where)} GROUP BY {', '.join(delivery_monthly_group)}"
    )
    delivery_agg_keys = [f"{_quote_column(alias)}" for _, alias in delivery_keys]
    delivery_agg = (
        f"SELECT {', '.join([*delivery_agg_keys, 'SUM(monthly_delivery_rmb) AS delivery_amount_rmb', 'SUM(monthly_delivery_rmb > 0) AS effective_month_count'])} FROM delivery_monthly"
        + (" GROUP BY " + ", ".join(delivery_agg_keys) if delivery_agg_keys else "")
    )
    join = " AND ".join(
        f"a.{_quote_column(debt_alias)} <=> v.{_quote_column(delivery_alias)}"
        for (_, debt_alias), (_, delivery_alias) in zip(debt_keys, delivery_keys)
    ) or "1 = 1"
    output_dimensions = [f"a.{_quote_column(alias)}" for _, alias in debt_outputs]
    select = [
        *output_dimensions,
        f"CASE WHEN COALESCE(v.delivery_amount_rmb, 0) > 0 THEN a.average_net_debt_rmb * {period_days} / v.delivery_amount_rmb ELSE NULL END AS metric_value",
        "a.average_net_debt_rmb",
        "COALESCE(v.delivery_amount_rmb, 0) AS delivery_amount_rmb",
        f"{period_days} AS period_natural_days",
        "a.snapshot_month_count",
        "COALESCE(v.effective_month_count, 0) AS effective_month_count",
        "a.snapshot_month_count + COALESCE(v.effective_month_count, 0) AS `__matched_row_count`",
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
    select = [
        *output_dimensions,
        "COALESCE(l.left_amount, 0) - COALESCE(r.right_amount, 0) AS metric_value",
        "COALESCE(l.left_amount, 0) AS net_delivery_amount_rmb",
        "COALESCE(r.right_amount, 0) AS net_receipt_amount_rmb",
        "CASE WHEN COALESCE(l.left_amount, 0) > 0 THEN COALESCE(r.right_amount, 0) / l.left_amount ELSE NULL END AS receipt_coverage",
        "COALESCE(l.__matched_row_count, 0) + COALESCE(r.__matched_row_count, 0) AS `__matched_row_count`",
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
    )
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
        f"{target_value} AS target_amount_rmb",
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
        if field in {"metric_value", "completion_rate"}:
            sql += f" ORDER BY ({_quote_column(field)} IS NULL) ASC, {_quote_column(field)} {direction}"
        else:
            sql += f" ORDER BY {_quote_column(field)} {direction}"
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
    }


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
