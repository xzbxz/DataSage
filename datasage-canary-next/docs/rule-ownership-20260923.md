# 规则所有权与 SOUL 迁移映射（R11 / R12）

日期：2026-09-23｜依据：综合审查 V2.0 的 R11、R12（C 批离线可执行部分）
性质：维护者文档。它记录每个口径的唯一权威来源、消费者，以及别处是「引用」还是
「摘要」；不新增常驻规则引擎，不替代任何合同，也不作为模型输入顺序的编排。

## 1. 口径所有权行表（R12）

| 口径 | 唯一权威来源 | 主要消费者 | 别处是什么 | 现有验证 |
| --- | --- | --- | --- | --- |
| 单位 | 各域 `contracts/*-semantics.yaml` 的单位声明；公开字段单位见 `schemas.py` 与合同 `result_fields` | catalog/schema 投影、查询构造、结果呈现 | references 只写「以合同单位为准」的提示，不复制单位表 | `test_semantic_inventory`、`test_business_contracts` 单位用例 |
| 币种 | 同上（`currency`/`unit` 声明）；原币/RMB 由合同与请求校验共同限定 | 请求校验、执行、呈现 | references 仅提示币种分组不能替代金额核验 | `test_remediation_null_integrity`、`test_result_identity_currency` |
| 账本 | 账本选择在指标合同（`attribution_mode`、`delivery_scope`、`inventory_scope`）；业务员分摊以 split 账本为独立权威 | 请求校验、catalog 披露、执行 | references 只提示「不得跨账本替代」，不定义账本 | `test_business_contracts`、`test_remediation_delivery_scope_contract` |
| 时间 | `query-policy.yaml` 与合同期间/比较声明；`request_contract.py` 展开左闭右开窗口 | 校验、执行、证据披露 | references 提示「本月不等于合同时间字段」 | `test_business_contracts` 期间用例、`test_delivery_l3_entity_time` |
| 粒度 | 合同 `grouping`/维度声明；`capability_contract.effective_dimension_definitions` | catalog、执行、Top-N 元数据 | references 提示排序/截断不构成全量 | `test_catalog_business_definitions`、`test_p1_projection_population` |
| 实体 | `contracts/entity-registry.yaml` 与 `entities.py`；维护者说明见 `entity-rules-maintainer.md`（非模型输入） | 实体解析、请求校验、执行 | `entity-guidance.md` 只给消歧提示；不创建或覆盖实体映射 | `test_remediation_cross_role_entity`、`test_customer_history` |
| 证据 | 执行层返回的 typed state/范围/单位/截断披露：`result_projection.py`、`evidence.py`；报告另见 `report_evidence.py` | 模型回答、报告、导出 | references 要求「返回证据高于本参考」 | `test_analysis_evidence`、`test_c01_c02_public`、`test_compact_payloads` |
| 输出 | 呈现规则由 `answer-boundary.md`（`datasage.answer-boundary/v1`）与 `SOUL.md` 呈现章节分述；数值形式由既有 renderer 与精度测试锁定 | 模型回答、本地报告 | 两者互为摘要；冲突时以合同数值与返回证据为准 | `test_report_presentation`、`test_repair_xlsx_precision`、`test_answer_ground_truth` |

规则：上表任一行若出现「摘要」与「真源」冲突，一律以真源为准并修正摘要；不得为
叙事方便选择旧版本，也不得把维护者文档当作模型权限来源。

## 2. SOUL 规则迁移映射（R11）

本轮只建立映射，不删减 SOUL 正文。删除条件：该行「消费者验证」完成后，由维护负责人
逐条移除，且不影响既有守卫。

| SOUL 区块 | 权威来源 | 别处是 | 消费者 | 消费者验证 | 处置 |
| --- | --- | --- | --- | --- | --- |
| Mission（最小充分取证、达到粒度即停、不查系统时钟） | 属 SOUL 自身职责，无业务真源 | — | 每轮模型行为 | 需真实回放（未做） | 保留：稳定认知原则 |
| 范围与成熟度（七域、L3、不声明 L4/cron） | `ARCHITECTURE.md`「定位、用户与升级线」 | SOUL 是面向模型的摘要 | 模型、维护者 | 文档一致性检查（本轮人工） | 保留摘要，不复制阈值 |
| WeCom 全员共享查询面、SQL SELECT-only | `config.yaml` 平台工具集＋插件能力＋`db_security.py` | SOUL 是摘要 | 模型、运行管理员 | `test_integration_boundaries`、`test_remediation_security_runtime` | 保留；不得与权限真源冲突 |
| 建议三档风险与升级线 | `ARCHITECTURE.md` 同段 | SOUL 是摘要 | 模型 | `test_answer_ground_truth`（因果边界、人工评审） | 保留摘要 |
| 最高事实边界（插件拥有指标/权限/typed state） | 指标合同＋`result_projection.py`/`evidence.py` | SOUL 是边界声明 | 模型 | `test_module_dependencies`、`test_remediation_contracts` | 保留 |
| 量化门槛只指向 ARCHITECTURE | `ARCHITECTURE.md`「验收门槛（唯一量化真源）」 | SOUL 只引用 | 模型、维护者 | 文档一致性检查 | 保留（已正确引用，不含阈值） |
| 归因/账本/排名/比例/行数/结构贡献段落 | 各域合同的 attribution 与 typed state | SOUL 是跨域通用摘要；域 references 另有局部摘要 | 模型 | `test_business_contracts`、`test_analysis_evidence` | 保留；域内冲突以合同为准 |
| Presentation（发送前核对、复用返回总额、表文一致） | 无数值真源；与 `answer-boundary.md` 互为摘要 | 双向摘要 | 模型 | `test_report_presentation`、`test_answer_ground_truth` | 保留；精简需先确认 answer-boundary 覆盖 |
| 利润默认账本（部门/客户/产品用利润报告账本，指定订单用订单全生命周期） | `profit-semantics.yaml` 默认账本与报告账本 | SOUL 是默认行为摘要 | 模型 | `test_profit_contract` | 保留；与合同默认一致性由测试锁定 |

## 3. 组合与顺序测试（R12）

新增（本轮）：`tests/test_expert_authority_inventory.py`

- 普通 reference 互链允许存在；引用图无环并不能证明语义一致。检查 Rule ID 唯一、目标存在、链接标签与目标声明一致；权威规则相互覆盖另做内容评审。
- 示例不得另立指标真源；代码围栏和普通文本格式均允许。示例的业务含义需引用并符合权威合同。
- 跨域参考必须写出兼容前提（period、ledger、grain、unit、currency、department）并
  明确声明自身不是 planner/固定查询序列。

复用（未新写）：`test_readonly_skill_trace`（真实读取与回执）、
`test_host_compaction_e2e`（压缩后保留最新纠正；仓库记录曾在锁定宿主通过，本轮未独立复验）、
`test_business_contracts`（同一口径跨域一致）。

## 4. 结论边界

- 本表是维护索引，不是模型输入；它不改变任何合同、权限或工具面。
- ID 与链接检查是结构检查；业务语义及模型实际消费仍需真实回放。
- 用户反向诱导与最新纠正的守卫由既有合同校验、压缩 fixture 与真实回放共同证据支撑，
  本表不重复声明其通过。

## 2026-09-24 修订：素材格式不代替语义审查

本节取代前文“无环、无 SQL／代码围栏即素材级证明”的门禁解释。合法文本、JSON、公式说明和 SQL 示例可用于解释，但必须明确是示例并引用权威合同，不能在方法文档中私自另立可执行口径。普通互链无需逐对批准；链接结构只能验证可追溯性，不能认证读取顺序无关或模型行为一致。

静态检查使用 `tests/reference_authority.py`，验证每份规则 reference 只有一个声明的 Rule ID（SKILL 入口索引不强制新增 ID，围栏内的示例声明不参与）、全局不重复，以及链接标注的 ID 与目标一致。不解释文档指令、不建立规则图调度器。业务定义一致性、合理示例和跨域组合效果仍由现有合同验证及业务评审确认。
