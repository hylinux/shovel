执行 Milestone A / Phase 6：Permission、Authorization、Risk、Policy 和 Approval Core。

必须严格区分：

Permission：
是否有权执行。

Approval：
已经有权执行的操作是否需要用户确认。

Approval 永远不能授予 Permission。

实现或适配：

1. PermissionResolver
2. AuthorizationService
3. RiskEvaluator
4. PolicyEvaluator
5. ApprovalResolver
6. ApprovalRequest Domain Model
7. ApprovalService

权限模型：

EffectiveGrantedPermissions 来源于：

- User grants
- Invocation grants
- Proactive Service boundary
- Workflow boundary
- Skill boundary
- Platform constraints

CapabilityVersion 声明：

RequiredPermissions

授权条件：

RequiredPermissions 必须是 EffectiveGrantedPermissions 的子集。

Child Execution 的权限不能大于 Parent Execution。

Approval Preference：

- ALWAYS
- RISK_BASED
- EXTERNAL_ACTIONS
- MINIMUM_REQUIRED

禁止使用 NEVER。

最终 Approval Decision 来源于：

- User preference
- Capability risk
- Side-effect metadata
- Explicit Workflow Approval Node
- Platform minimum

任何一层要求 Approval，就必须 Approval。

ApprovalRequest 必须：

- 绑定 execution_id
- 绑定 user_id

- 有 expiration
- 防重放
- 只能消费一次
- 支持 approve / reject / expire
- Approval 后重新进行 Authorization 和 Policy 检查

External Side Effect 必须依次经过：

Authorization
→ Risk
→ Policy
→ Approval
→ Execution

测试覆盖：

- Required vs granted permissions
- Child permission narrowing
- Permission escalation denied
- Approval cannot grant missing permission
- ALWAYS
- RISK_BASED
- EXTERNAL_ACTIONS
- MINIMUM_REQUIRED
- Platform override
- Destructive action
- External write
- Approval expiration
- Replay prevention
- Wrong user
- Wrong execution
- Re-check after approval

本阶段不实现 UI 和真实外部操作。

完成测试后停止，不要开始 Phase 7。