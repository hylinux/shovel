from typing import Protocol

from shovel.capability.domain import CapabilityScope
from shovel.enterprise.domain import ConnectionDraft, ConnectionRecord, ProviderManifest


class ConnectionRepository(Protocol):
    async def create(
        self, scope: CapabilityScope, draft: ConnectionDraft, manifest: ProviderManifest
    ) -> ConnectionRecord: ...

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        draft: ConnectionDraft,
        manifest: ProviderManifest,
        *,
        expected_revision: int,
    ) -> ConnectionRecord: ...

    async def disable(
        self, scope: CapabilityScope, identifier: str, *, expected_revision: int
    ) -> ConnectionRecord: ...

    async def get(self, scope: CapabilityScope, identifier: str) -> ConnectionRecord: ...
    async def list(self, scope: CapabilityScope) -> list[ConnectionRecord]: ...
