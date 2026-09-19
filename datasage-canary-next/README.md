# DataSage Canary Next

这是本机 `datasage-canary-next` Profile 的源码目录。本轮结构收敛代码已安装，
角色配置已按授权完成等价格式迁移；网关未重载，历史验收记录不代表当前进程已加载新代码。
维护方式是在现有 Git 工作区和活动分支上原地修改、测试、审查与提交；
不创建第二个安装实例，也不使用 `hermes profile install/update` 覆盖这个目录。

`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据属于用户态，
不得提交，也不得由发行更新覆盖。企微和数据库权限不属于发行流程的修改范围。

当前候选版本：`0.15.0-rc14`。

## 定位与使用边界

DataSage 面向公司经营负责人及出库、销售、应收、财务、库存和目标负责人，
覆盖 delivery、receipt/collections、receivable、target、inventory、profit、pattern_matching
七个经营域。它提供受治理证据的事实、诊断和分级建议，不是
公共研究、普通写作、用户文件分析器，也不批准或执行业务决策。
当前候选目标成熟度为 L3 数据专家；本候选版本不声明 L4 主动管理或主动巡检能力。

WeCom 入口声明 `clarify`、`datasage-query` 和官方 `code_execution` 三个 toolset，所有已认证企微成员
均可私聊和群聊，并共享七个经营域同一完整的 DataSage 查询面。Profile 不施加
用户、群组、部门、实体、行或领域过滤；这不替代外部数据库授权。数据库执行仍为
SELECT-only，并由治理查询合同、只读执行限制和 evidence 边界约束。

官方 `execute_code` 用于处理已取得的业务结果，沿用 Hermes 原生执行和审批机制，
不另建恢复工具或分页协议。当前本机 Python 可以读取和修改进程权限内的文件；
这不是仅限查询结果的文件隔离环境。DataSage 查询工具的 SELECT-only 约束不限制
Python 的文件访问。原生审批是否再次提示取决于已有审批状态，当前配置也未将审批
限定为机器所有者。

业务员个人目标、分摊实际和完成率使用 `salesperson_allocation` 的 split 账本作为
独立权威来源；transaction-detail 基表仅用于诊断，不决定销售分摊覆盖，也不得在
split 缺失时回填、混入或替代个人业绩口径。

本候选也支持 salesperson→customer 的 split 普通下钻；净收款登记额按收款拆分登记额
减退款拆分登记额解释，不代表实结或到账。期间或分解证据不完整时必须披露限制，因果
分解不作无证据推断；canary live replay 仍 pending。企微全员私聊/群聊与数据库业务
权限保持不变。

rc14 落实本轮出库事实口径：8 项数量指标恢复查询，必须按单位分组或限定单一单位，
`y`、`Pcs/PCS`、`m2/㎡` 分别解释为码、件、平方米且禁止跨单位合计；默认部门固定为
`customer_dept`，`biz_dept`/`biz_region` 作为独立业务发生归属；退货 `biz_org` 与
销售/出库 `org_name` 经业务 owner 确认为同一组织口径，各事实仍独立聚合；条码
`final_supplier` 仅开放于物理毛出库。历史原始码 `tao` 暂只按原码单列，不擅自解释。
企微全员私聊/群聊和数据库业务权限策略保持不变；正式 L3 live replay 与 150 案例门槛
仍 pending。

建议分为三档：描述性监测、诊断性解释、高影响建议。高影响建议必须带有
假设、负责审批的人、重大风险和复核点；证据歧义、不可用、过期或不完整时，
升级给相应指标/域负责人或人工审批人。

## 当前 Git 原地维护与 Hermes 原生能力

当前宿主为官方稳定版 `0.21.1`。维护只在当前 Git Profile 内进行：修改、离线测试、审查、提交，再通过官方 Hermes 重新加载。Git commit/tag 是版本与回退依据；不再维护 distribution manifest、自建发布 receipt、晋级脚本或第二个安装实例。运行时继续保留 `plugin.yaml` 等官方加载元数据。

### 角色配置与激活边界

工作流唯一活动的私有角色配置是 Profile 下 Git 忽略的
`local/workflow-roles.json`。非秘密的区域与部门规则只来自
`plugins/datasage-query/contracts/legacy-workflows.json`；执行人、管理人员、固定价格管理人员和其他私有目标不在公共合同中重复保存。
`workflow_roles.load()` 只读取活动路径，缺失、版本错误或口径不一致会直接返回
`ROLE_CONFIGURATION_REQUIRED` 或其他稳定阻断码，不探测 `report_runs`，也不从旧文件隐式回退。

旧 `report_runs/reminder_acceptance/legacy-recipient-reference.json` 只能作为一次明确的迁移来源。
迁移前用 `roles-check --source <source>` 做只读预检，再用同时指定
`--source` 和 `--output` 的 `roles-import` 导入；目标必须位于 Profile 的 `local` 目录内，
已有目标不覆盖，CLI 不提供覆盖选项。`local-report-bindings.json` 中遗留的
`recipients_file=legacy-recipients.json` 只是兼容标记，读取仍固定走
`local/workflow-roles.json`，不会读取同名文件。

角色命令均通过固定入口调用（`--mode` 是入口所需参数，角色动作自身不连库）：

```text
python <profile>/scripts/datasage_workflow.py --mode test roles-check
python <profile>/scripts/datasage_workflow.py --mode test roles-check --source <source>
python <profile>/scripts/datasage_workflow.py --mode test roles-import --source <source> --output <profile>/local/workflow-roles.json
python <profile>/scripts/datasage_workflow.py --mode test diagnose
```

无 `--source` 的 `roles-check` 检查活动角色配置；带 `--source` 时只做显式来源的
`prepare` 预检。`diagnose` 只验证当前磁盘 Profile 与当前 CLI 进程的运行环境，不替代角色检查；
正在运行的 gateway 版本是 `not_observed`。命令输出只含状态、哈希、计数和错误码，
不打印人员值。本轮代码已安装，角色配置已按授权完成等价格式迁移，网关未重载；SQL SHA2 实体回查在真实数据库的扫描成本和延迟尚未测量。

内建 Skill 由 Hermes 官方同步。当前启用 `docx`、`xlsx`、`pdf`、`powerpoint`，并接受宿主必需的 `hermes-agent`；其他当前与遗留内建入口通过 `skills.disabled` 关闭。`ocr-and-documents` 已不在当前 core 集合，不再声明为可用原生能力。升级时检查 `tests/fixtures/reviewed_host_skills.json` 的快照差异和 `related_skills`，再有选择地更新。调试期间显式关闭 `curator.enabled`，避免自动改变能力集合；background review 也保持关闭。

普通 CLI 可以编辑和测试源码，但普通 CLI 身份不能直接调用业务查询；业务入口仍要求已绑定的 WeCom 身份或合法 trusted replay。工具可见性不等于业务授权，本次没有扩展任何权限。

所有 Hermes 维护命令显式使用 `-p datasage-canary-next`。离线检查使用现有官方宿主 Python，在本目录运行 `python -B -m unittest discover -s tests -p "test_*.py" -v`。测试保留 SQL、数值完整性、证据、授权、并发、时限、宿主装配和会话配对验证，不再验证已删除的发布系统。

`tests/business_replay.py` 只解析已有会话导出并验证工具配对和结束边界，不读取会话数据库、不调用模型、不发送消息，也不授予上线资格。真实业务验收独立于源码回归；量化目标仍见 `ARCHITECTURE.md`，不得把计划门槛写成已达到的成绩。

统一 `acceptance_delivery` 路径产生的新投递回执使用 `datasage-delivery-binding/v1`，把业务周期/代次/阶段、范围、最终目标、正文与附件内容、组件集合和 progress scope 绑定在一起；只有 `verified_for_reuse` 才能抑制一次新的发送。没有新 binding 的历史 `provider_accepted` 只标为 `historical_provider_accepted`，不能自动复用。旧验收记录中的 10/9 计数不能提升为新 binding 的通过。

`report_evidence` 继续负责报表业务证据的来源、明细与汇总对账；共享
`result_completeness` 只负责原始查询包的状态、覆盖、截断、观察时点和完整报告门槛。
两者分别回答业务数值是否对账、投递所需来源是否完整；一个通过不能替代另一个。

LSP 使用 Profile 中官方 npm 的 Windows `.cmd` 入口。Git 操作和维护不得覆盖 `.env`、认证、state、sessions、logs、Memory 或更改企微与数据库权限。

DataSage 当前没有有 owner 的定时任务，因此不向 Hermes `cron` toolset 暴露
`datasage-query`，本候选版本不声明 L4 主动管理能力。未来新增主动巡检时，必须同时定义任务 owner、调度身份与权限、
超时/重试和端到端测试，不能只恢复配置项。


## 利润报表能力

利润域复用现有三个 DataSage 工具与官方执行链。70项指标分别属于四种账本，默认按问题主体选择相应月度报表；用户明确指定口径时保留该选择。没有核算完成标识，统一按“截至查询时已记录的利润”解释。

| 默认场景 | 口径与维度 | 边界 |
|---|---|---|
| 部门某月利润 | 部门月度；部门、组织、单据类型、次品、备货、中国订单等 | 不提供客户/业务员/产品拆分，不用客户利润按部门汇总代替 |
| 客户某月利润 | 客户月度；客户、部门、组织、业务员、地区和报表标签 | 不提供产品/订单拆分；缺失业务员归属保留未知 |
| 产品某月利润 | 产品月度；产品、归属部门、组织、供应商、产品地区、印花和业务标签 | 不提供客户/业务员/订单或内部客户拆分；不是出库单生命周期按产品分组 |
| 某笔订单/某批出库利润 | 按原出库日期选单的生命周期；订单、产品、客户、部门、组织、记录业务员名称等 | 历史期间可能因后续分配/退货更新；业务员只有记录名称 |

费用空值表示没有分配到，不等于最终没有费用。完整费用未知时单列已分配金额、未分配记录数和覆盖率；全部未分配时保持未分配，不自行按0重算利润。客户提成优先使用提成表，缺记录时按比例补算；部门使用提成表，产品等待提成表生成分配，不搬用客户补算。不存在结账标识，因此不能宣称已结账或最终利润。

月度报表汇付成本按各自记录的正收入计提1%，非正收入计零，再汇总存值；不以最终净收入合计乘1%替代。客户报表额外扣减项已确认是取消订单库存DDP金额；其他账本相似字段不自动继承其定义。收入/毛利直接读取报表存值，毛利率按合计毛利/合计收入，公开分子分母并保留符号，零分母或组成缺失时未定义。

权威定义在 `plugins/datasage-query/contracts/profit-semantics.yaml`。未提供币种或数量单位的月表，不开放跨币种原币求和或未经单位核验的数量指标；产品表订单数不能直接当跨产品去重订单数。

插件注册时固定合同快照；本地修改尚需受影响网关重新注册才会采用，旧会话还可能保留旧提示。本轮口径核对期间不自动推送或重载。


## 库存池、周/月观察与历史客户

inventory 域提供当前登记池、既有周基线比较、基线范围净出库、独立月报和历史购买客户关联。使用 `datasage_catalog` 的 inventory 索引发现指标，再读取精确指标详情；详情发布业务定义、必需分组、排序能力及选择边界，实际结果提供读取时点、缺失、单位和截断证据。

权威业务口径维护在 `plugins/datasage-query/contracts/inventory-semantics.yaml`；本 README 不复制门槛、名单、公式、出退资格或字段解释。当前池、周观察、独立月报与历史客户是不同问题范围，依据精确详情选择，不把一个接口替代成另一个。保持只读查询，不为查询创建冻结、定时任务或消息。

新增普通指标沿用合同和通用编译器。客户历史的专用查询接入方式及未迁移边界见 `ARCHITECTURE.md` 的“专用查询接入与规则归属”；运行版本仍需官方重新注册后才会采用磁盘变更。

## 找版任务与关联出库

pattern_matching域正式提供task_recorded_summary、linked_delivery_amount和person_attributed_delivery_amount，均通过原有catalog、entity_resolve、query入口。任务汇总同时返回任务、执行记录、执行人数、单一候选产品、找到/反馈/米样/关联记录的存在性计数及身份/状态缺口。一行不是一个任务；肯定、否定、未填可在同一任务并存，不能做互斥漏斗或任选最终状态。

金额按唯一匹配且核验通过的实际出库明细交易币种分组。linked_delivery_amount按明细身份去重；person_attributed_delivery_amount按用户确认的任务、执行人、单一最终产品、出库明细去重，同人重复执行不重复算同一明细，跨任务或不同人允许重复归因。相同金额的不同明细仍分别计入，同一单多明细不丢；冲突或缺身份/金额/币种只保留可靠部分，不选一行凑数。需求币种不充当交易币种，不换算人民币，不归因为增量收入或业绩功劳。

维度可选任务、客户、销售、执行人、最终候选产品编号、任务原始区域、任务类型、记录任务状态、记录执行状态；金额还必须按交易币种分组，省略维度时默认币种分组并公开标签。最多4个显式维度。客户、销售和执行人筛选复用已有稳定身份解析，执行人以ERP映射筛选，但统计与归因按执行人自身ID；未匹配当前员工表的历史执行人仍保留分组。组织路径和产品集合不猜成标准部门/产品身份。

pattern_time_basis默认current_observation。指定期间须选task_created、execution_completed；金额还支持linked_delivery，使用核验的实际出库时间。可按所选业务时间做月分组。创建月队列只是当前读取时对该创建队列的观察，后补关联会改变结果，不是历史当时快照、当月出库流水或统一观察时长的转化率。

读取时点与任务/执行业务修改最大时间分别披露；业务修改及信息模式表元信息均不证明ETL刷新频率。金额的币种总体和显示组数在截断前保留，但重复到每行的总体不可二次累加。没有价格明细、需求数量字符串汇总、备注全文、协作/审批人资料、历史快照重建、正式因果归因或绩效评分。本次不配置冻结、定时或发送。

## 公开投影与关联金额覆盖修复

编译器在具有固有分组时返回effective_dimensions，由统一公开投影入口消费；基线明细、基线按单位汇总和基线净出库省略dimensions或传空数组时，仍返回实际业务分组和单位。普通指标不因这项修复增加分组。

登记规格和利润订单维度通过受控public_display_fields声明可展示的业务标识值，仅允许各自的规格标识和单号；物理字段名、其他内部ID及敏感字段不因此公开。原值展示可用于现有精确筛选；若超长或含控制/空白字符而被现有展示规则缩略/规范化，返回display_only=true，不能把该展示值当原始筛选token，不新增身份或别名系统。

找版金额先以已记录或疑似关联为适用总体，再判断期间、币种、明细和金额覆盖；确实无关联的任务不污染金额完整性，仍保留在任务统计。缺关联身份、真实出库时间、交易币种、金额或来源冲突的疑似/坏关联仍保留部分或未知。此次不改变去重键、账本、费用/核算、单位、负数或其他已确认业务规则。

## 本机固定工作流测试入口

`scripts/datasage_workflow.py --mode test` 提供独立测试表上的持久化价格循环及滞销完整阶段编排，复用正式比较、查询、模板和投递状态机。初始化、重跑、故障恢复与配置见 [WORKFLOW_RUNTIME.md](plugins/datasage-query/WORKFLOW_RUNTIME.md)。运行不依赖外部验收脚本；生产模式和定时任务仍未启用。

## 真实来源验收

`--mode live` 使用真实源与数据库时间，仍仅写独立测试表。普通小时观察、手动新观察和发送分离；各部门按周/代次去重。配置、阻断和未开启的有界调度方案见 [LIVE_WORKFLOW.md](plugins/datasage-query/LIVE_WORKFLOW.md)，实际覆盖记录只保存在本机私有验收报告中，不随源码上传。

价格真实来源已改为逐键接续，并恢复采购报价表自身身份；当前验收、抽样未覆盖范围和未开启的小时窗口记录仅保留本机。旧 blocked 是保留的历史证据，不是当前全部价格停摆。


最新状态（2026-09-17）：用户已暂缓自动调度，原小时窗口仅留作历史提案，不视为授权或自动顺延。客户包本地检查及获准测试投递的实际结果只保留在本机私有验收清单中。
