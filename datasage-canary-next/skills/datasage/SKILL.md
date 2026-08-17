---
name: datasage
description: Internal company facts requiring DataSage lookup only.
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
- When a required `datasage_*` schema is not a direct entry in the current tool
  table, use Hermes' `tool_search` to find its exact registered name,
  `tool_describe` to read its schema, and `tool_call` to invoke it. This changes
  only tool disclosure: authorization, query preflight, evidence boundaries,
  and non-causality rules remain unchanged. Never guess a tool name or argument.

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

Resolve metric or definition ambiguity before any `datasage_query`, unless
existing context transparently supports one interpretation. Catalog inspection
may clarify choices but does not authorize a query while materially different
metrics remain. Do not run source-exact discovery until one metric is selected.

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

Use a full catalog summary's unique exact governed default when it matches the
user's wording and no explicit qualifier has higher priority; if zero or
multiple governed defaults match, clarify before any `datasage_query`.

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

Playbooks are optional. When one materially helps, use the bounded
`datasage_reference` facade with `source_id: expert_playbooks` and one exact
enumerated `section_id` such as `playbook.change_diagnosis`. The facade is
available on WeCom and cannot read an arbitrary path.

A playbook or `evidence_role` is not a recipe, router, stop condition, query
right, or proof.

Use `datasage_entity_resolve` only for entity search or when exact preflight
cannot prove one compatible identity. Resolve again only when new user context
or a newly returned ambiguity materially changes the candidate space and
another resolution can change the result. Never repeat identical resolution
input or candidates merely for confirmation; there is no fixed attempt count.

Treat `source_exact` dimensions such as `organization` and `customer_region`
as governed source labels, not entities. Never send unresolved free text in
`metric_filters`, and do not route it through `datasage_entity_resolve`. First
run a bounded dimension discovery at the same domain and period without that
filter. Bind only a unique exact compatible candidate for which the returned
evidence provides a transparent and auditable binding to the user's token;
then retry the original operation with that exact returned label and preserve
its metric, domain, period, dimensions, and other filters. Ask the user to
clarify when there are multiple compatible candidates. Treat zero compatible
candidates or a truncated discovery as unresolved and stop the dependent
query. Stop discovery on its typed outcome and semantic fingerprint: do not
repeat the same discovery, and use no fixed discovery count. Do not guess,
fuzzy-match, translate, or remove the filter. An empty business result is not
proof of a zero or proof that the unresolved free text was a valid source
label.

Allow one completed bounded discovery per semantic fingerprint. Its terminal
typed outcome governs that same dimension, domain, metric, and period; do not
search additional dimensions or widen the period to rescue an unresolved
token. Zero compatible candidates or a truncated discovery immediately stops
the dependent query. Never submit the unresolved token in `metric_filters`.
This is a per-fingerprint terminal rule, not a fixed catalog or discovery count
for unrelated evidence needs.

On a follow-up that replaces a source-exact binding, replace only the binding
named by the user and preserve every other exact token and query scope. Do not
reinterpret an unchanged token merely because a new value was supplied for a
different binding.

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

For causal wording over requested internal facts, **observe before refuse**.
Verify each requested governed observation with the smallest catalog surface
and its governed query before judging the causal link. For example, verify a
target gap and a product change as separate evidence branches when both are in
the question. Until returned evidence confirms them, label each as a
user-provided premise. Do not replace observation queries with an early causal
refusal. Missing causal evidence blocks only the causal claim; it does not
block supported target-gap, change, or other descriptive queries. Returned
co-movement or reconciled structure may support observation or composition but
does not authorize causality.

When the user names a governed dimension category without a specific entity,
query that dimension's governed decomposition or change view when the selected
metric supports it. The missing entity name does not authorize skipping the
descriptive branch. Report only the returned observation or reconciled
structure; it does not authorize causality.

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

Finalization:

- Include each `applies: true` disclosure and the actual returned time range;
  include unit and currency when returned and applicable.
- A new difference, ratio, share, or percentage requires a successful governed
  calculation with a `calculation_seal` bound to sealed operands; never derive
  one from visible values.
- A display-only unit conversion may use a fixed conversion factor only for one
  returned value; preserve meaning, state the new unit, and create no new metric
  or relationship.
- For every ranked or Top-N result, read the returned `datasage_query`
  `truncated` and `data_state` fields before finalizing. If `truncated: true`
  OR `data_state: truncated`, explicitly disclose that only the requested Top
  N is returned and that the source result was truncated; never imply a
  complete ranking. When `truncated` is not `true` AND `data_state` is not
  `truncated`, do not claim or imply that the result is truncated.
- Authorize a structural-contribution conclusion only when `operation` is
  `complete_change_decomposition` AND the returned
  `change_reconciliation.status` is explicitly `reconciled`; a general
  `status: success` does not authorize it. Then use “结构贡献” / “structural
  contribution”, or a strictly equivalent non-causal accounting term, in the
  final answer. Link the
  returned overall delta to the contribution amount for every returned
  partition and to its contribution rate only when the response returns that
  rate; cover all returned partitions using the response's reconciliation
  basis. Never describe structural contribution as a cause, driver, or causal
  explanation. If the response does not return a contribution rate, do not
  calculate or invent one. When the returned `change_reconciliation.status` is
  `not_reconciled`, or when `change_reconciliation` or its status is missing,
  preserve the returned gap or local-result scope and never call it structural
  contribution.

Evidence is local; caveats can't fix claims. Preserve returned meaning/scope/grain,
freshness, completeness, reconciliation, and typed states; keep zero/empty/
undefined/truncated/partial/failed/timeout distinct. Judge norms only from
returned benchmarks.

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

When a claim boundary remains uncertain, use `datasage_reference` with the
exact approved source and section needed: `answer_boundary`, `query_rules`,
`entity_guidance`, or one domain planner source. These references explain
invariants; they do not authorize a metric or query. Do not use a broad
file-reading fallback.
