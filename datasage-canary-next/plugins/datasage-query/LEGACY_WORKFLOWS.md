# Legacy workflow review and fixed adapters

Execution development has advanced: see [WORKFLOW_IO.md](WORKFLOW_IO.md).
The preview behavior below is retained; production I/O now has separate, default-off
per-job gates and concrete adapters. APP_NOTIFICATIONS.md describes the added
Profile application HTTP path, including text/file recovery and target ordering.

Behavior baseline: local Git object `3fd38fcb803307e1688688ca1dfbde271131157a`
(`release/datasage-0.2.0`). No historical script with import-time side effects is
imported. `contracts/legacy-workflows.json` holds the extracted schedules, regions
and workflow facts; actual receiver identifiers remain outside Git.

## Execution boundary

All six fixed script adapters default to exit 2, `WORKFLOW_EXECUTION_NOT_ENABLED`.
The reference policy does not activate execution. Preview calls register no jobs,
invoke no sender, accept no real price baseline and perform no MySQL writes.
The preview freeze simulator still accepts only in-memory `sqlite3.Connection`;
the separate gated production writer is documented in WORKFLOW_IO.md.
The public query executor's SELECT-only policy is unchanged.

These are fixed report-ID adapters for the official script/no_agent path, not
Windows startup services or a replacement scheduler. One shared helper performs
the dispatch, because the official script runner does not pass arbitrary CLI args.

| Script | Fixed report ID | Legacy candidate timing (Asia/Shanghai) |
|---|---|---|
| datasage_legacy_slow_task.py | legacy-slow-task | Tuesday 09:00 |
| datasage_legacy_slow_report.py | legacy-slow-report | Saturday 19:00; monthly follows weekly |
| datasage_legacy_idk.py | legacy-idk | Monday 19:00 |
| datasage_legacy_sales_price.py | legacy-sales-price | Hourly; exact minute not present in this Git object |
| datasage_legacy_purchase_price.py | legacy-purchase-price | Not recoverable from this Git object |
| datasage_legacy_fabric.py | legacy-fabric | On demand; no proved legacy scheduled job |

For explicit local previews only, add `--preview`. The adapter consumes exactly
`report_inputs/legacy/<job>.json`, where job is slow_task, slow_report, idk,
sales_price, purchase_price or fabric. Inputs must declare evidence_origin as
synthetic or existing_local_observation. It emits a fresh directory under
Git-ignored `report_runs/legacy/`, including a manifest and the actual XLSX/PNG/ZIP
where applicable. It does not load database credentials. Missing inputs fail
closed. The existing generic report entry also accepts `--legacy-preview <job>`;
it rejects combining this with report-id or price-snapshot acceptance.

## Recovered legacy behavior

- Task wrapper defaults to same-week re-freeze and full re-send. Manual no-refreeze
  and no-force-resend were legacy overrides. A plain baseline read reuses an
  existing valid week; the Tuesday wrapper deliberately requests replacement.
- Freeze takes qualifying ODS source rows individually: goods_num > 10, explicit
  is_whitelist=n, configured eight departments, unique source_row_id, version 2.
  It does not first aggregate smaller source rows past the threshold. The original
  MIN_INVENTORY_ROWS=1000 constant is not an executed freeze predicate.
- Original write contract: per-week advisory lock with 10-second wait, transaction
  replacing only the requested week, validation of saved count, version/source
  identity and one frozen_at, then commit or rollback and release. This contract
  is represented in the dry-run plan, not executed against a business database.
- Calendar labels use UTC+8 ISO weeks, Monday 09:00 to Saturday 19:00. They do not
  replace the current query's recorded frozen_at/read_at flow bounds.
- Task recipients merge configured executors, active sales/customer-service
  employees in each region's configured departments, and configured managers.
  Accounts are deduplicated while roles are retained. No active dynamic sales
  for a populated task region is an error. Ordinary same-week execution reuses
  the saved V2 recipient plan; legacy dry-run recomputes for inspection.
- Weekly/monthly recipients are configured executors plus managers. Weekly
  skips already confirmed text/file components unless force-resend is requested.
  The Saturday wrapper always forces the following monthly refresh.
- Customers match purchased product + warehouse department, then receive all
  matching baseline colors. Images may combine regions for one customer's same
  product/color. One PNG per customer and one ZIP per sales account; displayed
  rolls use legacy Decimal ROUND_HALF_UP. Unrounded quantities remain in task
  data. ZIPs over 20,000,000 bytes fail; no invented automatic splitting rule.
- Customer image ZIP names contain customer number/name. Dispatch workbooks
  have one sheet per sales owner and the legacy Customer No / Customer columns.
  Successful-region audits go to executors; all-failed audits additionally go
  to managers. A preview marks its simulated statuses, never claims delivery.
- Legacy V2 text/file progress markers can be translated into scoped receipt
  evidence. Corrupt or wrong-scope documents fail; log 'ok' is not a receipt.
  Official SendResult with positive evidence maps to provider_accepted only;
  bare success remains unverified, timeouts remain unknown, human read remains
  unknown. No progress store or sending queue is reimplemented.
- IDK repeats the current full unpriced candidate list each scheduled run by
  default. Source NULL/zero/negative are distinct in current query evidence;
  Promotion Offer displays Not Set for NULL/non-positive. A source-row count is
  not relabeled as a unique-product count.
- Sales-price messages cover all active sales/customer-service staff in affected
  regions. Only confirmed buyer lists produce customer attachments, one sheet
  per product. Manager lists use configured executor role matches plus the old
  fixed manager mapping; a regional manager attachment requires regional buyers.
- Purchase messages group decreases before increases, use tax-excluded delta
  first when both prices change, and show only changed tax sides. Missing/basis
  changes remain explicit under current semantics, not invented price movement.

## Attachments and presentation

The legacy standard-library XLSX emitter is isolated in legacy_xlsx.py so the
runtime does not depend on a Codex-only authoring environment. It preserves the
old task columns, Detail worksheet layout, freeze panes/filter and thin borders /
landscape options. Added presentation support is limited to Decimal numeric cells,
Chinese column widths/wrapping and explicit one-decimal percentages. PNG uses the
installed Pillow and a Windows CJK font; oversized or unrenderable cells fail
instead of being silently clipped. Customer PNG currently caps at 5,000 rows.

Business fields are not recalculated by a generic report engine. Complete,
assessable preview details are arithmetically reconciled to their supplied summary;
unknown/partial fields remain unverified rather than filled with zero. Report
messages preserve the key sections but do not restore a causal business-improvement
claim from stock movement alone. HT quantity display is supported when the
corresponding per-unit facts are provided; missing high-quantity facts stay unknown.

`operation_preview_input` consumes existing local IDK/price observations. Without
an accepted comparison baseline it does not produce an actual price-change claim.
Purchase adjustment time uses source_modified_at, never observed_at. Older saved
observations lacking that field stay unknown; they are not rewritten or re-queried.

## Explicit differences and pending integration

Current confirmed semantics override conflicting legacy fallbacks: missing source
unit is not replaced by promotion unit; missing rolls are not zero; missing customer
name is not replaced by a raw internal ID; current stock-state precision/presence
rules and exact high-net-roll values remain intact. These are disclosed differences,
not silent changes to freeze selectors or schedule times.

The old sender used a WeCom self-built-app API / webhook. The Profile now has a
finite, unified application HTTP transport for text and files, documented in
APP_NOTIFICATIONS.md. It reuses the official configuration loader and existing
business progress/locks. It does not start a gateway or WebSocket or copy a
scheduler, platform, webhook sender, retry engine or permission synchronizer.
Explicit application/user or application-owned-group mappings are required.
All pending attachments are uploaded before text. Incomplete notifications resume
only missing components even under force; unknown outcomes stop for review.
Completed legacy forced resend scenarios remain distinct from failure recovery.
Real sending remains disabled and untested; this is an implemented migration
path, not a claim of production acceptance or atomic text/file delivery.

Missing historical material: purchase scheduled wrapper/runtime cadence and
webhook/user target settings are not in the referenced tree; no secret URL was
read to fill that gap. Dynamic employee/customer rosters and old persisted runtime
plans are not static Git configuration and have not been assumed empty. Formal
low-price loss/responsibility conclusions still require the production ETL and
ownership evidence previously identified; a ten-sheet preview is not completion
of those conclusions.
