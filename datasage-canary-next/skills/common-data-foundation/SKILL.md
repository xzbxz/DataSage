---
name: common-data-foundation
description: Internal DataSage references; not a standalone workflow.
---

# Common Data Foundation

DataSage exposes approved, bounded planning and interpretation guidance
(playbooks, answer boundary, query rules, a model-safe entity projection, and
planner sections) through the `datasage_reference` facade.
Execution contracts (datasets, entity registry, query policy, domain
semantics) live in the plugin-private `plugins/datasage-query/contracts/`
directory and are read only by plugin code — they are not exposed through
`skill_view` or `skill_manage`.

This Skill does not select a business domain, authorize a metric, define a
user-facing workflow, or permit a query.

Read only an enumerated `source_id` and `section_id` named by the DataSage
Skill; never request a path or use this root as a discovery step. Treat the
returned material as planning and interpretation guidance. The plugin's
registered contracts and validators remain the only query authority.
