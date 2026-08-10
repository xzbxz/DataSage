# Audit findings & remediation log (datasage-canary-next, 2026-08-07)

Source: independent audit report + 3-subagent cross-verification (all read-only).
Final verdict: No-Go for production; query compiler/evidence skeleton at candidate level,
overall security governance not at "business digital expert" standard.

## Verified facts baseline

- 762 files / ~14.4 MB scanned; 100 Python, 21 YAML, 17 JSON, 337 Markdown — no parse errors.
- Plugin tests: 91 passed / 1 failed (missing `evaluation/` in installed distribution);
  `--import-mode=importlib` required due to hyphenated package `datasage-query`.
- Machine has 6 profiles; other profiles hold ~1,100+ sessions / ~44K messages (datasagecore
  932 sessions / 23,851 msgs; datasage 181 / 19,826) — cross-profile search is not an empty risk.

## P0 findings (paths + line numbers)

1. **WeCom cross-user/cross-profile session search** — config.yaml `platform_toolsets.wecom`
   included `session_search`; `allow_from: ['*']`, `group_allow_from: ['*']`,
   `allow_admin_from: []`. `session_search_tool.py:829` has NO user_id/chat_id filter and
   accepts a `profile` param; gateway layer never injects caller identity. Confirmed real.
2. **Cron privilege escalation** — wecom toolset included `cronjob`; `cronjob_tools.py:1024`
   lets the model set arbitrary `enabled_toolsets` (no server-side whitelist);
   `scheduler.py:214` prefers per-job toolsets when `platform_toolsets.cron` unset →
   terminal/file/code_execution reachable from chat. Confirmed real; 0 cron executions yet.
3. **Plugin-private execution contracts readable+writable** — CONTRACT_INDEX.md:46 claims model
   never sees physical mapping, but datasets.yaml (33.8K), entity-registry.yaml,
   query-policy.yaml lived under `skills/common-data-foundation/references/`; skill_view
   reads any file, skill_manage overwrites (write_approval=false). Confirmed real.
4. **Plaintext DB + high-privilege account** — config.yaml: production_mode=false,
   canary_accept_existing_account=true, require_tls=false. `db_security.py:251` canary branch
   only parses SHOW GRANTS, never rejects ALL PRIVILEGES. agent.log:69-73 shows
   transport_mode=plaintext, observed_privileges=ALL PRIVILEGES,LOCK TABLES,PROCESS,
   REPLICATION CLIENT,REPLICATION SLAVE,SELECT,SHOW VIEW,XA_RECOVER_ADMIN, then a successful
   real business query. "Production database" label is strong inference, not direct proof.
5. **Release identity broken but reported ready** — distribution.yaml:41 declares `.release`
   owned; `.release/RELEASE.json` absent. `runtime_health.py:177` non-production returns
   ready=true, state=source; startup log shows version=source artifact_id=unreleased.

## Semantic/logic issues (cross-verified)

- **Completion rate conflict** — semantics.yaml:135 says rate ∈ [0,1] but analytical_queries.py:1500
  computes actual/target with no clamp; net actual can be negative → rate >1 or <0. Fix: align
  wording or add explicit clamp; add 150% and negative-rate test cases.
- **Dimension combination promise exceeds compiler** — schemas allow 5 dimensions,
  planner-contract says max 5, but compiler enforces 1-2 per query kind; catalog returns full
  allowed_dimensions list without combination cap. Also: target_completion error message claims
  "2 dimensions" but code never checks count. Fix: versioned max_group_dimensions consumed by
  both catalog and compiler.
- **Orphan planner authority** — contracts.py:1162 builds planning_rules/intent_routes/
  answer_boundary, validated but never projected to model (expert_index docstring: "deliberately
  omits broad planning guidance"). Zero consumers outside validation. Fix: project key rules or
  prune _MODEL_GUIDANCE_KEYS; add orphan-key startup assertion.
- **Progressive loading broken** — datasage SKILL.md says don't load common-data-foundation root
  first, but Hermes generic skill prompt forces "partially relevant must load"; logs show two
  consecutive root skill_views before catalog. Fix: negative trigger wording or demote to
  reference file.

## Other findings

- cronjob `attach_to_session` declared in schema/signature/persistence but handler lambda
  (cronjob_tools.py:1068-1102) never forwards it — silently ignored.
- Invalid DB port silently falls back to 3306 (fail-open) — tools.py:533-544.
- Active profile path doubled in system prompt (`profiles/<profile>/profiles/<profile>/`) —
  agent/system_prompt.py:423 (core code, not plugin).
- Staging polluted: 104 .pyc / 25 __pycache__ / skills/.curator_state / .usage.json(.lock) /
  .hub/* runtime state brought into distribution; violates AGENTS.md.
- Simple metric query ≈20.7s, 5 model calls, context 19.3K→29.4K tokens; 72 skills dilute attention.
- Business query texts logged verbatim at INFO (agent.log:57), size-based rotation only.
- Query results sent to api.deepseek.com; browser/web/TTS outbound surfaces present.
- PyMySQL 1.2.0 verified clean: official wheel sha256 matches requirements.txt, RECORD 24/24,
  22 files byte-identical (recomputed independently, not just cited).

## Remediation executed (this session)

1. **WeCom toolset trim (config.yaml)**: wecom = [datasage-query, clarify, todo];
   removed web/browser/vision/image_gen/tts/skills/memory/session_search/delegation/cronjob.
   `platform_toolsets.cron = [datasage-query]`. `allow_admin_from = [zhangzhengwei]`;
   allow_from kept '*' by explicit user decision (all-employee open chat; risk contained by
   toolset trim). Backup: config.yaml.bak-20260807. Gateway restarted, WeCom connected.
2. **Context bloat**:
   - compression.enabled=true, threshold=0.50, target_ratio=0.20 (hermes config set, official keys).
   - datasage SKILL.md: added "Analysis-class planning rules" — prefer complete_change_decomposition
     over multiple Top-N; ≤1 ranking per turn; ≤3 requests per batch; priority guidance, not hard caps.
   - tools.py limit resolution: `ranking_cap = 10 if order_by else environment_cap`.
     Verified 6/6 (ad-hoc) + real query; 35,907 chars → 14,923 chars output, quality preserved
     (answer still labeled truncated/observation vs causal).
   - Key lesson: DO NOT lower max_result_bytes/max_cell_chars — they are fail-closed
     (OUTPUT_TOO_LARGE error), lowering them breaks legitimate queries.
3. **Contract migration (execution contracts → plugin-private)**:
   - Moved 9 files to `plugins/datasage-query/contracts/`: datasets.yaml, entity-registry.yaml,
     query-policy.yaml, {delivery,receipt,receivable,target,customer_risk,inventory}-semantics.yaml.
   - Updated path constants in contracts.py (_QUERY_POLICY_PATH, 3 semantics f-strings),
     entities.py (_REGISTRY_PATH, _SEMANTIC_PATHS, datasets refs), tools.py (_SEMANTIC_PATHS,
     _QUERY_POLICY_PATH, datasets ref).
   - Deleted old files from skills/*/references/ (verified gone).
   - planner-contract.yaml + expert-playbooks.yaml stay in skills (model-facing guidance).
   - Updated CONTRACT_INDEX.md + common-data-foundation/SKILL.md.
   - Verified 6/6 (ad-hoc): files present, old paths gone, no residual refs, compile OK,
     test suite 91/1 at known baseline, real query succeeded post-restart.
4. **Completion-rate alignment (B)**: target-semantics.yaml both unit_policy lines →
   "可为负，可超过1，不做截断"; planner-contract rule added (不截断到0-1);
   test_runtime_hardening.py updated assertion + 2 new regression tests
   (policy wording check; generated SQL has no LEAST/GREATEST/BETWEEN clamp).
   Verified: 3/3 targeted, full suite 93/1, real query HCM 7月完成率 130.51% with correct
   "不做截断" wording.
5. **Release identity (C / P0-5)**: hermes-agent HEAD was untagged (identity requires
   `_expected_tag_present`), so `git tag datasage-hermes-v0.19.0-dev40` on HEAD, then
   generated `.release/RELEASE.json` with hermes_source from live git (commit/tag/tree_oid/
   uv_lock_sha256) + distribution_version/artifact_id/payload_sha256. Startup log flipped:
   identity_state=source → installed, identity_expected==identity_actual (5268dc39b68b8099).
   Re-tag + regenerate after any hermes-agent update.
6. **Log redaction (D)**: agent/redact.py `redact_message_for_log()` — privacy.redact_pii=true
   → `[redacted len=N hash=H]` fingerprint; wired into turn_context.py conversation-turn log
   and gateway/run.py inbound-message log. logging.max_size_mb=5, backup_count=3.
   Verified: new-process log shows msg='[redacted ...]', no business text; 7/7 ad-hoc.
7. **E-series engineering fixes**:
   - cronjob_tools.py registry handler: added `attach_to_session=args.get("attach_to_session")`
     (was silently dropped — schema/signature/persist had it, handler didn't).
   - tools.py `_connection_port()` fail-closed: non-numeric / out-of-range / 0 raise
     QueryFailure; only unset defaults to 3306.
   - pytest.ini at profile root: `addopts = --import-mode=importlib -p no:cacheprovider`,
     testpaths. Key lesson: pytest 9.0.2 IGNORES the `import-mode` ini key / `-o` override;
     the flag must be in addopts.
   - Staging cleaned: 104 .pyc / 25 __pycache__ / skills/.curator_state / .usage.json(.lock) /
     .hub/* removed; verified 0 leftovers.

## Deferred / next

- P0-4 DB read-only account + TLS + credential rotation (user deferred; highest risk remaining —
  WeCom real traffic is running on plaintext high-privilege connection).
- Completion-rate wording/clamp decision is DONE (aligned to no-clamp); remaining semantic items:
  dimension-combination authority (max_group_dimensions), orphan planner guidance prune,
  progressive-loading trigger fix (common-data-foundation demote), evaluation/ include-or-skip.
- Re-tag hermes-agent + regenerate RELEASE.json after any hermes-agent update.
- system_prompt.py:423 doubled profile path (core code fix).
