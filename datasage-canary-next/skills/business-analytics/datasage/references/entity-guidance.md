# Entity guidance

Rule ID: `datasage.entity-guidance/v1`

- Owner: DataSage Skill's model-safe entity guidance.
- Consumers: Hermes on demand; architecture inventory tests.
- Lifecycle: version with the Skill. The plugin registry and domain semantics
  own executable identities; this reference cannot create a mapping.

- Call `datasage_entity_resolve` only for entity search or after exact preflight
  cannot prove a unique identity.
- Do not fuzzy-select an ambiguous business entity. Unknown natural-language
  geography must not bind to any governed filter value; ask the user to choose
  or provide the mapping.
- Resolve again only when new user context or a newly returned ambiguity
  materially changes the candidate space and another resolution can change the
  result. Never repeat the same input or candidates merely for confirmation.
- A dimension breakdown only enumerates observed labels. It does not prove an
  alias or create a candidate mapping, and must not become a durable Memory fact.
- Keep stable registered identities in the governed request and show business
  names in the answer.
- Historical analysis must retain departed employees; current-employment status
  applies only to an explicit current-employee question.
