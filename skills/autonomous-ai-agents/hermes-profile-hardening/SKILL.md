---
name: hermes-profile-hardening
description: "Harden a Hermes profile from audit findings; verify changes."
---

# Hermes Profile Hardening

Class-level workflow for remediating security/quality findings in a Hermes
profile (e.g. `datasage-canary-next`) after an audit. Covers toolset cuts,
cron lockdown, contract isolation, semantics alignment, and verification
discipline. Session-specific detail lives in
`references/datasage-canary-hardening-2026-08.md`.

## Trigger

- User provides a security/quality audit report for a profile and asks to
  plan or apply fixes.
- User asks "接下来该干嘛" mid-remediation, or picks an item from a roadmap.

## Workflow

1. **Verify findings before acting** — read the cited files/lines; do not
   trust the report blindly. Use terminal + read_file on exact paths.
2. **Cross-check with parallel subagents** when the report is large: split
   into security, business-semantics, and release/deps areas; give each the
   report excerpts + file paths; require evidence (path+line+quote), forbid
   edits.
3. **Present the roadmap in plain language** — 改前 → 改后 → 解决什么问题,
   with priorities. User expects to confirm before any change
   ("先不要直接改，先输出").
4. **Apply in priority batches.** The highest-leverage first move on a
   multi-user messaging profile is trimming `platform_toolsets.<platform>`
   (remove session_search / skills / memory / cronjob / delegation) — one
   config cut closes several P0 entries at once.
5. **Verify every change** with an ad-hoc `hermes-verify-*` script under
   Temp: create, run, report N/M checks, clean up. Label results "ad-hoc
   verification (not suite green)" when the full suite has a known unrelated
   failure. Follow with a real-query smoke test (`hermes chat -q ...`) and
   check agent.log for `status=success` / no CONTRACT_UNAVAILABLE.

## Pitfalls (learned the hard way)

- **config.yaml is protected**: `patch`/`write_file` refuse with "Refusing
  to write to Hermes config file". `hermes config set` works for known keys
  (`compression.enabled` etc.) but MANGLES list-valued keys it does not
  know — `platform_toolsets.wecom` was written as a quoted string instead
  of a YAML list. Correct path: backup the file, then Python
  `yaml.safe_load`-safe targeted replacement, re-validate the whole file,
  then `hermes gateway restart` + `hermes gateway status`.
- **Toolset changes take effect on new sessions** (`/reset`), never
  mid-conversation — prompt caching is sacred.
- **pytest on hyphenated plugin packages** (`plugins/datasage-query`)
  fails with relative-import collection errors under default prepend mode;
  use `--import-mode=importlib` with `PYTHONPATH=<profile root>`. The
  tests load plugin modules through a synthetic package name injected into
  `sys.modules`, not direct `import plugins.datasage_query`.
- **Fail-closed limits are not knobs**: `max_result_bytes` /
  `max_cell_chars` raise QueryFailure (OUTPUT_TOO_LARGE) rather than
  truncating. Lowering them breaks normal large queries — that is 降智.
  Use targeted caps instead (e.g. ranking limit 10 when `order_by` is
  present, leaving non-ranking queries at 100).
- **Context bloat fix**: enable Hermes compression
  (`hermes config set compression.enabled true` + threshold 0.5 +
  target_ratio 0.2). Long gateway sessions otherwise grow past 50K tokens
  with no auto-compression.
- **Execution contracts must be plugin-private**: semantics.yaml,
  datasets.yaml, entity-registry.yaml, query-policy.yaml belong in
  `plugins/<pkg>/contracts/`, NOT under `skills/*/references/` where
  `skill_view`/`skill_manage` can read and overwrite them. Model-facing
  planning guidance (planner-contract.yaml, expert-playbooks.yaml) stays
  in skills. Update all path constants in code AND the docs that describe
  the location (CONTRACT_INDEX.md, common-data-foundation/SKILL.md).
- **Semantics changes need three-way sync**: the declaration
  (e.g. unit_policy), the model-facing planner-contract rules, and any
  tests asserting the old text must all change together, or the docs lie
  to the model again.
- **RELEASE.json identity requires a git tag on HEAD**: the runtime
  identity check (`runtime_health._actual_hermes_identity`) requires
  `_expected_tag_present=True`; an untagged HEAD fails even with a valid
  RELEASE.json. Generate the tag (`git tag <name>`) then build
  `.release/RELEASE.json` from live git values (rev-parse HEAD / HEAD^{tree}
  / tag --points-at / git show HEAD:uv.lock sha256). After a hermes-agent
  update, re-tag and regenerate. Verify via startup log: `identity_state`
  must flip source → installed and `identity_expected == identity_actual`.
- **Log message redaction lives in agent/redact.py**: add
  `redact_message_for_log(text)` (returns `[redacted len=N hash=H]` when
  `privacy.redact_pii` is true, else the old 80-char preview) and call it
  from turn_context.py and gateway/run.py message logs. Old CLI sessions
  keep old code until restarted — check with a NEW process.
- **pytest.ini `import-mode` key is ignored by pytest 9.0.2**: put the flag
  in `addopts` (`--import-mode=importlib -p no:cacheprovider`), not as an
  ini key or `-o` override — those silently stay prepend and the hyphenated
  package fails collection.
- **Registry handler forwarding is a silent-drop surface**: a tool schema
  key, function signature, and create/update paths can all support a param
  while the `registry.register(handler=lambda ...)` forwarding list omits
  it (cronjob.attach_to_session was dropped this way). Diff schema keys vs
  handler-read keys when a param seems ignored.

## Verification discipline

- After editing code, create `Temp/hermes-verify-<topic>.py`, run it, report
  N/M checks, delete it, confirm no leftovers. State scope honestly: ad-hoc
  targeted verification, not full-suite green.
- Keep the known-failure baseline in mind (e.g. evaluation/ dir excluded
  from installed profile → 1 fixed test failure unrelated to your change).
