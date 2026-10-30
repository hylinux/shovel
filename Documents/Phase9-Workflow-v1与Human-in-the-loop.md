执行 Milestone B / Phase 9：Trigger-Agnostic Workflow V1 和 Human-in-the-loop。

Workflow Definition 中禁止保存：

- Schedule Trigger
- Event Trigger
- Cron Trigger
- Proactive Trigger

V1 仅实现以下 Node：

- Input / Start
- Function
- Skill
- Condition
- Approval
- Ask User
- Notification
- Emit Event
- Output / End

V1 不实现：

- Parallel
- Join
- Dynamic Fan-out
- Loop
- Arbitrary Script
- BPMN Compatibility

实现：

1. WorkflowDefinition / WorkflowVersion
2. Nodes / Edges
3. Input / Output Mapping
4. Workflow Validation
5. WorkflowExecutor
6. Child Execution
7. Dependency Lock
8. Workflow checkpoint
9. Approval Node
10. Ask User Node
11. waiting_for_approval
12. waiting_for_input
13. Resume
14. Output production
15. Failure path

Publish Validation 检查：

- Start / End
- Unreachable node
- Unknown node
- Missing capability
- Draft dependency
- Disabled dependency
- Invalid mapping
- Invalid condition
- Capability cycle
- Permission boundary
- Dependency lock
- Risk summary
- Invalid output

Approval Node 是强制审批节点，不能被 Service Approval Preference 绕过。

Ask User 不能将普通聊天内容绑定到错误的 Execution。Input Response 必须关联：

- interaction_id
- execution_id
- user_id

测试覆盖：

- Simple workflow
- Function node
- Skill node
- Condition branch
- Approval pause and resume
- Ask User pause and resume
- Child Execution
- Version freeze
- Dependency lock
- Invalid graph
- Cycle detection
- Permission narrowing
- Draft dependency rejection
- Trigger absent from workflow schema

完成后停止。