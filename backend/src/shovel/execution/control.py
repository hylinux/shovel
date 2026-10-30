from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from shovel.capability.domain import CapabilityContent, CapabilityScope, Value
from shovel.execution.domain import Execution, ExecutionLease
from shovel.runtime.errors import RuntimeFailure


class TransientExecutionError(RuntimeFailure):
    """Only trusted adapters classify failures as transient."""


class RetryPolicy(Value):
    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_seconds: float = Field(default=1, gt=0, le=60)
    maximum_seconds: float = Field(default=60, gt=0, le=3600)

    def backoff(self, retry: int) -> float:
        delay = self.initial_seconds
        for _ in range(min(retry, 10)):
            delay = min(delay * 2, self.maximum_seconds)
        return min(delay, self.maximum_seconds)


class Wakeup(Value):
    id: str
    execution_id: str
    wake_at: float
    kind: Literal["delay", "snooze", "retry", "resume", "recovery", "child_ready"]
    state: Literal["pending", "applied", "cancelled"]


class ControlRepository(Protocol):
    async def timer(
        self,
        scope: CapabilityScope,
        identifier: str,
        wake_at: float,
        kind: str,
        *,
        interaction_id: str | None = None,
    ) -> Wakeup: ...
    async def suspend(self, lease: ExecutionLease, wake_at: float) -> Execution: ...
    async def retry(self, lease: ExecutionLease, policy: RetryPolicy, safe: bool) -> Execution: ...
    async def tick(self) -> int: ...
    async def recover(self, safe: SafeReplay, policy: RetryPolicy) -> int: ...


class SafeReplay(Protocol):
    def __call__(self, content: CapabilityContent) -> bool: ...


class ExecutionWakeupService:
    def __init__(self, repository: ControlRepository, *, policy: RetryPolicy | None = None) -> None:
        self.repository, self.policy = repository, policy or RetryPolicy()

    async def delay(self, scope: CapabilityScope, identifier: str, *, wake_at: float) -> Wakeup:
        return await self.repository.timer(scope, identifier, wake_at, "delay")

    async def snooze(
        self, scope: CapabilityScope, identifier: str, *, wake_at: float, interaction_id: str | None = None
    ) -> Wakeup:
        return await self.repository.timer(
            scope, identifier, wake_at, "snooze", interaction_id=interaction_id
        )

    async def schedule_resume(self, scope: CapabilityScope, identifier: str, *, wake_at: float) -> Wakeup:
        return await self.repository.timer(scope, identifier, wake_at, "resume")

    async def suspend(self, lease: ExecutionLease, wake_at: float) -> Execution:
        return await self.repository.suspend(lease, wake_at)

    async def retry(self, lease: ExecutionLease, *, safe: bool) -> Execution:
        return await self.repository.retry(lease, self.policy, safe)

    async def run_due(self) -> int:
        return await self.repository.tick()


class RecoveryService:
    def __init__(
        self, repository: ControlRepository, safe: SafeReplay, *, policy: RetryPolicy | None = None
    ) -> None:
        self.repository, self.safe, self.policy = repository, safe, policy or RetryPolicy()

    async def recover(self) -> int:
        # Called only at Host startup, while the existing RuntimeLock owns all stores.
        return await self.repository.recover(self.safe, self.policy)
