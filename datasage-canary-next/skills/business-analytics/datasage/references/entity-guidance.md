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
  prove that a submitted filter was user-confirmed. Copy the selected
  candidate's opaque `resolution_receipt` into the later request's
  `resolution_receipts`; the receipt binds the selected identity, role, metric,
  current contracts, and (when available) the trusted session only. It never
  proves user confirmation. If the host cannot provide a trusted session id,
  the resolver marks the receipt `session_unbound`; it is informational only and
  cannot authorize a query. Resolve again inside a trusted session, and never
  call either form confirmation evidence.
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
- Resolution receipts and temporary entity scopes are transient tool outputs;
  never put either in Memory. If a query rejects a missing, stale, or mismatched
  detail receipt with `recovery_action: reload_metric_detail`, reload only the
  returned minimal catalog request and do not reconstruct the receipt.
- An empty bounded candidate result does not prove that the entity is absent
  outside the resolver's governed candidate scope.
- Keep stable registered identities in the governed request and show business
  names in the answer.
- Historical analysis must retain departed employees; current-employment status
  applies only to an explicit current-employee question.
