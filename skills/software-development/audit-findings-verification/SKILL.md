---
name: audit-findings-verification
description: "Verify audit/review findings read-only."
---

# Audit Findings Verification (独立交叉复核)

## When to use
- User asks to independently cross-verify (独立交叉复核 / 独立核查) the findings in an audit/review report — e.g. business-semantics & expert-logic review findings, security findings, contract-compliance checks.
- Output language: follow the user (this user works in Chinese → 输出中文).
- READ-ONLY by default; when the user says 严禁修改任何文件, never write a file, period.

## Workflow
1. **Inventory the findings**: list each claim with its asserted evidence paths/lines.
2. **Locate primary sources FIRST** — never trust paths/lines from the report. Verify they exist (`search_files` target=files, or `terminal ls`) before searching inside them. Stale paths are common: in one session 2 of 3 asserted paths were wrong (files existed but were not where the report said).
3. **Per finding, collect exact evidence**:
   - file:line + a short verbatim excerpt (code / contract line / log line).
   - For behavior claims trace the FULL chain: schema → handler → persistence → consumer. A "parameter is silently dropped" claim requires checking the registered handler lambda, not just the function signature.
   - Label the owning layer: plugin code vs core product code (e.g. hermes-agent) — each fix belongs to its layer.
4. **Verdict taxonomy**:
   - 属实 (confirmed) — evidence matches.
   - 部分属实 (partially confirmed) — core claim holds but nuance differs; state exactly what differs.
   - 不属实 — evidence contradicts; quote the counter-evidence.
   - Honest partial beats inflated confirm.
5. **Remediation**: per finding, priority P0/P1/P2 + owning layer + concrete change + regression test. Tie tests to the repo's engineering contract (e.g. AGENTS.md: "Add a failed natural-language example to evaluation before changing behavior").
6. **Summary matrix** at the end: finding | verdict | key evidence location | priority.

## Techniques (verified in release/distribution audits)
- **Re-run test suites read-only**: `PYTHONDONTWRITEBYTECODE=1 python -m pytest <tests> -q -p no:cacheprovider [--import-mode=importlib]` — writes nothing (no .pyc, no .pytest_cache). Report collected / passed / failed counts + the exact failing test name and error. This settles "N 项测试固定失败" claims empirically.
- **Dependency integrity by recomputation**: never trust "hash verified" from the report — recompute. Official PyPI JSON API (`https://pypi.org/pypi/<pkg>/<version>/json`) gives authoritative wheel URL + sha256 (guessing files.pythonhosted.org URLs yields 0-byte downloads). Full recipe incl. the RECORD urlsafe-base64 gotcha: references/wheel-integrity-verification.md.
- **Manifest vs shipped artifact**: a distribution manifest declaring a path (`distribution_owned: [..., .release]`) while the artifact never ships it = identity-chain finding. Verify by `find` for the artifact (RELEASE.json) across profile + staging. Release records / deployment notes (e.g. `outputs/*-release-record-*.json`) are primary evidence for packaging decisions (what was excluded and why) — read them early.

## Pitfalls
- **Gate-vs-config**: a "production gate exists" claim is only half the story — check the config that switches it. `_production_identity_required` may exist in code while `production_mode: false` disables it, so a broken identity chain still reports `ready=True` (non-production branch). Both code and config state are evidence.
- **Tooling claims must be tested in the actual layout**: "plain pytest fails to collect because of hyphenated package dir" — empirically false when tests/ has no `__init__.py` (prepend mode imports by basename; hyphen never enters the module name). But `python -m unittest discover` DID fail on the same tree. Test the claim; don't inherit it.
- **Hash-verification false alarms**: wheel RECORD hashes are urlsafe-base64 of sha256, not hex — a naive hex comparison reports fake mismatches (one session: 18/24 "mismatches" that were actually all-matching). See references/wheel-integrity-verification.md.
- **Installer rewrites RECORD**: uv adds `INSTALLER` (content `uv`) and `REQUESTED` (empty) lines to RECORD at install time; pip adds `pip`. Extra correctly-hashed lines are expected, not tampering. Official wheels do NOT contain INSTALLER.
- **Hardline-blocked terminal commands**: shell for-loops with escaped alternation (e.g. `for f in ...; grep -c '^def test_\|...'`) can be hardline-blocked by the command parser. Restructure (search_files count mode, or plain grep with multiple file args) — don't retry variants of the blocked shape.
- **Distinguish "semantics say X but implementation doesn't" conflicts from real bugs**: both need fixing, but one is a docs/contract fix and the other a code fix.
- **search_files on Windows (git-bash) can fail** with `系统找不到指定的路径` (os error 3/2) on long AppData paths or single-file targets, even when `read_file`/`terminal` work fine on the same path. Diagnose once (`terminal ls` to confirm the path), then fall back to `terminal` grep -n / sed -n. Do NOT retry the same failing call — the runtime warns on loops; switch tool instead of repeating.
- Scope greps per concern (`grep -n "completion"` then `sed -n` around hits) rather than one giant search that drowns the signal.
- **"Silent drop" claims**: schema has the param + function signature has the param + persistence handles it + consumer reads it does NOT mean the registered `registry.register(handler=lambda ...)` forwards it. Check the lambda's argument list line-by-line (cronjob `attach_to_session` was omitted there → silently ignored for model calls).
- **"Orphan authority" claims** (contract field nobody consumes): grep the whole package for consumers; zero hits outside the building module confirms it. Also check catalog/projection views — docstrings may explicitly say guidance is omitted.
- **Contract promise vs compiler enforcement**: catalog may expose full `allowed_dimensions` while the compiler caps combinations (1–2) and the tool schema caps at 5. The report's claim may be 部分属实 because the catalog never promised combinations — only the planner prose did. Verify what each layer actually promises.
- **Log evidence without arguments**: agent.log records tool calls and char sizes but often not parameters. Match char-size against file sizes (`wc -m`) to infer which skill/file was loaded, and say the inference is size-based.

## Verification recap format (Chinese)
```
## 发现N（标题）：**结论**
**证据**：file:line 摘录…
**整改方案**：P0/1…（层级 + 改动 + 回归用例）
```

## Support files
- references/datasage-plugin-audit-map.md — datasage-query plugin + hermes-agent cronjob/system_prompt anchor map with verified findings from the 5-finding cross-verification (useful starting point for future audits of this codebase).
- references/wheel-integrity-verification.md — vendored-dependency vs official PyPI wheel verification recipe: PyPI JSON API, RECORD urlsafe-base64 gotcha, uv/pip RECORD deltas, runtime enforcement check.
- references/datasage-canary-r12-release-audit.md — release/distribution audit anchors for profile datasage-canary-next: identity gates (.release/RELEASE.json), staging pollution inventory, evaluation-corpus exclusion, test layout (92 tests, 1 fixed failure), logging + outbound surfaces, vendored PyMySQL facts.
