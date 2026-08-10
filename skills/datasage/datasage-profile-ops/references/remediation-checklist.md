# Canary-Hardening Remediation Checklist (worked example)

From the 2026-08-07 session hardening `datasage-canary-next` (No-Go audit →
pre-launch). Order and evidence types below are the ones that worked.

## Remediation order that worked

1. **WeCom toolset trim (config.yaml)** — remove session_search, skills,
   memory, cronjob, delegation from `platform_toolsets.wecom`; keep
   datasage-query/clarify/todo. Also add `platform_toolsets.cron:
   [datasage-query]` so cron cannot escalate to terminal/file. Effect: three
   P0s closed with four config lines. NOTE: use Python block replacement +
   yaml.safe_load validation, NOT `hermes config set` for list keys.
2. **Contract migration** — move execution contracts
   (datasets.yaml/entity-registry.yaml/query-policy.yaml + each domain
   semantics.yaml) from `skills/*/references/` to
   `plugins/datasage-query/contracts/`. Update path constants in
   contracts.py/entities.py/tools.py (profile-root-relative strings). Keep
   planner-contract.yaml in skills (model-visible guidance). Update
   CONTRACT_INDEX.md and common-data-foundation/SKILL.md to match reality.
3. **Completion-rate semantics alignment** — semantics said 0-1 but SQL does
   actual/target with no clamp. Fix: change unit_policy to "可为负，可超过1，
   不做截断" (both delivery and receipt target metrics), add planner rule, add
   tests asserting no LEAST/GREATEST clamp + 150% case.
4. **Release identity** — generate `.release/RELEASE.json` matching the
   hermes-agent git checkout; tag HEAD; verify startup log
   identity_state=installed with expected==actual fingerprint.
5. **Log redaction** — add `agent/redact.py:redact_message_for_log(text)` that
   returns `[redacted len=N hash=H]` when privacy.redact_pii=true; use it in
   agent/turn_context.py and gateway/run.py inbound-message logs. Tighten
   logging rotation (max_size_mb 5, backup_count 3).
6. **Engineering fixes** — cronjob handler forward attach_to_session (schema
   had it, handler dropped it); `_connection_port` fail-closed on invalid
   DATA_QUERY_MYSQL_PORT (was silently defaulting to 3306); pytest.ini with
   addopts `--import-mode=importlib`; clean staging of __pycache__/.pyc and
   skills runtime state (.curator_state/.usage.json/.hub).
7. **Pre-launch review** — 3 parallel subagents for 快/稳, 准, 活 dimensions
   (log stats + contract checks + few real queries). Found identity
   CHECKOUT_DIRTY (from post-RELEASE code edits) → re-commit + re-tag +
   regenerate RELEASE.json + restart.

## Verification evidence pattern

- After ANY code/config change: run plugin pytest
  (`python -m pytest --basetemp=...` with pytest.ini in place) → 93 passed +
  1 skipped.
- Check startup health line:
  `grep datasage_startup_health logs/agent.log | tail -1` must show
  `ready=True reason_code=None identity_state=installed`.
- Real query smoke: `hermes chat -q "HCM部门7月出库目标完成率是多少"` → look
  for `"status":"success"` and no `readiness_blocked` in logs.
- Ad-hoc verification scripts: create under
  `C:/Users/<user>/AppData/Local/Temp/hermes-verify-*.py`, run, then delete.

## E2E batch results (26-sample run)

- 19 OK / 7 TIMEOUT@150s / 0 errors. Retry with E2E_TIMEOUT_SEC=240.
- Known capability gaps surfaced by production questions: supplier purchase
  amount (17), SQ/sample orders (8), profit (5), new-customer count (5),
  headcount efficiency (3), unit price (3). ~50 of 407 questions are outside
  the six domains; ~12 more are Excel/Word export requests (not supported on
  WeCom toolset).
- Multi-turn context is essential: 71/407 questions are follow-up references
  ("hcm呢", "Jolie呢", "1", "继续") that need session memory.

## WeCom question-set analysis helper

For a large production question JSON (`{user_id: [{time, question}]}`),
delegate an offline classification subagent (read-only, no query tools) that
returns: category counts, capability mapping to six domains, unsupported list,
multi-turn groups, and a ~25-item E2E sample. Then run the sample through
`workspace/e2e_runner.py`.
