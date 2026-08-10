---
name: datasage-profile-hardening
description: Use when auditing or hardening the DataSage/Hermes profile.
---

# DataSage Profile Hardening

Class-level playbook for reviewing a governed Hermes profile (DataSage) against a security/quality audit and executing remediation without degrading business capability. Covers: cross-verification with subagents, config remediation, execution-contract migration, context-bloat control, verification discipline, and speed/stability pre-launch review.

## Triggers

- User hands over a security/quality audit report for the profile ("审查一下", "No-Go", "对照报告整改").
- Pre-launch review by dimension ("上线前审查" 快/稳) — see the Speed/Stability section below.
- Hardening tasks: platform toolset trimming, cron toolset minimization, DB account/TLS, contract file migration, release identity, log redaction.
- Context-bloat fixes for the query plugin (compression, ranking caps, analysis planning rules).

## Workflow

1. **Verify audit claims yourself before acting** — read the cited files at the cited line numbers; a good audit is precise, but never trust it blindly.
2. **Cross-verify with 3 parallel leaf subagents** (security / semantics-logic / release-deps). Each is read-only, returns 验证结论 + 证据(路径+行号) + 整改方案. Merge, and state corrections explicitly (audits do contain false positives).
3. **Present remediation as 改前/改后/解决什么 in plain language**; user is business-side, not technical. Get confirmation before changing. Offer a staged order (config first, code later).
4. **Execute config changes** with the config-edit workflow below — never via patch/write_file on config.yaml.
5. **Execute code changes with minimal diffs**. Never "optimize" by lowering fail-closed limits (they raise errors, not truncate — that degrades capability).
6. **Verify every change**: targeted ad-hoc verification script (hermes-verify- prefix under Temp), pytest `--import-mode=importlib` for hyphenated packages, `hermes gateway restart`, then a real query smoke test. Report as ad-hoc verification, not suite-green.

## Pitfalls (hard-won)

- **config.yaml is protected** from patch/write_file tools (agent refusal). `hermes config set` is safe only for CLI-known scalar keys (e.g. `compression.*`); for gateway list keys like `platform_toolsets.wecom` it writes a STRING (`'["a","b"]'`) instead of a YAML list, corrupting the structure. Correct path: backup → Python edit with `yaml.safe_load` validation → `hermes gateway restart` → verify.
- **pytest collection fails on hyphenated plugin packages** (`datasage-query`) with default prepend mode. Fix: `python -m pytest ... --import-mode=importlib` with `PYTHONPATH=<profile root>`.
- **pytest 9.0.2 quirk (confirmed 2026-08-07)**: the `import-mode` KEY in `pytest.ini` does NOT take effect (collections still fail with 94 setup errors), but `addopts = --import-mode=importlib -p no:cacheprovider` in the same ini DOES. So to make plain `python -m pytest` work from the profile root, put the flag in `addopts`, not as an `import-mode` ini key. Also set `testpaths = plugins/datasage-query/tests` so the ini actually routes to the plugin tests.
- **Windows pytest temp-dir permission error** (`PermissionError: [WinError 5] 拒绝访问: Temp\pytest-of-<user>`): a pre-existing `pytest-of-*` dir can block collection. Fix: `--basetemp=C:/Users/<user>/AppData/Local/Temp/hermes-pytest` (or delete the stale dir). This is environment state, not a code defect.
- **Plugin result-size limits are FAIL-CLOSED**: `max_result_bytes` / `max_cell_chars` exceeding raises `QueryFailure OUTPUT_TOO_LARGE`, it does NOT truncate. Lowering them breaks legitimate large results. Control bloat at the planning layer instead (skill rules + ranking cap).
- **Ranked queries (`order_by` + `limit`) are the main context-bloat source** in "why/analysis" turns (multiple Top-10 × many columns). Cap ranking limit to 10 at the executor; leave non-ranking limit at environment cap.
- **Contract files under `skills/*/references/*.yaml` are model-readable AND model-writable** (skill_view reads any file, skill_manage overwrites with write_approval off). Execution contracts (datasets, entity-registry, query-policy, domain semantics) belong in `plugins/datasage-query/contracts/`. Model-facing guidance (planner-contract.yaml, expert-playbooks.yaml) stays in skills.
- **After contract migration**: grep for residual old paths (`semantics|datasets|entity-registry|query-policy`), update CONTRACT_INDEX.md and common-data-foundation/SKILL.md so the "model never sees physical mapping" claim matches reality.
- **Gateway restart required** after config/plugin changes: `hermes gateway restart`; verify with `hermes gateway status` and gateway_state.json (wecom connected). Toolset changes only apply to new sessions (/reset) — prompt-cache protection.
- **Enable Hermes compression** for long WeCom sessions: `compression.enabled=true, threshold=0.50, target_ratio=0.20` (official defaults; conservative, not degrading).
- **Any uncommitted change in the hermes-agent CHECKOUT fail-closes ALL `datasage_query` calls** (`HERMES_IDENTITY_UNVERIFIED`). The identity gate (`plugins/datasage-query/runtime_health.py`) fingerprints the hermes_cli checkout via `git status` — NOT just the plugin dir. Confirmed 2026-08-07: 4 dirty files in the hermes-agent install (`agent/redact.py`, `agent/turn_context.py`, `gateway/run.py`, `tools/cronjob_tools.py` — leftovers from log-redaction hardening) blocked every query on CLI AND WeCom, even though `identity_expected == identity_actual` (it is a checkout-dirty check, not a hash mismatch). Mechanics: `.release/RELEASE.json` presence forces installed-state verification regardless of `production_mode: false` (runtime_health.py L184: verification runs whenever release_path exists; `_production_identity_required` = production_mode OR `.production-release`); all `HERMES_IDENTITY_*` reason codes collapse to the public `HERMES_IDENTITY_UNVERIFIED` and are NON-retryable (only `DATABASE_*` retry). Diagnose (all read-only): `git -C <hermes-agent> status --porcelain` (dirty = culprit), existence of `.release/RELEASE.json`, then `runtime_health ready=`. **Fix (full sequence, confirmed working 2026-08-07)**: (1) `git add` + `git commit` the dirty files so checkout is clean (`git status --porcelain` = empty); (2) the OLD tag still points at the old commit — delete it and re-tag the NEW HEAD: `git tag -d <tag> && git tag <tag>`; (3) REGENERATE `.release/RELEASE.json` with the new commit/tree_oid/tag/uv_lock_sha256 (git `rev-parse HEAD`, `rev-parse HEAD^{tree}`, `tag --points-at HEAD`, `git show <commit>:uv.lock` hashed) — this is the step people forget: committing alone leaves the release file stale at the old fingerprint; (4) `hermes gateway restart`; (5) confirm the NEWEST `datasage_startup_health` line shows `ready=True reason_code=None` with `identity_expected == identity_actual` (a pre-restart "ready" line is stale); (6) one real `hermes chat -q` smoke test returning `status=success`. **Also: ANY change to `distribution_owned` files (SOUL.md, config.yaml, README.md, etc.) changes `payload_sha256`** — even with a clean checkout you must regenerate RELEASE.json after editing those, or the payload hash goes stale (identity still passes because hermes_source is unchanged, but logs show the old payload hash). Do NOT retry-loop or bypass; the model-facing behavior is correct (honest no-data answers, no fabrication), but root-cause explanations vary run-to-run (one run diagnosed the checkout precisely, another guessed a wrong cause) — standardize the user-facing failure template (error code + likely cause + escalation path).
- **Speed baseline (2026-08-07)**: datasage_query tool p50 ≈ 1.5s (SQL elapsed 467–968ms); catalog 0.09–0.36s; entity_resolve 0.6–1.0s. End-to-end WeCom latency 8.4–46.1s (p50 ≈ 20s) is dominated by 2–7 API calls per turn (1.4–28.1s each), NOT by the tools — big tool outputs (10–36K chars) fed back inflate the next API call. No streaming configured → users wait silently. Compression verified working from logs: input-token drops of 39–94% at ~300K tokens, i.e. it triggers late on large-window models — long sessions pay a slow/expensive phase before the drop.
- **Do NOT copy `evaluation/` into the profile root to fix the failing vocabulary test** — `db_security._profile_deployment_role` classifies a profile as `source` if `(profile_root / "evaluation").is_dir()`, and `canary_existing_account_accepted` returns False for source. Adding evaluation/ to a canary install silently revokes the existing-account exception and fails-closed all queries (observed 2026-08-07; the test went green but every datasage_query broke). Correct fix: edit the test to `skipTest("evaluation corpus not present in this deployment")` when `root/evaluation/expert-core/cases.yaml` is absent (93 passed + 1 skipped, suite stays green AND deployment_role stays canary). Keep evaluation/ only in real source checkouts.
- **Future-period target state**: `target_data_state` previously emitted `missing`/`zero` for future months (period_state=not_started), which a model could misread as "目标缺失/为0". Fix in `analytical_queries.py _target_completion_query`: prepend `CASE WHEN {period_state} IN ('not_started','includes_future') THEN 'not_set_for_future'` BEFORE the missing/incomplete/zero branches, and add the matching planner-contract rule ("target_data_state=not_set_for_future → 业务文字写 尚未开始/未到期间，不得描述为目标不完整或目标为0").
- **After trimming platform toolsets, constrain capability claims in SOUL.md**: the model answers "你能做什么/niyounaxiegongneng" from its identity file, not from the actual toolset. A trimmed WeCom surface (only datasage-query/clarify/todo) still produced an overclaim ("上网查资料、读文件、导出Excel/PDF") because SOUL.md didn't state the boundary. Add a `Platform capability boundaries are authoritative` rule: describe only capabilities backed by tools actually exposed on the current platform; say plainly when a capability is unavailable. Also: editing SOUL.md (a distribution_owned file) changes payload_sha256 → regenerate RELEASE.json (see identity-gate pitfall).
- **E2E batch testing on production question sets**: when the user supplies a real production WeCom question corpus (e.g. `Desktop/all_wecom_questions.json`, 23 users / 407 questions), do NOT run all 407 through `hermes chat -q` (hours of runtime + API cost) and do NOT run them one-by-one manually. Workflow: (1) dispatch one leaf subagent to classify all questions offline (pure Python, no tools): per-category counts, current-capability mapping vs the 6 domains, explicit list of unsupported items (利润/人效/物流线/样品SQ/客诉/导出Excel/Word/采购额/订单原数据 etc.), multi-turn context-dependent fragments, cross-language samples (Vietnamese/Indonesian/English), noise rows (`' reply_to_id=None'`, emoji, background-process markers); (2) have it return a representative ~20–25 item sample covering every answerable category + ≥1 per domain + time-grain/dimension variety + ≥1 cross-language + one multi-turn group + edge cases; (3) run the sample through a small batch runner (read sample JSON → `hermes chat -q` per item with timeout → record elapsed_s/exit_code/answer_len/answer_tail → write results JSON); expect ~30–40s per item and occasional single-query timeouts (model latency, retry once; do not treat as runner failure); (4) summarize per-category pass/fail + unverifiable items. Full runner + sampling recipe: `references/e2e-production-questions.md`.

## Speed/Stability pre-launch review

Read-only log forensics on `<profile>/logs/` + one live `hermes chat -q` probe (control cost). No file edits. Full recipe, regexes, and per-line semantics in `references/speed-stability-review.md`.

1. **E2E latency**: grep `response ready: platform=wecom ... time=Ns api_calls=N` in agent.log → per-user-turn wall-clock. This is the only true e2e signal for gateway users.
2. **Tool vs API split**: `tool X completed (Ns, N chars)` gives tool cost (datasage_query ≈ 1s); `API call #N ... in=N out=N latency=Ns cache=N/N` gives model cost (the real latency driver). Compute percentiles per session.
3. **Compression verification**: adjacent API calls whose `in=` drops >30% = compression event. No drops + long session = compression not triggering (bloat risk). Also sample `in=` trajectory to find peak-before-compress.
4. **Success/failure**: parse `datasage_query {...}` and `datasage_query_batch {...}` JSON lines → success/failed/partial/error_codes, `data_state` (rows vs truncated = expected cap, not error).
5. **Runtime health / identity**: `datasage_startup_health ... ready= reason_code= identity_state= identity_expected/actual=` — ready=False with a reason_code is a fail-closed state; check it BEFORE and AFTER any live probe, and distinguish stale earlier "ready" lines.
6. **Gateway stability**: gateway-exit-diag.log start/exit_clean pairs (crash vs clean restart), gateway-stdio.log websocket drops, gateway_state.json current state.
7. **errors.log triage**: dedupe by stripping timestamp/session-id/numbers; classify noise (auxiliary clients unconfigured, browser check_fn False) vs real (CONTRACT/OUTPUT_TOO_LARGE/timeouts). Report the failure rate over actual query batches, not log line counts.
8. **Live probe**: `timeout 180 hermes chat -q "<typical business question>"` — 1–2 probes max. If blocked by identity/readiness, report the reason_code; do NOT retry-loop a fail-closed path.
9. **Report shape**: per-dimension verdict (达标/基本达标/不达标), a risk table ordered by severity (blocking items first with exact reason_code), and evidence numbers (n, p50, p90, max).

## Accuracy pre-launch review

Read-only contract/code cross-check + 3-5 live probes through the normal authenticated Hermes/DataSage facade. Require runtime readiness to report `ready=True` before every live probe. If the identity gate blocks the agent, stop, report the reason code, repair the release identity through the governed operations workflow, restart the gateway, and re-check readiness. Never import plugin internals or invoke a tool entry directly to work around readiness. Full checklist and 2026-08-07 findings are in `references/accuracy-review.md`.

1. **口径一致性**: for 3-5 representative metrics, cross-check the contract definition (semantics.yaml `business_definition`/`unit_policy`/formula components) against the implementation (`analytical_queries.py`) — verify net=毛-退 signs, completion=actual/target no-clamp, currency separation, time-range expansion.
2. **数字正确性**: 3-5 live probes; verify returned numbers are self-consistent (e.g. 净=毛-退 reconciles), unit/currency labels correct, period bounds right. Cross-reproduce one value through a different entry (e.g. a customer's amount from a ranking session) to confirm determinism.
3. **边界处理**: confirm zero/missing/negative/truncated/future states are structurally typed (three-state target CASE, NULL completion for unset target, `not_started` for future months, `SOURCE_TRUNCATED` + no full-population claim on Top-N).
4. **证据边界**: from logs, check that "why/analysis" answers obey — truncated Top-N is observation only, no causal/driver claims, no joint inference from independent marginals, honest "可以确定的/不能确定的" split.
5. **实体解析**: verify aliases (HCM→胡志明, Mộc Miên→customer) resolve deterministically via preflight/entity-registry, with strict isolation (no prefix expansion).
6. **Report shape**: per-item 达标/基本达标/不达标 + evidence (numbers, contract/code line numbers, log excerpts); separate environment blockers (identity-gate) from logic defects — a dirty checkout blocking the probe is a release precondition, not an accuracy failure.

## Agility/Flexibility pre-launch review

Read-only forensics on `<profile>/logs/` + `state.db` transcripts + 2-3 live `hermes chat -q` probes (control cost). No file edits. Full recipe, extraction SQL, rubric, and 2026-08-07 findings in `references/agility-review.md`.

1. **Reconstruct full transcripts from the `state.db` `messages` table** — agent.log only records user msg + timings, NOT assistant text or query payloads. The `tool_calls` JSON column on assistant rows holds the EXACT request arguments (metric codes, batch size, `order_by`/`limit`, `evidence_role`, `time_bucket`) — the primary evidence for rule-compliance auditing.
2. **Rubric (5 items)**: 问法多样性 / 多轮追问 / 歧义处理 / 复杂问题(为什么类) / 体验细节; per-dimension verdict 达标/基本达标/不达标 + overall rating.
3. **Rule baseline**: compare observed request shapes against `skills/target-query/references/planner-contract.yaml` and the analysis-class planning rules; check skill/contract file mtimes vs session timestamps to distinguish pre-rule behavior from regressions.
4. **Live probes**: 2-3 diverse phrasings (pinyin business query, fuzzy ranking, cross-entity why/comparison). If blocked by `HERMES_IDENTITY_UNVERIFIED`, report the reason_code and do NOT retry-loop (see identity-gate pitfall above).

## Audit P0 taxonomy (this profile, 2026-08)

- WeCom toolset included session_search / skills / memory / cronjob / delegation with `allow_from: '*'` → cross-user + cross-profile session reads, and a chat→host-terminal privilege chain via cron `enabled_toolsets`.
- Plaintext DB connection with ALL PRIVILEGES account (`canary_accept_existing_account` skips privilege checks).
- Execution contracts readable/writable by model despite CONTRACT_INDEX claim.
- Unreleased version (`state=source`, no RELEASE.json) reported `ready=true`.
- Context bloat: 5-turn WeCom session reached 51K tokens (ranked Top-10 outputs).
- Details in references/audit-findings.md.

## References

- `references/audit-findings.md` — P0 findings with paths/line numbers, cross-verification corrections, and remediation log (what changed, what was deferred).
- `references/accuracy-review.md` — 准/accuracy review checklist with fail-closed identity handling, 口径/数字/边界/证据/实体 checks, and 2026-08-07 verified numbers (net-delivery reconciliation, completion-rate 130.51%, cross-entry reproducibility).
- `references/speed-stability-review.md` — log-forensics recipe for the 快/稳 pre-launch review: exact grep patterns, percentile math, compression detection, error-dedupe, and the 2026-08-07 baseline numbers + CHECKOUT_DIRTY incident timeline.
- `references/agility-review.md` — 活/灵活性 review recipe: state.db transcript extraction (SQL), request-shape auditing against planner-contract, 5-item rubric, live-probe pattern, and 2026-08-07 findings (pinyin feature-list overclaim, 出库-only default deviation, failure-answer inconsistency, identity-gate blocker).
- `references/e2e-production-questions.md` — E2E batch testing on a production WeCom question corpus: offline classification subagent, representative sampling, batch-runner pattern (hermes chat -q with timeout), per-category pass/fail reporting, and pitfalls (per-item latency, single-query timeouts, identity-gate mid-batch).
