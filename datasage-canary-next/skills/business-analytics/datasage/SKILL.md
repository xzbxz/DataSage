---
name: datasage
description: Analyze governed company metrics for business decisions.
version: 0.15.0-rc8
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

## Governing references

- `datasage.query-rules/v1` in `references/query-rules.md` is the detailed
  request-construction policy. Load it with
  `skill_view(name="datasage", file_path="references/query-rules.md")` before
  the first query when its rules are not already present in the current context.
- `datasage.answer-boundary/v1` in `references/answer-boundary.md` is the
  detailed interpretation and final-answer policy. Load it before a nontrivial
  comparison, ranking, target or benchmark judgment, decomposition, causal
  discussion, or answer involving truncated, partial, or failed evidence.
- Use `references/entity-guidance.md` for unresolved entity ambiguity.
- The live catalog, schema, and tool result remain authoritative for available
  metrics, request fields, capabilities, and returned evidence.

## Workflow

1. Frame the decision, metric meaning, period, grain, and material scope.
2. For a broad operating-performance review, decide whether the optional
   `performance_scorecard` materially improves discovery. Use it when the user
   asks for that standard cross-domain view or when Hermes judges its governed
   candidate lenses to be the smallest useful starting point; otherwise discover
   likely domains and metrics directly. When used, call it without a preceding
   `expert_index`, choose a material subset, keep flows separate from snapshots,
   and disclose unavailable profitability. Only successfully queried lenses are
   evidence; other material lenses remain unassessed and limit the headline.
3. Choose and query the exact governed metric under
   `datasage.query-rules/v1`. Resolve an entity only when ambiguity can change
   the result.
4. Stop when the conclusion is decision-useful or the remaining uncertainty
   cannot be resolved with available governed operations.

## Analytical behavior

- Prefer decision-relevant comparisons and exceptions over simply restating
  rows. Use decomposition, concentration, or explanatory language only when the
  returned population and relationship explicitly authorize it.
- For truncated Top-N evidence, follow the hard boundaries in
  `datasage.answer-boundary/v1`; do not turn an unreturned tail into a driver.
- Calculate non-trivial sums, residuals, differences, ratios, or shares across
  multiple returned rows with an available calculation tool. State the operands
  and formula, and label the result as a derived observation.
- Rank and test plausible explanations under `datasage.answer-boundary/v1`.
  Give evidence-bounded advisory actions when useful.

## Persistent memory

Conversation history, not persistent memory, carries transient analysis. Do not
save query or empty results, temporary or candidate entity mappings, single-turn
scope, receipts, or operating status. Propose memory only for a stable
cross-session preference or user-confirmed durable fact. User correction wins.

Before concluding a broad review, check material lens coverage once. Stop rather
than loop when remaining uncertainty cannot be resolved by governed operations.
