# V4 补丁落地核验记录（audit-fix-20260924）

日期：2026-09-24｜来源：外部复审交付 V4「已修改源码副本 + 离线复核」｜负责角色：集成 owner＋发布 owner
性质：落地与**三方哈希核验 + 独立复现**；本批不是生产验收，发布决定仍归发布 owner

## 1. 结论

- 修改包已**逐字节**落地：12 个变动文件与我方落地内容、与修改包清单的 after 哈希**全部一致**。
- 用修改包自带 `changes.patch` 在克隆里**独立复现**：`git apply --check` 通过、`git apply` 通过，
  12 个文件与清单一致且与我方落地文件**逐字节相同**。
- 修改包的 before 哈希与我方改前提交 `0dd1f4d` 的 9 个改动文件**全部一致**，来源标记吻合。
- 我方落地时补了两件修改包未包含的事（白名单 3 条、测量登记册重生成），另修掉一处我方自己的缺陷。

## 2. 哈希链

| 对象 | SHA256 |
| --- | --- |
| 输入源码包（对方记录） | `6b07d1ec…8adc`（含空格文件名 `DataSage-main (1).zip`） |
| 修改包 `DataSage_Reviewed_Patched_V4_20260924.zip` | `514840d3…c824`（与对方 `package_verification` 记录一致） |
| 证据包 `DataSage_Fix_Patch_And_Evidence_V4_20260924.zip` | `4f5bb3e1…fef7` |
| 来源标记 | `0dd1f4d6d8d6352ac3d4ba004f3c91e17d2b3603` = 我方改前提交 |

## 3. 逐文件核验（12 项）

| 文件 | 类型 | after 哈希 | before 哈希 |
| --- | --- | --- | --- |
| plugins/datasage-query/contract_store.py | 修改 | ✓ | ✓ |
| plugins/datasage-query/e2e/answer_ground_truth.py | 修改 | ✓ | ✓ |
| scripts/datasage_source_export.py | 修改 | ✓ | ✓ |
| tests/test_expert_authority_inventory.py | 修改 | ✓ | ✓ |
| tests/test_source_export.py | 修改 | ✓ | ✓ |
| docs/legacy-checklist-state-20260923.md | 修改 | ✓ | ✓ |
| docs/remediation-c-batch-20260923.md | 修改 | ✓ | ✓ |
| docs/rule-ownership-20260923.md | 修改 | ✓ | ✓ |
| docs/vendor-provenance-20260923.md | 修改 | ✓ | ✓ |
| docs/audit-fixes-20260924.md | 新增 | ✓ | — |
| tests/reference_authority.py | 新增 | ✓ | — |
| tests/test_revision_regressions.py | 新增 | ✓ | — |

核对结果：after 12/12 ✓、before 9/9 ✓、独立复现 12/12 ✓、与落地逐字节相同 12/12 ✓。
对方记录 `patch_apply_check=passed`、`patch_apply_byte_equivalence=passed`、`zip_crc=passed`、
`all_file_bytes=passed`、`removed=0`。

## 4. 我方落地时补的两件事

| 事项 | 原因 |
| --- | --- |
| 仓库根白名单补 3 条（新记录、helper、25 条回归测试） | 本 profile 白名单是「默认忽略 + 逐文件放行」，不补就进不了库、也进不了导出物 |
| 重生成 R28/R30 测量登记册 | 文件集变化（测试 146→147、辅助 7→8、文档 25→26）会让漂移守卫变红；重生成后通过 |

## 5. 顺手修掉的我方缺陷

维护守卫把 git 历史中位数放进**实时比对**，导致每多一个提交就假红（本批提交正好触发）。
已改为「历史作带来源的**快照**，不作实时比对」，其余部分仍严格比对。

## 6. 验证

| 项目 | 结果 |
| --- | --- |
| 修改包要求的 5 项最低复核 | 全部通过（`test_revision_regressions`、`test_answer_ground_truth`、`test_source_export`、`test_expert_authority_inventory`、`test_remediation_runtime_immutability`）；1 项 Windows 符号链接跳过 |
| 带 V4 的全量回归（副本） | **1556 项通过 / 3 跳过 / 0 失败**（1532 + 净增 24） |
| 运行态是否被触碰 | 否：Home、`.env`、会话、Memory、state、调度均未改动 |

## 7. 证据包自述的完成度与边界（照录，不代改）

- 完成：142 条 runner 条目中 133 成功、2 跳过、1 项因缺宿主断言失败、6 个模块因缺宿主仅导入占位；
  另有 42 条从最终 ZIP 重解压的冒烟重复测试通过（**不计入独立成功总数**）；广域发现尝试被中止且**未计入**。
- 静态：272 个 Python 文件可解析；7 域 215 指标与 15 份合同文件字节未变；三个工具入口未变。
- 未覆盖（与我方记录一致）：真实宿主锁定、真实模型、企微网关与审批、生产数据库与业务真值、
  ETL 水位与迟到数据、真实多轮/跨用户、发布背书——均需本机/owner 继续验证。

## 8. 回退

- 回退只回退本批源码与说明（`fc9714b` 及其后的记录提交），**不覆盖**运行 Home、凭据、state 或用户后来产生的数据。
- 若需回到修改包之前的版本：`git revert` 本批提交即可；回退路径已在灰度计划的演练中验证过（见 R31 记录）。
