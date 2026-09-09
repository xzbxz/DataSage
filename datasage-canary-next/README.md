# DataSage Canary Next

这是 DataSage Profile 的源码目录，也是当前 `datasage-canary-next` 运行目录。
当前维护方式是在现有 Git 工作区和活动分支上原地修改、测试、审查与提交；
不创建第二个安装实例，也不使用 `hermes profile install/update` 覆盖这个目录。

`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据属于用户态，
不得提交，也不得由发行更新覆盖。企微和数据库权限不属于发行流程的修改范围。

当前候选版本：`0.15.0-rc14`。

## 定位与使用边界

DataSage 面向公司经营负责人及出库、销售、应收、财务、库存和目标负责人，
覆盖 delivery、receipt/collections、receivable、target、inventory 和
customer_risk 六个经营域。它提供受治理证据的事实、诊断和分级建议，不是
公共研究、普通写作、用户文件分析器，也不批准或执行业务决策。
当前候选目标成熟度为 L3 数据专家；本候选版本不声明 L4 主动管理或主动巡检能力。

WeCom 入口声明 `clarify`、`datasage-query` 和官方 `code_execution` 三个 toolset，所有已认证企微成员
均可私聊和群聊，并共享六个经营域同一完整的 DataSage 查询面。Profile 不施加
用户、群组、部门、实体、行或领域过滤；这不替代外部数据库授权。数据库执行仍为
SELECT-only，并由治理查询合同、只读执行限制和 evidence 边界约束。

官方 `execute_code` 用于处理已取得的业务结果，沿用 Hermes 原生执行和审批机制，
不另建恢复工具或分页协议。当前本机 Python 可以读取和修改进程权限内的文件；
这不是仅限查询结果的文件隔离环境。DataSage 查询工具的 SELECT-only 约束不限制
Python 的文件访问。原生审批是否再次提示取决于已有审批状态，当前配置也未将审批
限定为机器所有者。

业务员个人目标、分摊实际和完成率使用 `salesperson_allocation` 的 split 账本作为
独立权威来源；transaction-detail 基表仅用于诊断，不决定销售分摊覆盖，也不得在
split 缺失时回填、混入或替代个人业绩口径。

本候选也支持 salesperson→customer 的 split 普通下钻；净收款登记额按收款拆分登记额
减退款拆分登记额解释，不代表实结或到账。期间或分解证据不完整时必须披露限制，因果
分解不作无证据推断；canary live replay 仍 pending。企微全员私聊/群聊与数据库业务
权限保持不变。

rc14 落实本轮出库事实口径：8 项数量指标恢复查询，必须按单位分组或限定单一单位，
`y`、`Pcs/PCS`、`m2/㎡` 分别解释为码、件、平方米且禁止跨单位合计；默认部门固定为
`customer_dept`，`biz_dept`/`biz_region` 作为独立业务发生归属；退货 `biz_org` 与
销售/出库 `org_name` 经业务 owner 确认为同一组织口径，各事实仍独立聚合；条码
`final_supplier` 仅开放于物理毛出库。历史原始码 `tao` 暂只按原码单列，不擅自解释。
企微全员私聊/群聊和数据库业务权限策略保持不变；正式 L3 live replay 与 150 案例门槛
仍 pending。

建议分为三档：描述性监测、诊断性解释、高影响建议。高影响建议必须带有
假设、负责审批的人、重大风险和复核点；证据歧义、不可用、过期或不完整时，
升级给相应指标/域负责人或人工审批人。

## 当前 Git 原地维护与 Hermes 原生能力

当前宿主为官方稳定版 `0.21.1`。维护只在当前 Git Profile 内进行：修改、离线测试、审查、提交，再通过官方 Hermes 重新加载。Git commit/tag 是版本与回退依据；不再维护 distribution manifest、自建发布 receipt、晋级脚本或第二个安装实例。运行时继续保留 `plugin.yaml` 等官方加载元数据。

内建 Skill 由 Hermes 官方同步。当前启用 `docx`、`xlsx`、`pdf`、`powerpoint`，并接受宿主必需的 `hermes-agent`；其他当前与遗留内建入口通过 `skills.disabled` 关闭。`ocr-and-documents` 已不在当前 core 集合，不再声明为可用原生能力。升级时检查 `tests/fixtures/reviewed_host_skills.json` 的快照差异和 `related_skills`，再有选择地更新。调试期间显式关闭 `curator.enabled`，避免自动改变能力集合；background review 也保持关闭。

普通 CLI 可以编辑和测试源码，但普通 CLI 身份不能直接调用业务查询；业务入口仍要求已绑定的 WeCom 身份或合法 trusted replay。工具可见性不等于业务授权，本次没有扩展任何权限。

所有 Hermes 维护命令显式使用 `-p datasage-canary-next`。离线检查使用现有官方宿主 Python，在本目录运行 `python -B -m unittest discover -s tests -p "test_*.py" -v`。测试保留 SQL、数值完整性、证据、授权、并发、时限、宿主装配和会话配对验证，不再验证已删除的发布系统。

`tests/business_replay.py` 只解析已有会话导出并验证工具配对和结束边界，不读取会话数据库、不调用模型、不发送消息，也不授予上线资格。真实业务验收独立于源码回归；量化目标仍见 `ARCHITECTURE.md`，不得把计划门槛写成已达到的成绩。

LSP 使用 Profile 中官方 npm 的 Windows `.cmd` 入口。Git 操作和维护不得覆盖 `.env`、认证、state、sessions、logs、Memory 或更改企微与数据库权限。

DataSage 当前没有有 owner 的定时任务，因此不向 Hermes `cron` toolset 暴露
`datasage-query`，本候选版本不声明 L4 主动管理能力。未来新增主动巡检时，必须同时定义任务 owner、调度身份与权限、
超时/重试和端到端测试，不能只恢复配置项。
