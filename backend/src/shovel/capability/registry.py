from __future__ import annotations

import builtins
from collections.abc import Sequence

from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityDefinition,
    CapabilityScope,
    CapabilityVersion,
    FunctionContract,
    FunctionImplementation,
    ValidationResult,
    WorkflowImplementation,
)
from shovel.capability.ports import CapabilityRepository
from shovel.capability.validation import content_errors
from shovel.runtime.errors import RuntimeFailure


class CapabilityRegistry:
    def retry_safe(self, content: CapabilityContent) -> bool:
        if isinstance(content.implementation, WorkflowImplementation):
            return True
        if content.side_effect_profile.kind in {"external_write", "destructive"}:
            return False
        if content.side_effect_profile.kind == "none":
            return True
        if isinstance(content.implementation, FunctionImplementation):
            contract = self._functions.get(content.implementation.implementation_id)
            return contract is not None and contract.retry_safe
        return False

    def __init__(
        self,
        repository: CapabilityRepository,
        *,
        functions: Sequence[FunctionContract] = (),
    ) -> None:
        self.repository = repository
        self._functions = {item.implementation_id: item.model_copy(deep=True) for item in functions}
        if len(self._functions) != len(functions):
            raise ValueError("Duplicate trusted function implementation.")

    async def get(self, scope: CapabilityScope, identifier: str) -> CapabilityDefinition:
        return await self.repository.get(scope, identifier)

    async def resolve_latest(self, scope: CapabilityScope, identifier: str) -> CapabilityVersion:
        version = await self.repository.latest_published(scope, identifier)
        return await self.resolve_exact(scope, identifier, version.number)

    async def get_version(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion:
        return await self.repository.get_version(scope, identifier, number)

    async def list(
        self,
        scope: CapabilityScope,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityDefinition]:
        return await self.repository.list(scope, limit=limit, offset=offset)

    async def search(
        self,
        scope: CapabilityScope,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[CapabilityDefinition]:
        if not query.strip() or len(query) > 200:
            raise RuntimeFailure("Search requires 1-200 non-blank characters.")
        return await self.repository.list(scope, query=query.strip(), limit=limit, offset=offset)

    async def validate_draft(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> ValidationResult:
        definition = await self.get(scope, identifier)
        version = await self.get_version(scope, identifier, number)
        errors = content_errors(definition.kind, version.content, self._functions)
        errors += await self.repository.dependency_errors(scope, version.content)
        if any(item.capability_id == identifier for item in version.content.dependency_lock.dependencies):
            errors += ("Capability cycles are not allowed.",)
        return ValidationResult(errors=errors)

    async def resolve_exact(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion:
        definition = await self.get(scope, identifier)
        version = await self.get_version(scope, identifier, number)
        unavailable = {Availability.DISABLED, Availability.ARCHIVED}
        if definition.availability in unavailable or version.availability in unavailable:
            raise RuntimeFailure("Capability version is unavailable.", status=409)
        if version.published_at is None:
            raise RuntimeFailure("Only published exact versions can be resolved for execution.", status=409)
        if version.content_hash != version.content.content_hash:
            raise RuntimeFailure("Published capability content hash does not match.", status=409)
        errors = content_errors(definition.kind, version.content, self._functions)
        errors += await self.repository.dependency_errors(scope, version.content)
        if errors:
            raise RuntimeFailure("Capability cannot be resolved: " + "; ".join(errors), status=409)
        return version
