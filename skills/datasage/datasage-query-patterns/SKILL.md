---
name: datasage-query-patterns
description: >-
  Use for DataSage entity-filtered or ranked metric queries.
---

# DataSage Query Patterns

Curator-managed companion to the protected `datasage` skill. The `datasage`
skill (and `common-data-foundation`) are plugin/user-owned and cannot be
patched by the curator; keep confirmed, reusable patterns here instead.

## When to use

- User asks for a ranked breakdown (e.g. "本月 HCM 部门出库最多的客户有哪些").
- User provides an explicitly labeled entity token (部门/客户/业务员/产品/仓库).
- Any governed query needing dimensions, order_by, limit, or metric_filters.

## Confirmed workflow (ranked entity query)

1. Load `datasage_catalog` with `view: expert_index` for the domain (e.g.
   delivery) to pick the exact metric code (e.g. `delivery_amount` 净出库金额).
2. If the request needs dimensions/filters/ranking (not an exact default
   lookup), load metric detail for that code to confirm allowed dimensions
   and filter support.
3. Put an explicitly labeled entity token directly into `metric_filters`
   (e.g. `{"department": "HCM"}`). Registered department aliases resolve
   deterministically inside query preflight — do NOT pre-call
   `datasage_entity_resolve` for a labeled token.
4. For ranking: use `dimensions: [customer]` (only user-requested dimensions),
   `order_by: {field: "metric_value", direction: "desc"}`, `limit: N`,
   and a single period (`calendar_month` or `time_range`).
5. Expect `data_state: "truncated"` with limitations
   `COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED` / `SOURCE_TRUNCATED` for a
   Top-N result.

## Context-bloat control for "为什么/分析" questions

Confirmed 2026-08 (WeCom + CLI replay): analysis questions ("为什么会这么好",
"为什么没达标") inflate context fast — one turn pulled 5 queries incl. two
Top-10 rankings → 35,907 chars tool output → API input jumped 16.5K → 33.7K →
51K tokens by turn 5. The following changes were applied and verified; use the
same shape for future tuning.

1. **Enable Hermes compression first** (cheapest, biggest win): `hermes config
   set compression.enabled true`, `compression.threshold 0.50`,
   `compression.target_ratio 0.20` — these ARE recognized keys, safe via
   `hermes config set` (no Python heredoc needed). Restart gateway after.
2. **Prefer `complete_change_decomposition` over multiple Top-N rankings** for
   why/change/contribution questions — one governed reconciled request instead
   of several truncated rankings. Ranking (`order_by`+`limit`) serves
   "who is largest/smallest" only; keep ≤1 ranking per turn and `limit` ≤ 10.
   Batch ≤ 3 requests: overall, comparison, then targeted follow-up.
   These are priority guidance, not hard caps — a question spanning several
   independent sub-questions may still batch more.
3. **Ranking limit is capped in the plugin**: `tools.py` clamps any
   order_by request to 10 rows (`ranking_cap = 10 if order_by ...`), while
   non-ranking requests keep the env cap (100). This is a safe, non-degrading
   cap — verified by pytest importlib run + behavior matrix.
4. **DO NOT lower `max_result_bytes` / `max_cell_chars` to fight bloat** —
   they are FAIL-CLOSED (`QueryFailure OUTPUT_TOO_LARGE`), not truncating.
   Lowering them turns legitimate large results into hard errors = 降智.
   Leave at defaults (262144 / 2000).

## Testing the plugin on this host

- The package name contains a hyphen (`datasage-query`), so default pytest
  prepend import fails with "attempted relative import with no known parent
  package" (92 collection errors). ALWAYS run:
  `PYTHONPATH=<profile> python -m pytest plugins/datasage-query/tests -q
  -p no:cacheprovider --import-mode=importlib`.
- Baseline (2026-08-07): 93 passed / 1 failed; the 1 failure is still the
  known missing `evaluation/expert-core/cases.yaml` (FileNotFoundError,
  excluded from installed distribution), NOT a regression.

## User preference: optimize without degrading capability

This user's explicit requirement when tuning ("不要给我改降智了"): reduce
bloat/cost/latency WITHOUT reducing answer quality or query capability.
Prefer: compression, planning guidance, small ranking caps. Avoid: fail-closed
output limits, aggressive truncation, hard bans on query patterns that
legitimately span sub-questions. When reporting optimizations, show the
before/after numbers (tool output chars, API input tokens, turn latency) so
the trade-off is visible.

## Pitfalls

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
- Monitoring analysis turns in agent.log: tool output size shows as
  `datasage_query completed (N chars)`; context growth shows as API call
  `in=` tokens. A healthy analysis turn stays ≤ ~3 queries/batch, ≤ ~15K
  chars tool output, and compression triggers before ~50% of context window.
- `hermes chat -q` failing with HERMES_IDENTITY_UNVERIFIED usually means the
  hermes-agent git checkout has uncommitted changes (runtime_health.py
  HERMES_IDENTITY_CHECKOUT_DIRTY, fail-closed by design; `.release/RELEASE.json`
  pins commit/tree_oid/uv_lock_sha256). Fix: commit/stash the hermes-agent
  checkout and restart the gateway. Model-facing failure UX is correct (honest
  no-data answers, no fabrication) but root-cause explanations vary run-to-run
  (one run diagnosed the dirty checkout precisely, another guessed a wrong
  cause) — standardize the user-facing template (error code + likely cause +
  escalation path). Do not invoke plugin internals while readiness is blocked.
- 毛出库 metrics (gross_delivery_amount, *_original) are fail-closed:
  `GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST` unless the request carries
  `delivery_scope: "explicit_gross"`; 净出库/退货 use `"default_net"` or omit.
- Future-month target completion returns `data_state: "undefined"`,
  `period_state: "not_started"`, null actual/completion/gap, and a 0.00
  target placeholder with `target_data_state: "not_set_for_future"`. Answer must lead
  with "尚未开始" (from period_state), never read the placeholder as
  "目标不完整" or a real zero target.

## Session-confirmed example

See `references/entity-ranking-examples.md` for the HCM department → 胡志明
resolution, the exact request JSON shape, and the response/answer phrasing
verified on 2026-08. See `references/context-bloat-optimization.md` for the
compression config, tools.py ranking-cap diff, pytest command, and the
verification matrix from the 2026-08 optimization pass.
