# R23 核验记录：超时、取消、并发与部分失败

日期：2026-09-23｜依据：综合审查 V2.0 的 R23（批次 E / P1）｜负责角色：执行层维护＋测试
前置：R06、R15、R17（已完成）｜性质：既有执行层覆盖复跑 + 两个空档补齐；隔离环境注入实测待做

## 1. 结论

- **执行层的既有覆盖很厚且本轮复跑通过**（38 项）：期限约束连接与语句预算、过期期限不建连、
  连接/语句失败不被包装且一定关闭、快照复用单连接、失败后释放槽位、并行失败释放租约、
  慢读游标排空与回滚被取消、截断/部分失败不冒充基线、重试预算耗尽保留原始失败。
- **本轮补上两个空档**（此前无用例）：
  1. **预算/期限不跨请求继承**——超时请求结束后，下一个请求不得继承上一个请求的期限；
     嵌套调用取更紧的期限并在退出后恢复。
  2. **定时器与线程不残留**——正常完成的请求必须取消并回收期限定时器线程；
     已过期的期限立即中止 socket 并抛出超时信号，而不是返回成功或空值。
- 结论：**超时不会被记成成功或空值**（既有 6 项 + 本轮 1 项），**独立成功分支保留**
  （既有 `test_completed_branch_survives_a_later_timeout` 等），**重试有上限**
  （既有 `test_retry_budget_exhaustion_preserves_original_component_failure` 等）。
- 真实隔离环境下的慢查询注入、连接断开、模型取消与饱和并发仍需人工执行（第 5 节），未标通过。

## 2. 证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 执行器契约复跑 | 15 项通过（期限预算、过期不建连、失败不包装且关闭、快照单连接、安全确认失败原样抛出并关闭） | `tests/test_db_executor.py` |
| 超时线上表现复跑 | 6 项通过 | `tests/test_remediation_timeout_wire.py` |
| 只读轨迹复跑 | 17 项通过 | `tests/test_readonly_skill_trace.py` |
| 期限不跨请求 | 超时退出后 `current_deadline()` 为 None；嵌套取更紧的期限 | `tests/test_execution_budget_isolation.py::test_a_new_request_does_not_inherit_the_previous_deadline` |
| 定时器不残留 | 完成后 `stop()` 使定时器线程结束，线程集合回到基线，未中止 socket | 同文件 `::test_a_completed_request_leaves_no_timer_thread_behind` |
| 过期即中止并超时 | 过期期限立即 shutdown/close 并抛 `DeadlineExceeded`，无定时器线程 | 同文件 `::test_an_expired_deadline_aborts_and_reports_a_timeout` |
| 超时不等于成功/空值 | 过期期限在作用域内抛超时信号而非产出行 | 同文件 `::test_a_timeout_is_never_a_success_or_an_empty_value` |
| 槽位释放（既有） | 8 实体共享 4 全局槽；单个实体失败后释放；并行失败释放租约供下一批使用 | `test_parallel_worker_failure_releases_leased_slots_for_next_full_batch` 等 |
| 并发不串身份（既有） | 绑定身份不跨并发线程泄漏；并发初始化只发布一个快照 | `test_bound_identities_do_not_leak_between_concurrent_threads` 等 |

## 3. 六场景矩阵

| 场景 | 机制 | 现有覆盖 | 状态 |
| --- | --- | --- | --- |
| 慢查询 | 期限控制连接/语句预算 + 定时器中止 socket | `test_deadline_controls_connection_and_statement_budgets`、`::test_deadlines_after_connect_and_fetch_close_single_statement`、`::test_expired_deadline_does_not_connect` | 离线已覆盖 |
| 连接断开 | 连接/语句失败原样抛出并一定关闭、回滚失败也尝试关闭 | `test_connection_failure_is_not_wrapped`、`test_statement_failure_is_not_wrapped_and_single_always_closes`、`test_close_is_attempted_when_rollback_fails` | 离线已覆盖 |
| 模型取消 | 游标排空与回滚被取消；身份不跨界；超时信号类型化 | `test_slow_drip_read_cursor_drain_and_rollback_are_cancelled`、`test_connection_authentication_is_cancelled_before_security_queries` | 离线已覆盖（模型侧取消需真实回放） |
| 返回截断 | 截断不冒充基线、页预算不返回部分源、`has_more` 语义 | `test_page_budget_does_not_return_partial_source`、`test_failed_or_partial_never_becomes_baseline_and_html_escapes` | 离线已覆盖 |
| 单分支失败 | 完成分支在后续超时中保留；组件部分失败标记 unknown；重试预算耗尽保留原失败 | `test_completed_branch_survives_a_later_timeout`、`test_component_partial_failure_unknown_and_forced_new_cycle`、`test_retry_budget_exhaustion_preserves_original_component_failure` | 离线已覆盖 |
| 饱和并发 | 4 全局槽 + 租约；失败释放槽位/租约；并发身份隔离 | `test_eight_entities_share_four_global_slots`、`test_entity_receives_deadline_and_releases_slot_after_failure`、`test_parallel_worker_failure_releases_leased_slots_for_next_full_batch` | 离线已覆盖 |

## 4. 与验收条件的对应

| R23 验收条件 | 本轮状态 |
| --- | --- |
| 不把 timeout 记成功或空值 | 既有 6 项 + 本轮 1 项（过期抛类型化超时） |
| 独立成功分支保留 | 既有：完成分支在后续超时中保留；部分失败时有效分支保留 |
| 重试有上限 | 既有：重试预算耗尽保留原始组件失败；消息超时阻止一切重试 |
| 并发无串数据 | 既有：绑定身份不跨并发线程泄漏 |
| 预算跨请求污染 | 本轮补充：超时请求不把期限留给下一个请求 |
| 无无限等待 | 本轮补充：定时器线程不残留；过期立即中止 |

## 5. 需要人工完成的隔离环境实测

| # | 场景 | 观察点 |
| --- | --- | --- |
| 1 | 注入慢查询（受控 sleep/锁等待） | 超时按类型化失败返回、不记为成功或零，连接关闭 |
| 2 | 执行中断开数据库连接 | 失败原样抛出、连接与事务被清理、不重试 |
| 3 | 生成中途取消模型/会话中断 | 连接、线程、槽位最终释放；无残留外发，无身份残留 |
| 4 | 饱和并发（超过槽位数的并发请求） | 排队有界、无串数据、预算不被其它请求消耗 |
| 5 | 同一进程内连续两次请求（第一次超时） | 第二次拿到全新预算，不继承上一次的期限 |

以上均需真实库或受控隔离环境，离线无法替代。

## 6. 回退

- 单项优化/预算回退即可；出现异常范围时停用受影响能力，不通过无限重试或隐藏失败提升成功率。
- 本轮未改执行器代码，只新增测试与记录。
