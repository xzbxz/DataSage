# DataSage repository guide

This repository contains the Hermes profile distribution source plus its DSRT control-plane source.

## Layout

- Repository root — the distributable DataSage profile, including the query plugin, semantic contracts, skills, tests, and release manifest.
- `dsrt-control/` — DSRT release-unit construction, verification, offline health, Windows runtime tests, control CLI, runtime-view bootstrap, and runtime ownership contract.

Hermes requires `distribution.yaml` at the repository root, so the profile must remain at this level. `dsrt-control/` is not listed in `distribution_owned` and is not copied into an installed profile.

## Security boundary

Runtime secrets and state do not belong in Git. Never commit `.env`, authentication files, database credentials, sessions, logs, state databases, trust roots, runtime candidates, or rollback backups. `config.yaml` refers to secrets through environment variables.

Model-facing tools accept governed metric requests only. They must not accept raw SQL, physical table names, joins, formulas, or unrestricted filters.

## Current status

The checked-in profile snapshot is `0.12.0-dev1`; the control history is preserved through `datasage-control-v0.12.0-dev10`.

This snapshot is not yet approved for business release. The remaining P0 is to make the DataSage startup identity verifier validate the DSRT attestation chain (`CURRENT`, runtime-view marker, canonical unit manifest, trust root, Hermes identity, and profile payload) without depending on a `.git` directory inside the materialized runtime checkout. The verifier must remain fail-closed.

Until that release work is complete, do not treat a plain `hermes profile install` as release approval: Hermes stamps installation provenance into `distribution.yaml`, while the current DataSage payload manifest hashes the authored file byte-for-byte. The governed release pipeline must reconcile those fields and regenerate or canonically validate the installed payload.

Database configuration and privileges were intentionally not changed during this import.

## Offline validation

Run the profile tests from `plugins/datasage-query`:

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
```

Run the DSRT control tests from `dsrt-control`:

```powershell
python -B -m unittest -v evaluation.test_windows_long_path
```

## Release order

1. Static safety and secret scan.
2. Offline contract and payload verification.
3. DSRT stage, verify, activate, and health.
4. Clean runtime-view bootstrap.
5. WeCom no-database smoke with an answer oracle.
6. Same-session, business-assertion E2E and rollback rehearsal.

## License

Proprietary.
