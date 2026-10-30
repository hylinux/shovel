from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue

from shovel.capability.domain import CapabilityScope
from shovel.execution.domain import (
    BudgetUsage,
    CapabilitySnapshot,
    ChildInvocation,
    Execution,
    ExecutionAttempt,
    ExecutionLease,
    ExecutionSnapshot,
    ExecutionState,
    ExecutionStep,
    ExecutorResult,
)

if TYPE_CHECKING:
    from shovel.security.ports import ExecutionGate
    from shovel.workflow.ports import WorkflowContext


@dataclass(frozen=True)
class ExecutorContext:
    execution: Execution
    consume: Callable[[BudgetUsage], Awaitable[Execution]]
    call: Callable[[ChildInvocation], Awaitable[Execution]]
    workflow: WorkflowContext | None = None


class CapabilityExecutor(Protocol):
    async def execute(self, context: ExecutorContext) -> ExecutorResult: ...


class ExecutionRepository(Protocol):
    async def find_request(self, scope: CapabilityScope, key: str) -> Execution | None: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> Execution: ...
    async def queued(self, identifier: str | None = None) -> Execution | None: ...
    async def replay(
        self,
        scope: CapabilityScope,
        fingerprint: str,
        *,
        request_key: str | None = None,
        parent_id: str | None = None,
        child_key: str | None = None,
    ) -> Execution | None: ...
    async def create(
        self,
        snapshot: ExecutionSnapshot,
        payload: dict[str, JsonValue],
        fingerprint: str,
        *,
        request_key: str | None = None,
        parent: ExecutionLease | None = None,
        child_key: str | None = None,
    ) -> Execution: ...
    async def start(self, scope: CapabilityScope, identifier: str) -> ExecutionLease: ...
    async def dispatch(self, lease: ExecutionLease, gate: ExecutionGate) -> Execution: ...
    async def consume(self, lease: ExecutionLease, delta: BudgetUsage) -> Execution: ...
    async def finish(
        self,
        lease: ExecutionLease,
        state: ExecutionState,
        result: ExecutorResult,
        *,
        error: str | None = None,
    ) -> Execution: ...
    async def cancel(self, scope: CapabilityScope, identifier: str) -> list[str]: ...
    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution: ...
    async def reject(self, scope: CapabilityScope, identifier: str, error: str) -> Execution: ...
    async def attempts(self, scope: CapabilityScope, identifier: str) -> list[ExecutionAttempt]: ...
    async def steps(self, scope: CapabilityScope, identifier: str) -> list[ExecutionStep]: ...
    async def check_snapshot(self, snapshot: CapabilitySnapshot, scope: CapabilityScope) -> None: ...
