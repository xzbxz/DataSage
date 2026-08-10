# Accuracy pre-launch audit checklist (datasage-query)

Read-only audit of the datasage-query plugin before canary/production.
Cross-check contract ↔ code ↔ tests ↔ live probe ↔ logs. No file
modifications; 3-5 live queries max.

## 1. Contract ↔ code parity

- `delivery_amount`: `formula: gross_delivery_amount - return_amount`
  (delivery-semantics.yaml), components sign +1/−1; net may be negative,
  no zero clamp.
- target completion: `unit_policy` "可为负，可超过1，不做截断"; SQL must be
  actual/target direct division (tests assert no LEAST/GREATEST/BETWEEN).
- currency: rmb metrics use `*_rmb` columns (aggregatable across currencies);
  original-currency metrics require currency filter/group
  (`never_sum_mixed_currency`), else `CURRENCY_SCOPE_REQUIRED`.
- time: start-inclusive / end-exclusive half-open intervals
  (query-policy.yaml), `calendar_month` expands deterministically.

## 2. Target state machine (analytical_queries.py `_target_completion_query`)

- `period_state`: not_started / in_progress / completed / includes_future /
  includes_in_progress.
- `target_data_state`: missing (rows=0) / incomplete (nulls>0) / zero /
  set; completion and gap NULL for missing/incomplete/zero.
- future month → actual/completion/gap all NULL; answer "尚未开始" from
  period_state, never read the 0.00 placeholder as 目标不完整 or zero target.
- zero target → completion NULL (zero_target_completion_rate: null), never 0%.

## 3. Evidence boundaries (evidence.py)

- truncated → limitations `SOURCE_TRUNCATED` +
  `COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED`; `change_driver` discarded;
  `structural_contribution` only when reconciliation == reconciled.
- ranking cap 10 rows (`tools.py` ranking_cap; limit = min(cap, env, requested)).

## 4. Entity resolution (entity-registry.yaml)

- HCM→胡志明, HCM-HT distinct; bare code never expands to -HT sibling.
- Labeled tokens resolve in query preflight (`scope_entities` carries the
  display_name) — no `datasage_entity_resolve` call needed.
- gross metrics fail closed (`GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST`) without
  `delivery_scope: "explicit_gross"`.

## 5. Governed facade verification

- Run only through the normal authenticated Hermes/DataSage facade after
  runtime readiness reports `ready=True`.
- If identity readiness fails, stop and report the reason code; do not import
  plugin internals or invoke a tool entry directly.
- Once ready, reconcile net == gross − returns exactly (single batch),
  verify a future-period typed state, and verify entity scope metadata.

## 6. Tests & logs

- pytest: `PYTHONPATH=<profile> python -m pytest
  plugins/datasage-query/tests -q -p no:cacheprovider --import-mode=importlib`
  → 93 passed / 1 failed (2026-08-07; sole failure = missing
  evaluation/expert-core/cases.yaml).
- agent.log: '为什么/分析' sessions must distinguish 事实 vs 假设, mark Top-N
  as truncated observations, and stay ≤ ~3 queries/batch; identity guard
  failures appear as `readiness_blocked reason_code=...`.
