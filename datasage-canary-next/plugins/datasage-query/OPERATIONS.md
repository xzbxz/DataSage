# Migrated operational observations

This extends the existing local report entrypoint, not the public admission or
the official scheduler. Production bindings, recipients, schedules and jobs remain disabled; the separately authorized test-only delivery configuration is documented in REMINDER_ACCEPTANCE.md. Prices and customer evidence never belong in Git. Reports and
accepted local snapshots use Git-ignored `report_runs/operations/<report-id>/`.

Role-backed workflows use the single Git-ignored private map
`local/workflow-roles.json`. The public region/department rules remain in
`contracts/legacy-workflows.json`; an old `report_runs` role reference is only
an explicit import source and is never a runtime fallback. Use
`roles-check --source <source>` before
`roles-import --source <source> --output <profile>/local/workflow-roles.json`;
an existing active file is not overwritten.

Business rules and source roles live in `contracts/operations.yaml`; the existing
contract snapshot pins this file. Implementations are in `operations.py`. The
existing local report binding configures production local observations; test-only delivery uses its separate ignored runtime configuration and progress.
There is no new general SQL tool or runtime rule language.

## Public query

`inventory.idk_unpriced_pool` is an ordinary governed metric. It returns source
row counts by source inventory unit, NULL/zero/negative-price counts and quantity/
roll coverage, using the legacy IDK threshold and whitelist. It never returns
raw DDP or promotion prices. It does not mean unique products or sent reminders.
Natural request: “IDK现在有哪些未定促销价的情况？按库存单位列出数量，并区分空价、零价、负价。”
This first public metric gives aggregates; the local IDK report below gives the
bounded product/color candidate list. Existing query admission remains required.

## Local report modes

Use the existing installed Hermes Python and matching HERMES_HOME:

```text
python scripts/datasage_slow_report.py --report-id <approved-report-id>
python scripts/datasage_slow_report.py --report-id <approved-report-id> --accept-snapshot <reviewed-observation-sha256>
```

The second command applies only to the separate local-observation mode. It is an explicit local baseline acceptance, not a database write or delivery acknowledgement. Original legacy sales/purchase reminder jobs now default to `reference_source: legacy_database`, reading the existing vk_ai price snapshot tables. Their observations cannot be accepted as new local baselines. See LEGACY_PRICE_REFERENCE.md. A genuinely new key without an old reference is not a price-change alert. Failed/truncated source reads cannot advance the baseline.
Acceptance uses an exclusive lock, verifies document digest and expected prior
baseline, and refuses stale concurrent candidates. Re-accepting the same digest
is harmless. Never infer receipt from generated stdout or an accepted snapshot.
For actual delivery, the receipt must carry
`datasage-delivery-binding/v1`; only `verified_for_reuse` suppresses a new send.
An old provider-accepted record without that binding remains historical and
cannot be reused, and old 10/9 acceptance counts do not pass the new binding.

For report delivery, `report_evidence` remains responsible for business source,
detail and summary reconciliation. The shared completeness gate checks the raw
query packets and delivery coverage; passing one evidence layer does not prove
the other.

In the existing `local-report-bindings.json`, a report may use `kind` instead of
the prior slow report's `views`. Root shape remains version/default_report/reports.
Existing six weekly/monthly views keep their original validation and behavior.

| kind | Required additional binding | Output and boundary |
|---|---|---|
| idk_unpriced | limit; optional window_days 0–366 | IDK product/color candidate records; NULL/zero/negative separated. Default window 0 includes legacy stock. A source row absent next time is not proof it was priced or sold. |
| sales_prices | regions, limit; reference_source=legacy_database for legacy reminders | Current ready/promoted sales price identities; region source preferred over organization fallback. Latest-time ties stay ambiguous. Currency, source units, tax and validity changes are separate from price changes. |
| purchase_prices | regions, limit; reference_source=legacy_database for legacy reminders | Product/color/supplier identities; included/excluded tax prices remain separate. Legacy organization scope retained. Missing source quote stays visible. |
| slow_assignment | department, baseline_week, max_baseline_age_days, products, include_customer_cards, limit | Existing weekly pool evidence plus optional existing historical-customer packets and local HTML cards. Products must be explicit, 1–10; customer scope must be explicitly true/false. No automatic recipient/owner assignment or responsibility inference. |
| fabric_review | time_range with start/end, inventory_scope total/on_hand, limit | Existing delivery/inventory source summaries, channels and formation review in one local artifact; keeps individual observation clocks and source evidence. Not a new responsibility model. |

Pricing regions must be a nonempty subset of HCM/HN/BKK/IDK. Pricing/IDK limits
are at most 10,000 and remain subject to existing executor limits; an incomplete
source read fails instead of inventing disappearance. Governed packet views
retain the existing per-query 100-row report bound. Each packet exposes its
truncation and full-scope facts; a bounded card list is not a complete contact list.

`slow_assignment.baseline_week` may be an explicit existing ISO week or the
explicit policy `current_iso_week`. The latter constructs the current business
week label; it does not use MAX(week), create a baseline or fall back to an older
week. Existing baseline age and integrity checks remain mandatory. A missing
current-week upstream artifact is a failure requiring operator attention.

## Deliberate migration differences

- Legacy sales JS used Number(NULL), potentially confusing NULL and zero. This
  implementation preserves missing values and uses exact Decimal comparisons.
- Legacy purchase comparisons rounded to six decimals and did not retain all
  comparison basis in their loaded snapshot. Local comparison preserves source
  precision and separates currency/unit/tax/validity, rather than generating a
  misleading pure-price alert.
- Latest-modified ties are not arbitrarily resolved. New/expired/missing quotes
  and changes of conditions remain explicit. The old price selection did not
  filter effective dates; this implementation preserves selection but labels
  whether the selected quote is currently effective. It does not silently pick
  an older quote instead. An absent end date means no recorded expiry; missing
  effective date is unknown.
- When purchase validity is missing but identity, currency/unit and numeric
  prices are present, only recorded-quote evolution is compared. Such a change
  is `recorded_quote_changed`, never a confirmed currently executable price.
- Old snapshots remain untouched. Sales snapshots lack unit/tax basis, so they
  are not automatically imported as a fully comparable local baseline.
- Old promotion membership uses the recorded dates and regions without a
  deletion-flag interpretation; the legacy rule is preserved pending that policy.
- Database snapshot rewrites, external lock services, official runtime patches and permission synchronizers are not copied. Finite app/webhook adapters and old recipient rules are documented separately in REMINDER_ACCEPTANCE.md; production cron registration remains off.

## Still not enabled or proven

This is a local execution layer plus the IDK public aggregate, not a new public
price-query domain. Pricing source details are only available to the approved
local operator binding, not newly granted to arbitrary model tool arguments.

Full low-price/responsibility conclusions still need production ETL definitions
for quality/repeated-return labels, complete lineage and defensible responsibility
assignment. Existing source-label summaries and coverage remain usable; source
“unique responsibility” does not prove actual fault. We do not restore the old
template's unrestricted responsibility ranking or mislabel DDP differences as loss.

Production task assignment and scheduling remain disabled. Customer PNG/ZIP and role-based review artifacts have been implemented and exercised through the separately approved test-only delivery mode; this does not mean production recipients were contacted. Official Hermes cron/no_agent owns scheduling and run status; the documented finite Profile HTTP adapters bridge the legacy application/webhook media capabilities. Delivery failures and
unknown outcomes must be investigated; do not automatically resend an entire batch.
