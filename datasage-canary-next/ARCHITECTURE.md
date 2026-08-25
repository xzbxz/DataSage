# DataSage Expert 0.15 架构

版本：`0.15.0-rc5`
运行基线：Hermes `0.20.5`

## 唯一目标

增强 Hermes 理解经营问题、选择证据、形成结论和提出行动建议的能力，
而不是用注册表、规则、工作流或答案模板替代 Hermes 的判断。

本版是边界重构，不增加数据库、不修改企微凭据、不引入路由引擎，也不
迁移全部指标到新 DSL。完整冻结标准见 `REFACTOR_CHARTER.md`，实现决策见
`REFACTOR_DESIGN.md`。

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

## 请求与失败域

```text
用户问题
  -> Hermes 自适应选择证据
  -> 公开 schema（由 capability contract 生成字段条件）
  -> 每个 public branch 独立验证
  -> 每个 branch 按 operation 申请物理执行槽
  -> 成功与局部失败共同进入 compact wire
  -> 最终字符预算保留 success-first 完整分支
  -> Hermes 形成答案
```

只有 envelope 无法解析、权限整体拒绝或无法建立可信执行环境时可以整批失败。
某个 branch 的字段、receipt、实体、能力或物理预算错误必须局部化，不能清空
其他有效结果。`complete` 操作成本为两个物理槽；普通操作成本为一个。公开
最多十个 branch，每个 branch 放不进剩余物理预算时仅返回
`EXECUTION_BUDGET_EXCEEDED`。

原始单结果和批结果 byte gate 已删除。行数、单元格长度、查询超时、SQL 只读、
权限和最终 `max_tool_result_chars` 仍保留。压缩发生在最终字符预算之前；超限时
先保留成功证据，再保留局部失败。

## Scorecard 边界

`performance_scorecard` 是经营分析候选镜头的目录视图，不是一份必须照抄的
执行计划。它可以提供增长、目标、回款、库存、风险等可用镜头和各自 receipt，
并披露利润、现金流等缺口；Hermes 根据问题和证据重要性选择最小充分子集。
测试不得锁定固定八项、固定顺序、固定调用数或固定最终答案。

## 已删除的设计

- 冲突的 `datasage-query-patterns` companion Skill；
- test-only `skill_prompt.py` 和自定义 `datasage_reference`/reference registry；
- 手工维护的 bundled Skill 禁用清单，改用 Hermes 官方 `.no-bundled-skills`；
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

Hermes 宿主的上下文压缩顺序不在 Profile 插件控制范围内。本包提供宿主合同
fixture，要求压缩后保留用户纠正、当前 period/scope/entity/metric 和事实/假设
区分；在宿主 E2E 通过前不得宣称该能力已由 Profile 自身修复。

## 发布身份与回滚

`build_release_receipt.py` 根据 `distribution_owned` 的实际文件内容计算 SHA-256；
`distribution.yaml` 中由 Hermes 安装器管理的 `name`、`source`、`installed_at`
会先做显式归一化，因此源码候选和合法安装实例可比较同一内容身份
身份，不再用模型名、绝对 source 路径、安装时间或一次答案哈希冒充版本证明。
receipt 不读取 `.env`、数据库状态、sessions、logs、Memory 或企微凭据。

本候选只能先部署到隔离 canary。回滚方式是恢复上一份经过 receipt 记录的完整
distribution-owned 文件集合；不得覆盖运行目标的 `.env`、`state.db`、sessions、
logs 或用户 Memory。部署、启动、业务数据库查询和企微发消息均不属于本次离线
重构授权。

## 验收门槛

- schema 与逐分支运行时字段合同机械等价；
- mixed valid/invalid batch 保留成功证据；
- complete 扩展超预算只局部失败；
- compact-before-budget 且 success-first；
- scorecard 的指标选择、顺序和调用数可自适应；
- planner/companion/fixed-answer scorer 不在发布路径；
- 发布身份由内容计算；
- 完整离线测试通过，并单独记录宿主 E2E、真实企微交付和三次稳定性测试的
  未验证状态，不能用单元测试替代。

## 当前验证状态

- 当前完整离线运行 `118/118 OK`，耗时 `37.461s`；故障注入测试中的
  synthetic traceback 是预期日志。
- capability/schema-runtime 等价、mixed partial、物理预算局部失败、complete
  内部 ID 隔离、compact-before-budget、success-first subset、coverage 重建、
  candidate scorecard、planner 删除链和内容发布身份均有回归测试。
- 本轮只读复盘了指定 canary 的真实企微会话，比较模型消息与实际回包，并将
  越南、Thai Kim 两条错误回答按消息 ID、工具证据和正文哈希固化为语义
  Golden；未读取业务数据库配置或企微凭据，也未发送企微消息。
- 尚未完成 Hermes 宿主 compaction 集成、rc5 重启后的真实企微复测、业务题
  每题三次稳定性、P50/P90 和成本验收。因此 `0.15.0-rc5` 是可审查的 canary
  候选，不是已获准扩大流量的版本。
