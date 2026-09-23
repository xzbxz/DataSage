---
name: datasage
description: Governed DataSage company facts; not public/user-provided.
version: 0.15.0-rc14
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
      - inventory
      - profit
      - pattern_matching
    non_activation_examples:
      - ordinary conversation or writing
      - public research
      - user-provided tables or files
      - internal policy, HR, or document questions without governed metrics
---

# DataSage Skill

Use this skill only when the answer needs new internal company facts; ordinary
conversation, public research, user-provided data, and writing remain normal
Hermes work. Use the smallest sufficient governed evidence. Choose the route
adaptively; this is not a mandatory call sequence.

## When to Use

- Use it for new governed facts, metrics, comparisons, rankings, or operating-
  performance evidence in delivery, receipt, receivable, target, inventory, profit, or pattern_matching.
- Do not use it for ordinary conversation, public research, user-provided data,
  writing, or internal policy/HR/document questions without governed metrics.

## Prerequisites

- The `datasage-query` toolset and `datasage_catalog`,
  `datasage_entity_resolve`, and `datasage_query` must be available.
- Live catalog, schema, and tool results are authoritative for metrics, fields,
  capabilities, and returned evidence.

## How to Run

On restricted WeCom, references are never a query prerequisite. When the
session exposes native `skill_view`, optionally load a relevant reference with
`skill_view(name="datasage", file_path="references/<file>.md")`. On a
skill-enabled CLI or maintenance surface, use the same optional path. If the
tool or reference is unavailable, say so when relevant and continue with SOUL,
public tool schemas, and returned evidence; do not use arbitrary file or code
execution to bypass the available Skill tools.

## Channel capability

What this session can deliver follows its tools, not this Skill. On WeCom the
surface is clarification, the three governed DataSage tools and Skill reading,
with Skill writes staged behind approval: reading user-supplied files, producing
file output (documents, spreadsheets, PDF, decks), public research, and free
code or SQL are out of scope there. State that limit and offer the operator path
instead of describing a deliverable the channel cannot produce. Details:
[`datasage.answer-boundary/v1`](references/answer-boundary.md) — Channel capability boundary.

## Quick Reference

- [`datasage.query-rules/v1`](references/query-rules.md) owns request construction;
  load it only on skill-enabled surfaces, never as a WeCom precondition.
- [`datasage.entity-guidance/v1`](references/entity-guidance.md) owns entity
  resolution, confirmation, and turn boundaries; load it when ambiguity matters.
- [`datasage.answer-boundary/v1`](references/answer-boundary.md) owns detailed
  interpretation of comparisons, targets, calculations, and typed result states.
- [`datasage.delivery-analysis/v1`](references/delivery-analysis.md) is optional
  supplemental guidance for L3 delivery, not a fixed planner recipe or WeCom query prerequisite.
- The following candidate method references are also optional and are loaded
  only when the question materially uses their domain: [`datasage.receipt-analysis/v1`](references/receipt-analysis.md), [`datasage.target-analysis/v1`](references/target-analysis.md), [`datasage.inventory-analysis/v1`](references/inventory-analysis.md), [`datasage.pattern-matching-analysis/v1`](references/pattern-matching-analysis.md), [`datasage.profit-analysis/v1`](references/profit-analysis.md), [`datasage.receivable-analysis/v1`](references/receivable-analysis.md), and [`datasage.cross-domain-analysis/v1`](references/cross-domain-analysis.md) for intentional multi-domain questions.
- These candidate references organize contract-backed evidence selection and
  answer boundaries. They do not add a planner, require a fixed metric set, or
  replace the live catalog/schema/results. No channel must load one before
  every query; if a reference is unavailable, proceed with public tool
  schemas, returned evidence, and the existing SOUL rules.

## Tool facts

- The catalog provides registered metric and capability facts. Its scorecard
  view contains optional candidate lenses, not a complete company-health result.
- `datasage_query` independently validates the requested metric, scope and
  identity; it returns facts, states and evidence relationships.
- `datasage_entity_resolve` provides bounded identity candidates and search
  scope. Query validation rechecks the selected identity.
- Unqueried, unavailable or failed material lenses remain unassessed. Period
  flows and current snapshots represent different time scopes.

## Pitfalls

- For truncated Top-N evidence, do not turn an unreturned tail into a driver.
- Conversation history, not persistent memory, carries transient analysis. Do
  not save query or empty results, temporary or candidate entity mappings,
  single-turn scope, or operating status. Propose memory only for a stable
  cross-session preference or user-confirmed durable fact. User correction wins.

## Verification

- Confirm each claim with successfully returned governed evidence and the public
  tool schema. An optional reference can guide any turn where native Skill
  reading is available, but its absence never blocks a WeCom query or conclusion.
- The candidate references are not evidence of real WeCom Skill loading or
  business approval. B01 live-channel consumption, independent numeric oracle,
  and owner sign-off remain separate verification items.
- Stop rather than loop when remaining uncertainty cannot be resolved by
  governed operations.
