# DataSage 源码综合复核与修复 V5

日期：2026-09-24。输入为 `DataSage-main(2).zip`，来源标记 `8c5b833e6399185fcc85d3fec7c887a64005b68e`。
输入SHA256：`2d204a31fda6e16f96f152cef2009b1cf708358b3513c63413698cf579f1cb89`。

这是一批源码修复，不是上游tag、正式上线或L3认证。配置中的rc14与宿主声明不变；没有换模型、增加工具、调整数据可见范围或启用投递。详细逐模块日志随本批次证据包交付。

## 定位与保留边界

Hermes仍拥有理解、规划、分析与最终回答；DataSage继续提供原生Plugin与按需Skill。没有新增Planner、Router、Finalizer、Reference Loader、权限/密钥/发布平台。15份业务合同、215指标、三个工具、公开Schema、SOUL、配置和身份审批均保持输入字节；查询SQL构造不改义。

V4的合同只读、YAML别名兼容、答案评分反例、vendor指纹及reference检查修复继续保留，本批次没有重写评分器或删除正常方法材料。

## 本批次实际修复

1. **纯规则导入与密钥访问解耦。** `settings.get_secret`只在实际取凭据时调用官方 `agent.secret_scope.get_secret`。不读进程环境、不缓存密钥或作用域；缺宿主仍抛错，不把缺失当默认值。`db_runtime/db_security/runtime_health`保留原调用参数与本地别名。单元spy只用于测试转发，不代表官方集成通过。
2. **报告完整性必须匹配真实行。** `result_completeness`比较声明行数与实际数组；缺集合、非法行、空状态有行、内嵌错误或数据包错误不能被success覆盖。没有声明行数时可根据已有合法数组计算；不新增查询协议。空结果原先的limited规则不变，不要求Top-N返回行数等于全量分组数。
3. **同一结果从门禁到报告保持一致。** 原始 `claim_ledger` 与紧凑 `rows` 都按同一集合消费；不在呈现之前伪造空数组抹掉缺失证据。报告仍保留原始数据，副作用入口重新计算门禁；合成集成测试确认声明“完整”不能绕过缺行拒绝。
4. **分组数不能冒充源行数。** `ranking_evidence.population_count`只用于分组口径，不填入 `population_row_count`；缺源行证据继续未知。
5. **图表数值与布局。** 比例条几何使用Decimal比值，避免有限大数转float溢出；标签保留原值，长数值/标题有足够空间，负值与未知不伪装成零。保持Windows原字体依赖；仅测试时可替换已安装字体，不能把替换结果当Windows像素金样通过。
6. **上下文测量跨平台。** 文本按规范LF与UTF-8测量，避免相同内容仅因CRLF多计字节。仅重新测量实际变化的artifacts节；不把字节推成Token，不改质量阈值。

## 证据与验收方式

本轮以全部147个现有测试模块为收集范围，每个模块在独立Python子进程运行并记录错误/跳过/超时；缺宿主导入占位不算业务题。新增测试使用合成结果、临时目录和mock投递，不访问生产。最终ZIP重新解压后再验证；工作目录测试与交付物重复回归不累加成独立业务覆盖率。

官方宿主、真实模型/数据库/企微并未在本次接入。不得把本文当作宿主认证、SQL实数验真、业务签字或上线批准。未完成项继续归入既有验收任务，不用新增运行框架替代证据。

## 复核命令（维护者在隔离环境执行）

先核对实际源版本与未提交修改。完整包和patch二选一，不覆盖整个运行Home。使用已安装声明版本Hermes的Python、空的实验Home、未填真实凭据的配置；不要在现役Profile中运行带外发或调度的手工入口。

```sh
# 当前工作目录：datasage-canary-next
python -m unittest discover -s tests -p 'test_revision_regressions.py' -v
python -m unittest discover -s tests -p 'test_report_completeness.py' -v
python -m unittest discover -s tests -p 'test_workflow_report_gate.py' -v
python -m unittest discover -s tests -p 'test_report_presentation.py' -v
python -m unittest discover -s tests -p 'test_context_cost_baseline.py' -v
python -m unittest discover -s tests -p 'test_source_export.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

真实宿主下重点补齐：原生Plugin注册与Skill发现/平台过滤；session与secret作用域；记忆压缩与并发身份；公开Schema经过宿主处理后的形状；官方_FileLock互斥；Windows原字体像素金样。维持独立业务Ground Truth和留出题，不将合成测试替代业务正确性。

维护测量的路径顺序统一为区分大小写的POSIX字符串，避免Windows与Linux对SKILL.md产生不同顺序。本批使用临时Git索引核对交付清单并更新当前文件/行数/关联测量，原Git历史统计保持原样；临时索引不证明原仓库历史。Git维护成本仍需在真实clone核验历史快照与变更范围，不能捏造历史测量。实际新增或修改能力按原维护规则归位。

## 回退

只撤回本批源码、测试、实测记录和说明；不回滚用户后续改动。没有生产数据或外发状态需要本批回退。实际回退前先核对patch能否逆向应用，不执行强制reset。
