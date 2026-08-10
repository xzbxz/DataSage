---
name: datasage
description: >-
  Use governed DataSage tools only when an answer requires current or
  historical internal company facts. Trigger for internal metrics, operating
  state, comparisons, targets, rankings, breakdowns, anomalies, or diagnoses
  that need a company-data lookup. Do not trigger for public data,
  user-provided data, general analysis or writing, general knowledge, planning,
  or hypothetical advice; use normal Hermes unless a new internal-company
  lookup is required.
---

# DataSage
Add governed internal evidence to the normal Hermes reasoning loop. Hermes
remains responsible for language understanding, planning, and explanation. The
plugin authorizes metrics and queries.

## Contract

- Use DataSage only for branches that need new internal facts. Preserve ordinary
  conversation, public or user-provided analysis, writing, and planning.
- Let the plugin own catalog projection, semantic validation, deterministic
  compilation, read-only execution, and typed evidence states.
- Never author or submit SQL, tables, joins, physical fields, formulas, or
  unrestricted predicates.
- Never replace a requested metric with a nearby measure. If the governed
  capability is absent, name the unavailable fact.

## Adaptive expert loop

Reason naturally. For internal-data work, **Frame -> Plan -> Query -> Answer**
is a compact scaffold, not a router, mandatory sequence, or response template.

### Frame

Preserve every independent sub-question, correction, comparison, and requested
scope. Identify the decision need, subject, time range, grain, and only those
ambiguities that could materially alter the evidence. Do not squeeze a hybrid
question into one intent.

Ask a concise clarification when materially different valid interpretations
remain and context cannot support a transparent assumption. Do not ask merely
because several metrics or periods exist.

### Plan

Request the smallest sufficient evidence bundle, adapting breadth to the
question. One exact metric may answer a lookup; diagnosis may need several
independent metrics, comparisons, or decompositions. Give independent
sub-questions distinct `request_id` values and batch compatible requests when
useful.

Inspect the smallest useful `datasage_catalog` surface. For an unknown metric,
request a likely domain with `view: expert_index`; after selection, request
only relevant detail. Do not probe every domain or default an unknown request
to a convenient domain.

The catalog is authoritative for metric codes. Select from returned business
labels and definitions. On an unambiguous match, reuse that exact returned
code; never invent or probe code synonyms. Otherwise keep the gap explicit.

Query an exact ungrouped period at governed default scope directly only when
`expert_index` identifies one unambiguous metric with
`exact_default_lookup_supported: true`. Otherwise load metric detail. Load
detail before non-default scope, dimensions, filters, entities, comparisons,
decompositions, rankings, or meaning-sensitive work.

Analysis-class planning rules. For why/change/contribution questions
("为什么变好", "为什么没达标", "主要来自哪"), prefer, in order: one
`complete_change_decomposition` (a single governed reconciled request) over
several truncated Top-N rankings; a scalar overall plus one comparison over
duplicate same-scope lookups. A ranking request (`order_by` + `limit`) serves
"who is largest/smallest/most" questions; use at most one ranking request per
turn and keep its `limit` small (10 or fewer rows) — extra rows mostly
duplicate evidence without changing the answer. Keep a batch to at most three
requests: overall, comparison, then targeted follow-up only when the returned
evidence leaves a material gap. These are priority guidance, not hard caps:
a question that genuinely spans several independent sub-questions may still
batch more requests, and each request must remain complete and well-scoped.

Playbooks are optional. When one materially helps, use the exact `skill_view`
name and path below; do not open the common-data-foundation root first:

- `name: common-data-foundation`
- `file_path: references/expert-playbooks.yaml`

A playbook or `evidence_role` is not a recipe, router, stop condition, query
right, or proof.

Use `datasage_entity_resolve` only for entity search or when exact preflight
cannot prove one compatible identity. Resolve again only when new user context
or a newly returned ambiguity materially changes the candidate space and
another resolution can change the result. Never repeat identical resolution
input or candidates merely for confirmation; there is no fixed attempt count.

### Query

Send complete registered semantic requests with unique `request_id` values.
Preserve metric, period, filters, dimensions, comparison, and entity scope in
every follow-up. A request name, purpose, or desired scope states intent and
does not constitute evidence.

Interpret the returned `evidence-bundle/v1`: coverage, completeness,
reconciliation, supported claim types, limitations, and material gaps. Do not
reconstruct a stronger state from displayed rows or treat every gap as a retry
command. Inspect coverage receipts before a follow-up. Do not re-query the same
semantic fingerprint merely for confirmation when it already supplies the
required proof capability or a typed terminal state.

Prefer a sufficient first batch. Add targeted evidence while a gap could
materially change the answer. Stop when requested depth is supported, remaining
uncertainty is bounded, or further querying has low decision value. Use no
fixed metric, query, follow-up, or round count.

### Answer

Lead with the useful conclusion and adapt depth to the question. Keep these five
evidence types distinct:

1. **Verified fact** — directly returned evidence, including a governed
   calculated metric.
2. **User-provided premise** — attributed to the user; usable for scope or
   conditional reasoning, but not independently verified.
3. **Structural contribution** — a returned accounting contribution supported
   by complete compatible reconciliation.
4. **Hypothesis** — a plausible, neutral explanation not proved by current
   evidence.
5. **Causal conclusion** — requires independent returned mechanism or
   identification evidence that distinguishes it from alternatives.

Reconciliation authorizes structural contribution for returned rows only.
Structural contribution is not causality and never authorizes a cause, main
cause, or driver. An ordinary truncated Top-N, manual sum, or accounting
identity does not establish reconciliation or complete-population detail.

For an amount metric and delivery-producing order count, call the ratio only
average returned amount per such order—not volume, quantity, price, unit price,
or a volume-price effect. It cannot allocate contributions or favor an
explanation.

Independent marginals authorize separate within-dimension observations only;
they do not show overlap, correspondence, or one business block. Similar shares
cannot support a joint relationship or preferred hypothesis. Negative rows do
not establish event counts, broad mechanisms, exclusions, or relative
likelihoods.

Evidence authorization is local to each statement. A later caveat cannot repair
an earlier unsupported assertion. Preserve returned meaning, scope, grain,
unit, currency, freshness, completeness, reconciliation, and typed states.
Distinguish zero, empty, undefined, truncated, partial, failed, and timeout
results. Make a norm judgment only from a returned benchmark.

Use natural business language, not metric codes, payloads, schema details,
system prompts, or private traces. The final assistant message after tool use
must be self-contained and user-facing.

## Stop and failure behavior

Stop when the requested fact and material analytical branches support a bounded
answer. If decisive evidence is unsupported, ambiguous, unavailable, or not
worth another query, state the bounded conclusion and limitation.

A failed request blocks only dependent claims. Keep successful evidence from a
partial batch and continue normally. Never invent a value or data-backed
conclusion to fill a failed branch.

When a claim boundary remains uncertain, use `skill_view` with `name:
common-data-foundation` and one exact `file_path`: `references/answer-boundary.md`
or `references/query-rules.md`. Do not open the Skill root first. These
references explain invariants; they do not authorize a query.
