# DataSage Expert Next 架构基线

版本：0.14.0-alpha2
Hermes：0.20.0

## 目标

将 DataSage 从“由模型搬运治理协议的 BI 查询器”重塑为“由 Hermes 负责理解与推理、由插件负责确定性治理与证据、由按需 Skill 提供分析方法”的企业数字专家。

## 权威设计原则

1. Hermes 官方插件边界：业务集成使用独立插件；`pre_llm_call` 注入会存入 `api_content` sidecar 并在后续会话回放，只用于确需随会话重放的动态上下文。永久规则放在 SOUL/Skill，不用它逐轮重复注入完整系统提示或工作流。
2. Hermes 官方提示分层：SOUL 属于稳定身份层；Skill 属于按需程序知识；运行状态不写进提示词。
3. Hermes 官方工具披露：四个 DataSage 非核心工具采用 eager schema，避免冷工具的 search/describe 往返；企微仍保留标准 Hermes host 工具面。
4. Hermes 官方插件状态：运行时游标、缓存和去重应使用 `ctx.state` 或插件自己的受控状态，不写入 `config.yaml`，也不要求模型记忆哈希协议。
5. 数据代理实践：工具少而清晰，提示指导目标而不是硬编码路径；上下文按需检索；真实端到端评测同时测正确性、延迟、工具错误和最终表达。

官方依据：

- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/developer-guide/prompt-assembly/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-search
- https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills
- https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.3
- https://openai.com/index/inside-our-in-house-data-agent/
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- https://www.anthropic.com/engineering/writing-tools-for-agents

## 分层职责

### SOUL：稳定身份

只保留使命、证据类型、建议权限、因果边界和回答风格。不得包含工具参数、receipt、seal、特殊指标公式或领域 recipe。

### DataSage Skill：按需分析方法

只在需要内部公司事实时加载。提供 Frame → Evidence → Diagnose → Advise 的启发式方法，不规定每题都执行固定调用序列。领域细节通过 catalog/detail/reference 按需获取。

### DataSage Plugin：确定性事实与治理

负责指标目录、实体绑定、查询编译、执行限制、计算、证据完整性、披露去重和渐进输出。模型不应验证哈希、拼装 seal 或记住物理字段。

### Hermes：对话与推理

负责理解用户意图、选择分析路径、综合内部证据与外部/用户前提、排列假设、形成建议以及多轮承接。

## 0.13.1-alpha1 基线

- 新建独立 Profile；旧版不修改。
- 默认复杂分析模型改为 `deepseek-v4-pro`；旧 Profile 可继续承担 Flash 查数流量。
- 四个 DataSage 非核心工具改为 eager schema，消除主要 tool-search/describe 税；`hermes-wecom` 的标准 host 工具面保持不变。
- 移除插件注册的完整 Skill 注入；改为 Hermes 原生按需 Skill。
- SOUL 与主 Skill 重写为短、高层、目标导向版本。
- 禁用重复/冲突的 companion Skill，并将无关的创意、开发、智能家居等 Hermes 播种技能从专家索引中排除；保留 DataSage、办公文档、引用与 Humanizer 等少量能力。技能文件仍保留，可随时恢复。
- `analysis_intent` 正式加入公开 schema。
- snapshot change decomposition 的 schema 与运行时对齐。
- reference 请求改为 source/section 判别联合，阻止非法笛卡尔组合。
- 每个 metric detail 返回独立 `detail_receipt`；迁移期兼容旧单详情 `content_hash`。
- 排名不再静默封顶 10；返回 requested/effective limit 和 has_more。
- 内部结果预算与最终 wire 对齐；批量超限优先返回完整结果前缀，而不是整包清空。
- catalog 默认返回紧凑模型投影；`full/audit` 显式保留兼容全量视图。
- 新增受治理的 `performance_scorecard` 规划视图，一次返回增长、回款、目标、库存周转与风险所需的独立指标详情和 receipt，并明确声明缺少利润指标。
- query wire 对 claim、seal、披露做去重，在保留事实、状态、Top-N 元数据、范围、限制、协调证明和计算结果的前提下降低回包负担。
- 新增回答证据边界校验：阻止把 Top-N 写成总体、无治理基准的健康/可控判断、无协调分解的结构贡献、未证明可比的跨指标节奏和任何未经授权的机制因果。
- 回答门禁按 request 的指标 label/ref 绑定约束；工具证据解析失败时保守拦截。
- 运行时 Memory 仅保留稳定用户偏好，不再保存 receipt、工具步骤、会话实体、测试事实、版本路径或账户标识。

## 0.14.0-alpha1 已实施

- 修复 `performance_scorecard` 在 Hermes 实际注册 handler 上被 entitlement 无条件拒绝的问题；只有具备完整跨域指标权限的主体可调用。
- scorecard 收敛为一套可执行 `recommended_bundle`；八个请求模板各自携带合法 `request_id` 与对应 `detail_receipt`，不再与 recipe/detail 重复。
- catalog 与执行层共享精确 `comparison_kinds`；current snapshot 和分析型指标不再宣称运行时无法执行的通用比较。
- 比例指标跨期比较保留未定义值为 `NULL`；未来目标在已有目标行时保留目标状态；正式 DSO 不再静默删除快照不完整分组，而是返回 typed undefined。
- compact-v2 E2E 适配器按 wire 版本解析 evidence item，同时保留 raw-v1 兼容并对未知版本失败关闭。
- 回答门禁保留 Markdown 与段落结构，补齐已复现的规范性、因果、Top-N 和跨指标漏判；指标命中的“整体增长”等表述只绑定相应 request，独立全局断言仍按整批证据校验，普通非业务因果句不受影响。
- 宽泛经营审视先走 scorecard，`expert_index` 仅处理非 scorecard 的未知指标或 scorecard 后明确的重大缺口；展示级基础运算与 governed calculation 的边界已统一。
- Humanizer 在本数字专家 profile 中禁用；Tool Search 保持关闭，未新增 Router、Skill、指标、数据源或业务解释规则。

## 0.14.0-alpha2 已实施

- 自动生成的完整变化分解只返回有完整分区证明支撑的前 20 行，并保留截断、尾部汇总和非完整人口声明；普通查询与其他分解不受影响。
- 查询完成后的容量或内部错误保留已确认的数据库来源证据；缺失或无效证据不再误报成数据库身份变化，真实的多来源身份漂移仍失败关闭。
- 未调整单结果、批量或最终工具回包上限，未修改 Hermes core、数据库配置或企微配置。

### 回答门禁的精确边界

- 对当前本地企微通道，`transform_llm_output` 在最终发送前替换不安全文本；在插件已加载、证据已捕获且非代理流式发送的前提下，这是交付层门禁。
- Hermes 0.20.0 会在 transform 前持久化原始 assistant draft，因此该插件不能改写 canonical `state.db` 历史。下一轮 `pre_llm_call` 注入短纠偏文本作为多轮补偿；该纠偏会随用户轮次持久化，不是一次性上下文，也不是历史重写。
- 进程在工具返回与最终转换之间重启、进程内证据状态被淘汰，或未来切换为不可编辑的代理流式发送时，不能宣称 fail-closed。若要解决 canonical 历史，需要 Hermes core 提供“持久化前 transform”或受支持的 assistant-row replacement API。

## 当前验证状态

- `python -B -m unittest discover -s tests -p 'test_*.py'`：124/124 通过（2026-08-24 隔离候选验证）。
- 108/108 个目录可用指标完成离线 request normalization、detail gate、pre-entity validation 与 SQL 静态编译；未执行 SQL。
- golden expert suite 从 31 例增至 35 例，新增 scorecard 首路由、比例未定义、分组正式 DSO 未定义和未来 target-only 边界；`validate_suite` 无错误。
- 0.13.1 目标 runtime 的整改前 Hermes 官方 `prompt-size --platform wecom --json` 实测基线：系统提示 14,648 字符，较旧版 16,770 字符降低 12.7%；Skill 索引为 8 项、1,000 字符。完整 Skill 正文按需加载，不计入该索引数字。本候选禁用 Humanizer 后必须在部署时重测，不把该基线冒充候选现值。
- 同次基线实测的模型可见工具为 21 个、70,934 bytes：其中四个 DataSage 非核心工具为 28,796 bytes，其余 17 个 Hermes core/host 工具约 42,138 bytes。当前 `tool_search: off` 让它们全部 eager；即使启用 Tool Search，也只有非核心 DataSage 部分可延迟。这是用固定 schema 换取更少的 `tool_search/tool_describe` 往返，不是“总 token 一定下降”；保持关闭，必须由真实 A/B 验证其延迟与成本收益。
- 隔离候选源目录的离线 `prompt-size` 为系统提示 13,011 chars、Skill 索引 1 项/127 chars、工具 21 个/71,036 bytes；源目录不含运行目标保留的非 owned 办公 Skills，因此该结果仅证明源包可组装，不能替代部署后的新会话测量。
- 本轮未查询业务数据库、未修改 `.env` 或企微凭据；能力通过离线合同、紧凑投影和对抗规则测试验证，真实答案质量仍需用户在 canary 企微入口验收。

## 后续必须完成

### P0：在进入真实流量前

- [x] 为新 receipt、reference union、Top-N 元数据、partial wire 增加架构回归测试。
- [x] 将受影响的旧 prompt-specific 测试替换为架构不变量与运行时合同测试，没有删除质量门槛。
- [ ] 为 receipt、reference union 和 partial wire 补充随机/属性测试。
- [ ] 建立 50–100 条真实业务问题的 Hermes E2E 基线，每题至少 3 次 trial。
- [ ] 硬门槛：工具执行后无最终答复为 0；不可恢复参数错误为 0；P90 小于 60 秒；成功 catalog 重放为 0。
- [ ] 用相同题库 A/B `deepseek-v4-flash` 与 `deepseek-v4-pro`，按“每个被接受结果的总成本”决定路由。

### P1：专家能力

- 增加带单位检查的受控计算 AST：百分点、CAGR、加权平均、指数、跨期和有限跨指标计算。
- 增加同比、自定义基准、周/季/年、滚动窗口和命名 cohort。
- 增加受控分布、分位数、异常扫描、透视和安全 drill-through 原语。
- 把 disclosure、格式化和 claim 校验完全下沉；默认只向模型返回高信号业务事实。
- 对独立过滤查询先统一实体预检，再并发执行；不要让一个 filter 串行化整批。
- 使用插件状态保存已选 domain/metric、有效 receipt 和 catalog 指纹，消除追问重放。

### P2：专家闭环

- 引入可验证的机制/实验/对照证据合同，使因果结论不再永久不可达。
- 建立 forecast、scenario、方案比较、行动跟踪和反馈学习能力。
- 增加 10–15 轮长对话与强制压缩测试，验证 period、scope、entity、receipt 和用户修正的保持。
- 对最终文本使用人工校准的 grader，评价结论性、简洁度、行动性、证据一致性和过度模板化。

## 发布规则

本版本只允许发布到 `datasage-canary-next` 隔离 canary。发布必须使用 `distribution_owned` 白名单并保留 `.env`、`state.db`、sessions、logs 与用户 Memory；启动后只验证企微连接，不自动发送消息或查询业务数据库。真实 E2E 验收完成前不得切换更大范围流量。
