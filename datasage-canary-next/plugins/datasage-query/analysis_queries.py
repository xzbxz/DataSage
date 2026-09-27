"""Finite analysis-slice planning and SQL fragments.

The public request contains a deliberately small ``analysis`` object.  This
module is the backend's second line of defence: it binds every analysis field
to a registered stage/unit/source scope and returns SQL fragments that use
only bound values.  It is intentionally not a general expression language.

The API owner may project the same metadata through ``analysis_contract``;
the query builders use the functions here so direct builder callers receive
the same fail-closed behaviour as the public request path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Mapping

from . import analysis_contract, capability_contract, sql_identifiers


ANALYSIS_PROTOCOL_VERSION = "finite-analysis/v1"
ANALYSIS_DECIMAL_CAST = "CAST(%s AS DECIMAL(38,12))"
ANALYSIS_OPERATORS = frozenset({"eq", "gt", "gte", "lt", "lte"})
MAX_ANALYSIS_FILTERS = 8
MAX_ANALYSIS_FILTERS_PER_STAGE = 6
_DECIMAL_RE = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_DECIMAL_DIGITS = 24
_MAX_DECIMAL_SCALE = 12


class AnalysisPlanError(ValueError):
    """Raised when a finite analysis cannot be bound to the metric contract."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True)
class AnalysisField:
    name: str
    stage: str
    unit: str
    currency: str | None
    source_scope: str
    unknown_policy: str
    operation: str


@dataclass(frozen=True)
class AnalysisFilter:
    field: AnalysisField
    op: str
    value: Decimal
    raw_value: Any


@dataclass(frozen=True)
class AnalysisPlan:
    protocol_version: str
    row_filters: tuple[AnalysisFilter, ...]
    group_filters: tuple[AnalysisFilter, ...]
    operation: str
    unit: str | None
    currency: str | None
    source_scope: str
    unknown_policy: str
    parent_population: str

    @property
    def filters(self) -> tuple[AnalysisFilter, ...]:
        return self.row_filters + self.group_filters

    def as_scope(self) -> dict[str, Any]:
        def project(item: AnalysisFilter) -> dict[str, Any]:
            field = item.field
            return {
                "field": field.name,
                "op": item.op,
                # ``item.value`` is an internal Decimal after backend
                # validation. Public JSON still rejects Decimal; stringify
                # before reusing the public canonicalizer for scope output.
                "value": analysis_contract.normalize_decimal(format(item.value, "f")),
                "stage": field.stage,
                "unit": field.unit,
                "currency": field.currency,
                "source_scope": field.source_scope,
                "unknown_policy": field.unknown_policy,
                "operation": field.operation,
            }

        return {
            "protocol_version": self.protocol_version,
            "row_filters": [project(item) for item in self.row_filters],
            "group_filters": [project(item) for item in self.group_filters],
            "unit": self.unit,
            "currency": self.currency,
            "source_scope": self.source_scope,
            "parent_population": self.parent_population,
            "unknown_policy": self.unknown_policy,
            "operation": self.operation,
            "stages": {
                "row": [item.field.name for item in self.row_filters],
                "group": [item.field.name for item in self.group_filters],
            },
        }


# This mapping is retained as documentation for the three execution families;
# it is never used as a registration fallback.  A metric must publish
# ``analysis_fields`` in its own semantic contract before execution is allowed.
def _as_field(name: str, raw: Mapping[str, Any]) -> AnalysisField:
    if not isinstance(raw, Mapping):
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段合同无效。")
    stage = raw.get("stage")
    unit = raw.get("unit")
    scope = raw.get("source_scope")
    unknown = raw.get("unknown_policy")
    operation = raw.get("operation", "value")
    if stage not in {"row", "group"} or not all(
        isinstance(value, str) and value.strip()
        for value in (unit, scope, unknown, operation)
    ):
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段合同缺少阶段或单位。")
    currency = raw.get("currency")
    if currency is not None and (not isinstance(currency, str) or not currency.strip()):
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段币种合同无效。")
    return AnalysisField(
        name=name,
        stage=stage,
        unit=unit,
        currency=currency,
        source_scope=scope,
        unknown_policy=unknown,
        operation=operation,
    )


def metric_analysis_fields(
    request: Mapping[str, Any], metric: Mapping[str, Any], semantics: Mapping[str, Any] | None = None
) -> tuple[str, dict[str, dict[str, AnalysisField]]]:
    """Return the operation family and bound fields for one metric.

    A metric must publish ``analysis_fields`` directly.  Physical columns and
    arbitrary expressions are never inferred from a missing registration.
    """

    metric_code = str(request.get("metric") or "")
    query_kind = metric.get("query_kind")
    if query_kind in {"frozen_pool_net_outbound", "monthly_slow_pool"} and (
        query_kind == "frozen_pool_net_outbound" or metric.get("monthly_view") == "flow"
    ):
        family = "inventory_flow"
    elif metric_code in {"open_receivable_amount", "open_receivable_amount_original"}:
        family = "open_receivable"
    elif metric_code in {"current_debt_amount", "current_debt_amount_original"}:
        family = "current_debt"
    elif query_kind == "target_completion":
        family = "target_completion"
    else:
        return "unsupported", {"row": {}, "group": {}}

    raw = metric.get("analysis_fields")
    if raw is None:
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED", "该指标未登记可执行分析字段。")
    try:
        fields = capability_contract.metric_analysis_fields(metric)
    except capability_contract.CapabilityContractError as exc:
        raise AnalysisPlanError(exc.code, exc.message) from exc
    result: dict[str, dict[str, AnalysisField]] = {"row": {}, "group": {}}
    for stage, public_stage in (("row", "row_filters"), ("group", "group_filters")):
        entries = (
            (fields.get(public_stage) or fields.get(stage, {}))
            if isinstance(fields, Mapping)
            else {}
        )
        if not isinstance(entries, Mapping):
            raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "指标分析阶段合同无效。")
        for name, definition in entries.items():
            if not isinstance(name, str) or not name:
                raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段名无效。")
            field = _as_field(name, definition)
            if field.stage != stage:
                raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段阶段不一致。")
            result[stage][name] = field
    return family, result


def _decimal(value: Any, *, path: str) -> Decimal:
    try:
        return analysis_contract.parse_decimal(value, path=path)
    except analysis_contract.AnalysisContractError as exc:
        raise AnalysisPlanError(exc.code, exc.message, path=exc.path or path) from exc


def _value_text(value: Any) -> str:
    if isinstance(value, Decimal):
        value = format(value, "f")
    return analysis_contract.normalize_decimal(value)


def _filter_list(raw: Any, stage: str) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AnalysisPlanError("INVALID_INPUT", f"analysis.{stage}_filters 必须是列表。", path=f"analysis.{stage}_filters")
    if len(raw) > MAX_ANALYSIS_FILTERS_PER_STAGE:
        raise AnalysisPlanError("INVALID_INPUT", f"analysis.{stage}_filters 过多。", path=f"analysis.{stage}_filters")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
            raise AnalysisPlanError("INVALID_INPUT", "分析条件必须只包含 field/op/value。", path=f"analysis.{stage}_filters[{index}]")
        result.append(item)
    return result


def validate_analysis_request(
    request: Mapping[str, Any], metric: Mapping[str, Any], semantics: Mapping[str, Any] | None = None
) -> AnalysisPlan | None:
    """Validate and bind the finite phase-one analysis object."""

    raw = request.get("analysis") if isinstance(request, Mapping) else None
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) - {"row_filters", "group_filters"}:
        raise AnalysisPlanError("INVALID_INPUT", "analysis 只接受 row_filters 和 group_filters。", path="analysis")
    try:
        raw = analysis_contract.validate_analysis_for_metric(
            raw,
            metric,
            metric_code=str(request.get("metric") or "") or None,
            domain=str(request.get("domain") or "") or None,
        )
    except analysis_contract.AnalysisContractError as exc:
        raise AnalysisPlanError(exc.code, exc.message, path=exc.path) from exc
    row_raw = _filter_list(raw.get("row_filters"), "row")
    group_raw = _filter_list(raw.get("group_filters"), "group")
    if len(row_raw) + len(group_raw) > MAX_ANALYSIS_FILTERS:
        raise AnalysisPlanError("INVALID_INPUT", "analysis 条件总数过多。", path="analysis")
    if not row_raw and not group_raw:
        raise AnalysisPlanError("INVALID_INPUT", "analysis 至少需要一个过滤条件。", path="analysis")
    family, fields = metric_analysis_fields(request, metric, semantics)
    if family == "unsupported":
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED", "该指标未登记可执行分析字段。", path="analysis")

    def bind(stage: str, values: list[Mapping[str, Any]]) -> tuple[AnalysisFilter, ...]:
        bound: list[AnalysisFilter] = []
        for index, item in enumerate(values):
            name = item.get("field")
            op = item.get("op")
            path = f"analysis.{stage}_filters[{index}]"
            if not isinstance(name, str) or name not in fields[stage]:
                raise AnalysisPlanError("ANALYSIS_UNSUPPORTED", "分析字段未登记或阶段不匹配。", path=f"{path}.field")
            if op not in ANALYSIS_OPERATORS:
                raise AnalysisPlanError("INVALID_INPUT", "分析比较符不受支持。", path=f"{path}.op")
            value = _decimal(item.get("value"), path=f"{path}.value")
            field = fields[stage][name]
            # Ratio fields represent a non-negative fraction.  Completion
            # rates may be negative, so only the price ratio receives this
            # lower-bound guard.
            if name == "price_to_ddp_ratio" and value < 0:
                raise AnalysisPlanError("INVALID_INPUT", "价格/DDP比例不能为负。", path=f"{path}.value")
            bound.append(AnalysisFilter(field, str(op), value, item.get("value")))
        return tuple(bound)

    row_filters = bind("row", row_raw)
    group_filters = bind("group", group_raw)
    if family == "inventory_flow" and group_filters:
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "库存价格切片暂不支持分组后筛选。", path="analysis.group_filters")
    if family in {"open_receivable", "current_debt"}:
        if request.get("currency_basis") == "original" or str(request.get("metric", "")).endswith("_original"):
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "应收联合分析当前只支持人民币口径。", path="analysis")
        if request.get("dimensions") not in (None, ["customer"]):
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "应收联合分析必须按客户分组。", path="dimensions")
    if family == "target_completion":
        if row_filters:
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "目标完成率只支持完整分组后的条件。", path="analysis.row_filters")
        if any(item.field.name != "completion_rate" for item in group_filters):
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "目标完成率只支持 completion_rate 分组条件。", path="analysis.group_filters")
    for field in (item.field for item in (*row_filters, *group_filters)):
        if field.currency == "RMB" and request.get("currency_basis") == "original":
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "分析字段与原币口径不兼容。", path="analysis")
    bound_fields = [item.field for item in (*row_filters, *group_filters)]
    units = {field.unit for field in bound_fields}
    currencies = {field.currency for field in bound_fields}
    parent_population = {
        "inventory_flow": "inventory_frozen_or_monthly_cohort",
        "open_receivable": "current_open_receivable_detail",
        "current_debt": "latest_monthly_customer_debt_snapshot",
        "target_completion": "target_actual_complete_groups",
    }.get(family, "registered_metric_population")
    return AnalysisPlan(
        protocol_version=ANALYSIS_PROTOCOL_VERSION,
        row_filters=row_filters,
        group_filters=group_filters,
        operation=family,
        unit=next(iter(units)) if len(units) == 1 else None,
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
        source_scope=parent_population,
        unknown_policy="retain_unknown",
        parent_population=parent_population,
    )


def operator_sql(op: str) -> str:
    if op not in ANALYSIS_OPERATORS:
        raise AnalysisPlanError("INVALID_INPUT", "分析比较符不受支持。")
    return {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]


def decimal_placeholder() -> str:
    return ANALYSIS_DECIMAL_CAST


def analysis_order_clause(
    request: Mapping[str, Any],
    *,
    allowed_fields: set[str],
    tie_fields: tuple[str, ...],
    default_field: str = "metric_value",
) -> str:
    """Validate phase-one ordering and return a stable, non-ranking clause."""

    raw = request.get("order_by")
    field = default_field
    direction = "DESC"
    if raw is not None:
        if not isinstance(raw, Mapping) or set(raw) != {"field", "direction"}:
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "analysis 排序结构无效。", path="order_by")
        field = raw.get("field")
        direction = str(raw.get("direction") or "").upper()
        if field not in allowed_fields or direction not in {"ASC", "DESC"}:
            raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "analysis 排序字段或方向不受支持。", path="order_by")
    elif field not in allowed_fields:
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "analysis 默认排序字段未登记。")
    order = f" ORDER BY ({_quote_column(str(field))} IS NULL) ASC, {_quote_column(str(field))} {direction}"
    for tie in tie_fields:
        order += f", {_quote_column(tie)} ASC"
    return order


def ratio_expression(alias: str = "f") -> str:
    """Return the governed price/DDP ratio; invalid inputs become NULL."""

    quoted_alias = sql_identifiers.quote_identifier(alias)
    return (
        f"CASE WHEN {quoted_alias}.`ddp_price` > 0 AND "
        f"{quoted_alias}.`deal_price` >= 0 THEN "
        f"{quoted_alias}.`deal_price` / {quoted_alias}.`ddp_price` ELSE NULL END"
    )


def ratio_state_sql(op: str, placeholder: str = "%s", alias: str = "f") -> tuple[str, str]:
    """Return (known-match predicate, unknown predicate) for a flow row."""

    expression = ratio_expression(alias)
    return f"({expression} {operator_sql(op)} {placeholder})", f"({expression} IS NULL)"


def qualified_filter_sql(column_sql: str, item: AnalysisFilter, params: list[Any]) -> str:
    """Bind one already-approved output expression to an analysis value."""

    params.append(_value_text(item.value))
    return f"({column_sql} {operator_sql(item.op)} %s)"


def analysis_scope(plan: AnalysisPlan | None) -> dict[str, Any] | None:
    return plan.as_scope() if plan is not None else None


def _quote_table(value: Any) -> str:
    try:
        return sql_identifiers.quote_table(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析数据集标识无效。") from exc


def _quote_column(value: Any) -> str:
    try:
        return sql_identifiers.quote_identifier(value)
    except sql_identifiers.SqlIdentifierError as exc:
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析字段标识无效。") from exc


def _qualified(alias: str, value: Any) -> str:
    return f"{_quote_column(alias)}.{_quote_column(value)}"


def _dataset(datasets_contract: Mapping[str, Any], table: Any) -> Mapping[str, Any]:
    datasets = datasets_contract.get("datasets")
    definition = datasets.get(table) if isinstance(datasets, Mapping) else None
    if not isinstance(definition, Mapping):
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析数据集未登记。")
    return definition


def _approved(dataset: Mapping[str, Any], column: str) -> str:
    if column not in set(dataset.get("allowed_columns") or []):
        raise AnalysisPlanError("COLUMN_NOT_ALLOWED", "分析引用了未批准字段。")
    if column.lower() in {str(value).lower() for value in dataset.get("forbidden_columns") or []}:
        raise AnalysisPlanError("COLUMN_NOT_ALLOWED", "分析引用了敏感字段。")
    return column


def _fixed_clause(alias: str, column: str, op: str, value: Any, params: list[Any]) -> str:
    sql_op = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(op)
    if sql_op is None:
        raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "分析固定过滤符无效。")
    params.append(value)
    return f"{_qualified(alias, column)} {sql_op} %s"


def _tri_state_and(expressions: list[str]) -> str:
    """Combine SQL boolean expressions while preserving NULL as unknown.

    Each expression is rendered once in the false branch and once in the
    unknown branch. Callers must therefore bind values in whole-pass order:
    ``values_for_all_expressions + values_for_all_expressions``. They must not
    append ``[value, value]`` while iterating individual expressions, which
    would produce ``a,a,b,b`` for SQL placeholders ordered ``a,b,a,b``.
    """

    if not expressions:
        return "'match'"
    false = " OR ".join(f"({expr}) = 0" for expr in expressions)
    unknown = " OR ".join(f"({expr}) IS NULL" for expr in expressions)
    return f"CASE WHEN {false} THEN 'exclude' WHEN {unknown} THEN 'unknown' ELSE 'match' END"


def build_open_receivable_analysis_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: Any = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Compile the current open-receivable customer analysis.

    This replacement keeps one parent population (positive open detail rows),
    deduplicates credit facts before joining, classifies row/group predicates
    with SQL three-valued logic, and aggregates the selected row slice before
    applying group conditions.  It intentionally supports the registered RMB
    open-receivable fields only; original-currency analysis remains rejected by
    the common analysis validator.
    """

    plan = validate_analysis_request(request, metric, semantics)
    if plan is None or plan.operation != "open_receivable":
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED", "该请求不是应收联合分析。")
    if request.get("dimensions") not in (None, ["customer"]):
        raise AnalysisPlanError(
            "ANALYSIS_UNSUPPORTED_COMBINATION",
            "应收联合分析必须按客户分组。",
            path="dimensions",
        )
    if request.get("currency_basis") == "original" or str(
        request.get("metric", "")
    ).endswith("_original"):
        raise AnalysisPlanError(
            "ANALYSIS_UNSUPPORTED_COMBINATION",
            "应收联合分析只支持人民币口径。",
            path="analysis",
        )

    table = metric.get("table") or "vk_dwd.receivable_bill_detail_dwd"
    source = _dataset(datasets_contract, table)
    credit_table = "vk_dwd.customer_credit_dwd"
    credit = _dataset(datasets_contract, credit_table)
    source_columns = [
        "customer_id",
        "customer_no",
        "customer_name",
        "customer_dept",
        "org_name",
        "currency_no",
        "detail_unsettled_amount",
        "exchange_rate",
        "bill_time",
        "bill_status",
        "is_inner_cus",
    ]
    for column in source_columns:
        _approved(source, column)
    for column in ("customer_id", "org_name", "currency_no", "credit_days"):
        _approved(credit, column)

    params: list[Any] = []
    parent_where = [
        _fixed_clause("r", "bill_status", "ne", "A", params),
        _fixed_clause("r", "is_inner_cus", "eq", "n", params),
        _fixed_clause("r", "detail_unsettled_amount", "gt", 0, params),
    ]
    filters = request.get("metric_filters") or {}
    if not isinstance(filters, Mapping) or set(filters) - {"customer"}:
        raise AnalysisPlanError(
            "ANALYSIS_UNSUPPORTED_COMBINATION",
            "应收联合分析只支持客户实体筛选。",
            path="metric_filters",
        )
    if "customer" in filters:
        definition = (
            (semantics.get("dimensions") or {}).get("customer")
            if isinstance(semantics, Mapping)
            else None
        )
        if not isinstance(definition, Mapping):
            raise AnalysisPlanError(
                "ANALYSIS_CONTRACT_UNAVAILABLE", "客户维度合同无效。"
            )
        try:
            bindings = capability_contract._entity_bindings(request)
            column, value = capability_contract._bound_entity_filter(
                bindings, "customer", definition, filters["customer"]
            )
        except capability_contract.CapabilityContractError as exc:
            raise AnalysisPlanError(
                "ANALYSIS_CONTRACT_UNAVAILABLE", exc.message
            ) from exc
        column = _approved(source, column)
        values = value if isinstance(value, list) else [value]
        if not values:
            raise AnalysisPlanError(
                "INVALID_INPUT",
                "客户筛选不能为空。",
                path="metric_filters.customer",
            )
        parent_where.append(
            f"{_qualified('r', column)} IN ({', '.join('%s' for _ in values)})"
        )
        params.extend(values)

    # Credit can contain multiple rows for one customer/org/currency.  A
    # single agreed value is retained; conflicting or partially missing rows
    # become NULL and therefore keep overdue predicates unknown.
    credit_unique = (
        "credit_unique AS (SELECT `customer_id`, `org_name`, `currency_no`, "
        "CASE WHEN COUNT(`credit_days`) = COUNT(*) "
        "AND MIN(`credit_days`) = MAX(`credit_days`) "
        "THEN MAX(`credit_days`) ELSE NULL END AS `credit_days` "
        f"FROM {_quote_table(credit_table)} GROUP BY `customer_id`, `org_name`, `currency_no`),"
    )
    overdue_expr = (
        "CASE WHEN r.`bill_time` IS NULL OR cu.`credit_days` IS NULL THEN NULL "
        "ELSE GREATEST(DATEDIFF(CURDATE(), r.`bill_time`) - cu.`credit_days`, 0) END"
    )
    amount_expr = (
        "CASE WHEN r.`detail_unsettled_amount` IS NULL "
        "OR r.`exchange_rate` IS NULL THEN NULL "
        "ELSE r.`detail_unsettled_amount` * r.`exchange_rate` END"
    )
    open_rows = (
        "open_rows AS (SELECT "
        + ", ".join(
            [
                "r.`customer_id`",
                "r.`customer_no`",
                "r.`customer_name`",
                "r.`customer_dept`",
                "r.`org_name`",
                "r.`currency_no`",
                "r.`detail_unsettled_amount`",
                "r.`exchange_rate`",
                "r.`bill_time`",
                f"{overdue_expr} AS `overdue_days`",
                f"{amount_expr} AS `rmb_amount`",
            ]
        )
        + f" FROM {_quote_table(table)} AS `r` "
        + "LEFT JOIN `credit_unique` AS `cu` "
        + "ON cu.`customer_id` = r.`customer_id` "
        + "AND cu.`org_name` <=> r.`org_name` "
        + "AND cu.`currency_no` <=> r.`currency_no` "
        + "WHERE "
        + " AND ".join(parent_where)
        + "),"
    )

    overdue_row_filters = [
        item for item in plan.row_filters if item.field.name == "overdue_days"
    ]
    unexpected_row = [
        item for item in plan.row_filters if item.field.name != "overdue_days"
    ]
    if unexpected_row:
        raise AnalysisPlanError(
            "ANALYSIS_UNSUPPORTED",
            "应收行条件字段未登记。",
            path="analysis.row_filters",
        )
    any_filters = [
        item for item in plan.group_filters if item.field.name == "any_overdue_days"
    ]
    metric_filters = [
        item for item in plan.group_filters if item.field.name == "metric_value"
    ]
    unexpected_group = [
        item
        for item in plan.group_filters
        if item.field.name not in {"any_overdue_days", "metric_value"}
    ]
    if unexpected_group:
        raise AnalysisPlanError(
            "ANALYSIS_UNSUPPORTED",
            "应收分组条件字段未登记。",
            path="analysis.group_filters",
        )

    # Each predicate is bound once in the row CTE. _tri_state_and deliberately
    # repeats its values as all-false tests followed by all-NULL tests. When
    # any_overdue_days is present, its same-row eligibility includes both the
    # row filters and the any filters; this prevents two different bills from
    # satisfying opposite bounds.
    row_values = [_value_text(item.value) for item in overdue_row_filters]
    row_predicates = [
        f"`overdue_days` {operator_sql(item.op)} {decimal_placeholder()}"
        for item in overdue_row_filters
    ]
    row_state_sql = _tri_state_and(row_predicates)

    any_values = [_value_text(item.value) for item in any_filters]
    any_predicates = [
        f"`overdue_days` {operator_sql(item.op)} {decimal_placeholder()}"
        for item in any_filters
    ]
    eligible_any_predicates = row_predicates + any_predicates
    any_state_sql = (
        _tri_state_and(eligible_any_predicates)
        if any_predicates
        else "'not_applicable'"
    )
    row_classified = (
        "row_classified AS (SELECT `of`.*, "
        f"{row_state_sql} AS `row_filter_state`, "
        f"{any_state_sql} AS `any_row_state` "
        "FROM open_rows `of`),"
    )

    # Aggregate the selected row slice. A row-filter unknown is a possible
    # selected row, so it prevents a known subamount from being presented as a
    # complete zero/total. Without row filters every parent row is selected.
    selected_unknown = (
        "SUM(CASE WHEN `row_filter_state` = 'unknown' THEN 1 ELSE 0 END)"
    )
    selected_match = (
        "SUM(CASE WHEN `row_filter_state` = 'match' THEN 1 ELSE 0 END)"
    )
    selected_amount_unknown = (
        "SUM(CASE WHEN `row_filter_state` = 'match' "
        "AND `rmb_amount` IS NULL THEN 1 ELSE 0 END)"
    )
    selected_amount = (
        "CASE WHEN "
        f"{selected_unknown} > 0 OR {selected_amount_unknown} > 0 THEN NULL "
        f"ELSE COALESCE(SUM(CASE WHEN `row_filter_state` = 'match' "
        "THEN `rmb_amount` ELSE 0 END), 0) END"
    )
    grouped = (
        "grouped AS (SELECT `customer_id`, "
        "MAX(`customer_no`) AS `customer_no`, "
        "MAX(`customer_name`) AS `customer_name`, "
        "MAX(`customer_dept`) AS `customer_dept`, "
        "MAX(`org_name`) AS `org_name`, "
        "MAX(`currency_no`) AS `currency_no`, "
        f"{selected_amount} AS `metric_value`, "
        "MAX(`overdue_days`) AS `any_overdue_days`, "
        f"{selected_match} AS `selected_match_count`, "
        f"{selected_unknown} AS `selected_unknown_count`, "
        f"{selected_amount_unknown} AS `analysis_amount_unknown_count`, "
        "COUNT(*) AS `analysis_scope_row_count`, "
        "SUM(CASE WHEN `rmb_amount` IS NULL THEN 1 ELSE 0 END) AS `analysis_parent_amount_unknown_count`, "
        "SUM(CASE WHEN `overdue_days` IS NULL THEN 1 ELSE 0 END) AS `analysis_overdue_unknown_count`, "
        "SUM(CASE WHEN `overdue_days` IS NOT NULL THEN 1 ELSE 0 END) AS `analysis_overdue_known_count`, "
        "SUM(CASE WHEN `any_row_state` = 'match' THEN 1 ELSE 0 END) AS `any_match_row_count`, "
        "SUM(CASE WHEN `any_row_state` = 'unknown' THEN 1 ELSE 0 END) AS `any_unknown_row_count` "
        "FROM `row_classified` GROUP BY `customer_id`),"
    )

    condition_expressions: list[str] = []
    if overdue_row_filters:
        condition_expressions.append(
            "CASE WHEN `selected_match_count` > 0 THEN 1 "
            "WHEN `selected_unknown_count` > 0 THEN NULL ELSE 0 END"
        )
    if any_filters:
        condition_expressions.append(
            "CASE WHEN EXISTS (SELECT 1 FROM `row_classified` AS `of` "
            "WHERE `of`.`customer_id` = `g`.`customer_id` "
            "AND `of`.`any_row_state` = 'match') THEN 1 "
            "WHEN EXISTS (SELECT 1 FROM `row_classified` AS `of` "
            "WHERE `of`.`customer_id` = `g`.`customer_id` "
            "AND `of`.`any_row_state` = 'unknown') THEN NULL ELSE 0 END"
        )
    metric_values = [_value_text(item.value) for item in metric_filters]
    for item in metric_filters:
        condition_expressions.append(
            "CASE WHEN `metric_value` IS NULL THEN NULL "
            f"WHEN `metric_value` {operator_sql(item.op)} {decimal_placeholder()} "
            "THEN 1 ELSE 0 END"
        )
    if not condition_expressions:
        raise AnalysisPlanError(
            "INVALID_INPUT",
            "analysis 至少需要一个应收 row/group 条件。",
            path="analysis",
        )
    match_state_sql = _tri_state_and(condition_expressions)
    classified = (
        "classified AS (SELECT `g`.*, "
        f"{match_state_sql} AS `analysis_match_state` "
        "FROM `grouped` AS `g`),"
    )

    counts = (
        "counts AS (SELECT COUNT(*) AS `analysis_population_count`, "
        "COALESCE(SUM(CASE WHEN `analysis_match_state` = 'match' THEN 1 ELSE 0 END),0) AS `analysis_match_count`, "
        "COALESCE(SUM(CASE WHEN `analysis_match_state` = 'unknown' THEN 1 ELSE 0 END),0) AS `analysis_unknown_count`, "
        "COALESCE(SUM(CASE WHEN `analysis_match_state` = 'exclude' THEN 1 ELSE 0 END),0) AS `analysis_excluded_count`, "
        "COALESCE(SUM(`analysis_scope_row_count`),0) AS `analysis_scope_row_count`, "
        "COALESCE(SUM(`analysis_amount_unknown_count`),0) AS `analysis_amount_unknown_count`, "
        "COALESCE(SUM(`analysis_overdue_unknown_count`),0) AS `analysis_overdue_unknown_count`, "
        "COALESCE(SUM(`analysis_overdue_known_count`),0) AS `analysis_overdue_known_count` "
        "FROM `classified`),"
    )
    selected_columns = (
        "`customer_id`, `customer_no`, `customer_name`, `customer_dept`, "
        "`org_name`, `currency_no`, `metric_value`, `any_overdue_days`, "
        "`analysis_match_state`, `__matched_row_count`, `__as_of_date`, "
        "`analysis_population_count`, `analysis_match_count`, "
        "`analysis_unknown_count`, `analysis_excluded_count`, "
        "`analysis_scope_row_count`, `analysis_amount_unknown_count`, "
        "`analysis_overdue_unknown_count`, `analysis_overdue_known_count`"
    )
    counted = (
        "counted AS (SELECT `c`.`customer_id`, `c`.`customer_no`, "
        "`c`.`customer_name`, `c`.`customer_dept`, `c`.`org_name`, "
        "`c`.`currency_no`, `c`.`metric_value`, `c`.`any_overdue_days`, "
        "`c`.`analysis_match_state`, 1 AS `__matched_row_count`, "
        "CURDATE() AS `__as_of_date`, "
        "`n`.`analysis_population_count`, `n`.`analysis_match_count`, "
        "`n`.`analysis_unknown_count`, `n`.`analysis_excluded_count`, "
        "`c`.`analysis_scope_row_count`, "
        "`c`.`analysis_amount_unknown_count`, "
        "`c`.`analysis_overdue_unknown_count`, "
        "`c`.`analysis_overdue_known_count` "
        "FROM `classified` AS `c` "
        "CROSS JOIN `counts` AS `n` "
        "WHERE `c`.`analysis_match_state` IN ('match','unknown')),"
    )
    coverage = (
        "coverage AS (SELECT NULL AS `customer_id`, NULL AS `customer_no`, "
        "NULL AS `customer_name`, NULL AS `customer_dept`, NULL AS `org_name`, "
        "NULL AS `currency_no`, NULL AS `metric_value`, NULL AS `any_overdue_days`, "
        "'coverage_only' AS `analysis_match_state`, 0 AS `__matched_row_count`, "
        "CURDATE() AS `__as_of_date`, `n`.`analysis_population_count`, "
        "`n`.`analysis_match_count`, `n`.`analysis_unknown_count`, "
        "`n`.`analysis_excluded_count`, `n`.`analysis_scope_row_count`, "
        "`n`.`analysis_amount_unknown_count`, "
        "`n`.`analysis_overdue_unknown_count`, "
        "`n`.`analysis_overdue_known_count` FROM `counts` AS `n` "
        "WHERE `n`.`analysis_match_count` = 0 "
        "AND `n`.`analysis_unknown_count` = 0)"
    )
    ctes_sql = "WITH " + "\n".join(
        [credit_unique, open_rows, row_classified, grouped, classified, counts, counted, coverage]
    )
    union_sql = (
        f"SELECT {selected_columns} FROM `counted` "
        "UNION ALL "
        f"SELECT {selected_columns} FROM `coverage`"
    )
    order_clause = analysis_order_clause(
        request,
        allowed_fields={"metric_value"},
        tie_fields=("customer_id",),
    )
    sql = (
        ctes_sql
        + " SELECT * FROM ("
        + union_sql
        + ") AS `analysis_result`"
        + order_clause
        + " LIMIT %s"
    )

    # Placeholder order follows the CTE text exactly: parent filters, row
    # tri-state pass, combined row+any same-row tri-state pass (when any
    # filters exist), then grouped metric filters in _tri_state_and's
    # false/unknown order, and finally LIMIT.
    params.extend(row_values + row_values)
    if any_values:
        eligible_values = row_values + any_values
        params.extend(eligible_values + eligible_values)
    params.extend(metric_values + metric_values)
    params.append(limit + 1)
    scope = {
        "metric": request.get("metric"),
        "dataset": table,
        "source_datasets": [str(table), credit_table],
        "dimension_outputs": [
            "customer_id",
            "customer_no",
            "customer_name",
            "customer_dept",
            "org_name",
            "currency_no",
        ],
        "effective_dimensions": ["customer"],
        "filters": filters,
        "time_range": {
            "source": "current_snapshot",
            "current_snapshot_evidence": "database_current_date",
        },
        "warnings": [str(metric.get("answer_note"))]
        if metric.get("answer_note")
        else [],
        "analysis": plan.as_scope(),
        "analysis_counts": {
            "population": "analysis_population_count",
            "match": "analysis_match_count",
            "unknown": "analysis_unknown_count",
            "excluded": "analysis_excluded_count",
        },
        "analysis_count_grain": "customer_groups",
    }
    return sql, params, scope



def build_current_debt_analysis_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any],
    datasets_contract: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: Any = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Compile A03 with the latest monthly net-debt snapshot as parent.

    The open receivable/credit side is retained as a separately labelled
    current observation.  The source contracts do not prove that its read
    time equals the debt snapshot's business month, so row/group overdue
    predicates remain unknown in the joint result.  AR-only customers never
    enter the parent population.
    """

    plan = validate_analysis_request(request, metric, semantics)
    if plan is None or plan.operation != "current_debt":
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED", "该请求不是当前净欠款联合分析。")
    if request.get("dimensions") not in (None, ["customer"]):
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "应收联合分析必须按客户分组。", path="dimensions")
    if str(request.get("metric", "")).endswith("_original") or request.get("currency_basis") == "original":
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "当前净欠款联合分析只支持人民币父集。", path="analysis")

    debt_table = metric.get("table") or "vk_dw.customer_debt_bymonth_dw"
    open_table = "vk_dwd.receivable_bill_detail_dwd"
    credit_table = "vk_dwd.customer_credit_dwd"
    debt = _dataset(datasets_contract, debt_table)
    open_source = _dataset(datasets_contract, open_table)
    credit = _dataset(datasets_contract, credit_table)
    for column in ("bill_date", "customer_id", "customer_no", "customer_name", "customer_dept", "org_name", "currency_no", "debt_amount_rmb", "is_inner_cus"):
        _approved(debt, column)
    for column in ("customer_id", "customer_no", "customer_name", "customer_dept", "org_name", "currency_no", "detail_unsettled_amount", "exchange_rate", "bill_time", "bill_status", "is_inner_cus"):
        _approved(open_source, column)
    for column in ("customer_id", "org_name", "currency_no", "credit_days"):
        _approved(credit, column)

    params: list[Any] = ["n", "n"]
    debt_where = "d.`is_inner_cus` = %s AND d.`bill_date` = (SELECT MAX(d2.`bill_date`) FROM " + _quote_table(debt_table) + " AS d2 WHERE d2.`is_inner_cus` = %s)"
    filters = request.get("metric_filters") or {}
    if not isinstance(filters, Mapping) or set(filters) - {"customer"}:
        raise AnalysisPlanError("ANALYSIS_UNSUPPORTED_COMBINATION", "应收联合分析只支持客户实体筛选。", path="metric_filters")
    if "customer" in filters:
        definition = (semantics.get("dimensions") or {}).get("customer") if isinstance(semantics, Mapping) else None
        if not isinstance(definition, Mapping):
            raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", "客户维度合同无效。")
        try:
            bindings = capability_contract._entity_bindings(request)
            column, value = capability_contract._bound_entity_filter(bindings, "customer", definition, filters["customer"])
        except capability_contract.CapabilityContractError as exc:
            raise AnalysisPlanError("ANALYSIS_CONTRACT_UNAVAILABLE", exc.message) from exc
        column = _approved(debt, column)
        values = value if isinstance(value, list) else [value]
        if not values:
            raise AnalysisPlanError("INVALID_INPUT", "客户筛选不能为空。", path="metric_filters.customer")
        debt_where += f" AND d.`{column}` IN ({', '.join('%s' for _ in values)})"
        params.extend(values)

    overdue_expr = (
        "CASE WHEN r.`bill_time` IS NULL OR c.`credit_days` IS NULL THEN NULL "
        "ELSE GREATEST(DATEDIFF(CURDATE(), r.`bill_time`) - c.`credit_days`, 0) END"
    )
    amount_expr = "CASE WHEN r.`detail_unsettled_amount` IS NULL OR r.`exchange_rate` IS NULL THEN NULL ELSE r.`detail_unsettled_amount` * r.`exchange_rate` END"
    row_filters = [item for item in plan.row_filters if item.field.name == "overdue_days"]
    any_filters = [item for item in plan.group_filters if item.field.name == "any_overdue_days"]

    def observation_predicates(filters_for_predicate: list[AnalysisFilter]) -> str:
        predicates: list[str] = []
        for item in filters_for_predicate:
            params.extend([_value_text(item.value)])
            # This predicate is emitted inside open_group, whose FROM source
            # is open_rows. The physical r/c aliases only exist in open_rows;
            # use its computed, uniquely scoped overdue_days column here.
            predicates.append(f"(`overdue_days` {operator_sql(item.op)} {decimal_placeholder()})")
        return " AND ".join(predicates) or "1 = 0"

    # open_rows placeholders precede open_group observation thresholds in the
    # statement text, so append their fixed values before building predicates.
    params.extend(["A", "n", "0"])
    row_observation_predicate = observation_predicates(row_filters) if row_filters else "1 = 0"
    any_observation_predicate = observation_predicates(any_filters) if any_filters else "1 = 0"
    ctes = [
        "debt_latest AS (SELECT d.`bill_date`, d.`customer_id`, d.`customer_no`, d.`customer_name`, d.`customer_dept`, d.`org_name`, d.`currency_no`, d.`debt_amount_rmb` FROM "
        + _quote_table(debt_table)
        + " AS d WHERE " + debt_where + "),",
        "debt_group AS (SELECT `customer_id`, MAX(`bill_date`) AS `net_debt_snapshot_month`, MAX(`customer_no`) AS `customer_no`, MAX(`customer_name`) AS `customer_name`, MAX(`customer_dept`) AS `customer_dept`, MAX(`org_name`) AS `org_name`, MAX(`currency_no`) AS `currency_no`, CASE WHEN SUM(CASE WHEN `debt_amount_rmb` IS NULL THEN 1 ELSE 0 END)>0 THEN NULL ELSE SUM(`debt_amount_rmb`) END AS `metric_value`, SUM(CASE WHEN `debt_amount_rmb` IS NULL THEN 1 ELSE 0 END) AS `debt_unknown_count`, COUNT(*) AS `debt_row_count` FROM debt_latest GROUP BY `customer_id`),",
        "credit_resolved AS (SELECT `customer_id`, `org_name`, `currency_no`, CASE WHEN COUNT(*)=1 THEN MAX(`credit_days`) ELSE NULL END AS `credit_days`, COUNT(*) AS `credit_policy_row_count` FROM "
        + _quote_table(credit_table)
        + " GROUP BY `customer_id`, `org_name`, `currency_no`),",
        "open_rows AS (SELECT r.`customer_id`, "
        + overdue_expr
        + " AS `overdue_days`, "
        + amount_expr
        + " AS `rmb_amount` FROM "
        + _quote_table(open_table)
        + " AS r LEFT JOIN credit_resolved c ON c.`customer_id`=r.`customer_id` AND c.`org_name` <=> r.`org_name` AND c.`currency_no` <=> r.`currency_no` WHERE r.`bill_status` <> %s AND r.`is_inner_cus` = %s AND r.`detail_unsettled_amount` > %s),",
    ]
    ctes.append(
        "open_group AS (SELECT `customer_id`, MAX(`overdue_days`) AS `any_overdue_days`, SUM(CASE WHEN `overdue_days` IS NULL THEN 1 ELSE 0 END) AS `overdue_unknown_count`, SUM(CASE WHEN `overdue_days` IS NOT NULL THEN 1 ELSE 0 END) AS `overdue_known_count`, SUM(CASE WHEN "
        + row_observation_predicate
        + " THEN 1 ELSE 0 END) AS `observed_row_match_count`, SUM(CASE WHEN "
        + any_observation_predicate
        + " THEN 1 ELSE 0 END) AS `observed_any_match_count`, COUNT(*) AS `open_row_count` FROM open_rows GROUP BY `customer_id`),"
    )

    group_conditions: list[str] = []
    metric_values: list[str] = []
    # Any overdue predicate is deliberately unknown in the joint result: the
    # current open observation has no contract proving the monthly debt
    # snapshot's business as-of time. A metric-only slice stays known.
    if row_filters or any_filters:
        group_conditions.append("NULL")
    for item in plan.group_filters:
        if item.field.name != "metric_value":
            continue
        metric_values.append(_value_text(item.value))
        group_conditions.append(f"CASE WHEN `metric_value` IS NULL THEN NULL WHEN `metric_value` {operator_sql(item.op)} {decimal_placeholder()} THEN 1 ELSE 0 END")
    params.extend(metric_values + metric_values)
    temporal_state = "unknown_source_time_alignment" if (row_filters or any_filters) else "not_applicable"
    ctes.append(
        "joined AS (SELECT d.*, o.`any_overdue_days`, COALESCE(o.`overdue_unknown_count`,0) AS `analysis_overdue_unknown_count`, COALESCE(o.`overdue_known_count`,0) AS `analysis_overdue_known_count`, COALESCE(o.`observed_row_match_count`,0) AS `observed_row_match_count`, COALESCE(o.`observed_any_match_count`,0) AS `observed_any_match_count`, COALESCE(o.`open_row_count`,0) AS `open_row_count`, CURDATE() AS `open_items_observation_date`, d.`net_debt_snapshot_month` AS `__snapshot_month`, CURDATE() AS `__as_of_date`, '" + temporal_state + "' AS `analysis_alignment_state`, '" + temporal_state + "' AS `analysis_temporal_state` FROM debt_group d LEFT JOIN open_group o ON o.`customer_id`=d.`customer_id`),"
    )
    ctes.append(
        "classified AS (SELECT `j`.*, " + _tri_state_and(group_conditions) + " AS `analysis_match_state` FROM joined `j`),"
    )
    ctes.append(
        "counted AS (SELECT `c`.*, 1 AS `__matched_row_count`, COUNT(*) OVER() AS `analysis_population_count`, SUM(CASE WHEN `analysis_match_state`='match' THEN 1 ELSE 0 END) OVER() AS `analysis_match_count`, SUM(CASE WHEN `analysis_match_state`='unknown' THEN 1 ELSE 0 END) OVER() AS `analysis_unknown_count`, SUM(CASE WHEN `analysis_match_state`='exclude' THEN 1 ELSE 0 END) OVER() AS `analysis_excluded_count` FROM classified `c`),"
    )
    ctes.append(
        "coverage AS (SELECT NULL AS `customer_id`, NULL AS `customer_no`, NULL AS `customer_name`, NULL AS `customer_dept`, NULL AS `org_name`, NULL AS `currency_no`, NULL AS `metric_value`, NULL AS `any_overdue_days`, 'coverage_only' AS `analysis_match_state`, 0 AS `__matched_row_count`, MAX(`net_debt_snapshot_month`) AS `net_debt_snapshot_month`, CURDATE() AS `open_items_observation_date`, MAX(`net_debt_snapshot_month`) AS `__snapshot_month`, CURDATE() AS `__as_of_date`, COUNT(*) AS `analysis_population_count`, COALESCE(SUM(CASE WHEN `analysis_match_state`='match' THEN 1 ELSE 0 END),0) AS `analysis_match_count`, COALESCE(SUM(CASE WHEN `analysis_match_state`='unknown' THEN 1 ELSE 0 END),0) AS `analysis_unknown_count`, COALESCE(SUM(CASE WHEN `analysis_match_state`='exclude' THEN 1 ELSE 0 END),0) AS `analysis_excluded_count`, 0 AS `observed_row_match_count`, 0 AS `observed_any_match_count`, 0 AS `open_row_count`, 0 AS `analysis_overdue_unknown_count`, 0 AS `analysis_overdue_known_count`, 0 AS `debt_row_count`, 0 AS `debt_unknown_count`, 'coverage_only' AS `analysis_alignment_state`, 'coverage_only' AS `analysis_temporal_state` FROM classified HAVING NOT EXISTS (SELECT 1 FROM classified WHERE `analysis_match_state` IN ('match','unknown')))"
    )
    sql = (
        "WITH " + "\n".join(ctes)
        + " SELECT * FROM (SELECT `customer_id`,`customer_no`,`customer_name`,`customer_dept`,`org_name`,`currency_no`,`metric_value`,`any_overdue_days`,`analysis_match_state`,`analysis_alignment_state`,`analysis_temporal_state`,`net_debt_snapshot_month`,`open_items_observation_date`,`__snapshot_month`,`__as_of_date`,`__matched_row_count`,`observed_row_match_count`,`observed_any_match_count`,`open_row_count`,`analysis_population_count`,`analysis_match_count`,`analysis_unknown_count`,`analysis_excluded_count`,`analysis_overdue_unknown_count`,`analysis_overdue_known_count`,`debt_row_count`,`debt_unknown_count` FROM counted WHERE `analysis_match_state` IN ('match','unknown') UNION ALL SELECT `customer_id`,`customer_no`,`customer_name`,`customer_dept`,`org_name`,`currency_no`,`metric_value`,`any_overdue_days`,`analysis_match_state`,`analysis_alignment_state`,`analysis_temporal_state`,`net_debt_snapshot_month`,`open_items_observation_date`,`__snapshot_month`,`__as_of_date`,`__matched_row_count`,`observed_row_match_count`,`observed_any_match_count`,`open_row_count`,`analysis_population_count`,`analysis_match_count`,`analysis_unknown_count`,`analysis_excluded_count`,`analysis_overdue_unknown_count`,`analysis_overdue_known_count`,`debt_row_count`,`debt_unknown_count` FROM coverage"
        + ") AS analysis_display" + analysis_order_clause(request, allowed_fields={"metric_value"}, tie_fields=("customer_id",))
        + " LIMIT %s"
    )
    params.append(limit + 1)
    scope = {
        "metric": request.get("metric"),
        "dataset": debt_table,
        "source_datasets": [str(debt_table), open_table, credit_table],
        "dimension_outputs": ["customer_id", "customer_no", "customer_name", "customer_dept", "org_name", "currency_no"],
        "effective_dimensions": ["customer"],
        "filters": filters,
        "time_range": {"source": "latest_snapshot", "snapshot_table": str(debt_table), "open_observation_source": open_table, "alignment_state": temporal_state, "temporal_state": temporal_state},
        "warnings": [str(metric.get("answer_note"))] if metric.get("answer_note") else [],
        "analysis": plan.as_scope() | {"alignment_state": temporal_state, "temporal_state": temporal_state, "parent_population": "latest_monthly_customer_debt_snapshot"},
        "analysis_source_times": {"net_debt_snapshot_month": "net_debt_snapshot_month", "open_items_observation_date": "open_items_observation_date"},
        "analysis_counts": {"population": "analysis_population_count", "match": "analysis_match_count", "unknown": "analysis_unknown_count", "excluded": "analysis_excluded_count"},
        "analysis_count_grain": "customer_groups",
    }
    return sql, params, scope


__all__ = [
    "ANALYSIS_PROTOCOL_VERSION",
    "ANALYSIS_DECIMAL_CAST",
    "ANALYSIS_OPERATORS",
    "AnalysisField",
    "AnalysisFilter",
    "AnalysisPlan",
    "AnalysisPlanError",
    "MAX_ANALYSIS_FILTERS",
    "MAX_ANALYSIS_FILTERS_PER_STAGE",
    "analysis_scope",
    "analysis_order_clause",
    "build_open_receivable_analysis_query",
    "build_current_debt_analysis_query",
    "metric_analysis_fields",
    "operator_sql",
    "decimal_placeholder",
    "qualified_filter_sql",
    "ratio_expression",
    "ratio_state_sql",
    "validate_analysis_request",
]
