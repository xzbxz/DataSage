# Entity guidance

Rule ID: `datasage.entity-guidance/v1`

- Owner: DataSage Skill's model-safe entity guidance.
- Consumers: Hermes on demand; architecture inventory tests.
- Lifecycle: version with the Skill. The plugin registry and domain semantics
  own executable identities; this reference cannot create a mapping.

- Follow [`datasage.query-rules/v1`](query-rules.md): first confirm the exact
  supported capability and metric with `datasage_catalog`, then call
  `datasage_entity_resolve` before `datasage_query` when the entity identity or
  type is not unique. Query validation is not entity discovery and does not
  prove that a submitted filter was user-confirmed.
- Never place `datasage_entity_resolve` and `datasage_query` in the same
  assistant tool-call batch. If the resolver does not return one unique identity
  and role, list its bounded candidates in ordinary assistant text and end the
  current turn. Do not call blocking `clarify`; query only after the user
  confirms the intended candidate.
- Do not fuzzy-select an ambiguous business entity. Unknown natural-language
  geography must not bind to any governed filter value; ask the user to choose
  or provide the mapping.
- Resolve again only when new user context or a newly returned ambiguity
  materially changes the candidate space and another resolution can change the
  result. Never repeat the same input or candidates merely for confirmation.
- A dimension breakdown only enumerates observed labels. It does not prove an
  alias or create a candidate mapping, and must not become a durable Memory fact.
- An empty bounded candidate result does not prove that the entity is absent
  outside the resolver's governed candidate scope.
- Keep stable registered identities in the governed request and show business
  names in the answer.
- Historical analysis must retain departed employees; current-employment status
  applies only to an explicit current-employee question.
