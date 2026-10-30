from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from shovel.capability.domain import CapabilityScope, Digest, Permission, Value
from shovel.runtime.errors import RuntimeFailure

type SideEffectKind = Literal["none", "local_write", "external_write", "destructive"]
type RiskLevel = Literal["low", "medium", "high", "critical"]


class SecurityDenied(RuntimeFailure):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=403)


class ApprovalPreference(StrEnum):
    ALWAYS = "ALWAYS"
    RISK_BASED = "RISK_BASED"
    EXTERNAL_ACTIONS = "EXTERNAL_ACTIONS"
    MINIMUM_REQUIRED = "MINIMUM_REQUIRED"


class UserSecurityPolicy(Value):
    user_grants: tuple[Permission, ...] = ()
    platform_constraints: tuple[Permission, ...] = ()
    preference: ApprovalPreference = ApprovalPreference.MINIMUM_REQUIRED
    allowed_side_effects: tuple[SideEffectKind, ...] = ("none",)
    require_approval: bool = False


class PermissionSources(Value):
    user_grants: tuple[Permission, ...]
    invocation_grants: tuple[Permission, ...]
    platform_constraints: tuple[Permission, ...]
    capability_boundary: tuple[Permission, ...]
    proactive_boundary: tuple[Permission, ...] | None = None
    workflow_boundary: tuple[Permission, ...] | None = None
    skill_boundary: tuple[Permission, ...] | None = None
    parent_permissions: tuple[Permission, ...] | None = None
    frozen_permissions: tuple[Permission, ...] | None = None


class RiskAssessment(Value):
    level: RiskLevel
    side_effect: SideEffectKind


class ApprovalDecision(Value):
    reasons: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return bool(self.reasons)


class GateEvaluation(Value):
    permissions: tuple[Permission, ...]
    risk: RiskAssessment
    approval: ApprovalDecision
    binding_hash: Digest


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalRequest(Value):
    id: str
    execution_id: str
    scope: CapabilityScope
    user_id: str
    number: int = Field(ge=1)
    binding_hash: Digest
    reasons: tuple[str, ...]
    state: ApprovalState
    created_at: float
    expires_at: float
    decided_at: float | None = None
    consumed_at: float | None = None
    consumed_attempt: int | None = None
