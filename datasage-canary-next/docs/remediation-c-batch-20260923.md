# C 批离线整改记录（R10 / R11 / R12 / R15 + R29 离线部分）

日期：2026-09-23｜依据：综合审查 V2.0 的 C 批与可离线执行的其它任务
性质：工程证据记录。C 批中需要真实渠道、审批人和业务 owner 的部分未执行，见 §4。

## 1. 本轮完成项

### R10 统一声明、注释与历史状态（P2）

- 改动：`ARCHITECTURE.md`「Hermes 内建 Skill 选择约束」段。
  - 现状写清：当前渠道面是 native `clarify`、三个 DataSage 工具与官方 `skills` 组
    （该组含管理入口，写入待审批、读取不执行内联 shell），企微不提供任意代码/终端/文件/Web 工具。
  - 历史观测单列：`execute_code`、`skills_list`、`skill_view` 的本地装配观测标注为
    「锁定官方基底（宿主 0.21.1 / `2237be3`）与撤回定制容器之前」的历史，并明确不代表当前工具面。
- 仍未落地：`config.yaml` 的渠道注释仍旧（"native Skill reads without skill_manage /
  official code processing"）。修正该注释需要编辑 Profile 配置，本机守卫明确拒绝 agent 写入
  （"Refusing to write to Hermes config file … Agent cannot modify security-sensitive
  configuration"），故未绕过。待人工替换的文本见 §3。
- 边界：只改文案，未变更任何工具面、权限或开关；`test_integration_boundaries` 的
  wecom 工具面断言与 `test_expert_authority_inventory` 的架构行断言均保持通过。

### R11 SOUL 规则迁移映射（P1 的离线部分）

- 新增 `docs/rule-ownership-20260923.md` §2：逐条记录 SOUL 区块 → 权威来源 → 是否摘要 →
  消费者 → 消费者验证 → 处置。
- 本轮不删减 SOUL 正文：删除条件是「消费者验证」完成（多为真实回放），避免在缺少证据时
  机械精简；已标明量化门槛只引用 `ARCHITECTURE.md`、不与合同重复。

### R12 规则所有权行表与组合测试（P1 的离线部分）

- 新增 `docs/rule-ownership-20260923.md` §1：单位/币种/账本/时间/粒度/实体/证据/输出
  八行所有权表，标注别处是「引用」还是「摘要」，并写明摘要与真源冲突时以真源为准。
- 新增测试（`tests/test_expert_authority_inventory.py`）：
  - 规则引用图必须无环，除已评审的对等互链（`answer-boundary` ↔ `delivery-analysis`）外，
    任何新环都会失败：环意味着读取顺序可能改变口径归属。
  - references 不得携带代码围栏或行首 SQL 语句：方法材料不能变成第二套执行真源。
  - 跨域参考必须写出兼容前提（period/ledger/grain/unit/currency/department）并声明
    自身不是 planner/固定查询序列。
- 未新写重复用例：真实读取、压缩后保留纠正、同口径跨域一致分别复用
  `test_readonly_skill_trace`、`test_host_compaction_e2e`、`test_business_contracts`。

### R15 宿主身份兼容边界（P1 的离线部分）

- 改动 `plugins/datasage-query/entitlements.py`：新增 `session_layer_state()`，
  区分「无会话层/未绑定」与「会话层存在但接口不兼容」（升级改变私有面）。
  不兼容时仍失败关闭，但拒绝理由变为 `session_layer_incompatible`，审计日志可见兼容问题，
  不再与「没有绑定身份」混淆。
- 新增测试（`tests/test_integration_boundaries.py::StrictSessionIdentityTests`）：
  - 两个用户并发读取互不串身份；
  - 同一工作线程在请求结束后不继承上一个绑定（宿主 teardown 后读到空）；
  - 不兼容会话层单独报兼容理由，且普通 CLI（无会话层）保持「无绑定身份」理由；
  - 进程环境变量伪造仍不生效。
- 边界：未改授权规则本身；`_session_value` 语义与既有 fail-closed 行为不变。

### R29 依赖来源/许可/指纹记录（P2 的离线部分）

- 新增 `docs/vendor-provenance-20260923.md`：PyMySQL 1.2.0 来源、许可位置、25 个文件的
  聚合 sha256、更新责任与流程。
- 新增测试（`tests/test_integration_boundaries.py`）：重新计算聚合指纹并与文档比对，
  校验 dist-info 的 Name/Version 与 LICENSE 存在。
- 边界：不声称完成 CVE 清点或供应链安全认证；宿主依赖不在范围内。

### R21 / R02 的宿主路径证据

- 事实：`tests/test_host_compaction_e2e.py` 依赖「完整官方宿主 checkout」。在 Profile 原位运行时
  它通过 `PROFILE_ROOT.parent.parent / "hermes-agent"` 找到宿主；在临时副本中该路径不存在，
  需要显式设置 `HERMES_AGENT_ROOT`。设置后该套件 3/3 通过（含真实宿主链在一次性子进程运行）。
- 因此此前全量回归里的 2 个 error 属副本环境问题，不是代码缺陷；等价环境复跑见 §2。
- 仍未执行：真实渠道工具枚举、办公 Skill 正文与同名覆盖、重启后新会话、跨用户共享 Profile 记忆
  与审批人授权——这些需要真实会话与管理 owner。

## 2. 回归结果

| 运行 | 范围 | 结果 |
| --- | --- | --- |
| B 批等价环境（临时副本 + 父级 `.gitignore` + 已提交 config.yaml） | 1439 项 | 0 失败、2 错误（宿主 checkout 路径，属副本环境）、2 跳过 |
| 同一副本 + `HERMES_AGENT_ROOT` 指向真实宿主 | 1439 项 | OK、1 跳过（0 失败 0 错误） |
| C 批内容（同上环境） | 1447 项 | OK、1 跳过（0 失败 0 错误） |
| 本轮新增用例 | `test_integration_boundaries`（48）、`test_expert_authority_inventory`（11）、`test_source_export`（12） | 全部通过（1 项符号链接环境跳过） |

## 3. 待人工处理的 config.yaml 注释（R10）

将

```
  # The paired host candidate adds native Skill reads without skill_manage.
  # WeCom exposes clarification, governed evidence and official code processing. Every
```

替换为

```
  # WeCom tool surface: native clarification, the three governed DataSage tools
  # and the official `skills` group.  That group contains management entries, so
  # Skill writes are staged behind skills.write_approval and inline shell stays
  # off; the channel exposes no arbitrary code, terminal, file or web tool, and
  # the retired execute_code experiment is not part of the current surface. Every
```

理由：原注释称「不含 skill_manage」并提到「official code processing」，与实测工具面
（clarify + datasage-query + skills，无 code_execution）不符。注意宿主会在网关启动时把
`config.yaml` 重写为无注释规范化版本；提交与注释维护应以此文件为准，重写后需人工复核。

## 4. 未执行（不属于本轮授权或缺少环境/输入）

- R13 渠道工具与办公任务闭环、R14 Skill/Memory 写审批与授权人：需产品/安全 owner 与真实企微审批人。
- R16–R20：需独立业务真值、数据开发与业务 owner 签字；离线不能自证正确。
- R21 的真实会话部分、R22–R26：需真实渠道、数据库、并发与投递环境；R24 为发布前置。
- R27–R31 的真实部署/灰度/回退与成本对照：需发布 owner 与真实运行环境。
- R32：原本缺少 V1.0 旧 50 项清单与证据包（deep_probes.json、selected_test_outcomes.json 等）。
  2026-09-23 用户提供 `DataSage_Profile_Refactor_Checklist_V1.0_20260919.md` 后已完成逐项状态更新，
  见 `docs/legacy-checklist-state-20260923.md`（50 项：源码已改善 15、真实验收待做 30、仍缺陷 1、
  无需采用实验 4）。审查证据包仍未取得，因此「本轮探针」类证据缺失的位置按现有可复跑证据标注。
