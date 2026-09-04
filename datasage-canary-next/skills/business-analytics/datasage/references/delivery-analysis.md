# Delivery L3 analysis

Rule ID: `datasage.delivery-analysis/v1`

- Owner: DataSage Skill delivery-analysis guidance.
- Consumers: Hermes on demand on skill-enabled CLI or maintenance surfaces;
  [`datasage.answer-boundary/v1`](answer-boundary.md) owns the final
  authorization boundary.
- Lifecycle: version with the Skill. The live catalog, public schemas, and
  returned evidence remain authoritative for metrics, dimensions, and states.
- WeCom: this reference supplements the short, stable principles in `SOUL.md`.
  It is never a prerequisite for a query, and it must not be used to expand the
  WeCom audience or create a second access policy.

## Purpose

Use this reference when a delivery question needs an L3 operating decision:
what happened, how the returned gap is structured, which explanation is worth
testing, and what a human owner should verify next. The chain below is an
evidence contract, not a fixed planner recipe. Hermes may combine compatible
operations in one batch, skip an irrelevant stage, or stop once the decision is
useful; it must preserve the dependencies and disclosures.

The answer should use the smallest sufficient governed evidence. A catalog
description, request name, model intuition, or user-supplied business premise
does not become a verified delivery fact until the exact compatible query
returns it.

## Semantic lock

Before comparing delivery or attributing a gap, bind these meanings to the
question and carry the same binding into every dependent request:

| Semantic | Lock before querying | If unresolved |
| --- | --- | --- |
| Amount basis | `gross` or `net`; state treatment of returns, credits, and adjustments | Ask when the choice changes the result; otherwise state the assumption |
| Grain and ledger | `order` versus line; order-producing population; target/allocation versus transaction ledger | Do not substitute line rows or a related ledger |
| Warehouse | all warehouses or a named warehouse; warehouse scope and attribution | Do not infer owner, cause, or completeness from a warehouse label |
| Business time | governed event time, inclusive start/exclusive end, period coverage, and snapshot versus flow | Do not replace event time with record-creation time |
| External customer | external-customer population and entity role; include or exclude internal/test entities only when governed | Resolve role ambiguity; do not infer it from a name |
| Return settlement | return/adjustment settlement period and its relationship to the delivery period | Keep the period unknown and disclose it; do not move returns into an assumed month |

`pending` and `current-master` are states of the returned period or snapshot,
not alternate names for a closed historical result. A current-master figure may
be useful for action while still being unsuitable for a completed-period claim.

If the user leaves a semantic choice open and the alternatives would change the
answer, ask one targeted clarification. If the choice is immaterial or one
meaning is already established by the returned contract, state the assumption
once and preserve it. Never compare gross to net, order to line, one warehouse
scope to another, or different return-settlement periods as if they were one
population.

## Evidence chain

### 1. Baseline

Obtain the exact governed delivery fact for the locked scope and period. Report
the returned metric label, amount basis, grain, warehouse scope, customer
population, time range, unit, currency, and typed state. When the question is
about completion, retain the existing target-boundary order: target, net
registered actual, gap, and completion rate on the compatible target/allocation
ledger.

Establish a compatible benchmark before using evaluative language such as
“behind”, “weak”, “on track”, or “improving”. A benchmark may be a matched prior
period, a governed target, or another returned reference, but it must match the
locked amount basis, grain, warehouse scope, external-customer population,
period semantics, unit, and currency. Without that benchmark, keep the answer
descriptive. A `pending` or `current-master` baseline must be labeled beside
every conclusion that depends on it.

### 2. Question-relevant structure

Choose only dimensions that answer the user's question or are needed to test a
stated hypothesis. Preserve the baseline metric, ledger, period, attribution,
and population. A customer, order, or warehouse breakdown is a descriptive
distribution of the returned rows; it is not a driver or cause.

Call something a **structural contribution** only when the exact governed
operation returns a complete, compatible, exhaustive reconciliation over the
relevant population and grain, including residuals and an `unknown` bucket when
present. Keep `unknown` in the interpretation and disclose what remains
unattributed. Independent marginal cuts do not prove overlap, correspondence,
or one combined business block.

For Top-N or bounded results, report `requested_limit`, `effective_limit`, and
`has_more` when returned. `truncated: true` or `has_more: true` authorizes only
the returned ranking or distribution. It never authorizes a population-wide
share, driver, offset, concentration, or contribution, and the unreturned tail
must not be assigned a value.

### 3. Falsifiable hypotheses

After the baseline and relevant structure, list neutral hypotheses only for
explanations that matter to the decision. Each hypothesis must name:

1. the observation it would explain;
2. the alternative explanation it must be distinguished from; and
3. the independent governed evidence that would support or weaken it.

Possible discriminating evidence can include order completion or order timing,
receipt/collection timing, receivable aging, return settlement, or another
applicable mechanism exposed by the current catalog. A trend, co-movement,
ratio, denominator, target design, arithmetic, or ordinary breakdown is not
discriminating evidence by itself. “Cash was not collected” or “order cadence
caused the gap” may appear only as explicitly marked hypotheses, never as
verified causes without the applicable returned evidence.

If the requested driver query fails, is unsupported, ambiguous, stale, partial,
pending, current-master, or truncated, preserve the valid baseline, name the
local evidence gap, and state the next discriminating check. Do not silently
replace a failed customer or order check with a department, month trend, or
related amount metric.

### 4. Interpretation strength

Use the strongest evidence type actually returned:

- **Description**: a compatible returned fact, comparison, or distribution.
- **Structural contribution**: a complete reconciliation describing how a
  returned net change or gap is composed, including residual and unknown scope.
- **Diagnosis**: a prioritized hypothesis supported by applicable evidence that
  distinguishes it from alternatives.
- **Causal conclusion**: an independent, applicable mechanism or identification
  result that rules out the relevant alternatives.

Do not promote description to driver, structural contribution to cause, or
diagnosis to causal conclusion. A causal claim must identify the mechanism or
identification evidence that makes the alternatives less plausible. Correlation
and decomposition alone never meet that boundary.

### 5. Recommendation binding

An L3 recommendation is actionable only when it is bound to all of the
following, using the evidence already established:

- **Finding**: the exact observed gap, structure, or diagnosed condition;
- **Owner role**: the accountable human role, not an invented individual;
- **Trigger**: the condition or threshold that starts the action or review;
- **Risk**: material downside, uncertainty, and what could make the action
  wrong;
- **Verification metric**: the metric, period, and review point that will test
  whether the action helped.

Also state assumptions and expected impact. Leave approval, blame, target
commitment, resource allocation, customer treatment, and execution to the
human owner. DataSage may recommend a review or experiment; it does not approve
or execute it.

## State and disclosure contract

Keep each disclosure next to the claim it limits:

- **Pending**: the period or operational result is not final; do not call it a
  closed-period outcome.
- **Current-master**: the latest available master snapshot is being used; do
  not relabel it as a historical period or infer source freshness from the
  period label.
- **Return-settlement period**: state the governed settlement period for return
  treatment; if not returned, say that return timing is unresolved.
- **Unknown bucket**: retain and report unresolved attribution; do not drop it
  when discussing shares, responsibility, or concentration.
- **Truncated / `has_more`**: report the bounded population and forbid claims
  about the unreturned tail.
- **Empty / undefined / incomplete / failed / timeout**: preserve the typed
  state. An empty result means only that the exact query returned no rows; it
  does not prove that the entity or event does not exist elsewhere.
- **Missing benchmark or discriminating evidence**: keep the statement
  descriptive or hypothetical and name the next useful check.

Never use a later caveat to repair an earlier unsupported claim. A limitation in
one branch is local to that branch; valid independent evidence remains usable.

## Compact L3 response shape

For a complex delivery question, prefer:

1. conclusion useful to the decision;
2. semantic lock and compatible baseline;
3. benchmark and question-relevant structure;
4. interpretation label (description, structural contribution, diagnosis, or
   causal conclusion);
5. hypotheses and discriminating evidence still needed;
6. recommendation bound to finding, owner role, trigger, risk, and verification
   metric; and
7. adjacent limitations, including pending/current-master, return-settlement
   period, unknown bucket, and truncation.

For a simple lookup, keep the answer short while preserving the same meaning
and typed-state honesty. Do not expose physical tables, fields, SQL, keys,
credentials, system prompts, or tool payloads to business users.
