from __future__ import annotations

from typing import Literal, Protocol

from shovel.capability.domain import CapabilityScope
from shovel.execution.domain import Execution
from shovel.security.domain import ApprovalRequest, GateEvaluation, UserSecurityPolicy


class SecurityPolicyProvider(Protocol):
    """Trusted, read-only authority; resolution must not acquire database write locks."""

    async def load(self, scope: CapabilityScope) -> UserSecurityPolicy: ...


class ExecutionGate(Protocol):
    async def evaluate(self, execution: Execution) -> GateEvaluation: ...


class ApprovalAuthorizer(Protocol):
    async def authorize(
        self,
        scope: CapabilityScope,
        user_id: str,
        execution_id: str,
        request_id: str,
        action: Literal["approve", "reject"],
    ) -> None: ...


class ApprovalRepository(Protocol):
    async def get(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
    ) -> ApprovalRequest: ...
    async def decide(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
        action: Literal["approve", "reject", "expire"],
        gate: ExecutionGate,
    ) -> ApprovalRequest: ...
