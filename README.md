# DataSage Mini Next

DataSage Mini Next is a Hermes Agent Profile that combines Hermes' general
assistant capabilities with a governed, read-only business-data plugin.

The relationship is intentionally one-way:

```text
Hermes Agent
  └─ runs the datasage-canary Profile
       ├─ loads DataSage identity and business-analysis Skill
       └─ enables the datasage-query plugin
```

Hermes remains the agent. DataSage adds business semantics and evidence; it is
not a second conversation framework.

## Runtime behavior

### Ordinary conversation

```text
user
  -> Hermes understands the message and conversation history
  -> Hermes answers normally
```

DataSage does not run a turn classifier, require a scope declaration, or
intercept the final answer.

### Business-data question

```text
user
  -> Hermes understands and decomposes the question
  -> datasage_catalog exposes the smallest relevant semantic surface
  -> datasage_entity_resolve clarifies an entity only when necessary
  -> datasage_query validates and executes a governed read-only request
  -> Hermes analyzes the returned evidence and answers the user
```

Follow-ups such as “那华东呢”, “换成按产品看”, and “不是出库，是下单” are
resolved from Hermes conversation history. Every DataSage query remains
complete and stateless from the plugin's perspective.

## Public DataSage tools

- `datasage_catalog` — compact domain discovery and exact metric details;
- `datasage_entity_resolve` — deterministic entity lookup and safe ambiguity;
- `datasage_query` — governed metric execution, typed evidence, and optional
  closed `difference` / `ratio` / `share` observations over compatible scalar
  results from the same batch.

The model cannot submit SQL, table names, fields, joins, formulas, or
unrestricted predicates.

## Supported business domains

- delivery and orders;
- receipts and refunds;
- receivables and aging;
- targets and completion;
- customer-risk evidence;
- current and month-end inventory.

The exact supported metrics and dimensions are versioned in the catalog and
execution contracts. README text is not an authorization source.

## Platform policy

### CLI

CLI is the trusted operator surface. It receives the standard
`hermes-cli` toolset plus DataSage, including terminal, files, code execution,
Web, Skills, memory, delegation, and other Hermes-native capabilities.

### WeCom

WeCom is open to all employees in direct messages and internal groups. It
receives normal conversation, DataSage, Web, browser, vision, Skills, memory,
planning, clarification, delegation, and scheduled-task capabilities.

WeCom does not receive host terminal, file, code-execution, computer-control,
or unconfigured MCP tools. This is a platform boundary, not a restriction on
ordinary conversation or business analysis.

## Configuration

Secrets and connection identity remain in `.env`:

- `DEEPSEEK_API_KEY`;
- MySQL host, port, database, user, password, and optional CA path;
- `WECOM_BOT_ID` and `WECOM_SECRET`.

The same-statement full-partition reconciliation path requires MySQL 8.0 or a
compatible server with window-function support. Production promotion must run
the generated window SQL and `EXPLAIN` against the target read-only database;
offline fixtures do not prove server compatibility or query cost.
Complete change decompositions also keep their independent overall and
partition statements on one `REPEATABLE READ`, read-only consistent snapshot.
Promotion must verify that behavior on InnoDB with a concurrent-write barrier;
mock connections do not prove MVCC consistency.

Behavioral settings live in `config.yaml`, including query budgets, row and
byte caps, TLS policy, production/canary mode, toolsets, and WeCom access
policy.

The v0.12 development Profile intentionally uses progressive hardening:

- the Canary may use the existing production-database account while the
  end-to-end flow is established;
- database operations remain read-only and bounded;
- validation findings should first be observed and measured;
- production promotion later enables the strict release, TLS, and reviewed
  grant gates together.

## Skill model

`skills/datasage/SKILL.md` teaches Hermes when and how to use DataSage. The
Skill is not parsed as executable authorization and does not own a router.

Hermes' bundled Skills are not globally opted out. The installed Profile may
therefore use native Hermes Skills alongside the distribution-owned DataSage
Skill.

## Source validation

Source-maintainer checks live under `evaluation/` and do not belong in an
installed Profile:

```powershell
$env:HERMES_RUNTIME_ROOT='C:\path\to\hermes-agent'
python evaluation/validate_candidate.py
```

The validator runs runtime-context isolation, RDS-gate safety contracts, expert
case contracts, scorer tests, plugin evidence contracts, provider replay
contracts, fair-intelligence contracts, typed fixture and replay-home checks,
the native harness and paired-runner suites, cross-file vocabulary checks, and
an old/new real-Hermes preflight for every typed fixture. It needs no database
or runtime secret, but
`HERMES_RUNTIME_ROOT` is mandatory so a release check cannot silently skip
native integration. `--offline-only` explicitly permits a partial developer
check; its success is not release evidence. Do not replace the validator with
top-level `unittest discover`: recursive test directories are separate suites
and that command does not discover all of them.

Static and mocked tests cannot approve production alone. Promotion requires
the same immutable artifact to pass:

1. static safety and contract checks;
2. natural-language and multi-turn model replay;
3. production read-only database reconciliation;
4. clean installation with exact Hermes and DataSage identities;
5. WeCom canary verification;
6. rollback and roll-forward rehearsal.

## Status

`0.12.0-dev1` is an architecture-reset development candidate. Its first
acceptance target is deliberately small:

1. ordinary conversation works without a DataSage call;
2. one natural-language metric question completes end to end;
3. a follow-up changes the prior data request correctly;
4. a query failure does not block subsequent ordinary conversation.

It is not a production-approved release until all promotion evidence gates
pass on one immutable artifact.
