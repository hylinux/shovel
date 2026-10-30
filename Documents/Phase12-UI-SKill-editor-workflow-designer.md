执行 Milestone C / Phase 12：Proactive UI、Approval Center、Skill Editor 和 Workflow Designer V1。

先检查当前 Next.js / AG-UI 架构。复用现有前端，不引入第二套前端技术栈。

所有页面调用已有 API / Application Services。

不得在前端实现 Authorization Truth。

服务端始终重新进行：

- Authorization
- Policy
- Approval
- Version validation

实现最低可用 UI。

1. Proactive Services

展示：

- Name
- Status
- Trigger
- Target Capability
- Capability Version
- Approval Preference
- Notification Policy
- Last Activity
- Next Trigger

操作：

- Run Now
- Pause
- Resume
- Enable
- Disable
- Edit
- History

2. Edit Proactive Service

支持：

- Name
- Description
- Trigger
- Condition
- Target Skill / Workflow
- Version Policy
- Input Mapping
- Permission Boundary
- Approval Preference
- Notification Preference
- Quiet Hours
- Cooldown

Approval Preference：

- Always ask me
- Ask based on risk
- Ask before external or sensitive actions
- Don't ask unless Shovel requires approval

禁止提供“Disable all safety approval”之类的选项。

3. Approval Center

展示：

- Requested action
- Service
- Workflow
- Risk
- Side effect
- Reason
- Expiration
- Approve
- Reject
- View details

4. Activity

展示：

- Execution
- Invocation source
- Service revision
- Capability
- Capability version
- Status
- Decision
- Approval
- Duration
- Result
- Error

5. Skill Editor V1

支持：

- Name
- Description
- Instructions
- Input Schema
- Output Schema
- Allowed Capabilities
- Memory Access
- Permission Boundary
- Risk Summary
- Test
- Save Draft
- Validate
- Publish
- Version History

6. Workflow Designer V1

Node Palette 仅包含：

- Input
- Skill
- Function
- Condition
- Approval
- Ask User
- Notification
- Emit Event
- Output

严禁加入：

- Schedule Trigger
- Event Trigger
- Cron Trigger
- Script Node
- Parallel
- Join
- Loop

可视化 Risk、Permission、Approval 和 Validation Error。

测试：

- UI uses API
- Same state visible from CLI and UI
- Approval cannot be forged client-side
- Publish explicit
- Service control
- Global pause / resume
- Workflow has no trigger nodes
- Skill cannot select unauthorized capability

完成后停止。