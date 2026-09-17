# 结构收敛与运行边界

本文是本机 Profile 的结构收敛指南。代码已安装，角色配置已按授权完成等价格式迁移。
网关未重载；磁盘安装不代表运行进程已加载，也不构成真实投递证明。SQL SHA2 实体回查在真实数据库的扫描成本和延迟尚未测量。

## 唯一来源

| 内容 | 唯一来源 | 说明 |
|---|---|---|
| 私有工作流角色 | `local/workflow-roles.json` | Git 忽略；执行人、管理人员和价格管理人员等私有值只在这里保存 |
| 区域/部门规则 | `plugins/datasage-query/contracts/legacy-workflows.json` | 非秘密公共合同；不在角色文件重复维护 |
| 业务查询口径 | `plugins/datasage-query/contracts/` 下相应合同 | 由现有 catalog、compiler、query 和 wire 路径消费 |
| 投递进度 | Git 忽略的 `report_runs/...` | 运行证据和 progress，不是角色来源，也不是发送队列 |
| 源码版本 | 当前 Profile Git commit/tag | 由官方 Hermes 重新加载；不维护自建发布链 |

运行时 `workflow_roles.load(profile)` 只读取 `local/workflow-roles.json`。文件缺失、版本错误、
区域集合不一致或公共合同漂移时直接阻断，不探测 `report_runs`，不从旧文件回退。
旧 `report_runs/reminder_acceptance/legacy-recipient-reference.json` 只能作为显式导入源。

## 角色命令

固定入口是 `scripts/datasage_workflow.py`；入口要求 `--mode test` 或 `--mode live`，角色动作本身
不连接数据库。命令语义如下：

```text
python <profile>/scripts/datasage_workflow.py --mode test diagnose
python <profile>/scripts/datasage_workflow.py --mode test roles-check
python <profile>/scripts/datasage_workflow.py --mode test roles-check --source <source>
python <profile>/scripts/datasage_workflow.py --mode test roles-import --source <source> --output <profile>/local/workflow-roles.json
```

- `diagnose` 只验证当前磁盘 Profile 与当前 CLI 进程的运行环境，不替代角色就绪检查；
  正在运行的 gateway 版本是 `not_observed`，该命令不证明网关已重载。
- `roles-check` 无 `--source` 时检查活动角色；带 `--source` 时只读执行 `prepare` 预检。
- `roles-import` 必须同时给出旧来源和输出路径，只转换到 Profile 的 `local` 目录；源文件只读，
  已存在输出拒绝覆盖，CLI 没有覆盖选项。
- `roles-check`/`roles-import` 的输出只包含状态、哈希、计数和错误码，不打印人员值。

`local-report-bindings.json` 中旧的 `recipients_file=legacy-recipients.json` 仅是兼容标记；实际
读取仍固定到 `local/workflow-roles.json`。它不是第二个活动文件。

## 回执与报告完整性

统一 `acceptance_delivery` 路径产生的新投递回执使用 `datasage-delivery-binding/v1`，绑定业务周期、代次、阶段、业务范围、最终目标、
正文/附件内容、组件 key 和 progress scope。`workflow_delivery_review` 先核验回执自身的 binding、
progress 和组件状态，再判断业务内容是否等价；只有 `verified_for_reuse` 才能抑制新发送。
没有新 binding 的旧 `provider_accepted` 归类为 `historical_provider_accepted`，只保留历史证据，
不能自动复用。旧验收记录中的 10/9 计数不能提升为新 binding 的通过。

`report_evidence.validate` 负责报表业务证据的来源、明细、汇总、粒度和业务范围对账。
共享 `result_completeness.gate_for_document` 负责原始 query packet 的请求集合、状态、覆盖、截断、
观察时点和完整报告门槛。前者回答业务数值是否对账，后者回答投递所需来源是否完整；两者不互相替代。
Fabric 明确请求投递而完整性门槛失败时返回 `delivery=blocked_incomplete_report` 并保留
`report_delivery_gate`；未请求投递时返回 `delivery=not_requested`。

旧角色、旧 progress 和旧回执不会因为代码安装而自动迁移、重写或升级。候选的离线测试、历史验收
数字和接口哈希只能作为相应范围的证据；它们不能替代真实 Profile 安装、网关重载、实际目标验收或
真实来源性能测试。
