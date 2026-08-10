---
name: security-audit-verification
description: Cross-verify security audit P0/P1 findings in Hermes.
---

# Security Audit Verification (安全审查交叉复核)

## When to use
- User hands a security review/audit report with P0/P1 findings and asks for independent cross-verification (交叉复核) plus remediation plans.
- Auditing a Hermes agent installation's security posture: platform toolset exposure, privilege-escalation chains, contract/data protection, DB transport security.
- Output is in Chinese when the review context is Chinese (报告/整改方案).

## Hard rules
- READ-ONLY. Never modify any file. For sqlite reads always use `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.
- Every verdict needs evidence: file path + line number + short excerpt. When code shifts, the excerpt is the durable anchor.
- Never accept the report's claims at face value — verify each against the actual artifact (config, source, logs, DB counts).

## Workflow
1. Read the report; extract each finding's claim and its cited evidence locations (paths + line numbers).
2. Verify each finding in three layers:
   - **Config**: `profiles/<p>/config.yaml` — `platform_toolsets`, `platforms.*` (allow_from / dm_policy / group policy), `plugins.*.settings`, `skills`/`memory` write_approval, `agent.disabled_toolsets`.
   - **Code**: tool source in the agent repo; always check the REGISTRY registration (grep `registry.register(` near file bottom) — `check_fn` decides runtime availability, and the schema/handler show which params exist.
   - **Runtime evidence**: `logs/agent.log` lines, state DB contents.
3. For "tool X is available on platform Y" claims, verify the full chain:
   `platform_toolsets.<platform>` includes the toolset → tool registered in registry → check_fn truthy under the ACTUAL runtime env. Find where the gate env var is SET (grep runtime entry points like gateway/run.py), not just where it's referenced — e.g. `os.environ["HERMES_EXEC_ASK"]="1"` at gateway/run.py:2146 is what makes cronjob's check_fn pass in the gateway.
4. For data-exposure claims: enumerate ALL profiles' `state.db` read-only (sessions/messages counts), then inspect the tool signature for caller-identity params (user_id/chat_id — absence = no per-user filter) and cross-profile params (`profile=` = cross-profile read).
5. For DB-security claims: read the plugin's transport + grant-verification code; check whether an "exception" branch (canary/dev mode) skips the strict checks; confirm with log lines showing the actual transport_mode / observed_privileges used.
6. Produce per finding: 【验证结论：属实/不属实/部分属实 + 证据（路径+行号+摘录）】【整改方案：具体步骤、涉及文件、优先级 P0/P1】，then an overall remediation-order section (which P0 first and why — data-plane before execution-plane before integrity-plane before confidentiality-plane is a defensible default).

## Pitfalls
- search_files can return `IO error (os error 2/3)` on some Windows/MSYS paths even though read_file works on the same path — fall back to `terminal` grep/rg.
- The hardline command parser BLOCKS commands that use command substitution inside sed/awk args (e.g. `sed -n "$(grep -n ...)"`). Use `grep -A N -B N` context flags or read_file instead.
- A mechanism existing ≠ reachable. E.g. cronjob tool only matters if its check_fn passes under the gateway env; session_search only leaks data if the platform toolset includes it AND the tool lacks caller filtering. State the full reachability chain in the verdict.
- Watch for "documented policy vs enforced policy" gaps — e.g. an index doc claiming "the model never receives physical mappings" while skill_view can read the .yaml contracts and skill_manage can overwrite them.
- Mark inferred attributes explicitly (e.g. "production DB" inferred from account privileges + real business queries, not directly provable from logs) — that is the difference between 属实 and 部分属实.

## Support files
- references/hermes-security-surface.md — Hermes-specific security-relevant surfaces verified during audits (toolset resolution precedence, check_fn env gates, cron toolset protections, session_search cross-profile surface, skills read/write surface, DataSage DB canary exceptions).
