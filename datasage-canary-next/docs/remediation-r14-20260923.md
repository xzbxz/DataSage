# R14 核验记录：Skill/Memory 写入审批与授权人

日期：2026-09-23｜依据：综合审查 V2.0 的 R14（批次 C / 发布前置）｜负责角色：安全/运行 owner
前置：R01（已完成）｜性质：核验记录 + 待批配置建议。本文不构成发布批准。

## 1. 结论

- 源码层可用：宿主原生写审批路径存在且测试全绿（24 项）。
- 配置层**不满足 R14 验收条件**：企微侧斜杠命令门控处于「关闭」状态，任何被
  `allow_from: ['*']` 允许的成员都能执行 `/skills pending|approve|reject|approval off`
  与 `/memory ...`。也就是说，普通业务成员可以批准自己触发的知识写入，也可以把
  审批闸门整体关掉。
- 这不是宿主缺陷：宿主按设计「该作用域未配置管理员 = 不启用门控」
  （`gateway/slash_access.py:93`）。本 profile 的 `allow_admin_from` 为空，因此
  门控被判定为未启用。修法属原生配置，不需要改代码。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 原生写审批测试 | 24 项通过 | 宿主 `tests/tools/test_write_approval.py`(15) + `test_skill_manage_batch.py`(9)：`24 passed in 3.92s` |
| 运行方式 | 未改动宿主环境 | 宿主 venv 无 pytest（也不应装入），用 `uv run --no-project --with pytest --with pyyaml --with jsonschema --with requests --with rich --with python-dotenv --python 3.13 -- python -m pytest ... -p no:cacheprovider --basetemp=<Temp>` 运行 |
| 本 profile 权限策略 | dm/group 均 `enabled=False`；访客 `is_admin=True`、`can_run(guest,/skills)=True` | 用宿主自身的 `gateway.slash_access.policy_from_extra` 读取本 profile `config.yaml` 实测 |
| 代码定位 | 空管理员列表即关闭门控 | `gateway/slash_access.py:93`；未启用时 `is_admin` 恒真（`:38-42`）；拒绝路径 `gateway/run_busy.py:964-984` |
| 环境引用可行性 | `${VAR}` 展开覆盖 `allow_admin_from` | `hermes_cli/config.py:1542-1580`；实测 `['${WECOM_BOT_ID}','plain-id']` → `['expanded-value','plain-id']` |

## 3. 四层权限分解（R14 要求区分）

| 层次 | 现状 | 依据 |
| --- | --- | --- |
| 可见 | 业务 Skill 与办公 Skill 可见；约 30 个 bundled Skill 在配置中 disabled | `config.yaml` 的 `skills.disabled` |
| 可调用 | 写入会进入待批准（`skills.write_approval: true`、`memory.write_approval: true`） | `config.yaml:74,77` |
| 可批准 | **任何被允许的企微成员**（门控未启用） | 第 2 节实测 |
| 文件权限 | 企微通道无通用文件/终端工具，写入只能经受管工具；宿主对凭据目录与会话状态另有硬拒绝 | `platform_toolsets.wecom = clarify / datasage-query / skills`；`agent/file_safety.py` 的 `_HERMES_PROTECTED_SUBPATHS` |

## 4. 待批配置建议（原生配置，不新增代码）

### 选项 A — 指定运营 owner 为企微审批人（需要群内审批时推荐）

```yaml
platforms:
  wecom:
    extra:
      allow_admin_from: ["${env:WECOM_APPROVER_USER_ID}"]
      group_allow_admin_from: ["${env:WECOM_APPROVER_USER_ID}"]
```

- 效果：门控启用；非管理员只剩 `help`/`whoami` 与 `new`/`help`/`status`；owner 可在会话内
  审批。
- 真实 user id 放 profile 的 `.env`，不进可分享源码（符合 R29 的「无具体渠道标识」要求）。
- **风险（必须同时加护栏）**：变量未设置时展开为空，`admin_ids` 为空会让门控退回关闭
  （fail-open）。因此该配置落地时必须配一条校验测试：解析后两个作用域都至少有一个管理员。
- 该护栏测试在配置落地前无法通过（现状就是空管理员），所以建议「配置 + 测试」同一次提交。

### 选项 B — 企微侧不留审批人（审批只在本地 CLI 做）

- 用不可匹配的**字面量**占位 id（例如 `cli-only-approver`）使门控启用，但没有任何企微成员
  匹配它；成员只能 `help`/`new`/`status`。
- 审批走本机 CLI 会话内的 `/skills pending|approve|reject`、`/memory pending|approve`。
- 注意占位必须写字面量：写成 `${env:...}` 时未设置会退回门控关闭。

### 选项 C — 保持现状

不满足 R14 验收条件。风险：成员可 `/skills approval off` 关闭写入审批，随后让机器人写共享
业务知识；治理声明与实际能力不符。

## 5. 需要人工完成的真实验证（离线无法替代）

| # | 步骤 | 谁 | 期望表现 |
| --- | --- | --- | --- |
| 1 | 选定 A 或 B；选 A 时提供运营 owner 的企微 user id | 用户/运营 owner | 决策记录 |
| 2 | 落地配置变更（含空管理员护栏测试） | 助手，需授权 | 一次提交 |
| 3 | 非 owner 账号在测试会话执行 `/skills pending`、`/skills approval off` | 测试成员 | 被拒绝，机器人回复拒绝提示，`gateway.log` 出现 `denied ... (not admin, not in user_allowed_commands)` |
| 4 | owner 执行 `/skills pending`，再 `approve` / `reject` 一次 | 运营 owner | 批准生效且文件变化；拒绝后文件不变 |
| 5 | 三类变更分别验证：profile 共享目录、外部目录、同名 Skill | 测试成员 + owner | 均进入待批准；未经批准不落盘 |
| 6 | 前后哈希对比（下方命令）留回执 | 执行人 | 拒绝路径哈希不变 |

```text
cd C:/Users/10192/AppData/Local/hermes/profiles/datasage-canary-next
sha256sum $(find skills -type f -name '*.md' | sort) memories/MEMORY.md memories/USER.md
```

## 6. 回退

- 配置回退：恢复 `allow_admin_from`/`group_allow_admin_from` 为空即回到「门控关闭」现状。
- 本项不改变查询能力、合同或会话数据；不涉及恢复任何越权写入能力。
