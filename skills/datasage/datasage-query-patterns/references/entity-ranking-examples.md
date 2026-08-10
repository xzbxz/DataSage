# Entity-Ranking Example (verified 2026-08-07)

Verified against the delivery domain with `datasage-mini-delivery-planner/v11`
and `datasage-mini-delivery-semantics/v14`.

## Business question

"本月 HCM 部门出库最多的客户有哪些？"

Meaning: rank customers by net delivery amount for the current month,
filtered to the HCM department.

## Request shape (datasage_query, metric mode)

```json
{
  "requests": [{
    "request_id": "q1",
    "domain": "delivery",
    "mode": "metric",
    "metric": "delivery_amount",
    "purpose": "查询本月HCM部门净出库金额最高的客户排名",
    "calendar_month": "2026-08",
    "dimensions": ["customer"],
    "metric_filters": {"department": "HCM"},
    "order_by": {"field": "metric_value", "direction": "desc"},
    "limit": 10,
    "evidence_role": "concentration"
  }]
}
```

Notes:
- `calendar_month` expands to start-inclusive/end-exclusive (2026-08-01 .. 2026-09-01).
- `department: "HCM"` resolved deterministically in query preflight to
  display_name 胡志明 (scope_entities role=department). No
  `datasage_entity_resolve` call was needed.
- Only the user-requested dimension (`customer`) was used. No currency
  dimension: `delivery_amount` is a CNY net metric that can aggregate across
  source currencies.

## Response semantics observed

- `data_state: "truncated"`, `completeness: "truncated"`,
  `reconciliation: "not_requested_or_unavailable"`,
  `supports: ["dimension_breakdown", "observation"]`.
- limitations: `COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED`,
  `SOURCE_TRUNCATED`.
- 10 rows returned, sorted descending by metric_value.
- Every row's scope_entities carried the department filter (胡志明).

## Answer phrasing used (works)

"本月（2026年8月1日至8月31日）HCM 部门（解析为"胡志明"部门）净出库金额最高的客户排名如下：
1..N 列表（客户名 + 金额元）
口径说明：金额为净出库金额（毛出库扣除同期退货），人民币计价；排名按金额降序，以上为返回的前 N 名（结果被截断，不代表全量客户完整清单，也未包含未返回客户的明细）。"

## Metric meaning disclosure (from evidence bundle)

`delivery_amount` 净出库金额 = 所选期间毛出库人民币金额 − 同期有效退货人民币金额；
结果允许为负数（disclosure `delivery.net-flow.meaning` applies always).
