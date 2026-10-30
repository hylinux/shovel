执行 Milestone A / Phase 7：Platform Kernel Persistence、Migration 和 Kernel Integration Verification。

目标是将 Phase 4 到 Phase 6 的核心模型可靠持久化并验证集成。

实现或完善：

1. CapabilityDefinition Repository
2. CapabilityVersion Repository
3. Invocation Repository，如设计需要
4. Execution Repository
5. ExecutionStep Repository
6. ExecutionAttempt Repository
7. ApprovalRequest Repository
8. Transaction boundary / Unit of Work
9. 必要数据库 migration
10. 唯一索引和查询索引
11. JSON 字段的 SQLite 兼容
12. user_id 隔离
13. 状态更新的 optimistic concurrency 或等价保护
14. Execution idempotency key
15. Approval consumption uniqueness
16. Capability ID + Version uniqueness

必须验证：

- Domain Model 与 ORM Model 不被不必要地耦合
- Application Service 不直接拼 SQL
- UI/CLI/Agent 不直接访问 ORM
- Published Version 无法被 Repository 原地更新
- Execution Version Snapshot 可恢复
- Approval 防重复消费
- 数据库存储中不存在 Secret

运行：

- migration up
- migration test
- repository tests
- kernel integration tests
- formatter
- linter
- type checker
- full backend unit tests

输出 Milestone A 完成报告，包括：

- Implemented
- Deferred
- Known limitations
- Schema
- Tests
- Architecture invariant compliance

停止。不要开始 Automation。