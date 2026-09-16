# 企业微信应用通知：文字与附件一起迁移

## 实现范围

`wecom_app_transport.AppTransport` 是 Profile 内的有限 HTTP 发送模块，统一由
`workflow_io.deliver_components` 调用。滞销任务、客户 ZIP、周/月报、价格变化、
货源工作簿均复用此路径；货源工作簿现在带同一通知的说明文字。
没有复制平台、调度器、权限系统或发送队列，没有改 Hermes 官方源码。

复用 Hermes `load_gateway_config()` 读取既有 `wecom_callback` 应用配置，按
应用名、企业 ID、agent ID 精确匹配；不启动 callback 服务或机器人 WebSocket。
配置中的凭据只在内存使用，不复制到 Profile Git、回执或错误消息。
HTTP 使用现有 httpx，固定企业微信 HTTPS 域名，TLS 校验开启，不跟随跳转，
不自动重试消息；令牌只做本次执行的内存缓存。没有新依赖安装。

## 配置契约（仅示例，不代表已安装/启用）

现有 `local-report-bindings.json` 的 `target_map` 可显式选择：

```json
{
  "old-reviewed-account": {
    "platform": "wecom_app_http",
    "app_name": "existing-app-name",
    "corp_id": "existing-corp-id",
    "agent_id": "1000000",
    "target_kind": "user",
    "target_id": "explicit-userid"
  }
}
```

`target_kind=user` 是单个企业成员；`target_kind=appchat` 是该应用可访问的应用群，
不是机器人群、客户群或 webhook URL。运行前分别用 `user/get`、`appchat/get` 校验。
成员必须处于启用状态；最终应用可见权限仍以发送接口回执为准。
不允许 `@all`、多账号拼接、默认群回退、混合 HTTP/WS 通道。
文字和附件始终采用同一映射。原有业务接收名单/角色规则不变；缺失映射直接停止。

原有独立动作开关仍默认关闭；本次没有创建运行配置、读取真实人员、上传文件、
发送消息、冻结、接受价格基线、注册/启用任务、重载网关或推送远程。

## 一条通知如何执行

1. 所有附件先在本地生成；校验路径、类型、大小、ZIP/XLSX 结构及内容指纹。
   附件只能位于本 Profile 的 `report_runs/legacy_execution` 下；支持 XLSX、ZIP、PNG，
   单文件不超过 20 MiB。中文文件名通过 multipart 原样传递。
2. 获得跨任务、同应用同目标的有限锁；按固定顺序获取多个锁，竞争时停止本轮，不排队。
3. 验证目标，并通过 `media/upload` 上传本批次所有待发附件。任意上传失败或超时，
   本批次不开始发文字。已上传素材不是业务消息，上传失败后的重传不代表重发消息。
4. 按通知顺序先文字后文件，人员走 `message/send`，应用群走 `appchat/send`。
   超过 2048 UTF-8 字节的文字按字符边界无损拆分，所有文字片段先于附件。
5. 每个组成部分分别保存 in-flight、接口接受、明确失败或未知回执；所有组成部分
   都获接口接受，才将整条通知标为接口接受。`errcode=0` 但有无效/无权限收件人时失败。
   不保存原始 HTTP 错误/令牌 URL，不把接口接受称为员工已读。

文字和文件是独立 API 请求，不能承诺原子同时送达，也没有回滚或撤回承诺。
锁保证本机这些任务的提交顺序，不能控制企业微信服务端/客户端显示顺序或其他发送者。

## 部分失败、重跑与旧强制场景

- 文字明确失败：停止这条通知，文件不发；之后可重试未成功部分。
- 文字成功、文件明确失败：保留文字回执；即使业务调用 force，也只补文件。
- 任意消息超时、异常或崩溃后留下 in-flight：结果未知，停止自动重发，force 不能绕过。
- 同一目标保留未完成通知的归属。明确失败允许原通知恢复；其他任务停止，避免插入文字。
  未知状态阻止该目标后续通知，需先人工核实。状态仍在既有业务进度目录，不是发送队列。
- 同一 run 内重复调用不重发；普通重跑跳过已接受通知。
- 原任务强制重发、周报后月报强制刷新、IDK 再提醒的业务调用保留。只有上一通知完整
  成功且当前没有待恢复失败时才建立新的强制发送代次，先整体持久化代次，再发文字。
  旧代次回执归档，不能拿上一代次的文件成功来掩盖本代次的文件失败。
- 恢复时检查目标、文字和附件语义指纹及完整成员清单。内容/目标已变化则停止复核，
  不能把旧文字与新附件混为一条成功通知。ZIP 时间戳/压缩差异不算业务内容变化。

## 依据与验证范围

旧仓库 `3fd38fcb803307e1688688ca1dfbde271131157a` 的
`datasage-core/send_slow_customer.py` `_send_file`、`send_slow_report.py`
已经使用应用 `media/upload` + `message/send`；这是本次恢复的能力，不是新业务规则。
当前官方 callback 仅实现文字，WS 子进程路径有媒体/连接限制，这些事实只限制原
`OfficialTransport`，不再作为整个迁移不能发送附件的理由。

官方接口文档位置：
- https://developer.work.weixin.qq.com/document/path/90236 （应用消息）
- https://developer.work.weixin.qq.com/document/path/90253 （临时素材）
- https://developer.work.weixin.qq.com/document/path/90248 （应用群消息）

本轮尝试联网打开这些官方文档，但网页抓取失败；未宣称完整核验当前官方文档。
实现依据本机旧版可读代码、当前 Hermes 源码及上述接口契约，真实 API 兼容性、
权限和员工端呈现仍需明确指定测试账号后验收。隔离测试用 httpx.MockTransport，
测试进程禁止外部 socket 和真实 Profile/官方源码写入，不构成真实送达证明。

尚需配置的仅是原有应用身份与显式目标映射、相应动作开关；测试账号/范围未指定，
不在本轮进行真实端到端发送。旧采购运行周期等独立迁移缺口不因本次通道完成而消失。
