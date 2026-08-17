---
name: datasage-query-patterns
description: >-
  Use for DataSage entity-filtered or ranked metric queries.
---

# DataSage Query Patterns

Business-query companion to the governed `datasage` skill. Use it only for
entity-filtered, dimensional, or ranked requests; the main Skill remains the
authority for planning and evidence boundaries.

## When to use

- User asks for a ranked breakdown.
- User provides an explicitly labeled entity token.
- Any governed query needing dimensions, order_by, limit, or metric_filters.

## Governed workflow

1. Load `datasage_catalog` with `view: expert_index` for the domain to pick the
   exact metric code.
2. If the request needs dimensions/filters/ranking (not an exact default
   lookup), load metric detail for that code to confirm allowed dimensions
   and filter support.
3. Put an explicitly labeled entity token directly into `metric_filters`
   when the selected metric exposes that filter. Registered aliases resolve
   deterministically inside query preflight; do not pre-call
   `datasage_entity_resolve` for a labeled token.
4. For ranking: use `dimensions: [customer]` (only user-requested dimensions),
   `order_by: {field: "metric_value", direction: "desc"}`, `limit: N`,
   and a single period (`calendar_month` or `time_range`).
5. Expect `data_state: "truncated"` with limitations
   `COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED` / `SOURCE_TRUNCATED` for a
   Top-N result.

## Bounded analysis

1. Prefer `complete_change_decomposition` over multiple Top-N rankings for
   why/change/contribution questions — one governed reconciled request instead
   of several truncated rankings. Ranking (`order_by`+`limit`) serves
   "who is largest/smallest" only; keep ≤1 ranking per turn and `limit` ≤ 10.
   Batch ≤ 3 requests: overall, comparison, then targeted follow-up.
   These are priority guidance, not hard caps — a question spanning several
   independent sub-questions may still batch more.

## Evidence and scope boundaries

- Do not call `datasage_entity_resolve` pre-emptively. It is only for
  entity-only questions, type-neutral clarification, or after query preflight
  reports an unresolved/ambiguous identity. The catalog must confirm the
  metric capability first.
- A truncated Top-N supports only `observation` / `dimension_breakdown`
  claims for the RETURNED rows. Never claim full-population totals, ranks
  beyond the returned rows, or structural contribution ("driver"/"cause").
  Phrase as: "以上为返回的前 N 名（结果被截断，不代表全量客户完整清单）".
- Do not add dimensions the user did not ask for; an overall total query has
  no dimensions.
- The net delivery metric (`delivery_amount`) = 毛出库 − 同期退货; results
  may be negative. State this口径 when the user only said "出库金额".
- Keep scope consistent across follow-ups: preserve metric, period, filters,
  and dimensions in every re-query.
- 毛出库 metrics (gross_delivery_amount, *_original) are fail-closed:
  `GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST` unless the request carries
  `delivery_scope: "explicit_gross"`; 净出库/退货 use `"default_net"` or omit.
- Future-month target completion returns `data_state: "undefined"`,
  `period_state: "not_started"`, null actual/completion/gap, and a 0.00
  target placeholder with `target_data_state: "not_set_for_future"`. Answer must lead
  with "尚未开始" (from period_state), never read the placeholder as
  "目标不完整" or a real zero target.
