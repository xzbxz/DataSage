# Cross-domain analysis (optional candidate reference)

Rule ID: `datasage.cross-domain-analysis/v1`

- Status: candidate reference; this is a compatibility checklist, not a
  planner, mandatory query sequence, join layer, or health score.
- Load only when a question intentionally combines two or more governed
  domains. For a single-domain question, the domain reference is sufficient.
- Sources remain `plugins/datasage-query/contracts/*-semantics.yaml`, optional
  `delivery-analysis.md`, and `answer-boundary.md`; returned evidence outranks
  this reference.
- Optional on every channel. It does not prove B01 live WeCom consumption.

## Candidate relationships

The following relationships are prompts for compatibility review, not automatic
combinations:

- delivery/receipt: prefer the governed `delivery_receipt_comparison`; retain
  its asymmetric customer scope and period;
- delivery/target and receipt/target: use the corresponding target completion
  metric with the same ledger, period, population, department, and unit;
- delivery/inventory: do not substitute general delivery for the turnover
  contract's denominator or compare a flow with a snapshot by label alone;
- receipt/receivable: keep registered receipt flow separate from debt/aging
  snapshots and cash;
- pattern matching/delivery: linked detail is a bounded association, not total
  delivery or performance credit;
- delivery/receipt/profit and inventory/profit: keep report family, period,
  grain, valuation, and recorded/close state separate.

## Compatibility gates

Check every input's event-flow, complete-period, current/latest snapshot,
month-end snapshot, or task-time meaning. “This month” is not a substitute for
the contract time field. Check department identity explicitly: customer
department, business department, warehouse department, turnover department,
and profit department are not equivalent by name. Check ledger and attribution
(`transaction_detail` versus `salesperson_allocation`) before comparing
targets, delivery, or receipt. Check currency, unit, numerator/denominator,
population, `status`, completeness, unknowns, and truncation.

If one branch fails, preserve valid independent evidence and state the local
gap. Empty, undefined, incomplete, truncated, failed, and timeout states are
not interchangeable. A cross-domain difference, ratio, or structural
contribution is allowed only when the returned evidence and contract support
compatible operands and a complete reconciliation. A scorecard lens is a
candidate evidence list, not a requirement to query every domain.

## Boundaries

- Independent domain trends do not prove a mechanism or causal chain.
- Do not multi-join facts at different grains, sum across ledgers, fill NULL
  with zero, or infer a joint customer block from independent marginal cuts.
- Do not combine unlike departments, currencies, units, flows, snapshots, or
  report families because their labels look similar.
- Keep recommendations conditional, with assumptions and verification; do not
  execute allocations, credit changes, customer treatment, or target changes.

## Review prompts

Open question: which flow/snapshot/report family, department mapping, ledger,
and observation date should be used when an operating summary mentions delivery,
receipt, inventory, receivable, and profit together?

Boundary question: do not infer that falling delivery, slower inventory,
falling receipt, higher overdue, and lower recorded profit form one causal chain;
return separate observations or a clearly labeled hypothesis with the next
discriminating evidence.

Owner confirmation remains needed for cross-domain department mappings,
population symmetry, ledger comparability, and the business purpose of any
combined summary.
