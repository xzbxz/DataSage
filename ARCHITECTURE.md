# Hermes Agent × DataSage Architecture

## Product model

The system has three distinct identities:

1. **Hermes Agent** is the upstream runtime and intelligent agent.
2. **DataSage** is a product capability made of a plugin, business contracts,
   a Skill, evaluations, and release controls.
3. **datasage-canary** is one isolated Hermes Profile that installs a pinned
   DataSage release for pre-production verification.

DataSage is not a fork of the Hermes agent loop. The Profile configures and
extends Hermes through its official Profile, plugin, Skill, toolset, provider,
and gateway surfaces.

## Ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Hermes | language understanding, conversation, planning, multi-turn context, tool selection, final response | DataSage metric definitions or SQL |
| DataSage catalog | registered metrics, dimensions, filters, entity roles, time and comparison capabilities | conversation classification |
| DataSage execution | validation, compilation, read-only database access, typed results | natural-language routing or final prose |
| DataSage Skill | usage guidance and analysis procedure | executable authorization or hidden state |
| Profile | model, enabled plugin, platform toolsets, gateway policy, behavior settings | business truth duplicated from contracts |
| Release/DSRT | immutable identity, install, health evidence, activation, rollback | runtime mutation of an installed artifact |

The primary architectural test is:

> If the DataSage plugin is unavailable, the Profile remains a usable Hermes
> assistant. Installing DataSage adds governed data capability without taking
> over the agent.

## Runtime flows

### Ordinary turn

```text
user message
  -> Hermes conversation history and language understanding
  -> Hermes plans or answers
  -> user response
```

No DataSage hook, router, classifier, or final-answer transformer participates.

### Data turn

```text
user message
  -> Hermes understands the business question
  -> Hermes decomposes metric, period, dimensions, filters and comparison
  -> datasage_catalog (only the smallest relevant domain/metric view)
  -> datasage_entity_resolve (only if exact identity is unavailable)
  -> datasage_query
       -> semantic validation
       -> parameterized query compilation
       -> bounded read-only execution
       -> typed evidence
  -> Hermes interprets evidence and writes the response
```

### Multi-turn data flow

Hermes interprets follow-ups from the canonical conversation. It submits a
complete typed query on every DataSage call. The plugin does not infer the
current topic by scanning prose and does not keep a parallel session state
machine.

```text
“看看本月销售”
  -> complete query A
“那华东呢”
  -> Hermes applies region=华东 -> complete query B
“换成按产品看”
  -> Hermes changes dimensions -> complete query C
“不是出库，是下单”
  -> Hermes corrects metric -> catalog check -> complete query D
```

## Public plugin surface

DataSage exposes exactly three model-facing tools:

- `datasage_catalog`;
- `datasage_entity_resolve`;
- `datasage_query`.

Catalog discovery is two-level:

- `{"requests":[{"domain":"delivery"}]}` returns a compact domain summary;
- `{"requests":[{"domain":"delivery","metric":"<exact_code>"}]}` returns one
  metric's detailed capabilities.

This avoids sending the full semantic catalog on every turn while keeping
capability discoverable through Hermes' stable tool surface.

## Semantic authority

Semantic truth is one-directional:

```text
planner contracts -> catalog projection
execution semantics + datasets + entity registry -> validation/compiler
query policy -> shared deterministic limits
```

SOUL, README, and Skill prose may explain these contracts but may not introduce
another metric definition, route, formula, physical mapping, or authorization
rule.

The following former authorities do not exist in v0.12:

- mandatory per-turn scope declarations;
- conversation keyword whitelist;
- unknown-to-data and unknown-to-delivery defaults;
- obligation and goal-type ledgers;
- Profile capability copies;
- Skill-substring authorization;
- deterministic ordinary-conversation rendering;
- model-emitted private final-answer envelopes;
- whole-session poisoning after a failed tool call.

## Evidence boundary

DataSage query results distinguish rows, verified zero, empty, undefined,
truncated, partial, ambiguous, failed, and timed-out states.

Hermes may choose wording and structure, but data-backed claims must preserve:

- exact metric meaning;
- time or snapshot scope;
- filters and dimensions;
- unit, currency, sign, and numeric scale;
- data limitations and partial failures;
- whether a driver is reconciled, observational, hypothetical, or unknown.

The evidence boundary activates only for claims derived from a current
DataSage result. It never governs ordinary chat or unrelated knowledge.

## Failure isolation

| Failure | Required behavior |
| --- | --- |
| catalog lacks a metric | explain the unsupported business measure; do not substitute |
| entity is ambiguous | return safe candidates and ask one clarification |
| database unavailable | fail the current lookup; preserve normal Hermes |
| one batch item fails | answer successful items and disclose the missing item |
| plugin unavailable | normal Hermes continues; current internal lookup is unavailable |
| evidence incomplete | omit unsupported company claims; conversation continues |

No failure may mark the whole session unsafe or require a DataSage call on the
next unrelated turn.

## Platform assembly

### CLI

The trusted CLI uses the official `hermes-cli` toolset plus DataSage. This
preserves terminal, files, code execution, Web, browser, Skills, memory,
delegation, cron, vision, and planning for operator workflows and engineering
review.

### WeCom

WeCom is open to all employees and internal groups. It keeps conversational and
analytical capabilities, DataSage, Web/browser, Skills, memory, delegation,
vision, and cron.

Host terminal, file operations, code execution, desktop control, and unrelated
platform-admin tools are absent from the WeCom toolset. The distinction is
fixed when the session is created so Hermes' per-conversation prompt prefix and
tool surface remain stable.

## Configuration

`config.yaml` owns non-secret behavior:

- Hermes model and reasoning settings;
- platform toolsets;
- WeCom access policies;
- query budgets and row/byte caps;
- canary/production, TLS, and grant policy;
- memory, Skill, session, and logging behavior.

`.env` owns credentials and deployment connection identity:

- provider API key;
- WeCom bot credentials;
- database host, database, account, password, and optional CA path.

The plugin reads DataSage behavior only from `config.yaml`. Environment
fallbacks for production mode, TLS policy, grant scopes, timeouts, row or byte
budgets, and other behavior are forbidden; only credentials and deployment
connection coordinates use `.env`.

Hermes session history is automatically pruned after 30 inactive days.
Application logs remain local to the operator host and are not exposed through
the WeCom toolset; Hermes 0.19 rotates them by size rather than calendar age.
An exact 30-day application-log deletion SLA therefore belongs to the host
operations layer and is a production-promotion gate, not a claim of this
Profile configuration.

## Release identity

A release manifest binds:

- DataSage source commit and tag;
- Profile payload hash;
- plugin and contract hashes;
- exact Hermes version, commit, tag, and tree;
- build and evaluation evidence.

An installed Profile is materialized atomically from one immutable artifact.
Source files, release manifests, DSRT `CURRENT`, runtime view, and the launched
Hermes checkout must identify the same release unit. Hand-copying files over a
live Profile is not a deployment mechanism.

## Progressive hardening

Development proceeds in this order:

1. ordinary conversation;
2. one metric end to end;
3. multi-turn correction and comparison;
4. multi-domain semantic coverage;
5. shadow evidence and quality observation;
6. local deterministic validation;
7. production TLS, grant, identity, canary, and promotion gates.

New restrictions require a reproducible failure, a single owning layer,
behavioral regression coverage, and proof that unrelated Hermes capabilities
still work.
