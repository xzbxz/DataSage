# Entity rules

> Maintainer-only rationale. This file is not a model or runtime contract and must not be loaded as a second semantic authority. Runtime identity behavior comes from `entity-registry.yaml` plus each domain's execution semantics; model-visible identity behavior comes only from the generated contract projection. Keep this note solely for release-review traceability.

1. Follow the selected domain semantics for attribute precedence. A field stored on a fact row is not automatically a transaction-time historical snapshot.
2. When a fact already contains the domain-approved customer, product, color, or supplier attribute, use that copy instead of joining the same master attribute again.
3. When the fact genuinely lacks an attribute, current master data may be used only through a registered governed dimension. Treat it as a current-attribute supplement, not historical truth.
4. Join customer master data by `customer_id` only after applying `is_delete = 'n'` and `is_void = 'n'` inside the LEFT JOIN condition.
5. Join product master data by `goods_id` only after applying `is_delete = 'n'` and `is_void = 'n'` inside the LEFT JOIN condition.
6. Supplier and SKU enrichment must use their stable IDs and the domain-declared relationship; do not match by names.
7. Aggregate facts to the required grain before joining another fact or target dataset. A registered many-to-one master join may occur before metric aggregation when grouping by that master attribute.
8. Do not fuzzy-match an ambiguous organization, department, customer, salesperson, product, color, or supplier into one record. Return candidates or ask one concise clarification.
9. Preserve stable IDs in query plans; expose business names rather than internal IDs when answering users.
10. Fact `sales_id` or `final_sales_id` maps to `employee_dwd.person_id`. The mixed `employee_dwd.id` and person names are never fact join keys.
11. Employee master attributes are current supplements only. They must not overwrite transaction salesperson ownership, historical department, or split allocation.
12. Historical fact analysis must retain departed employees. Apply current employment status only to an explicit current-employee question.
13. `employee_dwd` is not a governed many-to-one metric dimension until `person_id` uniqueness or an owner-confirmed canonical view is available; a direct join would risk multiplying facts.
