# Hermes security-relevant surfaces (verified Aug 2026, datasage-canary-next profile)

Condensed knowledge bank from a P0 cross-verification audit. Paths are relative to the
hermes-agent repo root and the active profile dir. Re-verify line numbers on each audit —
excerpts are the durable anchors.

## Toolset exposure chain
- `config.yaml platform_toolsets.<platform>` is the gate for messaging platforms.
  Observed WeCom list: web, browser, vision, image_gen, tts, skills, todo, memory,
  session_search, clarify, delegation, cronjob, datasage-query.
- `_get_platform_tools(config, platform_key)` in `hermes_cli/tools_config.py` resolves
  the list. An explicit list of configurable keys wins; a composite (e.g. `hermes-cli`)
  expands to member toolsets. `_DEFAULT_OFF_TOOLSETS` = {homeassistant, spotify, discord,
  discord_admin, video, video_gen, x_search} — terminal/file/code_execution are NOT
  default-off anywhere.
- Toolsets are platform-scoped, NOT sender-scoped: gateway/run.py (~L16296-16297) calls
  `_get_platform_tools(user_config, platform_key)`. There is no per-user toolset config.
- `platforms.<platform>.allow_from: ['*']` + `dm_policy: allowlist` + `allow_admin_from: []`
  = effectively open to all users (DM and group). Tightening = real userid whitelist.

## Tool availability gates (check_fn)
- Registry: `tools/registry.py`; tools register via `registry.register(name, toolset,
  schema, handler, check_fn, emoji)` (usually at file bottom). Tools whose check_fn returns
  False are excluded from the schema (registry ~L523-542, cached ~30s).
- cronjob check_fn (`tools/cronjob_tools.py` check_cronjob_requirements): truthy if
  HERMES_INTERACTIVE / HERMES_GATEWAY_SESSION / HERMES_EXEC_ASK is truthy.
  **gateway/run.py L2146 sets `os.environ["HERMES_EXEC_ASK"] = "1"`** → cronjob is always
  available inside the gateway (tests only set HERMES_GATEWAY_SESSION).
- session_search check_fn: only requires the state DB dir to exist.

## session_search cross-profile surface (`tools/session_search_tool.py`)
- Signature: query / role_filter / limit / session_id / around_message_id / window / sort /
  profile — NO user_id/chat_id caller identity.
- `profile=` swaps the DB to another profile's state.db read-only (~L876-887);
  `_resolve_profile_db` only validates the name exists (no authorization check).
- Read-shape miss falls back to `_locate_session_db`, scanning ALL profiles (~L905-917).
- `session_search` is in `_HERMES_CORE_TOOLS` → present in hermes-cli / hermes-cron /
  hermes-wecom defaults.

## Cron toolset resolution (`cron/scheduler.py`)
- `_resolve_cron_enabled_toolsets` precedence: per-job `enabled_toolsets` (set via the
  cronjob tool, NO server-side whitelist in cronjob_tools.py create/update) → platform
  `cron` config → None (full default set).
- `_resolve_cron_disabled_toolsets` protects ONLY {cronjob, messaging, clarify} plus
  `agent.disabled_toolsets` — terminal/file/code_execution/process/delegation NOT protected.
- AIAgent is built with `enabled_toolsets=job list` (~L3497), so a model-supplied list wins.
- Cron default toolset `hermes-cron` = `_HERMES_CORE_TOOLS` (toolsets.py ~L440-446):
  terminal, process, read_file, write_file, patch, execute_code, delegate_task,
  skill_manage, session_search, cronjob, clarify, memory, ...
- cronjob create hardening that EXISTS: `script` restricted to relative paths under
  HERMES_HOME/scripts/ (`_validate_cron_script_path`, no absolute/`~`); prompt scanned;
  model/provider/base_url NOT accepted from agent args (user-owned pins only).
- Escalation chain (verified P0): wecom user → model calls cronjob create with
  enabled_toolsets=["terminal","file","code_execution"] → non-interactive auto-approve job
  runs with host terminal/file/code execution.

## Skills read/write surface (`tools/skills_tool.py`, `tools/skill_manager_tool.py`)
- `skill_view(name, file_path)` reads ANY file under skill_dir — only `..` traversal is
  blocked, no extension filter; references/ files are auto-listed as linked_files.
- `skill_manage` (toolset="skills", NO check_fn): write_file/patch/remove_file restricted
  to ALLOWED_SUBDIRS = {references, templates, scripts, assets}; with
  `skills.write_approval: false` there is no approval gate; `guard_agent_created: false`
  allows agent-created skills.
- Consequence: execution contracts living in `skills/<x>/references/*.yaml` are both
  READABLE (skill_view) and OVERWRITABLE (skill_manage) by the model — "plugin-private"
  doc claims (CONTRACT_INDEX.md) are not enforced.

## DataSage DB security (`plugins/datasage-query/db_security.py`)
- `canary_existing_account_accepted` returns True only if ALL of:
  canary_accept_existing_account=true AND deployment_role=="canary" (profile name in
  `_CANARY_PROFILE_NAMES` = {datasage-canary-next, datasage-canary-v012-dev1}) AND no
  `.production-release` marker AND production_mode=false.
- Canary branch of `verify_mysql_read_only_grants`: parses SHOW GRANTS, collects
  observed_privileges/observed_scopes, returns grants_verified=True WITHOUT comparing to
  `_READ_ONLY_PRIVILEGES` = {SELECT, SHOW VIEW, USAGE} — does NOT reject ALL PRIVILEGES.
  Strict branch (non-canary) rejects non-read-only privileges, WITH GRANT OPTION, and
  wildcard scopes.
- TLS: `tls_required = production_mode OR require_tls`. With no DATA_QUERY_MYSQL_SSL_CA:
  tls_required → error; elif role in {source, canary} → `ssl_disabled` (plaintext allowed);
  else error. `verify_mysql_tls` plaintext branch returns insecure_transport_allowed=True.
- Observed runtime evidence (agent.log): transport_mode=plaintext tls_verified=False,
  grants_verified=True under `user_accepted_canary_existing_account` with
  observed_privileges = ALL PRIVILEGES, LOCK TABLES, PROCESS, REPLICATION CLIENT,
  REPLICATION SLAVE, SELECT, SHOW VIEW, XA_RECOVER_ADMIN → query succeeded (row_count=1).
  Fix: dedicated read-only account + require_tls + canary_accept_existing_account:false +
  explicit mysql_allowed_grant_scopes.

## Profiles & state
- Each profile has its own `state.db` (tables: sessions, messages + FTS trigram). Read-only
  counts via `sqlite3.connect("file:<path>?mode=ro", uri=True)`.
- Profiles on this machine: datasage, datasage-canary-next, datasage-canary-next-legacy,
  datasage-canary-next-sourcecopy, datasage-canary-v012-dev1, datasagecore.
- Cross-profile exposure volume: datasagecore 932 sessions / 23,851 messages (412MB),
  datasage 181 / 19,826 (254MB) — all reachable via session_search(profile=...).
