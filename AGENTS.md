# DataSage Mini Next Engineering Contract

- Preserve separation between language interpretation, semantic authorization,
  query compilation, execution and answer synthesis.
- Business metrics, dimensions, physical mappings and organization constants
  belong in versioned contracts, not prompt routing code.
- Never accept SQL, table names, joins, formulas or unrestricted filters from a
  model-facing tool call.
- Every query path must be parameterized, bounded, timed out and read-only.
- Add a failed natural-language example to evaluation before changing behavior.
  Do not repair one example with question-specific keywords or blacklists.
- Every change must state its owning layer and the invariant it preserves.
- Keep runtime secrets, `.env`, auth files, sessions, logs, state databases,
  caches and `__pycache__` outside the distribution.
- Release order is: static safety, offline contracts, model replay, read-only
  database reconciliation, clean install, WeCom canary and rollback rehearsal.
- Mocked tests alone cannot approve production.

