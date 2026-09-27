# Receivable and credit analysis (optional candidate reference)

Rule ID: `datasage.receivable-analysis/v1`

- Status: candidate reference; it does not turn receivables into cash or make
  a credit decision.
- Load for receivable occurrence, open balance, debt snapshot, aging, overdue,
  credit, settlement, or formal turnover questions.
- The authoritative source is
  `plugins/datasage-query/contracts/receivable-semantics.yaml`.
- Optional on every channel; it does not prove B01 live WeCom consumption.

## Choose the evidence family

The contract separates receivable detail occurrence, open positive-unsettled
detail, monthly debt snapshot, aging snapshot, credit policy, and completed
settlement samples. Use matching metric families such as:

- occurrence: `receivable_occurrence_amount`, `receivable_bill_count`,
  `receivable_customer_count`;
- open/current debt: `open_receivable_amount`, `current_debt_amount`,
  `positive_debt_amount`, `debt_customer_count`;
- trend/aging: `debt_balance_trend`, `aging_total_amount`,
  `aging_over_90_amount`;
- credit coverage: `overdue_receivable_amount`, `overdue_days`,
  `excess_credit_amount`, `uncredited_positive_debt_amount`;
- completed settlement: `average_settlement_days`, `maximum_settlement_days`,
  and `settlement_days_distribution` for completed, validated documents;
- formal turnover: `formal_receivable_turnover_days`, a debt-snapshot and
  gross-delivery period metric with its own continuous-month coverage, not a
  settlement-document sample.

`receivable_quantity` remains `pending_validation` under its contract error
and activation gate. Do not use it or infer a unit conversion.

## Minimum evidence

Keep the fact family, actual period/snapshot month, observation date, and the
customer department, role-specific salesperson attribution, organization, or
currency dimensions when they are requested and allowed by that metric. Keep
the internal-customer scope and typed coverage. `open_receivable_amount` is
not the debt snapshot. Debt snapshots retain positive, negative, and zero
values.
Overdue evidence requires the applicable customer/organization/currency credit
join and coverage; missing credit is not zero overdue. Aging and debt snapshots
are independent evidence and need their coverage difference stated.

RMB receivable metrics retain their RMB basis; an original request to an RMB-only
metric is unsupported even when a currency filter is available. For a supported
original-currency metric, `auto` may use original evidence only in a single
currency scope; cross-currency work requires the governed RMB counterpart or
remains separate by currency. Explicit basis and named currency take precedence.
Original means transaction `currency_no`, not base-accounting `s_currency_no`.

Settlement metrics cover completed and validated documents. Formal turnover
needs its contract-defined continuous month coverage; do not silently shorten
the requested window.
Formal DSO uses average month-end net debt divided by same-period gross
delivery, multiplied by natural days, in one compatible currency basis.
Keep all N+1 snapshots and the gross denominator; do not substitute net delivery.

## Current net-debt plus overdue qualification

The confirmed default for “欠款1–5万元且逾期超过30天” uses two registered
facts: customer-group `current_debt_amount` in RMB yuan from 10,000 through
50,000 inclusive on its latest monthly net-debt snapshot, and at least one
current open item whose registered `overdue_days` is greater than 30. Apply the
row condition to open items first, then the customer group condition to the
net-debt fact. `any_overdue_days` is a group existence fact, not the maximum age
and not an overdue amount. `open_receivable_amount`, current net debt and the
overdue-item ledger remain distinct contract sources; never infer the net-debt
balance by summing open items.

Use `analysis.row_filters` for `overdue_days` and `analysis.group_filters` for
the registered net-debt group fact (`metric_value` bound to
`current_debt_amount`) and existence fact (`any_overdue_days`) when the catalog
exposes that combination. Resolve RMB yuan, the latest snapshot month, the
current overdue observation date, internal-customer population, organization
and credit-day join before group filtering; a group threshold must not choose a
currency or reverse the row scope. A known amount outside the range can be
excluded. If either fact or combination is not registered, report interface
unsupported rather than substituting an overdue subtotal, open-item sum or
per-bill amount.

The two facts may be read in one SQL/read snapshot, but that does not prove
their business as-of dates match. Require explicit authoritative alignment
evidence. If the latest monthly net-debt snapshot and current item observation
cannot be aligned, report each observation with its own date and keep joint
membership unknown, including when current `overdue_days` is false. Missing
amount, credit days, credit join, item coverage or snapshot freshness is
data-missing/unknown, not zero overdue. The finite user range needs no new KPI
approval; only a changed source family or comparison meaning needs focused
clarification.

## Phase2 customer net-debt detail

The registered receivable detail grain is the customer aggregate of
`current_debt_amount` on its latest available monthly signed net-debt snapshot.
It exposes only the customer display value, currency, snapshot month, amount and
typed state. It contains no invoice number, open-item row, credit-policy input,
or physical key. Current open receivable and overdue observations remain
independent metrics; they are not a Phase2 detail grain and cannot become a
customer match list for the joint net-debt/overdue predicate.

A customer page inherits the main metric, validated analysis, period/snapshot,
unit, currency and ledger. A full page/collection may reconcile only when its
coverage proves the same complete customer population. Unknown membership is
not summed, and cursor continuation is a revalidated current observation rather
than the old monthly snapshot. Keep `joint_scope_state=joint_unknown` when the
net-debt snapshot and current overdue evidence lack authoritative same-business-
as-of alignment.

## Boundaries

- Receivables, receipts, cash balance, liquidity, and risk decisions are
  distinct capabilities.
- A negative debt value is a returned snapshot fact, not an inferred refund or
  cause. Absolute overdue proximity does not establish equal risk.
- A missing credit policy or credit days is unable-to-determine, not no
  overdue. Product is a governed grouping only where the contract permits it.
- Do not use current customer master attributes to rewrite historical facts or
  snapshots, and do not infer a universal credit threshold.

## Diagnostic path

- **Goal**: judge the size and age profile of what is owed, and which customers
  or departments carry it. It is not a credit decision or a loss estimate.
- **Candidate explanations** (hypotheses): sales really grew on the affected
  accounts; receipts slowed (compare `receipt_amount`/`net_receipt_amount` in the
  same period); the aging profile shifted (e.g. `aging_91_120_amount` rising
  against `aging_0_30_amount`); master or organization attribution changed;
  currency or unit scope changed.
- **Discriminating evidence**: the same-scope aging bands, the registered
  `current_debt_amount` latest-month snapshot with its business date, the
  current overdue-item observation and its credit/date key, and the receipt
  side in a compatible period. A same read timestamp does not align different
  business as-of dates; absolute proximity does not establish equal risk.
- **Materiality**: ageing buckets need the credit policy or a compatible target.
  Where credit days or policy are missing, the answer is unable-to-determine, not
  "no overdue".
- **Candidate actions** (advice only): list the customers to review with their
  balances and ages; for anything above the owner's threshold, route to the
  credit owner. DataSage does not set credit limits or approve releases.
- **Stop when** (only the unsupported claim or comparison): policy or credit days are missing, the snapshot's freshness is
  unknown, `receivable_quantity` would be summed across units, or the request
  needs a capability that is not governed (cash, liquidity, customer loss).
  Missing credit policy blocks policy-dependent overdue or risk verdicts, not
  independently valid debt/aging observations; retain their own snapshot and
  coverage. Never convert that policy gap to zero overdue or a no-risk claim.

## Review prompts

Open question: when `current_debt_amount` is reviewed with current overdue
items, which authoritative business as-of key aligns the latest monthly
snapshot with the item observation? Report each date and source separately;
joint membership stays unknown without that proof, even when current overdue is
false. Disclose the matched customer/organization/currency key and any returned
policy freshness such as `updated_at`, without assuming an unexposed
policy-version field or one common snapshot month.

Boundary question: do not infer cash from occurrence, treat missing credit as
not overdue, reinterpret negative debt as an error, or aggregate
`receivable_quantity` across units.

Owner confirmation remains needed for snapshot freshness, credit-key coverage,
aging/debt reconciliation, turnover period, and the pending quantity gate.
