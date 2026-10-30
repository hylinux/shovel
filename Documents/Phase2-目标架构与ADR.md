执行 Phase 2：Target Architecture 和 ADR Proposal。

基于已经完成的 Repository Analysis，设计适配当前 Shovel 仓库的目标架构。不要机械照搬通用模板，不要修改业务代码。

必须保持以下边界：

CAPABILITY PLANE

CapabilityDefinition
CapabilityVersion
CapabilityRegistry

Capability 类型：

- Function
- Skill
- Workflow
- Agent

AUTOMATION PLANE

- NormalizedEvent
- ScheduleTriggerService
- ProactiveService
- ConditionEvaluator

EXECUTION PLANE

- InvocationService
- Execution
- ExecutionEngine
- CapabilityExecutor
- FunctionExecutor
- SkillExecutor
- WorkflowExecutor
- AgentExecutor
- ExecutionWakeupService
- RecoveryService

CONTROL AND SECURITY

- PermissionResolver
- AuthorizationService
- RiskEvaluator
- PolicyEvaluator
- ApprovalResolver
- ApprovalService

INTERACTION PLANE

- ApprovalRequest
- Interaction
- Notification Delivery
- Ask User / Input Request

严格遵守：

1. 删除或避免 ProactiveTask、SkillTask、WorkflowTask 等平行生命周期模型。
2. 不新建多个生命周期 Runtime。
3. Workflow 不包含 Schedule/Event/Cron Trigger。
4. Proactive Service 只保存 Trigger、Condition、Capability Reference、Input Mapping 和用户 Policy。
5. Permission 先于 Approval，Approval 不能补足 Permission。
6. CapabilityVersion 承载 required permissions、risk、side effect 和 Dependency Lock。
7. Execution 创建时冻结具体版本。
8. ScheduleTriggerService 与 ExecutionWakeupService 分开。
9. ApprovalRequest、Interaction 和 Notification 分开建模。
10. Main Agent、UI、CLI 和 API 使用相同 Application Services。

请提出以下 ADR：

- Single Durable Execution Model
- Single Execution Engine with Executors
- Capabilities Are Trigger-Agnostic
- Proactive Service Contains No Business Logic
- Permission and Approval Are Independent
- Permission Can Only Narrow
- Immutable Published Versions
- Dependency Lock
- Durable Execution Boundary
- Schedule Trigger vs Execution Wakeup
- No Arbitrary User Code
- Explicit Publish and Approval
- Shared Application Services
- Connection Reference Instead of Embedded Secrets
- Proactive Service Revision Snapshot

每个 ADR 输出：

- Context
- Decision
- Alternatives considered
- Consequences
- Migration impact
- Current repository compatibility

同时给出 Mermaid 架构草图，但不要创建实现代码。

停止条件：

输出 Target Architecture、Domain Boundaries、Application Service Boundaries、ADRs、Migration Strategy 和 Decisions Requiring Confirmation 后停止。