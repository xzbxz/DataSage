# V5 补丁落地核验记录（system-review-20260924）

日期：2026-09-24｜来源：外部综合复核交付 V5「源码修复批次」｜负责角色：集成 owner＋发布 owner
性质：落地与**逐文件差异核对 + 本机验证**；本批不是宿主认证或上线批准，发布决定仍归发布 owner

## 1. 结论

- V5 的来源标记 `8c5b833e…5b68e` **正是我方当前提交**，因此它是在我方状态之上叠加的补丁：
  373 个受跟踪文件我这边全有、**0 个被删除**、新增 1 个（V5 复核记录）、改动 17 个。
- 改动分两类：**插件侧六组修复**与**我方测量脚本的跨平台补强**（后者与我 R28/R30 的方向一致，采纳）。
- 它的白名单只做**新增**（3 行，无删除），我按交付版本直接采用；新记录已可入库。
- 我方落地只需重生成维护登记册（文件集变化），context 登记册原样即通过（说明它的归一化修复到位）。

## 2. 哈希链

| 对象 | SHA256 |
| --- | --- |
| V5 交付包 `DataSage_System_Reviewed_Patched_V5_20260924.zip` | `156a84a6…75fb`（本机实测） |
| V5 自述的输入包 `DataSage-main(2).zip` | `2d204a31…1cb89`（对方记录，本机无此文件） |
| 来源标记 | `8c5b833e6399185fcc85d3fec7c887a64005b68e` = 我方改前提交 `8c5b833` |

## 3. 插件侧六组修复（照 V5 自述）

| 组 | 内容 | 落地文件 |
| --- | --- | --- |
| 1 | 纯规则导入与密钥访问解耦：`settings.get_secret` 在**实际取凭据时**才转调官方宿主；不读进程环境、不缓存；缺宿主仍抛错不静默取默认 | `settings.py`、`db_runtime.py`、`db_security.py`、`runtime_health.py` |
| 2 | 报告完整性必须匹配**真实行**：声明行数与实际数组比对；缺集合、非法行、空状态有行、内嵌错误、数据包错误不得被 success 覆盖 | `result_completeness.py` |
| 3 | 同一结果从门禁到报告一致：原始 ledger 与紧凑 rows 按同一集合消费，不伪造空数组 | `result_completeness.py`、`fabric_report.py` |
| 4 | 分组数不得冒充源行数：`ranking_evidence.population_count` 只用于分组口径 | `result_completeness.py` |
| 5 | 图表数值与布局：比例条用 Decimal 比值避免有限大数转 float 溢出；负值与未知不伪装成零 | `fabric_report.py` |
| 6 | 上下文测量跨平台：文本按 LF+UTF-8 归一化测量 | `tests/context_cost.py`、`tests/maintenance_cost.py` |

## 4. 我方测量脚本被采纳的补强

| 文件 | 补强 |
| --- | --- |
| `tests/context_cost.py` | 报告正文按**归一化换行**测量（我此前只归一化了 JSON，正文仍取原始字节大小——这是它抓到我的一处小缺陷） |
| `tests/maintenance_cost.py` | 路径排序改用**POSIX 大小写敏感**键，避免 Windows 与 POSIX 排序不同导致登记册顺序漂移 |
| 两个登记册 | 按新口径重测（context 正文 295→291 字节；维护面顺序按 POSIX 键） |
| 两个守卫 | 各加一条跨平台用例（LF/CRLF 成本一致；路径顺序跨平台一致） |

## 5. 验证

| 项目 | 结果 |
| --- | --- |
| V5 要求的 6 项复核 | 全部通过（`test_revision_regressions`、`test_report_completeness`、`test_workflow_report_gate`、`test_report_presentation`、`test_context_cost_baseline`、`test_source_export`）；1 项 Windows 符号链接跳过 |
| 测量登记册 | context 原样通过；maintenance 因文件集变化重生成后通过 |
| 文件集核对 | 我方可入库 docs 28 个 = V5 包内 28 个，无差异 |
| 新增测试 | 18 个新测试方法（密钥解耦 3、报告完整性 9、门禁 1、跨平台测量 2、图表/精度 3） |
| 全量回归（副本） | **1574 项通过 / 3 跳过 / 0 失败**（1556 + 18 新增，算术对得上） |
| 运行态 | 未触碰：Home、`.env`、会话、Memory、state、调度均未改 |

## 6. 边界（与 V5 自述及既有记录一致）

官方宿主、真实模型/数据库/企微、业务真值、ETL 水位、真实多轮与发布背书仍未接入；V5 明确不是宿主认证、
SQL 实数验真、业务签字或上线批准。这些继续归入既有验收任务，不新增运行框架替代证据。

## 7. 回退

回退本批源码与说明即可（`git revert` 本批提交），**不覆盖**运行 Home、凭据、state 或用户后来产生的数据；
回退路径已在灰度计划的演练中验证过。
