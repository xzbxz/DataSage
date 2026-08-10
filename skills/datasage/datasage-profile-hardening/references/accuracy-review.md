# Accuracy Review — recipe and 2026-08-07 findings

Read-only pre-launch review of the 准 (Accuracy) dimension for the
datasage-canary-next profile. Companion to SKILL.md's Accuracy section.

## Identity boundary for live verification

Run live accuracy checks only through the normal authenticated Hermes/DataSage
facade after runtime health reports `ready=True`. Treat
`HERMES_IDENTITY_UNVERIFIED` as a fail-closed release blocker: record the
reason code, repair the release identity through the governed operations
workflow, restart the gateway, and re-check readiness before querying.

Do not import plugin internals, invoke a tool entry directly, or otherwise skip
runtime readiness. Contract/code checks that require no live data may continue
offline while identity is blocked, but they are not substitutes for a governed
end-to-end verification.

## 2026-08-07 baseline findings

### 口径一致性 — 达标
- `delivery_amount` (net delivery): delivery-semantics.yaml formula
  `gross_delivery_amount - return_amount`, components sign +1/−1, disclosure
  required_always. Live check 2026-08: 毛 5,907,661.7092 − 退 78,605.620737
  = 净 5,829,056.088463 — exact reconciliation in CNY.
- `delivery_target_completion` rate: target-semantics.yaml unit_policy now
  "可为负，可超过1，不做截断"; analytical_queries.py L1512-1519 direct
  `actual_value / target_value`, no LEAST/GREATEST/clamp (guarded by test
  test_runtime_hardening.py L917-940). Live check HCM 2026-07:
  7,153,062.03 / 5,480,769.69 = 1.30512 (130.51%).
- Currency: CNY label correct on live probes; original-currency metrics forbid
  summing mixed currencies (`mixed_currency_total: forbidden`).
- Time range: probes returned half-open `2026-08-01..2026-09-01` as expected.

### 数字正确性 — 达标 (4 probes, all self-consistent, cross-reproducible)
- Q1 8月整体净出库: 5,829,056.09 = 毛 − 退, self-consistent.
- Q2 HCM 7月目标完成率: target 548.08万 / actual 715.31万 / 130.51% /
  gap −167.23万, `period_state=completed`, `target_data_state=set`.
- Q3 Mộc Miên 7月出库: 347,464.30 元, role=customer resolved — matches the
  34.7万 figure from the earlier "为什么" analysis session (cross-entry
  reproducibility).
- Q4 HCM 2026-10 (future): `data_state=undefined`, `period_state=not_started`,
  `completion_rate=null`, `actual_amount_rmb=null` — future month returns
  target only, per contract L307.

### 边界处理 — 达标 (code + tests + probes)
- Target zero/missing/incomplete: analytical_queries.py L1487-1491 three-state
  CASE; completion NULL for all three; contract says missing → "未设置目标"
  wording (never "目标为0").
- Negative net delivery allowed, no clamp (contract L322 + disclosure).
- Future periods structurally typed (not_started).
- Top-N truncation: evidence.py L223-227 forces SOURCE_TRUNCATED +
  COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED; executor ranking cap = 10.
- Gross-scope misuse fails closed (GROSS_SCOPE_REQUIRES_EXPLICIT_REQUEST) and
  was seen self-corrected in a real "为什么" session.
- Test baseline: 93 passed / 1 failed — the only failure is the missing
  `evaluation/expert-core/cases.yaml` (distribution excludes evaluation/),
  functional no-op.

### 证据边界 — 达标 (log evidence)
The 20260807_113645 "为什么 HCM 8月出库目标完成这么好" session showed:
- active user-scope correction (8月 in-progress 24.3% ≠ "好"; real good month
  was 7月 130.5%);
- gross-scope hard block → no fabricated number → corrected delivery_scope →
  re-query;
- Top-10 customer ranking labeled "结果按排名截断，只能作为参考" and
  "不能据此说就是这些客户导致超额";
- explicit 可以确定的(事实)/不能确定的(假设) split; no causal claim;
- only 2 query rounds (context-bloat control effective).

### 实体解析 — 达标
- HCM→胡志明 registered_exact in entity-registry.yaml L62-66
  (`canonical_values: [HCM]`); probes returned
  `scope_entities: [{role: department, display_name: 胡志明}]` via
  deterministic preflight (no DB lookup).
- Mộc Miên → customer exact bind.
- HCM vs HCM-HT strict isolation (no prefix expansion).

## Residual items (non-blocking, from 2026-08-07)
1. Future-month `target_data_state=incomplete` with value 0.00 is ambiguous —
   the "not started" meaning comes from `period_state=not_started`, so answer
   quality depends on the planner rule. Option: add a dedicated
   `not_set_for_future` state or document state priority.
2. "超额完成" wording is not code-enforced (disclosure + unit_policy pin it;
   risk low).
3. `production_mode=false` → plaintext TLS warnings; this is an expected gate
   before production (not an accuracy issue).
