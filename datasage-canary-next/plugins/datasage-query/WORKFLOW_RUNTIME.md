# Profile 固定工作流入口

`scripts/datasage_workflow.py` 在本机 Profile 中运行，自动定位相邻官方 Hermes 运行库并固定 HERMES_HOME。使用官方虚拟环境的 Python；不需要 Codex、工作目录、历史 Git 仓库、临时注入或输出目录脚本。入口只接受 `--mode test`，生产模式尚未提供。

## 配置与存储

本机 Git 忽略文件 `workflow-runtime.json` 必须明确开启 `isolated_acceptance`，指定唯一批准实例 `profile-v1`、库 `vk_ai`、固定前缀 `ds_test_profile_v1_`、owner、服务器 UUID、预期账号和 `credential_source=explicit_existing_DATA_QUERY_MYSQL`，且 `production_writes=false`。字段须完全匹配 `workflow_storage.assert_binding`；缺配置、错误模式、不同身份、不同目标均直接拒绝。

读取凭据复用官方环境加载器和现有 DATA_QUERY_MYSQL_* 安全配置，没有修改生产 freeze_writer 的独立账号门槛。通知继续使用 `acceptance-delivery.secrets.json` 的固定测试成员及批准测试群。滞销角色参考使用既有本机 `report_runs/reminder_acceptance/legacy-recipient-reference.json`；它只决定原业务角色和核对口径，不允许向这些旧目标发送。所有这些本机配置都不进入 Git。

新实例 8 张表：registry、stock_input、monthly_stock_input、slow_baseline、sales_snapshot、purchase_snapshot、price_input、cycles。只接受固定映射，无任意库名、表名或 SQL 参数。核验服务器、账号、库、表 owner、引擎、字段类型和无触发器。创建明确指定 utf8mb4；不通过 IF NOT EXISTS 接管未知对象。已存在对象只在全部校验通过后复用，不覆盖未知表。

宽权限账号与未启用 TLS 仍是当前环境实况。这里提供应用层白名单，不宣称数据库权限隔离；生产切换前仍需单独落实最小权限写账号和传输保护，不能由本入口自动修改权限或服务器。

## 固定操作

从任意工作目录用官方 Hermes 虚拟环境 Python 执行 Profile 下的脚本：

```text
python <profile>/scripts/datasage_workflow.py --mode test init
python <profile>/scripts/datasage_workflow.py --mode test seed
python <profile>/scripts/datasage_workflow.py --mode test slow
python <profile>/scripts/datasage_workflow.py --mode test prices
python <profile>/scripts/datasage_workflow.py --mode test change-fixture
python <profile>/scripts/datasage_workflow.py --mode test prices
python <profile>/scripts/datasage_workflow.py --mode test prices
python <profile>/scripts/datasage_workflow.py --mode test recovery-check
python <profile>/scripts/datasage_workflow.py --mode test status
```

`init` 只创建或验证本实例对象。`seed` 只首次初始化并原子记录种子标记；再次运行保留已有数据，不随实时源更新而重置。滞销选 HCM 3 个 SKU 的当前/月度输入；价格各取旧快照 2 条可比记录，改为显式测试标签。旧快照只读。

`slow` 使用正式冻结规划、客户关系和角色规则、查询编译器、报表核对、模板与投递状态机，顺序固定为冻结→任务→一个完整客户包→基于回执的派发核对→周报→月报。已成功阶段不重发，完成后重跑只返回完成状态。周月报绑定测试物理表，并标注观察时点及样本范围，不能解释为全业务覆盖。基线已有但缺有效周期登记时拒绝覆盖。

`prices` 执行销售及采购各一轮；观察时钟为种子时间加完整小时 tick，仅用于手动测试。复用正式旧基线比较器；新增键不冒充零价上涨，不可比或歧义会阻止推进。事件、输入、旧摘要和待发正文先持久化；发送组件回执确认后，将测试快照和提交标记放在同一事务内。下一轮相同输入零发送。`change-fixture` 只对本实例主场景施加一次 +1 变价及新增键，重复调用不再改价。

## 恢复边界

明确失败可从同一持久事件恢复，投递状态机只补缺失组件。持久状态为 sending 时，仅已确认所有组件成功才能恢复确认，不能靠猜测重发。unknown 保留并阻断该场景，不能删除状态来制造通过。提交回执丢失通过数据库周期标记和快照摘要恢复；数据库未提交时保留 delivered，从事务边界重试，不再投递。已提交后重跑以新快照比较，不重复发送旧事件。

`recovery-check` 用同一周期函数和真实测试库验证明确失败、未知、真实子进程退出、提交前后故障；通知传输完全本地注入，不伪造真实企微回执。故障场景与主场景分开保留；完成证据存在时只核验，不重放。内部 `fault-exit` 仅能作用于固定注入场景，不能发送真实消息。

运行证据及写入日志保存在 `report_runs/workflow_v1/`；附件在既有验收私有根的 `wv1/`；模拟投递状态在 `report_runs/wv1_fault/`。既有验收资料、旧 unknown 和正式进度不清理。所有通知仅代表平台接收，人工已读未知。

未注册定时任务，未重载网关，未改官方源码，未写生产冻结或价格快照。后续小时调度及生产适配需独立落实，不由测试开关自动启用。

另有真实来源验收 `--mode live`，仍非生产模式，见 [LIVE_WORKFLOW.md](LIVE_WORKFLOW.md)。本页的 seed、change-fixture 和受控 tick 仅属于原合成测试实例。
