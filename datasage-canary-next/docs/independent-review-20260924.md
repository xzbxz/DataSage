# DataSage 独立审查与修复记录 · 2026-09-24

## 1. 基线与状态

本批次以 `DataSage-main (1)(3).zip` 为唯一源码基线，不把历史报告的通过结论作为本轮验收依据。

- 输入 SHA256：`41e00cf54a37ae1d7306f6effc27363dcd41b720b0e97a7b0db98fd223f9b4e7`。
- ZIP 来源标记：`531c1b5e52ba9553ff6b9b523b0cf339b3438db8`；不是本次验证的远程 HEAD。
- Profile：`datasage-canary-next`；版本声明仍保留 `0.15.0-rc14`。
- 本批次标识：`independent-review-20260924`，不冒充上游提交或正式发布。
- 精确宿主声明：Hermes `0.21.1 / 2237be355906fbe6065ce1815711eee52b2d646e`。
- 本次未取得该宿主源码与运行环境。不能把当前官网机制核对当作精确版本兼容认证。

交付状态是完成独立源码核验和定向修改的候选包。精确宿主、真实数据库、模型、企微、审批和正式投递仍需对应环境验收；不宣称已达到 L3、性能达标或可直接覆盖生产 Home。

## 2. 原则和不变边界

Hermes 仍负责理解、分析路径、证据综合和最终回答；DataSage 通过现有 Plugin 提供确定性能力，通过原生 Skill 提供按需业务方法。本批次不新增 Planner、Router、Finalizer、Reference Loader、RBAC、密钥平台、调度器或发布系统。

本次不修改业务指标公式、数据库查询构造、实体规则、15份业务合同、SOUL、三个工具的注册与公开 Schema、模型、权限、企微配置或定时任务。保留全部七域215指标和既有运营能力。不连接数据库、模型API或真实企微，不调用真实投递和调度，不读写生产状态。

## 3. 修复内容

### 3.1 模型工具输出的JSON边界

位置：`plugins/datasage-query/wire.py`。

异常 handler 输出可能包含 `NaN`、`Infinity`、溢出指数、重复JSON键、无法UTF-8编码的孤立代理字符或不可序列化内容。默认JSON处理可能将它们透传、按最后一个重复键覆盖，或抛出未结构化异常。

本次复用现有最终输出边界，拒绝非有限数值、重复键和无效对象，返回既有 `INVALID_TOOL_RESULT` 失败结构；不补零、不补null、不伪装成功。保留正常整数、浮点、中文、布尔、null和文字中的NaN字样。最终响应的大小治理仍交给原有宿主方式，不增加第二套预算体系。

范围：数据库结果转换本身已有非有限数值保护。本次修复是最终适配边界的完整性防护，不证明此前真实数据库已经返回错误事实，也不保证任意文本具有业务语义正确性。

### 3.2 XLSX合法文本与标题

位置：`plugins/datasage-query/legacy_xlsx.py`。

原实现对单元格C0字符有部分处理，但单元格非字符/代理字符和标题控制字符仍可能生成坏XML或编码失败。改为复用一个XML 1.0字符判断，非法字符显示为U+FFFD，不做标识符的兼容归一化；标题的制表和换行明确转空格，保持清洗后的名称唯一。

不改变数值、Decimal精确文本、公式样式文本的inlineStr处理、报表字段或已存在的格式字符策略。合法中文、特殊XML字符与合法Unicode仍保留。替代符只属于文件呈现，不改源数据。

### 3.3 投递前验证实际附件

位置：`plugins/datasage-query/wecom_app_transport.py`。

保留既有私有目录、大小、数量和展开字节限制以及幂等摘要算法；补充实际内容检查：

- PNG使用项目既有Pillow依赖验证并解码，不再只看签名。缺少该依赖时明确失败，不绕过预检。
- ZIP拒绝穿越、绝对路径、反斜线、符号链接和Windows歧义路径；检查大小与CRC。验证过程不解压到文件系统。
- XLSX核心XML必须可解析且无DTD；根工作簿关系、工作簿声明的内部部件、工作表目标需要存在，根类型匹配。
- 保留普通中文目录、合法工作簿、正常PNG及ZIP时间戳变化后的内容身份。
- 模拟完整投递入口验证：附件不合法时，在创建HTTP客户端之前失败，文字也不先发送。

边界：这不是通用杀毒、所有OOXML功能认证、公式计算器或任意递归压缩包扫描。外部关系和宏等更完整的文件信任策略仍按业务使用与已有权限处理；本批次没有引入任意文件上传能力。

### 3.4 业务方法与合同含义

位置：五份现有reference（receipt、inventory、profit、target、cross-domain）。

- 收款金额和实收金额同用收款事件时间，差异首先涉及记录值/换算基准，不能只凭二者不同推断入账滞后或归属变化。收款事实不具备产品维度时，产品结构解释需要兼容的额外证据。
- IDK未定促销价候选池与登记滞销池是不同母集，不能拿前者代表后者的全量分类、产品数或历史变化。
- 客户月度的取消订单库存DDP扣减不是一般库存账面成本，已包含于源毛利，不在解释阶段再扣一次。
- 未结束目标期间不禁止展示有效的当前目标与实际；只禁止未经支持的完结结论或比率。
- 跨域某分支不足，停止的是不受支持的组合比较/因果结论，不应丢弃其他有效独立证据。

这些改动依据现有合同和同篇已存在的边界，不新增业务规则、阈值、管理结论或固定分析顺序。候选身份、owner确认和真实模型验证要求保持不变。文案对齐不等于证明真实模型已经使用正确。

### 3.5 自有知识的版本纳入

位置：Profile `.gitignore`。

原例外只覆盖早期四份reference，后增七份自有reference在干净Git副本中被忽略。当前已经被跟踪的文件不会因为ignore立即丢失，但全新导入、重建或新增跟踪可能漏掉它们。

仅为包内已有的七份自有reference补齐明确例外；不开放宿主全部bundled Skills，也不改成忽略规则全取消。

### 3.6 无Git副本的维护检查

位置：`tests/maintenance_cost.py`。

无Git检查原来用顶层glob寻找Skill参考，误报真实存在的嵌套文件缺失。改用已有 `measure_surface()` 的分类枚举，不复制另一套文件遍历规则。真正缺失文件仍失败；无Git仍不宣称测得真实提交历史。

### 3.7 交付闭包与登记

新回归文件：`tests/test_independent_review.py`。本说明与测试加入已有源码导出白名单；修改后按实际文件与知识载荷更新相应测量，保留真实历史指标来源和未完成验收状态。不靠删除测试或下调标准消除失败。

## 4. 独立验证方法

先建立行为不变量，再读实现和运行已有测试；历史回归作为完整测试集的一部分，不作为唯一问题目录。

新增测试包括严格JSON正反例、实际OOXML解析、合法标题冲突、精确Decimal和公式式文本、有效图片、损坏图片、ZIP目录/CRC/穿越/符号链接、核心OOXML依赖、无网络模拟投递、知识版本纳入、无Git正常与真缺失对照，以及独立合成SQL算术。

SQL合成oracle在内存SQLite执行当前生成SQL的兼容SELECT子集，只替换参数占位符；使用手算结果验证出库区间、状态、内外部范围、净流量退货符号、负值、缺失值和收款换算差异。加入错误符号及错误期间的受控变形，确认oracle真的会识别错误。它不认证MySQL完整方言，不替代公司数据真值。

没有伪造agent、gateway或hermes_cli使集成测试通过。纯模块通过命名空间加载，只验证纯实现；Pillow和openpyxl用于正常/异常文件对照，openpyxl仅是可选测试依赖，不加入生产依赖。

## 5. 本机复核

在实验分支或独立目录操作，先核对输入版本和用户未提交修改。完整源码包和补丁是替代方式，不要重复覆盖，更不要覆盖生产Home的凭据、会话、记忆和调度。

```bash
# 补丁路径以DataSage-main仓库根目录为基准
git apply --check changes.patch

# 在实际官方Hermes Python环境、Profile目录中
python -B -m unittest discover -s tests -p test_independent_review.py -v
python -B -m unittest discover -s tests -p test_app_notification.py -v
python -B -m unittest discover -s tests -p test_compact_payloads.py -v
python -B -m unittest discover -s tests -p test_domain_diagnostic_paths.py -v
python -B -m unittest discover -s tests -p test_source_export.py -v
python -B tests/context_cost.py check
python -B tests/maintenance_cost.py check
```

之后执行已有全量测试和真实业务验收，不把上述小集合当完整验收。测试有host、字体、私有配置和外部环境依赖；必须保留受阻原文，不算成业务通过率。

回退仅限本批次源码、测试和说明。在匹配版本中先 `git apply -R --check changes.patch`，不以回退为由清除用户后续修改或真实业务状态。本批次没有真实外发副作用。

## 6. 剩余验收与官方依据

尚需：锁定宿主注册/Skill消费、会话压缩与多用户上下文、授权/审批、真实数据与迟到记录、完整业务真值与模型留出题、端到端性能/取消、原Windows字体、受控运营投递与恢复。每项应有实际通过证据，不因代码修复自动关闭。

公开官方资料仅作为机制对照：

- Hermes Plugins：https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins/
- Hermes Skills：https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/
- Hermes Context：https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files/
- Hermes Profiles：https://hermes-agent.nousresearch.com/docs/user-guide/profiles/
- Python JSON：https://docs.python.org/3/library/json.html
- XML合法字符：https://www.w3.org/TR/xml/#charsets
- Pillow验证与解码：https://pillow.readthedocs.io/en/stable/reference/Image.html

这些是2026-09-24读取的指南，不强制要求当前锁定宿主支持最新版所有功能。目录大小、工具数量、普通文档互链或是否开启Tool Search都不能单独证明架构好坏。
