from typing import Protocol

from shovel.capability.domain import CapabilityScope
from shovel.identity.domain import AuthenticationRequest, AuthenticationState, TokenRequest
from shovel.runtime.errors import RuntimeFailure


class AuthenticationRequestRepository(Protocol):
    async def create(
        self,
        scope: CapabilityScope,
        request: TokenRequest,
        reason: AuthenticationState,
    ) -> AuthenticationRequest: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> AuthenticationRequest: ...
    async def list(self, scope: CapabilityScope) -> list[AuthenticationRequest]: ...
    async def cancel(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        execution_id: str,
    ) -> AuthenticationRequest: ...


class AuthenticationRequests:
    def __init__(self, repository: AuthenticationRequestRepository) -> None:
        self.repository = repository

    async def create(
        self,
        scope: CapabilityScope,
        request: TokenRequest,
        reason: AuthenticationState,
    ) -> AuthenticationRequest:
        if reason == AuthenticationState.READY_SILENT:
            raise RuntimeFailure("A ready identity does not require an authentication wait.")
        return await self.repository.create(scope, request, reason)

    async def get(self, scope: CapabilityScope, identifier: str) -> AuthenticationRequest:
        return await self.repository.get(scope, identifier)

    async def list(self, scope: CapabilityScope) -> list[AuthenticationRequest]:
        return await self.repository.list(scope)

    async def cancel(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        execution_id: str,
    ) -> AuthenticationRequest:
        return await self.repository.cancel(scope, identifier, execution_id=execution_id)
