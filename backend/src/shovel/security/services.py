from __future__ import annotations

import hashlib
import json
from typing import Literal

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityScope,
    Permission,
)
from shovel.execution.domain import CapabilitySnapshot, Execution, InvocationContext
from shovel.security.domain import (
    ApprovalDecision,
    ApprovalPreference,
    ApprovalRequest,
    GateEvaluation,
    PermissionSources,
    RiskAssessment,
    SecurityDenied,
    UserSecurityPolicy,
)
from shovel.security.ports import (
    ApprovalAuthorizer,
    ApprovalRepository,
    ExecutionGate,
    SecurityPolicyProvider,
)


class PermissionResolver:
    def resolve(self, sources: PermissionSources) -> tuple[Permission, ...]:
        sources = PermissionSources.model_validate(sources.model_dump(mode="json"))
        permissions = set(sources.user_grants)
        for boundary in (
            sources.invocation_grants,
            sources.platform_constraints,
            sources.capability_boundary,
            sources.proactive_boundary,
            sources.workflow_boundary,
            sources.skill_boundary,
            sources.parent_permissions,
            sources.frozen_permissions,
        ):
            if boundary is not None:
                permissions.intersection_update(boundary)
        return tuple(sorted(permissions, key=lambda item: (item.action, item.resource)))


class DefaultSecurityPolicyProvider:
    async def load(self, scope: CapabilityScope) -> UserSecurityPolicy:
        return UserSecurityPolicy()


class AuthorizationService:
    def __init__(self, provider: SecurityPolicyProvider | None = None) -> None:
        self.provider = provider or DefaultSecurityPolicyProvider()
        self.permissions = PermissionResolver()

    async def authorize(
        self,
        capability: CapabilitySnapshot,
        context: InvocationContext,
        *,
        frozen_permissions: tuple[Permission, ...] | None = None,
        parent_permissions: tuple[Permission, ...] | None = None,
    ) -> tuple[tuple[Permission, ...], UserSecurityPolicy]:
        policy = await self.provider.load(context.scope)
        policy = UserSecurityPolicy.model_validate(policy.model_dump(mode="json"))
        granted = self.permissions.resolve(
            PermissionSources(
                user_grants=policy.user_grants,
                invocation_grants=context.permission_ceiling,
                platform_constraints=policy.platform_constraints,
                capability_boundary=capability.content.permission_boundary,
                proactive_boundary=context.proactive_boundary,
                workflow_boundary=context.workflow_boundary,
                skill_boundary=context.skill_boundary,
                parent_permissions=parent_permissions,
                frozen_permissions=frozen_permissions,
            )
        )
        if not set(capability.content.required_permissions).issubset(granted):
            raise SecurityDenied("Required permissions are not granted.")
        return granted, policy


class RiskEvaluator:
    def evaluate(self, content: CapabilityContent) -> RiskAssessment:
        levels = ("low", "medium", "high", "critical")
        level = content.risk_profile.level
        side_effect = content.side_effect_profile.kind
        minimum = {
            "none": "low",
            "local_write": "medium",
            "external_write": "high",
            "destructive": "critical",
        }
        effective = max((level, minimum[side_effect]), key=levels.index)
        return RiskAssessment(level=effective, side_effect=side_effect)


class PolicyEvaluator:
    def evaluate(self, content: CapabilityContent, risk: RiskAssessment, policy: UserSecurityPolicy) -> None:
        if risk.side_effect not in policy.allowed_side_effects:
            raise SecurityDenied("Platform policy prohibits this side effect.")
        if content.connection_requirements:
            raise SecurityDenied("Connection-backed execution is not enabled.")


class ApprovalResolver:
    def resolve(
        self,
        content: CapabilityContent,
        risk: RiskAssessment,
        policy: UserSecurityPolicy,
        *,
        workflow_approval_required: bool = False,
        proactive_preference: ApprovalPreference | None = None,
    ) -> ApprovalDecision:
        reasons: list[str] = []
        for source, preference in (("user", policy.preference), ("proactive", proactive_preference)):
            if preference == ApprovalPreference.ALWAYS:
                reasons.append(f"{source}_always")
            if preference == ApprovalPreference.RISK_BASED and risk.level != "low":
                reasons.append(f"{source}_risk_based")
            if preference == ApprovalPreference.EXTERNAL_ACTIONS and risk.side_effect in {
                "external_write",
                "destructive",
            }:
                reasons.append(f"{source}_external_actions")
        if content.approval_requirement == "always":
            reasons.append("capability_requires_approval")
        if workflow_approval_required:
            reasons.append("workflow_approval")
        if policy.require_approval:
            reasons.append("platform_requires_approval")
        if risk.level in {"high", "critical"} or risk.side_effect in {"external_write", "destructive"}:
            reasons.append("platform_minimum")
        return ApprovalDecision(reasons=tuple(reasons))


class SecurityGate:
    def __init__(self, authorization: AuthorizationService | None = None) -> None:
        self.authorization = authorization or AuthorizationService()
        self.risks = RiskEvaluator()
        self.policies = PolicyEvaluator()
        self.approvals = ApprovalResolver()

    async def prepare(
        self,
        capability: CapabilitySnapshot,
        context: InvocationContext,
        *,
        frozen_permissions: tuple[Permission, ...] | None = None,
        parent_permissions: tuple[Permission, ...] | None = None,
    ) -> tuple[tuple[Permission, ...], UserSecurityPolicy, RiskAssessment, ApprovalDecision]:
        permissions, policy = await self.authorization.authorize(
            capability,
            context,
            frozen_permissions=frozen_permissions,
            parent_permissions=parent_permissions,
        )
        risk = self.risks.evaluate(capability.content)
        self.policies.evaluate(capability.content, risk, policy)
        approval = self.approvals.resolve(
            capability.content,
            risk,
            policy,
            workflow_approval_required=context.workflow_approval_required,
            proactive_preference=context.proactive_preference,
        )
        return permissions, policy, risk, approval

    async def evaluate(self, execution: Execution) -> GateEvaluation:
        context = execution.snapshot.context
        if execution.checkpoint and execution.checkpoint.get("wait_reason") == "waiting_for_approval":
            context = context.model_copy(update={"workflow_approval_required": True})
        permissions, policy, risk, approval = await self.prepare(
            execution.snapshot.capability,
            context,
            frozen_permissions=execution.snapshot.granted_permissions,
        )
        binding = {
            "execution_id": execution.id,
            "snapshot": execution.snapshot.model_dump(mode="json"),
            "input": execution.input,
            "permissions": [item.model_dump(mode="json") for item in permissions],
            "policy": policy.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "approval": approval.model_dump(mode="json"),
        }
        if execution.checkpoint is not None:
            binding["checkpoint"] = execution.checkpoint
        digest = hashlib.sha256(
            json.dumps(
                binding,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return GateEvaluation(permissions=permissions, risk=risk, approval=approval, binding_hash=digest)


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        gate: ExecutionGate,
        *,
        authorizer: ApprovalAuthorizer | None = None,
    ) -> None:
        self.repository, self.gate, self.authorizer = repository, gate, authorizer

    async def get(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        *,
        user_id: str,
    ) -> ApprovalRequest:
        return await self.repository.get(scope, execution_id, request_id, user_id)

    async def _decide(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
        action: Literal["approve", "reject"],
    ) -> ApprovalRequest:
        await self.get(scope, execution_id, request_id, user_id=user_id)
        if self.authorizer is None:
            raise SecurityDenied("Explicit human approval authorization is not configured.")
        await self.authorizer.authorize(scope, user_id, execution_id, request_id, action)
        return await self.repository.decide(
            scope,
            execution_id,
            request_id,
            user_id,
            action,
            self.gate,
        )

    async def approve(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        *,
        user_id: str,
    ) -> ApprovalRequest:
        return await self._decide(scope, execution_id, request_id, user_id, "approve")

    async def reject(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        *,
        user_id: str,
    ) -> ApprovalRequest:
        return await self._decide(scope, execution_id, request_id, user_id, "reject")

    async def expire(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        *,
        user_id: str,
    ) -> ApprovalRequest:
        return await self.repository.decide(
            scope,
            execution_id,
            request_id,
            user_id,
            "expire",
            self.gate,
        )
