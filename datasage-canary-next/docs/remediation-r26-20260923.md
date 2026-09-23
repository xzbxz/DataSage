# R26 核验记录：产物完整性与接收人安全

日期：2026-09-23｜依据：综合审查 V2.0 的 R26（批次 E / 发布前置）｜负责角色：安全测试＋集成 owner
前置：R04、R11、R15、R22（已完成）｜性质：注入式离线验证 + 两处源码修正；真实客户端打开与真实投递待做

## 1. 结论

- 产物侧的既有设计本来就**按构造**避开了大部分注入面：本机报表产物文件名是固定字面量
  （`report.json`/`report.txt`），目录名是 uuid4，写入前逐级校验符号链接与包含性
  （`REPORT_OUTPUT_PATH_INVALID`）；xlsx 是手写 XML，文本一律 `t="inlineStr"`，
  **代码里不存在公式元素**；超出 Excel 精度的十进制转精确文本；超长单元格与非有限数值
  **直接失败**而不是截断。
- **本轮补上两处真实空档**：零宽/双向控制字符（Unicode Cf）此前会原样进入 xlsx 单元格、
  工作表名与渠道文本（可造成显示顺序伪造/隐藏），现已剔除；并加 6 项注入守卫。
- 同形字（如西里尔 `а` 与拉丁 `a`）**不改变字节内容**，属显示层风险：本轮不作规范化处理
  （会改变业务名称），保留为已登记残余风险，由 R27 的专家评审一并确认。

## 2. 注入清单与结果

| R26 注入项 | 结果 | 依据 |
| --- | --- | --- |
| 公式样文本（`=1+1`、`=cmd\|'/C calc'!A0`、`@SUM`、`+1+1`、`-1-1`、制表符开头、`=HYPERLINK(...)`） | 全部作为内联字符串写入；产物 XML 中无 `<f>`/`<f ` 元素 | `tests/test_artifact_integrity.py::test_formula_shaped_text_never_becomes_a_spreadsheet_formula` |
| 极端小数/指数（`1E-320`、`123456789012345.67`、`2^63+1`） | 判为精确文本并原样落盘；普通金额 `100.15` 仍是数值单元格 | 同文件 `::test_extreme_decimals_keep_their_exact_value_and_amounts_stay_numeric` |
| RTL/零宽/控制字符（U+200B、U+202E、U+2060、U+200E、U+2066、C0） | 单元格、工作表名与渠道文本中不再出现 Cf；C0 转替换字符，不产生非法 XML 字节 | 同文件 `::test_invisible_formatting_characters_never_reach_a_cell_or_a_message` |
| 同形字 | 记录为残余风险（不规范化，避免改动业务名称） | 本节第 1 条 |
| 路径穿越 | 相对路径与越出 profile 的绝对路径都被拒绝；产物只落在 uuid 目录下的固定文件名；注入 `name: ../../evil.html` 不产生任何文件 | 同文件 `::test_artifact_names_cannot_escape_the_profile` |
| 非预期扩展名 | 产物名不含操作者可控部分（固定字面量 + uuid 目录）；扩展名由代码固定为 `.json`/`.txt`/`.html` | 同文件 `::test_artifact_names_cannot_escape_the_profile` |
| 接收人分离 | 绑定层拒绝 SQL 接收人/区域/未批准客户范围与空部门接收人；产物只带被评审过的键与身份基准 | `tests/test_operations.py`、`tests/test_customer_audit.py`；`::test_artifacts_carry_only_reviewed_keys_and_declare_their_identity_basis` |
| 大小/CPU 上限 | 超长文本（> `max_cell_chars`）与非有限数值都抛错停止，不静默截断；压缩包尺寸限制在打开归档前拒绝 | 同文件 `::test_oversized_or_non_finite_values_stop_loudly_instead_of_truncating`；`tests/test_slow_report_delivery.py::test_real_size_limit_rejected_before_opening_archive` |

## 3. 源码修正

| 位置 | 修正 | 原因 |
| --- | --- | --- |
| `plugins/datasage-query/legacy_xlsx.py::_xml_text` | 先剔除 Unicode Cf，再替换 C0 控制字符并做 XML 转义 | 零宽/双向控制字符可让单元格显示顺序与真实内容不一致 |
| `plugins/datasage-query/legacy_xlsx.py::_safe_workbook_sheet_title` | 工作表名同样剔除 Cf | 同上（表名会显示在标签栏） |
| `plugins/datasage-query/local_report.py::_display` | 渠道文本剔除 Cf，再中性化 `MEDIA`/`[SILENT]`/换行 | 避免不可见字符参与伪装渠道指令 |

修改前已确认全库（含夹具与黄金文件）**不含任何 Cf 字符**，因此不改动既有期望输出；
相关套件复跑结果见第 4 节。

## 4. 与验收条件的对应

| R26 验收条件 | 状态 |
| --- | --- |
| 公式当作文本不被执行 | 通过（无公式元素 + 7 种公式样文本注入） |
| 控制字符不破坏日志/渲染 | 通过（Cf 剔除、C0 替换、XML 合法） |
| 小数/指数不被静默改变 | 通过（精确文本路径 + 普通金额仍为数值） |
| 扩展名/接收人/路径校验 | 通过（固定产物名、包含性校验、绑定层拒绝未批准接收人） |
| 大产物有上限且不泄露 | 通过（超长/非有限值抛错停止；产物只带被评审键） |
| 不引入新富文本引擎、不扩大文件类型 | 未引入：仍为手写 XML + 固定扩展名 |

## 5. 需要人工/真实环境确认的

1. 用真实客户端（Excel/WPS/企微）打开一次含公式样文本与极端小数的产物，确认只显示文本、数值不变形。
2. 一次真实投递的接收人复核：确认同一产物不会带出其它区域/部门的接收人或内容（与 R25 的投递演练合并）。
3. 同形字与零宽字符的业务样本复核（登记残余风险或给出限制措施）。

## 6. 回退

- 三处清洗改动均为单函数级，回退即恢复原行为；相关套件可整体移除。
- 若某客户端仍出现公式或显示伪造，按报告要求**限制该产物的投递范围**，不通过放宽校验来消除告警。
