# 业务验收试点：18个独立案例

这是验收准备，不是18次真实模型测试。全部数值来自手写合成业务记录；预期结果不从插件YAML、SQL或模型回答生成。`fixtures/business_acceptance_cases.json` 是逐案源记录、独立预期和回答边界。它不进入运行时提示，不授予发布/生产资格。

## 执行与评审

1. 冻结当前Git commit、官方Hermes版本、模型配置和本次案例版本。先运行离线回归；夹具算术自检只能证明案例没有明显数字矛盾。
2. 合成回放时，将源记录映射为受控测试数据库或查询证据，用实际Profile走完整会话。不得把合成身份用于真实数据库；这些逻辑业务夹具尚不是五域物理SQL数据库种子。记录实际工具请求、返回证据、最终答案和多轮上下文。
3. 真实数据试点必须使用合法绑定的WeCom或受信replay入口，并先取得期间/部门范围和独立对账数据。不得新增账号权限、伪造身份或默认允许发消息。选择少量低成本、只读查询开始；没有模型预算时不调用模型。
4. 人工分别检查事实、证据遗漏、因果边界、行动建议和澄清质量，按0/1/2记录错误/部分满足/充分满足，并附答案和证据定位。关键金额错误、隐藏缺失证据、无依据因果、越权执行均单列阻断问题，不能由平均分掩盖。
5. 延迟从用户输入到最终答案实测，包含工具时间；成本记录真实provider usage与当次价格依据。缺失的usage、价格和成本保持空值，不以零代替。7个评价维度必须都有记录或明确未测原因。

不能用小样本或只通过夹具自检宣称“已达到L3”或“业务专家成功率”。真实公司数据对账、模型回答质量及企微交付分别记录。已迁移的 `business_replay.py` 可验证给定会话导出的工具配对/结束边界，但不能替人工判断回答质量。

## 案例索引

| ID | 业务域 | 问题 | 核心独立预期 |
| --- | --- | --- | --- |
| B01 | delivery | 毛出库、退货与净额 | {"gross_delivery_rmb":150000,"returns_rmb":15000,"net_delivery_rmb":135000} |
| B02 | delivery | 退货率分母 | {"return_rate":0.1,"net_rmb":180000} |
| B03 | delivery | 跨单位数量 | {"cross_unit_total":null,"separate_quantities":{"米":100,"件":20,"tao":3}} |
| B04 | receipt | 登记收款与实际到账 | {"net_registered_receipts_rmb":100000,"settled_receipts_rmb":98000} |
| B05 | receipt | 缺失汇率 | {"complete_rmb_total":null,"known_partial_rmb":70,"missing_rows":1,"known_rows":1,"data_state":"incomplete"} |
| B06 | receivable | 净应收与正向风险敞口 | {"net_balance_rmb":130000,"positive_exposure_rmb":150000} |
| B07 | receivable, receivable | 已确认逾期明细 | {"overdue_total_rmb":60000,"amount_priority":"甲","age_priority":"乙"} |
| B08 | inventory | 库存成本覆盖不足 | {"complete_inventory_rmb":null,"known_partial_rmb":100000,"known_rows":1,"missing_rows":1} |
| B09 | inventory | 周转效率证据不足 | {"inventory_turnover_days":null,"efficiency_grade":null} |
| B10 | target, delivery | 销售分摊目标诊断 | {"allocated_net_actual_rmb":80000,"completion_rate":0.64,"shortfall_rmb":45000} |
| B11 | target | 目标未设置与目标为零 | {"甲_completion_rate":null,"乙_completion_rate":null,"甲_target_state":"missing","乙_target_state":"zero"} |
| B12 | receivable | Top5缺少全量分母 | {"top5_subtotal_rmb":100000,"company_share":null} |
| B13 | delivery, receipt, receivable, target, inventory | 跨域经营判断 | {"profitability_assessment":null,"complete_company_health_assessment":null} |
| B14 | delivery | 变化贡献不等于原因 | {"total_delta":200000,"A_delta":300000,"B_delta":-100000,"A_contribution":1.5,"B_contribution":-0.5} |
| B15 | delivery | 多轮纠正取代旧范围 | {"final_scope":"HCM/2026-08/customer_dept","final_net_rmb":400000} |
| B16 | delivery, receipt, target | 部分失败保留有效证据 | {"net_delivery_rmb":135000,"target_completion":0.75,"receipt_rmb":null,"overall_status":"partial"} |
| B17 | delivery | 实体角色澄清 | {"initial_action":"clarify_entity_role","final_role":"department"} |
| B18 | receivable, receivable | 高影响建议需要审批 | {"may_execute_freeze":false,"confirmed_cause":null} |

当前全部案例的执行状态为 `not_run`。真实业务入口、独立对账依据、模型预算及外发许可尚未用于本轮验收；不进行真实企微发送。

补充：案例JSON中的execution_status指真实模型业务验收，仍为not_run；已完成的离线SQL/接口覆盖单独见BUSINESS_COVERAGE.md，不以它推导模型成功率。
