# Entity guidance

Rule ID: `datasage.entity-guidance/v1`

- Owner: DataSage Skill's model-safe entity guidance.
- Consumers: Hermes on demand; architecture inventory tests.
- Lifecycle: version with the Skill. The plugin registry and domain semantics
  own executable identities; this reference cannot create a mapping.

- Follow [`datasage.query-rules/v1`](query-rules.md): call
  `datasage_entity_resolve` only when the entity identity, type, or filter role
  is not unique. Query validation is the execution authority and performs an
  exact identity check again; a resolver result never proves user confirmation.
- If the resolver does not return one unique identity and role, list its bounded
  candidates in ordinary assistant text and end the current turn. Do not call
  blocking `clarify`; after the user confirms a candidate, send the selected
  token to `datasage_query` for exact revalidation.
- Do not fuzzy-select an ambiguous business entity. Unknown natural-language
  geography must not bind to any governed filter value; ask the user to choose
  or provide the mapping.
- Resolve again only when new user context or a newly returned ambiguity
  materially changes the candidate space and another resolution can change the
  result. Never repeat the same input or candidates merely for confirmation.
- A dimension breakdown only enumerates observed labels. It does not prove an
  alias or create a candidate mapping, and must not become a durable Memory fact.
- Candidate mappings are transient conversation context; never put them in
  Memory. A query rejection for an unknown, ambiguous, or role-incompatible
  entity requires clarification or a stable identifier, not a guessed filter.
- An empty bounded candidate result does not prove that the entity is absent
  outside the resolver's governed candidate scope.
- Keep stable registered identities in the governed request and show business
  names in the answer.
- Historical analysis must retain departed employees; current-employment status
  applies only to an explicit current-employee question.

- `resolution_scope` describes only the considered entity types and sources.
  An unsearched type is not proved absent. A single customer candidate plus an
  unsearched department type does not justify a type-neutral business binding;
  clarify the intended type without inventing a department candidate.
- Department discovery covers registered aliases only; unregistered department
  literals can exist in fact data. After the user explicitly selects a
  `source_exact` department, send its exact supplied value in the complete query's
  department filter. Query validation preserves that literal and does not reuse
  a same-named customer's identity. Do not repeat an unsupported department
  resolver lookup merely to confirm the already supplied literal.
- The plugin is stateless with respect to user confirmation. Correct structured
  role selection is still Hermes's responsibility; a prior ambiguous result or
  a later successful query is not proof that the user confirmed that role.
