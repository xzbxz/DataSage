# DataSage v0.15 Frozen Refactor Design

This design is frozen before implementation. Findings that do not violate the
charter invariants go to a backlog.

## Decision

Keep the existing versioned `*-semantics.yaml` files as the metric and physical
adapter source during v0.15. Do not create a database, DSL, workflow engine, or
second metric registry. Introduce one small pure contract/compiler seam for the
cross-domain request facts that are currently duplicated by public Schema and
runtime validation.

## Capability contract interface

`plugins/datasage-query/capability_contract.py` owns only:

- supported domains and their semantics source locations;
- allowed values and domain ownership for `attribution_mode`,
  `delivery_scope`, and `inventory_scope`;
- the requirement that target metric requests declare attribution;
- public request count and physical execution budget;
- pure JSON Schema condition generation for those facts;
- pure per-request field validation for those same facts;
- physical cost calculation for explicit complete operations;
- a forbidden-key validator proving that routing, fixed plans, answer templates,
  thresholds, causes, and recommendations are not capability facts.

Stable public functions:

```python
query_request_schema_conditions() -> list[dict]
validate_request_field_contract(request: Mapping[str, Any]) -> None
physical_request_cost(request: Mapping[str, Any]) -> int
assert_capability_boundary(value: Any) -> None
```

Validation raises `CapabilityContractError(code, message)`. It has no imports
from contracts, tools, scorecard, wire, references, or Skills.

Metric-specific dimensions, filters, comparisons, adapters, receipts, scope
values, and disclosures remain compiled from existing semantics by the current
Catalog/runtime path. v0.15 adds equivalence tests instead of moving all 121
metrics at once.

## Execution contract

- Envelope errors may fail the whole call: malformed top-level arguments,
  invalid/duplicate request IDs, and invalid calculation references.
- Branch errors never clear independent results.
- Complete operations consume two physical slots. The planner allocates the
  ten-slot budget by public branch in input order. An operation that cannot fit
  returns local `EXECUTION_BUDGET_EXCEEDED`; unrelated public branches remain.
- Raw internal byte limits do not decide model delivery. Existing row and cell
  limits remain. The compact wire applies the final `max_tool_result_chars`
  budget and preserves a deterministic success-first subset of complete
  branches.

## Hermes intelligence boundary

- `performance_scorecard` remains outside the capability compiler and exposes
  candidate lenses. Hermes chooses the material subset.
- Planner recipes, prompt triggers, exact overview bundles, and fixed routes
  are not model-visible references and are not release requirements.
- Golden plans become required/forbidden semantic constraints, never exact set
  equality.
- Model final prose is never compared to a fixed hash and is never mutated.

## Implementation ownership

- Architecture agent: `capability_contract.py`, `schemas.py`, `contracts.py`,
  `references.py`, `scorecard.py`.
- Execution agent: `tools.py` only.
- Acceptance agent: `tests/**` and `plugins/datasage-query/e2e/**` only.
- Main agent: `wire.py`, version/config/distribution/Skill/docs, companion Skill
  deletion, integration fixes, and final verification.

No agent may edit the running profile.
