执行 Milestone C / Phase 11：Main Agent Integration、FastAPI 和 Typer / Rich CLI。

所有入口必须调用相同 Application Services。

禁止：

- Main Agent 直接访问 ORM
- API 直接访问 ORM
- CLI 直接访问 ORM

Main Agent 集成：

1. Capability Discovery
2. InvocationService
3. Create Skill Draft
4. Create Workflow Draft
5. Create Proactive Service Draft
6. Validate Draft
7. Explain Proactive Decision
8. List / Pause / Resume Services
9. Approve / Reject Execution
10. Provide Input
11. View Execution Status

Main Agent 不能：

- 静默 Publish
- 静默启用高风险自动化
- 将普通“好的”视为任意高风险 Approval
- 扩大 Invocation Permission

FastAPI：

为现有 Application Services 提供最小 API，不复制业务逻辑。

CLI 至少支持：

Capability：

- list
- show
- search

Skill：

- list
- show
- validate
- publish
- versions

Workflow：

- list
- show
- validate
- run
- publish
- versions

Proactive：

- list
- show
- pause
- resume
- enable
- disable
- run
- history

Approval：

- list
- show
- approve
- reject

Execution：

- list
- show
- cancel
- resume

要求：

- Rich 输出遵循现有风格
- CLI 错误对用户友好
- 内部异常日志保留诊断信息
- Approval 使用结构化 ID
- Publish 使用结构化确认步骤

测试：

- API and CLI share services
- Agent and UI-visible state agreement
- No ORM access from entry points
- Draft creation
- Explicit publish
- Explicit approval
- Explain last execution
- Permission enforcement

完成后停止。