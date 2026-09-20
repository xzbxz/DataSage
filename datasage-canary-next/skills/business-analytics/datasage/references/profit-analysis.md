# Profit-report analysis (optional candidate reference)

Rule ID: `datasage.profit-analysis/v1`

- Status: candidate reference; profit outputs remain source-report records,
  not a close or accounting approval.
- Load for profit, gross-profit, expense, or margin questions.
- The authoritative source is
  `plugins/datasage-query/contracts/profit-semantics.yaml`; the profitability
  scorecard lens remains candidate-only.
- Optional on every channel; it does not prove B01 live WeCom consumption.

## Select the matching report family per metric

The contract keeps four report families separate. For a single-family question,
select the family that owns the requested metric and dimensions. If a question
intentionally compares report families, query each family independently with
compatible period, scope, grain, and attribution; keep the values as separate
observations and never add them without a complete bridge.

- customer month: `customer_month_*` from the customer report;
- order lifetime: `order_lifetime_*` selected by original delivery time;
- department month: `department_month_*`;
- product month: `product_month_*`.

Representative governed evidence includes the corresponding `*_sales_revenue`,
`*_gross_profit`, `*_gross_margin`, cost, return, and expense metrics. A
customer-month report does not provide product/order detail; department and
product reports do not automatically provide customer or salesperson detail.

## Minimum evidence

Keep the report family, actual time field/range, available dimensions, returned
income/profit/expense metrics, completeness/known subset, NULL state, and
internal-customer scope. Margin evidence must use the same report, period,
scope, and returned numerator/denominator meaning. Use the governed result;
do not rebuild a stored report value in the answer. Preserve its typed state:
if revenue is zero or a required component is missing, gross margin is
undefined. With negative revenue, keep the signed ratio alongside revenue and
gross profit; a positive ratio is not proof of positive profit.

The contract's expense semantics remain authoritative. An unallocated NULL is
not zero. Without a close flag, describe the value as recorded through the
observation, including the possibility of later updates.

## Boundaries

- Customer-month, department-month, product-month, and order-lifetime values
  cannot be added as if they were one ledger. Differences are observations
  unless a complete compatible bridge is returned.
- Recorded gross profit is not finalized net profit. Missing expenses do not
  prove no expense and cannot be filled with zero.
- Margin, trend, or expense ranking does not establish a cause, health,
  controllability, or collection result.
- Do not use a current customer owner to fill historical report attribution.

## Review prompts

Open question: for a complete month by department, is the department-month
report the intended grain for recorded revenue, gross profit, margin, and
expense coverage?

Boundary question: do not add customer/product/order report values, fill NULL
expenses with zero, or call the result a closed net profit.

Owner confirmation remains needed for close status, expense allocation,
department/report comparability, and the lifecycle treatment of order values.
