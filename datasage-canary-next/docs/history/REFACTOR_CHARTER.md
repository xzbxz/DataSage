# DataSage v0.15 Architecture Stabilization Charter (Historical)

> Historical design input retained for audit. It is not shipped in the runtime
> Profile and is not a current operator instruction.

## Objective

Create one isolated refactor candidate that reduces maintenance cost without replacing Hermes business judgment with a registry, workflow, fixed plan, or answer template.

The running profile at `C:\Users\10192\AppData\Local\hermes\profiles\datasage-canary-next` is read-only and must never be modified by this work.

## Architectural invariants

1. One invalid request branch must not erase other valid results.
2. Public schema acceptance and runtime validation must come from one authoritative capability contract, or be mechanically checked for equivalence.
3. The plugin returns facts, typed states, scope proofs, limitations, and local failures; it never rewrites final model prose.
4. Hermes chooses relevant metrics, sequencing, hypotheses, conclusions, and recommendations. No registry field may encode a fixed workflow or answer.
5. The newest user message always outranks memory, compaction summaries, old tasks, drafts, and prior corrections.

## Registry boundary

Allowed registry content:

- metric identity and business definition;
- unit, currency, period-flow or point-in-time semantics;
- supported dimensions, filters, comparison and calculation affordances;
- population and scope compatibility facts;
- formal target or benchmark availability;
- query adapter identity and receipt inputs;
- evidence authorization and explicit non-authorization.

Forbidden registry content:

- prompt-to-metric routing rules;
- a required number of metrics;
- fixed tool-call sequences;
- answer templates or narrative labels;
- informal high/medium/low thresholds;
- causal explanations, hypotheses, or recommendations;
- instructions that duplicate the main Skill workflow.

## In scope

- a small semantic capability contract and compiler/checker seam;
- alignment of public schema and runtime domain constraints;
- per-request preflight and partial-success preservation;
- compact-before-budget or success-prefix preservation semantics;
- removal of exact-plan release scoring and the disabled conflicting companion Skill;
- release identity and structural tests proving these invariants;
- a host-facing specification/test for compaction ordering if the actual host implementation is outside this profile.

## Out of scope

- new metrics, domains, forecasting, prediction, or recommendation engines;
- rewriting correct metric SQL or database adapters;
- fixed scorecard bundles, fixed call order, or fixed answer wording;
- keyword, regex, synonym, sentence-deletion, or final-answer mutation gates;
- production configuration, credentials, state databases, WeCom delivery, or live canary mutation;
- unrelated cleanup, formatting rewrites, or broad file moves.

## Acceptance criteria

- the existing compact, receipt, decomposition, calculation, and business-contract tests remain green;
- full candidate tests are green with the chosen model identity explicitly versioned;
- schema-valid requests do not fail for missing domain constraints that the schema could express;
- mixed valid/invalid batches return valid results plus local failures;
- raw-size limits do not erase compactable successful evidence;
- the scorecard candidate lenses remain valid and Hermes' metric choice remains adaptive;
- no output mutation hook or fixed-plan equality scorer remains in the release path;
- a normal metric extension has one capability definition, one query adapter, and tests, without editing multiple copies of the same constraint;
- compaction ordering has an explicit failing regression fixture and a host integration boundary;
- no change is made to the running canary.

## Change control

Only findings that violate an invariant or an acceptance criterion may expand this refactor. Other improvements go to a backlog. The main agent alone integrates overlapping changes and decides whether a proposed edit is in scope.
