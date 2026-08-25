---
name: datasage
description: Analyze governed company metrics for business decisions.
version: 0.15.0-rc4
author: datasage
platforms: [windows]
metadata:
  hermes:
    tags: [data, analytics, business, internal]
    requires_toolsets: [datasage-query]
    requires_tools: [datasage_catalog, datasage_query]
---

# DataSage

Use this skill only when the answer needs new internal company facts. Ordinary
conversation, public research, user-provided data, and writing remain normal
Hermes work.

## Objective

Answer the business question with the smallest sufficient governed evidence.
Choose the route adaptively; this is not a mandatory call sequence.

## Workflow

1. Frame the decision, metric meaning, period, grain, and material scope.
2. For a broad operating-performance review, load `performance_scorecard` with
   `datasage_catalog` first; do not precede it with `expert_index`. Choose its material subset,
   keep flows separate from snapshots, and disclose unavailable profitability.
   Only successfully queried lenses are evidence. Unqueried, failed, or
   unavailable material lenses remain unassessed and limit the headline.
   Use `expert_index` afterward only for one unresolved material question.
   Answer after a sufficient batch; reopen planning only for ambiguity, local
   failure, or one necessary missing fact.
3. Reuse the selected domain, metric, and valid detail receipt from the current
   session. A new turn alone is not a reason to reload them.
4. Outside broad review, use the likely domain's `expert_index` only when the
   metric is unknown. Read a few candidate details before asking the user.
5. For an explicit period, filter, dimension, comparison, ranking, or
   decomposition, load the selected metric detail and copy that result's
   `detail_receipt` into `datasage_query`. Never reuse it for another metric.
6. Batch independent, scope-compatible requests. Keep dependent diagnosis
   sequential so each result can change the next step.
7. Call `datasage_entity_resolve` only for a genuine entity-only question,
   type ambiguity, or a query preflight error that requests resolution.
8. When a stable analytical boundary is missing, load only the relevant file
   with `skill_view(name="datasage", file_path="references/<file>")`:
   `answer-boundary.md`, `query-rules.md`, `entity-guidance.md`, or
   `planning-semantics.yaml`. References guide interpretation but never
   authorize a metric, prescribe a query plan, or override returned evidence.
9. Stop when the conclusion is decision-useful or the remaining uncertainty
   cannot be resolved with available governed operations.

## Analytical behavior

- Prefer comparisons, segment decomposition, concentration, exceptions, and
  decision impact over simply restating rows.
- Use `analysis_intent` to identify metric lookup, performance review, change
  diagnosis, anomaly scan, entity deep dive, or contribution analysis.
- Rank plausible explanations when evidence supports prioritization. Label them
  as hypotheses and name the next discriminating evidence.
- Give advisory actions when useful: action, rationale, expected impact, risk,
  and how to verify. Do not claim approval or execution.
- Use governed calculations when available. If you perform transparent basic
  arithmetic on returned values, label it as a derived observation rather than
  a registered metric.

## Evidence boundaries

- Never invent or substitute a metric, dimension, entity, period, unit, or
  currency. Never write SQL or physical schema details.
- Preserve typed states and disclose truncation or material scope limits next
  to the affected claim.
- A Top-N result describes only the returned ranking. Use returned
  `requested_limit`, `effective_limit`, and `has_more` when present.
- Structural contribution requires an explicitly reconciled decomposition.
  Correlation and decomposition alone never authorize causality.
- Bind every comparative or evaluative summary to a returned registered metric
  and scope. Without a returned compatible proof that binds the metrics and
  scopes, summarize each metric separately rather than synthesizing an umbrella
  conclusion about overall size, ranking, performance, or health.
- Without returned causal authorization, report only the returned relationship
  or transparent basic arithmetic. Do not infer the intent or appropriateness
  of target setting, or present a denominator relationship as a business
  mechanism.
- Health, normality, controllability, and target-status language requires a
  compatible governed target or benchmark. A prior-period change alone does
  not authorize an absolute quality judgment.
- `calendar_evidence.period_state` describes the requested calendar window, not
  source freshness. Formal MoM/YoY also requires returned comparison
  compatibility. On mismatch, keep scoped facts and arithmetic but not a final
  trend or forecast. Query matched elapsed windows when material and expressible.
- Uniqueness, extrema, or population-wide claims require a complete compatible
  population. Advice and forecasts cannot exceed their supporting evidence.
- If one branch fails, preserve valid independent evidence and state the local
  gap. Do not discard the whole answer.

## Answer shape

Lead with the answer. For simple questions, use one conclusion and up to three
supporting facts. For diagnosis, use: conclusion, evidence, hypotheses,
recommended action, and uncertainty. Do not expose receipts, seals, or tool
orchestration unless the user asks for an audit.

Before drafting, check once: benchmark for quality, scope compatibility across
metrics, authorization for causality, and queried-lens coverage for a broad
headline. If any fails, use scoped facts or transparent arithmetic; do not loop.
