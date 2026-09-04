# DataSage Canary Next

这是 DataSage Profile 的源码目录，也是当前 `datasage-canary-next` 运行目录。
当前维护方式是在现有 Git 工作区和活动分支上原地修改、测试、审查与提交；
不创建第二个安装实例，也不使用 `hermes profile install/update` 覆盖这个目录。

`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据属于用户态，
不得提交，也不得由发行更新覆盖。企微和数据库权限不属于发行流程的修改范围。

当前候选版本：`0.15.0-rc12`。

## 定位与使用边界

DataSage 面向公司经营负责人及出库、销售、应收、财务、库存和目标负责人，
覆盖 delivery、receipt/collections、receivable、target、inventory 和
customer_risk 六个经营域。它提供受治理证据的事实、诊断和分级建议，不是
公共研究、普通写作、用户文件分析器，也不批准或执行业务决策。
当前候选目标成熟度为 L3 数据专家；本候选版本不声明 L4 主动管理或主动巡检能力。

WeCom 入口仅声明 `clarify` 和 `datasage-query` 两个 toolset，所有已认证企微成员
均可私聊和群聊，并共享六个经营域同一完整的 DataSage 查询面。Profile 不施加
用户、群组、部门、实体、行或领域过滤；这不替代外部数据库授权。数据库执行仍为
SELECT-only，并由治理查询合同、只读执行限制和 evidence 边界约束。

业务员个人目标、分摊实际和完成率使用 `salesperson_allocation` 的 split 账本作为
独立权威来源；transaction-detail 基表仅用于诊断，不决定销售分摊覆盖，也不得在
split 缺失时回填、混入或替代个人业绩口径。

本候选也支持 salesperson→customer 的 split 普通下钻；净收款登记额按收款拆分登记额
减退款拆分登记额解释，不代表实结或到账。期间或分解证据不完整时必须披露限制，因果
分解不作无证据推断；canary live replay 仍 pending。企微全员私聊/群聊与数据库业务
权限保持不变。

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
- `hermes profile install/update/info` 仅用于未来独立发布的 Profile Distribution，
  不是当前 Git 同址工作区的维护或重启步骤。
- `build_release_receipt.py` 只是源码仓库中的 DataSage 质量门禁：它把真实 replay、
  compaction、交付和性能证据绑定到一个经过审查的运行载荷。它不是安装器、
  版本系统或回滚系统，也不随运行 Profile 发行。
- `distribution_owned` 只列运行所需文件。测试、E2E scorer、构建脚本和历史重构
  文档留在源码仓库，不进入干净安装实例。

## 源码质量工具（不进入 runtime）

`build_release_receipt.py`、离线测试、Host compaction/performance evidence 与 candidate receipt
仅用于源码审查，绑定当前 Git 载荷并阻断错配；它们不进入 `distribution_owned`。
这些工具不改变 Profile 的查询、身份或结论边界，也不是安装器、版本系统、回滚器或 SLA。
量化验收门槛唯一以 `ARCHITECTURE.md` 的“验收门槛（唯一量化真源）”为准，本文件不重复阈值。
正式版本身份仍由 Git commit/tag 和 `distribution.yaml` 管理；运行时不读取 receipt。
测试、E2E scorer、release/pending 产物与历史文档均留在源码仓库。

代码冻结、candidate receipt 和 Git tag 只表示 canary 载荷已固定并可审查，
不等于 production-ready 或生产安全证明。生产声明还须满足 `ARCHITECTURE.md` 的生产边界
和唯一量化验收门槛；owner 仅指 Profile 维护/发布责任角色，具体责任人和 SLA 由发布记录填写，
不在 Profile 文档中虚构。

## 当前 Git 原地维护

所有 CLI/维护命令必须显式使用 `-p datasage-canary-next`；不得依赖默认 Profile、
当前目录或 `HERMES_HOME` 推断。
所有源码变更留在当前活动分支。重启前必须确认 `git status --short` 只包含本次
已审查改动，完整离线测试和候选门禁通过，再提交并记录可回滚的 commit/tag。
Git 操作不得覆盖 `.env`、`state.db`、sessions、logs、Memory、企微或数据库配置。

未来如需对外发布独立 Profile Distribution，再按 Hermes 官方要求建立根目录含
`distribution.yaml` 的独立发行源；不得复制第二份可编辑源码或自建安装器。

DataSage 当前没有有 owner 的定时任务，因此不向 Hermes `cron` toolset 暴露
`datasage-query`，本候选版本不声明 L4 主动管理能力。未来新增主动巡检时，必须同时定义任务 owner、调度身份与权限、
超时/重试和端到端测试，不能只恢复配置项。
