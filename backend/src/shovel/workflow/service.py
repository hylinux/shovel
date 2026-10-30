import hashlib
from dataclasses import dataclass

from pydantic import JsonValue

from shovel.automation.domain import NormalizedEvent
from shovel.automation.ports import EventBus
from shovel.capability.domain import CapabilityScope, WorkflowNode
from shovel.execution.domain import ExecutionLease
from shovel.runtime.errors import RuntimeFailure
from shovel.security.ports import ExecutionGate
from shovel.workflow.domain import InputRequest, WorkflowCheckpoint
from shovel.workflow.ports import InputAuthorizer, WorkflowContext, WorkflowRepository


@dataclass(frozen=True)
class _Bound:
    repository: WorkflowRepository
    lease: ExecutionLease
    gate: ExecutionGate
    bus: EventBus | None

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        await self.repository.save(self.lease, checkpoint, self.gate)

    async def ask(self, node: WorkflowNode) -> InputRequest:
        return await self.repository.ask(self.lease, node)

    async def notify(self, node: WorkflowNode, payload: dict[str, JsonValue]) -> str:
        await self.repository.check(self.lease, self.gate)
        return await self.repository.notify(self.lease, node, payload)

    async def emit(self, node: WorkflowNode, payload: dict[str, JsonValue]) -> str:
        if self.bus is None or node.event_type is None:
            raise RuntimeFailure("Workflow EventBus is not configured.")
        await self.repository.check(self.lease, self.gate)
        identifier = "workflow_" + hashlib.sha256(f"{self.lease.execution.id}:{node.id}".encode()).hexdigest()
        await self.bus.publish(
            NormalizedEvent(
                id=identifier,
                scope=self.lease.execution.snapshot.context.scope,
                event_type=node.event_type,
                source="workflow",
                occurred_at=self.lease.execution.created_at,
                payload=payload,
            )
        )
        return identifier


class WorkflowService:
    def __init__(
        self,
        repository: WorkflowRepository,
        gate: ExecutionGate,
        *,
        bus: EventBus | None = None,
        authorizer: InputAuthorizer | None = None,
    ) -> None:
        self.repository, self.gate, self.bus, self.authorizer = repository, gate, bus, authorizer

    def bind(self, lease: ExecutionLease) -> WorkflowContext:
        return _Bound(self.repository, lease, self.gate, self.bus)

    async def get_input(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, *, user_id: str
    ) -> InputRequest:
        return await self.repository.get_input(scope, execution_id, interaction_id, user_id)

    async def respond(
        self,
        scope: CapabilityScope,
        execution_id: str,
        interaction_id: str,
        *,
        user_id: str,
        response: JsonValue,
    ) -> InputRequest:
        await self.repository.get_input(scope, execution_id, interaction_id, user_id)
        if self.authorizer is None:
            raise RuntimeFailure("Trusted input authorization is not configured.", status=503)
        await self.authorizer.authorize(scope, execution_id, interaction_id, user_id)
        return await self.repository.respond(
            scope, execution_id, interaction_id, user_id, response, self.gate
        )
