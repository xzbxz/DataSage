# DataSage 维护与能力接入

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
