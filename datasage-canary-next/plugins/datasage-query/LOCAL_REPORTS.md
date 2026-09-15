# Local trusted slow reports

Legacy schedule behavior, fixed disabled adapters and actual attachment previews
are documented in [LEGACY_WORKFLOWS.md](LEGACY_WORKFLOWS.md). They do not enable
freezing, delivery or price-baseline acceptance.

Additional migrated IDK, price observation, slow assignment review and fabric
report modes share this entrypoint and binding file; see [OPERATIONS.md](OPERATIONS.md).
They remain unconfigured by default and do not enable scheduling or delivery.

This is an operator entrypoint, not a registered model tool. Public DataSage
admission still requires the existing bound identity. The report entrypoint
does not set WeCom or replay identities and does not register jobs or send
messages. Same-account processes can read or change local bindings; a report
ID, script name or environment variable is not an unforgeable cron principal.

The installed entrypoint is `scripts/datasage_slow_report.py`, under the active
Profile. It requires the official Hermes environment and matching HERMES_HOME.
Invoke it with the installed Hermes Python, optionally `--report-id ID`.
No binding file is shipped. Until an operator-approved binding exists it exits
2 with `REPORT_NOT_CONFIGURED` before runtime/database initialization and writes
no report artifacts. Do not configure or schedule it merely because the script
exists.

Future approved bindings belong to Profile-local `local-report-bindings.json`
(Git ignored). It is an object with exactly `version: 1`, `default_report`
(report ID) and `reports` (ID-to-definition mapping). Each selected definition
requires common fields department, views and limit. Weekly views additionally
require baseline_week and max_baseline_age_days; monthly views require an explicit
calendar_month and use the independent monthly opening pool. A monthly-only binding
rejects weekly baseline/age/time_range fields; it does not reinterpret a week as a
month-start pool. All real bindings remain unconfigured. Available views:

- pool_summary / flow_summary / flow_sales for weekly cohorts;
- monthly_pool_summary / monthly_flow_summary / monthly_flow_sales for monthly cohorts.

Field details:

| Field | Contract |
|---|---|
| department | One reviewed source warehouse department; no CLI override |
| baseline_week | One explicit valid existing ISO week; no automatic latest-week fallback |
| max_baseline_age_days | Explicit operator-approved bound, integer 1–366; no shipped business default |
| views | Nonempty unique selection of the six views above |
| limit | Integer 1–100, further limited by existing query/wire budgets |
| calendar_month | Required for monthly views, YYYY-MM; not a weekly baseline |
| time_range (optional) | Exactly start/end ISO dates, half-open; applies only to flow views |

No SQL, metric field, arbitrary request plan, recipients, schedule or send
options are accepted. A definition using a period must include a flow view.
Weekly pool comparison stays a current observation when weekly flows use a past
window; independent monthly closing uses the specified current/historical source.
The existing metric time validation, units, unknown states, negative quantities,
source identity, concurrency, deadlines and evidence projection remain active.

Both ordinary and high-price net rolls come from the shared query result. The
renderer does not recalculate high-price totals or remove zero ordinary-net rows.

The entrypoint loads settings via the existing Profile configuration and pins
the contract snapshot. After the local allowlist passes, it calls the existing
`runtime_guarded_datasage_query` through the existing bounded wire handler.
It does not copy the query engine or weaken the public entitlement wrapper.
Only local operator access is asserted; this is not per-user authorization.

Output is field/state presentation, not a model conclusion, causal explanation
or depletion/completion score. Source text is escaped so MEDIA directives cannot
be injected into official stdout delivery. Different units are kept separate;
repeated unit totals are shown once per unit per result, never added again.

Successful/partial query packets and rendered text are stored as JSON/text under
the Git-ignored `report_runs/slow/<run-id>/`. Atomic per-file replacement avoids
publishing half-written files. A partial or failed computation remains locally
inspectable but exits 3 without ordinary report stdout. Missing data inside a
successful query remains explicitly unknown; it is not coerced to zero. Binding,
stale-baseline and context errors exit 2; unexpected runtime errors exit 3 with
a generic code, avoiding credential/error-body output. Artifact retention is
not automated in this stage.

Official no_agent owns interpreting stdout/silence/exit status. No local sending
queue, cron store, message API or attachment delivery is implemented. Bounded
official-script subprocess tests use only a temporary Profile and synthetic
SQLite fixtures with network blocked; they prove neither production binding nor
recipient delivery. Real scope, age policy, baseline owner, business cycle and
recipient choices remain to be approved before B/C/D stages.

Sales report views consume SQL-provided scope and sales roll totals, including
cross-quantity-unit roll totals. No quantities are summed across units. For a
war total plus sales breakdown, one flow_sales query already carries both.
Different query branches retain their own read times; do not claim their results
are a single atomic observation or derive inventory change equals sales.
