# R25 核验记录：运营任务授权、调度与生命周期收尾

日期：2026-09-23｜依据：综合审查 V2.0 的 R25（批次 E / P1）｜负责角色：运营 owner＋开发
前置：R06、R14、R24（已完成）｜性质：现状台账 + 离线守卫；真实调度与真实群发送需运营 owner 授权后做

## 1. 结论

- **现役入口共 16 个，全部是「固定绑定 + 默认关闭」形态**：每个适配器只绑定一个 report id，
  自动执行默认关闭；真实写/发需要 bindings 里的显式开关（当前 `send_enabled=false`、
  `register_schedule_enabled=false`、`schedule=null`）。
- **本 profile 没有注册任何调度作业**（`cron/` 下无 `jobs.json`），调度处于关闭状态；
  `LIVE_WORKFLOW.md` 的「调度准备，当前关闭」与该事实一致。
- **查询路径不依赖运营专用文件**：`local-report-bindings.json` 只在运营商侧两处出现
  （`plugins/datasage-query/local_report.py`、`scripts/datasage_slow_report.py`），
  查询执行侧没有任何引用。
- **窗口过期会明确停止**：本机报表要求显式 `baseline_week` 与显式 `max_baseline_age_days`
  （1–366，无出厂默认），没有「自动取最新周」的回退，因此不会静默续期。
- **重试不重复投递已有用例**：写入前/头写入前失败的两次恢复都不重复发送。
- 真实调度注册、真实群发送、真实周期续期仍需运营 owner 授权后执行（见第 5 节）。

## 2. 现役入口台账

表 A：入口、责任人、生成→审批→发送路径、标签、默认路径

| 入口 | 责任人 | 生成→审批→发送 | test/live 标签 | 默认路径行为 |
| --- | --- | --- | --- | --- |
| `datasage_legacy_idk.py` | 运营 owner | 固定 report id → bindings 显式开关 → `send_enabled` | 固定适配器（无 CLI 标签） | 自动执行保持关闭 |
| `datasage_legacy_slow_task.py` | 运营 owner | 同上（`legacy-slow-task`） | 固定适配器 | 自动执行保持关闭 |
| `datasage_legacy_slow_report.py` | 运营 owner | 同上（`legacy-slow-report`） | 固定适配器 | 自动执行保持关闭 |
| `datasage_legacy_sales_price.py` | 运营 owner | 同上（`legacy-sales-price`） | 固定适配器 | 自动执行保持关闭 |
| `datasage_legacy_purchase_price.py` | 运营 owner | 同上（`legacy-purchase-price`） | 固定适配器 | 自动执行保持关闭 |
| `datasage_legacy_fabric.py` | 运营 owner | 同上（`legacy-fabric`） | 固定适配器 | 自动执行保持关闭 |
| `datasage_live_slow_task.py` | 运营 owner | `scheduled-tick --job slow_task` → 有界审批 → `send_enabled` | `--mode live` | 有界审批默认关闭 |
| `datasage_live_slow_report.py` | 运营 owner | 同上（slow_report） | `--mode live` | 有界审批默认关闭 |
| `datasage_live_sales.py` | 运营 owner | 同上（sales） | `--mode live` | 有界审批默认关闭 |
| `datasage_live_purchase.py` | 运营 owner | 同上（purchase） | `--mode live` | 有界审批默认关闭 |
| `datasage_slow_report.py` | 运营 owner | 本机报表 → bindings → 显式发送开关 | `--legacy-preview`（不发送）／`--legacy-run`（显式受门） | 未配置 bindings 时 `REPORT_NOT_CONFIGURED` |
| `datasage_workflow.py` | 开发 | 官方运行时调度入口 `scheduled-tick` | `--mode live`／`--mode preview` | 缺相邻官方运行时即拒绝 |
| `datasage_source_export.py` | 开发 | 分享物导出（无外发） | 导出工具（无写入业务库） | 只读源树 + 写导出目录 |
| `datasage_region_acceptance.py` | 开发 | 验收夹具（无外发） | 需显式动作参数 | 无参数即 `REGIONAL_ACCEPTANCE_ACTION_REQUIRED` |
| `datasage_reminder_acceptance.py` | 开发 | 验收夹具（无外发） | 需显式动作参数 | 无参数即拒绝执行 |
| `datasage_weekly_acceptance.py` | 开发 | 只读本机验收（无外发） | 仅 `--read-hcm-2026-w38` | 其他参数即 `ACCEPTANCE_READ_NOT_ENABLED` |

表 B：写/发副作用、到期停止、审计与补偿

| 入口 | 写副作用 | 发副作用 | 到期/停止 | 审计与补偿 |
| --- | --- | --- | --- | --- |
| `datasage_legacy_idk.py` | 冻结/写入受 bindings 开关控制 | 受 `send_enabled` 控制 | 固定窗口，无自动续期 | 投递进度与回执记录；失败回退不重复发送 |
| `datasage_legacy_slow_task.py` | 同上 | 同上 | 显式周次 + 最大基线年龄 | 同上 |
| `datasage_legacy_slow_report.py` | 同上 | 同上 | 显式周次 + 最大基线年龄 | 同上 |
| `datasage_legacy_sales_price.py` | 价格接受受 `price_accept_enabled` | 受 `send_enabled` | 小时级窗口，过期不执行 | 同上 |
| `datasage_legacy_purchase_price.py` | 同上 | 同上 | 小时级窗口，过期不执行 | 同上 |
| `datasage_legacy_fabric.py` | 同上 | 同上 | 按需，无证明的周期作业 | 同上 |
| `datasage_live_slow_task.py` | 只读 + 有界审批 | 受 `send_enabled` | 调度未注册即不触发 | 同上 |
| `datasage_live_slow_report.py` | 只读 + 有界审批 | 受 `send_enabled` | 调度未注册即不触发 | 同上 |
| `datasage_live_sales.py` | 只读 + 有界审批 | 受 `send_enabled` | 调度未注册即不触发 | 同上 |
| `datasage_live_purchase.py` | 只读 + 有界审批 | 受 `send_enabled` | 调度未注册即不触发 | 同上 |
| `datasage_slow_report.py` | 无业务库写入 | 默认不发送 | 显式 `baseline_week` 过期即停 | 本机报表文件与执行输出 |
| `datasage_workflow.py` | 由所选 job 决定 | 由所选 job 决定 | 单次 tick，无自续期 | 执行输出 |
| `datasage_source_export.py` | 写导出目录 | 无 | 单次执行 | 导出清单 |
| `datasage_region_acceptance.py` | 无 | 无 | 单次执行 | 执行输出 |
| `datasage_reminder_acceptance.py` | 无 | 无 | 单次执行 | 执行输出 |
| `datasage_weekly_acceptance.py` | 无（只读） | 无 | 仅固定周次参数 | 执行输出 |

## 3. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 调度未注册 | `cron/` 下无 `jobs.json`，无作业条目 | 本 profile `cron/` 目录实况 |
| 默认关闭 | 绑定报告的全部门禁均为 false：`enabled`/`read_enabled`/`customer_mapping_enabled`/`freeze_enabled`/`send_enabled`/`price_accept_enabled`/`register_schedule_enabled`，`schedule=null`、`failure_deliver=false` | `local-report-bindings.json`（运营本机文件） |
| 查询不依赖运营文件 | `local-report-bindings.json` 仅出现于 `local_report.py` 与 `scripts/datasage_slow_report.py` | 全插件 grep |
| 双标签 | `--legacy-preview`（不发送）与 `--legacy-run`（显式受门），各自 6 个 report id | `local_report.py:291-292` |
| 未配置即拒绝 | 未配置 bindings 时 `REPORT_NOT_CONFIGURED`；适配器默认路径 `WORKFLOW_EXECUTION_NOT_ENABLED` | `scripts/datasage_slow_report.py`、`fixed_workflow_main` |
| 窗口过期停止 | 显式 `baseline_week`、显式 `max_baseline_age_days`（1–366，无默认），无自动最新周回退 | `LOCAL_REPORTS.md` 合同表 |
| 重试不重复投递 | 写入前失败、头写入前失败两条恢复用例通过（共 43 项） | `tests/test_purchase_price_recovery.py`、`tests/test_sales_price_recovery.py` |
| 台账不漂移 | 6 项守卫（覆盖全部脚本、无未授权调度、未授权发送必须登记、查询侧不引用运营文件、双标签存在、四列非空） | `tests/test_operational_entry_inventory.py` |

## 4. 与验收条件的对应

| R25 验收条件 | 本轮状态 |
| --- | --- |
| 未授权不注册 | 无 cron 作业；`register_schedule_enabled=false`、`schedule=null`；守卫禁止未登记就开启 |
| 未授权不发送 | `send_enabled=false`；真实发送需 bindings 显式开关；守卫要求开启即须在台账登记 |
| 重试不重复投递 | 既有幂等回执用例通过 |
| query 不依赖 operator 专用文件 | 运营商文件只出现在运营商侧两处，查询侧零引用（守卫固定） |
| 过期窗口明确停止 | 显式周次 + 最大基线年龄，无自动续期回退 |
| 保留审计与补偿说明 | 台账表 B 逐入口列出写/发/到期/审计与补偿 |

## 5. 需要运营 owner 授权的动作（未授权前助手不做）

1. 注册真实调度（官方 cron 或宿主调度）——需要 owner 明确时段、job、上限与失败通知目标。
2. 打开任一 `send_enabled` / `register_schedule_enabled`——需要 owner 指定群与内容边界，并在我方台账登记后执行。
3. 真实周期续期策略（例如滞销周的推进方式）——需要 owner 给出显式周次推进规则，不接受自动取最新周。
4. 一次真实测试群投递 + 一次失败恢复演练（对比投递记录，确认不重复发送）。

## 6. 回退

- 台账与守卫可整体移除，不涉及任何调度或发送开关变更。
- 若某入口被误开启：把对应 bindings 开关置回 false 即回到默认关闭；已有回执保留用于审计。
