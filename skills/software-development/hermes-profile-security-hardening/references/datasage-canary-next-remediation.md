# datasage-canary-next Remediation Record (2026-08-07)

Session-specific audit findings and exact remediation commands for the
datasage-canary-next Hermes profile. Profile root:
`C:\Users\10192\AppData\Local\hermes\profiles\datasage-canary-next`
Hermes source: `C:\Users\10192\AppData\Local\hermes\hermes-agent` (git checkout).

## Audit baseline (from review report, verified by 3 subagents)

- 762 files / ~14.4 MB scanned; 92 plugin tests (91 passed, 1 failed on
  missing `evaluation/` dir); only one real CLI query in logs before this
  session; no WeCom/multi-user/rollback evidence.
- Machine has 6 profiles; cross-profile session search was reachable
  (datasagecore: 932 sessions/23,851 msgs; datasage: 181/19,826).
- Vendored PyMySQL 1.2.0 verified untampered (wheel sha256 + RECORD 24/24 +
  22 files byte-identical).

## P0 findings (all confirmed)

1. WeCom exposed `session_search` (no user_id/chat_id filter, cross-profile
   read reachable), `skills`, `memory`, `cronjob`, `delegation`.
2. Cron fallback granted terminal/file/code_execution via per-job
   `enabled_toolsets` (no server-side whitelist).
3. Execution contracts (datasets/entity-registry/query-policy/semantics yaml)
   lived under `skills/*/references/`, readable via `skill_view` and writable
   via `skill_manage` (write_approval=false).
4. Plaintext + high-privilege DB account in real use (ALL PRIVILEGES,
   transport_mode=plaintext, tls_verified=false in agent.log).
5. Release identity broken: distribution.yaml declares `.release` but no
   `.release/RELEASE.json`; runtime_health returned ready=true/state=source.

## Remediation performed (in order)

### A. WeCom toolset trimming + cron minimization (config.yaml)
```yaml
platform_toolsets:
  cli: [hermes-cli, datasage-query]
  wecom: [datasage-query, clarify, todo]
  cron: [datasage-query]
```
- `allow_from: ['*']` / `group_allow_from: ['*']` kept (business decision:
  all employees may chat; risk mitigated by toolset strip).
- `allow_admin_from: [zhangzhengwei]` (user's WeCom userid).
- KEY PITFALL: `hermes config set platform_toolsets.wecom '[...]'` writes a
  STRING, corrupting the list. Use backup + Python block replace +
  `yaml.safe_load` validation, then `hermes gateway restart`.
- Backup file: `config.yaml.bak-20260807`.

### B. Context explosion fixes
- `hermes config set compression.enabled true` (+ threshold 0.50,
  target_ratio 0.20) — worked via config set (known key).
- `skills/datasage/SKILL.md`: added "Analysis-class planning rules" —
  prefer `complete_change_decomposition` for why questions; <=1 ranking
  request/turn with limit <=10; batch <=3 requests; priority guidance not
  hard caps.
- `plugins/datasage-query/tools.py`: `ranking_cap = 10 if
  request.get("order_by") is not None else environment_cap; limit =
  max(1, min(ranking_cap, environment_cap, requested_limit))`.
- Do NOT lower max_result_bytes/max_cell_chars (fail-closed → errors).
- Result: 5-query/35KB turn → 3-query/15KB turn; answer quality kept
  (explicit truncation caveats).

### C. Contract migration (P0-3)
- Moved 9 files to `plugins/datasage-query/contracts/`:
  datasets.yaml, entity-registry.yaml, query-policy.yaml, and
  {delivery,receipt,receivable,target,customer_risk,inventory}-semantics.yaml
  (customer_risk uses underscore in filename; folder is customer-risk-query).
- Updated path constants in contracts.py, entities.py, tools.py (3 files,
  ~12 references). Planner-contract.yaml stays in skills (model-visible).
- Deleted originals from skills; updated CONTRACT_INDEX.md and
  common-data-foundation/SKILL.md.
- Verified: skill_view no longer exposes yaml; tests pass; real query OK.

### D. Release identity (P0-5)
- `git tag datasage-hermes-v0.19.0-dev40` at HEAD (clean checkout required).
- Generated `.release/RELEASE.json` with hermes_source
  {commit, tag, tree_oid, uv_lock_sha256} + distribution_version
  0.12.0-dev1 + artifact_id + payload_sha256 (sha256 of owned files).
- Verified: startup log changed from `version=source artifact_id=unreleased
  identity_state=source` to `version=0.12.0-dev1 identity_state=installed
  identity_expected==identity_actual`.

### E. Log redaction
- Added `redact_message_for_log(text)` to `agent/redact.py` (reads
  `privacy.redact_pii` via `hermes_cli.config.load_config_readonly`; returns
  `[redacted len=N hash=<sha256-16>]` when true, else 80-char preview).
- Wired into `agent/turn_context.py` (turn-start log) and `gateway/run.py`
  (inbound message log).
- `hermes config set logging.max_size_mb 5` / `logging.backup_count 3`.
- Verified: new-process log shows `msg='[redacted len=14 hash=aa3f66...]'`;
  repo tests pass (test_redact.py 170, test_turn_context*.py 38, gateway
  message tests 10).

## Testing gotchas hit on Windows

- pytest collection PermissionError on `Temp\pytest-of-10192` → use
  `--basetemp=<writable-dir>` (e.g. Temp/hermes-pytest).
- Hyphenated package `datasage-query` needs `--import-mode=importlib` and
  `PYTHONPATH=<profile-root>`.
- Baseline: 91 passed / 1 known fail (evaluation/ not distributed) →
  after adding 2 completion tests: 93 passed / 1 known fail.

## Not done (user deferred)

- P0-4 DB read-only account + TLS (user chose to skip for now; plaintext +
  ALL PRIVILEGES still active).
- E items: cronjob attach_to_session handler forwarding (schema has it,
  registry handler drops it), port fail-open (invalid port → 3306),
  pytest.ini unification, staging cleanup (104 .pyc / 25 __pycache__ /
  .curator_state / .hub).
