---
name: datasage
description: Governed DataSage company facts; not public/user-provided.
version: 0.15.0-rc10
author: datasage
platforms: [windows]
metadata:
  hermes:
    tags: [data, analytics, business, internal]
    requires_toolsets: [datasage-query]
    requires_tools: [datasage_catalog, datasage_entity_resolve, datasage_query]
    supported_domains:
      - delivery
      - receipt
      - receivable
      - target
      - customer_risk
      - inventory
    non_activation_examples:
      - ordinary conversation or writing
      - public research
      - user-provided tables or files
      - internal policy, HR, or document questions without governed metrics
---

# DataSage Skill

Use this skill only when the answer needs new internal company facts; ordinary
conversation, public research, user-provided data, and writing remain normal
Hermes work. Answer with the smallest sufficient governed evidence. Choose the
route adaptively; this is not a mandatory call sequence.

## When to Use

- Use it for questions requiring new governed DataSage facts, metrics,
  comparisons, rankings, or operating-performance evidence.
- Domains are delivery, receipt, receivable, target, customer_risk, and inventory.
- Do not use it for ordinary conversation, public research, user-provided data,
  writing, or internal policy/HR/document questions that do not require new
  governed operating facts.

## Prerequisites

- The `datasage-query` toolset and its `datasage_catalog`,
  `datasage_entity_resolve`, and `datasage_query` tools must be available.
- Live catalog, schema, and tool results are authoritative for metrics, request
  fields, capabilities, and returned evidence.

## How to Run

On restricted WeCom, use only SOUL, public tool schemas, and returned evidence;
references are never a query prerequisite. On a skill-enabled CLI or maintenance
surface, optionally load a reference with
`skill_view(name="datasage", file_path="references/<file>.md")`; otherwise continue
with the public schema and tool evidence.

## Quick Reference

- [`datasage.query-rules/v1`](references/query-rules.md) owns request construction;
  optionally load it on skill-enabled CLI/maintenance, never as a WeCom precondition.
- [`datasage.entity-guidance/v1`](references/entity-guidance.md) solely owns
  entity resolution, confirmation, and turn boundaries; optionally load it on
  skill-enabled CLI/maintenance for an ambiguous entity, geography, or alias.
- [`datasage.answer-boundary/v1`](references/answer-boundary.md) solely owns
  detailed interpretation and final-answer policy. On skill-enabled CLI/maintenance,
  optionally load `references/answer-boundary.md` before a nontrivial comparison,
  ranking, target, decomposition, causal discussion, multi-row calculation, or
  zero, empty, undefined, truncated, partial, failed, or timeout evidence.

## Procedure

1. Frame the decision, metric meaning, period, grain, and material scope.
2. For a broad operating-performance review, decide whether the optional
   `performance_scorecard` operation of `datasage_catalog` materially improves
   discovery. Use it when the user asks for that standard cross-domain view or
   when its governed candidate lenses are the smallest useful starting point;
   otherwise discover likely domains and metrics directly. When used, call it
   without a preceding `expert_index` catalog operation, choose a material
   subset, keep flows separate from snapshots, and disclose unavailable
   profitability. Only successfully queried lenses are evidence; other material
   lenses remain unassessed and limit the headline.
3. Choose and query the exact governed metric with `datasage_query`; the optional
   [`datasage.query-rules/v1`](references/query-rules.md) reference can help on
   skill-enabled surfaces. Use
   `datasage_catalog` only when the metric is unknown or optional planning detail
   is needed. When entity ambiguity can change the result, use
   `datasage_entity_resolve` and its public resolver contract. On skill-enabled
   surfaces, the optional [`datasage.entity-guidance/v1`](references/entity-guidance.md)
   guidance can help with resolution, confirmation, and turn boundaries. After
   the user selects a candidate, send the selected token to `datasage_query`; the
   query service performs exact identity validation again.
4. Interpret the result with the returned evidence and, when available on a
   skill-enabled surface, the optional
   [`datasage.answer-boundary/v1`](references/answer-boundary.md). Prefer
   decision-relevant comparisons and exceptions over restating rows; use
   decomposition or concentration only when the returned population and
   relationship explicitly authorize it. Multi-row arithmetic also requires an
   available calculation tool.
5. Before concluding a broad review, check material lens coverage once. Stop
   when the conclusion is decision-useful or the remaining uncertainty cannot
   be resolved with available governed operations.

## Pitfalls

- For truncated Top-N evidence, do not turn an unreturned tail into a driver.
- Do not treat the procedure as a fixed planner recipe or mandatory call
  sequence.
- Conversation history, not persistent memory, carries transient analysis. Do
  not save query or empty results, temporary or candidate entity mappings,
  single-turn scope, or operating status. Propose memory only for a stable
  cross-session preference or user-confirmed durable fact. User correction wins.

## Verification

- Confirm each claim with successfully returned governed evidence and the public
  tool schema. An optional reference can guide a skill-enabled CLI/maintenance
  turn, but its absence never blocks a WeCom query or conclusion.
- For broad reviews, confirm material lens coverage was checked once and any
  unassessed material lens limits the headline.
- Stop rather than loop when remaining uncertainty cannot be resolved by
  governed operations.
