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
