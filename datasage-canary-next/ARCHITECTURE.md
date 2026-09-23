# DataSage Expert 0.15 架构

版本：`0.15.0-rc14`
运行基线：Hermes `0.21.1`

## 唯一目标

增强 Hermes 理解经营问题、选择证据、形成结论和提出行动建议的能力，
而不是用注册表、规则、工作流或答案模板替代 Hermes 的判断。

公开查询工具保持只读；本地运营适配层另有明确门禁控制的测试表写入、冻结、产物和投递。
不修改企微凭据、不引入路由引擎，也不迁移全部指标到新 DSL。历史冻结标准与实现计划仅供审计，见
`docs/history/REFACTOR_CHARTER.md` 和 `docs/history/REFACTOR_DESIGN.md`；
它们不随运行 Profile 发行，也不是当前操作指令。

## 定位、用户与升级线

本 Profile 面向公司经营负责人及出库、销售、应收、财务、库存和目标
负责人，支持七个受治理经营域：delivery、receipt/collections、receivable、
target、inventory、profit 和 pattern_matching。它提供事实、诊断和证据约束的建议，
不是通用制度/HR问答或公共研究助手。已授权的提醒和报告由独立本地入口执行，
不能由模型的业务结论自动触发；查询、运营适配和真实外发各自有明确边界。
当前候选目标成熟度为 L3 数据专家；本候选版本不声明 L4 主动管理或主动巡检能力。

建议按风险分三档：描述性监测（事实与趋势）、诊断性解释（比较、核对和
明确标注的假设）、高影响建议（授信、客户损失、资源配置或目标承诺）。
高影响建议必须给出假设、负责审批的人、重大风险和复核点；证据不完整、
过期、不可用或歧义无法消除时，升级到相应的指标/域负责人或人工审批人，
不得自动批准、承诺或执行。

WeCom 的原生 Profile toolset 声明 `clarify`、`datasage-query`、`skills`。
宿主保持官方0.21.1基底源码，不依赖自定义工具组或宿主补丁。企微认证与七域查询权限不变；
三个业务工具继续SELECT-only、合同校验和证据约束。官方skills组包含管理入口，写入由
`skills.write_approval: true` 暂存待人工审批，`skills.inline_shell: false` 禁止内联执行；
不把原生审批称为严格只读toolset，不新增工具重注册或loader。

企微不暴露任意 `execute_code`/终端/文件入口。官方基底无法原样实现此前定制的两目录
显式挂载约束，故收窄任意执行而不是降级成本地无限执行。受管查询/计算和既有本地受控
报告导出保留，聊天自由Python/任意文件生成目前不提供。维护CLI工具集保持原有设置。
CMD/VBS由官方生成器维护，与gateway start/restart/run共用本机Profile配置，不运行
refactor-work中的依赖。附加通用执行指引通过官方 `agent.execution_guidance: false` 关闭。
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
自适应分析和解释证据；已启用原生 Skill 读取的会话可选按需加载方法。
使用官方 `skills` 组，不增设只读工具组；管理写入使用官方待审批机制，
并关闭 `skills.inline_shell`。读取会记录原生usage元数据，不等于OS隔离。
WeCom 继续以 SOUL、公开工具 schema 和返回 evidence 为基础，
不以 `skill_view` 或 references 作为查询前置条件。Skill 不拥有指标能力、权限、物理查询或结论
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
SOUL 当前保留原有规则，须在真实渠道消费验证和逐条迁移验收后再精简；动态期间、scope、benchmark
和兼容性只在工具证据中计算一次，wire 只做结构投影。

请求字段和类型校验由 `query_validation.py` 拥有，读取既有 capability、request 和
target-gap 合同；`query_errors.py` 共享错误类型。`tools.py` 保留原校验调用入口，
执行器、观察时点、分支预算和证据处理继续使用现有实现。校验模块不反向导入工具执行或运营模块。

`query_sql.py` 拥有标识符引用、合同允许的聚合表达式与行完整性 SQL 片段，只依赖纯合同和共享错误类型。
`query_builders.py` 拥有普通指标、比较、复合和比例构造及时间/关联规划，不执行实体查询或数据库请求。
`tools.py` 保留调用兼容入口、执行上限和批次协调；构造层不反向导入它或数据库执行器。

`query_execution.py` 组合既有 `db_executor` / `db_runtime` 的连接、只读事务、截止和关闭能力，
拥有 DataSage 错误映射及一致快照适配；不复制底层驱动或权限校验。
`result_projection.py` 负责已形成结果及 calculation 的模型投影与完整性验证，继续调用 `evidence.py` 的封存真源。
`wire.py` 保留最终 JSON 和大小预算。并发槽、冻结观察日期、`_run_one`、批次流程和公开鉴权顺序仍由 `tools.py` 协调。

## 权威与维护职责

Hermes 拥有问题理解、工具选择、推理和最终结论；SOUL、公开工具 schema 与返回 evidence
继续保留当前规则。references 在工具实际可用时按需读取，不增加固定查询步骤。
本地候选接线和真实企微消费分别验收；后者未通过前不据此删减 SOUL。
插件不注册 prompt 副本，也不拥有结论授权。

| 资源 | Owner | 使用方 | 边界 |
| --- | --- | --- | --- |
| `datasage.query-rules/v1` | Profile Skill | Skill 读取会话（可选） | 请求构造提示；可用字段以 live schema/catalog 为准 |
| `datasage.entity-guidance/v1` | Profile Skill | Skill 读取会话（可选） | 实体消歧提示；不能创建或覆盖实体映射 |
| `datasage.answer-boundary/v1` | Profile Skill | Skill 读取会话（可选） | 解释提示；插件不注册 prompt 副本 |
| `datasage.delivery-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 出库 L3 分析提示；不能替代工具证据 |
| `datasage.receipt-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.target-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.inventory-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.pattern-matching-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.profit-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.receivable-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |
| `datasage.cross-domain-analysis/v1` | Profile Skill | Skill 读取会话（可选） | 可选方法；不替代合同或工具证据 |

`*-semantics.yaml`、typed capability contract、实体 registry 和公开工具结果分别
拥有指标定义、执行事实、实体身份和当前证据；`datasage.entity-maintainer-rationale/v1`
（`entity-rules-maintainer.md`）仅供源码维护者使用，不进入发行载荷。`contract_store`
只做受限路径和缓存，不引入规则引擎或运行状态机。

## 专用查询接入与规则归属

`*-semantics.yaml` 维护业务值和来源；相同业务块使用 YAML anchors 共享。精确指标详情从同一合同的公开必需披露投影选择边界，结果阶段仍按实际状态生成完整披露；文档和 SOUL 不另存逐指标口径。

`analytical_handlers.py` 是插件内的静态接入表，不读取用户问题、不选择指标，也不表达 SQL。周/月/历史查询的参数接受集合在这里维护，目录和构造器共用。客户历史已迁移 builder、观察器、显式事实许可、快照要求与时间投影；相应 `tools.py` 和分析分发中的具名分支已删除。

客户历史的 SQL、结果观察、身份展示引用、许可字段及时间展示由 `customer_history.py` 拥有，一条接入记录连接公共执行链。其池来源由合同共享的 `pool_definition` 提供，不通过另一公共指标继承可用性。单位声明不授予字段公开许可，公共事实继续按显式白名单过滤。

普通同构指标仍只需语义合同、必要数据集字段和行为测试。同类特殊形状增加模块内实现、合同与一条接入记录，不再分别添加事实集合、快照、观察器和时间文案中心分支。新增身份角色或新的通用协议仍可能需要相应公共能力变更，不能承诺所有新形状只改一个文件。

其他业务构造器暂沿用现有分发；周/月本轮只共享参数能力，没有迁移其全部 SQL 和结果观察。后续迁移一项时删除被替代的旧分支，不保留并行真源。不新增发布链、万能 DSL 或 provider 框架。

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
利润仅为报表已记录值，核算完成状态未确认；并披露现金流等缺口；Hermes 根据问题和证据重要性选择最小充分子集。
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

Hermes `0.21.1` 在读取配置失败时会让自动 background review fail-open，同时
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

Hermes 官方同步内建 Skill；本 Profile 不修改官方内容。当前 core 为60个，启用办公文件能力 `docx`、`xlsx`、`pdf`、`powerpoint`，同时接受宿主不可禁用的 essential `hermes-agent`。`skills.disabled` 关闭其余当前及遗留入口；Skill 选择与渠道工具面分别配置。当前渠道面是 native `clarify`、三个 DataSage 工具与官方 `skills` 组（该组含管理入口，写入先进入待审批、读取时不执行内联 shell），企微不提供任意代码、终端、文件或公共 Web 工具。更早的候选配套宿主本地装配曾观察到 `execute_code`、`skills_list` 与 `skill_view`；那是锁定官方基底（宿主 0.21.1，基底 `2237be355906fbe6065ce1815711eee52b2d646e`）与撤回定制容器之前的历史观测，不代表当前工具面，真实渠道加载与完整工具枚举仍待实测（见 `docs/maintenance.md` 与 `README.md`）。本机 Python 的权限边界见上文。`ocr-and-documents` 已不属于当前 core，不声明为启用能力。

每次宿主升级做完整快照差异审查；新增、删除或重命名必须更新 `tests/fixtures/reviewed_host_skills.json` 与 denylist，并检查 `metadata.hermes.related_skills`。Profile 显式关闭独立的 `curator.enabled` 和 background review，使调试中的能力集合由 Git 维护。普通 CLI 用于源码维护，业务调用仍受现有 WeCom/trusted replay 授权约束。

## 期间比较合同

流量指标公开支持 `year_over_year + matched_elapsed`。该能力使用一次批次冻结的
`observed_on`，把尚在进行中的当前窗口和上年窗口裁剪到同一历年边界，并在
公开证据中保留请求结束日、有效结束日、裁剪状态和数据新鲜度未证明状态。
只有统一 period compatibility 合同验证为 `compatible` 时，模型 wire 才授权
正式期间比较；两个独立查询加透明算术不能绕过这项授权。

Catalog 的 metric detail 与 domain view 在公开 schema 和运行时都机械互斥，
避免模型得到“schema 可表达、运行时却拒绝”的伪能力。上述合同同样贯通完整
变化分解的 overall 与 partition 分支，并保留既有逐分支 partial-success 语义。

## Git 维护与验收边界

Git commit/tag 负责源码身份和回退，运行时按官方 `plugin.yaml` 加载这个唯一 Profile。不再维护自建发布链、receipt 晋级或 distribution 安装载荷。业务收款（receipt/collections）、coverage receipt、target-gap receipt 和查询 evidence 都属于业务合同，继续保留，不能因名称相似而删除。

测试保留查询、证据、权限、并发、取消和官方宿主集成；纯会话配对验证迁移到 `tests/business_replay.py`。测试结果、会话结论及业务验收成绩不成为模型提示中的事实。离线通过不等于 production-ready，也不证明达到上文“验收门槛（唯一量化真源）”。

Hermes CLI 维护显式使用 `-p datasage-canary-next`。普通 CLI 身份不能直接进行业务查询，不通过环境变量或本机文件可读性推导业务授权。维护不修改官方源码、企微权限、数据库授权或生产模式，不覆盖 `.env`、认证、state、sessions、logs 和 Memory。
