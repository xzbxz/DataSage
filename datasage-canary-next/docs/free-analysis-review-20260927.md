# 自由分析审查与修复 · 2026-09-27

本批次基于 `DataSage-codex-remediation-20260926 (1).zip`，源标记
`7b8322ac51482d82affaeb40ce7c8485988a7732`。标记不是已验证的远程HEAD或网关加载版本。
目标：复用Hermes原生理解/规划/Skill，不把临时分析条件等同永久KPI，不以解除权限或暴露任意SQL冒充自由分析。

## 重要事实

包内没有日志、会话、状态数据库，无法还原2026-09-26 15:34/15:39实际调用。
用户贴出的451/437/13仅是对话报告数字，本轮未验真。新的源码已经有 `analysis`，
有9个指标登记字段（应收原币分析有执行限制），并不是只能使用固定75%档。
源测试中的451/437主例属于合成oracle，不是实际日志；其低价14卷不能当作真实答案。

## 已处理

1. 价格切片SELECT中的 `g.*` 与守卫输出重复使用 `analysis_gross_rolls/quantity`。
   改为显式无重复投影，未知价格时只有known-subset，完整毛量仍为NULL。
2. 切片沿用了未筛选母集的 `metric_data_state` 和缺失计数。按选中行及未归档退货
   计算切片状态；不动无analysis的旧KPI计算。排除记录缺数不污染完整切片；毛量
   完整并不代表净量完整。
3. 应收原币发布分析字段但执行只允许人民币。目录增加由同一执行要求提供的说明，
   不允许静默换币种；普通原币查询与显式RMB counterpart保留。
4. SOUL不再把10k–50k、30天和0.8当成常驻用户条件；保留期间、账本与未知边界。
   需要时先读取当前能力，支持的临时条件不需新KPI批准；不强制每题固定流程。
5. Skill读取适用于实际暴露原生能力的企微和CLI，参数来自实际宿主Schema；
   更换跨域混合的无效示例为两个独立可执行示例。
6. 补自由分析验收题：历史成交价不是当前促销价，>75%集合不能挑<=50%，净KPI差
   不能反推任意低档；新读取不是原15:35快照。

## 本地复核

在隔离副本、实际Hermes Python环境中运行：

```bash
python -B -m unittest discover -s tests -p test_free_analysis_independent.py -v
python -B -m unittest discover -s tests -p test_analysis_*.py -v
python -B -m unittest discover -s tests -p test_monthly_slow_pool.py -v
python -B -m unittest discover -s tests -p test_slow_baseline_net_outbound.py -v
python -B -m unittest discover -s tests -p test_source_export.py -v
python -B tests/context_cost.py check
python -B tests/maintenance_cost.py check
```

新增测试使用独立内存SQLite兼容子集和手算源行，模拟execute_query只证明SQL/证据链，
不是MySQL全方言、真实授权、真实模型或企微验证。现有缺宿主测试不能删除或记通过。
`free_analysis_review_cases.json`提供18类业务回放题，全部初始not_run；不另造评分平台。

## 运行真相所需材料

仅提取该成员该会话09-26 15:30–15:45的脱敏轨迹：实例/进程、宿主版本、加载源码
与合同摘要、模型ID、实际工具Schema、实际SOUL/Skill快照或摘要、catalog/skill/query
参数和返回状态、错误、数据读取时间、最终回答。不得附.env、密钥、全库state或其他
成员私人会话。需要比较旧会话与新会话，区分旧进程、旧工具面、方法未消费和模型早拒。
缺这些证据时不能把“部署旧版”或“模型降智”写成已证根因。

## 保留与边界

无analysis时的固定高折>75%及退货定义不变；251个指标的机器合同、实体、权限、
数据库连接要求、模型设置、调度和发送范围不修改。没有新增Agent/Planner/Router。
当前自由分析仍受已治理字段、运算、聚合和数据覆盖约束，不是任意事实层浏览。
登记字段没有等同全部可运行组合；原币应收联合分析仍受限。

完整包与changes.patch二选一，只在实验分支合并；不得覆盖整个运行Home。代码和
SOUL/Skill变化需要在正式批准的宿主重新加载/新会话后核验，本包不执行服务重启。
