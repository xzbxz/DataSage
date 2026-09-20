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

### X03 网关执行配置（已接入，运行验收暂缓）

配置仅作用于 `gateway-service/Hermes_Gateway_datasage-canary-next.cmd` 和同名 `.vbs`
启动的单 Profile Gateway 子进程；同 Profile 的维护 CLI、本机运营脚本保持原环境。
直接运行 `hermes gateway run`，以及 Windows 的 `hermes gateway start/restart` 直接派生路径，
不会读取这两个入口的覆盖设置；授权启动隔离实例时应使用上述已配置入口，不能把其他启动路径标为已隔离。

| 宿主来源（相对本 Profile） | 容器路径 | 模式 |
| --- | --- | --- |
| `workspace/execution-input`（已批准的网关共享输入目录，当前为空） | `/input` | 只读 |
| `skills/business-analytics/datasage` | `/root/.hermes/skills/business-analytics/datasage` | 只读 |
| 不挂宿主目录；每个容器独立的 128 MiB tmpfs | `/output` | 容器内读写 |

不挂载整个 Home、cache、report_runs、用户文档、凭据、会话或状态。
输出使用绝对 `/output/...` 路径，由既有原生文件取回/媒体路径保存；远程 Python 内核
有自己的工作目录，不把它假定为终端的 `cwd`。首次大查询结果与聚合预算 spillover
均在需要时建立执行容器，继续使用原生临时文件回退，不要求模型重新查询数据。

后端沿用已安装 Podman 的 Docker 接口及专用机器 `datasage-r4-01a0b8d5`，镜像固定为
`public.ecr.aws/docker/library/python@sha256:de572b33eae61a53675a87bbd02b5e365df7b6b2b06c9276124e965cec08c452`。
使用 `docker_auto_mounts=explicit`、关闭网络和宿主持久化、只读根文件系统，限制为2核/4 GiB。
入口在实际启动时检查专用机器；后端不可用就停止，不退回 local。

两个正式入口已安装上述配置，并通过安装后的 `--inspect-terminal` 检查；Gateway未启动。
将来获得启动授权后，通过 `wscript.exe //B //Nologo <Profile>\gateway-service\Hermes_Gateway_datasage-canary-next.vbs`
启动该隔离入口；只检查配置时使用 `cscript.exe //Nologo` 调用同一文件并传入 `--inspect-terminal`，不会启动服务。
原文件保存在各自同名 `.before-x03` 文件中，已确认可恢复原字节。业务权限与共享Profile配置未改。
回退应先保持网关停用，再恢复这两个备份；不能把未隔离的 local 当作自动恢复方案。
原生服务重装/更新可能重建入口，届时应核对这些配置差量。当前状态以既有任务清单为准。

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
