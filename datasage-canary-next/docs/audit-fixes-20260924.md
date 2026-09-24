# DataSage audit-fix-20260924

状态：源码定向修复＋离线验证；不是已批准生产发布。
输入ZIP：DataSage-main (1).zip。输入SHA256：6b07d1ec493c15dbef413f0c30e12a127795741503ab132f3f767106496b8adc。
来源标记：0dd1f4d6d8d6352ac3d4ba004f3c91e17d2b3603；保留0.15.0-rc14版本声明，不冒充上游提交。

## 实施范围

- R07：冻结dict禁止普通`|=`，dict/list禁止重新初始化；拒绝操作后共享缓存不变。显式deepcopy可修改。API护栏不是安全沙箱。
- R08：YAML显式键在原节点被merge展开前检查一次，合法alias/merge复用保持，真正重复键拒绝。
- R05：离线数字、算术和表文校验使用肯定陈述数值；常见假设、引用、否定、待核验、问句、阈值与表格上下文转待验证；正常纠错不把被否定旧值混入。`m2`单位数字不再误提取。
- R29：vendor指纹按相对POSIX字符串排序、UTF-8编码；25个依赖文件未修改。
- R12：结构检查验证Rule ID和链接身份，允许正常互链与说明围栏。SKILL索引不强制新增Rule ID；示例内声明不参与结构检查。语义正确性仍需业务评审。
- 导出白名单包含新增helper、25条回归测试与本说明。无新常驻路由、规则引擎、Finalizer或发布平台。

## 未改

15份合同文件、7域215指标、查询SQL/实体、公开Schema、三个工具注册、SOUL、config、ARCHITECTURE、模型配置、权限、调度、投递和真实状态均未改。原ZIP和原始副本保持不变。

## 验证与边界

完成的选定离线测试记录：133成功、1项因子进程缺hermes_cli失败、6个测试模块因缺agent/gateway无法导入、2跳过。导入占位不是业务用例，不计算业务通过率。272个Python文件语法可解析。25条新增回归和原有17条答案测试通过。详细记录在单独交付的V4报告与证据包。

没有官方锁定宿主、生产DB、真实模型/企微和Git历史的完整验收，不把静态资料或人工维护记录作为本次运行通过证明。评分器仍是有限的确定性辅助，不认证任意自然语言；不熟悉的表达保留独立复核。此前台账的“全部源码关闭”不代表正式验收。

## 最低本机复核

在声明的官方Hermes Python环境和Profile根目录运行：

```bash
python -m unittest discover -s tests -p test_revision_regressions.py -v
python -m unittest discover -s tests -p test_answer_ground_truth.py -v
python -m unittest discover -s tests -p test_source_export.py -v
python -m unittest discover -s tests -p test_expert_authority_inventory.py -v
python -m unittest discover -s tests -p test_remediation_runtime_immutability.py -v
```

再执行既有全回归、独立业务真值与真实网关探针，并在真实Git环境中评审更新context/maintenance基线。只在实验分支合并，不覆盖运行Home、.env、会话、Memory、state或调度。正式加载/重启需维护方授权。回退只回退本批源码和说明，不覆盖用户后来产生的状态。
