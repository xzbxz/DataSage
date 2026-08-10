# Speed/Stability pre-launch review — log forensics recipe

Read-only review of a DataSage/`datasage-canary-next` profile on the 快 Speed / 稳 Stability
dimensions. Works entirely from `<profile>/logs/*` + at most 1–2 live probes. No file edits.

## Signals to parse from agent.log

All lines below are INFO/WARNING lines in `logs/agent.log` (UTF-8; on Windows read with
`errors='replace'`). A Python script over `read().splitlines()` + `re` beats grep for
percentiles.

### 1. End-to-end latency (gateway users only)

```
grep "response ready" logs/agent.log
# INFO gateway.run: response ready: platform=wecom chat=<id> time=37.8s api_calls=7 response=323 chars
```

- `time` = true wall-clock the user waited for a reply (includes all API calls + tools).
- Only emitted on the gateway path (wecom/telegram/...), not CLI.
- Per-session chat id lets you isolate a business session from test sessions.
- Baseline (2026-08-07): 8.4–46.1s, p50 ≈ 19.6s, p75 ≈ 37.8s; api_calls 1–7 per turn.

### 2. Tool-level cost (separates tool time from model time)

```
grep "tool .* completed" logs/agent.log
# INFO [SID] agent.tool_executor: tool datasage_query completed (1.50s, 6264 chars)
# INFO [SID] agent.tool_executor: tool datasage_catalog completed (0.22s, 7576 chars)
```

- Baseline: datasage_query 0.96–3.34s (p50 ≈ 1.5s); catalog 0.09–0.36s; entity_resolve 0.63–0.97s.
- Large `chars` outputs (10–36K) are the latency amplifier: they inflate the NEXT API call's
  `in=` tokens → longer latency. This is why "why/analysis" turns are the slowest.

### 3. Model/API cost (the real latency driver)

```
grep "API call #" logs/agent.log
# INFO [SID] agent.conversation_loop: API call #12: model=deepseek-v4-flash provider=deepseek in=33772 out=2283 total=36055 latency=28.1s cache=...
```

- `latency` per call: baseline 1.4–28.1s (p50 ≈ 5.3s) on WeCom business sessions; long test
  sessions hit 60–94s at 300K+ input.
- `cache=` shows prompt-cache hit ratio (90–100% = healthy; a drop signals cache invalidation).

### 4. Compression verification (bloat control)

- Compression event = adjacent API calls in the same session where `in=` drops >30% between
  consecutive calls.
- No drops + long session = compression NOT triggering → context-bloat risk.
- Sample the `in=` trajectory; on deepseek-v4-flash (large window, threshold 0.5) compression
  fired only around ~300–338K tokens (drops of 39–94% back to ~18–25K). Long sessions pay a
  slow/expensive phase before the drop; short WeCom sessions (≤51K) never trigger it — fine.
- `compression.enabled=true, threshold=0.5, target_ratio=0.2` in config.yaml (profile defaults).

### 5. Query success/failure (authoritative numbers)

```
grep "datasage_query_batch" logs/agent.log
# INFO ... datasage_query_batch {"request_count":2,"success_count":2,"failed_count":0,"partial":false,"error_codes":[]}
# INFO ... datasage_query {"query_id":"dq_...","domain":"target","mode":"metric","status":"success","data_state":"truncated","row_count":10,"elapsed_ms":577,"execution_retry_count":0,"preflight_status":"passed",...}
```

- Report failure rate over `request_count`/`failed_count` sums, NOT log line counts.
- `data_state: truncated` is the expected row-cap (limit 10), NOT an error. `rows` = full.
- Baseline 2026-08-07: 24/24 success, 0 failed, 0 partial, empty error_codes.
- `elapsed_ms` = SQL execution only (467–968ms, p50 ≈ 609ms).

### 6. Runtime health / identity (fail-closed gate)

```
grep "datasage_startup_health" logs/agent.log
# INFO hermes_plugins.datasage_query.runtime_health: datasage_startup_health component=database_query version=0.12.0-dev1 artifact_id=datasage-canary-next payload_sha256=... ready=True reason_code=None identity_state=installed identity_expected=<hex> identity_actual=<hex>
```

- `ready=False reason_code=...` = fail-closed: ALL datasage_query calls are blocked until fixed.
- Multiple health lines exist (one per plugin load/restart). **A stale earlier `ready=True` does
  NOT mean the profile is queryable now** — check the LAST line and then do a live probe.
- Blocked queries surface to the user as `HERMES_IDENTITY_UNVERIFIED` while the internal
  reason_code is more specific (e.g. `HERMES_IDENTITY_CHECKOUT_DIRTY`).

### 7. Gateway stability

- `logs/gateway-exit-diag.log`: JSON lines `gateway.start` / `asyncio.run.returned` /
  `gateway.exit_clean` per pid. Every start should end with `exit_clean success=true`;
  anything else = crash. Baseline: 7 starts, all clean.
- `logs/gateway-stdio.log`: websocket drops ("[Wecom] WebSocket error: WeCom websocket closed")
  followed by reconnects — count them, confirm reconnect.
- `gateway_state.json`: current `gateway_state`, `platforms.wecom.state`, pid. Cross-check the
  pid is alive.

### 8. errors.log triage (noise vs real)

- Dedupe: strip `^YYYY-MM-DD HH:MM:SS,mmm`, `[session_id]`, numbers, `Ns` durations → count
  message skeletons.
- Known noise for this profile: auxiliary_client (nous/openrouter not configured), tools.registry
  check_fn False (browser etc. unavailable), `datasage_database_plaintext_transport
  production_mode=false tls_verified=false` (canary config, 38 lines), skill_manage description
  length, search_files rg path errors, gateway "Shutdown context signal=UNKNOWN" (planned
  restarts — cross-check exit-diag before calling it a crash).
- Real failures for THIS plugin family: CONTRACT_UNAVAILABLE, OUTPUT_TOO_LARGE, query timeouts.

## Live probe (1–2 max, control cost)

```
cd <profile-root> && timeout 180 hermes chat -q "<typical business question>"
```

- Pick a question the business session logs already used (e.g. "hcm上个月目标") for comparability.
- If blocked (`HERMES_IDENTITY_UNVERIFIED` / readiness_blocked), report the reason_code, verify
  health-line state, and STOP — do not retry-loop a fail-closed path. A blocked probe is itself a
  finding (an identity/readiness regression = release blocker).
- Session dir grows sessions/ + state.db — acceptable for a probe; don't clean up (read-only).

## Incident timeline (2026-08-07) — CHECKOUT_DIRTY release blocker

```
14:32:53  .release/RELEASE.json generated (identity_expected=5268dc39...)
14:33:25  runtime_health ready=True  identity_state=installed, expected==actual
14:42:48  runtime_health ready=False reason_code=HERMES_IDENTITY_CHECKOUT_DIRTY
          (identity_expected still == identity_actual → NOT a hash mismatch)
14:49:29  plugins/datasage-query/tools.py mtime — source edited AFTER release
14:59+    datasage_query readiness_blocked ... public_reason_code=HERMES_IDENTITY_UNVERIFIED
15:00     live probe fails: "查询失败，没有返回任何数字 ... 身份校验错误"
```

Root cause: runtime identity check hashes the plugin checkout against the release payload;
post-release source edits flip it to CHECKOUT_DIRTY and fail-close every query. Fix options:
restore the touched file(s) to the released state, or regenerate RELEASE.json, then
`hermes gateway restart` and confirm the LAST `datasage_startup_health` line is
`ready=True reason_code=None` followed by one successful live query.

## Report shape (中文, business-facing)

- Verdict per dimension: 达标 / 基本达标 / 不达标 + one-line justification.
- Risk table ordered by severity: blocking items first (with exact reason_code), then 🟡/🟢.
- Evidence numbers: n, p50, p90, max for latency; failure rate for queries; gateway restart count.
- 上线建议: the single first action that unblocks (e.g. re-sign identity, restore file).
