"""Pure query construction for the governed IDK unpriced pool.

This module is intentionally separate from operations.py. It owns only the
query shape and its public fact-field allowlist; observation snapshots, local
reports and operator lifecycle remain in operations.py.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from . import contract_store
from .analytical_queries import (
    AnalysisQueryError,
    _approved,
    _dataset,
    _value_filter,
)


FACT_FIELDS = {
    "idk_null_price_rows",
    "idk_zero_price_rows",
    "idk_negative_price_rows",
    "idk_quantity",
    "idk_known_quantity",
    "idk_rolls",
    "idk_known_rolls",
    "idk_scope_rows",
    "idk_read_at",
    "idk_identity_errors",
}


class OperationError(ValueError):
    """Compatibility error shared by the pure query and operator facade."""

    pass


def policy() -> dict[str, Any]:
    """Read the existing operator contract without importing operator code."""

    return contract_store.read_yaml("plugins/datasage-query/contracts/operations.yaml")


def _table(value: Any) -> str:
    """Quote the existing two-part source-table contract.

    The lower-case source rule and OperationError type are kept for
    compatibility with the historical operator implementation.
    """

    if not isinstance(value, str) or re.fullmatch(r"[a-z_]+\.[a-z_]+", value) is None:
        raise OperationError("OPERATION_SOURCE_INVALID")
    return ".".join("`"+part+"`" for part in value.split("."))


def build_idk_query(
    request: Mapping[str, Any],
    metric: Mapping[str, Any] | None,
    datasets: Mapping[str, Any],
    semantics: Mapping[str, Any],
    limit: int,
    *,
    observed_on: Any = None,
    policy_reader: Callable[[], Mapping[str, Any]] | None = None,
    table_quoting: Callable[[Any], str] | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Build the governed IDK query with the legacy SQL and parameter order.

    metric, semantics and observed_on remain part of the public builder
    signature because the analytical dispatcher supplies them. The IDK
    contract is sourced from operations.yaml exactly as before.
    """

    # Keep this import lazy: handler registration imports this module while
    # assembling the public fact-field allowlist.
    from .analytical_handlers import validate_parameters

    validate_parameters("idk_unpriced", request)
    chosen = request.get("dimensions") or ["unit"]
    filters = request.get("metric_filters") or {}
    if chosen != ["unit"] or set(filters) - {"unit"}:
        raise AnalysisQueryError(
            "UNSUPPORTED_DIMENSION",
            "IDK未定价汇总按源库存单位分列。",
        )

    read_policy = policy if policy_reader is None else policy_reader
    quote_table = _table if table_quoting is None else table_quoting
    cfg = read_policy()["idk"]
    table = cfg["table"]
    ds = _dataset(table, datasets)
    for field in (
        "id",
        "source_unit",
        "goods_num",
        "piece_num",
        "promotion_price",
        "whse_dept",
        "is_whitelist",
    ):
        _approved(field, ds)

    params: list[Any] = [
        cfg["department"],
        cfg["minimum_quantity_exclusive"],
        cfg["whitelist_value"],
    ]
    sql = (
        "WITH selected AS (SELECT id,COALESCE(source_unit,'未记录') AS unit,"
        "goods_num,piece_num,promotion_price FROM "
        + quote_table(table)
        + " WHERE whse_dept=%s AND goods_num>%s AND is_whitelist=%s AND "
        "(promotion_price IS NULL OR promotion_price<=0)), "
        "scoped AS (SELECT * FROM selected s"
    )
    if filters:
        sql += " WHERE " + _value_filter("s", "unit", filters["unit"], params)
    sql += (
        ") SELECT unit,CASE WHEN COUNT(*)=COUNT(DISTINCT id) THEN COUNT(*) ELSE NULL END AS metric_value,"
        "COUNT(*)-COUNT(DISTINCT id) AS idk_identity_errors,"
        "SUM(CASE WHEN promotion_price IS NULL THEN 1 ELSE 0 END) AS idk_null_price_rows,"
        "SUM(CASE WHEN promotion_price=0 THEN 1 ELSE 0 END) AS idk_zero_price_rows,"
        "SUM(CASE WHEN promotion_price<0 THEN 1 ELSE 0 END) AS idk_negative_price_rows,"
        "CASE WHEN COUNT(*)=COUNT(DISTINCT id) AND COUNT(goods_num)=COUNT(*) THEN SUM(goods_num) ELSE NULL END AS idk_quantity,"
        "SUM(goods_num) AS idk_known_quantity,"
        "CASE WHEN COUNT(*)=COUNT(DISTINCT id) AND COUNT(piece_num)=COUNT(*) THEN SUM(piece_num) ELSE NULL END AS idk_rolls,"
        "SUM(piece_num) AS idk_known_rolls,"
        "SUM(COUNT(*)) OVER() AS idk_scope_rows,NOW(6) AS idk_read_at,"
        "COUNT(*) AS known_value_count,COUNT(*)-COUNT(piece_num) AS missing_value_count "
        "FROM scoped GROUP BY unit ORDER BY unit LIMIT %s"
    )
    params.append(limit + 1)
    return sql, params, {
        "metric": request.get("metric"),
        "dataset": None,
        "source_datasets": [table],
        "dimension_outputs": ["unit"],
        "effective_dimensions": ["unit"],
        "filters": filters,
        "time_range": {"source": "current_snapshot"},
        "warnings": [],
    }
