# R27 核验记录：独立真值、留出题与发布矩阵

日期：2026-09-23｜依据：综合审查 V2.0 的 R27（批次 F / 发布前置）｜负责角色：业务 owner＋独立复核人＋发布 owner
前置：R05、R11、R17、R18、R19、R20、R21（已完成）｜性质：证据与矩阵已就位；真值、留出确认与发布决定必须由人做

## 1. 结论

- **助手只准备证据，不代签发布**：矩阵 15 个能力面全部 `released=false`、`release_decision=null`；
  签名字段为空，策略里明写「助手不代签」。
- **真值与模型输出分离**：24 个业务案例各有独立真值位，当前 **0 案已填**（全部 `pending`）；
  允许的证据类型只有 `independent_sql`／`manual_fixture`／`owner_statement`／`expert_review`，
  **模型输出、引擎输出与自评被显式排除**。
- **留出题与调参集分开**：B19–B24（6 个跨域/边界案例）为留出题，B01–B18 为调参集，
  两者互斥且并集等于全部案例。**该划分目前是助手提议**（`confirmed_by=null`），
  需业务 owner 确认后生效——确认前不得声称留出隔离已完成。
- **失败不隐藏、不删除**：每个能力面都有样本数、通过数、状态、失败原因与复现路径；
  `retired` 为空（本轮没有任何能力被撤下），撤下必须在册并写原因。
- **不可复现的数据不入矩阵**：由 `release_matrix.unreproducible_problems` 校验，
  标为不可复现的行不得携带测量数字。

## 2. 发布矩阵（15 个能力面，全部未发布）

| 能力面 | 样本 | 通过 | 状态 | 失败原因（摘要） |
| --- | --- | --- | --- | --- |
| domain.delivery | 36 指标 | 0 | unverified | 指标缺独立证据 |
| domain.receipt | 13 指标 | 0 | unverified | 同上 |
| domain.receivable | 51 指标 | 0 | unverified | 同上 |
| domain.target | 8 指标 | 0 | unverified | 同上 |
| domain.inventory | 34 指标 | 0 | unverified | 同上 |
| domain.pattern_matching | 70 指标 | 0 | unverified | 同上 |
| domain.profit | 3 指标 | 0 | unverified | 同上 |
| analysis.cross_domain_reasoning | 24 案 | 0 | unverified | 0 案有独立业务评审、0 案实际执行 |
| channel.wecom_surface | 7 工具 | 7 | partial | 真实渠道回执未到 |
| reports.operational_entries | 16 入口 | 16 | partial | 真实调度与发送未演练（全部默认关闭） |
| answers.business_presentation | 4 类答案 | 4 | partial | 真实渠道答案评审未做 |
| analysis.domain_diagnostic_paths | 8 方法 | 8 | partial | 业务 owner 未评审诊断链 |
| artifacts.integrity_and_recipients | 8 注入项 | 8 | partial | 真实客户端与接收人复核未做 |
| operations.authorisation_and_lifecycle | 16 入口 | 16 | partial | 周期续期与投递演练待授权 |
| security.data_source_and_privacy | 8 外部项 | 0 | blocked_external | canary 模式：明文传输被允许；8 项全部 pending |

样本与通过数由 `tests/release_matrix.py generate` 从 R16 指标册、R19 案例夹具与 R25 台账
派生，审阅者可用 `check` 复现；数字不可手填。

## 3. 真值与留出

| 项目 | 现状 |
| --- | --- |
| 案例总数 | 24（B01–B24） |
| 已有独立真值 | 0 |
| 留出题 | 6（B19–B24，提议待确认） |
| 调参集 | 18（B01–B18） |
| 允许的真值证据 | 独立 SQL／人工夹具／owner 陈述／专家复核 |
| 禁止的真值证据 | 模型输出、引擎输出、自评 |
| 专家偏差与争议 | 每案有 `disagreement` 与 `adjudication` 位（当前为空） |

## 4. 守卫（8 项，全绿）

覆盖全部能力面且样本/通过/状态/复现路径齐全｜失败必须写明原因（`verified` 要求通过数等于样本数）｜
不可复现数据不得携带测量数字（含反例校验）｜真值不得来自模型（含允许/禁止证据类型的正反例）｜
留出与调参互斥且等待 owner 确认｜助手不签名不发布（含策略原文校验）｜能力不得静默删除｜
矩阵与其来源逐行一致。

## 5. 与验收条件的对应

| R27 验收条件 | 状态 |
| --- | --- |
| 真值与模型输出可追溯分离 | 通过：真值位独立，模型/引擎输出被排除 |
| 留出题不进入调参 | 通过（划分已固化；生效待 owner 确认） |
| 发布矩阵覆盖全部已发布能力 | 通过：7 域 + 8 面 = 15，逐面记录样本/通过率/原因/复现路径 |
| 失败有原因无静默删除 | 通过：原因是必填；撤下需在 `retired` 记原因 |
| 无法复现的数据不写矩阵 | 通过：校验函数 + 反例用例 |
| 未验能力既不标已发布也不删 | 通过：8 面 unverified、1 面 blocked_external，全部 `released=false` |
| 不代签发布 | 通过：签名字段为空且受守卫保护 |

## 6. 需要谁做什么

1. **业务 owner**：逐案确认 24 个独立真值（或在册记争议与裁决）；确认 B19–B24 的留出划分。
2. **独立复核人**：给出与模型输出无关的复核证据（独立 SQL 或人工夹具）。
3. **数据 owner**：填 R16 指标册的独立证据（与 R27 的域能力面联动）。
4. **发布 owner**：逐能力面给发布决定；无决定即保持未发布，并在 R31 灰度复验后签字。

## 7. 回退

- 登记册、生成器与守卫可整体移除，不涉及调度、发送或模型配置。
- 若矩阵与来源不一致，按 `check` 提示重跑 `generate`；**不得**手工改样本/通过数或删除失败行。
