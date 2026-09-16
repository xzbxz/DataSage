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

源为空、跨日、截断或超预算拒绝使用部分数据。歧义、缺身份、旧键消失、当前计价口径缺失、已观测前后单位/税/币种不同均阻断推进。旧销售快照没有历史单位/税字段时也明确阻断完整口径迁移，不从当前字段补造历史。已有旧快照并不等于已证明全部历史计价口径。测试销售字段保留旧表的两位小数结构；若真实价格需要更高精度，会在发送前阻断，不能舍入后声称完成推进，需另行做有证据的测试存储迁移。

无变化且完整性门槛通过，零通知后提交自己的验收快照。存在可解释变化时，先持久化事件及按旧角色/客户规则生成的待发清单；`deliver-prices` 只处理已准备内容，不能临时读取新来源。每次最多两个逻辑通知；尚有后续批次则保持 planned，快照不推进。发送成功后复用同一事务核心推进快照及提交标记。明确失败只补缺失组件；unknown 不盲重试；sending 仅在已确认全部组件成功时恢复。数据源阻断不会被当作发送失败。

初始销售历史口径缺失需要单独确认迁移策略，例如保留旧价仅作参考并明确批准以当前完整观察建立新的验收起点；本轮没有自动采用该策略。采购身份缺失与消失键需查明，不能以删除旧行或过滤问题源行制造无差异。当前观测时点也不证明 ETL 刷新频率或报价可立即执行。

## 滞销：真实部门、周次和明确代次

```text
python <profile>/scripts/datasage_workflow.py --mode live slow-prepare --department HCM
python <profile>/scripts/datasage_workflow.py --mode live slow-preview --department HCM
python <profile>/scripts/datasage_workflow.py --mode live slow-deliver --department HCM
```

部门仅能从 HCM、HN、BKK、IDK 及四个 -HT 部门选择。每部门完整读取当前库存（上限 20,000 行）及前月/本月库存（上限 100,000 行），按所有选中字段稳定分页，并在同一源快照下收齐；不是 3 SKU 抽样。超过预算、数据为空或不完整时停止，不降级为抽样全量。

固定 scope 为部门＋ISO 周＋generation。完整读取后只写本实例按 scope 隔离的输入，复用正式 freeze_plan 冻结；同一普通周次存在时保留已冻结数据，不再次重冻或重复发。周次改变生成新 scope，不复用旧周。显式 `slow-new-generation --department ... --reason ...` 才申请新代次；原因必填，上一代未完成或 unknown 时禁止用新一代绕过恢复。

`slow-prepare` 完成测试冻结和周/月报准备，不发送。`slow-preview` 进一步封存任务和一个完整客户包的具体内容。客户关系按原 12 个月规则查完整计划，但本次只抽验一个完整销售包，未选包数量明确保留；不声称客户投递全覆盖。`slow-deliver` 每次仅执行一个阶段，严格按任务→客户包→实际回执核对→周报→月报。全部完成后重跑返回 already_completed_no_resend。

原角色选择、客户关系、核对、单位和汇总完整性门槛复用已有代码。任务/报表实际只重定向至测试成员，不能把平台回执说成原销售或客户已收到。周月查询映射到本实例 scope 的真实输入，血缘字符串保持原样，证据另记实际测试表映射。未发送报表超过 1 小时需重新观察；已经部分发送的内容不自动改写。历史待发版本及查询证据保留。

## 调度准备，当前关闭

```text
python <profile>/scripts/datasage_workflow.py --mode live schedule-plan
python <profile>/scripts/datasage_workflow.py --mode live lock-check
python <profile>/scripts/datasage_workflow.py --mode live status
```

固定官方脚本：`datasage_live_sales.py`、`datasage_live_purchase.py`、`datasage_live_slow_task.py`、`datasage_live_slow_report.py`，采用官方 script/no_agent/local 作业机制。没有创建本线程自动化，也没有实际注册 Hermes 作业。

建议下一次单独批准 **6 小时**验收窗口：销售每小时第 07 分钟、采购第 12 分钟；这是新测试分钟方案，不声称找回了旧调度分钟。目标仍是现有批准测试私信/群，每次最多两条逻辑通知，有阻断则不发送、不推进。窗口内没有真实变价也算有效观察，不能制造变价证明调度成功。

滞销到期规则为周二 09:00 起的 30 分钟内每 5 分钟检查未完成任务阶段，普通周期不重新冻结；周六 19:00/19:05 按先周后月恢复报告阶段。手动观察/准备不等于到期投递。每次最多两个逻辑阶段，超出继续保留待处理状态。

开启需另外批准确切起止时间和目标，然后同时开启本机 live 配置的 schedule_enabled，并提供 `workflow-schedule-acceptance.json`：enabled、带时区 starts_at/ends_at、max_invocations（1–60）、departments（最多两个批准部门）。窗口最长 24 小时，全局调用预算用数据库锁保护；缺文件、关闭开关、过期、非到期时刻均不运行。官方作业应先 paused 创建、核对后再启用。本轮两层开关未开启、没有作业注册。

停止：关本机审批开关或等待窗口过期，并暂停官方作业；数据/回执不清理。恢复：核对 unknown/未完成周期，重新批准有限窗口，恢复原作业；不得重置基线或新建代次逃避未知状态。

证据位于 `report_runs/workflow_live/`：status.json（完整）、summary.json（紧凑）、分轮价格结果、部门准备结果、查询/核对文件、发送阶段回执和 SQL 参数摘要日志。它们不进 Git。`lock-check` 已用两条真实连接验证重复启动拒绝及释放后可恢复。
