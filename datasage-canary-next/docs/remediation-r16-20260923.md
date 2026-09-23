# R16 核验记录：独立业务数值真值与数据血缘

日期：2026-09-23｜依据：综合审查 V2.0 的 R16（批次 D / P1）｜负责角色：数据开发＋业务 owner
前置：R04、R05、R12（已完成）｜性质：清单化与机具落地；真实数值核验需数据 owner 与数据库

## 1. 结论

- 215 个指标里，**只有 5 个在合同里显式声明了可用性**：4 个业务员分摊口径指标
  （`allocated_net_delivery_amount`、`allocated_net_receipt_amount`、
  `delivery_allocated_target_amount`、`receipt_allocated_target_amount`，状态
  `available` + 激活门 `ready_for_live_replay`，遗留检查 `trusted_live_replay`），
  1 个 `receivable_quantity`（`pending_validation` + 门态 `blocked`）。
- 其余 **210 个指标是「未声明即视为可用」**：引擎的
  `capability_contract.validate_availability()` 在缺少 `availability` 块时返回
  `available`，因此这些指标既没有 owner 也没有任何证据。这正是 F13
  「测试资产不等于独立业务真值」的具体形态——可用性来自缺省，不来自验证。
- 已有的 18 个业务验收案例确实是独立手写的
  （`expected_result_origin: hand_authored_independent_of_plugin_contracts`），但覆盖的是
  场景层，不能替代 215 个指标各自的数值真值。
- 本轮把这层隐含状态变成显式、可复核的登记册，并让「未验」成为默认、无法被悄悄改成通过。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 指标总数与状态分布 | 215 个：`available` 214（其中仅 5 个显式声明）、`pending_validation` 1 | 插件自身合同加载器 `contracts.execution_contracts(domain)` 逐域读取 |
| 缺省语义 | 无 `availability` 块 → 返回 `available` | `plugins/datasage-query/capability_contract.py:712-720` |
| owner / 证据覆盖 | 仅 5 个指标有 owner；4 个带证据引用（含 2 个 sha256 回执）与遗留检查 | 合同扫描，完整结果见登记册 |
| 场景层独立真值 | 18 个案例为独立手写，非引擎生成 | `tests/fixtures/business_acceptance_cases.json` 的 `expected_result_origin` |
| 登记册一致性 | 215 行、摘要自洽、可确定性重生成 | `python -B tests/metric_readiness.py check` |

## 3. 登记册（本轮落地）

文件：`tests/fixtures/metric_readiness_register.json`（schema `datasage-metric-readiness/v1`）
生成/校验：`python -B tests/metric_readiness.py generate|check`

每行字段：

| 字段 | 含义 |
| --- | --- |
| `domain` / `metric` / `label` / `unit` / `time_policy` | 合同事实（口径所属域、指标代码、业务名称、单位、时间口径） |
| `declared_availability` | 合同声明的可用性；缺省为 `available` |
| `declared_by_contract` | 是否真有显式声明（`false` 表示「缺省即可用」） |
| `owner` / `activation_gate` / `remaining_checks` | 合同里的责任人、激活门与遗留检查 |
| `verified` | 是否已有独立证据（初始全部 `false`） |
| `independent_evidence` | 独立证据记录列表，由数据 owner 填写 |

当前摘要：`total=215`、`declared_available=214`、`declared_by_contract=5`、
`pending_or_blocked=1`、`verified=0`、`unverified=215`。

同一目录另有既有的 `business_acceptance_cases.json`（18 个场景级独立案例）；登记册补充的是
**指标级**可用性，两者不互相替代。

## 4. 已落地的守卫（本轮）

`tests/metric_readiness.py`（生成/校验模块）＋ `tests/test_metric_readiness_register.py`（7 项）：

1. 登记册恰好覆盖每个已注册指标一次，且与合同的指标集合一致。
2. 合同侧字段必须与当前合同一致——合同改了而登记册没跟上会失败（不静默漂移）。
3. 登记册结构校验：状态取值、字段完整、摘要与行数一致。
4. 缺省可用被显式标出：`declared_by_contract=false` 的行不得有 owner（缺省不是声明）。
5. `verified=true` 必须有独立证据记录；证据 `kind` 只允许
   `independent_sql` / `manual_fixture` / `owner_statement`——**引擎自身输出不被接受**
   （测试用 `kind=engine_output` 的反例证明会被拒）。
6. 合同声明为 `pending_validation`/`blocked` 的指标不得被标为已验证。
7. 摘要与行数不符会被拒。

7 项全部通过。

## 5. 数据 owner 需要做的（真实部分）

| # | 步骤 | 期望产物 |
| --- | --- | --- |
| 1 | 定优先级：先做合同已显式声明的 5 个，再按使用与风险排序（`skills/` 文本中被引用最多的是 `receipt_amount`、`delivery_amount`、`net_receipt_amount`、`delivery_receipt_comparison`、`refund_amount`、`receivable_quantity`、`open_receivable_amount`、`positive_debt_amount`、`delivery_target_completion`、`idk_unpriced_pool`、`task_recorded_summary`、`linked_delivery_amount` 等，可作起点） | 排序表 |
| 2 | 对选定指标用业务批准的独立 SQL 或手工夹具产出期望值（不得用待测引擎的结果当期望） | 查询文本或夹具文件 + 结果文件 |
| 3 | 把结果文件哈希（`sha256`）与出处、审批人、日期填进登记册对应行 | 更新后的 `independent_evidence` |
| 4 | 把 `verified` 置为 `true`（仅当证据齐备），或保留 `pending` | 登记册 + 审批记录 |
| 5 | 对 4 个 target 分摊指标补 `trusted_live_replay`（它们只差这一项） | 真值回放回执 |
| 6 | 跑 `python -B tests/metric_readiness.py check` 并留存输出 | 校验输出 |

证据记录格式（一行一条）：

```json
{
  "kind": "independent_sql",
  "reference": "独立 SQL 文件名或工单号",
  "artifact_sha256": "<64 位十六进制>",
  "approved_by": "审批人",
  "approved_on": "2026-09-23"
}
```

## 6. 与验收条件、回归场景的对应

| R16 验收条件 | 本轮状态 |
| --- | --- |
| 被声明正式可用的指标都有独立支持证据 | 机制已建立并有守卫；证据补齐属数据 owner（第 5 节），当前 `verified=0`，不冒充通过 |
| pending 指标保留 | `receivable_quantity` 在登记册中保留 `pending_validation`，且测试禁止其被标为已验证 |
| 错误按独立业务修复处理 | 未做（需真实数据错误样本）；本记录不含任何数据修复 |
| 未验项不标通过 | 登记册默认 `verified=false`；缺证据即拒绝、引擎输出即拒绝 |

回归场景 V19/V20/V25：离线部分由登记册与守卫覆盖；真实数值对照、以及模型对缺失/未验指标的
表述，仍需第 5 节的证据与一次真实会话评审。

## 7. 回退

- 删除登记册与其测试即回到原状（合同与引擎未改动，无运行时影响）。
- 证据记录属增量数据，回退只影响该指标的 `verified` 状态，不影响查询能力。
- 本项不修改任何指标口径；若发现口径错误，按报告要求走「独立业务修复单独版本」。
