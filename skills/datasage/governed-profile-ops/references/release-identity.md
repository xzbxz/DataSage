# RELEASE.json identity — fields, failure codes, regeneration

## What it is

`<profile_root>/.release/RELEASE.json` binds an installed profile to the exact
hermes-agent git checkout it was built against. `runtime_health.runtime_identity_status`
reads it and compares against the live checkout. When present, ALL
`datasage_query` calls are gated on identity matching (fail-closed): mismatch or
dirty checkout → `ready=False`, query blocked with `HERMES_IDENTITY_UNVERIFIED`
visible to the user.

## Fields (under `hermes_source`)

| field | source | regex check |
|---|---|---|
| `commit` | `git rev-parse HEAD` | `[0-9a-fA-F]{40,64}` |
| `tag` | `git tag --points-at HEAD` | no control chars; MUST point at HEAD |
| `tree_oid` | `git rev-parse HEAD^{tree}` | `[0-9a-fA-F]{40,64}` |
| `uv_lock_sha256` | `git show HEAD:uv.lock` → sha256 | `[0-9a-fA-F]{64}` |

Top-level fields used in startup log: `distribution_version`, `artifact_id`,
`payload_sha256` (hash of `distribution_owned` profile files).

Match requires: checkout CLEAN (`git status --porcelain` empty), exact tag
present at HEAD, all four fields equal, and no reparse markers.

## Failure codes

- `HERMES_IDENTITY_RELEASE_MISSING` — no `.release/RELEASE.json`; only OK when
  not production and treated as `state=source` (not launchable).
- `HERMES_IDENTITY_RELEASE_INVALID` — file unreadable or fields malformed.
- `HERMES_IDENTITY_CHECKOUT_DIRTY` — RELEASE.json exists but hermes-agent
  working tree has uncommitted changes. Most common during active dev: any edit
  to hermes-agent files (e.g. log redaction, cron handler) after RELEASE.json
  was generated blocks all queries.
- `HERMES_IDENTITY_EXACT_TAG_MISMATCH` / `HERMES_IDENTITY_MISMATCH` — tag/commit
  drift.
- `HERMES_IDENTITY_CHECKOUT_NOT_FOUND` / `REPARSE_REJECTED` — checkout discovery
  failed or symlink/reparse detected.

## Regeneration recipe (after ANY hermes-agent or distribution_owned change)

1. Commit or stash hermes-agent working-tree changes → `git status --porcelain` empty.
2. Delete old tag if HEAD moved, retag current HEAD:
   `git tag -d <tag> && git tag <tag>` (tag name e.g. `datasage-hermes-v0.19.0-dev40`).
3. Run `scripts/gen-release-json.py` (in this skill) or the inline Python
   recipe below.
4. `hermes gateway restart`, then confirm:
   `grep datasage_startup_health logs/agent.log | tail -1` → `ready=True`,
   `identity_state=installed`, `identity_expected == identity_actual`.
5. Run one real query (CLI) to confirm the gate opens.

## Inline generation recipe (equivalent to the script)

```python
import hashlib, json, os, subprocess
HERMES = r"C:/.../hermes-agent"; PROFILE = r"C:/.../profiles/<name>"
def git(*a): return subprocess.run(["git","-C",HERMES,*a],capture_output=True,text=True,check=True).stdout.strip()
commit = git("rev-parse","HEAD"); tree = git("rev-parse","HEAD^{tree}")
tag = git("tag","--points-at","HEAD").splitlines()[0]
uv = subprocess.run(["git","-C",HERMES,"show",f"{commit}:uv.lock"],capture_output=True,check=True).stdout
h = hashlib.sha256()
for name in sorted(["distribution.yaml","profile.yaml","config.yaml","AGENTS.md","ARCHITECTURE.md","README.md","SOUL.md","CHANGELOG.md"]):
    p = os.path.join(PROFILE,name)
    if os.path.isfile(p): h.update(name.encode()); h.update(b"\0"); h.update(open(p,"rb").read()); h.update(b"\0")
release = {"release_version":"0.12.0-dev1","distribution_version":"0.12.0-dev1",
           "artifact_id":os.path.basename(PROFILE),"payload_sha256":h.hexdigest(),
           "hermes_source":{"commit":commit,"tag":tag,"tree_oid":tree,
                            "uv_lock_sha256":hashlib.sha256(uv).hexdigest()},
           "created_at":"2026-08-07T00:00:00Z"}
os.makedirs(os.path.join(PROFILE,".release"),exist_ok=True)
json.dump(release,open(os.path.join(PROFILE,".release","RELEASE.json"),"w",encoding="utf-8"),ensure_ascii=False,indent=2)
```

## Verification (ad-hoc)

Check: RELEASE exists+parses; commit/tree/tag/uv_lock all match live git;
latest `datasage_startup_health` line shows `ready=True`; latest
`datasage_query {` line shows `"status":"success"` with no `readiness_blocked`.
