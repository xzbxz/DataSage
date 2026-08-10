# datasage-canary-next 安全审计与整改记录（2026-08-07）

本文件记录一次针对 datasage-canary-next profile 的安全审查及后续整改的
完整事实基线，供后续会话继续整改时直接引用（避免重新审计）。

## 审查基线

- 全树 762 文件 / 约 14.4MB；100 Python、21 YAML、17 JSON、337 Markdown
  解码/语法扫描无解析错误。
- 插件测试 92 项：91 通过，1 项因安装包缺少 `evaluation/` 报错。
- 六个业务域 12 次动态 catalog 检查通过；PyMySQL 1.2.0 wheel hash 与
  vendored RECORD 全部核验一致（无篡改）。
- 机器上有 6 个 profile，其他 profile 约 1,133 会话 / 43,927 消息
  （datasagecore 932 会话/23,851 消息、datasage 181/19,826）。

## P0 发现（子代理交叉复核后全部属实）

1. **WeCom 全员可检索其他用户/profile 会话**（config.yaml wecom toolsets
   含 session_search；session_search_tool.py 无 user_id/chat_id 过滤，
   有 profile 跨档参数）。
2. **Cron 提权链**：wecom 含 cronjob；cronjob_tools.py 允许模型自指
   enabled_toolsets（无服务端约束）；scheduler.py per-job 优先且
   platform_toolsets.cron 未配置时回退全量（含 terminal/file/code）。
3. **执行契约可读可改**：datasets/entity-registry/query-policy.yaml 曾
   在 skills/common-data-foundation/references/ 下，skill_view 可读、
   skill_manage 可覆盖（write_approval=false）。
4. **明文+高权限连真实库**：canary_accept_existing_account=true、
   require_tls=false；agent.log 实锤 plaintext + ALL PRIVILEGES +
   查询成功。
5. **发布身份断链**：distribution.yaml 声明 .release 但缺失；
   runtime_health 非 production 下返回 ready=true/state=source。

## 业务口径问题

- 目标完成率：semantics 声明"0至1"但实现直接 actual/target 无截断
  （可 >1、可为负）。
- Catalog 承诺维度组合超编译器能力（planner 说"最多五个"但实现
  settlement 2 维 / turnover 1 维）；metric detail 不返回组合上限。
- planner-contract guidance 构建后除校验外零消费（孤儿权威）。
- 渐进加载失效：简单查询同时加载 datasage 根 + common root。
- 暗伤：cronjob.attach_to_session 注册 handler 未转发（静默忽略）；
  无效端口静默退回 3306（fail-open）；system_prompt 拼出重复
  profiles/<profile> 路径；日志 INFO 存业务问题原文；轮转按大小非天数。

## 已完成的整改（2026-08-07）

- **企微工具集裁剪**：wecom = datasage-query/clarify/todo（移除 web/
  browser/vision/image_gen/tts/skills/memory/session_search/delegation/
  cronjob）；cron = datasage-query；allow_admin_from = zhangzhengwei；
  allow_from 保持 '*'（业务决策：全员可用，靠裁剪兜底）。备份在
  config.yaml.bak-20260807。
- **上下文膨胀控制**：compression.enabled=true / threshold=0.5 /
  target_ratio=0.2；datasage/SKILL.md 增加分析类规划规则（为什么类优先
  complete_change_decomposition、排名每轮最多 1 个且 limit≤10、批次≤3）；
  tools.py 排名查询 limit 上限 10（order_by 存在时），普通查询保持 100。
- **契约迁移**：9 个执行契约（datasets/entity-registry/query-policy +
  6 域 semantics）移入 plugins/datasage-query/contracts/；更新
  contracts.py / entities.py / tools.py 路径常量；CONTRACT_INDEX.md 与
  common-data-foundation/SKILL.md 声明同步；planner-contract.yaml 与
  expert-playbooks.yaml 保留在 skills（模型可见的规划指引）。
- **完成率口径对齐**：target-semantics.yaml 两处 unit_policy 改为
  "可为负，可超过1，不做截断"；planner-contract 加不截断规则；
  test_runtime_hardening.py 断言更新 + 2 个回归用例（语义声明检查 +
  SQL 无 LEAST/GREATEST 夹取）。真实查询验证：HCM 7 月完成率 130.51%，
  回答正确说明"不做截断"。
- **发布身份 RELEASE.json（P0-5 完成）**：hermes-agent HEAD 无 tag 导致
  身份校验失败（_expected_tag_present=False），先在 HEAD 打本地 tag
  （git tag datasage-hermes-v0.19.0-dev40），再生成
  .release/RELEASE.json，hermes_source 四字段从 git 读取
  （rev-parse HEAD / HEAD^{tree} / tag --points-at / git show
  HEAD:uv.lock 的 sha256），distribution_version 取 distribution.yaml 版本，
  payload_sha256 为 distribution_owned 清单内容哈希。重启后日志
  identity_state 从 source 变 installed，identity_expected==actual
  （指纹一致）。注意：hermes-agent 更新后需重新打 tag + 重新生成。
- **日志降敏（D 完成）**：agent/redact.py 新增
  redact_message_for_log(text)——privacy.redact_pii=true 时返回
  "[redacted len=N hash=H]" 指纹（hashlib.sha256 前 16 位），关闭时保持
  原 80 字符截断预览；turn_context.py 的 conversation-turn 日志与
  gateway/run.py 的 inbound-message 日志改用该函数。config 轮转收紧
  logging.max_size_mb=5 / backup_count=3。验证：新进程日志
  msg='[redacted len=14 hash=...]'，旧 CLI 会话进程不重启仍走旧代码。
- **E 项工程小修（完成）**：cronjob_tools.py registry handler 补
  attach_to_session 转发（schema/签名/create/update 都有但 handler 漏传，
  调用被静默丢弃）；tools.py _connection_port 改 fail-closed（非数字/
  越界/0 抛 QueryFailure，仅未配置默认 3306）；profile 根建 pytest.ini
  （testpaths + addopts）；staging 清理 104 pyc / 25 __pycache__ /
  .curator_state / .usage.json* / .hub/。

## pytest.ini 关键坑（E3 实测）

- **pytest 9.0.2 忽略 ini 里的 `import-mode = importlib` 键**（-o
  import_mode= 同样不生效），必须把 flag 放进 `addopts`：
  `addopts = --import-mode=importlib -p no:cacheprovider`。
- 只放 testpaths + import-mode 键时仍是 prepend 模式 → 连字符包名
  相对导入收集错误（94 errors）。
- Windows 下 pytest 默认 basetemp（Temp\pytest-of-<user>）可能
  PermissionError WinError 5，加 `--basetemp=<writable>` 修复（该权限
  问题与代码无关，不是技能要记的失败，修复方式是 --basetemp）。

## 测试基线（整改后）

- `PYTHONPATH=<profile root> python -m pytest plugins/datasage-query/tests
  -q -p no:cacheprovider --import-mode=importlib` → 93 passed /
  1 failed（唯一失败 = evaluation/expert-core/cases.yaml 缺失，安装包
  排除所致，与整改无关）。
- 默认 prepend 模式会因连字符包名产生相对导入收集错误，必须
  --import-mode=importlib。

## 待办（按优先级）

- 数据库只读账号 + TLS（P0-4，用户暂时略过，但企微真实流量已在明文高
  权限上运行，优先级最高）。
- 孤儿权威二选一（planning_rules 投影或收敛）；维度组合上限版本化
  （max_group_dimensions，catalog 与编译器同一权威）；evaluation 纳入
  分发或 skip；渐进加载失效（common-data-foundation 触发词/降级）。
- 后续 hermes-agent 更新后需重新打 tag + 重新生成 RELEASE.json。

## 关键文件路径

- config: C:/Users/10192/AppData/Local/hermes/profiles/datasage-canary-next/config.yaml
- 契约: 同根 plugins/datasage-query/contracts/
- 插件: 同根 plugins/datasage-query/{contracts,entities,tools,analytical_queries}.py
- 日志: 同根 logs/{agent,gateway,errors}.log
- 技能: 同根 skills/datasage/SKILL.md、skills/target-query/references/planner-contract.yaml
