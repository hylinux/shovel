from typing import Literal, Protocol

from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityDefinition,
    CapabilityKind,
    CapabilityScope,
    CapabilityVersion,
)


class PublishAuthorizer(Protocol):
    async def authorize(
        self,
        scope: CapabilityScope,
        action: Literal["publish", "availability"],
        capability_id: str,
        version: int | None,
        content_hash: str,
    ) -> None:
        """Raise unless a trusted caller explicitly authorized this exact action."""
        ...


class CapabilityRepository(Protocol):
    async def latest_published(self, scope: CapabilityScope, identifier: str) -> CapabilityVersion: ...
    async def create(
        self,
        scope: CapabilityScope,
        kind: CapabilityKind,
        name: str,
        description: str,
        content: CapabilityContent,
    ) -> CapabilityVersion: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> CapabilityDefinition: ...
    async def get_version(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion: ...
    async def list(
        self,
        scope: CapabilityScope,
        *,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityDefinition]: ...
    async def update_draft(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
        content: CapabilityContent,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
    ) -> CapabilityVersion: ...
    async def fork(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion: ...
    async def dependency_errors(
        self,
        scope: CapabilityScope,
        content: CapabilityContent,
    ) -> tuple[str, ...]: ...
    async def publish(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
        *,
        expected_revision: int,
        content_hash: str,
    ) -> CapabilityVersion: ...
    async def set_availability(
        self,
        scope: CapabilityScope,
        identifier: str,
        availability: Availability,
        *,
        number: int | None = None,
    ) -> None: ...
