# Inventory, slow-pool, and turnover analysis (optional candidate reference)

Rule ID: `datasage.inventory-analysis/v1`

- Status: candidate reference; no new inventory threshold or reclassification
  is introduced here.
- Load for current inventory, month-end inventory, turnover, registered slow
  pool, or IDK unpriced questions.
- The authoritative source is
  `plugins/datasage-query/contracts/inventory-semantics.yaml`.
- Optional on every channel; it does not prove B01 live WeCom consumption.

## Keep the objects separate

The contract exposes distinct objects:

- current barcode inventory: `current_inventory_quantity`,
  `current_inventory_roll_count`, `current_inventory_amount_rmb`,
  `current_inventory_amount_original`, and `current_warehouse_age_days`;
- month-end accounting snapshots: `month_end_inventory_quantity`,
  `month_end_inventory_cost_rmb`, `month_end_inventory_ddp_rmb`, and related
  snapshot metrics;
- turnover evidence: `turnover_net_delivery_rmb` and
  `inventory_turnover_days`;
- current source pools: `registered_slow_pool_*` and `idk_unpriced_pool`.

Current inventory has governed status scopes such as total, on-hand,
available, allocated, and in-transit. Physical warehouse dimensions and
turnover/customer dimensions are not interchangeable. Registered pool rows,
products, SKUs, rolls, and quantities are different populations.

## Minimum evidence

Keep the inventory scope, observation/snapshot date, applicable unit or
currency, actual dimensions, completeness, unknown items, and truncation state.
Month-end results need their selected accounting month. Turnover needs its
complete period and the governed denominator readiness. A missing month or
NULL is not zero; a zero denominator remains undefined.

Only the contract's unit policy applies: `m`, `y`, `kg`, and `Pcs` remain
separate, with only the declared `M` spelling normalization. Do not convert
or add unlike units. Original currency remains per-currency.

Currency basis is metric-owned. For an ordinary amount question, `auto` may
retain original currency for a single supported currency; cross-currency
valuation or comparison requires the governed RMB metric when one is registered.
Explicit RMB metrics remain RMB, while an original-only metric without an RMB
counterpart remains per-currency.
Book cost and DDP use their paired ledger `currency_no`; purchase prices retain
their own purchase currency. Turnover keeps the global RMB cost accounting
readiness rule even when its amounts are original currency. Unknown currency
or missing amounts do not become zero or a complete total.

Registered slow-pool results retain source-row, price-state, classification,
identity, unit, and known-subset gaps. IDK retains source-row,
NULL/zero/negative-price, identity, unit, and known quantity/roll gaps; it has
no slow-pool classification field. `idk_unpriced_pool` is an observation
metric; it does not send a reminder. Do not invent or change a source
threshold or whitelist. When relevant, disclose the contract-defined scope
and basis as returned evidence rather than creating a new business rule.

## Boundaries

- Current DDP valuation and month-end accounting cost are distinct. Their
  difference is not automatically loss, profit, or an accounting adjustment.
- `oldest_inventory_days` is a contract-defined month-end no-movement/no-sale
  measure, not a universal physical age.
- A registered slow pool is not a full-company reclassification, real-zero
  inventory proof, price completion proof, or treatment result.
- A snapshot or turnover result does not prove inventory health, cause, loss,
  or responsibility. Keep unknown and incomplete coverage beside the claim.

## Diagnostic path

- **Goal**: judge how much capital sits in inventory, how old it is, and which
  products or warehouses carry it. It is not an inventory-health verdict.
- **Candidate explanations** (hypotheses): purchasing outpaced sales; sales of the
  affected products slowed (compare `delivery_quantity`/`delivery_amount` in a
  compatible period); valuation moved (`month_end_inventory_ddp_rmb` vs
  `month_end_inventory_cost_rmb` are different measures, not a bridge); the
  registered slow-pool composition differs (use its registered-pool metrics);
  the separate IDK unpriced promotion candidate pool differs
  (`idk_unpriced_pool`); coverage is incomplete (missing cost rows). The IDK
  candidate count is not the full registered slow-pool population, product
  count, price amount, or proof of change without comparable observations.
- **Discriminating evidence**: current value by warehouse
  (`current_inventory_amount_rmb`) beside the month-end snapshot, ageing
  (`current_warehouse_age_days`, and `oldest_inventory_days` where returned),
  turnover (`inventory_turnover_days`) and the slow-pool counts. Keep quantities
  in their own units.
- **Materiality**: needs an owner-stated benchmark or target; turnover is
  undefined while its period inputs or cost coverage are missing.
- **Candidate actions** (advice only): list the products or warehouses to review
  with their values, ages and coverage gaps; disposal, write-down or purchasing
  changes need the operations owner's approval.
- **Stop when** (only the unsupported claim or comparison): meters/yards/kilograms/pieces would be summed, cost coverage is
  incomplete, the snapshot period is not the one being discussed, or the request
  needs an ungoverned capability (physical age, loss, health). Missing cost
  blocks cost-dependent valuation or turnover claims, not independent quantity
  or roll observations whose own unit, scope and returned coverage are valid.

## Review prompts

Open question: for a product request, and only where each metric's declared
dimensions are compatible, should current available inventory, a selected
month-end cost snapshot, and turnover be shown as separate observations with
their own units and periods? If the request is warehouse-scoped for current or
month-end inventory, state that turnover has no warehouse grouping and keep
the dimension-specific branches separate.

Boundary question: do not subtract current DDP from month-end cost as loss,
rename no-movement days to physical age, or sum meters, yards, kilograms, and
pieces.

Owner confirmation remains needed for the default current scope, month-end
selection, turnover completeness interpretation, and the operational meaning
of slow-pool classifications.
