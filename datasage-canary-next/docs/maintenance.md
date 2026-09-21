# DataSage 维护与能力接入

当前维护边界：官方Hermes0.21.1基底2237be3源码不做定制，只在本Profile的Git中维护业务。
先前宿主补丁与Podman特殊启动入口已撤回；下面相关实验记录只作历史证据，不是现行部署要求。

本 Profile 为 **L3 候选**。量化验收只以 [ARCHITECTURE.md](../ARCHITECTURE.md) 的
“验收门槛（唯一量化真源）”为准；离线测试通过不等于业务验收或上线批准。
本文供维护者使用，不是模型处理经营问题时的必读材料。

## 每批变更

记录清单任务 ID、源码/宿主基线、允许写入路径、保留能力、必跑测试和回退范围。
在现有 Git 工作区定位任务、做最小修改并运行相关测试；不为普通整改新建一轮副本或报告。
保留已有改动，只提交归属明确的文件或无冲突的独立改动，不使用整体重置或 `git add -A`。
使用固定的 Hermes 0.21.1 宿主及其 Python；不借 Profile 重构升级或修改宿主。

数据库账户、企微身份和权限、生产模式、服务重启、真实发送/调度、Memory、会话、状态库、
凭据和历史业务产物均需独立明确授权。分析或生成报告不构成投递授权。
缺少真实网关、业务 owner 或数据核验时，记录未验证及所需证据，不能写成通过。

## 新能力放在哪里

| 变更 | 首选入口与真源 | 必须验证 |
| --- | --- | --- |
| 同构指标 | 对应 `*-semantics.yaml`；确需时补 `datasets.yaml` | 独立数值 fixture、粒度/单位/账本、catalog/schema/runtime 一致 |
| 分析方法 | DataSage Skill 的按需 reference | 引用可达、渠道可读、开放题与边界题；不修改查询核心来固定分析顺序 |
| 特殊查询 | 既有专用 builder 与 `analytical_handlers.py` | SQL 参数、公开字段许可、观察时点及局部失败；不增加意图路由 |
| 新业务域 | 先核实数据 owner、指标合同与现有能力边界 | 独立来源与业务验收；不得仅复制域名宣称支持 |
| 报告样式 | 既有 renderer 和 `report_evidence.py` | 复用同一证据，数字/表文/文件一致，生成不发送 |
| 运营任务 | 既有运营入口、规则合同和私有角色配置 | 生成/审批/投递分离，幂等、取消、重试与 owner；不自动启用调度 |
| 宿主升级 | 独立兼容性任务 | 插件注册、Skill 读取/写审批、spillover、压缩、执行和真实渠道验证 |

每个新增入口应记录 owner 角色、来源、消费者与回归用例。尚未获得 owner 确认的
低频/Legacy/验收入口只列候选，不删除。历史材料只用于追溯，不作为当前运行指令。
同构指标沿用既有能力合同；方法保留多种合理分析路径；不新增 Planner、Finalizer、
通用 DSL、发布晋级或权限平台。

## 入口生命周期

下表是稳定的入口分类；当前进程是否已加载、外部任务是否存在以及最近使用时间，
必须由运行环境证据单独确认。

| 类别 | 入口与真源 | 默认生命周期 | 保留策略 |
| --- | --- | --- | --- |
| Public runtime | `plugins/datasage-query/tools.py`、`analytical_handlers.py`、`idk_query.py`；`operations.py` 保留兼容 facade | 宿主加载后可用；不由本表推断网关已加载 | 保留查询入口、合同和兼容 seam；结构移动需做 baseline 对照 |
| Operator report | `scripts/datasage_slow_report.py`、`local_report.py`、`local-report-bindings.json` | operator-only；绑定文件未随源码交付 | 保留无绑定时 fail-closed、预览和显式本地接受；不因脚本存在而注册任务 |
| Legacy compatibility | `scripts/datasage_legacy_*.py`、`legacy_workflow.py`、`LEGACY_WORKFLOWS.md`、`contracts/legacy-workflows.json` | migration/acceptance；动作 gate 默认关闭 | 保留固定 report ID、预览、历史回执和恢复路径；停用须有 owner、替代路径和恢复步骤 |
| Bounded live acceptance | `scripts/datasage_live_*.py`、`workflow_live_runner.py`、`workflow_schedule.py` | acceptance-only；窗口和 schedule gate 默认关闭 | 保留有界准备/投递/恢复代码；外部作业未核验前不称 active cron |
| One-time acceptance | `datasage_weekly_acceptance.py`、`datasage_reminder_acceptance.py`、`datasage_region_acceptance.py` 及对应模块 | acceptance-only；周期/目标固定且会过期 | 保留离线复现、证据和回执；周期延续需重新参数化和 owner 确认，不按名称删除 |
| Evidence and delivery | `report_evidence.py`、`workflow_delivery_review.py`、`acceptance_delivery.py`、`wecom_app_transport.py` | 生成、审批、投递分离；发送需显式 gate | 保留 typed receipt、binding、局部失败和 unknown；生成报告不隐含发送授权 |
| Audit export and history | `scripts/datasage_source_export.py`、`docs/history/`、Git-ignored `report_runs/` | audit/archive；不是普通查询运行入口 | 保留显式白名单导出和历史证据；分享、归档和清理另行审批 |

### 新能力接入最小清单

| 能力 | 必须先登记 | 真源与验证 |
| --- | --- | --- |
| 新指标 | owner 角色、粒度、单位、账本、公开维度 | 对应 `*-semantics.yaml`、catalog/schema/runtime、独立数值 fixture |
| 新分析方法 | owner、开放题/边界题、消费 reference | Skill reference；不把方法改成固定查询流程 |
| 新运营任务 | 输入源、执行身份、报告 ID、状态/回执键、恢复路径 | `operations.yaml`、`legacy-workflows.json` 或已有 workflow contract；幂等/取消/局部重试测试 |
| 新调度 | 时区、周期边界、窗口、目标、启停和回滚 owner | 已验证宿主/外部调度配置；代码中的候选 cron 不算注册证据 |
| 新输出 | 证据绑定、精度/完整性检查、发送权限 | 既有 renderer 与 `report_evidence.py`；生成和发送分别验收 |
| 宿主/环境变化 | 版本、扩展点、失败回退和回滚步骤 | 独立宿主兼容性记录；不在 Profile 复制宿主运行时 |

## 测试与回退

### 原清单 B06 / D02 的能力与字段边界

下表记录已有能力及仍缺的验证，不据此启用任何工具。skills-only 合成对话不能代表
完整生产工具面；没有质量非劣化和成本对照时，继续保留现有接口与配置。

| 业务能力 | 最小既有入口 | 已有证据 / 剩余条件 |
| --- | --- | --- |
| 七域内部事实与实体 | `datasage_catalog`、`datasage_entity_resolve`、`datasage_query` | 合同/隔离回归已有；生产独立SQL、权限和真实渠道仍暂缓 |
| 受管派生计算 | `datasage_query.calculations`、既有比较/分解 | 受管标量的差/比/份额校验已有；不能把任意用户表格或缺失范围塞入该接口 |
| 按需业务方法 | `skills_list`、`skill_view` | 原生实际读取与合成连续更正已观察；不开放写Skill，不替代业务GT |
| 文件读取与交付 | 已审批输入、原生文件取回、现有writer | X03有合成隔离证据；真实渠道文件/会话隔离与交付仍待验，不扩大任意目录权限 |
| 图表与报告 | 既有renderer、证据绑定与受控导出 | 沿用已实现产物链；自由图表能力的实际工具面/质量AB未完成，生成不隐含发送 |
| 公共研究、普通问答与用户资料 | 普通回答及会话已暴露的公共/文件工具 | 不强制内部查询；本轮未开放或验收公共网络工具，需独立质量/范围对照 |
| 连续更正 | 原生会话历史与现有工具回执 | 单位和找版合成多轮有真实模型证据；企微身份/长会话仍暂缓 |

字段归属按现役 `plugins/datasage-query/schemas.py` 记录如下；这只是维护索引，
不是新请求合同。类型、互斥关系、默认值仍由该文件及 `request_contract.py` /
`capability_contract.py` 拥有。模型选择业务语义，程序展开和校验，不静默替用户选账本或币种。

| 字段（子字段同属其父项，例外单列） | 归属 | 保留/默认边界 |
| --- | --- | --- |
| `requests`、`calculations` | 用户语义驱动的组批 | 由模型选择必要证据与运算；程序执行数量上限和独立失败隔离 |
| `request_id`、`calculation_id` | 程序可确定的关联标识 | 当前仍由调用方提供稳定ID；仅用于关联，不作为业务口径，不在未验证前删字段 |
| `domain`、`metric`、`dimensions`、`metric_filters` | 用户语义选择 | 按注册合同解析；不能用物理表、SQL或任意字段替代 |
| `attribution_mode`、`delivery_scope`、`inventory_scope` | 用户语义选择 | 保留账本/净毛/库存范围选择；省略时仅使用已登记默认，禁止自创默认 |
| `pattern_time_basis`、`baseline_week`、`movement_state` | 用户语义选择 | 时间口径/历史基线/状态必须按能力支持；现有可省略默认由合同处理 |
| `time_range`、`calendar_month`、`time_bucket` | 用户语义选择，程序展开 | 月份确定展开为左闭右开日期；互斥、合法期间和粒度由程序校验，不从时间字段擅自选口径 |
| `comparison.kind`、`comparison.months` | 用户语义选择 | 保留比较方式与月份偏移；程序计算合法对齐窗口 |
| `comparison.coverage` | 程序约束的口径参数 | matched-elapsed同比的固定覆盖要求由合同约束，不能当可省略的质量说明 |
| `decomposition_of_request_id`、`left_request_id`、`right_request_id` | 语义关系选择后的程序关联 | 必须指向同批真实请求；程序验证同范围、单位、期间及分区关系 |
| `complete_change_decomposition`、`complete_target_gap_decomposition` | 用户语义操作，程序展开 | 模型选择维度/方向；程序展开完整分区与总体核验，不把计划操作等同成功对账 |
| `period_summary.field`、`period_summary.periods`、`operation` | 用户语义选择 | 保留字段/月份/差比份额选择；程序检查可加性与兼容性，不接受自由公式 |
| `order_by`、`limit` | 用户输出选择，程序限额 | 排序不改变总体，截断不构成全量；环境上限可进一步收紧 |
| catalog 的 `domain`、`metric`、`view` | 模型发现范围 / 明确审查选择 | view=full/audit是已有兼容审查入口，不据名称移除；日常用既有紧凑索引 |
| entity 的 `token`、`entity_types`、`domain`、`metric`、`attribution_mode`、`limit` | 用户实体/角色语义，程序候选搜索 | 搜索范围不是人工确认；保留歧义澄清，候选上限按注册默认/硬限额 |
| 物理表列、join、SQL、执行快照、内部诊断 | 仅内部拥有，非公共请求字段 | 不加入模型参数；公开证据仍保留必要范围/状态/限制与追溯信息 |

本地归属清单与能力矩阵可交付，但 B06 的质量/开销AB、D02 的首次调用正确率及真实
token/无效调用改善尚未闭环，两个整项继续待验；没有据此删除参数或恢复通用终端。

D03 已按“保留当前投影，不继续压缩”收口。查询职责抽取后，`result_projection.py` 保留
typed state、单位/范围/账本、截断/缺失披露和分支证据；既有安装回归（1317通过、2环境跳过）中，
[`test_c01_c02_public.py`](../tests/test_c01_c02_public.py) 验证失败分支不污染其他分支和部分结果保留，
[`test_compact_payloads.py`](../tests/test_compact_payloads.py) 验证大结果完整进入原生spillover，
[`test_analysis_evidence.py`](../tests/test_analysis_evidence.py) 与
[`test_business_contracts.py`](../tests/test_business_contracts.py) 验证截断、未定义值及限制披露。
这些证据支持本次投影兼容结论，不证明真实模型费用下降；完整成本与质量对照仍由D01/H05单独记录。

本轮受限模型实验已完成40次授权请求（20个合成案例、两组单轮提示），均正常结束；
参考材料组在若干口径边界上更稳健，但总token为106,552，对照组为57,188，未证明降本。
因此保持方法按需读取，不把参考材料常驻到每次请求，也不调整Tool Search或推理配置。
诊断发现的 `tao` 释义及找版冲突明细问题已按既有合同补入对应reference；定向合同测试与
原生读取通过，但没有追加模型调用复测。原输入/结果保持冻结；这些合成诊断不构成独立GT、
holdout、真实Skill消费或连续多轮验收，也不提升当前L3候选的批准状态。

一致性复核确认单位方法与合同/B03一致；找版方法进一步区分已知来源币种与未核验金额。
币种分组本身不证明金额通过：扩展原冲突用例后，按任务/币种分组仍全部保留未知总额，
已知部分合计50、币种去重已知部分50，冲突明细未被任选计入。该离线用例通过；未改SQL。

后续原生合成验证另获批最多8次请求，实际使用7次、44,958 token。两份reference均由模型
实际调用 `skill_view` 读取，工具回执进入下一次请求。单位案例使用3次请求完成两轮，
第二轮真实承接历史，按更正仅报5 tao并拒绝解释为5套。找版案例使用4次请求：额外读取
主Skill后请求了未暴露的terminal，宿主返回工具不存在，未执行命令；随后迭代上限触发
强制总结，返回 `completed=false`，未发送第二轮更正。不挪用另一段剩余1次额度。
找版总结保留总额未知/已知50，但自行提出140～150候选总额区间；已补充“冲突候选不构成
未知金额上下界”的方法边界，该后续文案没有再次模型复测。这里只证明单位定向两轮及
两份原生读取，找版连续更正仍未验；不替代独立业务GT、holdout或真实企微验收。

找版terminal请求的离线定位发现宿主提示矛盾：真实请求的schema仅有两个Skill工具，
通用执行指引却要求算术必须调用terminal/execute_code；SOUL原文只要求可用计算工具，
方法reference没有要求terminal。宿主提交 `6eda1a1e5` 按会话已暴露工具过滤该指引，
71项定向测试通过，未开放工具或修改生产权限。该skills-only实验仅覆盖原生方法读取、
回答与会话历史，不能用于认定生产catalog/entity/query或计算链路故障。
下一仅找版两轮方案不指定工具或reference路径，拟共用12次请求硬上限；续轮、探测、
重试尝试和强制总结均计入。实际宿主离线5次假请求完成两轮，7项预算/历史检查通过；
尚无新增真实外发授权，本轮真实请求0，旧实验账本与额度不挪用。

该12次方案后经用户明确批准，实际用4次真实请求、30,037 token完成找版两轮，未使用
余下8次额度。模型自行读取主Skill及找版reference，成功回执进入后续请求，第二轮
真实承接第一轮答复/工具历史；无terminal调用或强制总结。首轮保留总额未知/已知50且
不推导上下界，更正后按稳定明细去重为170，数值与历史检查通过。
答案仍有解释问题：把指标概括成只依赖身份/币种，遗漏销售侧金额相等及其他匹配校验；
还将未提供的未关联总体描述为空。因此不宣称整体质量验收通过。已按现有实现补充方法
中的关联校验前提，并用“关联记录一致为20但销售侧30”的SQLite反例验证仍为未知、
另一明细已知50保留；该用例和原冲突用例均通过。SQL未改，事后文案未新增模型复测。

用户随后明确要求在同一12次总额度内自主用剩余量完成复验。运行器绑定前4次原始记录，
发送前扣除已用额度，8项预算检查通过；未重开12次。输入补齐销售侧/关联侧的金额、
人员、产品与时间字段，并限定为所列记录。真实复验新增5次、43,468 token，两轮完成；
该额度累计9/12次、73,505 token，余3次未使用。模型首次选错Skill名后收到失败回执，
自行读主Skill及reference；这是工具选择失误，不是HTTP重试。实际回执与6162b89一致。
独立复核确认：冲突时总额未知/已知50，更正后170；解释保留销售金额相等、人员/产品/
时序校验，不把未提供总体断言为空；原生读取和真实历史承接成立，无terminal/强制总结。
剩余答案级不足是将4条任务行误写成“三条”以及自行扩展观察时点/完成日期口径；未发现
新的代码缺陷，不以重复提示替代完整质量验收。局部复验不代表生产查询链或独立业务GT。

当前可执行代码修复、定向本地验证和上述索引已完成。清单整项仍保留原验收门槛：
B04/B05/G01/G04缺完整业务覆盖与独立评审；B06/D01/D02/D05/H03/H05缺受控工具/接口/
配置对照和完整成本链路；H02/H04缺独立真值、holdout与业务签字。数据库、企微、Gateway
及真实恢复/发送等14项仍按用户要求暂缓，X05另需管理员核实旧包暴露史并决定处置。
不因完成本地工作就把这些条件改记为通过，也不自动开放权限、启动服务或处置旧材料。

### 官方宿主与原生启动

宿主受跟踪源码必须与官方基底 `2237be355906fbe6065ce1815711eee52b2d646e` 的树一致。
先前五个定制提交通过定向revert撤回，Git历史和实验原始记录保留；不要把它们重新应用为
Profile的安装前置条件，也不要用Profile monkey patch、shadow工具或复制宿主代码替代。

唯一生效配置是本机Profile的 `config.yaml`。企微原生工具集为
`clarify`、`datasage-query`、`skills`；官方Skill写审批保持开启、inline shell关闭。
官方skills组包含管理入口，写入先暂存待人工批准；没有定制的严格只读工具组。
本Profile用官方 `agent.execution_guidance: false` 关闭与受限工具面冲突的附加执行指引。
CLI维持其原有工具集，不能据此推定企微有相同权限。

原有CMD/VBS由官方 `hermes_cli.gateway_windows` 生成器管理，与原生
`hermes --profile datasage-canary-next gateway start/restart/run` 共用配置；不注入
TERMINAL环境、不自动启动Podman、不依赖refactor-work。机器路径/凭据配置与生成入口
继续Git忽略，保留原字节备份；不要把秘密或机器启动参数写入版本化源码。

当前不向企微提供任意代码/终端/文件执行，因为官方基底不能保持原定制容器的显式挂载
限制。受管查询/计算与现有本地受控报告导出保留；自由Python/任意文件生成没有被伪装成
已支持。历史容器与已保存材料不删除，Podman不再是此Profile运行前置条件。
原生配置损坏/导入失败时的行为以官方实现为准，不再承诺已撤回补丁提供的额外保护。

在仅含源码和合成数据的临时 Profile 目录使用宿主 Python：

```text
python -B -m unittest discover -s tests -p "test_*.py" -v
```

测试结果分别报告 pass/fail/skipped/blocked。宿主不可用、可选依赖缺失和真实渠道缺证据
不能算通过。数字 Ground Truth 必须独立于被测 builder；不改预期、删断言或降门槛来掩盖回归。
模型质量和端到端性能只按受控回放判定，不从工具数量或字符数推断。

结构移动和业务口径修复分开提交。以 Git diff 和提交记录追踪变更；沿用一份任务状态清单，
记录完成、待做、用户暂缓及必要验证。局部回退本次提交时保留用户先前改动；
不覆盖用户状态库，不声称已发送消息可以随代码回滚撤回。
