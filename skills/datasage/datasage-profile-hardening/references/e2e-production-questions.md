# E2E batch testing on production question sets

When the user hands over a real production WeCom question corpus and asks for
E2E tests ("这是生产环境中搜集出来的问题...帮我做一下E2E，并把测试结果给我"),
do NOT run every question through `hermes chat -q` and do NOT hand-test one at
a time. Observed corpus scale (2026-08-07): 23 users / 407 questions,
53KB JSON, ~4-7 hours if run fully at ~30-60s each plus API cost.

## Workflow

1. **Read the corpus first** (read_file may truncate; page through with
   offset/limit until total_lines reached). Understand scale and shape before
   planning. A 407-question file pages in ~4 reads.
2. **Dispatch ONE leaf subagent for offline classification** (pure Python on
   the JSON, NO datasage/hermes tools, no file edits):
   - Per-category counts (suggest: 出库/销售, 收款, 应收/欠款/风险, 目标完成,
     库存/备货/滞销, 客户/供应商主数据, 订单明细, 对比分析, 多轮追问/指代,
     功能询问, 噪声, 跨语言, Excel/导出请求, 当前不支持).
   - Capability mapping: 能答 / 部分能答 / 不能答 with reason (metric/dimension
     in the 6 domains or not).
   - Explicit unsupported list with question text + user + time + reason
     (e.g. 利润, 人效, 物流线, 样品/SQ/样卡, 采购额, 客诉, 拜访, 账期控制,
     BI 报表, 导出 Excel/Word, 订单明细/原数据, 新客户数定义).
   - Multi-turn context-dependent fragments (same user adjacent questions that
     need prior context: "Jolie呢", "两者都查", "1", "继续", "可以").
   - Cross-language samples (Vietnamese / Indonesian / English / pinyin).
   - Noise rows: emoji (`[合十]`), system echoes (`' reply_to_id=None'`,
     `[IMPORTANT: Background process ...]`).
   - A representative sample of ~20-25 items covering every answerable
     category: ≥1 per domain, different time grains, dimension variety
     (department/customer/sales/product), ≥1 cross-language, one multi-turn
     group (2-3 items to run sequentially), edge cases (negative, truncated,
     future period) where possible.
3. **Batch runner** (small standalone script in profile `workspace/`, e.g.
   `e2e_runner.py`): read sample JSON (array of `{"id","question"}` or
   `[id, question]`), per item run `hermes chat -q "<question>"` with timeout
   (150s), record `elapsed_s / exit_code / answer_len / answer_tail`
   (strip the "Resume this session with:" tail), write results JSON. Verify
   the runner once with a single known-good question before the batch
   (compile + 1 real run + structure check) — use the hermes-verify- ad-hoc
   pattern.
4. **Run the sample batch** and report per-category pass/fail, elapsed
   distribution, timeouts, and unverifiable items. Keep the answer corpus in
   the results JSON for later review.

## Pitfalls

- Expect ~30-40s per item end-to-end (tool ~1.5s; the rest is model API
  calls). A 20-item sample ≈ 10-15 min; use `timeout` per item and a
  generous overall window.
- Occasional single-query TIMEOUT is model latency, not a runner defect —
  retry that item once (a previous identical question returned in 30s).
- `hermes chat -q` writes full tool/API traces to `logs/agent.log`; each run
  is a fresh session (history=0). For multi-turn group items, run them in
  sequence and rely on the answer_tail of the first to judge the second —
  `hermes chat -q` does not chain context, so true multi-turn behavior is
  better judged from existing WeCom session transcripts in `state.db`.
- Do not modify profile/plugin files during E2E; the identity gate
  (`.release/RELEASE.json` vs hermes-agent checkout) is checked per query and
  a dirty checkout fails-closed ALL queries (see SKILL.md identity-gate
  pitfall) — if that happens mid-batch, stop and fix identity first.
