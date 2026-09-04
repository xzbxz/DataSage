# DataSage Expert 0.15 架构

版本：`0.15.0-rc14`
运行基线：Hermes `0.20.5`

## 唯一目标

增强 Hermes 理解经营问题、选择证据、形成结论和提出行动建议的能力，
而不是用注册表、规则、工作流或答案模板替代 Hermes 的判断。

本版是边界重构，不增加数据库、不修改企微凭据、不引入路由引擎，也不
迁移全部指标到新 DSL。历史冻结标准与实现计划仅供审计，见
`docs/history/REFACTOR_CHARTER.md` 和 `docs/history/REFACTOR_DESIGN.md`；
它们不随运行 Profile 发行，也不是当前操作指令。

## 定位、用户与升级线

本 Profile 面向公司经营负责人及出库、销售、应收、财务、库存和目标
负责人，支持六个受治理经营域：delivery、receipt/collections、receivable、
target、inventory 和 customer_risk。它提供事实、诊断和证据约束的建议，
不是通用制度/HR问答、公共研究、用户文件分析器，也不是业务执行系统。
当前候选目标成熟度为 L3 数据专家；本候选版本不声明 L4 主动管理或主动巡检能力。

建议按风险分三档：描述性监测（事实与趋势）、诊断性解释（比较、核对和
明确标注的假设）、高影响建议（授信、客户损失、资源配置或目标承诺）。
高影响建议必须给出假设、负责审批的人、重大风险和复核点；证据不完整、
过期、不可用或歧义无法消除时，升级到相应的指标/域负责人或人工审批人，
不得自动批准、承诺或执行。

WeCom 的 Profile toolset 仅声明 `clarify` 和 `datasage-query`；所有已认证企微
成员均可私聊和群聊，并共享六个经营域同一完整的 DataSage 查询面。Profile 不施加
用户、群组、部门、实体、行或领域过滤；这不替代外部数据库授权。数据库执行仍为
SELECT-only，并由治理查询合同、只读执行限制和 evidence 边界约束。Hermes 原生
Skill 管理面不向 WeCom 暴露。
本候选版本无有 owner 的 cron/主动巡检任务，因此不声明 L4 主动管理能力。

业务员个人目标、分摊实际和完成率以 `salesperson_allocation` 的 split 账本为
独立权威来源。transaction-detail 基表只承担诊断参照，不是销售分摊账的覆盖
门禁；Profile 禁止在 split 缺失时从基表回填或跨账替代。

rc14 落实本轮出库事实口径：8 项数量指标恢复查询，必须按单位分组或限定单一单位，
`y`、`Pcs/PCS`、`m2/㎡` 分别解释为码、件、平方米且禁止跨单位合计；默认部门固定为
`customer_dept`，`biz_dept`/`biz_region` 作为独立业务发生归属；退货 `biz_org` 与
销售/出库 `org_name` 经业务 owner 确认为同一组织口径，各事实仍独立聚合；条码
`final_supplier` 仅开放于物理毛出库。历史原始码 `tao` 暂只按原码单列，不擅自解释。
企微全员私聊/群聊和数据库业务权限策略保持不变；正式 L3 live replay 与 150 案例门槛
仍 pending。

## 验收门槛（唯一量化真源）

本节是本 Profile 唯一的量化验收真源；README、SOUL 和 profile.yaml 只引用本节，
不重复阈值。按 V1.0，关键流程成功率须为 100%，总体流程成功率至少 98%，核心结果
正确率至少 97%，语义和业务交付成功率至少 95%，证据违规率为 0，故障恢复或正确失败率
至少 90%；总分至少 85，核心域至少 75%，且无未关闭 P0/P1。L3 基线至少 150 个独立案例，
其中开放分析不少于 25 个、专家判断不少于 15 个、故障与隔离不少于 15 个。只有在这些
门槛和全部硬门禁均通过后，才可讨论 production 声明。

## 四层职责

### Hermes：经营判断

Hermes 负责理解问题、选择指标和分析深度、安排工具调用、区分观察与假设、
形成结论及建议，并在多轮对话中接受用户最新纠正。任何能力元数据都不得
指定固定指标数、固定调用顺序、原因、阈值、建议或答案措辞。

### Profile Skill：分析方法

`skills/business-analytics/datasage/SKILL.md` 只说明何时使用 DataSage、如何
自适应分析和解释证据；Skill-enabled CLI/维护面可选按需加载稳定定义。
受限 WeCom 只依赖 SOUL、公开工具 schema 和返回 evidence，不以 `skill_view`
或 references 作为查询前置条件。Skill 不拥有指标能力、权限、物理查询或结论
授权，也不再通过插件实现第二套 reference loader。

### Capability contract：可验证事实

`plugins/datasage-query/capability_contract.py` 是请求字段所有权、物理执行成本、
公共查询策略、维度值域和 target-gap receipt 事实的 typed 单一合同。
`contract_store.py` 只负责受限路径和缓存；catalog、executor 与 evidence 通过
同一 parser 读取这些事实，不各自维护枚举或 validator。

现有 `*-semantics.yaml` 继续作为 v0.15 指标定义和物理查询来源。新增普通
指标不需要再维护一份路由注册表；能力合同也不能包含 prompt 关键词、recipe、
question type、固定 plan、业务判断或回答模板。

### DataSage plugin：确定性查询安全和证据

插件负责 schema、只读查询编译、执行限制、typed state、
证据压缩和披露。插件返回结构化证据，不读取、删改或重写 Hermes 的最终文本。
SOUL 只保留专家身份与事实/假设/建议等高层原则；动态期间、scope、benchmark
和兼容性只在工具证据中计算一次，wire 只做结构投影。

## 权威与维护职责

Hermes 拥有问题理解、工具选择、推理和最终结论；受限 WeCom 只使用 SOUL、公开
工具 schema 与返回 evidence。references 只在 skill-enabled CLI/维护面可选加载，
不改变 WeCom 查询路径。插件不注册 prompt 副本，也不拥有结论授权。

| 资源 | Owner | 使用方 | 边界 |
| --- | --- | --- | --- |
| `datasage.query-rules/v1` | Profile Skill | CLI/维护面（可选） | 请求构造提示；可用字段以 live schema/catalog 为准 |
| `datasage.entity-guidance/v1` | Profile Skill | CLI/维护面（可选） | 实体消歧提示；不能创建或覆盖实体映射 |
| `datasage.answer-boundary/v1` | Profile Skill | CLI/维护面（可选） | 解释提示；插件不注册 prompt 副本 |
| `datasage.delivery-analysis/v1` | Profile Skill | CLI/维护面（可选） | 出库 L3 分析提示；不能替代工具证据 |

`*-semantics.yaml`、typed capability contract、实体 registry 和公开工具结果分别
拥有指标定义、执行事实、实体身份和当前证据；`datasage.entity-maintainer-rationale/v1`
（`entity-rules-maintainer.md`）仅供源码维护者使用，不进入发行载荷。`contract_store`
只做受限路径和缓存，不引入规则引擎或运行状态机。

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
某个 branch 的字段、实体、能力或物理预算错误必须局部化，不能清空
其他有效结果。`complete` 操作成本为两个物理槽；普通操作成本为一个。公开
最多十个 branch，每个 branch 放不进剩余物理预算时仅返回
`EXECUTION_BUDGET_EXCEEDED`。

原始单结果、批结果 byte gate 与 Profile 自建最终字符预算均已删除。行数、
单元格长度、查询超时、SQL 只读和权限仍保留。DataSage 只做领域语义压缩；
完整结果交给 Hermes 原生 spillover，不再按 success-first 丢弃 branch。

## Scorecard 边界

`performance_scorecard` 是经营分析候选镜头的目录视图，不是一份必须照抄的
执行计划。它可以提供增长、目标、回款、库存、风险等可用镜头，
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

Profile Memory 只保存声明式的稳定偏好和稳定事实；`USER.md`
明确记录当前请求和用户纠正代表最新意图，可以取代旧偏好。Profile
不将指令式工作流、临时 period、临时 entity、工具步骤或模型草稿
写入 Memory。宿主角色优先级不由 Profile 重新定义。

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

未知地域与实体别名的模型处理由 `datasage.entity-guidance/v1` 唯一拥有；
可执行身份仍只来自插件 registry 与 domain semantics。本架构只声明这条所有权
边界，不复制实体选择、澄清或 Memory 行为正文。

### Hermes 内建 Skill 选择约束

删除 `.no-bundled-skills`，由 Hermes 官方同步机制提供内建 Skill。Hermes
`0.20.5` 的 `skills.disabled` 是全局排除列表，`skills.platform_disabled` 会与
全局列表按平台取并集；宿主没有持久 allowlist。因此本 Profile 在精确锁定
`hermes_requires: ==0.20.5` 的同时，用完整 denylist 模拟保守 allowlist，只启用：

- `docx`、`xlsx`、`pdf`、`powerpoint`、`ocr-and-documents`：处理常见办公文件，
  包括提取、生成、编辑与验证；

只保留了 `metadata.hermes.related_skills` 在启用集合内闭合的办公文件
Skill。`document-to-action-items`、`meeting-action-items`、`grounded-citations` 和
`weekly-review-planning` 会将流程引向当前禁用的外部账号型或专项 Skill，
因而不作为可见入口。`hermes-agent` 仍禁用；Profile 不为满足宿主的
自助提示而扩大自修改权限。

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

## 发布与维护边界

Git commit/tag 负责源码身份，`distribution.yaml` 只描述运行载荷；Profile 不实现
安装器、版本解析器或回滚器。`build_release_receipt.py`、离线测试、Host/E2E 检查
和 candidate receipt 都是源码质量工具，不进入 runtime，也不拥有业务结论。
`distribution_owned` 只包含运行文件；测试、eval、release/pending 产物和历史文档
留在源码仓库。运行时事实来自当前 semantics、typed contracts、实体 registry 和
工具返回 evidence；本架构不保存测试结果、会话结论或发布状态。

所有 CLI/维护命令必须显式使用 `-p datasage-canary-next`；不得依赖默认 Profile、
当前目录或 `HERMES_HOME` 推断。代码冻结、candidate receipt 和 Git tag 只表示
canary 载荷已固定并可审查，不等于 production-ready 或生产安全证明。生产声明还须
满足本节的发布边界和“验收门槛（唯一量化真源）”；owner 仅指 Profile 维护/发布
责任角色，具体责任人和 SLA 由发布记录填写，不在 Profile 文档中虚构。

维护检查只确认 schema/contract、只读执行和宿主兼容性；不改变 WeCom 身份面、
查询边界或 Hermes 的判断权。运行目录中的 `.env`、state、sessions、logs 和
Memory 不由发行或回滚覆盖。
