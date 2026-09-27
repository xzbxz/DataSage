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

Use the smallest sufficient governed evidence and choose the route adaptively;
this Skill is not a mandatory call sequence.

## When to Use

- Use for new internal company metrics and analysis in the listed domains.
- Do not use for ordinary conversation, public research, user-provided data,
  writing, or unrelated policy/HR questions; these remain normal Hermes work.

## Prerequisites

- Requires `datasage-query` and the three tools listed in metadata.
- Live catalog, schemas and tool results own metric meanings and capabilities.

## Governed analysis

An optional metric `analysis` object carries finite comparisons. `row_filters`
apply to registered row facts; `group_filters` apply after a complete group is
built and before sorting or limiting. Each list has at most 6 items and the
object at most 8. Operators are only `eq`, `gt`, `gte`, `lt`, and `lte`; values
are JSON numbers or bounded decimal strings, never booleans, NaN/Inf, SQL,
columns, functions, unknown fields, or overlong text. The catalog registers
the field, stage, unit, currency, period and population. First fields are
inventory row `price_to_ddp_ratio`, receivable row `overdue_days`, receivable
customer groups `metric_value` and `any_overdue_days`, and target group
`completion_rate`. Unsupported combinations are interface limits, not data
absence; if source semantics cannot support one, report it before substituting
its field or population.

Analysis is bound to metric, entity/cohort, absolute period or snapshot, unit,
currency, population, ledger, completeness and typed states. Follow-ups inherit
those values and change only the axis the user changes. A new read is a new
observation. Group chat keeps each member's language and transient scope
separate. Registered calculations may optionally select `left_field` and
`right_field` (omitted fields keep the compatible `metric_value` default), and
may select only public facts with compatible units, periods, currencies,
ledgers, populations, identities and completeness;
unknown, unmatched and truncated operands remain visible. A user threshold
needs no new KPI approval.
Keep data missing (数据缺失), interface unsupported (接口不支持), business
meaning unresolved (口径未定) and channel limitation (渠道限制) distinct.
Retain the existing `>75%` inventory KPI and its
deduction of all qualified returns; a new historical price slice is not a slice
of that net result and never uses a current promotion price.

Conversation history, not persistent memory, carries transient analysis. Do not
save query or empty results, temporary or candidate entity mappings, single-turn
scope or operating status. Propose memory only for a stable cross-session
preference or user-confirmed durable fact; user correction wins. No arbitrary
formula or new session store is introduced.

## Currency basis

Use `currency_basis: auto` for ordinary amounts; explicit `rmb`/`original` wins.
Auto keeps supported single-currency original evidence and uses governed RMB for
combined currencies. Omission keeps the exact metric; never invent FX.

## Reply language

Follow SOUL's current-user language rule for prose, tables, clarifications and
errors. Translate Chinese tool explanations faithfully; preserve facts, units,
currency, scope and exact entity tokens. Language does not select currency.

## How to Run

On restricted WeCom, references are never a query prerequisite. On a
skill-enabled CLI or maintenance surface, optionally use
`skill_view(name="datasage", file_path="references/<file>.md")` for a relevant
ref; if unavailable,
continue with SOUL, public schemas and returned evidence. Never use arbitrary
file or code execution to bypass the available Skill tools.

## Channel capability

What this session can deliver follows its tools. On WeCom the surface is
clarification, the three governed DataSage tools and Skill reading; files,
public research and free code/SQL are out of scope. State that limit and offer
the operator path. Details: [`datasage.answer-boundary/v1`](references/answer-boundary.md).

## Verification

[`datasage.query-rules/v1`](references/query-rules.md) owns request construction;
[`datasage.answer-boundary/v1`](references/answer-boundary.md) owns typed result
interpretation and disclosure; [`datasage.entity-guidance/v1`](references/entity-guidance.md)
owns entity resolution and turn boundaries; [`datasage.delivery-analysis/v1`](references/delivery-analysis.md)
is optional delivery guidance. Other optional domain methods cover
delivery, receipt, target, inventory, pattern matching, profit, receivable and
cross-domain analysis. Read only a relevant reference when useful; these refs
guide evidence selection and never replace live contracts or require a planner.

Confirm claims against successful governed evidence and the public schema.
Period flows and current snapshots stay distinct. Unqueried, unavailable and
failed lenses remain unassessed; do not turn an unreturned tail into a driver.
Reference absence never blocks a WeCom query. Preserve unknown, empty,
undefined, incomplete, failed and timeout states. Stop when governed operations
cannot resolve the remaining uncertainty.
