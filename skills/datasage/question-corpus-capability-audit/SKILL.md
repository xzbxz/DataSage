---
name: question-corpus-capability-audit
description: >-
  Use when auditing question corpora against DataSage 六域.
---

# Question-Corpus Capability Audit（问题语料 × 能力目录审计）

Task class: given a JSON corpus of real questions (e.g. 407 条企微生产问题 from 23 users), produce a
structured Chinese deliverable in 5 sections:
1. 全量分类统计（14 类：DLV/RCP/AR/TGT/INV/MD/ORD/CMP/MT/CAP/XL/EXP/NOISE/UNS）
2. 六域能力映射（每类给出 能答/部分能答/不能答 + 理由，基于 governed 域）
3. 特别标注（不支持清单按原因分组、多轮上下文片段、跨语言、噪声）
4. E2E 抽样清单（~25 条，覆盖全部能答类别 + 边界点，每条带预期验证点）
5. 当前不支持问题全清单（原文+用户标签+时间+原因，按频次给排期建议）

## Workflow
1. **结构探查**：`python -c` 打印 type / 用户数 / 总数。questions 是 dict 列表（字段 time/question）——
   迭代必须用 `q['question']`，直接 `classify(q)` 会报 `'dict' object has no attribute 'strip'`。
2. **全量导出**：Windows 上 read_file 打不开 `/tmp/...`（file not found）——写到 `C:/Users/<user>/xxx_dump.txt`
   再分页 read_file 通读全部问题。
3. **锚点表**：打印每个用户的 完整ID | 条数 | 首/次/末条问题 作为锚点，再写人工覆盖表。
4. **分类器 + 人工覆盖 + 逐条复核**：关键字规则先粗分，覆盖表修正，最后通读分类输出表修正残余误判。
5. **输出**：统计（14 类合计必须=总数）+ 专项清单文件（UNS/MT/XL/EXP/NOISE）+ 最终报告。

## 分类器优先级（顺序错误会吞掉类别——实测踩坑）
NOISE → CAP(功能/寒暄/质疑) → XL(跨语言) → EXP(纯导出指令) → UNS(六域外指标) → TGT → AR → INV → RCP → CMP → DLV → ORD → MD → MT → 兜底 DLV
- **UNS 必须在领域类之前检查**，否则"利润/采购额/人效/SQ"会被 `排名|top|销售|出库` 关键字抢走归入 DLV/RCP。
- EXP 主类判定：含 excel/导出/word 且**无查询动词与指标词**；内嵌导出（"帮我查…然后生成excel"）只打 `exp` 标记，主类归查询域。
- XL：越南语变音符正则 `[àáạảãă…ỹ]` 或英文查询词；纯格式短句（"用VND表示""以VND计价"）归 MT。
- MT：≤10 字或含"呢/可以/继续/1/两者都查/原币/印尼盾/客户名称"等指代——上下文依赖类。
- 复合问题（多指标/多问）：主类 + flags(`exp`/`xl_vn`/`mt`/`uns`) + note 备注口径，不要硬塞单一意图。

## Pitfalls（全部实测）
- **WeCom 用户 ID 前 8 位完全相同**（`wo8gxKDA…`）→ 人工覆盖表必须用**完整 user_id** 做 key，
  截断会跨用户串数据、静默改错行。
- 复用旧脚本片段时 exec() 会覆盖新定义的 `classify`（旧版返回含 set 的元组）→ Counter 报
  `unhashable type: 'set'`。修复：只提取覆盖表定义段（`src.index('# ==== 人工覆盖')` 到 `results = []`）。
- 写含 emoji/`\U0001F…` 的 Python 正则时用 raw string 或 `ord(c)` 范围判断，普通字符串会
  `unicodeescape codec can't decode`。
- 典型规则误判（逐条复核要抓）：纯导出指令被 DLV 抢走（"把明细导出Excel文件给我"）；"1785 有哪些
  颜色在印尼仓库"应归 INV 而非 DLV；"客户phappim 142，这个客人怎么样"=客户风险诊断应归 AR；
  "子不语…TOP10产品…供应商名字"应归 DLV 而非 MD。
- 分类后必须重跑统计并核对 14 类合计 = 语料总数。
- 读文件用 read_file、写脚本用 write_file（heredoc/cat 违反约定且易炸转义）。

## 能力映射 verdict 口径
- 每类给 能答/部分能答/不能答 三元判定 + 一条理由（引用 governed 域指标）。
- 明确列出"待验证"项（去重客户数/产品数、色号下钻、按天粒度、双语输出、供应商编号查询、地址区域）。
- 六域组合指标（如 有出库无回款=回款风险客户）标"组合能答"。

## 报告要点
- 用户标签法（用户A/B/… 映射完整ID）保持报告可读；报告用中文。
- E2E 抽样覆盖：六域各≥1、时间粒度（本月/上月/某月/1月到今天/去年每月/按周/同比）、维度
  （部门/客户/销售/业务员/产品/色号/仓库/区域）、跨语言 2 条、多轮 2 组连测、边界（目标完成率
  可为负/超100%、Top-N 截断、原币）。
- 抽样执行闭环（实测验证，2026-08-07）：26 条抽样跑完 = 24 完成（92%）/ 2 超时 / 0 硬错误。
  两个"超时"都是**实体/口径歧义 → 模型正确 clarify 等用户**（8883 多候选、"家纺"无精确匹配），
  不是失败——无人值守 E2E 里挂起是正常现象，真人对话中会继续。解读结果时不要把 clarify 挂起当
  失败：先查会话的 `tool_name='clarify'` 记录，有 `question`+`choices_offered` 就是"等待澄清"。
- 抽样执行细节与报告生成见 `datasage-profile-ops` 的 E2E 部分与
  `references/e2e-report-generation.md`（runner、超时重测、state.db 提取完整回答）。
- 支持文件：
  - `references/datasage-six-domain-mapping.md` — 六域指标/维度/时间支持 + 实测不支持指标族 + 多轮/噪声模式
  - `scripts/classify_questions.py` — 通用关键字分类器骨架（优先级顺序、完整ID覆盖键、flags）
