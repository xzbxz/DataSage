# DataSage contract ownership

This maintainer index classifies distribution content. It is not inserted into
the model prompt and is not an execution authority.

## Hermes-owned behavior

- Hermes owns natural-language understanding, ordinary conversation, planning,
  conversation history, tool selection, follow-up interpretation, and final
  language generation.
- `SOUL.md` defines stable identity, communication principles, product
  boundaries, and local failure behavior.
- `skills/datasage/SKILL.md` teaches Hermes when and how to use the three
  DataSage tools. It is guidance, not an authorization engine.

## Model-visible DataSage surface

- `*/references/planner-contract.yaml` is projected through
  `datasage_catalog`.
- `common-data-foundation/SKILL.md` makes explicitly named shared references
  reachable through Hermes `skill_view`; it is not a standalone workflow.
- `common-data-foundation/references/expert-playbooks.yaml` gives Hermes
  non-binding evidence roles, claim requirements, and continue/stop guidance
  for the six analysis intents. It grants no metric or query authority.
- Model-authored query requests may optionally label a non-authorizing
  `evidence_role`; legacy callers may still carry `analysis_intent`, but it is
  hidden from the model surface. `evidence-bundle/v1` reports deterministic
  proof coverage without generating business conclusions.
- A domain catalog request with `view: expert_index` exposes a compact expert
  discovery index; omitting the view preserves the existing domain summary.
- An exact ungrouped single-metric lookup with an explicit period and no
  filters, entity, comparison, or non-default scope may proceed directly from
  `expert_index` only when that metric returns
  `exact_default_lookup_supported: true`; query validation remains
  authoritative. All other, advanced, or non-default requests load the
  selected metric detail first.
- An exact metric catalog request exposes only that metric's governed
  dimensions, filters, entity roles, time behavior, and comparisons.
- The public execution tools are `datasage_catalog`,
  `datasage_entity_resolve`, and `datasage_query`.

There is no per-turn scope tool, ordinary-conversation classifier, obligation
ledger, goal-type router, deterministic conversation renderer, or hidden
plugin conversation state.

## Plugin-private execution authority

- `plugins/datasage-query/contracts/*-semantics.yaml`: metric meaning, physical
  mapping, fixed filters, joins, formulas, and execution behavior.
- `plugins/datasage-query/contracts/datasets.yaml`: physical allowlist,
  dataset grain, and fixed-scope authority.
- `plugins/datasage-query/contracts/entity-registry.yaml`: stable entity roles,
  aliases, and exact-resolution policy.
- `plugins/datasage-query/contracts/query-policy.yaml`: shared deterministic
  time-range limits.

These execution contracts live in the plugin-private contracts directory and
are read only by plugin code; they are not exposed through `skill_view` or
`skill_manage`. The model never receives or authors SQL, physical datasets,
fields, joins, or formulas.

## Maintainer specifications and evidence

- `common-data-foundation/references/query-rules.md`,
  `answer-boundary.md`, and `entity-rules.md` explain the invariants mirrored
  by contracts, schemas, validators, and behavioral tests.
- Every domain `source-inventory.md` records provenance and source-review
  evidence. It is not model-facing.
- `evaluation/` contains source-only release gates and is excluded from an
  installed Profile.

## Removed authorities

The former conversation policy, Profile capability copy, turn-scope schema,
obligation routing, and model-emitted final-answer envelopes are not retained
as compatibility paths. Historical behavior belongs only in `CHANGELOG.md`.
