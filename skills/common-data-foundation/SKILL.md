---
name: common-data-foundation
description: Internal governed reference library for DataSage. Load a specific linked file only when the DataSage Skill directs it; this is not a standalone workflow or query authority.
---

# Common Data Foundation

This Skill exposes DataSage planning and interpretation guidance (playbooks,
answer-boundary, query-rules, entity-rules) through Hermes `skill_view`.
Execution contracts (datasets, entity registry, query policy, domain
semantics) live in the plugin-private `plugins/datasage-query/contracts/`
directory and are read only by plugin code — they are not exposed through
`skill_view` or `skill_manage`.

This Skill does not select a business domain, authorize a metric, define a
user-facing workflow, or permit a query.

Load only the specific `file_path` named by the DataSage Skill; do not load this
root as a discovery step. Treat expert playbooks and answer-boundary documents
as planning and interpretation guidance. The plugin's registered contracts and
validators remain the only query authority.
