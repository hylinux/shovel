执行 Phase 3：Detailed Implementation Plan。

基于已批准的 Repository Analysis、15 条架构不变量和 ADR，生成可实施计划。本阶段不写业务代码。

将工作拆分为：

Milestone A：Platform Kernel
Milestone B：Automation
Milestone C：Experience

对每个 Milestone 列出：

1. 新增文件
2. 修改文件
3. 保持不变的文件
4. 每个文件的职责
5. Domain Model
6. Application Services
7. Interfaces / Protocols
8. Infrastructure Adapters
9. SQLAlchemy Models
10. Database Migration
11. Public API 变化
12. Backward Compatibility
13. Tests
14. Verification Commands
15. Rollback Strategy
16. 安全风险
17. 实施顺序

必须明确：

- 不创建 ProactiveTask 表
- 不创建多套 Execution 表
- 不让 UI/CLI/Agent 直接访问 ORM
- 不在 Workflow 中保存 Trigger
- 不把 Snooze 保存为业务 Schedule
- 不把 Secret 保存进 CapabilityVersion 或 Workflow
- 不在 V1 实现 Parallel、Join、Loop、Script Node
- 不在 V1 实现真实外部写操作

为数据库修改给出单独的 Database Change Plan：

- Existing tables reused
- New tables
- Modified tables
- Unique constraints
- Indexes
- Foreign keys
- JSON columns
- Migration order
- Data migration requirements

停止条件：

输出完整 File Change Plan、Database Change Plan、Test Plan 和分批提交建议后停止。不要实现 Phase 4。