# R17 核验记录：粒度、单位、时点与跨源边界

日期：2026-09-23｜依据：综合审查 V2.0 的 R17（批次 D / P1）｜负责角色：数据开发＋业务复核
前置：R12、R16（已完成）｜性质：既有覆盖复跑 + 缺口界定；真实库抽样仍待数据方

## 1. 结论

- R17 列出的 11 个边界维度里，**8 个已有离线覆盖并已真实复跑通过**（本轮 198 项，0 失败），
  依据同时落在合同/引擎规则与既有用例两侧。
- **1 个部分覆盖**（跨月退货）：合同有退货结算期披露、离线用例存在，但真实跨月样本未验。
- **2 个没有引擎侧语义**（迟到数据、多源水位）：现有设计只做「不宣称时效」，
  没有「迟到行归到哪个期间」的规则；这属于需要数据 owner 先定口径、再用真实数据取证的项。
- 本轮**未新增测试**：报告明确要求已有覆盖先真实复跑、不重复写一份；缺口里也没有
  可纯离线验证却没人覆盖的例子（新增只会重复既有断言）。

## 2. 复跑结果（本轮，`HERMES_AGENT_ROOT` 指向锁定宿主）

| 套件 | 结果 | 主要覆盖维度 |
| --- | --- | --- |
| `test_business_contracts.py` | 80 项 OK | 期间/时点、状态化、账本、币种、单位 |
| `test_analysis_evidence.py` | 21 项 OK | 快照不跨月相加、缺失月份不补齐、记录粒度与覆盖分母 |
| `test_delivery_l3_semantics.py` | 7 项 OK | 单位、退货结算期、币种策略 |
| `test_delivery_l3_entity_time.py` | 13 项 OK | 实体归属与时间口径 |
| `test_delivery_l3_runtime.py` | 11 项 OK | 运行时单位/范围 |
| `test_customer_history.py` | 25 项 OK | 当前主数据、重复主数据不放大 |
| `test_result_identity_currency.py` | 12 项 OK | 原币/RMB、结果身份 |
| `test_c01_c02_public.py` | 15 项 OK | 两个账本各自对账 |
| `test_remediation_analytical_integrity.py` | 14 项 OK | 分析证据完整性 |
| **合计** | **198 项 OK，0 失败** | — |

## 3. 逐维度矩阵

| # | 维度 | 合同/引擎依据 | 现有覆盖 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 1:N 与 N:N 去重 | `contracts/datasets.yaml:155`：先聚合再连另一事实以避免放大；指标维度只支持受管的 many-to-one 主数据连接 | `test_customer_history.py::test_same_name_missing_master_and_duplicate_master_no_amplification`；`test_analysis_evidence.py::test_two_coverage_denominators_and_record_grain_are_distinct` | 离线已覆盖；真实库抽样待做 |
| 2 | 历史归属／当前主数据 | `datasets.yaml:128`：仓库属性是当前主数据，不是交易时点历史；`:41/:73/:154`：未匹配主数据键保留为 unknown/null 并计入覆盖 | `test_customer_history.py`（25 项）、`test_delivery_l3_entity_time.py`（13 项） | 离线已覆盖 |
| 3 | 分账／交易 | 各语义合同的 `ledger_policy`（默认 `transaction_detail`，另有 `salesperson_allocation`） | `test_c01_c02_public.py::test_both_target_ledgers_reconcile_and_hide_physical_overall` | 离线已覆盖 |
| 4 | NULL／0 | `delivery-semantics.yaml` 的 `data_state_contract.actual_missing`：无匹配记录仅为证据缺失，不等于业务发生量为零 | `test_analysis_evidence.py::test_missing_month_and_nonadditive_period_summary_are_not_filled`、`::test_full_returned_zero_net_partition_still_distinguishes_signs`；另有约 237 个用例名匹配 null/zero/empty/undefined | 离线已覆盖 |
| 5 | 期间未完结 | 返回的 `period_state` 区分已过月与进行中月；查询日观测不等于源时效 | `test_business_contracts.py::test_period_evidence_distinguishes_calendar_progress_from_freshness` | 离线已覆盖 |
| 6 | 跨月退货 | 交付语义含退货结算期披露；账本按交易明细 | `test_delivery_l3_semantics.py`、`test_delivery_l3_runtime.py` | 部分：真实跨月样本待业务提供 |
| 7 | 迟到数据 | **无迟到归属规则**；仅「不主动解释同步时间、不据空值推断过期」 | 无 | 需 owner 定口径 + 真实数据 |
| 8 | 单位 m/y/kg/Pcs/tao | `query_builders.py:647/658`：数量指标必须按单位分组或限定单一单位（`UNIT_SCOPE_REQUIRED`） | `test_delivery_l3_semantics.py`、`test_delivery_l3_runtime.py`、`test_delivery_l3_catalog.py`、`test_answer_ground_truth.py` 的混合单位失败用例 | 离线已覆盖 |
| 9 | 原币／RMB | `query_builders.py:624/637`：原币指标必须按币种分组或限定单一币种（`CURRENCY_SCOPE_REQUIRED`） | `test_result_identity_currency.py`（12 项）、`test_delivery_l3_semantics.py::test_required_filters_and_currency_policy_remain_governed` | 离线已覆盖 |
| 10 | 多源水位 | **无水位语义** | 无 | 需 DBA/数据开发给出各源刷新时点 |
| 11 | 读时点不宣称原子 ETL 批次 | `delivery-semantics.yaml:94-98` 的 `freshness` 规则；`query_builders.py:261` 与 `schemas.py:133`：查询日观测不是源时效水位 | `test_business_contracts.py::test_period_evidence_distinguishes_calendar_progress_from_freshness` | 离线已覆盖（属「明确不宣称」的正向规则） |

## 4. 缺口与需要人工的部分

| # | 缺口 | 需要谁 | 具体要做的事 | 判据 |
| --- | --- | --- | --- | --- |
| 1 | 迟到数据（晚到行算哪个月） | 数据 owner（口径）＋数据开发（取证） | 先定义迟到行的期间归属与可见性规则；再用真实样本给出：同一指标在「按业务时间」与「按入库时间」下的差异行数与金额 | 规则书面化 + 真实样本对照 |
| 2 | 多源水位／刷新时点 | DBA＋数据开发 | 列出各源刷新时点、保留期与允许的最大延迟；确认探针与配置一致（与 R24 合并做） | 书面政策 + 探针一致 |
| 3 | 跨月退货真实样本 | 业务复核 | 提供跨月退货的真实案例（同月发出、次月退货）与两个月份的独立期望值 | 独立期望值与引擎返回一致 |
| 4 | 真实库上的 N:N 抽样 | 数据开发 | 对指标用到的每个连接键抽样确认基数（是否 1:N 或 N:N），核对是否只走受管 many-to-one | 抽样记录 + 结论 |
| 5 | 真实月末／账期末取数行为 | 数据开发 | 在月末与账期末各取一次，确认进行中期间的 `period_state` 与数值表现符合披露 | 两次取数记录 |

以上五项都需要真实库或业务输入，离线无法替代；完成后应把证据登记到 R16 的指标登记册对应行。

## 5. 回退

- 本轮不改合同、不改引擎、不删 fixture；无运行时影响。
- 若后续按口径小修复，按逐口径提交、单独回退；出现重大错误时停用受影响能力并保留 fixture。
