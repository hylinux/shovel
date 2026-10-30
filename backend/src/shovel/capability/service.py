from __future__ import annotations

import hashlib

from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityKind,
    CapabilityScope,
    CapabilityVersion,
    ValidationResult,
)
from shovel.capability.ports import PublishAuthorizer
from shovel.capability.registry import CapabilityRegistry
from shovel.runtime.errors import RuntimeFailure


class CapabilityService:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        authorizer: PublishAuthorizer | None = None,
    ) -> None:
        self.registry = registry
        self.repository = registry.repository
        self.authorizer = authorizer

    async def create_draft(
        self,
        scope: CapabilityScope,
        kind: CapabilityKind,
        name: str,
        content: CapabilityContent,
        *,
        description: str = "",
    ) -> CapabilityVersion:
        if not name.strip() or len(name) > 200 or len(description) > 2000:
            raise RuntimeFailure("Capability name or description is invalid.")
        if content.implementation.kind != kind.value:
            raise RuntimeFailure("Implementation kind does not match the definition.")
        return await self.repository.create(scope, kind, name.strip(), description, content)

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
    ) -> CapabilityVersion:
        if name is not None and (not name.strip() or len(name) > 200):
            raise RuntimeFailure("Capability name is invalid.")
        if description is not None and len(description) > 2000:
            raise RuntimeFailure("Capability description is invalid.")
        return await self.repository.update_draft(
            scope,
            identifier,
            number,
            content,
            expected_revision=expected_revision,
            name=name.strip() if name is not None else None,
            description=description,
        )

    async def create_draft_from_version(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion:
        return await self.repository.fork(scope, identifier, number)

    async def validate_draft(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> ValidationResult:
        return await self.registry.validate_draft(scope, identifier, number)

    async def publish(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
        *,
        expected_revision: int,
    ) -> CapabilityVersion:
        if self.authorizer is None:
            raise RuntimeFailure("Explicit publish authorization is not configured.", status=403)
        version = await self.registry.get_version(scope, identifier, number)
        if version.published_at is not None:
            raise RuntimeFailure("Published versions are immutable; create a new draft.", status=409)
        if version.revision != expected_revision:
            raise RuntimeFailure("Capability draft has changed; reload before publishing.", status=409)
        result = await self.validate_draft(scope, identifier, number)
        if not result.valid:
            raise RuntimeFailure("Invalid capability draft: " + "; ".join(result.errors))
        digest = version.content.content_hash
        await self.authorizer.authorize(scope, "publish", identifier, number, digest)
        return await self.repository.publish(
            scope,
            identifier,
            number,
            expected_revision=expected_revision,
            content_hash=digest,
        )

    async def set_availability(
        self,
        scope: CapabilityScope,
        identifier: str,
        availability: Availability,
        *,
        number: int | None = None,
    ) -> None:
        if self.authorizer is None:
            raise RuntimeFailure("Explicit availability authorization is not configured.", status=403)
        await self.registry.get(scope, identifier)
        if number is not None:
            await self.registry.get_version(scope, identifier, number)
        digest = hashlib.sha256(availability.value.encode("ascii")).hexdigest()
        await self.authorizer.authorize(scope, "availability", identifier, number, digest)
        await self.repository.set_availability(scope, identifier, availability, number=number)
