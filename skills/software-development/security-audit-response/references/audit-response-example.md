# Worked Example: datasage-canary-next audit (2026-08)

Demonstrates the security-audit-response workflow on a real audit. Evidence
map, subagent split, roadmap shape, and the plain-language explanation
pattern. Reuse the evidence map when this profile's remediation continues.

## Context

- Subject: Hermes profile `datasage-canary-next` (governed business-query
  assistant, DataSage plugin). Machine: Windows, 6 profiles present
  (`datasage`, `datasage-canary-next`, `-legacy`, `-sourcecopy`,
  `-v012-dev1`, `datasagecore`).
- Original audit verdict: No-Go — "compiler/evidence skeleton at candidate
  level; security governance, semantics closure, domain planning, production
  evidence not at business-expert standard." User asked: cross-check with
  subagents, output remediation plan, do NOT change anything yet.
- Session produced: (a) own spot-verification, (b) 3-subagent cross-review
  (17/35/18 API calls), (c) roadmap, (d) plain-language 改前/改后 explanation
  at user request ("你说的这些都好专业").

## Evidence map (paths are MSYS-readable on this host)

P0-1 WeCom session_search exposes all sessions across profiles:
- config.yaml:14-31 wecom toolsets include session_search/skills/memory/
  cronjob/delegation; :79-95 allow_from=['*'] group_allow_from=['*']
- hermes-agent/tools/session_search_tool.py:829-843 (no user_id/chat_id),
  :876-917 (profile param + scan all profiles), :325-346
- Other profiles: datasagecore 932 sessions/23,851 msgs (412MB), datasage
  181/19,826 (254MB)

P0-2 cron privilege escalation (chat → terminal/file/code):
- config.yaml:30 cronjob in wecom; no platform_toolsets.cron key
- cron/scheduler.py:214-244 per-job enabled_toolsets wins, no whitelist;
  :160-177 disabled list omits terminal/file/code_execution/process
- tools/cronjob_tools.py:1024-1028 enabled_toolsets unvalidated

P0-3 "plugin-private" contracts readable AND writable:
- skills/CONTRACT_INDEX.md:46-58 claims model never sees physical mapping
- skills/common-data-foundation/references/datasets.yaml (33.8K),
  entity-registry.yaml, query-policy.yaml — real files in skill references/
- tools/skills_tool.py:1293-1307 reads any file; tools/skill_manager_tool.py
  allows overwrite; config.yaml:68-69 write_approval=false

P0-4 plaintext + privileged DB account:
- config.yaml:48-52 canary_accept_existing_account=true require_tls=false
- plugins/datasage-query/db_security.py:251-281 canary branch parses grants,
  does NOT reject ALL PRIVILEGES (strict branch :282-349 does)
- logs/agent.log:69-73 transport=plaintext, observed ALL PRIVILEGES,
  LOCK TABLES, PROCESS, REPLICATION*; real query succeeded
- Note: "production DB" label = strong inference (privilege + real data),
  not directly proven by logs

Semantics/logic findings:
- Completion rate: skills/target-query/references/semantics.yaml:135 says
  ratio 0-1; plugins/datasage-query/analytical_queries.py:1500-1509 computes
  actual/target with no clamp (can exceed 1 / go negative)
- Dimension combos: schemas.py:115 maxItems=5, planner-contract.yaml:20/52
  says max 5, but compiler allows 1-2 (analytical_queries.py:556/909/1055);
  catalog returns full allowed_dimensions with no combo cap
- Orphan guidance: contracts.py:1162-1169 builds planning_rules/intent_routes/
  answer_boundary; zero consumers outside contracts.py itself
- Progressive loading ineffective: agent.log shows double skill_view
  (1711+8726 chars) on simple query, both root docs loaded
- cronjob attach_to_session: schema + signature + create path ok, but handler
  lambda at cronjob_tools.py:1068-1102 omits forwarding → silently dropped
- Port fail-open: plugins/datasage-query/tools.py:533-544 non-numeric port
  silently returns 3306
- Profile path duplicated in system prompt: hermes-agent/agent/
  system_prompt.py:423 (HERMES_HOME already profile dir → profiles/<p> twice)

Release/deps/logs:
- distribution.yaml:29-43 declares .release owned but dir missing;
  runtime_health.py:177-201 returns ready=true state=source when non-production
- Staging: 104 .pyc / 25 __pycache__ / skills/.curator_state /
  .usage.json(.lock) / .hub/* — violates profile AGENTS.md
- Tests: pytest collects 92, 1 failed (evaluation/expert-core/cases.yaml
  missing — evaluation/ excluded by design); unittest discover fails on
  hyphenated package but pytest is fine (original report's "pytest collection
  error" was NOT reproduced — a cross-review correction)
- PyMySQL 1.2.0 vendored: wheel sha256 matches requirements.txt, RECORD
  24/24, 22 files byte-identical → NO tampering (verified, a positive)
- logs/agent.log:57 stores full business question text; rotation size-only
  (20MB x 12)

## Roadmap shape (what worked)

1. Data-plane: dedicated SELECT/SHOW VIEW account, TLS+CA, credential
   rotation, canary fail-closed on ALL PRIVILEGES/PROCESS/REPLICATION
2. Config-only toolset cut (highest ROI, 4 lines): remove session_search /
   skills / memory / cronjob from wecom toolsets; tighten allow_from — closes
   P0-1/2/3 entry points at once
3. cron enabled_toolsets server-side whitelist; scheduler protected list
4. Migrate contract yamls into plugins/datasage-query/contracts/; hash
   protection; write_approval=true
5. Semantics: completion-rate doc vs clamp (pick one) + 150%/negative tests;
   max_group_dimensions single authority; orphan guidance project-or-prune;
   attach_to_session forwarding; port fail-closed
6. Release: generate .release/RELEASE.json pre-freeze, health check must not
   return source-ready for declared-installed artifacts; staging cleanup; CI
   gates; unified pytest.ini; log desensitization (drop msg= at INFO)
7. P1/P2: common-data-foundation trigger wording, system_prompt path fix
   (core repo), data classification/DLP, WeCom outbound tool cut

## Plain-language pattern (改前/改后) — the user-facing layer

- 企微大门太宽: before = any WeCom user can search other users'/profiles'
  history, create cron jobs with host terminal access, edit query rules;
  after = WeCom keeps only query capability, allowlist-only. Problem solved:
  stop ordinary employees reaching others' data / host control / rule edits.
- 数据库钥匙: before = admin account + plaintext; after = dedicated read-only
  account + TLS + rotated credentials. Problem: shrink blast radius from
  "whole DB" to "read-only".
- 保险柜账本: before = contract files readable+writable by model; after =
  plugin-private dir, read whitelist, write refused, startup hash check.
- 口径打架: before = docs say 0-1, code computes actual/target unclamped;
  after = aligned + regression tests.
- 合格证: before = distribution claims .release but missing, health says
  ready; after = release identity required, missing = build failure.
- 小暗伤: port typo silently connects 3306; attach_to_session silently
  dropped; business question text stored in logs; staging ships caches.
- Overall framing: 安全层 (don't let chat reach data/host) → 可信层 (consistent
  numbers, traceable installs) → 工程质量层 (no silent failures).

## Reusable tips from execution

- search_files returned 0 for known files; terminal `ls` with exact path
  worked (MSYS path quirk).
- Subagent prompts: require 验证结论 verdicts + path:line evidence + priority;
  they returned 3 corrections to the original report (production-DB label =
  inference; pytest collection error not reproduced; catalog-vs-compiler =
  partially true) — surfacing corrections is the point.
- DataSage query session earlier in the same conversation ran clean with the
  datasage skill; do not conflate audit findings with skill correctness.
