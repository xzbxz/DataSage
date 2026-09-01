# DataSage Canary Next

这是 DataSage Profile 的源码目录，也是当前 `datasage-canary-next` 运行目录。
当前维护方式是在现有 Git 工作区和活动分支上原地修改、测试、审查与提交；
不创建第二个安装实例，也不使用 `hermes profile install/update` 覆盖这个目录。

`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据属于用户态，
不得提交，也不得由发行更新覆盖。企微和数据库权限不属于发行流程的修改范围。

当前候选版本：`0.15.0-rc9`。

## 定位与使用边界

DataSage 面向公司经营负责人及出库、销售、应收、财务、库存和目标负责人，
覆盖 delivery、receipt/collections、receivable、target、inventory 和
customer_risk 六个经营域。它提供受治理证据的事实、诊断和分级建议，不是
公共研究、普通写作、用户文件分析器，也不批准或执行业务决策。

WeCom 入口仅声明 `skills`、`clarify` 和 `datasage-query` 三个 toolset，但会话
访问按业务授权向所有已认证企微成员开放私聊和群聊；DataSage 查询和实体解析
不施加 Profile 行级过滤。数据库执行仍保持只读和证据治理边界。`skills` 按
Hermes 原生定义仍包含 `skill_view` 和 `skill_manage`，Skill 写入继续受
`skills.write_approval` 保护。

建议分为三档：描述性监测、诊断性解释、高影响建议。高影响建议必须带有
假设、负责审批的人、重大风险和复核点；证据歧义、不可用、过期或不完整时，
升级给相应指标/域负责人或人工审批人。

## Hermes 原生专家能力

本 Profile 不再使用 `.no-bundled-skills` 退出 Hermes 内建 Skill 同步。针对精确
锁定的 Hermes `0.20.5`，`config.yaml` 用完整 `skills.disabled` denylist 只保留
经过审查的文档、表格、PDF、演示文稿和 OCR 能力。内建 Skill 由 Hermes 在下一次
`hermes update` 时同步；需要立即同步时运行
`hermes -p datasage-canary-next skills opt-in --sync`。本仓库不复制或修改这些宿主资产。

升级 Hermes 前必须先运行离线测试。测试会比较新宿主的完整 bundled Skill
名称集合和当前 denylist；任何新增、删除或重命名都会阻断升级，直到维护者完成
快照差异审查。不要只因为新 Skill 看起来相关就默认启用，也不要手工维护一份
Profile 内的内建 Skill 副本。

## 权威边界

- Git commit/tag 是源码版本身份；`distribution.yaml` 是 Hermes 安装载荷与版本声明。
- `plugins/datasage-query/contracts/metric-governance.yaml` 是指标负责人、生命周期和
  复核状态的唯一治理来源。未知负责人或复核信息保持为空并阻断 release，不能用
  假日期补齐；它们不会让 active 指标在运行中突然不可用。
- Catalog `full` 是业务能力摘要，`audit` 是不含物理表、字段和公式的治理审计视图。
- `hermes profile install/update/info` 仅用于未来独立发布的 Profile Distribution，
  不是当前 Git 同址工作区的维护或重启步骤。
- `build_release_receipt.py` 只是源码仓库中的 DataSage 质量门禁：它把真实 replay、
  compaction、交付和性能证据绑定到一个经过审查的运行载荷。它不是安装器、
  版本系统或回滚系统，也不随运行 Profile 发行。
- `distribution_owned` 只列运行所需文件。测试、E2E scorer、构建脚本和历史重构
  文档留在源码仓库，不进入干净安装实例。

## 候选质量门禁

全部实现合并后，在源码根目录为当前载荷生成一个新的、不可覆盖的候选 receipt：

```powershell
$candidate = "pending/datasage-v015-rc9-candidate-receipt-$(Get-Date -Format yyyyMMddTHHmmss).json"
python -B build_release_receipt.py --output $candidate
python -B build_release_receipt.py --verify-candidate
```

不带 `--receipt` 的 `--verify-candidate` 只扫描 `pending/` 下的 candidate receipt，
并按版本和当前 `content_sha256` 唯一发现。旧的同版本候选可以保留；它们不会被
误选。如果当前内容没有唯一匹配项，命令必须失败，而不是猜测“最新”文件。

需要锁定某份候选时可显式运行：

```powershell
python -B build_release_receipt.py --verify-candidate --receipt $candidate
```

Host compaction 与非数据库性能门禁只接受当前 Git HEAD 的原始证据。源码完成
审查并提交、Profile 与 Hermes 两个工作区都干净后，按同一 HEAD 生成：

```powershell
$head = git rev-parse HEAD
python -B tests/test_host_compaction_e2e.py --output "pending/evidence/host-compaction-$head.json"
python -B tests/run_performance_evidence.py --output "pending/evidence/performance-$head.json"
python -B build_release_receipt.py --verify-candidate --receipt $candidate
```

第一条复用 Hermes 官方 compaction 链且禁止网络；第二条会调用合同固定的模型，
但三条工具路径都必须在数据库前返回 `DATA_ENTITLEMENT_DENIED`，总调用数和按官方
峰值价格快照估算的测试费用受跟踪合同限制。两类报告只保存 raw trace/usage，
通过状态、P50/P90 和总成本均由 release builder 重算。它们不验证数据库、企微、
业务结果或生产并发，也不是业务 SLA。

这些本地 JSON 绑定 HEAD、Git blob、Hermes commit 与当前工作区，但没有独立签名；
它们用于防止过期、错配和手填派生状态，不能抵御同一机器上有文件写权限的恶意
操作者。需要跨人员的防篡改证明时，应由受保护 CI/签名系统补充 attestation，
不得在 Profile 运行时自建签名器或 planner。

candidate 与 final receipt 使用严格分离的目录和文件名：

- `--verify-candidate` 只接受 `pending/*-candidate-receipt*.json`；
- `--check` 只接受 `release/*-release-receipt.json`；
- `--output` 只创建新的 pending candidate，拒绝覆盖，也不能直接写入 `release/`。

退出码 `2` 表示当前载荷没有匹配的 receipt 或身份不一致；退出码 `3` 表示载荷
一致，但 DataSage live/host/performance 门禁仍阻断。离线测试通过不等于可发布。
所有质量门禁均通过并完成审查后，才把完全相同的候选 receipt 作为新的
`release/*-release-receipt.json` 提交；随后对最终提交打 tag。可用以下命令检查
final receipt 与载荷的绑定：

```powershell
python -B build_release_receipt.py --check
```

历史 receipt 保持不可变。receipt 的内容哈希只是质量证据的受测对象，不取代
Git commit/tag，也不证明 Hermes 安装来源。

## 当前 Git 原地维护

所有源码变更留在当前活动分支。重启前必须确认 `git status --short` 只包含本次
已审查改动，完整离线测试和候选门禁通过，再提交并记录可回滚的 commit/tag。
Git 操作不得覆盖 `.env`、`state.db`、sessions、logs、Memory、企微或数据库配置。

未来如需对外发布独立 Profile Distribution，再按 Hermes 官方要求建立根目录含
`distribution.yaml` 的独立发行源；不得复制第二份可编辑源码或自建安装器。

DataSage 当前没有有 owner 的定时任务，因此不向 Hermes `cron` toolset 暴露
`datasage-query`。未来新增主动巡检时，必须同时定义任务 owner、调度身份与权限、
超时/重试和端到端测试，不能只恢复配置项。
