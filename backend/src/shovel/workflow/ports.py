from typing import Protocol

from pydantic import JsonValue

from shovel.capability.domain import CapabilityScope, WorkflowNode
from shovel.execution.domain import ExecutionLease
from shovel.security.ports import ExecutionGate
from shovel.workflow.domain import InputRequest, WorkflowCheckpoint


class WorkflowContext(Protocol):
    async def save(self, checkpoint: WorkflowCheckpoint) -> None: ...
    async def ask(self, node: WorkflowNode) -> InputRequest: ...
    async def notify(self, node: WorkflowNode, payload: dict[str, JsonValue]) -> str: ...
    async def emit(self, node: WorkflowNode, payload: dict[str, JsonValue]) -> str: ...


class InputAuthorizer(Protocol):
    async def authorize(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, user_id: str
    ) -> None: ...


class WorkflowRepository(Protocol):
    async def check(self, lease: ExecutionLease, gate: ExecutionGate) -> None: ...
    async def save(
        self, lease: ExecutionLease, checkpoint: WorkflowCheckpoint, gate: ExecutionGate
    ) -> None: ...
    async def ask(self, lease: ExecutionLease, node: WorkflowNode) -> InputRequest: ...
    async def notify(
        self, lease: ExecutionLease, node: WorkflowNode, payload: dict[str, JsonValue]
    ) -> str: ...
    async def get_input(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, user_id: str
    ) -> InputRequest: ...
    async def respond(
        self,
        scope: CapabilityScope,
        execution_id: str,
        interaction_id: str,
        user_id: str,
        response: JsonValue,
        gate: ExecutionGate,
    ) -> InputRequest: ...
