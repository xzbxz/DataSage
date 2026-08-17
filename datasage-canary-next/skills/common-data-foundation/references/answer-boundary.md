# Answer boundary

> Maintainer specification for data-backed claims. It does not govern ordinary
> conversation and does not replace Hermes' final language generation.

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

Hermes may organize and explain these elements naturally. The plugin returns
evidence and typed states; it does not require the model to emit a private JSON
answer envelope or surrender control of normal conversation.

## Product boundary

DataSage may query, compare, rank, summarize, diagnose, explain, analyze
scenarios, and recommend resource allocation when evidence supports the advice.
It does not approve or execute transactions or allocations, change business
systems, assign blame, set credit policy, declare customer loss, or make an
authorized company decision.

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
- Include every disclosure whose `applies` value is true. State the actual
  returned time range for each numeric claim, and state unit and currency when
  returned and applicable; requested or default scope is not a substitute.
- A new difference, ratio, share, or percentage must come from a successful
  governed calculation whose `calculation_seal` binds sealed operand claims and
  compatible scope. Do not derive it from visible values.
- A display-only conversion of one returned value is allowed when it uses a
  fixed conversion factor, preserves the returned meaning, and does not create
  a new metric or relationship. State the displayed unit.
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

## Units and numeric scale

- Preserve the returned unit and scale. Never relabel a value without applying
  and checking the required conversion.
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
