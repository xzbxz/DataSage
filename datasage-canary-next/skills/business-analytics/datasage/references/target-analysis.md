# Target completion analysis (optional candidate reference)

Rule ID: `datasage.target-analysis/v1`

- Status: candidate reference; not a release gate or a production target
  approval.
- Load only for target, completion, gap, or governed actual-versus-target
  questions.
- The authoritative source is
  `plugins/datasage-query/contracts/target-semantics.yaml`; this reference
  never copies its formulas or state machine.
- Optional on every channel. It does not prove B01 live WeCom consumption.

## Select the ledger and scope

The target contract separates `transaction_detail` from
`salesperson_allocation`. Customer, department, organization, and overall
target questions use the transaction ledger. Salesperson, collaboration, and
allocation questions require the split-ledger path. Split tables are the
source of truth for that path; base-table fallback and cross-ledger
substitution are forbidden.

Use the governed metric that matches the question:

- ordinary targets: `delivery_target_amount` or `receipt_target_amount`;
- allocated targets/actuals: `delivery_allocated_target_amount`,
  `receipt_allocated_target_amount`, `allocated_net_delivery_amount`, or
  `allocated_net_receipt_amount`;
- completion evidence: `delivery_target_completion` or
  `receipt_target_completion`.

The delivery target path excludes internal customers. The receipt target and
registered receipt path includes internal customers. Delivery allocation
deducts returns according to the return fact attribution; it does not apply a
second collaboration split. Receipt allocation is a registered amount and is
not a settlement or arrival proof.

## Minimum evidence

Keep the returned completion object, ledger/attribution mode, complete target
month, actual observation time, target/period state, department and
organization mapping, population scope, units, and coverage. An unfinished
month is a query-time cumulative observation against the contract's complete
month target. Target zero, missing, incomplete, and future states are typed
states; they are not ordinary zero completion.

Do not infer why a target is missing or zero (for example, unassigned,
placeholder, or data error) without returned evidence or owner confirmation.
An undefined completion ratio does not automatically make every absolute
target-versus-actual difference unavailable. Determine separately whether a
governed gap is supported for the same period, scope, attribution ledger, and
required evidence; a transparent display calculation on compatible returned
values must not be presented as that governed gap.

For salesperson allocation, the returned evidence must also show the split
ledger's availability and activation boundary. A catalog entry or a
transaction-ledger result alone does not authorize an allocation answer.

## Boundaries

- Completion, gap, ranking, or a same-metric customer breakdown describes the
  returned target relationship. It does not establish target quality, cause,
  responsibility, or a health threshold.
- Do not divide incompatible ledgers, populations, periods, or units. Do not
  create a ratio because one target is missing or zero.
- A target result does not turn registered receipt into cash or settlement.
- Ordinary breakdown is not structural contribution; structural contribution
  needs the contract's compatible, complete reconciliation and residual.

## Review prompts

Open question: for a complete natural month by customer department, should
`delivery_target_completion` use the transaction ledger and the customer
department field, with unfinished-month status shown beside the result?

Boundary question: do not divide salesperson allocation receipt completion by
transaction-ledger delivery completion to label ability, and do not replace a
missing target with zero.

Owner confirmation remains needed for split-ledger activation, target-version
ownership, organization equivalence, and the business wording for incomplete
or future target periods.
