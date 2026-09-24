# V6 补丁落地核验记录（independent-review-20260924）

日期：2026-09-24｜来源：外部独立审查交付 V6「源码核验与定向修复」｜负责角色：集成 owner＋发布 owner
性质：落地与差异核对 + 本机验证；V6 自述不是宿主认证、SQL 实数据验真、业务签字或上线批准

## 1. 结论

- V6 的来源标记 `531c1b5e…8db8` **正是我方当前提交**，因此是叠加补丁：375 个受跟踪文件我这边全有、
  **0 个被删除**、新增 2 个、改动 14 个。
- **config.yaml 已做语义等价核验**：除宿主写回的 `onboarding.seen.busy_input_prompt` 段外，
  **无键差异、无取值差异**（其余全是注释与行内列表格式）。因此配置按「受跟踪的已复核版本」采用，
  宿主的规范化重写仍属运行副作用。
- **实测到的宿主副作用**：对比时线上工作区的 `config.yaml` 已被宿主规范化（注释被剥离，188 行），
  而受跟踪的已复核版本为 220 行；V6 的 config 与该已复核版本**逐字节相同**（同一 blob），
  因此复制后工作区自动回到已复核版本，该文件也就没有出现在本批提交里。宿主下次重启仍可能再规范化，
  以受跟踪版本为准（与 R10 记录一致）。
- 白名单**只新增 2 行**（新回归文件与新记录），无删除；profile 级 `.gitignore` 另补 7 行——
  正是 V6 3.5 指出的问题：后续新增的七份自有 reference 在干净副本里会被忽略。
- **发现并修掉 V6 自身的一处平台缺陷**（见第 4 节），修后其自带回归 26 项全过。

## 2. 哈希链

| 对象 | SHA256 |
| --- | --- |
| V6 交付包 `DataSage_Independent_Reviewed_Patched_V6_20260924.zip` | `00bac056…7767`（本机实测） |
| V6 自述的输入包 `DataSage-main (1)(3).zip` | `41e00cf5…b4e7`（对方记录，本机无此文件） |
| 来源标记 | `531c1b5e52ba9553ff6b9b523b0cf339b3438db8` = 我方改前提交 `531c1b5` |

## 3. V6 的修复内容（照其自述，并逐文件核对）

| 组 | 内容 | 落地文件 |
| --- | --- | --- |
| 3.1 | 模型工具输出的 JSON 边界：拒绝 NaN/Infinity/溢出指数/重复键/孤立代理字符，返回既有 `INVALID_TOOL_RESULT`，不补零不伪装成功 | `wire.py` |
| 3.2 | XLSX 合法文本与标题：复用 XML 1.0 字符判定，非法字符显示为 U+FFFD；标题的制表/换行转空格并保持唯一 | `legacy_xlsx.py` |
| 3.3 | **投递前验证实际附件**：PNG 用 Pillow 解码校验（缺依赖明确失败）；ZIP 拒绝穿越/绝对路径/反斜线/符号链接/Windows 歧义路径并校验大小与 CRC；XLSX 核心 XML 必须可解析且无 DTD；附件不合法时**在创建 HTTP 客户端之前失败**，文字也不先发 | `wecom_app_transport.py` |
| 3.4 | 五份业务方法 reference 的表述纠正：收款事件时间与换算基准、IDK 促销候选池≠登记滞销池、月度取消订单 DDP 扣减不重复扣、未结束期间不禁止展示当前目标与实际、跨域分支不足停止的是不受支持的结论而非丢弃其它有效证据 | `skills/.../references/{receipt,inventory,profit,target,cross-domain}-analysis.md` |
| 3.5 | 自有知识纳入版本：为七份自有 reference 补显式白名单例外 | profile 级 `.gitignore` |
| 3.6 | 无 Git 副本的维护检查：原用顶层 glob 误报嵌套 reference 缺失，改用已有 `measure_surface()` 分类枚举 | `tests/maintenance_cost.py` |
| 3.7 | 交付闭包：新增回归文件与记录并加入导出白名单；按实际文件更新测量 | `tests/test_independent_review.py`、`docs/independent-review-20260924.md`、`scripts/datasage_source_export.py`、两份登记册 |

## 4. 我方发现并修掉的 V6 平台缺陷

`tests/test_independent_review.py` 的归档非法名用例断言 `a\b.txt` 必须被拒，但在 Windows 上
**stdlib 在写入归档时会把反斜线改写成 `/`**，成员名变成安全的 `a/b.txt`，该断言在本平台按构造
无法成立（Linux 才保留）。实测证据：`输入 'a\b.txt' → 归档内 'a/b.txt'`。

修法（不降低标准）：
- 端到端用例保留其余非法名（`../`、绝对路径、`x/../`、`C:/`、尾随空格/点、双斜线、符号链接）；
- 新增一条**直接调用成员名规则**的用例，用字面反斜线与控制字符验证规则本身，并带一个安全嵌套名作正向对照；
- 修后该文件 26 项全过（1 项环境性跳过）。

## 5. 验证

| 项目 | 结果 |
| --- | --- |
| V6 要求的复核 | `test_independent_review` 26 项 OK（修后）、`test_app_notification` OK、`test_compact_payloads` OK、`test_domain_diagnostic_paths` OK、`test_source_export` OK（1 跳过）；`context_cost check`、`maintenance_cost check` 均通过 |
| 其余守卫 | 13 个守卫全绿（灰度计划、发布矩阵、产物完整性、集成边界、V4/V5 回归、答案呈现、运营台账等） |
| 配置语义 | 除宿主写回段外无差异（见第 1 节） |
| 全量回归（副本） | **1600 项通过 / 4 跳过 / 0 失败**（1574 + V6 新增 26 项，算术对得上） |
| 运行态 | 未触碰：Home、`.env`、会话、Memory、state、调度 |

## 6. 边界（与 V6 自述一致）

精确宿主（0.21.1 / 2237be3）、真实数据库、模型、企微、审批与正式投递仍需对应环境验收；
本轮不宣称达到 L3、性能达标或可直接覆盖生产 Home。V6 的合成 SQL oracle 只覆盖兼容子集，不认证
MySQL 方言，也不替代公司数据真值。

## 7. 回退

回退本批源码、测试与说明即可（`git revert` 本批提交），不覆盖运行 Home、凭据、state 或用户后续改动。
