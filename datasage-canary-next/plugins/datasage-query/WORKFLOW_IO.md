# Legacy workflow execution adapters

This supersedes the earlier preview-only execution boundary in LEGACY_WORKFLOWS.md.
Preview inputs remain supported, but the fixed adapters now have an actual
production-input path. **No activation file or credentials are shipped.**

This document describes source capabilities, not active deployment or delivery
evidence. Existing private role configuration and runtime activation are separate
from code installation. Installing code does not register a job or prove that a
production recipient received anything.

## Approved compatibility exceptions

On 2026-09-18 the user chose accurate current values over known legacy display
defects, and ordinary reruns that continue incomplete work rather than refreezing
or resending completed notifications. Missing values must not become zero and
missing customer names must not become internal IDs. Long image fields remain
readable instead of restoring clipping.

The production-input adapter and preview now default to `refreeze=False` and
`force_resend=False`, including monthly reports. Explicit local-operator options
are `--refreeze`, `--force-resend`, and `--replay-reason` (5–120 characters).
Refreezing a sending task requires both action flags, and cannot bypass an
incomplete or unknown previous generation. These options do not enable any
existing read, freeze or delivery gate. No schedule invokes them by default.

Pending deliveries created by older code must retain their original sealed
content and receipt bindings. A changed renderer does not authorize invalidating
old fingerprints, relabeling old accepted states, or resending them automatically.

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

Other allowed fields: `recipients_file`, `target_map`, `operation`,
`failure_deliver`, and `schedule` for previously unresolved timing.
`recipients_file` is a compatibility token: the preferred value is
`local/workflow-roles.json`; the old value `legacy-recipients.json` is accepted
only as a marker and never causes a file lookup. The active private role map is
always loaded from Profile-local `local/workflow-roles.json`.
No SQL, table name, arbitrary input filename, freeze week or source identifiers
can be supplied through activation arguments. Normal execution is pinned to the
physical Profile and existing local-operator admission; public query admission
and SELECT-only enforcement remain unchanged.

The old `report_runs/reminder_acceptance/legacy-recipient-reference.json` is an
explicit import source only. Run `roles-check --source <source>` for a read-only
preflight, then `roles-import --source <source> --output <profile>/local/workflow-roles.json`
to create the active file. The importer requires the destination inside `local`,
publishes without overwriting an existing file, and exposes no overwrite option.
Dynamic rosters and buyers are read only when their gate is explicitly enabled.
No credentials or private role values belong in this JSON or Git.

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

Receipts produced by the unified `acceptance_delivery` path carry
`datasage-delivery-binding/v1`, binding the business period,
generation, phase, scope, final target, content, component keys and progress
scope. Only `verified_for_reuse` suppresses a new delivery. A historical
`provider_accepted` record without that binding is classified as
`historical_provider_accepted` and cannot be reused; the old 10/9 acceptance
counts cannot be promoted to a passing new binding.

The official `cronjob` adapter can create a **paused** fixed script/no_agent job
after a separate registration gate. It lists existing names first and does not
create duplicates or alter unrelated/active jobs. UTC+8 is verified. Purchase's
hourly frequency and existing-group intent were user-confirmed on 2026-09-19;
its exact production minute is not yet evidenced. The user has now supplied
a production robot group target; configuration remains disabled. Exact
minute offsets require reviewed configuration before registration; the proposed
test minute :12 is not adopted as a production fact. This method has not been called
against real cron state in this implementation batch.

Purchase's formal entry still defaults to read-only `legacy_database` comparison.
An explicit `profile_local` mode now shares the price reference and batch engine
with sales; its group/initialization boundaries are documented below. Acceptance
storage and the approved test webhook are not a production purchase route.
`production_group_binding=user_supplied_disabled`: the user supplied the target
after pausing historical lookup; old-target equivalence is unproved. No private-recipient fallback or customer/manager workbook is
added to the confirmed hourly group-delivery intent.

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
- The `fabric` path first applies `result_completeness.gate_for_document` to raw
  query packets. When delivery was requested but the gate rejects incomplete
  evidence, the result reports `delivery=blocked_incomplete_report` and exposes
  `report_delivery_gate`; when delivery was not requested it reports
  `delivery=not_requested`. This shared source-completeness gate is separate from
  `report_evidence`'s business source/detail/summary reconciliation.
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

For `slow_task`, all task workbooks, independently verified customer plans, customer
ZIPs and audit preparation materials are prepared before the first send. Dynamic
targets, format/file capabilities and existing component bindings are preflighted
first. Delivery still follows task text/workbook, then customer summary/ZIP, then
the audit generated from actual component receipts. Audit preparation is explicitly
unsent preview material and never overwrites an accepted notification manifest.
Post-send audit or external I/O failure cannot be called a cross-system rollback.

Known component failures may continue through the existing delivery function, with
at most two retries shared by the entire invocation's delivery phase, 5/15 second
backoff, and a 120-second retry-start window. Accepted components are skipped.
Unknown/in-flight results, timeouts, content/target changes, configuration and
preparation errors do not auto-retry. Exhaustion raises the original error to the
official failure path; without configured failure delivery, no responsible-person
notification is claimed. This adds no scheduler, persistent retry queue or job.

The customer transaction window remains the 12-calendar-month window observed at
the first successfully verified plan; that plan is then reused in the same week.
Customer/personnel master data are from the read snapshot, not historical as-of
the inventory freeze. The observation window and snapshot marker are recorded
with the plan; legacy caches without metadata are explicitly identified as such.
Empty source remains blocked as pending source verification, not successful zero
work. Existing valid weekly freezes are reused. The official recurring task never
implicitly refreezes; refreeze/resend still require explicit flags and a reason.

An additional audit-only Customer Coverage workbook is prepared for each product
department, including Summary, Coverage, Exceptions, Unmatched Products and Notes.
It uses existing department executor/manager roles and is not included in the
general sales task attachment. Counts distinguish unique customer IDs, actual
generated-package membership, customer/product/color relations and unmatched
product grains; regional customer counts must not be summed into a global total.
Product department and personnel region remain separate. A completed send is
limited to the prepared components, not a claim of complete business coverage.
Unassigned/no-match cases stay visible in the result and CLI completion message.
The old Products workbook, customer ZIP and dispatch workbook retain their
existing layouts; the coverage notice/file are additional audit components.

## Sealed weekly and monthly report batches

`slow_report` remains a Saturday 19:00 Asia/Shanghai job: weekly reports first,
then progress for the batch's reporting month. Monthly data is not a sum of
weekly reports and is not certified as a month-end financial close. Existing
monthly opening snapshots, whitelist semantics, complete-pool zero projection,
New net-outbound N/A, precise HT values and Detail/Notes formatting are unchanged.
Department observations are independent; the batch does not claim a global
simultaneous database snapshot.

Each phase prepares all department bodies/workbooks and a nonempty recipient
plan before its first send. Explicit target mappings are required even when
preparing an unsent batch, so an unbound preview cannot silently acquire a new
recipient route later. Material is atomically sealed beneath the existing
`report_runs/legacy_execution/slow_report_batches/<week>` private directory.
Bodies, attachment bytes, original observations, accounts and target mappings
are bound together. Publication means prepared material, not provider acceptance.
Failed temporary preparation directories are retained; they are not a send queue.

Normal same-week reruns load the sealed material, not current roles or fresh
report data. Missing/corrupt published content and missing receipt bindings stop
recovery; accepted labels without content bindings are not automatically adopted.
The monthly target plan is inherited from the batch's sealed weekly plan, so a
role edit during weekly recovery cannot silently change monthly recipients.
Current routes for those accounts must still match the frozen routes before send.

Confirmed text/file failures are accumulated locally while other independent
notifications in that phase continue. A failed text does not send its file;
accepted text is skipped when only its file needs continuation. Unknown/in-flight
results stop the phase, with no blind retry or new failure notification. Monthly
sending is blocked until every notification in the current weekly delivery
generation is provider-accepted. No new automatic retry schedule or RetryBudget
is added for `slow_report`; the slow-task-only policy remains separate.

The existing trusted local operator script supports three distinct actions:

- Ordinary `--legacy-run slow_report`: resume the current week's batch. An
  already completed batch sends nothing and does not recollect report data.
- `--force-resend --replay-reason <reason>`: only after both phases are complete,
  advance the delivery generation and resend the same sealed materials. It
  does not refresh the observation, content generation or recipient plan.
- `--regenerate-report --replay-reason <reason>`: only after both phases are
  complete, create a new content generation from fresh observations for the
  same reporting week/month. It is incompatible with force-resend/refreeze.

Reasons are 5–120 characters. `--resume-report-week YYYY-Www` selects an existing
batch only, including cross-week recovery; it cannot create a historical batch
and cannot combine with regeneration. Default execution in a new business week
creates that week's distinct batch; it does not mark older unfinished batches as
complete or automatically replay them. The batch month remains fixed across
month-boundary continuation. A not-yet-prepared monthly phase records its own
actual first observation, not an invented historical as-of time.

These flags are supplied to `scripts/datasage_slow_report.py --legacy-run
slow_report ...`; the fixed official cron adapter remains a no-argument entry.
Read-only prepared output is marked `WORKFLOW_PREPARED_NOT_SENT`, never as a
confirmed delivery. Existing private batches need explicit review for adoption;
code installation does not migrate active state or enable a job.

All weekly report delivery still precedes monthly input execution when delivery
is enabled; unsent preparation may inspect both phases without receipt claims. Same-week
recipient and customer plans are persisted privately. Customer plans rebuild
after an enabled re-freeze or fail
when the accepted source baseline no longer matches. This local envelope does not
claim old runtime cache files were imported. The new app HTTP path is no longer blocked by live-gateway media support, but
production activation and scoped real acceptance remain outstanding.
# IDK weekly continuation (2026-09-19)

The local IDK schedule definition is Monday 12:00 Asia/Shanghai. Editing this
definition does not register, change, enable, or reload a running job.

Only IDK is selected (not IDK-HT): `goods_num > 10`, whitelist `n`, promotion
price NULL or <= 0. This is quantity, not rolls; negative prices remain included.
Counts retain source-row grain, even when product/color repeats. Default scope
is all current unresolved rows, including older entries. An explicit `window_days`
uses SQL `gmt_create >= DATE_SUB(NOW(6), INTERVAL N DAY)` and displays that exact
rolling timestamp cutoff, not a calendar-day cutoff. For a complete empty query,
the audit clock is a subsequent database clock read, explicitly labelled as such.

The first fully validated formal weekly plan seals its observation, complete
record parts, scope/time, executor accounts and exact target bindings. Ordinary
same-week continuation reads this plan, not fresh source data or roles. Changed
target mappings block sending. A new business week selects all still-unpriced
rows again. A read-only preview is not a formal weekly plan and cannot claim
delivery. Old progress without sealed content blocks; there is no automatic
migration or repair of accepted receipts.

Complete zero selection is a sealed, auditable `empty_no_task`, with zero sends.
This intentionally replaces the old zero-product notice. Missing/invalid
executors or routes, incomplete source, invalid identities or bad data are errors,
not zero-task success. This policy is IDK-only: slow_task empty input still stops
as “空源待核验”.

Complete source records are escaped and grouped into UTF-8 byte-bounded parts.
Every part repeats scope/as-of and Part i/N; numbering is global. App Markdown
uses a conservative 4000-byte budget within its 4096-byte limit; callback text
uses 2000 within 2048. An oversized single record blocks before any delivery.
Per-account parts share a notification but have distinct component keys and
receipts. Known failure stops later parts for that account; unknown stops the
run and requires review. Accepted parts are skipped on ordinary continuation.

`--force-resend --replay-reason ...` requires the entire nonempty batch to be
accepted, and resends only its sealed content. It does not requery or regenerate.
There is no same-week reobserve/regeneration command in this implementation;
operators must not delete the plan or change keys to bypass partial/unknown state.
IDK reminders do not advance price acceptance baselines. Production takeover
remains deferred; source installation does not prove a running process loaded it.
# Sales reminders with an explicit Profile-local reference

Approved compatibility exception (2026-09-19): manager workbooks retain customers
with valid identity and the existing product/region/purchase match even when
their sales owner is absent. Display an empty/whitespace Sales value as
`Unassigned` (未分配), matching the English template. This is presentation only:
keep the source owner absent, retain the same customer rows, and do not assign
them to personal sales workbooks. The old manager script's omission is not
restored. Customer relationship row counts are not distinct customer counts.

`operation.reference_source: profile_local` is an explicit local-reference
mode. For sales it requires all four existing regions and the unchanged registered sales
selection/comparison rules. Omission or `legacy_database` retains the original
read-only legacy-reference behavior. Purchase uses the same shared mechanism
through its own explicitly selected mode below. Generic
`operations.execute` / `--accept-snapshot` cannot initialize this mode.

The ordinary fixed `datasage_legacy_sales_price.py` entry selects this mode only
from a separately reviewed local binding. A configuration example, deliberately
inactive and without recipients or credentials:

```json
{
  "kind": "legacy_execution",
  "enabled": false,
  "read_enabled": false,
  "customer_mapping_enabled": true,
  "send_enabled": false,
  "price_accept_enabled": false,
  "register_schedule_enabled": false,
  "operation": {
    "kind": "sales_prices",
    "regions": ["HCM", "HN", "BKK", "IDK"],
    "limit": 10000,
    "reference_source": "profile_local"
  }
}
```

Do not activate this example as part of source installation. A formal local
run that sends requires both `send_enabled` and `price_accept_enabled`, existing
customer-mapping admission, and an explicitly initialized trustworthy reference.
A preview never sends or advances the reference. With no reference, only a
silent-initialization candidate can be previewed; current prices, old database
snapshots and test-accepted snapshots are never imported automatically.

For an operator-supplied JSON object containing `rows` and `provenance`,
`scripts/datasage_slow_report.py --sales-reference-plan <local-json-path>` writes
an initialization review plan. It makes no source query, initializes no head,
and does not prove historical delivery. The first reference's origin and time
must be reviewed before any explicit initialization; the store exposes a
separate reviewed initialization API, not an implicit operation in a reminder.
No CLI activation command or scheduled takeover is introduced here.

Each new batch fixes its before/after prices, observation time, customer/role
evidence, message bodies, complete attachment bytes and resolved destinations
before sending. Pending recovery uses those files and the existing shared
Progress, notification fingerprints and transport; it does not reread source
prices or roles. For example, pending 100→95 remains 100→95 even if the source
later becomes 90. Only after required components are provider-accepted may
the local reference advance to 95; the next observation can then compare 95→90.
Provider acceptance never means human read, customer need, or sales follow-up.

Known partial failures continue only unaccepted components; an accepted body
and failed workbook resume the workbook. Unknown/in-flight results require
review and cannot be bypassed with new content. Target, body or attachment
changes block recovery. No-change, new-key and reentry behavior remains the
existing silent policy; anomalous keys retain their previous reference while
healthy keys follow the already reviewed per-key continuation. No comparison
threshold is added. Exact decimal strings are used in JSON; the old two-decimal
storage guard remains the default for existing database-backed callers.

Reference/pending/commit markers share one atomic JSON head. Immutable batch
evidence retains observation and receipt bindings. The price entries reuse
Hermes' OS file lock; process exit releases it, while old marker locks are
explicitly held for review. The original slow-task, slow-report, IDK and legacy
read-only price flows are not redesigned. Source installation does not start or reload them.

The reminder continues to cover affected regions' existing eligible sales and
customer-service recipients. A salesperson without matched historical buyers
still gets the price body but no workbook. Personal buyer sheets and manager
summaries match the last twelve months by product number **and region**, not by
changed color. They do not represent transactions at the new price or current
customer demand. Existing manager qualification and no-buyer behavior remain.

Still deferred: actual initial reference choice, production takeover time,
hourly minute, and stopping/handing over the old maintainer. This implementation
does not write either business or test databases, create a scheduler, replace
the official transport, or introduce a release pipeline.

## Purchase: shared local reference, one explicit group

The business intent is hourly observation of the existing four-region ready/
active-promotion pool. Purchase identity is product number + color + supplier;
included and excluded tax prices are compared independently in the same recorded
currency and unit. The existing comparator, anomaly handling and new/reentry
silent policy remain authoritative. There are no customer or employee lookups,
personal attachments, manager workbooks or private-message fallbacks.

The fixed `datasage_legacy_purchase_price.py` entry can select
`operation.kind: purchase_prices` and `reference_source: profile_local` through
a separately reviewed binding. It uses the same `PriceReferenceStore` and
`PriceWorkflowEngine` implementation as sales, with separate purchase paths,
schemas, Progress scope and batch bindings. Sales' previous module/API/head
format remain compatible; no existing state is migrated.

Purchase prepares a complete group Markdown batch before any send. Complete
records are stably ordered down then up, with tax-excluded direction first;
opposite tax-side movements both remain visible, and unchanged tax sides are
not displayed. Every part includes the fixed observation timestamp, adjustment
dates, batch count and part i/N. The final escaped UTF-8 message is budgeted at
4000 bytes within the existing Markdown limit; an oversized single record
blocks before sending instead of being truncated or turned into an attachment.

Only explicit successful provider business results may advance the reference.
An HTTP success, helper exit code 0 or arbitrary message identifier alone is
insufficient. A purchase-specific receipt gate delegates to the existing
transport; it adds no HTTP sender or scheduler. Nonzero business codes such as
40058 are failures. Missing/unknown outcomes remain for review. All required
parts must be accepted before the shared atomic reference commit; accepted
parts are skipped on recovery, and a newer source quote cannot replace an
unfinished sealed batch. Empty/truncated source does not clear the reference.

`--purchase-reference-plan <local-json-path>` creates an operator review plan
only, using rows/provenance and exact decimal strings. It does not query a live
source, initialize a purchase reference, activate a group or infer historical
delivery. With no head, an ordinary read-only run may produce a silent
initialization candidate; formal sending remains blocked.

The user supplied a production robot webhook. Its URL belongs only in the
existing private credential JSON's `production_webhooks.purchase_price` entry,
with its own `enabled: false` gate. The separately disabled workflow binding
uses `platform: wecom_webhook`, `target_kind: robot_group`,
`webhook_ref: purchase_price` and a SHA256 `target_ref`; raw credentials never
enter sealed batches or receipts. The transport reuses the existing HTTP,
target-lock and delivery-fence primitives. The acceptance-test webhook remains
separate. Offline verification uses synthetic targets and fake HTTP/transport;
it does not prove online validity or identity with the old group. No personal
target is accepted as a substitute.

Production sending approval, initial reference, exact hourly minute and old-maintainer
handover remain deferred. No actual group lookup/send/upload, database write,
reference initialization, cron registration, gateway restart, Git push or
repackaging is authorized by this code installation.
