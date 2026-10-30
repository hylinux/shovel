执行 Milestone B / Phase 10：Execution Wakeup、Retry、Recovery 和 Interaction。

实现：

1. ExecutionWakeupService
2. execution_wakeups persistence
3. Retry scheduling
4. Snooze
5. Delay
6. Approval expiration
7. Input expiration
8. Scheduled resume
9. RecoveryService
10. Interaction
11. Notification Delivery
12. In-App persisted notification
13. Console / Rich notification

严格区分：

Business Schedule：
ScheduleTriggerService

Execution Timer：
ExecutionWakeupService

Snooze 必须实现为：

Execution suspended
+
wake_at

禁止为每次 Snooze 创建 Business Schedule。

Retry：

- transient vs permanent
- max attempts
- bounded exponential backoff
- next retry
- attempt history
- no infinite retry
- non-idempotent external write cannot be blindly retried

Recovery：

- stale running → interrupted
- waiting approval can recover
- waiting input can recover
- suspended can recover
- retry scheduled can recover
- completed non-idempotent external side effect cannot repeat

严格区分：

ApprovalRequest：
审批事实

Interaction：
用户交互投影

Notification：
投递机制

Interaction 类型：

- information
- approval
- input_request
- warning
- result

Interaction Action：

- VIEW
- APPROVE
- REJECT
- DISMISS
- SNOOZE
- CONFIGURE
- PROVIDE_INPUT

测试覆盖：

- Snooze without business schedule
- Wakeup resume
- Retry
- Max attempts
- Permanent failure
- Crash recovery
- Waiting approval recovery
- Waiting input recovery
- Approval expiration
- Interaction creation
- Notification delivery
- No duplicated non-idempotent side effect

完成 Milestone B 集成测试后停止。