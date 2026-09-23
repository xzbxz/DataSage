# R13 核验记录：渠道工具与办公 Skills 的任务闭环

日期：2026-09-23｜依据：综合审查 V2.0 的 R13（批次 C / P1）｜负责角色：产品 owner＋运行管理员
前置：R02、R14（已完成）｜性质：能力面核验 + 一处待批的最小配置改动

## 1. 结论

- 企微渠道实测只有 **7 个工具**：`clarify`、`datasage_catalog`、`datasage_entity_resolve`、
  `datasage_query`、`skills_list`、`skill_view`、`skill_manage`。文件、终端、代码执行、
  网络检索、记忆、委托等工具集都不在通道内。
- 五类承诺按此拆分：**问数**与**深度分析**可支持（后者受证据边界约束）；
  **用户文件**、**外部研究**不支持；**报告**只支持会话内文本/表格形式，文件产出不支持。
- 本 profile 有 6 个启用 Skill，其中只有 `datasage` 能在企微执行；另外 5 个
  （`docx`、`xlsx`、`pdf`、`powerpoint`、`hermes-agent`）的指令需要 shell 或文件工具
  （例：docx 要求 `python scripts/docx_create.py …`），在企微里**可见但不可执行**——
  这正是 F09 验收点「Skill 可见但根本无工具执行还说已完成」的结构性来源。
- 已落地：把能力边界写进 Skill（面向模型的行为约束）＋一条守卫测试，使「文档承诺」与
  「实测工具面」绑定，任一侧漂移都会失败。
- 已落地（2026-09-23，按建议）：用原生 `skills.platform_disabled.wecom` 把 4 个办公 Skill
  从企微索引中隐藏（`docx`、`pdf`、`powerpoint`、`xlsx`），本机 CLI 保留。
  **`hermes-agent` 隐藏不了**：宿主把它列为 ESSENTIAL 并从所有禁用集合中减去
  （`agent/skill_utils.py:270,285`），因此它只能由第 4 节的文字边界覆盖。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 声明的工具集 | `['clarify', 'datasage-query', 'skills']` | 本 profile `config.yaml` 的 `platform_toolsets.wecom` |
| 展开后的工具 | 7 个（见上） | 宿主 `toolsets.TOOLSETS` 解析 + 插件真实 `register()` 输出 |
| 通道外工具集 | web / file / terminal / code_execution / vision / tts / memory / session_search / todo / delegation 全部不在通道内 | 同上展开结果 |
| Skill 可执行性扫描 | 启用 6 个、可执行 1 个、受阻 5 个 | 逐个 `SKILL.md` 扫描 shell/文件/代码/网络依赖信号；办公 Skill 明确要求本地运行 `python scripts/…` |
| 写入审批 | Skill 写入仍进待批准（R14） | `skills.write_approval: true`；R14 记录 |
| 隐藏后的按平台解析 | wecom 禁用 84 个（全局 80 + 办公 4）；cli 与未指定平台仍为 80，4 个办公 Skill 未被禁用；`hermes-agent` 在任何平台都未被禁用 | 宿主 `agent.skill_utils.get_disabled_skill_names(platform)`，`HERMES_HOME` 指向本 profile |

## 3. 逐任务承诺矩阵

| 任务类型 | 企微承诺 | 实际路径 | 成功场景 | 不足场景（要明确说，不得声称完成） | 转授权 |
| --- | --- | --- | --- | --- | --- |
| 问数（指标事实 / 对比 / 排名） | 支持 | `clarify` + 三个受管数据工具 | 返回带期间、口径、typed state 的数值 | 指标未注册、实体歧义、期间无覆盖、凭据缺失 → 明确拒绝或要求澄清 | — |
| 深度分析（诊断 / 分解 / 跨域） | 支持，受证据边界约束 | 同上，必要时按需读取 references | 结构贡献与因果分开，给出可验证的下一步 | 需要自由计算、原始明细或外部数据时说明限制 | 复杂离线建模交 operator |
| 用户文件（附件 / 用户表） | **不支持** | 通道无文件工具 | — | 明确说明本渠道无法读取附件或文件 | operator 本地处理 |
| 报告与产物 | **部分**：会话内文本/表格可；文件产出不支持 | 文本报告直接回复；文件需终端或代码执行 | 结构化文本报告 | 说明本渠道不能产出 xlsx/docx/pdf/pptx | operator 用既有受控脚本在本地产出 |
| 外部研究（公开信息检索） | **不支持** | 通道无网络工具 | — | 明确说明不在本渠道承诺内 | 走 CLI 或 operator |

发送路径：通道内只能发送文本消息；适配器本身支持媒体发送，但通道里没有能产出文件的工具，
因此实际不存在「会话内产出附件」的路径。

## 4. 已落地的行为约束

- `skills/business-analytics/datasage/references/answer-boundary.md` 新增
  **Channel capability boundary**：说明「能力属于会话而非 profile」，逐条列出企微可用面、
  不可用面（用户文件 / 文件产出 / 外部研究 / 自由代码与 SQL），并要求明确说明限制、
  给出 operator 路径、**绝不把做不到的任务说成已完成**。
- `SKILL.md` 新增 **Channel capability** 小节（始终加载）指向上述章节。
- 守卫测试 `test_wecom_surface_matches_the_documented_capability_boundary`
  （`tests/test_integration_boundaries.py`）断言：声明的工具集列表、展开后的 7 个工具、
  「文档声明不可用」的那些能力对应的工具不得出现在通道面、以及边界文本必须包含那几条承诺。
  任一侧漂移（例如日后给企微加了文件工具）都会失败并要求同步修改。

## 5. 已落地的配置改动

```yaml
skills:
  platform_disabled:
    wecom: [docx, pdf, powerpoint, xlsx]
```

- 效果：这 4 个办公 Skill 不再出现在企微的 Skill 索引里；本机 CLI 不受影响（它们不在全局
  `disabled` 里，资产保留、operator 路径照旧）。
- 理由：实测它们的指令依赖企微没有的 shell/文件工具；隐藏可结构性消除「看起来能做、
  实际做不了」的误导路径。
- `hermes-agent` 无法隐藏：宿主 `agent/skill_utils.py:270` 定义
  `ESSENTIAL_SKILLS = {"hermes-agent"}`，第 285 行会把该名字从最终禁用集合里减去。
  因此它对企微仍可见，其限制由第 4 节的文字边界负责表述。
- 该键是宿主原生能力（`skills.platform_disabled.<platform>`），不需要新代码，也不新增
  任何宿主权限。
- 守卫测试 `test_wecom_hides_the_skills_it_cannot_execute` 断言：隐藏列表恰好是这 4 个、
  不得与全局 `disabled` 重叠（CLI 必须保留）、且不得把 `hermes-agent` 写进去。

## 6. 待人工验证（V04 / V05 / V21）

| # | 步骤 | 期望 |
| --- | --- | --- |
| 1 | 企微里发一份 Excel 附件并问「帮我分析这份表」 | 明确说明本渠道读不了附件，并给出本地处理路径，不假装完成 |
| 2 | 发「把上面的数据导成 Excel 发我」 | 说明不能产出文件；可给文本表格 |
| 3 | 发「帮我查一下行业公开数据」 | 说明本渠道无外部检索能力 |
| 4 | 发一个正常问数问题 | 正常返回带口径与期间的数值（回归） |
| 5 | 重启网关后发「Skill 有哪些」 | 列表中不再出现这 4 个办公 Skill（`hermes-agent` 仍会列出，属宿主保留项） |

回执（对话截图或原文）交回后，R13 才算完成真实验收部分。

## 7. 回退

- 隐藏改动：删掉 `skills.platform_disabled.wecom` 即恢复现状。
- 文本改动：`SKILL.md` 与 `answer-boundary.md` 的边界章节可单独回退。
- 查询能力、合同、会话数据均不受影响。
