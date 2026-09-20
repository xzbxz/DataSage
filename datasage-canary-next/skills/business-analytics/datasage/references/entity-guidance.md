# Entity guidance

Rule ID: `datasage.entity-guidance/v1`

- Owner: DataSage Skill's model-safe entity guidance.
- Consumers: Hermes on demand whenever native Skill reading is available in any
  supported session; architecture inventory tests.
- Lifecycle: version with the Skill. The plugin registry and domain semantics
  own executable identities; this reference cannot create a mapping.
- Channel: optional when native Skill reading is available; never a query
  precondition, and this reference does not claim real WeCom loading or
  business approval.

- `datasage_entity_resolve` provides bounded identity candidates and search
  scope. Query validation (`datasage_query`) is the execution authority and
  performs an exact identity check
  again; a resolver result never proves user confirmation.
- A query dimension's `entity_ref_v1_...` is a stable comparison reference,
  independent of its display name or amount. For a follow-up, pass it to the
  existing `datasage_entity_resolve` with the relevant type/domain/metric, then
  use the returned governed canonical value through normal query validation.
  Do not use the opaque reference as a raw filter token or invent a reference
  when `identity_state` reports missing identity.
- Ambiguous, fuzzy or unsupported identity bindings do not establish an exact
  business entity. Candidate metadata is advisory; it does not create a new
  governed mapping.
- A dimension breakdown only enumerates observed labels. It does not prove an
  alias or create a candidate mapping, and must not become a durable Memory fact.
- Candidate mappings are transient conversation context; never put them in
  Memory. A query rejection for an unknown, ambiguous, or role-incompatible
  entity requires clarification or a stable identifier, not a guessed filter.
- An empty bounded candidate result does not prove that the entity is absent
  outside the resolver's governed candidate scope.
- Keep stable registered identities in the governed request and show business
  names in the answer.
- Historical analysis retains departed employees; current-employment status is
  a separate population filter.

- `resolution_scope` describes only the considered entity types and sources.
  An unsearched type is not proved absent. A single customer candidate plus an
  unsearched department type does not justify a type-neutral business binding;
  the candidate is not proof that the department role is absent.
- Department discovery covers registered aliases only; unregistered department
  literals can exist in fact data. A `source_exact` department filter preserves
  its supplied literal and does not reuse a same-named customer's identity.
- The plugin is stateless with respect to user confirmation. Correct structured
  role selection is still Hermes's responsibility; a prior ambiguous result or
  a later successful query is not proof that the user confirmed that role.
