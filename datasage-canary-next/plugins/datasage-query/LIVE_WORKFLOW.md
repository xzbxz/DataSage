# 真实来源验收模式

入口：`scripts/datasage_workflow.py --mode live`。`live` 表示读取真实业务来源，**仍然是独立测试存储和测试投递目标**，不是生产启用。原 `--mode test` 继续作为合成 fixture 的独立证据，不混入真实实例。

本机显式配置为 Git 忽略的 `workflow-live-runtime.json`，示例在 `docs/workflow-live-runtime.example.json`。身份、库、owner、固定实例和前缀须完全匹配，不接受任意库表/SQL 覆盖。表固定为 `vk_ai.ds_test_live_v1_` 加 registry、stock_input、monthly_stock_input、slow_baseline、sales_snapshot、purchase_snapshot、price_input、cycles。创建前核对冲突；既有对象必须全部归属、引擎、字段和无触发器检查通过才可复用。原 fixture 表和更早验收表不清理。

凭据继续由官方环境加载器及现有安全运行配置读取，通知沿用锁定的验收成员和测试 webhook。没有旧 Git 抽取凭据或 Codex 工作目录依赖。当前账号权限较宽、数据库连接无 TLS，仍是明确的生产切换前条件；应用白名单不等于数据库权限隔离。

## 价格：观察与投递分离

```text
python <profile>/scripts/datasage_workflow.py --mode live init
python <profile>/scripts/datasage_workflow.py --mode live prices
python <profile>/scripts/datasage_workflow.py --mode live observe-prices
python <profile>/scripts/datasage_workflow.py --mode live deliver-prices
```

`prices` 是正常小时周期：取数据库时钟，一侧同一小时至多观察一次；已观察的 blocked 也会返回原状态，不靠重试掩盖问题。`observe-prices` 是明确的手动新观察，使用真实数据库时间建立唯一轮次，不采用 fixture tick。两个观察动作均不能外发。

首次完整有界读取四区域现货/主推集合的销售、采购记录（每侧最多 10,000 行），同一只读一致性快照内读取旧快照作为已标识起点。只首次复制旧参考；后续与本实例自己的快照比较，不重复读旧快照来重置基线。原始观察和周期证据保留，当前测试输入只按固定侧别替换。

源为空、跨日、截断、超预算或源读取失败仍整批阻断。单键缺身份、歧义、币种/已知单位税口径变化、精度无法保存则单键隔离：保留旧参考并记录独立异常，其他健全键继续。完整来源中旧键未见不等于删商品，保留旧值；新键首次建立参考，不当作从零涨价。问题修复后与该键保留的最近可比参考比较，成功后再推进，后续相同值不重复报警。

销售恢复旧版同币种名义 DDP 字段比较。旧快照未保存历史单位/税字段只限制解释层级，不再令 838 条记录整体停摆；不补历史字段，也不称经济意义一致的纯涨跌。采购分别比较含税/未税记录价格并核对已记录币种和计价单位。无法保存的更高小数精度仍单键隔离，不能先发送再舍入。

采购的报价身份取自报价表 p.goods_no，监控池通过 EXISTS 只判定成员，不把池文本投影成报价键或放大行数。原始池中的普通空格/NBSP 变体保留诊断。若来源报价 detail_id、供应商、颜色完全一致且货号仅差首尾空白，只沿用较新的已观测参考作比较，避免同一报价跨别名重复提醒；没有这些证据就不合并。原始键、before_records、异常和旧 blocked 周期均保留。
无变化且完整性门槛通过，零通知后提交自己的验收快照。存在可解释变化时，先持久化事件及按旧角色/客户规则生成的待发清单；`deliver-prices` 只处理已准备内容，不能临时读取新来源。每次最多两个逻辑通知；尚有后续批次则保持 planned，快照不推进。发送成功后复用同一事务核心推进快照及提交标记。明确失败只补缺失组件；unknown 不盲重试；sending 仅在已确认全部组件成功时恢复。数据源阻断不会被当作发送失败。

`anomaly-evidence` 对当前已隔离空白货号做有限定点只读检查；无现存变体时保留既有证据，不覆盖为空。`continuity-replay` 仅用封存的 before/current 重算并核对已提交摘要，不查生产、不写库、不发送。旧价参考、历史单位税缺口、观察时间与 ETL 新鲜度仍分别解释。
## 滞销：真实部门、周次和明确代次

```text
python <profile>/scripts/datasage_workflow.py --mode live slow-prepare --department HCM
python <profile>/scripts/datasage_workflow.py --mode live slow-preview --department HCM
python <profile>/scripts/datasage_workflow.py --mode live slow-deliver --department HCM
```

部门仅能从 HCM、HN、BKK、IDK 及四个 -HT 部门选择。每部门完整读取当前库存（上限 20,000 行）及前月/本月库存（上限 100,000 行），按所有选中字段稳定分页，并在同一源快照下收齐；不是 3 SKU 抽样。超过预算、数据为空或不完整时停止，不降级为抽样全量。

固定 scope 为部门＋ISO 周＋generation。完整读取后只写本实例按 scope 隔离的输入，复用正式 freeze_plan 冻结；同一普通周次存在时保留已冻结数据，不再次重冻或重复发。周次改变生成新 scope，不复用旧周。显式 `slow-new-generation --department ... --reason ...` 才申请新代次；原因必填，上一代未完成或 unknown 时禁止用新一代绕过恢复。

`slow-prepare` 完成测试冻结和周/月报准备，不发送。`slow-preview` 进一步封存任务和一个完整客户包的具体内容。客户关系按原 12 个月规则查完整计划，但本次只抽验一个完整销售包，未选包数量明确保留；不声称客户投递全覆盖。`slow-deliver` 每次仅执行一个阶段，严格按任务→客户包→实际回执核对→周报→月报。全部完成后重跑返回 already_completed_no_resend。跨入口去重要求同一 ISO 投递周、正文/附件封存未变且组件回执真实成功；不把不同实例名或外层文件名当新内容，也不会因为月报正文相同而跳过下一周的正式月报。明确新代次仍按显式业务意图处理。

原角色选择、客户关系、核对、单位和汇总完整性门槛复用已有代码。任务/报表实际只重定向至测试成员，不能把平台回执说成原销售或客户已收到。周月查询映射到本实例 scope 的真实输入，血缘字符串保持原样，证据另记实际测试表映射。未发送报表超过 1 小时需重新观察；已经部分发送的内容不自动改写。历史待发版本及查询证据保留。

## 调度准备，当前关闭

```text
python <profile>/scripts/datasage_workflow.py --mode live schedule-plan
python <profile>/scripts/datasage_workflow.py --mode live lock-check
python <profile>/scripts/datasage_workflow.py --mode live status
```

固定官方脚本：`datasage_live_sales.py`、`datasage_live_purchase.py`、`datasage_live_slow_task.py`、`datasage_live_slow_report.py`，采用官方 script/no_agent/local 作业机制。没有创建本线程自动化，也没有实际注册 Hermes 作业。

建议下一次单独批准 **6 小时**验收窗口：销售每小时第 07 分钟、采购第 12 分钟；这是新测试分钟方案，不声称找回了旧调度分钟。目标仍是现有批准测试私信/群，每次最多两条逻辑通知，有阻断则不发送、不推进。窗口内没有真实变价也算有效观察，不能制造变价证明调度成功。

滞销到期规则为周二 09:00 起的 30 分钟内每 5 分钟检查未完成任务阶段，普通周期不重新冻结；周六报告在全部所选部门周报完成后才允许任何部门月报；先验证全部任务链和周冻结。手动单部门验收可以依次完成其五阶段，必须标明与正式分阶段调度不同。手动观察/准备不等于到期投递。每次最多两个逻辑阶段，超出继续保留待处理状态。

开启需另外批准确切起止时间和目标，然后同时开启本机 live 配置的 schedule_enabled，并提供 `workflow-schedule-acceptance.json`：enabled、带时区 starts_at/ends_at、max_invocations（1–60）、departments（最多两个批准部门）。窗口最长 24 小时，全局调用预算用数据库锁保护；缺文件、关闭开关、过期、非到期时刻均不运行。官方作业应先 paused 创建、核对后再启用。本轮两层开关未开启、没有作业注册。

停止：关本机审批开关或等待窗口过期，并暂停官方作业；数据/回执不清理。恢复：核对 unknown/未完成周期，重新批准有限窗口，恢复原作业；不得重置基线或新建代次逃避未知状态。

证据位于 `report_runs/workflow_live/`：status.json（完整）、summary.json（紧凑）、分轮价格结果、部门准备结果、查询/核对文件、发送阶段回执和 SQL 参数摘要日志。它们不进 Git。`lock-check` 已用两条真实连接验证重复启动拒绝及释放后可恢复。

## 2026-09-17 提议的有界自动窗口（未开启）

提议 2026-09-17 12:00–18:00（Asia/Shanghai），仅开启销售/采购两个小时脚本，分别每小时 :07 和 :12，最多 12 次自动调用。若该窗口已过去，必须提出并批准新窗口，不自动顺延。样例 `docs/hourly-acceptance-proposal-20260917.json` 为 enabled=false，不是运行审批文件。

写对象仅 ds_test_live_v1_ 八张自有表；旧生产快照只读。无变化静默；真实可解释变化仅发既有批准测试私信/群，每次最多两个逻辑通知。异常键保留，整批读取失败不推进；未完成投递或 unknown 保持状态，不能靠下一小时重置。通知批次超预算则留待恢复，不能提前接受基线。

启用前还需明确批准窗口内真实价格变化的测试投递范围，将本机两层开关显式开启，并由官方创建 paused 的两个作业核对后恢复，deliver/failure_deliver 均为 local。到时、到次数上限或关闭审批立即不再执行；暂停原作业并保留账本。恢复需要检查未知/未完成状态，另行批准有限窗口，不能新建周期绕过。

已经证明的是手动真实业务连续性、锁与幂等、故障测试、官方脚本关闭状态可运行；尚未证明钟表自动触发、真实自动到期和自动暂停恢复。本轮没有注册或启用任何持续调度。
