from typing import Protocol

from pydantic import ValidationError

from shovel.capability.domain import CapabilityScope
from shovel.identity.domain import IdentityDraft, IdentityRecord
from shovel.runtime.errors import RuntimeFailure


class IdentityRepository(Protocol):
    async def create(self, scope: CapabilityScope, draft: IdentityDraft) -> IdentityRecord: ...
    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        draft: IdentityDraft,
        *,
        expected_revision: int,
    ) -> IdentityRecord: ...
    async def disable(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        expected_revision: int,
    ) -> IdentityRecord: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> IdentityRecord: ...
    async def list(self, scope: CapabilityScope) -> list[IdentityRecord]: ...


class IdentityService:
    def __init__(self, repository: IdentityRepository) -> None:
        self.repository = repository

    @staticmethod
    def parse_draft(value: object) -> IdentityDraft:
        try:
            return IdentityDraft.model_validate(value)
        except ValidationError:
            raise RuntimeFailure("Identity draft validation failed.", status=422) from None

    async def create(self, scope: CapabilityScope, draft: IdentityDraft) -> IdentityRecord:
        return await self.repository.create(scope, self.parse_draft(draft))

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        draft: IdentityDraft,
        *,
        expected_revision: int,
    ) -> IdentityRecord:
        return await self.repository.update(
            scope,
            identifier,
            self.parse_draft(draft),
            expected_revision=expected_revision,
        )

    async def disable(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        expected_revision: int,
    ) -> IdentityRecord:
        return await self.repository.disable(scope, identifier, expected_revision=expected_revision)

    async def get(self, scope: CapabilityScope, identifier: str) -> IdentityRecord:
        return await self.repository.get(scope, identifier)

    async def list(self, scope: CapabilityScope) -> list[IdentityRecord]:
        return await self.repository.list(scope)
