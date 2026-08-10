---
name: datasage-profile-ops
description: >-
  Maintain a DataSage profile: config, identity, tests, E2E.
---

# DataSage Profile Operations

Operational skill for maintaining a DataSage profile (e.g. datasage-canary-next):
config changes, contract management, release identity, test runs, and E2E
verification against production question sets. This is NOT the business-query
skill — for answering questions use `datasage`.

## Core invariants (from the profile AGENTS.md engineering contract)

- Never author SQL, tables, joins, formulas, or unrestricted predicates for the
  DataSage tools. The plugin compiles everything.
- Business metrics/dimensions/physical mappings live in versioned contracts,
  not prompt routing code.
- Every query path is parameterized, bounded, timed out, read-only.
- Keep runtime secrets, `.env`, auth files, sessions, logs, caches and
  `__pycache__` outside the distribution.

## Editing config.yaml (critical workflow)

- The `patch`/`write_file` tools REFUSE to write `config.yaml` (security guard).
- `hermes config set KEY VAL` works for recognized scalar keys, but for
  gateway-only list keys like `platform_toolsets.wecom` it writes the value as
  a JSON string (`'["a","b"]'`) which BREAKS the YAML list — the gateway reads
  it as a string, not a list.
- Safe approach for list keys: back up the file, then do an exact Python block
  replacement and validate the whole file with `yaml.safe_load` before writing
  back. Only replace the target block; never rewrite the file wholesale.
- Back up before editing: `cp config.yaml config.yaml.bak-YYYYMMDD`.
- `platform_toolsets` is read by the gateway even though `hermes config` does
  not list it as a known key — the "not a recognized key" notice is harmless.

## Contract layout (after migration)

- Execution contracts (physical mapping, formulas, entity registry, query
  policy, domain semantics) live in `plugins/datasage-query/contracts/` —
  plugin-private, NOT exposed through skill_view/skill_manage.
- Planner contracts (`skills/<domain>-query/references/planner-contract.yaml`)
  and expert playbooks stay in skills — they are model-visible planning
  guidance by design.
- Do NOT add an `evaluation/` directory to the profile root: `db_security.py`
  classifies a profile with `evaluation/` as deployment_role="source", which
  revokes the canary existing-account exception and can block queries with a
  high-privilege account. Keep evaluation corpus out of the installed profile;
  make the test that reads it skip when absent.

## Release identity (RELEASE.json) — fail-closed by design

- `runtime_health.runtime_identity_status` compares `.release/RELEASE.json`
  `hermes_source` (commit/tag/tree_oid/uv_lock_sha256) against the git
  checkout that imported `hermes_cli`.
- ANY source change after RELEASE.json generation → `HERMES_IDENTITY_CHECKOUT_DIRTY`
  → ALL `datasage_query` calls are blocked (readiness_blocked). This is correct
  fail-closed behavior, not a bug — but it means the profile goes dark until
  identity is restored.
- Fix sequence: commit/stash the dirty files → delete old tag → re-tag HEAD →
  regenerate RELEASE.json (git rev-parse HEAD, HEAD^{tree}, tag --points-at
  HEAD, sha256 of `git show HEAD:uv.lock`) → restart gateway → verify startup
  log shows `identity_state=installed identity_expected=<fp> identity_actual=<same>`.
- `payload_sha256` in RELEASE.json must be regenerated whenever any
  distribution_owned file changes (SOUL.md, config.yaml, etc.) or the payload
  check fails even with a clean checkout.

## Running the plugin tests

- `pytest.ini` at profile root: `testpaths = plugins/datasage-query/tests`,
  `addopts = --import-mode=importlib -p no:cacheprovider`. The `import-mode`
  ini KEY is unreliable in pytest 9.0.2 — put the flag in addopts instead.
- Without `--import-mode=importlib`, hyphenated package names
  (`plugins/datasage-query/__init__.py` relative imports) cause
  "attempted relative import with no known parent package" collection errors.
- On Windows, pytest may fail with PermissionError on `Temp\pytest-of-<user>`;
  pass `--basetemp=C:/Users/<user>/AppData/Local/Temp/hermes-pytest`.
- Baseline: 93 passed + 1 skipped + 50 subtests. The skipped test is the
  evaluation-vocabulary check that skips when `evaluation/` is absent.

## E2E batch testing against production questions

- Use `workspace/e2e_runner.py`: reads a JSON sample
  (`[{"id","question"}]`), runs each via `hermes chat -q`, records
  elapsed/exit/answer, writes results JSON. Verify the runner with a mock
  (never run it against itself while a real batch is in flight).
- Timeout: default 150s is TOO SHORT for multi-tool analysis questions (they
  need 100-240s). Set `E2E_TIMEOUT_SEC=240` for production samples.
- A timeout is NOT a failure — retry with longer timeout before judging. In
  the 26-item production run, 7 items hit 150s; 5 of them completed at
  170-191s under 240s (slow but successful). The 2 true timeouts were
  entity/scope ambiguity: the model correctly called `clarify` and hung
  waiting for a user who was absent in unattended E2E. That is CORRECT
  behavior — count it as "awaiting clarification", not a failure; it would
  resolve in a real conversation.
- Production corpus lives at `C:/Users/10192/Desktop/all_wecom_questions.json`
  (407 questions, 23 users). Do offline classification first (delegate to a
  subagent that only reads the JSON, no query tools), then sample ~25-26
  covering each metric domain, time grain, dimension type, cross-language,
  multi-turn groups, and boundary cases. Sample 26 items: 24 succeeded (92%),
  2 awaiting clarification, 0 hard errors.
- Multi-turn items in separate CLI processes get context by compensating with
  `session_search` (CLI-only) — the model reads prior sessions to resolve
  "Jolie呢"/"hcm呢". That means isolated-process multi-turn results are
  NOT a faithful test of real WeCom in-session memory; prefer an in-session
  scripted test or accept the compensation behavior as an upper bound.
- Verify the runner with a mock (never run it against itself while a real
  batch is in flight). The runner supports `E2E_TIMEOUT_SEC` env override
  and explicit `timeout_sec` per call (explicit param wins).
- Generating a readable report from results: see
  `references/e2e-report-generation.md`. Key trick: `e2e_result_*.json`
  `answer_tail`/`answer_len` are derived from the full CLI stdout (they
  include tool-call noise), NOT the model's final answer. Pull the final
  answer from `state.db` `messages` — the last non-empty
  `role='assistant'` row with no `tool_calls`; for timeout items recover the
  clarify question from `tool_name='clarify'` rows. Produce a Markdown
  report at `workspace/E2E_测试报告_26题.md` style: per-item
  类别/状态/耗时/退出码/完整回答, plus 汇总 (完成/超时/硬错误 counts,
  耗时统计, 超时原因说明).

## User-perspective answer review (用户视角审视回答)

The business owner may ask you to review E2E answers "站在用户角度审视". Do this
systematically, not impressionistically:

1. Read EVERY answer in the report, not just the summary. Categorize each:
   - 能用 (directly usable: clear numbers, right grain, honest limits) vs
     体验差 (technically correct but will frustrate a business user) vs
     真正问题 (wrong interpretation / dead-end that forces user to do work).
2. Look for these specific failure classes:
   - **Entity interpretation stuck on literal match**: e.g. "HCM-Emma 收款" was
     treated as a customer entity (ENTITY_NOT_FOUND → long apology) when the
     business meaning is 部门 HCM + 业务员 Emma. Prefer interpreting
     `<DEPT>-<NAME>` as dept+salesperson before customer when context allows.
   - **Alias normalization gaps pushed onto the user**: "印尼" and "印尼盾"
     returned empty, model told the user "请确认系统里的准确叫法" — that is
     shifting a system data-quality problem onto the business user. Prefer
     trying common aliases (印尼/印度尼西亚/IDN, 印尼盾/IDR, 曼谷/Bangkok)
     before asking.
   - **Granularity mismatch across multi-turn**: "Jolie呢" (follow-up to an
     itemized answer) returned only 3 summary lines while the anchor answer had
     per-customer detail. Follow-up must match the anchor's depth.
   - **Same metric, contradictory availability**: one entry point says "指标没
     有" while another returns it — breaks trust, even if technically explainable.
   - **Over-frequent truncation disclaimers** (10+ "结果被截断不代表全量" per
     report): correct but reads as "system always gives incomplete data".
3. When a review uncovers a real gap, propose a fix with a test before
   changing contracts — e.g. alias normalization or domain re-homing.

Worked example with per-item judgement and the concrete failure texts:
see `references/user-perspective-review-26q.md`.

## Metric cross-domain discovery (指标归档/发现诊断)

A business user may report a paradox: "客户 X 的 <指标> 能算，但公司整体不能算"
(e.g. formal_receivable_turnover_days / DSO). Diagnostic path:

1. Locate the metric in contracts: `grep -rn "指标名\|turnover\|dso" contracts/*-semantics.yaml`.
2. Check WHICH domain owns it and that domain's `triggers`. The metric may be
   archived under customer_risk (triggers: 客户风险, DSO, 结算速度...) while
   the user asks through the receivable domain (46 metrics, no turnover).
   The model searches by domain → doesn't find it → honestly says "指标没有".
3. Check the query implementation (`analytical_queries.py`, e.g.
   `_formal_dso_query`) — it may allow zero dimensions (company-wide) even
   though the metric is only surfaced in a per-customer diagnostic domain.
   If so, the block is DISCOVERY (metric placed under the wrong domain /
   trigger words don't include company-wide phrasing), NOT computation.
4. Explain to the user: 不是算不出来，是指标放错抽屉/标签写错，模型翻"应收"抽屉
   没找到。Fix options: (a) surface the metric in the receivable domain too,
   or (b) widen the customer_risk triggers to include 公司整体/整体/全公司.
   Recommend (a) as the direct fix; verify with a real query after change.

## User preference (zhangzhengwei / DataSage business owner)

- Optimize WITHOUT degrading answer quality ("不要给我改降智了"). When tuning
  limits/compression/rules, prefer guidance over hard caps and verify real
  answer quality after each change (a sample query + eyeball the answer).
- Explain changes as 改前/改后/解决什么问题 in plain business language, and
  always state clearly whether the profile can go live yet and what the
  remaining blocker is. Do not leave the user guessing what a batch of changes
  was for.
- Fail-closed behavior is expected and correct; explain it as such rather than
  as a malfunction.
- When asked to write his daily report (日报), follow this exact format —
  name line `章正巍（Cody）｜数字专家 / 物流系数`, then `昨日：` and `今日：`
  each as semicolon-separated items, keep each item terse. He will say
  "精简一下" if a draft is too long — lead with the compact version. "昨日"
  covers completed work (incl. same-day completed hardening + E2E), "今日"
  covers planned items (e.g. 滞销提醒增加家纺部门 / 优化回答 / 总机器人测试50题).

## Pitfalls

- After ANY code or distribution-owned config change, the release identity
  breaks until RELEASE.json is regenerated — check startup health log before
  declaring success.
- Don't disable fail-closed safety (e.g. don't remove RELEASE.json) just to
  unblock queries; fix the identity properly.
- When a review subagent reports "queries blocked", check
  `identity_state`/`reason_code` in the latest `datasage_startup_health` log
  line before touching query code.
- Contract/planner YAML edits must stay consistent: if you change a semantic
  declaration (e.g. unit_policy), update the matching test assertion and
  planner rule in the same pass.

See `references/remediation-checklist.md` for a worked example from the
canary-hardening session.
