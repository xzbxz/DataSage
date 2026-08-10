---
name: governed-profile-ops
description: Audit/harden a governed Hermes profile before launch.
---

# Governed Profile Ops

Class-level workflow for taking a governed Hermes profile (read-only business-data agent, e.g. DataSage) from security-audit findings to launch-ready. Companion to the `datasage` skill (how to USE the tools) — this is how to OPERATE, SECURE, and RELEASE the profile.

## Triggers

- Security audit findings (P0/P1) need remediation on a profile
- WeCom/gateway surface trimming or toolset policy changes
- Execution contracts need to move out of model-visible skill paths
- Queries blocked with `HERMES_IDENTITY_*` / release identity missing or dirty
- Pre-launch capability review (快/准/稳/活)
- pytest fails on Windows with hyphenated plugin packages

## Hard invariants and pitfalls

1. **Never edit config.yaml via patch/write_file** — Hermes refuses (security-sensitive). Use `hermes config set KEY VAL` for known keys. BUT `hermes config set` stringifies list values for keys not in its table (e.g. `platform_toolsets.wecom` becomes the literal string `'["a","b"]'` and breaks YAML structure). For gateway-only keys (`platform_toolsets.*`, `platforms.*`), edit with Python + `yaml.safe_load` round-trip, replacing only target blocks, then validate the whole file. Always backup first (`config.yaml.bak-<date>`).

2. **WeCom toolset trimming is the highest-leverage P0 fix**: remove `session_search`, `skills`, `memory`, `cronjob`, `delegation` from `platform_toolsets.wecom`; keep `datasage-query` / `clarify` / `todo`. Also explicitly set `platform_toolsets.cron` to a minimal list (e.g. `[datasage-query]`) — otherwise cron jobs fall back to full default toolsets (terminal/file/code) = chat-to-host privilege escalation. `allow_from: ['*']` can stay if the business wants all-employee access; the toolset trim is what bounds the risk.

3. **Execution contracts must NOT live under `skills/`** — `skill_view` can read any file in `references/`, and `skill_manage` can overwrite them when `skills.write_approval=false`. Move `semantics.yaml`, `datasets.yaml`, `entity-registry.yaml`, `query-policy.yaml` into a plugin-private dir (e.g. `plugins/<plugin>/contracts/`), update path constants in code (`contracts.py` / `entities.py` / `tools.py` — paths are resolved relative to profile root via `_trusted_path`), update `CONTRACT_INDEX.md` and the skill's SKILL.md so "model never sees physical mappings" is actually true. `planner-contract.yaml` and `expert-playbooks.yaml` are planning guidance → stay model-visible.

4. **Release identity (RELEASE.json) gates ALL queries (fail-closed)**: fields `commit/tag/tree_oid/uv_lock_sha256` must match the hermes-agent git checkout; checkout must be CLEAN; tag must point at HEAD. ANY code edit after generating RELEASE.json → `HERMES_IDENTITY_CHECKOUT_DIRTY` → `datasage_query` blocked (ready=False, user sees unverified). Changing profile files listed in `distribution_owned` (SOUL.md, config.yaml) changes `payload_sha256` → also regenerate. Full recipe + failure codes: `references/release-identity.md`; reusable generator: `scripts/gen-release-json.py`.

5. **`evaluation/` directory flips deployment role**: `db_security._profile_deployment_role` returns `"source"` when profile root has an `evaluation/` dir, which revokes the canary existing-account exception and rejects the high-privilege account. Do NOT drop `evaluation/` into the installed profile just to green a test — change the test to `skipTest` when the corpus is absent (baseline: 93 passed + 1 skipped is fine).

6. **pytest on Windows + hyphenated plugin package**: default prepend mode fails with "attempted relative import with no known parent package" from the plugin `__init__.py`. `--import-mode=importlib` works as a CLI flag but NOT via the ini `import-mode` key on pytest 9.0.2 — put it in `addopts`. `Temp\pytest-of-<user>` PermissionError (WinError 5) → pass `--basetemp=...`. Run the committed test suite through pytest; ad-hoc scripts must not import plugin internals or call model-facing tool entries outside the authenticated facade.

## User communication preferences (this user)

- Explain changes in plain business language with 改前/改后 (before/after) plus "在解决什么问题" — the user is a business owner, not an engineer. Lead with the business conclusion, then the mechanics.
- NEVER degrade capability while optimizing ("不要给我改降智了"): only cut redundant expansion (duplicate history, huge Top-N rows, overlong cells); keep query correctness and answer quality. Before lowering any limit, check its semantics — e.g. `max_result_bytes` / `max_cell_chars` are FAIL-CLOSED (raise `OUTPUT_TOO_LARGE`), so lowering them breaks legitimate big queries; prefer precise caps (ranking `limit` 10) over global shrinkage.
- Present a change list and get confirmation before executing ("先不要直接改，先输出"); then execute with backup → change → verify → restart gateway → confirm via logs.
- Use numbered remediation items (A/B/C/D/E…) with a priority order the user can approve one at a time.

## Pre-launch review (快准稳活) methodology

Dispatch 3 parallel subagents:

1. **Speed + Stability**: log-driven timing stats (`response ready` time=, `api_calls=`, `datasage_query completed` chars/elapsed), error profile from errors.log, gateway-exit-diag / gateway-stdio, degradation paths (partial-failure behavior, no silent failures).
2. **Accuracy**: contract-vs-implementation consistency (formulas, unit policies, time ranges), real queries checked for self-consistency (e.g. net = gross − return), boundary states (zero/missing/negative/truncated/future periods), evidence boundaries (truncated Top-N = observation only, no causal claims), entity resolution.
3. **Agility**: real WeCom session transcripts — question variety (colloquial/abbreviation/pinyin/ambiguous), multi-turn reference resolution, clarify-vs-assume judgment, complex "why" planning compliance, UX details (capability claims must match exposed tools).

Give each subagent: profile root, plugin path, log paths, a list of completed remediations (so it validates effects, not re-audits), and explicit constraints (read-only, limited real queries, no file modification).

## Standard remediation sequence (audit → launch)

1. WeCom/cron toolset trimming (config-only, ~4 lines, immediate effect after gateway restart)
2. Database read-only account + TLS (DBA work — the user often defers this; leave it as an explicit open item)
3. Contract migration to plugin-private dir
4. RELEASE.json generation/regeneration + retag
5. Log redaction (`redact_message_for_log` pattern in agent/redact.py, applied to turn_context + inbound logs, gated on `privacy.redact_pii`)
6. Engineering fixes (cronjob handler forwarding, port fail-closed, pytest.ini, staging cleanup)
7. Re-verify: test suite, identity ready=True, real query success, gateway restart + log confirmation.

## Support files

- `references/release-identity.md` — RELEASE.json fields, failure codes, regeneration recipe
- `references/pytest-windows-quirks.md` — working pytest.ini, basetemp fix, and the boundary between committed tests and governed runtime verification
- `scripts/gen-release-json.py` — deterministic RELEASE.json generator (run after any code or distribution_owned change)
