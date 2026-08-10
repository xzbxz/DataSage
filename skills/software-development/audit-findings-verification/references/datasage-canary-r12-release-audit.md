# datasage-canary-next Release/Distribution Audit Anchors (2026-08)

Read-only cross-verification of a release/dependency/log/test review report for profile `datasage-canary-next` (distribution 0.12.0-dev1). Complements references/datasage-plugin-audit-map.md (business-semantics audit of the same profile).

## Key paths
- Profile: `C:\Users\10192\AppData\Local\hermes\profiles\datasage-canary-next\`
- Staging: `C:\Users\10192\Documents\Codex\2026-08-04\c-users-10192-appdata-local-hermes\work\release-staging\datasage-canary-next\`
- Source work dirs with `evaluation/`: `work\datasage-kernel-convergence\`, `work\datasage-kernel-hardening\`
- Release record (primary evidence for packaging decisions): `work\..\outputs\datasage-r12-canary-release-record-20260806.json` — documents `evaluation_directory_present: false`, `runtime_only_exclusion: skills/.usage.json`, and deployment_note explaining manifest-only staging.

## Verified anchors
- **Identity chain broken but reported ready**: `distribution.yaml:29-43` `distribution_owned` lists `.release` (L41); no `.release/RELEASE.json` anywhere (profile + staging). `runtime_health.py:53-56` production gate = `settings.get_bool("production_mode", False)` OR `root/.production-release` file; `:184-201` missing release + non-production → `ready=True, state="source"`; `config.yaml:49` `production_mode: false`. `_IDENTITY_FIELDS = (commit, tag, tree_oid, uv_lock_sha256)` (runtime_health.py:32); fingerprint = first 16 hex of sha256 of sorted JSON. `db_security.py:28-34` `_profile_deployment_role`: `evaluation/` present → "source"; name in `_CANARY_PROFILE_NAMES` → "canary"; else "production".
- **Staging pollution**: 104 `.pyc` / 25 `__pycache__` dirs (plugins/datasage-query{,/tests,/vendor/pymysql{,/constants}} + 15+ skills/*/scripts); runtime state: `skills/.curator_state`, `skills/.usage.json`, `skills/.usage.json.lock`, `skills/.hub/{audit.log, lock.json, taps.json, index-cache/, quarantine/}`. No `logs/ sessions/ cron/ memories/ state.db* .env auth.json` in staging. Release record's exclusion list covered only `.usage.json`.
- **Evaluation corpus excluded → fixed test failure**: `test_evidence_bundle.py:494` `test_playbook_and_evaluation_vocabularies_match_plugin` reads `root/evaluation/expert-core/cases.yaml` → FileNotFoundError. Verified run: `1 failed, 91 passed, 50 subtests passed` (92 collected = 5+19+50+18 defs). `db_security.py:30` uses `evaluation/` presence as source-mode marker — restoring it into distributions changes role classification (fix must account for this coupling).
- **Test layout**: 4 unittest-style files; each aliases the hyphenated plugin dir via synthetic package (`datasage_query_test_package` etc., sys.modules injection, e.g. test_evidence_bundle.py:16-27). Plain pytest 9.0.2 (prepend mode) collects fine — tests/ has no `__init__.py`; `python -m unittest discover -s plugins/datasage-query/tests` fails with "Start directory is not importable". No pyproject.toml/pytest.ini/setup.cfg/tox.ini anywhere; plugin has only `requirements.txt` (`PyMySQL==1.2.0 --hash=sha256:62169ce6...`).
- **Vendored PyMySQL 1.2.0** (`plugins/datasage-query/vendor/pymysql/` + `pymysql-1.2.0.dist-info/`): official wheel sha256 matches requirements.txt; 22 wheel files byte-identical; RECORD 24/24 hashes match; RECORD delta = uv INSTALLER/REQUESTED lines only. Runtime enforcement at `tools.py:2280-2330`.
- **Logging**: `config.yaml:75-78` `logging: level INFO, max_size_mb 20, backup_count 12` (size-based rotation only). `logs/agent.log:57` logs full business question text at INFO (`msg='...'` in turn_context). Outbound: `config.yaml:3-6` deepseek `base_url https://api.deepseek.com/v1` (client creation visible in agent.log); wecom `platform_toolsets` include web/browser/vision/image_gen/tts/session_search (`config.yaml:18-31`); dm/group `allow_from: '*'` (:85-95); `terminal.backend: local`, `home_mode: auto` (:32-36).

## Commands that worked
- Read-only suite run: `cd <profile root> && PYTHONDONTWRITEBYTECODE=1 python -m pytest plugins/datasage-query/tests -q -p no:cacheprovider --import-mode=importlib`
- Counts: `find <dir> -name '*.pyc' | wc -l`; `find <dir> -type d -name '__pycache__' | wc -l`
- PyPI JSON: `https://pypi.org/pypi/PyMySQL/1.2.0/json` → wheel url + digests.sha256
