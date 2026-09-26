# Pattern-matching and linked-delivery analysis (optional candidate reference)

Rule ID: `datasage.pattern-matching-analysis/v1`

- Status: candidate reference; no sales-credit or causal model is introduced.
- Load for find-pattern task, execution, candidate-product, or linked-delivery
  questions.
- The authoritative source is
  `plugins/datasage-query/contracts/pattern-matching-semantics.yaml`.
- Optional on every channel; it does not prove B01 live WeCom consumption.

## Object and time basis

The contract distinguishes task/record rows from execution records and linked
outbound detail. One linked record is not automatically one task, execution,
person, or outbound document. Use the governed metric that matches the need:

- `task_recorded_summary` for task, execution, person, candidate, and
  found/feedback/sample/linked-record coverage;
- `linked_delivery_amount` for stable-identity deduplicated linked outbound
  detail amounts;
- `person_attributed_delivery_amount` for the contract's task/person/product
  record attribution.

The allowed time bases are metric-specific: `task_recorded_summary` supports
current observation, task creation, and execution completion; linked-delivery
amounts additionally support linked delivery. A current observation of a task
cohort is not a historical snapshot of what was known at creation time.

## Minimum evidence

Keep the selected time basis, read/observation time, task/customer/person and
candidate-product identity state, currency, stable linked-detail identity,
duplicate/conflict status, and `has_more`/coverage state. Original currency
comes from the matched outbound detail; the request's desired currency is not
a conversion instruction.

Verified linkage requires more than stable identity and currency. The live
validation also requires a non-missing sales-side amount equal to the linked
amount, compatible salesperson and product, and execution completion before
delivery. Resolving a linked-amount conflict does not waive those checks;
missing evidence stays unresolved unless the supplied scenario explicitly
establishes that the matching conditions are satisfied.

Found, feedback, sample, and linked-record evidence can overlap when the
contract records them independently. Task and execution statuses, including
completion-related values, remain source-exact; conflicts and undefined values
must be retained rather than treated as mutually exclusive funnel stages.
Group counts have their contract grain and cannot be added across customers,
people, products, or periods without a compatible population.

## Boundaries

- Linked amount is an association record amount, not sales credit, incremental
  revenue, performance, or proof that the task caused the outbound.
- Current absence of a link is an evidence gap, not proof of failure, no sale,
  or permanent absence.
- The same detail may occur in different task/person groups under the contract
  attribution rules. Do not treat group sums as a global deduplicated amount.
- Truncation, unknown identity, non-single products, and amount conflicts
  narrow the claim; they are not resolved by guessing.

For `linked_delivery_amount`, conflicting linked amounts for the same stable
detail leave that detail unresolved. Do not choose the row that happens to
match a sales-side amount and count the disputed detail as verified. Keep it
out of the known subset, retain unaffected details separately, and leave the
complete governed total unknown until the conflict is resolved. A uniquely
identified outbound detail can have a confirmed source currency even while
the linked amount is disputed. Preserve that currency as source metadata,
without presenting the disputed amount as a verified amount in that currency.
A candidate match alone does not establish either fact; keep currency unknown
when its own source evidence is missing or conflicting.

Conflicting candidate amounts do not establish a lower or upper bound on the
unresolved detail or complete total. Do not present their minimum and maximum
as the size of the evidence gap. If explicitly asked for scenarios, state each
assumption separately; the correct amount may differ from every observed
candidate, so those scenarios do not bound the unknown governed total.

## Diagnostic path

- **Goal**: judge how much delivered value is linked to a task or person under the
  contract's attribution rules, and where the links failed. It is not proof that a
  task caused an outbound, nor a performance rating.
- **Candidate explanations** (hypotheses): the link rules really matched fewer
  details; the task window differs from the delivery window; identity is unknown
  or conflicting; the population is truncated; the same detail appears in more
  than one task or person group.
- **Discriminating evidence**: `linked_delivery_amount` against
  `person_attributed_delivery_amount` for the same details, plus
  `task_recorded_summary` and the returned unknown/conflict counts. Conflicting
  linked amounts for one stable detail stay unresolved - keep them out of the
  known subset instead of choosing the convenient row.
- **Materiality**: the linked share needs an owner-stated expectation; the
  complete governed total stays unknown while details are unresolved.
- **Candidate actions** (advice only): list the affected details or groups for the
  operations owner to reconcile; re-read after the window closes. Never present a
  link as proof of cause or credit.
- **Stop when** (only the unsupported claim or comparison): the time basis is not the verified one, identities conflict or
  are unknown, the result is truncated, or the requested judgement is causality
  or performance.

## Review prompts

Open question: should a task-created cohort and linked-delivery amounts be
shown as separate views when their time bases differ?

Boundary question: do not rank an executor's linked amount as incremental sales
performance or label unlinked tasks failed without a returned failure state.

Owner confirmation remains needed for status meanings, stable linked-detail
keys, single-product matching, and the accepted time basis for operating
reviews.
