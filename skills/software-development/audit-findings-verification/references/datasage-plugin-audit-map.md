# DataSage Plugin Audit Map (verified anchors)

Read-only cross-verification notes from auditing a 5-finding business-semantics & expert-logic review report (2026-08-07, profile `datasage-canary-next`). Useful starting point for future audits of this codebase — file/line anchors verified against the actual sources.

## Layout
- Plugin: `profiles/datasage-canary-next/plugins/datasage-query/` — `contracts.py` (catalog projection + guidance), `analytical_queries.py` (SQL compilers), `tools.py` (execution, `_connection_port`, security logging), `schemas.py` (tool schemas), `entities.py` (entity resolve), `db_security.py`, `runtime_health.py`, `__init__.py` (registers datasage_catalog / datasage_entity_resolve / datasage_query).
- Domain contracts: `skills/<domain>-query/references/{planner-contract.yaml, semantics.yaml}` for domains: target, delivery, receipt, receivable, inventory, customer-risk.
- Shared skill: `skills/common-data-foundation/references/` — expert-playbooks.yaml, answer-boundary.md, query-rules.md, query-policy.yaml, datasets.yaml, entity-registry.yaml, entity-rules.md.

## Verified findings (5) with anchors
1. **completion_rate semantics conflict (属实)** — `analytical_queries.py:1500-1509` computes `actual/target` with NO clamp; actual is net outbound (components include `sign: -1` return deduction, `semantics.yaml:157-159`), so completion can be >1 or <0, while `semantics.yaml:135` `unit_policy` claims "完成率为比例（0至1）". Planner rule (target-query planner-contract.yaml:49) explicitly says 净实际允许为负.
2. **Catalog dimension promise vs compiler cap (部分属实)** — schema `maxItems: 5` (schemas.py:115); planner claims `max_business_dimensions: 5` (planner-contract.yaml:20) and "最多五个" (:52); but compilers cap: `len(selected) > 2` (analytical_queries.py:556), `> 1` (:909, :1055), target_completion checks type only but error text claims 2 (:1247-1249). Catalog `_catalog_metric_detail` (contracts.py:1517-1588) returns full `allowed_dimensions` with NO combination cap; summary/expert_index views return only `supports_dimensions` bool. Gap: catalog never promised combinations, planner prose did.
3. **Orphan planning guidance (属实)** — `contracts.py:1162-1169` builds `guidance` from `_MODEL_GUIDANCE_KEYS` (:39-50: planning_rules, intent_routes, recipe_policy, recipes, analysis_recipes, answer_boundary, answer_contract, tool_planning...). Consumers are only internal validators (`_validate_change_guidance` :639, `_validate_customer_risk_recipe_requests` :866, `_validate_target_change_handoff` :580). Catalog views `_catalog_summary` (:1219), `_catalog_expert_index` (:1263, docstring: "deliberately omits ... broad planning guidance"), `_catalog_metric_detail` (:1517) — none project guidance. Zero consumers outside contracts.py.
4. **Progressive skill loading rule defeated (属实)** — `datasage/SKILL.md:68-72` & `:154-157` require "do not open the common-data-foundation root first"; `common-data-foundation/SKILL.md:3,12-13` agrees. But `logs/agent.log` shows two consecutive `skill_view` calls (~8726 chars ≈ datasage SKILL.md 7579B; ~1711 chars ≈ common-data-foundation SKILL.md 717B, matched via `wc -m`) before `datasage_catalog` on a simple single-metric query (lines ~226-227, 262). Note: log lines don't record skill_view name args — inference is size-based.
5. **Dark spots**:
   - 5a cronjob attach_to_session silently dropped (属实): schema `cronjob_tools.py:1033`, signature :637, create forwards :710, update :888-889, persist `cron/jobs.py:1298/1393`, consumed `cron/scheduler.py:633` — BUT `registry.register` handler lambda (`cronjob_tools.py:1068-1102`) omits `attach_to_session` → model calls silently ignored.
   - 5b DB port fail-open (属实): `tools.py:533-544` `_connection_port()` — empty/non-numeric → returns 3306; out-of-range clamped to [1,65535] instead of error.
   - 5c profile path duplicated in system prompt (属实, core code): `hermes-agent/agent/system_prompt.py:423` `f"{get_hermes_home()}/profiles/{active_profile}/"` — with HERMES_HOME already `.../profiles/datasage-canary-next`, produces duplicated `profiles/datasage-canary-next/profiles/datasage-canary-next/`. Visible verbatim in the session's own system prompt.

## Log facts
- Simple query ("帮我看下本月整体出库金额是多少？") → skill_view ×2 → datasage_catalog → datasage_query (delivery, metric mode, row_count=1, data_state=rows).
- `datasage_query` log line carries query_id/request_id/status/data_state/row_count/elapsed_ms/session_ref/task_ref.
- `datasage_database_plaintext_transport production_mode=false` warning precedes each query (TLS not configured; transport_mode=plaintext, grants_verified=True).
- Startup health: `runtime_health.py` logs `datasage_startup_health component=database_query ... ready=True`.

## Remediation summary (as delivered)
- F1: align `unit_policy` with implementation (or clamp in SQL) + regression tests for >1 and negative completion.
- F2: versioned `max_group_dimensions` per query_kind in planner-contract; compiler reads single authority; catalog returns cap with allowed_dimensions; consistency assert in contracts.py.
- F3: either project key planning rules in catalog views or shrink `_MODEL_GUIDANCE_KEYS` to validated-only subset + startup orphan-key assertion.
- F4: negative trigger on common-data-foundation description or per-file load policy; verify via log replay (skill_view count).
- F5a: add `attach_to_session=args.get("attach_to_session")` to handler lambda + registration-chain test. F5b: fail-closed on invalid port. F5c: build path from root (hermes-agent core fix + test for HERMES_HOME=profiles/X).
