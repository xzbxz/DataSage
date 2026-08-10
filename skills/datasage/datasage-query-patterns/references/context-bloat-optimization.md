# Context-bloat optimization pass (2026-08)

Session record for the datasage-canary-next optimization that fixed runaway
context growth on "为什么/分析" questions. All values below were applied,
verified, and measured on 2026-08-07.

## Problem observed (before)

WeCom analysis turn "你帮我分析一下，为什么会这么好":
- 5 queries in one batch, two of them Top-10 rankings
- `datasage_query completed (3.34s, 35907 chars)` — one tool result of 35.9 KB
- API input: 16.5K → 33.7K tokens in that turn
- By turn 5 of the conversation: 51,254 tokens input, one API call 28.1s
- Total turn latency 46.1s

## Changes applied

### 1. Hermes compression enabled (config, recognized keys — safe via hermes config set)

```bash
hermes config set compression.enabled true
hermes config set compression.threshold 0.50
hermes config set compression.target_ratio 0.20
hermes gateway restart
```

Config now contains:
```yaml
compression:
  enabled: true
  threshold: 0.5
  target_ratio: 0.2
```

Note: compression triggers only when context reaches ~50% of the model
window. With a ~100K window, a single short turn at 53K does NOT trigger it —
expected; it protects long multi-turn sessions, not single turns.

### 2. datasage/SKILL.md analysis-class planning rules

Added "Analysis-class planning rules" to the Plan section (the `datasage`
skill is plugin/user-owned so this edit was done in-session via patch; the
rule content is mirrored in the curator-managed `datasage-query-patterns`
skill for future reference). Core rules:
- Why/change/contribution questions: prefer one `complete_change_decomposition`
  over several truncated Top-N rankings.
- Ranking serves "who is largest/smallest" only; ≤1 ranking per turn, limit ≤ 10.
- Batch ≤ 3 requests: overall, comparison, then targeted follow-up.
- Framed as priority guidance, not hard caps.

### 3. tools.py ranking limit cap (plugin code)

At the limit-resolution site in `plugins/datasage-query/tools.py`
(~line 5287, `_execute` metric path):

```python
environment_cap = _bounded_int("max_rows", 100, 1, 100)
requested_limit = request.get("limit", environment_cap)
if not isinstance(requested_limit, int):
    raise QueryFailure("INVALID_INPUT", "limit 必须是整数。")
ranking_cap = 10 if request.get("order_by") is not None else environment_cap
limit = max(1, min(ranking_cap, environment_cap, requested_limit))
```

Effect: any ranking request (order_by present) is clamped to 10 rows;
non-ranking requests keep env cap 100. Verified behavior matrix:
- order_by=True, requested=50/100 → 10
- order_by=True, requested=1 → 1 (small requests preserved)
- order_by=False, requested=100/20/unset → 100/20/100 (unchanged)

## What was deliberately NOT changed (fail-closed limits)

`max_result_bytes` (262144) and `max_cell_chars` (2000) are FAIL-CLOSED:
exceeding them raises `QueryFailure("OUTPUT_TOO_LARGE", ...)` — the query
FAILS, it does not truncate. Lowering them to fight bloat would turn
legitimate large results into hard errors. This is the "不要降智" trap:
do not lower these; leave at defaults.

## Verification

- pytest baseline (importlib mode): `PYTHONPATH=<profile> python -m pytest
  plugins/datasage-query/tests -q -p no:cacheprovider --import-mode=importlib`
  → 91 passed / 1 failed (known missing `evaluation/expert-core/cases.yaml`,
  excluded from installed distribution; not a regression).
- Default pytest WITHOUT `--import-mode=importlib` fails to collect (92
  errors, "attempted relative import with no known parent package") because
  the package name contains a hyphen. Always use importlib mode.
- Post-change CLI replay of "帮我分析一下，为什么 HCM 部门 8 月出库目标完成
  情况这么好": tool output dropped 35,907 → 14,923 chars (first batch) /
  14,075 chars (second), queries came in 2 batches of 3 (matching the new
  guidance), answer correctly labeled 截断/观察/不能下结论 — quality
  preserved, evidence boundaries respected.

## Log-reading recipe (post-change monitoring)

- Tool output size: `agent.tool_executor: tool datasage_query completed (N chars, ...)`
- Context growth: `agent.conversation_loop: API call #N ... in=<tokens>`
- Turn latency: `gateway.run: response ready: platform=wecom ... time=<s>`
- Compression trigger: grep for "compress" in agent.log (absent = not yet triggered)
- Toolset applied: startup `tool_search activated (tier 1): N core/visible tools kept`
