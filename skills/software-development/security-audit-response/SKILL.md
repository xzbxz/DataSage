---
name: security-audit-response
description: Verify audit report claims and plan remediation.
---

# Security Audit Response

Handle a security/governance audit report end-to-end: verify the claims,
cross-review with subagents, produce a remediation plan, and explain it to
whoever needs to act on it.

## When to use

- User pastes or references an audit/review document with structured findings
  (P0/P1/P2, severity, confidence, file:line evidence) about a system,
  profile, or plugin.
- User asks to "自检", "对照审查文档核实", "看看怎么整改/优化", or "explain
  what to change" — often with an explicit "先不要直接改 / 先输出" constraint.
- This is NOT for a normal code review of a PR or a one-off question about a
  single metric; those use other skills.

## Workflow

1. **Confirm the mandate.** Read-only analysis vs. actual changes. If the user
   says "先不要直接改, 先输出", do NOT modify anything — verify and plan only.
2. **Verify the baseline evidence yourself first.** Do not delegate the whole
   thing blind. Spot-check the report's key claims:
   - Resolve the cited paths (Windows/MSYS: search_files can return 0 for
     existing files; fall back to terminal `ls` with the exact path).
   - Read the cited files at the cited lines (read_file with offset/limit).
   - Confirm the specific config values, function signatures, log lines.
   - Check for corroborating evidence the report may have missed (e.g.
     sibling profiles, session DB sizes, vendored dependency trees).
3. **Dispatch parallel subagents for independent cross-review.** Split by
   domain, 3 at a time:
   - Security/perimeter (tool exposure, authz, credential/TLS posture)
   - Business semantics/logic (metric definitions vs. implementation,
     catalog vs. compiler capability)
   - Release/deps/logs/tests (identity, staging hygiene, dependency hashes,
     test baselines)
   Each subagent prompt must require: READ-ONLY, output per finding as
   【验证结论：属实/不属实/部分属实 + 证据 (path:line)】【整改方案：
   具体步骤+优先级】, and explicitly report corrections to the original
   report. Subagent reports are self-reports — treat them as claims to
   integrate, not ground truth; verify any surprising conclusion.
4. **Synthesize a prioritized remediation roadmap.** Group into batches:
   data-plane security first (credentials, TLS, fail-closed), then
   execution/supply-chain exposure (toolset cuts, contract protection), then
   semantics/engineering quality, then hygiene. Call out the highest
   ROI/lowest-risk change explicitly (often a config-only toolset cut).
   Note overlaps where one config change closes multiple P0s.
5. **Explain in the stakeholder's language.** Business users want: 改前什么样,
   改后什么样, 在解决什么问题 — with analogies, not jargon. Keep file paths,
   line numbers, and code details in an appendix or second pass. Lead with
   the plain-language problem framing.
6. **When the user approves a specific change, EXECUTE it with the
   `hermes-config-editing` skill** (safe config.yaml edit workflow). Config
   changes here are usually config-only; do not assume the audit mandate
   extends to code changes. Confirm scope per change (e.g. user may say
   "do A and B, skip C for now" or "keep allow_from '*' for all staff" —
   business decisions change the exact edit).

## Pitfalls

- Subagents return self-reports: require path:line evidence in their output
  and spot-verify claims that change the verdict.
- A "review" mandate is not a "fix" mandate. Respect 先不要直接改 literally.
- Do not soften or drop "部分属实/不属实" corrections — the cross-review's
  value is refining the original report, not rubber-stamping it.
- Distinguish verified facts from strong inference (e.g. "production DB"
  labels may be inferred from privilege level + real business queries).
- Independent recomputation (dependency hash checks, file counts, session
  DB counts) adds real confidence — do it where cheap.

## References

- `references/audit-response-example.md` — worked example from the
  datasage-canary-next audit: evidence map, subagent split, roadmap shape,
  and the plain-language 改前/改后 explanation pattern.
