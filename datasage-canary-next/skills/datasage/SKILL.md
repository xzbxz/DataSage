---
name: datasage
description: Use governed internal company metrics to answer, diagnose, compare, and advise.
version: 0.14.0-alpha2
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
2. For a broad operating-performance review, load one
   `performance_scorecard` catalog view first. This route takes precedence over
   metric-unknown routing: do not call an `expert_index` before the scorecard
   call completes. Query only its material subset, keep period flows separate
   from current/latest snapshots, and disclose that profitability is
   unavailable rather than inferring company-wide health. After the scorecard,
   use an `expert_index` only for a material question it leaves unresolved, and
   state why the extra lookup is necessary.
3. Reuse the selected domain, metric, and valid detail receipt from the current
   session. A new turn alone is not a reason to reload them.
4. Only when the request is not a broad operating-performance review and the
   metric is unknown, call `datasage_catalog` with the most likely domain's
   `expert_index`. Read a small number of candidate details before asking the
   user when their definitions can resolve the ambiguity.
5. For an explicit period, filter, dimension, comparison, ranking, or
   decomposition, load the selected metric detail and copy that result's
   `detail_receipt` into `datasage_query`. Never reuse it for another metric.
6. Batch independent, scope-compatible requests. Keep dependent diagnosis
   sequential so each result can change the next step.
7. Call `datasage_entity_resolve` only for a genuine entity-only question,
   type ambiguity, or a query preflight error that requests resolution.
8. Use `datasage_reference` only when metric detail lacks necessary planning
   guidance. Read only source/section pairs returned by its index.
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
- Health, normality, controllability, and target-status language requires a
  compatible governed target or benchmark. A prior-period change alone does
  not authorize an absolute quality judgment.
- If one branch fails, preserve valid independent evidence and state the local
  gap. Do not discard the whole answer.

## Answer shape

Lead with the answer. For simple questions, use one conclusion and up to three
supporting facts. For diagnosis, use: conclusion, evidence, hypotheses,
recommended action, and uncertainty. Do not expose receipts, seals, or tool
orchestration unless the user asks for an audit.
