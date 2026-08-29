# DataSage Expert 0.15 架构

版本：`0.15.0-rc8`
运行基线：Hermes `0.20.5`

## 唯一目标

增强 Hermes 理解经营问题、选择证据、形成结论和提出行动建议的能力，
而不是用注册表、规则、工作流或答案模板替代 Hermes 的判断。

本版是边界重构，不增加数据库、不修改企微凭据、不引入路由引擎，也不
迁移全部指标到新 DSL。历史冻结标准与实现计划仅供审计，见
`docs/history/REFACTOR_CHARTER.md` 和 `docs/history/REFACTOR_DESIGN.md`；
它们不随运行 Profile 发行，也不是当前操作指令。

## 四层职责

### Hermes：经营判断

Hermes 负责理解问题、选择指标和分析深度、安排工具调用、区分观察与假设、
形成结论及建议，并在多轮对话中接受用户最新纠正。任何能力元数据都不得
指定固定指标数、固定调用顺序、原因、阈值、建议或答案措辞。

### Profile Skill：分析方法

`skills/business-analytics/datasage/SKILL.md` 只说明何时使用 DataSage、如何
自适应分析和解释证据；少用的稳定定义通过 Hermes 原生 `references/` 按需加载。
Skill 不拥有指标能力、权限、receipt、物理查询或结论授权，也不再通过插件
实现第二套 reference loader。

### Capability contract：可验证事实

`plugins/datasage-query/capability_contract.py` 是请求字段所有权和物理执行成本
的单一合同。它只描述可确定验证的事实：字段属于哪个 domain、允许枚举、
目标指标是否需要归因方式、公开分支和物理操作预算，以及 schema 条件。

现有 `*-semantics.yaml` 继续作为 v0.15 指标定义和物理查询来源。新增普通
指标不需要再维护一份路由注册表；能力合同也不能包含 prompt 关键词、recipe、
question type、固定 plan、业务判断或回答模板。

### DataSage plugin：确定性治理和证据

插件负责 schema、权限、receipt、只读查询编译、执行限制、typed state、
证据压缩和披露。插件返回结构化证据，不读取、删改或重写 Hermes 的最终文本。
SOUL 只保留专家身份与事实/假设/建议等高层原则；动态期间、scope、benchmark
和兼容性只在工具证据中计算一次，wire 只做结构投影。

## 规则所有权与生命周期

下列清单是治理库存，不改变既有模型安全约束。一个规则文件可以被发行包携带，
但只有列出的 consumer 可以把它当作语义输入：

| 规则 ID / 资源 | Owner | Consumer | 生命周期与权威边界 |
| --- | --- | --- | --- |
| `SOUL.md` | Profile 身份层 | Hermes 启动上下文 | 高层身份与安全原则；不是指标、实体或查询事实源 |
| `skills/business-analytics/datasage/SKILL.md` | Profile Skill | Hermes Skill loader | 激活、工作流与既有安全边界；随 Skill 版本发布 |
| `datasage.query-rules/v1` | Skill 请求规则 | Hermes 按需加载、库存测试 | 请求构造方法；live schema/catalog 拥有可用字段和值 |
| `datasage.entity-guidance/v1` | Skill 实体指导 | Hermes 按需加载、库存测试 | 模型安全投影；不能创建或覆盖实体映射 |
| `datasage.answer-boundary/v1` | Skill 最终回答策略 | Hermes 按需加载、库存测试 | 详细回答边界；插件不注册 prompt 副本 |
| `datasage.entity-maintainer-rationale/v1` | 插件维护者 | 维护者、库存测试 | 源码仓库中的非模型、非运行时文档；不进入发行载荷，只有落入 registry/domain semantics 并有测试才生效 |
| plugin contracts/schema/results | DataSage plugin | plugin runtime、Hermes tools | 指标能力、权限、执行与返回证据的确定性权威 |

发行与模型权威是两回事：`entity-rules-maintainer.md` 只留在源码仓库供审计，
不属于 `distribution_owned`；Skill 不得加载它，插件也不得 import 它。
库存测试负责机械检查这些 owner、consumer、lifecycle 和引用关系，防止无
consumer 的孤儿规则。

## 请求与失败域

```text
用户问题
  -> Hermes 自适应选择证据
  -> 公开 schema（由 capability contract 生成字段条件）
  -> 每个 public branch 独立验证
  -> 每个 branch 按 operation 申请物理执行槽
  -> 成功与局部失败共同进入领域语义压缩
  -> 保留全部完整分支交给 Hermes 原生 spillover/turn budget
  -> Hermes 形成答案
```

只有 envelope 无法解析、权限整体拒绝或无法建立可信执行环境时可以整批失败。
某个 branch 的字段、receipt、实体、能力或物理预算错误必须局部化，不能清空
其他有效结果。`complete` 操作成本为两个物理槽；普通操作成本为一个。公开
最多十个 branch，每个 branch 放不进剩余物理预算时仅返回
`EXECUTION_BUDGET_EXCEEDED`。

原始单结果、批结果 byte gate 与 Profile 自建最终字符预算均已删除。行数、
单元格长度、查询超时、SQL 只读和权限仍保留。DataSage 只做领域语义压缩；
完整结果交给 Hermes 原生 spillover，不再按 success-first 丢弃 branch。

## Scorecard 边界

`performance_scorecard` 是经营分析候选镜头的目录视图，不是一份必须照抄的
执行计划。它可以提供增长、目标、回款、库存、风险等可用镜头和各自 receipt，
并披露利润、现金流等缺口；Hermes 根据问题和证据重要性选择最小充分子集。
测试不得锁定固定八项、固定顺序、固定调用数或固定最终答案。

## 已删除的设计

- 冲突的 `datasage-query-patterns` companion Skill；
- test-only `skill_prompt.py` 和自定义 `datasage_reference`/reference registry；
- Hermes bundled Skill 的全局 `.no-bundled-skills` opt-out，改为受测试约束的
  保守 denylist；
- 每次查询重复返回的静态 `answer_guardrails` 和英文解释规则；
- 模型可见的 planner source、固定 overview bundle 和 recipe 路由；
- schema 允许但运行时整批拒绝的重复字段验证路径；
- compact 之前的 raw result / raw batch byte gate；
- 精确 plan 集合评分和固定最终答案哈希作为发布门槛。

Golden 测试改为“必需能力 + 禁止行为 + 语义结论”约束。它验证专家能力的
边界，不要求模型重复某个作者预设的思考轨迹。

## 多轮与宿主边界

最新用户消息必须高于 Memory、压缩摘要、旧任务、旧草稿和门禁纠偏文本。
Profile 只允许 Memory 保存稳定偏好和稳定事实，不保存 receipt、临时 period、
临时 entity、工具步骤或模型草稿。

Memory 永远不是查询语法、指标/实体 ID、别名、地域映射、catalog 能力或插件
运行状态的权威来源；即使旧 Memory 中存在此类内容，Hermes 也不得采纳，必须
重新以 live schema/catalog、实体 registry 或用户当前明确输入为准。清理已有
持久内容属于用户数据变更，必须另获用户明确授权，不能由架构迁移静默完成。

Hermes `0.20.5` 在读取配置失败时会让自动 background review fail-open，同时
write-approval gate 也可能回落为关闭。本 Profile 因而显式关闭自动 background
review，只保留由用户主动触发的 `/refine`；Memory 和 Skill 写入继续通过现有
write-approval gate 进入待审批区。每次重启前的门禁必须验证 `config.yaml` 可解析，
并在 `HERMES_HOME` 指向本 Profile 时使用锁定宿主的真实判定，确认自动 review
关闭且两个 approval gate 均开启。该门禁不改变 Hermes 的失败默认值，也不是
上游根因修复；Profile 不为此复制宿主决策或增加第二套配置层。

Hermes 宿主的上下文压缩顺序不在 Profile 插件控制范围内。本包提供宿主合同
fixture，要求压缩后保留用户纠正、当前 period/scope/entity/metric 和事实/假设
区分；在宿主 E2E 通过前不得宣称该能力已由 Profile 自身修复。

未知自然语言地域不能由维度枚举自动升级为受控筛选值。枚举值只证明数据库中
观察到了哪些标签，不证明国家、区域代码或部门之间的别名关系，也不能成为
长期 Memory 事实。缺少稳定映射时由 Hermes 请求用户选择或提供映射；插件不
维护事件型国家到区域代码候选表。

### Hermes 内建 Skill 选择约束

删除 `.no-bundled-skills`，由 Hermes 官方同步机制提供内建 Skill。Hermes
`0.20.5` 的 `skills.disabled` 是全局排除列表，`skills.platform_disabled` 会与
全局列表按平台取并集；宿主没有持久 allowlist。因此本 Profile 在精确锁定
`hermes_requires: ==0.20.5` 的同时，用完整 denylist 模拟保守 allowlist，只启用：

- `document-to-action-items`、`meeting-action-items`：从文档和会议材料生成有出处的
  决策、义务与待办；
- `docx`、`xlsx`、`pdf`、`powerpoint`、`ocr-and-documents`：处理常见办公文件，
  包括提取、生成、编辑与验证；
- `grounded-citations`：为外部事实建立可验证引用；
- `weekly-review-planning`：把既有承诺、阻塞和下一步整理为周度工作计划。

未启用外部账号型、代码开发型、桌面控制型、社交媒体型、创意媒体型和功能重叠
但边界更窄的 Skill（例如 `nano-pdf`）。内建 Skill 仍由 Hermes 拥有，本 Profile
不复制或修改其内容。

每次 Hermes 升级都必须先运行测试，将宿主 bundled Skill 名称与
`skills.disabled` 做完整快照差异审查。新增、删除或重命名任一内建 Skill都会使测试
失败；只有人工审查并更新 denylist 后才允许升级。`platform_disabled` 仅在某个
渠道需要比全局集合更窄时使用，不能用来暗中扩大能力。

## 期间比较合同

流量指标公开支持 `year_over_year + matched_elapsed`。该能力使用一次批次冻结的
`observed_on`，把尚在进行中的当前窗口和上年窗口裁剪到同一历年边界，并在
公开证据中保留请求结束日、有效结束日、裁剪状态和数据新鲜度未证明状态。
只有统一 period compatibility 合同验证为 `compatible` 时，模型 wire 才授权
正式期间比较；两个独立查询加透明算术不能绕过这项授权。

Catalog 的 metric detail 与 domain view 在公开 schema 和运行时都机械互斥，
避免模型得到“schema 可表达、运行时却拒绝”的伪能力。上述合同同样贯通完整
变化分解的 overall 与 partition 分支，并保留既有逐分支 partial-success 语义。

## 发布身份与回滚

Git commit/tag 是当前源码与运行目录的版本身份，`distribution.yaml` 定义未来独立
发行时的安装载荷与版本声明。当前同址工作区由 Git 原地维护，不通过
`hermes profile install/update` 覆盖，也不创建第二个安装实例。
Profile 不实现第二套安装器、版本解析器或回滚器。

源码仓库中的 `build_release_receipt.py` 根据 `distribution_owned` 的运行文件计算
SHA-256，把 DataSage 特有的 replay、compaction、交付和性能门禁绑定到一个受测
载荷。Hermes 合法写入的 `name`、`source`、`installed_at` 会在计算时归一化；
这个 checksum 不是发行版本身份，也不能替代 Git tag 或安装来源证明。receipt
不读取 `.env`、数据库状态、sessions、logs、Memory 或企微凭据，并且构建脚本、
测试、E2E scorer 与历史设计文档不进入运行载荷。

重启前必须保持当前 Git 工作区可审计，并完成离线、宿主和候选门禁。回滚使用
上一份经过审查的 Git tag/commit，且不得覆盖 `.env`、`state.db`、sessions、
logs 或用户 Memory。未来如发布独立 Profile Distribution，必须先满足官方发行根
布局；该发布工作不是当前原地维护或重启的前置步骤。

## 验收门槛

- schema 与逐分支运行时字段合同机械等价；
- mixed valid/invalid batch 保留成功证据；
- complete 扩展超预算只局部失败；
- 领域语义压缩保留全部完整分支，并由 Hermes 原生 spillover 承载宿主预算；
- scorecard 的指标选择、顺序和调用数可自适应；
- planner/companion/fixed-answer scorer 不在发布路径；
- Git commit/tag 与 Profile Distribution 提供发行/安装身份，receipt 只绑定领域质量证据；
- 完整离线测试通过，并单独记录宿主 E2E、真实企微交付和三次稳定性测试的
  未验证状态，不能用单元测试替代。

## 验证状态的事实源

架构文档不记录测试数量、耗时、单次会话结论或候选是否通过；这些信息会随代码、
模型和环境变化，嵌入本文会成为漂移的第二事实源。离线测试报告、不可变 candidate
receipt、真实 gateway replay/delivery、宿主 compaction 以及性能/成本产物共同构成
当次发布证据，`build_release_receipt.py --verify-candidate` 汇总其机器可读状态。

任何缺失、目标版本不一致或未通过的 live/host/performance 证据都保持候选阻断。
离线 semantic fixture、provider 接收或正文字符数都不能单独解除门禁；是否可以
重启、切换或扩大流量必须读取当前证据，而不是引用本文件中的历史描述。
