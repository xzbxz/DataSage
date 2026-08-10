# Agility/Flexibility (活) pre-launch review — datasage-canary-next

Recipe for auditing the 活 dimension of the governed query profile: how the
agent handles diverse, ambiguous, multi-turn, and complex real-user questions.
Complements `references/speed-stability-review.md` (快/稳). Verified 2026-08-07.

## Evidence sources

### 1. `state.db` `messages` table — full transcripts (primary)

agent.log records only user message text + timings; the actual assistant
answers and query payloads live in the SQLite state DB. Schema (key columns):
`id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp`.

```python
import sqlite3, json
con = sqlite3.connect('<profile>/state.db'); cur = con.cursor()
# 1) full transcript (assistant text + tool outputs):
cur.execute("SELECT id, role, content, tool_name FROM messages WHERE session_id=? ORDER BY id", (sid,))
# 2) EXACT request arguments (the audit gold): assistant rows' tool_calls JSON
cur.execute("SELECT id, tool_calls FROM messages WHERE session_id=? AND tool_calls IS NOT NULL ORDER BY id", (sid,))
for r in cur.fetchall():
    for tc in json.loads(r[1]):
        fn = tc.get('function', {})
        print(r[0], fn.get('name'), fn.get('arguments'))  # parse arguments as JSON
```

This reveals per-request: metric code, `evidence_role`, `order_by`/`limit`,
`time_bucket`, `metric_filters` — everything needed to judge planner-contract
compliance. `datasage_query` tool outputs contain `data_state` (rows/truncated)
and `limitations` (e.g. `COMPLETE_POPULATION_STATEMENT_NOT_AUTHORIZED`) — the
answer layer's honesty can be checked against these.

### 2. `logs/agent.log` — timing + user messages

- User msgs + session: `grep "conversation turn" logs/agent.log` (each line has `msg='...'`).
- Per-query status: lines `hermes_plugins.datasage_query.tools: datasage_query {"query_id":...,"status":"success","data_state":"rows","row_count":N,"elapsed_ms":N,...}` and `datasage_query_batch {"request_count":N,"success_count":N,...}`.
- Per-turn cost: `Turn ended: ... api_calls=N tool_turns=N response_len=N`.
- Distinguish sessions by `[session_id]` prefix; `history=N` shows context growth across turns (multi-turn retention evidence).

### 3. Rule baseline + mtime forensics

- `skills/target-query/references/planner-contract.yaml` — domain rules
  (`ambiguous_target_type: return_delivery_and_receipt_together`, completion
  overview recipe, 为什么没完成 routing, etc.).
- `skills/datasage/SKILL.md` analysis-class planning rules (batch ≤3,
  ≤1 ranking, limit ≤10, decomposition preference) — user/plugin-owned file,
  but READ it for the baseline; the curator mirror is `datasage-query-patterns`.
- `ls -la --time-style=+%m-%d_%H:%M skills/...` — compare skill/contract
  mtimes against session timestamps: a session BEFORE a rule's mtime is not a
  regression. (2026-08-07: rules landed 11:35; morning 10:14 5-request batch
  predates them; 11:36 session complies.)

## Rubric (5 items)

1. **问法多样性** — 口语/缩写 (hcm), 模糊时间 (上个月), 拼音, 混合输入.
   Pass: correctly resolved + assumption stated.
2. **多轮追问** — 指代 ("它", "HN怎么样呢"), implicit scope reuse (same
   metrics/period as previous turn), cross-turn comparison.
   Pass: interpretation stated BEFORE querying; resolver confirms entities.
3. **歧义处理** — clarify only when materially different interpretations
   remain (with a "两个都要看"-style option); transparent assumption otherwise;
   never over-clarify (e.g. "我肯定是想看完成情况" needs zero clarification).
4. **复杂问题 (为什么类)** — batch ≤3, ≤1 ranking/turn, limit ≤10, truncation
   flagged, 事实 vs 假设 layered, no causal overclaim; error recovery
   (e.g. failed request retried with corrected scope).
5. **体验细节** — conclusion-first, tables, plain language, 口径 caveats
   (出库 exclude internal customers / 收款 include), next-step offers,
   capability-boundary accuracy (see quirk below).

Overall verdict: 达标 / 基本达标 / 不达标 with a severity-ordered risk table
(P0 blocking items first, with exact reason_code).

## Live probes

2-3 diverse phrasings, one per area: pinyin business query, fuzzy ranking,
cross-entity why/comparison. Run: `cd <profile> && timeout 180 hermes chat -q "..."`.
If blocked by `HERMES_IDENTITY_UNVERIFIED`: report reason_code, do NOT
retry-loop; inspect the hermes-agent checkout (see datasage-profile-hardening
identity-gate pitfall). Verify the model's failure UX: no fabrication, honest
"没拿到数据", error code surfaced.

## Known quirks (2026-08-07 findings)

- **WeCom toolset is only `datasage-query` / `clarify` / `todo`** (config.yaml
  `platform_toolsets.wecom`). Answers listing Hermes general capabilities
  (web search, file reading, Excel/PDF export) OVERCLAIM the channel — the
  pinyin "你有哪些功能" answer did exactly this. Capability lists must match
  the channel toolset; the "你是谁" answer got this right.
- **"目标完成" without 出库/收款**: planner-contract default is return BOTH;
  the model may instead clarify (allowed by datasage skill) then default to
  出库-only on no reply — minor deviation, note it.
- **Ranking pairs**: "最低+最高" two-sided look = 2 ranking requests in one
  turn (letter-rule violation, defensible if the answer stays bounded).
- **Failure-answer inconsistency**: two live runs both refused to fabricate,
  but gave different root causes (one diagnosed the dirty checkout precisely,
  the other guessed "CLI 未绑定企微身份") — recommend a standardized failure
  template (error code + likely cause + escalation path).
- **Identical query asked twice** (14:26 success vs 14:58 + 15:03 fail) is the
  signature of an environment-state change (dirty checkout), not a regression
  in planning — check `git -C <hermes-agent> status --porcelain` first.
