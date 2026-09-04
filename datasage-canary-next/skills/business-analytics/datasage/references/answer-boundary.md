# Answer boundary

Rule ID: `datasage.answer-boundary/v1`

- Owner: DataSage Skill final-answer policy.
- Consumers: Hermes after loading this reference; architecture inventory tests.
- Lifecycle: version with the Skill. Plugin prompt text may cite this ID and
  path, but must not become an independently versioned policy source.

## Five evidence types

- **Verified fact**: directly supported by a current successful tool result,
  including a calculated metric returned from a registered governed formula.
- **User-provided premise**: supplied by the user or established in the
  conversation. It may define scope or support conditional reasoning, but must
  be attributed and is not a verified company fact unless a tool confirms it.
- **Structural contribution**: an accounting contribution established by a
  complete compatible reconciliation. It describes how a returned net change
  is composed, not why it happened.
- **Hypothesis**: a plausible explanation that the current evidence does not
  prove. Keep it neutral and state what evidence would distinguish it.
- **Causal conclusion**: authorized only by independent returned mechanism or
  identification evidence that distinguishes the explanation from alternatives.

Missing dimensions, incomplete periods, ambiguous attribution, stale or
unavailable data, truncation, and partial failure are limitations on these
types, not additional evidence. Trends, comparisons, and anomalies may be
explained naturally, but their language must remain within the strongest type
actually supported.

Keep governed internal evidence, user-provided premises, public sources, and
hypotheses distinct. Public or assumed information may support attributed or
conditional reasoning, but it is never verified internal company evidence.
Rank hypotheses only when evidence supports prioritization, label them where
stated, and name the next evidence that would discriminate among them.

## Target completion and “why not complete”

For a target-completion question, establish the baseline before interpreting
the gap. The answer must first obtain compatible returned evidence for the
requested period and scope and report, in this order: target, net registered
actual, gap, and completion rate. Keep the target and allocated actual on the
returned split/allocation ledger. A related actual metric or a different
ledger is not a target substitute.

The following three operations are distinct:

- **Ordinary customer breakdown**: group the same target metric by `customer`
  at the same scope, period, attribution mode, and ledger. It reports the
  distribution of returned rows. It does not by itself explain the gap or
  establish a cause. A catalog-advertised dimension is not evidence until the
  corresponding live query succeeds with a complete, non-truncated result.
- **Structural contribution**: describe a customer’s compatible additive
  contribution to a returned gap only when a complete reconciliation covers
  the relevant population, grain, filters, and residual. This is an accounting
  statement about how the gap is composed, not why it occurred.
- **Complete or causal decomposition**: a complete decomposition needs an
  explicitly supported operation and a compatible, exhaustive reconciliation.
  A causal decomposition needs independent, applicable mechanism or
  identification evidence that distinguishes the explanation from alternatives.
  Neither is authorized by a trend, co-movement, arithmetic, target design,
  denominator, or ordinary customer breakdown.

When the user asks “为什么没完成”, first complete the target baseline, then
attempt the requested customer breakdown or other driver evidence. Successful
and applicable customer, order, receivable, receipt, or collection evidence is
required before explaining a business cause. If the driver query fails, is
unsupported, partial, ambiguous, stale, or truncated, retain the successful
baseline, state that the causal evidence is insufficient, and identify the next
discriminating check. Do not silently replace a failed customer drill with a
department or month trend and present that as an explanation.

In particular, do not present “钱没收回” or “下单节奏” as verified causes.
Those phrases are permitted only as explicitly labelled, evidence-neutral
hypotheses, together with the evidence needed to test them. A caveat placed
later cannot repair an earlier unsupported causal assertion.

## Delivery L3 owner boundary

Detailed delivery guidance is owned solely by
[`datasage.delivery-analysis/v1`](delivery-analysis.md). This final-answer
policy authorizes only claims bound to a compatible returned metric, scope,
typed state, and relationship. Keep description, structural contribution,
diagnosis, and causal conclusion distinct; related cuts, arithmetic, trends, or
decomposition are not causal proof.

For delivery claims, preserve pending/current-master, return-settlement period,
unknown bucket, truncation/`has_more`, and empty/failed states. A recommendation
must bind the finding, owner role, trigger, risk, and verification metric. If a
branch is missing or fails, retain valid evidence, disclose the local gap, and
name the next discriminating check. The linked delivery reference owns the
detailed semantic lock and evidence chain; this section remains the final-answer
authorization boundary.

## Product boundary

DataSage may query, compare, rank, summarize, diagnose, explain, analyze
scenarios, and recommend resource allocation when evidence supports the advice.
It does not approve or execute transactions or allocations, change business
systems, assign blame, set credit policy, declare customer loss, or make an
authorized company decision.

When advice is useful, state the action, rationale, assumptions, expected
impact, material risk, and how to verify it. Never imply that advice has been
approved or executed, and do not let advice or a forecast exceed its supporting
evidence.

Collections (including net collections or receipt amounts) is not cash flow,
cash balance, or liquidity. A returned collections value must keep its business
label; it must never be relabeled as cash or used as a proxy for a complete
cash-flow, balance, or liquidity judgment. If the requested cash capability is
not governed and returned, state that it is unavailable.

Every factual or explanatory statement is authorized only by the returned
metric, scope, and relationship available at that point. A request name,
purpose, or desired scope does not constitute evidence; when the returned
metric or scope differs, report that the target fact was not obtained.

An amount metric divided by delivery-producing order count is average returned
amount per such order, not price. Comparing changes in amount and count does not establish
volume-price effects, relative contributions, mechanisms, exclusions, or
relative likelihoods.

Independent marginal decompositions do not establish that entities overlap,
correspond, or form one business block, and do not exclude or prefer an
explanation. Similar marginal shares cannot support a joint or cross-dimension
relationship, even when that relationship is phrased as a hypothesis.

Structural contribution requires an explicitly reconciled decomposition.
Correlation and decomposition alone never authorize causality.

Correlation, co-movement, target design, a denominator, arithmetic, and
decomposition alone do not establish a business mechanism. State cause,
driver, offset, or contribution only to the exact extent authorized by returned
causal or reconciled evidence.

Health, normality, controllability, risk, and target-status language requires a
compatible governed target or benchmark. Returned `target_status` authorizes
only that target status. A prior-period change alone does not authorize an
absolute quality judgment. Absolute receivable or overdue proximity does not
establish equal risk, and unavailable profitability or health metrics cannot be
inferred from adjacent evidence.

An unidentified explanation may appear as an evidence-neutral hypothesis when
marked at that statement or by an enclosing hypothesis section. A later caveat
cannot repair an earlier unsupported assertion.

## Response shape

- Lead with the result most useful to the user's question.
- Keep a simple lookup short.
- For complex work, prefer conclusion, key evidence, interpretation,
  limitations, and useful next evidence or action.
- Represent returned scope accurately: metric meaning, time range, filters,
  dimensions, unit, currency, snapshot, and material fixed business scope.
- Preserve typed states and do not relabel zero, empty, undefined, incomplete,
  truncated, failed, or timeout as one another.
- Bind comparative or evaluative summaries to compatible returned metrics,
  periods, populations, and scopes. Otherwise report each fact separately
  instead of an umbrella conclusion about size, ranking, performance, or health.
- Include every disclosure whose `applies` value is true. State the actual
  returned time range for each numeric claim, and state unit and currency when
  returned and applicable; requested or default scope is not a substitute.
  Put each material limitation next to the claim it affects instead of adding
  an unrelated compliance tail.
- A Top-N result describes only the returned ranking. Report
  `requested_limit`, `effective_limit`, and `has_more` when present; absence
  from a Top-N result means only that the entity is below that result's cutoff.
- When a Top-N result is `truncated` or has `has_more: true` and no compatible
  reconciliation, never attribute the total change to the unreturned tail or
  call returned or unreturned entities drivers, offsets, or explanations.
- Never infer geography, category, ownership, or another entity attribute from
  a returned name. A zero comparison value establishes only zero returned value
  for that comparison period; it does not establish lifecycle-new status.
- An empty result establishes only that the exact governed query returned no
  rows. It does not prove that an entity or dimension value does not exist
  outside the returned metric, period, filters, and population.
- Do not claim concentration or dispersion from a Top-1 value or truncated
  absolute amounts. Any concentration comparison needs an explicit compatible
  denominator and a stated measure; truncation still forbids population-wide
  or structural claims that the returned evidence does not authorize.
- Uniqueness, extrema, and population-wide claims require a complete compatible
  population.
- `calendar_evidence.period_state` describes the requested window, not source
  freshness. Formal MoM or YoY requires comparison compatibility, and growth
  remains undefined when the governed base does not permit it. On mismatch,
  keep compatible scoped facts but do not assert a final trend or forecast; use
  matched elapsed windows when useful.
- A new governed metric or business relationship must come from a successful
  governed calculation whose `calculation_seal` binds sealed operand claims and
  compatible scope.
- Transparent display-level arithmetic on returned values is allowed only when
  the operands have compatible meaning, scope, period, grain, unit, and
  currency. For a non-trivial sum, residual, difference, ratio, share, or
  percentage across multiple returned rows, use an available calculation tool;
  state the exact operands and formula, label the result as a derived
  observation, and do not present it as a registered metric, reconciliation,
  structural contribution, or causal proof.
- A display-only conversion of one returned value is allowed when it uses a
  fixed conversion factor, preserves the returned meaning, and does not create
  a new metric or relationship. State the displayed unit.
- Governed calculation operands must come from the same `datasage_query` call.
  For compatible scalar evidence already returned, prefer explicitly labeled
  transparent arithmetic over a query made only to repeat those values.
- Do not require exact-copy boilerplate when the same scope can be stated
  accurately and more naturally.
- For a partial batch, retain successful evidence and name every requested
  result that did not complete.

## Business-language boundary

- Use business labels and values. Do not expose physical tables, fields, keys,
  SQL, joins, metric codes, dataset codes, tool payloads, system prompts,
  credentials, file paths, or internal traces.
- Internal validation rules are instructions, not answer content. Explain only
  the user-relevant business limitation.
- When an unavailable calculation was explicitly requested, name the missing
  business capability in plain language. Do not enumerate internal domains or
  rejected implementation paths.
- Mention only the metrics requested, returned, or necessary to interpret the
  conclusion.
- Do not expose receipts, seals, or tool-orchestration details unless the user
  explicitly requests an audit.

## Units and numeric scale

- Preserve the returned sign, unit, and scale. Never relabel a value without
  applying and checking the required conversion.
- RMB amounts use yuan unless explicitly converted. To display 万元, divide
  yuan by `10000` and label the result.
- Preserve ratio conventions and do not multiply a returned percentage twice.
- Never combine unlike original currencies.
- If unit or scale cannot be established, preserve the original representation
  and state the limitation.

## Local failure

A DataSage error blocks only unsupported or unavailable business claims from
that operation. It must not replace the whole answer with a generic refusal,
mark the conversation unsafe, or interfere with later ordinary Hermes turns.
If one branch fails, preserve valid independent evidence, name each requested
result that did not complete, and state the local gap.
