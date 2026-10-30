执行 Phase 1：Repository Analysis 和 Architecture Invariant Compliance Review。

本阶段只做分析。不要修改业务代码、数据库 Schema 或公共 API。

请分析当前仓库中的：

- Generic Host
- Dependency Injection
- Hosted Services
- Microsoft Agent Framework 集成
- Main Agent / Agent Runtime
- Tool / Function / Plugin 系统
- Skill 抽象
- Workflow / Pipeline 抽象
- Memory Service
- Zvec 集成
- SQLAlchemy / SQLite
- Repository 与 Unit of Work
- Migration
- Typer / Rich CLI
- FastAPI
- Next.js / AG-UI
- Configuration
- Logging
- Background Workers
- Event Bus
- Scheduler
- Tests

逐条检查以下 15 条架构不变量：

1. Capability 永远不拥有 Trigger。
2. Proactive Service 不实现业务逻辑。
3. 只有一个持久化 Execution 模型。
4. 只有一个 ExecutionEngine。
5. Published Capability Version 不可变。
6. Execution 启动前冻结 Version 和 Dependency Lock。
7. Permission 与 Approval 相互独立。
8. 权限只能缩小。
9. External Side Effect 经过全部 Gate。
10. 所有入口共用 Application Services。
11. Event 与 Durable Execution 的边界明确。
12. Schedule Trigger 与 Execution Wakeup 分离。
13. Workflow 不包含外部 Trigger。
14. 禁止 User-authored arbitrary code。
15. Main Agent 只能创建 Draft，显式用户操作才能 Publish 或批准敏感操作。

针对每条不变量输出：

- Current state
- Evidence from the repository
- Compliant / Partially compliant / Not compliant / Not implemented
- Architecture conflict
- Required change
- Migration risk
- Components that can be reused

还需识别：

- 是否已经存在重复 Runtime
- 是否已经存在多个 Task Model
- Workflow 是否拥有 Trigger
- Permission 和 Approval 是否耦合
- Version 是否可变
- 是否已有 Registry
- 是否已有可靠的 Execution State Machine
- 是否存在动态 import、eval、exec 或任意脚本执行
- 是否存在 UI/CLI/Agent 直接访问 ORM

不要仅根据类名判断，请检查实际职责。

停止条件：

输出 Repository Findings、Invariant Compliance Matrix、Reusable Components、Architecture Conflicts、Missing Foundations 和 Risks 后停止。不要开始设计或编码。