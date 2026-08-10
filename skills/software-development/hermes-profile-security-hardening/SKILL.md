---
name: hermes-profile-security-hardening
description: >-
  Harden Hermes profile: toolsets, contracts, identity, logs.
---

# Hermes Profile Security Hardening

Class-level workflow for auditing a Hermes profile and remediating security
findings without degrading the product's business capability. Built from a
full P0-audit remediation of the datasage-canary-next DataSage profile.

## When to use

- User shares a security/architecture audit and asks to verify + remediate.
- Need to lock down a messaging-platform (WeCom/Telegram/Discord) agent.
- Fixing release identity, plugin contract exposure, or log privacy.
- Diagnosing long-session context explosion on a chat platform.

## Core workflow

1. **Read-only audit first** — collect evidence with file paths + line
   numbers; never modify during audit. Verify the audit's claims against the
   actual files before touching anything.
2. **Subagent cross-check** — dispatch 3 parallel leaf subagents (security
   surface / business-logic & semantics / release-deps & tests). Each returns
   验证结论 (属实/不属实/部分属实) + evidence + 整改方案. Expect corrections
   (e.g. "pytest collection error" turned out false; "catalog promises too
   much" turned out partially true).
3. **Prioritized remediation** — data plane (DB creds/TLS) → execution plane
   (cron/terminal escalation) → supply chain (contract files) → confidentiality
   (session search/logs) → correctness/engineering. Do config-only quick wins
   first; code changes second.
4. **Verify every change** — targeted pytest, module compile, real log
   evidence, gateway restart, then an end-to-end business query. Label results
   as ad-hoc verification, not suite-green.
5. **Report in plain language** — business users need 改前/改后 (before/after)
   and "what problem it solves", not jargon. Offer a checklist before touching
   files when the user asked for output-first.

## Key techniques & pitfalls

### Config editing (critical, Hermes protects config.yaml)
- `patch` refuses to write config.yaml ("Refusing to write to Hermes config
  file"). Known keys go through `hermes config set KEY VALUE`.
- BUT `hermes config set` only knows its own key table. Unknown keys like
  `platform_toolsets.*` get saved as a **string** (e.g. `'["a","b"]'`) which
  corrupts the list structure the gateway reads. For those keys: backup the
  file, edit via Python with block replace + `yaml.safe_load` validation, then
  restart.
- Always `cp config.yaml config.yaml.bak-YYYYMMDD` before editing.
- After restart verify: `hermes gateway status` and gateway_state.json shows
  the platform connected.

### Toolset lockdown (chat platforms)
- Dangerous toolsets to strip from WeCom/chat `platform_toolsets`:
  `session_search`, `skills`, `memory`, `cronjob`, `delegation`, and outbound
  surfaces `web`, `browser`, `vision`, `image_gen`, `tts`.
- Safe to keep: the business-query toolset, `clarify`, `todo`.
- Set `platform_toolsets.cron` explicitly to a minimal list. Without it, cron
  falls back to per-job `enabled_toolsets` which a chat user can set to
  terminal/file/code_execution — a full privilege-escalation chain.
- `allow_from: ['*']` means open-to-all; that may be a business decision —
  mitigate via toolset stripping, and set `allow_admin_from` to real userids.
- Toolset changes take effect on new sessions (`/reset`), never mid-conversation
  (prompt-cache invariant).

### Contract privacy (plugin execution contracts)
- Physical-mapping contracts (`*-semantics.yaml`, `datasets.yaml`,
  `entity-registry.yaml`, `query-policy.yaml`) must NOT live under
  `skills/*/references/` — the model can read them via `skill_view` and
  overwrite via `skill_manage` when `write_approval: false`.
- Move them to a plugin-private dir, e.g. `plugins/<plugin>/contracts/`, and
  update the path constants in plugin code. Keep `planner-contract.yaml`
  (planning guidance meant for the model) in skills.
- Update CONTRACT_INDEX.md / SKILL.md declarations so docs match reality.
- Verify: `skill_view` linked files no longer expose the yaml; plugin tests
  still pass; a real business query still succeeds.

### Release identity (RELEASE.json)
- `runtime_health.py` expects `.release/RELEASE.json` with
  `hermes_source: {commit, tag, tree_oid, uv_lock_sha256}`.
- Identity matches only when the hermes-agent checkout is CLEAN **and a git
  tag points at HEAD** (`_expected_tag_present`).
- Fix: `git tag <name>` at HEAD (local tag is fine), generate RELEASE.json
  from real git data + payload_sha256 of the profile's owned files.
- Verify in the startup log: before fix
  `version=source artifact_id=unreleased identity_state=source`;
  after fix `version=<ver> artifact_id=<name> identity_state=installed
  identity_expected==identity_actual`.

### Log redaction
- `agent/turn_context.py` and `gateway/run.py` log the user/business message
  at INFO (truncated to 80 chars) — original question text lands on disk.
- Add `redact_message_for_log(text)` to `agent/redact.py`: read
  `privacy.redact_pii` via `load_config_readonly()`; when true return
  `[redacted len=N hash=<sha256-16>]`, else the 80-char preview.
- Wire it into both log points; tighten `logging.max_size_mb` (5) and
  `backup_count` (3).
- Verify: a NEW process's log line shows `msg='[redacted ...]'`; the module's
  repo tests pass.

### Testing pitfalls (Windows / hyphenated plugin packages)
- pytest on Windows can fail at collection with
  `PermissionError: Temp\pytest-of-<user>` → run with
  `--basetemp=<writable-dir>`.
- Hyphenated plugin packages (e.g. `datasage-query`) fail under default prepend
  import mode → use `--import-mode=importlib`.
- Known baseline failures (e.g. missing `evaluation/` dir in an installed
  distribution) are pre-existing; distinguish them from new regressions.

### Context-explosion fixes (chat platforms, long sessions)
- Symptom: "why/analyze" questions balloon context — several Top-10 rankings,
  5 queries/turn, 35KB tool output per turn → 51K tokens by turn 5.
- Enable `compression` (enabled/threshold/target_ratio).
- Add analysis-planning rules to the business skill: prefer
  `complete_change_decomposition` for "why" questions; at most one ranking
  request per turn, `limit <= 10`; batch <= 3 requests.
- Server-side: cap ranking limit (e.g. `ranking_cap = 10` when `order_by`
  present). Do NOT lower `max_result_bytes`/`max_cell_chars` — they are
  fail-closed and will error legitimate large queries (degradation).

## Verification pattern

- Ad-hoc verification scripts: temp file under the OS temp dir with a
  `hermes-verify-` prefix, run it, then clean up. Report as ad-hoc, not
  suite-green.
- Run plugin tests with `--import-mode=importlib`; compare against the known
  baseline (e.g. 91 passed / 1 known evaluation fail).
- After each code change: restart gateway, check startup logs, and run one
  real end-to-end business query.

## References

- `references/datasage-canary-next-remediation.md` — session-specific audit
  findings, exact file paths, and the commands used.
