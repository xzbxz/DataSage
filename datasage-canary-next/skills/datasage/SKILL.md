---
name: datasage
description: Internal company facts requiring DataSage lookup only.
---

# DataSage

Add governed internal evidence to Hermes' normal reasoning loop. Hermes keeps
language understanding, planning, memory, tools, and explanation; the plugin
alone authorizes business metrics and queries.

## Boundaries

- Use DataSage only when a branch needs new internal facts. Preserve ordinary
  conversation and public or user-provided analysis, writing, and planning.
- Let the plugin own catalog projection, semantic validation, deterministic
  compilation, read-only execution, permissions, and typed evidence states.
- Never author or submit SQL, tables, joins, physical fields, formulas, or
  unrestricted predicates. Never replace an unavailable metric with a nearby
  measure; name the unavailable fact.
- If a DataSage tool returns `error.code: DATA_ENTITLEMENT_DENIED`, stop all
  further DataSage calls for that turn. If the turn has no independent
  non-DataSage request, the entire final answer must be exactly
  `当前请求未获授权，业务查询未执行。` with nothing added. For a mixed turn with
  an independent ordinary conversation or writing request, render only the
  denied business branch as exactly that sentence and complete each independent
  non-DataSage branch normally. Do not add an administrator, account, caller,
  authorization principal, identity value, contact method, configuration,
  diagnosis, or remediation advice to the denied branch. This rule applies only
  to that code; never reuse it for another failure or ordinary conversation.
- When a required `datasage_*` schema is absent from the current tool table, use
  Hermes' `tool_search` for its exact registered name, `tool_describe` for its
  schema, and `tool_call` to invoke it. Only disclosure changes: authorization,
  query preflight, evidence boundaries, and non-causality rules do not.
  Never guess a tool name or argument.

## Governed expert loop

Use **Frame -> Plan -> Query -> Answer** as a compact scaffold, not a router,
mandatory sequence, or response template. Preserve independent sub-questions,
corrections, comparisons, subject, time, grain, and scope. Ask only about an
ambiguity that could materially change the evidence.

### Select the governed metric

Resolve definition ambiguity before any query. Inspect the smallest useful
`datasage_catalog` surface: normally the likely domain's `expert_index`, then
only the selected metric detail. Select only returned codes from returned
labels and definitions; never invent or probe synonyms.

After `expert_index`, follow its `metric_selection_boundary`. If multiple
materially distinct returned metrics remain compatible, call the official
Hermes `clarify` tool directly. Before the user's clarification response,
metric-detail calls and `datasage_query` calls must both be zero. Querying every
candidate is not clarification. Query both target-completion metrics only when
the user selected both or originally asked for both. If none matches in the
requested domain, treat that as a domain-local gap. Check one other expert
index only when the user's semantics support that single minimal related
domain; otherwise report the gap or clarify. Never enumerate every domain or
infer global absence from one index.

Before any `datasage_catalog` call, route formal receivable turnover days or
formal DSO to the `customer_risk` expert index: the first catalog request for
that branch must be only `{domain: customer_risk, view: expert_index}`. Do not
begin that formal-turnover branch with a `receivable` expert index or summary as
a discovery detour. A request for ordinary net debt, aging, or overdue
receivables without that formal meaning stays in the `receivable` domain.

The default governance path is `expert_index -> metric_detail -> query`. A
direct query is allowed only when `expert_index` identifies one unambiguous
metric with `exact_default_lookup_supported: true` and the request has no
explicit qualifier. Load detail if `requires_metric_detail: true`, if that flag
is false or missing, or if any qualifier exists. `calendar_month` and
`time_range` are explicit qualifiers, as are dimensions, filters, entities,
comparisons, decompositions, and rankings; empty `dimensions: []` is not. If a
full catalog summary has zero or multiple matching defaults, clarify first.
After a successful single-metric `metric_detail`, copy its `content_hash`
unchanged into that query's `detail_receipt`. Preserve the same receipt on
same-metric follow-ups and retries; load fresh detail instead of reusing it for
another metric or domain.

Apply `expert_index -> metric_detail -> query` per newly selected metric branch,
not per turn. Run it for a new Hermes session, changed domain or metric, or no
retained proof of selection plus successful detail. In the same Hermes session,
reuse the prior successful detail receipt only for that metric while runtime
accepts it. If rejected as stale or invalid, refresh only the selected detail;
refresh `expert_index` only if selection becomes unsupported or ambiguous. A
new turn alone never forces refresh.

### Plan and bind scope

Request the smallest sufficient evidence bundle. Give independent requests
unique `request_id` values and batch compatible requests. A scalar may answer a
lookup; a comparison, ranking, structure analysis, or diagnosis can require
more. Prefer one governed `complete_change_decomposition` for a contribution
question over several truncated rankings. Use ranking (`order_by` plus `limit`)
for largest/smallest questions and normally keep one ranking of at most 10 rows.
Normally batch overall, comparison, and one targeted follow-up, but use no fixed
metric, query, follow-up, or round count; depth follows material evidence gaps.

Optional playbooks come only through `datasage_reference` with
`source_id: expert_playbooks` and an exact allowed `section_id`. A playbook or
`evidence_role` is not a router, query right, proof, or stop condition.

Use `datasage_entity_resolve` only for entity search or when exact preflight
cannot prove one identity. Repeat only if new context changes the candidates
and could change the result; never repeat identical input for confirmation.

For `source_exact` dimensions such as `organization` and `customer_region`,
never place unresolved free text in `metric_filters` or entity resolution.
Make one bounded dimension discovery for the same domain, metric, and period
without that filter. Bind only a unique exact returned compatible label with an
auditable link to the user's token, then retry while preserving all other
scope. Multiple candidates require clarification. Zero candidates or truncated
discovery is terminal for that semantic fingerprint and dependent query. Do
not widen time, search other dimensions, fuzzy-match, translate, guess, remove
the filter, or repeat the discovery. Empty data proves neither zero nor a valid
label. A follow-up replacement changes only the binding named by the user.

### Query and investigate

Send complete registered semantic requests. Every follow-up preserves metric,
period, filters, dimensions, comparison, entity scope, and the matching
`detail_receipt`. Purpose or desired scope is intent, not evidence.

Interpret returned `evidence-bundle/v1` coverage, completeness, reconciliation,
supported claim types, seals, limitations, and gaps. Do not reconstruct a
stronger state from displayed rows. Inspect coverage receipts before a targeted
follow-up. Do not re-query the same semantic fingerprint for confirmation once
it supplies the needed proof or a typed terminal state. Stop when the requested
depth is supported, uncertainty is bounded, or another query has low decision
value.

For causal wording, **observe before refuse**: verify each requested governed
observation separately before judging the causal link. Until verified, call it
a user-provided premise. Missing causal evidence blocks only causality, not
supported descriptive branches. Co-movement, reconciliation, an accounting
identity, or independent marginals never authorizes causality, overlap, or a
preferred hypothesis. When a governed dimension category is named without an
entity, query its supported decomposition or change view; report only returned
observation or reconciled structure.

## Answer contract

Lead with the useful conclusion. Keep distinct: verified returned facts;
user-provided premises; reconciled non-causal structural contribution;
hypotheses; and causal conclusions, which require independent returned
mechanism or identification evidence. Reconciliation applies only to returned
rows. A truncated Top-N or manual sum cannot establish full reconciliation.

An amount divided by delivery-producing order count is only average returned
amount per such order, never volume, quantity, price, unit price, or a
volume-price effect. It does not allocate contributions. Judge norms only from
returned benchmarks.

Finalization:

- When `answer_scope_line` is non-empty, faithfully state its actual returned
  range; equivalent wording may not change it.
- For every sealed `disclosure_ledger` item whose `applies` value is `true`,
  fully cover all of its independent business propositions. Natural rewording
  and lossless merging of overlaps are allowed, but never omit, change, or
  broaden an inclusion, exclusion, definition, or conditional scope to another
  request, metric, or domain. Semicolon-separated, coordinated, and conditional
  clauses remain material. Consume only the returned ledger; never start
  another catalog, detail, or query call merely to restate a disclosure. State
  returned applicable unit and currency. Raw JSON is unnecessary.
- For `formal_receivable_turnover_days`, follow the selected metric detail's
  `answer_contract` only when the same returned claim contains a
  `formal-receivable-turnover-calculation-attestation/v1` with `status:
  verified`, a valid `attestation_seal`, and a valid enclosing `claim_seal`.
  State the formal formula only if it confirms both the formula disclosure and
  the two-sided external-customer-scope disclosure are applicable and validly
  sealed. Then present the returned turnover value, average net debt,
  natural-day period, month-end snapshot count, effective-month count, attested
  denominator meaning, and every applicable sealed two-sided scope disclosure.
  It proves participation and coverage, not hidden values: never invent a
  denominator amount. If it is missing, invalid, or `status: undefined`, keep
  the typed state and do not make a formal turnover numeric or component-formula
  assertion. `undefined` or `partial` is a governed result state, not a tool
  error; retain independently sealed non-formula facts and disclosures within
  their bounds. Do not re-query merely to repair or restate this finalization
  contract.
- A new difference, ratio, share, or percentage needs a successful governed
  calculation whose `calculation_seal` covers sealed operands; never derive it
  from displayed values. A fixed-factor display conversion may transform one
  returned value only; preserve meaning, state the unit, and create no metric
  or relationship.
- For every ranked or Top-N result, read the returned `datasage_query`
  `truncated` and `data_state` fields before finalizing. If `truncated: true` OR
  `data_state: truncated`, state that only the requested Top N is returned and
  the source result was truncated; never imply a complete ranking. When
  `truncated` is not `true` AND `data_state` is not `truncated`, do not claim or
  imply that the result is truncated.
- Authorize structural contribution only when `operation` is
  `complete_change_decomposition` AND the returned
  `change_reconciliation.status` is explicitly `reconciled`; a general `status:
  success` does not authorize it. Use “结构贡献” / “structural contribution” or
  a strictly equivalent non-causal accounting term. Link the returned overall
  delta to every partition's contribution amount using only that partition's
  returned `delta_value`; cover all returned partitions using the response's
  reconciliation basis. Call a partition a structural contributor and report a
  rate only when that same returned claim has `structural_contribution` in
  `allowed_relations` and its valid seal covers a returned
  `facts.net_change_contribution_rate`. A zero-delta partition or a claim
  without that relation is not a structural contributor and has no zero rate to
  fill. Never describe structural contribution as a cause, driver, or causal
  explanation. When reporting “结构贡献” for this authorized reconciled
  operation, state in the same paragraph or adjacent sentence:
  “这是净变化的结构分解，不代表业务原因或驱动。” Outside that negated boundary,
  never name a partition with 原因, 驱动, 导致, or causal equivalents. Use each
  authorized returned decimal-string rate directly. It is a signed
  dimensionless fraction: `1` means `100%`; negative values and absolute values
  greater than `1` are valid. Preserve sign and value; for
  percentage display, multiply by 100 exactly once and use one consistent
  display precision across partitions. Preserve every nonzero direction: when
  a nonzero percentage would round to zero, show `0 < rate < threshold` for a
  positive rate or `-threshold < rate < 0` for a negative rate, using the
  smallest positive percentage unit at that precision; never show it as
  `0.00%` or `-0.00%`. Never recompute a rate from visible amounts, scale it
  twice, take its absolute value, clamp it, normalize partition rates to 100%,
  or force them to sum to 100%. If absent, do not calculate, infer, or invent
  it; absence for a zero overall delta or zero partition delta is not a zero
  rate. When the returned `change_reconciliation.status` is `not_reconciled`, or
  when `change_reconciliation` or its status is missing, preserve the returned
  gap or local-result scope and never call it structural contribution.

Only consume governed, sealed evidence and applicable disclosures. Evidence is
local: caveats cannot repair claims. Preserve numbers, sign, scope, grain,
freshness, completeness, reconciliation, limitations, and typed states. Keep
zero, empty, `undefined`, truncated, partial, failed, and timeout distinct.
Missing or invalid proof returns the governed typed `undefined`; never invent a
number. In a user-visible answer for a DataSage business branch, never name,
cite, or reverse-announce that internal governance or implementation artifacts
were hidden: `detail_receipt`, `content_hash`, claim or disclosure seals,
attestation schema IDs, model wire or payload, SQL, schemas, or physical fields.
Keep required natural business disclosures, typed states, truncation, and
reconciliation. A user-visible `metric_id` remains allowed for testing or
clarification. Describe a successful `metric_detail` only as “所选指标详情已返回”.
Never call a contract, evidence, or evidence chain complete unless the same
returned scope has an explicit completeness proof that is sealed and
`applies: true`. These hygiene rules do not affect independent ordinary chat or
the `DATA_ENTITLEMENT_DENIED` rule above. The final assistant message is
self-contained and user-facing.

## Stop and failure behavior

Stop with the bounded answer and explicit limitation when decisive evidence is
unsupported, ambiguous, unavailable, or not worth another query. A failed
request blocks only dependent claims; keep independently sealed successes from
a partial batch and continue normal Hermes behavior. DataSage failure must not
block unrelated conversation. Never invent a value or data-backed conclusion.

When a claim boundary remains uncertain, use `datasage_reference` with only the
exact approved source and section (`answer_boundary`, `query_rules`,
`entity_guidance`, or one domain planner). References explain invariants; they
do not authorize a metric or query. Do not use arbitrary file reading.
