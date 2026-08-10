# E2E 结果报告生成（从 state.db 提取真实回答）

Worked example: 26 条生产问题 E2E → `workspace/E2E_测试报告_26题.md`（929 行，41.7KB）。

## 为什么不能直接用 e2e_result_*.json

`e2e_runner.py` 捕获的是 `hermes chat -q` 的完整 CLI stdout：
- `answer_tail` = stdout 尾部（含 Reasoning/工具调用渲染、ANSI 装饰）
- `answer_len` = 整个 stdout 长度（26 条里最大 42131 chars）——不是模型最终回答的长度

模型最终回答存在 `state.db` 的 `messages` 表里，`content` 字段：
- 角色 `role='assistant'`、无 `tool_calls`、`content != ''`
- 取该会话**最后一条**这样的消息即完整回答（2600-1700 字符不等）

## state.db 关键表结构

```
sessions: id, title, started_at(epoch float), end_reason, api_call_count, message_count, ...
messages: id, session_id, role, content, tool_calls, tool_name, timestamp, ...
```

- 按问题找会话：`SELECT session_id FROM messages WHERE role='user' AND content=? ORDER BY timestamp DESC`
- 取最终回答：`SELECT content FROM messages WHERE session_id=? AND role='assistant' AND (tool_calls IS NULL OR tool_calls='') AND content != '' ORDER BY id DESC LIMIT 1`
- 超时项取 clarify 提问：`SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name='clarify' ORDER BY id DESC LIMIT 1`（`question`/`choices_offered` 在 JSON 里）
- 会话时间：`started_at` 是 epoch float，不是字符串；会话 ID 形如 `20260807_163217_074d0e`（时间戳内嵌）

## 合并两轮结果

超时重测后合并：`merged = {}`；`for d in prod + retry: merged[d["id"]] = d`（retry 后写覆盖）。
合并后才有最终结论：26 条 = 24 完成 + 2 超时（等待澄清）+ 0 硬错误。

## 报告格式（用户偏好：完整、可分享）

```
# DataSage 生产问题 E2E 测试报告
**测试时间** / **测试方式** / **总体结果**（完成/超时/硬错误 + 百分比）
## N. <问题原文>
- **类别**：<六域+子类>
- **状态**：✅ 完成 | ⏱ 超时（等待澄清）
- **耗时**：<秒>s
- **退出码**：0 | TIMEOUT
**等待澄清**（超时项）：> clarify 的 question（含 choices_offered 摘要）
模型已正确……（说明这是规范行为，真人对话中会正常完成）
**回答**：
```<模型最终回答>```
## 汇总
- 完成/超时/硬错误 计数与百分比
- 成功项耗时统计：最短/最长/平均
- 2 条超时说明（逐条：什么歧义、模型做了什么、为什么无人值守会挂起）
- 说明：回答为模型最终输出（不含工具调用过程）；超时项耗时=上限截断值
```

## 清理 ANSI

```
t = re.sub(r"\x1b\[[0-9;]*m", "", t)
t = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", t)
t = re.sub(r"\n{3,}", "\n\n", t)
```

## 超时项的真实性质（判断而非标签）

超时 ≠ 失败。日志/会话证据显示超时项往往是：
- 实体多候选 → 模型正确 `clarify`（如 8883 匹配多个产品）
- 口径不明 → 模型正确追问（如"家纺"无精确匹配 → 问家纺客户/部门/产品）
- `user_response` 字段显示 "The user did not provide a response within the time limit. Use your best judgement..."——证明是等用户而非卡死

判定方法：查该会话的 clarify tool 结果，有 `question`+`choices_offered` 即"等待澄清"；无 clarify 且工具调用序列完整但超时，才是慢（240s 内 170-190s 完成属正常慢查询）。
