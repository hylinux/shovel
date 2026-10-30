from collections.abc import Awaitable, Callable
from typing import Protocol

from shovel.automation.domain import (
    AutomationControl,
    NormalizedEvent,
    ProactiveConfiguration,
    ProactiveService,
)
from shovel.capability.domain import CapabilityScope

type EventHandler = Callable[[NormalizedEvent], Awaitable[None]]


class EventBus(Protocol):
    def subscribe(self, handler: EventHandler) -> None: ...
    async def publish(self, event: NormalizedEvent) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ProactiveRepository(Protocol):
    async def create(
        self,
        scope: CapabilityScope,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool,
        name: str = "",
        description: str = "",
    ) -> ProactiveService: ...
    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        paused: bool | None = None,
    ) -> ProactiveService: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> ProactiveService: ...
    async def list(self, scope: CapabilityScope | None = None) -> list[ProactiveService]: ...
    async def cursor(self, service: ProactiveService) -> float: ...
    async def checkpoint(self, service: ProactiveService, through: float) -> None: ...
    async def last_invoked(self, service: ProactiveService) -> float | None: ...
    async def control(self, scope: CapabilityScope) -> AutomationControl: ...
    async def set_paused(
        self, scope: CapabilityScope, *, paused: bool, expected_revision: int
    ) -> AutomationControl: ...
