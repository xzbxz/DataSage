# R14 核验记录：Skill/Memory 写入审批与授权人

日期：2026-09-23｜依据：综合审查 V2.0 的 R14（批次 C / 发布前置）｜负责角色：安全/运行 owner
前置：R01（已完成）｜性质：核验记录 + 待批配置建议。本文不构成发布批准。

## 1. 结论

- 源码层可用：宿主原生写审批路径存在且测试全绿（24 项）。
- 配置层**已修复（2026-09-23，选项 A 落地；id 形式已按运行日志纠正）**：
  `allow_admin_from` 与 `group_allow_admin_from` 指定运营 owner 的企微身份，两个作用域的
  斜杠门控均启用；非管理员只剩 `help`/`whoami` 与 `new`/`help`/`status`，无法再自批或关闭闸门。
- **id 形式务必用适配器实际上报的那个**：本机器人是「智能机器人」形态，回调里的
  `from.userid` 是 open-userid（`wo...` 形式），不是企业内 userid。首轮按企业内 id 配置后，
  重启后的真实日志显示**所有成员（含 owner 本人）都被拒**——门控生效但没人能批准，
  等同于选项 B。已改为日志与 `/whoami` 一致的那个 id，并用宿主解析器复核 owner 在两个
  作用域均为 admin、访客为 user。
- 修复前的问题（已复现）：`allow_admin_from` 为空时宿主按设计判定「该作用域未配置管理员
  = 不启用门控」（`gateway/slash_access.py:93`），任何被 `allow_from: ['*']` 放行的成员都会
  被当作管理员，可执行 `/skills pending|approve|reject|approval off` 与 `/memory ...`。
- 仍未完成的部分是需要真人在真实企微会话里执行的验证（第 5 节），本项在这些步骤完成前
  不能标记为完全通过。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 原生写审批测试 | 24 项通过 | 宿主 `tests/tools/test_write_approval.py`(15) + `test_skill_manage_batch.py`(9)：`24 passed in 3.92s` |
| 运行方式 | 未改动宿主环境 | 宿主 venv 无 pytest（也不应装入），用 `uv run --no-project --with pytest --with pyyaml --with jsonschema --with requests --with rich --with python-dotenv --python 3.13 -- python -m pytest ... -p no:cacheprovider --basetemp=<Temp>` 运行 |
| 本 profile 权限策略 | dm/group 均 `enabled=False`；访客 `is_admin=True`、`can_run(guest,/skills)=True` | 用宿主自身的 `gateway.slash_access.policy_from_extra` 读取本 profile `config.yaml` 实测 |
| 代码定位 | 空管理员列表即关闭门控 | `gateway/slash_access.py:93`；未启用时 `is_admin` 恒真（`:38-42`）；拒绝路径 `gateway/run_busy.py:964-984` |
| 环境引用可行性 | `${VAR}` 展开覆盖 `allow_admin_from` | `hermes_cli/config.py:1542-1580`；实测 `['${WECOM_BOT_ID}','plain-id']` → `['expanded-value','plain-id']` |
| 重启后的运行态（第一次修复） | 门控已生效，但连 owner 也被拒 | `logs/gateway.log`：`2026-09-23 16:00:02,379 INFO gateway.run: Slash command /skills denied for wecom:wo8gxKDAAA0e36LXk9vXJ_nmbnmE2gzg (not admin, not in user_allowed_commands)` |
| 网关加载确认 | 重启 15:59:15 晚于配置改动 15:49:57；单实例 | `gateway_state.json`（pid 18780、`gateway_state: running`、wecom `connected`、code_sha 095947d8 / 0.21.1）；两 PID 为同一启动链父子 |
| id 形式纠正后复核 | dm/group 均 `enabled=True`；owner `is_admin=True`、可跑 `/skills` 与 `/memory`；访客被拒 | 宿主 `policy_from_extra` 实测 |

## 3. 四层权限分解（R14 要求区分）

| 层次 | 现状 | 依据 |
| --- | --- | --- |
| 可见 | 业务 Skill 与办公 Skill 可见；约 30 个 bundled Skill 在配置中 disabled | `config.yaml` 的 `skills.disabled` |
| 可调用 | 写入会进入待批准（`skills.write_approval: true`、`memory.write_approval: true`） | `config.yaml:74,77` |
| 可批准 | **任何被允许的企微成员**（门控未启用） | 第 2 节实测 |
| 文件权限 | 企微通道无通用文件/终端工具，写入只能经受管工具；宿主对凭据目录与会话状态另有硬拒绝 | `platform_toolsets.wecom = clarify / datasage-query / skills`；`agent/file_safety.py` 的 `_HERMES_PROTECTED_SUBPATHS` |

## 4. 待批配置建议（原生配置，不新增代码）

> 状态：选项 A 已于 2026-09-23 落地并提交；B/C 未采用。以下保留三选项原文以便复核。

### 选项 A — 指定运营 owner 为企微审批人（**已落地，2026-09-23**）

```yaml
platforms:
  wecom:
    extra:
      allow_admin_from: ["wo8gxKDAAA0e36LXk9vXJ_nmbnmE2gzg"]        # 运营 owner（DM）
      group_allow_admin_from: ["wo8gxKDAAA0e36LXk9vXJ_nmbnmE2gzg"]  # 运营 owner（群）
```

- **必须是 open-userid 形式**：智能机器人回调只给 `from.userid` 的 open-userid；企业内 userid
  （如 `zhangzhengwei`）永远不会匹配，写成它会让所有成员（含 owner）一起被拒。
  确认方法：在目标账号发 `/whoami`，回复里的 `User ID` 就是该值，日志里的拒绝行也带同一个值。
- 效果（用宿主自身的 `policy_from_extra` 实测）：dm 与 group 均 `enabled=True`、
  管理员仅该 id；访客 `is_admin=False`、`can_run(访客,/skills)=False`、
  `can_run(访客,/memory)=False`，而 `new`/`help`/`status` 仍可用。
- 采用**字面量**而非 `${env:...}`：变量未设置时展开为空会让 `admin_ids` 为空、门控退回
  关闭（fail-open）。配套测试 `test_wecom_slash_admin_gate_is_enabled_for_both_scopes`
  断言两个作用域都至少有一个管理员、且该位置不得使用环境引用；该测试在配置落地前为红色。
- 该 id 属身份信息，分享物不得携带：导出器（`scripts/datasage_source_export.py`）现把
  `allow_admin_from`/`group_allow_admin_from` 替换为 `${WECOM_APPROVER_USER_ID}`，
  与 `home_channel` 的处理一致；导出测试断言真实 id 不出现在模板中。

### 选项 B — 企微侧不留审批人（审批只在本地 CLI 做）｜未采用

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
| 1 | 选定 A 或 B；选 A 时提供运营 owner 的企微身份 | 用户/运营 owner | ✅ 已完成：选 A，id = `wo8gxKDAAA0e36LXk9vXJ_nmbnmE2gzg`（open-userid） |
| 2 | 落地配置变更（含空管理员护栏测试） | 助手，需授权 | ✅ 已完成：配置 + 护栏测试 + 导出器脱敏同批提交 |
| 2b | **再次重启网关**，随后由 owner 发 `/whoami` | 用户 | 预期 `Tier: **admin**`；若不是 admin，说明 id 又不对，先停下来找原因 |
| 2c | 非 owner 账号发 `/whoami` | 测试成员 | 预期 `Tier: user`，可运行命令仅 `/help`、`/whoami`、`/new`、`/status` |
| 3 | 非 owner 账号在测试会话执行 `/skills pending`、`/skills approval off` | 测试成员 | 被拒绝，机器人回复 `⛔ /skills is admin-only here. …`，`gateway.log` 出现 `denied ... (not admin, not in user_allowed_commands)` |
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
