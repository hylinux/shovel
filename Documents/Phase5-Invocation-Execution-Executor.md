执行 Milestone A / Phase 5：Invocation、Single Durable Execution 和 ExecutionEngine。

必须遵守：

- 只有一个 Execution 模型
- 只有一个 ExecutionEngine
- 不创建 ProactiveTask、SkillTask、WorkflowTask、AgentTask 或 FunctionTask
- Capability 类型通过 Executor 执行

实现：

1. Invocation 和 InvocationContext。
2. Execution Domain Model。
3. ExecutionStep，如当前设计需要。
4. ExecutionAttempt。
5. ExecutionStateMachine。
6. 唯一的 ExecutionEngine。
7. CapabilityExecutor Protocol。
8. FunctionExecutor。
9. SkillExecutor Skeleton 或现有 Agent Framework Adapter。
10. WorkflowExecutor Skeleton。
11. AgentExecutor Adapter。
12. Parent / Child Execution。
13. root_execution_id、parent_execution_id、correlation_id。
14. Version Resolution 和 Version Freeze。
15. Execution 创建前解析具体 Capability Version。
16. 固定 Dependency Lock。
17. ExecutionBudget：
    - max duration
    - max steps
    - max child executions
    - max call depth
    - max agent turns
    - max function calls
18. capability_call_path 和递归检测。
19. 集中管理合法状态转换。
20. 支持：
    - create
    - start
    - dispatch
    - suspend
    - resume
    - cancel
    - succeed
    - fail

本阶段不要实现：

- Schedule
- Event Bus
- Proactive Service
- Workflow Graph 执行
- Approval UI
- Retry Worker
- Recovery Worker

测试必须验证：

- 只有一个 Execution 生命周期模型
- 各 Executor 不管理独立生命周期
- 合法和非法状态转换
- Parent / Child Execution
- Correlation propagation
- Exact version freeze
- Dependency lock freeze
- Budget consumption
- Call depth
- Recursive capability detection
- Cancel
- Fail
- Complete

完成后运行相关验证并停止，不要开始 Phase 6。