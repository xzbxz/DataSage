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

This reference describes delivery meanings and evidence relationships. The
catalog and returned evidence supply metric-specific facts; a catalog
description or user premise is not itself a verified delivery result.

## Delivery semantics

| Meaning | Distinction |
| --- | --- |
| Amount basis | Gross precedes returns; net includes the governed return treatment. |
| Grain and ledger | Order versus line grain, and allocation versus transaction ledgers, are different populations. |
| Warehouse | An all-warehouse scope differs from a named warehouse and its attribution. |
| Business time | Governed event time differs from record creation time; ranges are start-inclusive/end-exclusive, and snapshots differ from flows. |
| External-customer population | The metric contract defines internal/external scope and entity roles. A name does not establish these attributes. |
| Return settlement | Return-settlement periods can differ from the underlying delivery period. |

`pending` and `current-master` describe returned states. A current-master fact
may support current action without representing a completed historical period.

## Evidence relationships

### Benchmarks and comparisons

Evaluative language such as
“behind”, “weak”, “on track”, or “improving” depends on a compatible benchmark.
A benchmark may be a matched prior
period, a governed target, or another returned reference, but it must match the
returned amount basis, grain, warehouse scope, external-customer population,
period semantics, unit, and currency. Without that benchmark, keep the answer
descriptive. A `pending` or `current-master` baseline must be labeled beside
every conclusion that depends on it.

### Breakdowns and reconciliation

Comparisons and breakdowns retain compatible metric, ledger, period,
attribution and population. A customer, order, or warehouse breakdown is a descriptive
distribution of the returned rows; it is not a driver or cause.

Call something a **structural contribution** only when the exact governed
operation returns a complete, compatible, exhaustive reconciliation over the
relevant population and grain, including residuals and an `unknown` bucket when
present. Keep `unknown` in the interpretation and disclose what remains
unattributed. Independent marginal cuts do not prove overlap, correspondence,
or one combined business block.

For Top-N or bounded results, report `requested_limit`, `effective_limit`, and
`has_more` when returned. Truncation alone supplies no population-wide or
structural proof. Apply the population-proof and authorized-relationship boundary
in [`datasage.answer-boundary/v1`](answer-boundary.md); do not treat truncation
as invalidating a relationship that the exact metric's returned evidence supports.

### Hypotheses and causal evidence

A hypothesis is an explanation not established by current facts. Evidence
that distinguishes alternatives can strengthen or weaken it.

Possible discriminating evidence can include order completion or order timing,
receipt/collection timing, receivable aging, return settlement, or another
applicable mechanism exposed by the current catalog. A trend, co-movement,
ratio, denominator, target design, arithmetic, or ordinary breakdown is not
discriminating evidence by itself.

If the requested evidence cannot support the requested relationship because it
is failed, unsupported, ambiguous, stale, partial, pending, current-master, or
truncated without the required proof, preserve the valid baseline, name the
local evidence gap, and state the next discriminating check. Do not silently
replace a failed customer or order check with a department, month trend, or
related amount metric.

### Evidence types and advice

The distinctions between description, structural contribution, hypothesis and
causal conclusion are defined in
[`datasage.answer-boundary/v1`](answer-boundary.md). Reconciliation accounts for
a change or gap; it does not identify its cause.

Recommendations are advice, not approval or execution. High-impact decisions
retain the human approval, assumptions, material risk and review requirements
in SOUL. DataSage does not assign blame or execute resource allocation,
customer treatment or target commitments.

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
