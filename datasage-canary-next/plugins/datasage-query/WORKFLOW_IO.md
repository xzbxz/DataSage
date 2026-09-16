# Legacy workflow execution adapters

This supersedes the earlier preview-only execution boundary in LEGACY_WORKFLOWS.md.
Preview inputs remain supported, but the fixed adapters now have an actual
production-input path. **No activation file or credentials are shipped.**

## Activation and fixed entrypoints

Each existing `datasage_legacy_<job>.py` still defaults to
WORKFLOW_EXECUTION_NOT_ENABLED. `--preview` retains the existing file preview.
Without that flag it resolves its fixed report ID in the existing
`local-report-bindings.json`; it no longer remains an unconditional stub.

A selected binding uses `kind: legacy_execution`. All gates default false:
`enabled`, `read_enabled`, `customer_mapping_enabled`, `freeze_enabled`,
`send_enabled`, `price_accept_enabled`, `register_schedule_enabled`.
`read_enabled` does not authorize sending, freezing, accepting prices or reading
customer/personnel mappings. Sending the task/sales-price workflow requires the
mapping gate because those legacy outputs depend on buyer/recipient evidence.

Other allowed fields: `recipients_file` (exactly `legacy-recipients.json`),
`target_map`, `operation`, `failure_deliver`, and `schedule` for previously unresolved timing.
No SQL, table name, arbitrary input filename, freeze week or source identifiers
can be supplied through activation arguments. Normal execution is pinned to the
physical Profile and existing local-operator admission; public query admission
and SELECT-only enforcement remain unchanged.

The private recipient file uses the previously extracted old mapping, not new
hand-designed recipients. Dynamic rosters and buyers are read only when their
gate is explicitly enabled. No credentials belong in this JSON or Git.

## Implemented finite write action

`workflow_io.freeze_current_week` implements the actual MySQL transaction using
injected I/O for tests and the concrete `open_freeze_writer` factory for a live
authorized run. It obtains the database clock/current ISO week itself; callers
cannot pass rows, SQL, arbitrary tables or a backfill identifier. It uses the old
per-week GET_LOCK with a ten-second wait, then reads source/existing rows through
the existing consistent-snapshot read executor and the existing freeze planner.

The only DML is DELETE for that week and one bulk INSERT into
`vk_ai.slow_moving_baseline`. Saved count, IDs, source/version, validity and a
single non-null current-week frozen_at are checked before commit. Failures before
commit roll back. A lost commit acknowledgement is **unknown**, not a claimed
rollback, and blocks another automatic freeze attempt pending review. The freeze
commit is separate from subsequent delivery; a later send failure does not undo it.

The writer **cannot reuse the read-only account**. It requires separately
provisioned `DATASAGE_FREEZE_MYSQL_HOST`, `DATASAGE_FREEZE_MYSQL_USER` and
`DATASAGE_FREEZE_MYSQL_PASSWORD`, obtained via the official secret loader.
Host/port must match the existing source; database is fixed to vk_ai; authenticated
user, server UUID and TLS are checked. The existing validated driver and TLS
policy are reused. The normal read connection still verifies read-only grants.
No GRANT, credential installation or connection-policy change is performed.
Database-side writer permission provisioning remains an external prerequisite;
the adapter does not claim those privileges already exist.

## Delivery and progress

The finite Profile application HTTP transport is now implemented; see
[APP_NOTIFICATIONS.md](APP_NOTIFICATIONS.md). Set an explicit `wecom_app_http`
target mapping to use application text and media delivery from script children.
This bypasses the live-gateway requirement without modifying Hermes or starting
a WebSocket. The limitations below apply only to the retained OfficialTransport.


`OfficialTransport` calls the official live adapter's `send` / `send_document`
through Hermes' own `_dispatch_on_gateway_loop`, retaining its queue and rate
controls. It contains no HTTP sender, token refresh engine or standalone socket.
The target map must explicitly map each legacy account to `platform` / `chat_id`;
callback targets additionally name the expected `app_name` and corp prefix.
The adapter rechecks enabled configuration before every real send.

Preflight checks all targets and required media support before sending. Current
official limitations are concrete:

- `wecom_callback` only implements text `send`; it inherits the unsupported
  document stub. Its text path slices content to 2048 characters, so longer text
  is rejected by this adapter before truncation.
- Smart Robot `wecom` supports media in a live gateway. Its inspected standalone
  sender opens a second WebSocket when not in-process (potentially disconnecting
  the question-answering bot) and does not forward `media_files`. This route is
  explicitly refused rather than used as a workaround.
- The current official script/no_agent child process does not share the live
  adapter reference. Consequently fixed script delivery reports
  OFFICIAL_LIVE_GATEWAY_REQUIRED_NO_STANDALONE_FALLBACK until a supported official
  in-process integration is available. This is a remaining architecture blocker,
  not a claim that changing a platform name makes the old channel equivalent.
- WebSocket group sends need a cached passive reply context; callback app/user
  resolution is checked against the intended app, not silently the first app.
  Raw legacy webhook URLs are not accepted as bot chat IDs. Unsupported target
  types remain blocked; no old webhook sender is copied.

Business component progress lives under Git-ignored
`report_runs/legacy_execution`. It is not a send queue. An audit intent is saved,
then in_flight is persisted before I/O, then the official evidence/status and
message ID are saved. Known text success plus file failure retries only the file.
Unknown/in-flight/unverified outcomes stop before any component is retried, even
under force-resend. Per-job overlap uses a finite lock like the old task lock;
a stale lock requires review, not blind deletion. Human receipt remains unknown.

The official `cronjob` adapter can create a **paused** fixed script/no_agent job
after a separate registration gate. It lists existing names first and does not
create duplicates or alter unrelated/active jobs. UTC+8 is verified. Missing
purchase timing and sales minute offset must come from the old external runtime
configuration; known schedules are reused. This method has not been called
against real cron state in this implementation batch.

## Production data and reports

- Task inputs come from the old bounded source specs, not report_inputs files.
  Without freeze permission, only the current existing week is consumed, with
  source/identity/time checks and no MAX-week fallback. Customer mapping can be
  off while a local task workbook is generated.
- `workflow_inputs.customer_mapping` implements the legacy notification buyer
  relationship with bound product/department pairs, a 12-calendar-month window,
  external/bulk-or-HT rules, active customer and employee lookups, ambiguity and
  completeness checks. The current exclusive observation end is retained. This
  operational notification relationship does not replace the governed historical
  customer metric. No new roster/customer data was fetched during development.
- Weekly/monthly report input uses the current governed query path. Complete
  product/SKU/unit/sales flows are required. The old Detail columns are restored
  using unique private source labels; unknown/ambiguous labels stay Unknown.
  Scope totals and sales/unit totals are taken from returned facts, not repeated
  across rows. All weekly regions precede monthly regions; incomplete results fail
  instead of being sent as a complete report. Existing query/wire limits still
  apply, so a large region may require future partitioning rather than relaxing
  the read boundary.
- IDK and price workflows invoke existing operations directly. First observations
  do not invent price changes. Customer mapping is separately gated. Real price
  baseline acceptance is a final, separately enabled step after allowed delivery
  succeeds; it is never implied by read permission or preview generation.
- `fabric_report` consumes current governed packets and exports populated source
  tables with four source-backed static PNG charts embedded in XLSX. It preserves
  read/ETL times, distinct occurrence/contribution rates, known subsets and unknown
  values. It can reuse saved observations without re-querying. It does not invent
  formal responsibility, loss, quality, repeated-return or recursive lineage facts.

## Explicit remaining gaps

Real freeze, send, price-acceptance and scheduler activation are untested and off.
The supported live-gateway delivery bridge retains its upstream limitations.
The Profile application HTTP bridge now implements child-process text/file sending
via the old application API. It is disabled and untested against real recipients;
explicit existing-app target configuration and scoped acceptance are still needed.
Full-scale report partitioning and historical missing display-label recovery
are not claimed complete. Mixed-success customer audits now select confirmed
versus unconfirmed packages using the shared executor/manager recipient rule.
Formal fabric responsibility/quality/lineage still lacks production evidence.
These are distinct from missing runtime settings; they are not hidden behind an
"enable" toggle. Existing previews, original observations and unknown outcomes
are retained for individual acceptance.

Production task delivery precedes customer lookup/package execution; all weekly
delivery precedes monthly input execution. Same-week recipient and customer plans
are persisted privately. Customer plans rebuild after an enabled re-freeze or fail
when the accepted source baseline no longer matches. This local envelope does not
claim old runtime cache files were imported. The new app HTTP path is no longer blocked by live-gateway media support, but
production activation and scoped real acceptance remain outstanding.
