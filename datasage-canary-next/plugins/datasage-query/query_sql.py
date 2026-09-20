"""Pure SQL fragments shared by the governed metric query compiler.

This module owns identifier quoting, contract-approved SQL expressions, and
row-integrity projections.  It deliberately has no dependency on ``tools``
or on database execution. Higher-level planning lives in ``query_builders``;
``tools.py`` retains the existing callable names at its compatibility boundary.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import capability_contract, sql_identifiers
from .capability_contract import CapabilityContractError
from .query_errors import QueryFailure


_FILTER_OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_INTERNAL_MATCH_COUNT = "__matched_row_count"


def _quote_identifier(value: str) -> str:
    try:
        return sql_identifiers.quote_identifier(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise QueryFailure("INVALID_PLAN", "查询包含无效字段标识。") from exc


def _quote_table(value: str) -> str:
    try:
        return sql_identifiers.quote_table(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise QueryFailure("INVALID_PLAN", "查询包含无效数据表标识。") from exc


def _qualified_identifier(alias: str, column: str) -> str:
    try:
        return sql_identifiers.qualified_identifier(alias, column)
    except sql_identifiers.SqlIdentifierError as exc:
        raise QueryFailure("INVALID_PLAN", "查询包含无效字段标识。") from exc


def _blocked_columns(
    datasets_contract: Mapping[str, Any], dataset: Mapping[str, Any]
) -> set[str]:
    global_blocked = datasets_contract.get("defaults", {}).get("blocked_columns") or []
    local_blocked = dataset.get("forbidden_columns") or []
    return {str(item).lower() for item in [*global_blocked, *local_blocked]}


def _approved_column(column: Any, allowed: set[str], blocked: set[str]) -> str:
    try:
        sql_identifiers.quote_identifier(column)
    except sql_identifiers.SqlIdentifierError:
        raise QueryFailure("INVALID_PLAN", "字段标识无效。")
    if not isinstance(column, str):
        raise QueryFailure("INVALID_PLAN", "字段标识无效。")
    if column not in allowed or column.lower() in blocked:
        raise QueryFailure("COLUMN_NOT_ALLOWED", "查询引用了未批准或敏感字段。")
    return column


def _filter_clause(
    column: str, spec: Mapping[str, Any], params: list[Any], *, alias: str | None = None
) -> str:
    try:
        op, value = capability_contract._fixed_filter_spec(spec)
    except CapabilityContractError as exc:
        raise QueryFailure("INVALID_PLAN", "指标定义包含不支持的过滤规则。") from exc
    quoted = _qualified_identifier(alias, column) if alias else _quote_identifier(column)
    if op == "in":
        params.extend(value)
        return f"{quoted} IN ({', '.join(['%s'] * len(value))})"
    params.append(value)
    return f"{quoted} {_FILTER_OPERATORS[op]} %s"


def _latest_snapshot_resolver_sql(
    table: str,
    time_field: str,
    population_filters: Sequence[tuple[str, Mapping[str, Any]]],
    params: list[Any],
    *,
    required_non_null: str | None = None,
) -> str:
    """Build the governed latest-snapshot resolver without widening its population.

    A latest snapshot is global to the governed fact population, not to an
    individual user filter or a metric's measure-eligibility predicate.  The
    resolver therefore inherits only dataset-level required filters.  Preserve
    the no-filter SQL shape so unrelated snapshot metrics retain their exact
    historical query form and parameter sequence.
    """

    quoted_table = _quote_table(table)
    quoted_time = _quote_identifier(time_field)
    if not population_filters and required_non_null is None:
        return f"SELECT MAX({quoted_time}) FROM {quoted_table}"
    if not population_filters:
        return (
            f"SELECT MAX({quoted_time}) FROM {quoted_table} "
            f"WHERE {_quote_identifier(required_non_null)} IS NOT NULL"
        )

    alias = "snapshot_f"
    where = [
        _filter_clause(column, spec, params, alias=alias)
        for column, spec in population_filters
    ]
    if required_non_null is not None:
        where.append(f"{_qualified_identifier(alias, required_non_null)} IS NOT NULL")
    return (
        f"SELECT MAX({_qualified_identifier(alias, time_field)}) "
        f"FROM {quoted_table} AS {_quote_identifier(alias)} "
        f"WHERE {' AND '.join(where)}"
    )


def _dimension_columns(definition: Mapping[str, Any]) -> list[tuple[str, str]]:
    try:
        return capability_contract._dimension_columns(definition)
    except CapabilityContractError as exc:
        raise QueryFailure("CONTRACT_UNAVAILABLE", exc.message) from exc


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
    try:
        bindings = capability_contract._entity_bindings(request)
        return capability_contract._bound_entity_filter(
            bindings, code, definition, fallback_value
        )
    except CapabilityContractError as exc:
        raise QueryFailure(
            "CONTRACT_UNAVAILABLE",
            exc.message,
        ) from exc


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
        additive = " + ".join(
            f"COALESCE({_qualified_identifier(alias, column)}, 0)"
            for column in approved
        )
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


def _metric_missing_input_sql(metric, dataset, datasets_contract, alias, *, joined_alias=None, joined_dataset=None):
    """Count incomplete source rows, never infer a rate or amount from NULL."""
    aggregation = metric.get("aggregation")
    columns = []
    if aggregation in {"sum_product", "sum_product_many"}:
        columns.extend(metric.get("measure_columns") or [])
        if aggregation == "sum_product_many":
            columns.append(metric.get("multiplier"))
    elif aggregation in {"sum", "sum_positive", "sum_positive_difference", "sum_positive_difference_product"}:
        columns.append(metric.get("measure"))
        if aggregation == "sum_positive_difference_product":
            columns.append(metric.get("multiplier"))
    if metric.get("completeness_measure") is not None:
        columns.append(metric["completeness_measure"])
    allowed = set(dataset.get("allowed_columns") or [])
    blocked = _blocked_columns(datasets_contract, dataset)
    expressions = [
        _qualified_identifier(alias, _approved_column(column, allowed, blocked))
        for column in dict.fromkeys(columns)
    ]
    if aggregation in {"sum_positive_difference", "sum_positive_difference_product"}:
        if joined_alias is None or not isinstance(joined_dataset, Mapping):
            raise QueryFailure("CONTRACT_UNAVAILABLE", "派生指标缺少受控关联。")
        expressions.append(_qualified_identifier(joined_alias, _approved_column(
            metric.get("subtract_measure"), set(joined_dataset.get("allowed_columns") or []),
            _blocked_columns(datasets_contract, joined_dataset))) )
    currency_missing = _currency_missing_input_sql(
        metric,
        dataset,
        datasets_contract,
        alias,
    )
    if currency_missing is not None:
        expressions.append(currency_missing)
    # COUNT DISTINCT intentionally ignores NULL identifiers; that is not a
    # missing amount. Other non-additive aggregations retain their own policy.
    return " OR ".join(f"{expression} IS NULL" for expression in expressions) or "1 = 0"


def _currency_missing_input_sql(metric, dataset, datasets_contract, alias):
    """Return the governed currency expression for original amounts.

    Currency is part of the value's unit.  A NULL or blank currency therefore
    makes the row unassessable even when the numeric amount itself is present.
    The column is taken exclusively from the metric's declared currency policy;
    no inferred or user-supplied identifier is accepted here.
    """

    policy = metric.get("currency_policy")
    if not (
        isinstance(policy, Mapping)
        and policy.get("mode") == "original_currency"
        and policy.get("require_filter_or_group") is True
    ):
        return None
    column = policy.get("column")
    allowed = {str(item) for item in dataset.get("allowed_columns") or []}
    blocked = _blocked_columns(datasets_contract, dataset)
    approved = _approved_column(column, allowed, blocked)
    qualified = _qualified_identifier(alias, approved)
    return f"NULLIF(TRIM(CAST({qualified} AS CHAR)), '')"


def _metric_unclassified_source_value_sql(
    metric,
    dataset,
    datasets_contract,
    alias,
    *,
    sign=1,
):
    """Build one direct measure expression for an unknown-currency group.

    Original-currency base amount metrics use ``aggregation: sum``.  Keeping
    this helper to that declared direct measure avoids copying the compiler's
    sum-product and derived-metric formulas; those metrics still retain their
    existing NULL completeness guard and expose the unknown-row count/state.
    """

    if metric.get("aggregation") != "sum":
        return None
    allowed = {str(item) for item in dataset.get("allowed_columns") or []}
    blocked = _blocked_columns(datasets_contract, dataset)
    expression = _qualified_identifier(
        alias,
        _approved_column(metric.get("measure"), allowed, blocked),
    )
    return expression if sign == 1 else f"-({expression})"


def _integrity_columns(value_sql, missing, known):
    return [
        f"CASE WHEN {missing} > 0 THEN NULL ELSE {value_sql} END AS metric_value",
        f"{missing} AS missing_value_count",
        f"{known} AS known_value_count",
        f"CASE WHEN ({missing}) + ({known}) > 0 THEN 1.0 * ({known}) / (({missing}) + ({known})) ELSE NULL END AS value_coverage_rate",
        f"CASE WHEN ({missing}) + ({known}) = 0 THEN 'missing' WHEN {missing} = 0 THEN 'complete' WHEN {known} = 0 THEN 'missing' ELSE 'incomplete' END AS metric_data_state",
    ]


def _overdue_integrity_columns(metric, dataset, datasets_contract, joined_alias,
                               joined_dataset, eligibility, missing_input, sign):
    """Measure eligibility before filtering NULL credit/date inputs out of scope.

    Full metric_value stays unknown when any scoped positive-open row cannot be
    assessed, or an eligible row lacks a required measure. known_subset_value
    is only the aggregate of assessable, eligible rows with known inputs.
    """
    predicate, missing_credit, missing_date, required_nulls = eligibility
    unknown_input = " OR ".join([missing_credit, missing_date, *required_nulls])
    eligible = f"COALESCE(({predicate}), FALSE)"
    unknown = f"({unknown_input})"
    missing_input = f"({missing_input})"
    aggregation = metric.get("aggregation")

    def base(column):
        return _qualified_identifier("f", _approved_column(
            column, set(dataset.get("allowed_columns") or []),
            _blocked_columns(datasets_contract, dataset)))

    if aggregation == "sum_product":
        value = " * ".join(base(c) for c in metric["measure_columns"])
    elif aggregation in {"sum", "count_distinct"}:
        value = base(metric["measure"])
        if aggregation == "count_distinct":
            missing_input = f"({missing_input} OR {value} IS NULL)"
    elif aggregation == "max_days_over":
        credit = _qualified_identifier(joined_alias, _approved_column(
            metric["subtract_measure"], set(joined_dataset.get("allowed_columns") or []),
            _blocked_columns(datasets_contract, joined_dataset)))
        value = f"DATEDIFF(CURDATE(), {base(metric['measure'])}) - {credit}"
    else:
        raise QueryFailure("CONTRACT_UNAVAILABLE", "逾期资格覆盖不支持该聚合。")

    def count(condition):
        return f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END), 0)"

    known = count(f"{eligible} AND NOT {unknown} AND NOT {missing_input}")
    missing = count(f"{unknown} OR ({eligible} AND {missing_input})")
    selected = f"CASE WHEN {eligible} AND NOT {unknown} AND NOT {missing_input} THEN {value} ELSE NULL END"
    if aggregation == "count_distinct":
        subset = f"COUNT(DISTINCT {selected})"
    elif aggregation == "max_days_over":
        subset = f"MAX({selected})"
    else:
        subset = f"SUM({selected})"
    if sign == -1:
        subset = f"-({subset})"
    subset = f"CASE WHEN ({known}) > 0 THEN {subset} ELSE NULL END"
    unassessable = count(unknown)
    eligible_count = count(f"{eligible} AND NOT {unknown}")
    return [
        *_integrity_columns(subset, missing, known),
        f"{subset} AS known_subset_value",
        "COUNT(*) AS scope_row_count",
        f"{unassessable} AS unassessable_row_count",
        f"COUNT(*) - ({unassessable}) AS assessed_row_count",
        f"{eligible_count} AS eligible_row_count",
        f"{count(missing_credit)} AS missing_credit_days_count",
        f"{count(missing_date)} AS missing_bill_time_count",
        f"CASE WHEN COUNT(*) > 0 THEN 1.0 * (COUNT(*) - ({unassessable})) / COUNT(*) ELSE NULL END AS eligibility_coverage_rate",
        f"({eligible_count}) + ({unassessable}) AS {_quote_identifier(_INTERNAL_MATCH_COUNT)}",
    ]
