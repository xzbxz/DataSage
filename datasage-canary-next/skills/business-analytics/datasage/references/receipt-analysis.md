# Receipt analysis (optional candidate reference)

Rule ID: `datasage.receipt-analysis/v1`

- Status: candidate reference; not a production approval or a live-channel
  consumption claim.
- Load this reference only when the question materially concerns receipts,
  refunds, net receipts, or the governed delivery/receipt comparison.
- The metric contract remains authoritative:
  `plugins/datasage-query/contracts/receipt-semantics.yaml`.
  Returned catalog/schema/evidence can narrow or reject any method below.
- It is optional on every channel. B01 live WeCom Skill consumption is still
  unverified; a missing reference must not block a governed query.

## Choose the object first

Receipt facts are detail rows in the receipt fact and refund fact. Bill counts
are distinct document counts, not detail-row counts. The default ledger is
`transaction_detail`; a salesperson allocation question belongs to the target
contract's `salesperson_allocation` path and must not be silently substituted.

Use the contract metric that matches the requested object, for example:

- recorded or converted receipt/refund amounts: `receipt_amount`,
  `actual_receipt_amount`, `refund_amount`, `actual_refund_amount`;
- document counts: `receipt_bill_count`, `refund_bill_count`;
- governed net flow: `net_receipt_amount` or
  `net_receipt_amount_original`;
- the paired comparison: `delivery_receipt_comparison`.

`department` means the receipt contract's customer department. Settlement,
receipt, and sales organizations are separate dimensions. Current customer
master attributes supplement a fact; they do not rewrite the historical
receipt or final-salesperson attribution.

## Minimum evidence

Before making a receipt claim, retain the returned metric, actual time range,
the time field(s) used by its fact components, dimensions/filters, status, unit,
currency, and material disclosures. Ordinary receipt metrics use the contract's
default `transaction_detail` policy; require an explicit ledger/attribution
mode only where the selected contract exposes one, such as target allocation.
For net receipt, rely on the returned composite metric state and component
coverage; do not promise separate component states unless they are actually
returned. Original currency must be filtered to one currency or grouped by
`currency`, the governed public dimension. Use the published request key, not
an underlying storage-column name.

For ordinary receipt metrics, `auto` and explicit `rmb` retain the contract's
RMB basis; a named currency narrows the RMB population. For an original-currency
metric, a single supported currency may remain original; cross-currency work
uses a governed RMB counterpart when registered, otherwise keeps per-currency
evidence without a unified result. An explicit basis wins and no exchange rate
is inferred.

`delivery_receipt_comparison` has its own governed window and intentionally
asymmetric population. For this paired metric, retain each fact component's
returned time field and range (delivery, return-settlement, receipt, and
refund), and describe both sides with the returned scope. A non-positive
delivery denominator leaves coverage undefined; it is not a zero coverage
result.

## Boundaries

- A receipt or net receipt is not cash flow, cash balance, or liquidity.
- A receipt/delivery gap, ranking, trend, or customer cut describes returned
  evidence. It does not prove a cause, responsibility, or collection quality.
- Empty, failed, incomplete, truncated, unknown, and zero states remain
  distinct. Do not fill a missing currency, customer tail, or component with
  zero.
- Usage filters apply when the request includes the contract's `receipt_usage`
  or `refund_usage` dimension/filter. The usage value need not be a returned
  grouping dimension; when it is only a filter, disclose the actual filtered
  scope and do not describe it as all usages. Default internal-customer wording
  does not override an explicit filter.

## Diagnostic path

- **Goal**: judge whether receipts keep pace with what was shipped and settled,
  and where the gap sits. This is not a liquidity or collection-quality verdict.
- **Candidate explanations** (hypotheses, not causes): shipments themselves moved
  (`delivery_amount`); returns or refunds moved (`return_amount`, `refund_amount`,
  `actual_refund_amount`); the recorded amount and governed conversion basis
  differ (`receipt_amount` vs `actual_receipt_amount`); customer mix changed;
  the paired comparison's asymmetric population differs. Both receipt metrics
  use the same receipt event time and RMB unit, so their difference alone does
  not discriminate a recording lag or attribution change. Those hypotheses
  need independent timing or attribution evidence. Product-mix questions need
  separate compatible delivery evidence; ordinary receipts have no product
  grouping.
- **Discriminating evidence**: the paired `delivery_receipt_comparison` for the
  same window, then the same-period `delivery_amount` beside `net_receipt_amount`,
  plus returns/refunds, and the salesperson split (`allocated_net_receipt_amount`)
  only where that ledger is exposed. A trend, ranking or single cut does not
  discriminate.
- **Materiality**: needs a compatible target (`receipt_target_completion`) or an
  owner-stated benchmark. Without one, report the change and say that its
  importance is undetermined.
- **Candidate actions** (advice only): ask the customer owner to check settlement
  timing, and re-read after the return-settlement period closes. Anything touching
  credit or customer treatment needs the human approver.
- **Stop when** (only the unsupported claim or comparison): the comparison's population or period is incompatible, the state
  is pending/incomplete/truncated, currencies cannot be isolated, or the requested
  capability (cash, collection quality) is not governed.

## Review prompts

Open question: for a complete month and fixed customer department/currency,
should a change in `net_receipt_amount` be shown by customer or salesperson
using the same ledger and compatible period, with structural language only?

Boundary question: do not relabel net receipt as cash flow or add unlike
original currencies. If the requested cash capability is not governed and
returned, state that it was not obtained.

Owner confirmation remains needed for the business meaning of recorded versus
actual receipt, the governed exchange-rate interpretation, organization
selection, and whether the paired comparison's asymmetric scope is still the
intended operating view.
