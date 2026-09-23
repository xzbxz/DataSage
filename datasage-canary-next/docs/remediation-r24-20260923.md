# R24 核验记录：数据库、授权与时效由外部 owner 闭环

日期：2026-09-23｜依据：综合审查 V2.0 的 R24（批次 E / 发布前置）｜负责角色：DBA＋平台管理员＋数据 owner
前置：R01、R15（已完成）｜性质：配置事实固化 + 外部核验清单；闭环动作必须由外部 owner 完成

## 1. 结论（含一条必须先摆上台面的事实）

- **当前 profile 处于 canary 模式**：`production_mode: false`、`require_tls: false`。
  按执行层实现，`tls_required = production_mode or require_tls`（`db_security.py:331`），
  因此现在**明文传输被允许**；同时 canary 允许项均为 true：特权账号允许、端口不一致允许
  （配对 `3306:3002`）、接受既有账号；`production_mode` 为 false 时**不强制服务端 UUID 白名单**
  （`db_security.py:543`）。运行健康会带上 `datasage_database_plaintext_transport
  production_mode=false` 这一状态码。
- 按 R24 验收条件「实际未验或风险未接受不得正式全员发布」，**当前不具备全员发布条件**：
  传输安全、服务端身份、账号权限、可见范围、时效与保留周期都还没有外部证据。
- 本轮把要外部出具的清单固化为**可校验的登记册**（8 项，全部 `pending`、`accepted` 为 0），
  并让登记册引用的配置声明与线上配置**逐条比对**：任何开关被改动而登记册没跟上都会让测试变红。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| canary 模式事实 | `production_mode=false`、`require_tls=false`、canary 允许项全为 true、`mysql_allowed_grant_scopes=[]` | 本 profile `config.yaml` 的 `plugins.entries.datasage-query.settings` |
| 传输语义 | `tls_required = production_mode or require_tls`；明文状态码出现在运行时健康 | `plugins/datasage-query/db_security.py:331`、`db_runtime.py:389` |
| 服务端白名单语义 | 仅 `production_mode` 时强制 | `db_security.py:543` |
| 登记册结构 | 8 项全部具备领域/责任人/请求/证据格式/状态 | `tests/fixtures/external_verification_register.json` |
| 配置声明不漂移 | 逐条比对线上配置（含 `require_tls`、`production_mode`、`dm_policy`、`model.provider`） | `tests/test_external_verification_register.py::test_configuration_claims_match_the_live_configuration` |
| 未验不得标通过 | `accepted` 必须有独立证据记录；`pending` 不得携带证据；摘要与条目一致 | 同文件 4 项守护用例 |
| 不含秘密 | 登记册内不得出现渠道地址、密钥占位符、长串字面量 | 同文件 `::test_register_carries_no_secret_values` |

## 3. 登记册清单（8 项）

| id | 领域 | 责任人 | 需要出具 | 登记册引用的配置声明 |
| --- | --- | --- | --- | --- |
| db_account_grants | 数据库授权 | DBA | 只读核验：SELECT 且无 GRANT OPTION、可见范围、有效期（SHOW GRANTS 原文） | `mysql_allowed_grant_scopes = []` |
| tls_transport | 传输安全 | DBA＋平台管理员 | 服务端 TLS/证书身份；开启决定或**书面风险接受**（当前明文被允许） | `require_tls = false` |
| server_identity_allowlist | 服务端身份 | DBA＋平台管理员 | 服务端 UUID 与白名单生效证据 | `production_mode = false` |
| gateway_visibility_scope | 网关可见范围 | 平台管理员 | 实际可达成员/群清单与政策对照（含差异处置） | `dm_policy = allowlist` |
| data_refresh_times | 数据时效 | 数据开发 | 逐源刷新时点、允许最大延迟、保留期 | — |
| log_and_artifact_retention | 日志与产物保留 | 数据 owner＋平台管理员 | 保留周期/访问范围/销毁方式（与 R22 授权范围一致） | — |
| model_provider_data_scope | 模型供应方数据范围 | 平台管理员＋安全 owner | 供应方对输入的处理与保留 + 实际模型配置 | `model.provider = deepseek` |
| authorized_data_scope | 授权数据范围 | 业务数据 owner | 共享渠道可见的业务数据范围与授权依据 | — |

## 4. 与验收条件的对应

| R24 验收条件 | 本轮状态 |
| --- | --- |
| 已批准的共享数据政策可追溯 | 登记册 + 逐项证据位；当前 `accepted = 0`，未验项不含证据 |
| 运行探针与配置一致 | 配置声明与线上配置逐条比对（自动校验）；运行探针见 `runtime_health` |
| 实际未验或风险未接受不得正式全员发布 | 登记册 `policy` 明确「pending 项阻止全员发布」；当前 canary 事实已记录 |
| 不把未自建 RBAC 判缺陷 | 未自建权限平台，仅要求外部核验现有授权 |
| 不自动改账号/网络/生产权限 | 助手未改任何账号、TLS、网络或生产权限 |

## 5. 需要谁做什么（可直接转发）

1. **DBA**：`SHOW GRANTS` 原文 + 账号/主机标识；服务端 TLS 配置与证书身份；服务端 UUID。
2. **平台管理员**：企微实际可达成员/群清单；模型供应方条款与本 profile 实际模型标识；日志保留与访问范围。
3. **数据开发**：逐源刷新时点与允许最大延迟。
4. **业务数据 owner**：共享渠道可见的数据范围与授权依据；对 canary 传输状态给出「开启 TLS」或「书面接受风险」的决定。

回执交回后：把证据记录（kind/reference/sha256/approved_by/approved_on）填进登记册对应项，
`status` 置 `accepted`，并重跑 `tests/test_external_verification_register.py` 通过即为闭环。

## 6. 回退

- 登记册与守卫可整体移除；不涉及任何账号、网络或生产权限变更。
- 若某外部核验结论为「风险不接受」，按报告要求限制使用范围或暂停发布，不通过放宽边界来消除告警。
