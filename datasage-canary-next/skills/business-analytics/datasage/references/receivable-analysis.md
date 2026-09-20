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

Settlement metrics cover completed and validated documents. Formal turnover
needs its contract-defined continuous month coverage; do not silently shorten
the requested window.

## Boundaries

- Receivables, receipts, cash balance, liquidity, and risk decisions are
  distinct capabilities.
- A negative debt value is a returned snapshot fact, not an inferred refund or
  cause. Absolute overdue proximity does not establish equal risk.
- A missing credit policy or credit days is unable-to-determine, not no
  overdue. Product is a governed grouping only where the contract permits it.
- Do not use current customer master attributes to rewrite historical facts or
  snapshots, and do not infer a universal credit threshold.

## Review prompts

Open question: when positive debt and aging-over-90 are reviewed together,
which selected/latest `bill_date` should be shown for those snapshot facts?
Report determined overdue separately as a current query-date result with its
credit join and coverage; disclose the matched customer/organization/currency
key and any returned policy freshness such as `updated_at`, without assuming
an unexposed policy-version field or one common snapshot month.

Boundary question: do not infer cash from occurrence, treat missing credit as
not overdue, reinterpret negative debt as an error, or aggregate
`receivable_quantity` across units.

Owner confirmation remains needed for snapshot freshness, credit-key coverage,
aging/debt reconciliation, turnover period, and the pending quantity gate.
