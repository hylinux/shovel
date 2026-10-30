执行 Milestone B / Phase 8：Event、Schedule Trigger 和 Proactive Service。

必须遵守：

- Proactive Service 不实现业务逻辑
- Workflow 不拥有 Trigger
- Condition 不是独立 Trigger Engine
- Schedule Trigger 与 Execution Wakeup 分离
- Event 在 Execution 持久化前不是 Durable
- 不声称 Exactly Once

实现：

1. NormalizedEvent
2. EventBus Protocol
3. InProcessEventBus
4. Event handler failure isolation
5. Bounded concurrency
6. Graceful start and stop
7. ScheduleDefinition
8. ScheduleTriggerService
9. Schedule occurrence key
10. ProactiveService
11. ProactiveService revision
12. ProactiveServiceManager
13. ConditionEvaluator
14. Event / Schedule matching
15. InvocationService 集成
16. Execution 成功持久化后的 Durable Boundary
17. Manual Run Now

Trigger 类型仅支持：

- Schedule
- Event
- Manual Run

Condition Operator：

- eq
- ne
- contains
- in
- exists
- gt
- gte
- lt
- lte

禁止 eval 和 exec。

ProactiveService 只包含：

- trigger
- condition
- target capability
- version policy
- input mapping
- permission boundary
- approval preference
- notification policy
- quiet hours
- cooldown
- revision

ScheduleTriggerService 只发布 Event，不直接运行 Capability。

处理：

- UTC
- timezone
- DST
- invalid schedule
- missed schedule policy
- duplicate occurrence
- restart
- disabled schedule

测试覆盖：

- Event dispatch
- Multiple handlers
- Failure isolation
- Stop behavior
- Schedule occurrence dedupe
- Timezone and DST
- Disabled schedule
- Event matching
- Condition matching
- Service enabled / disabled
- Service revision snapshot
- Version policy resolution
- Manual Run Now
- Event-to-durable-execution boundary

本阶段不实现 Workflow Graph 或 UI。

完成后停止。