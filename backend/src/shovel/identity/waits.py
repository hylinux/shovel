from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from shovel.capability.domain import CapabilityScope, Digest
from shovel.identity.domain import AuthContext, AuthenticationRequest, AuthenticationState, TokenRequest
from shovel.runtime.errors import RuntimeFailure

if TYPE_CHECKING:
    from shovel.execution.domain import Execution, ExecutionLease

authentication_execution_lease: ContextVar[ExecutionLease | None] = ContextVar(
    "authentication_execution_lease", default=None
)


class AuthenticationWaitRequired(Exception):
    def __init__(self, request: TokenRequest, reason: AuthenticationState, operation_digest: str) -> None:
        self.request = TokenRequest.model_validate(request)
        self.reason = AuthenticationState(reason)
        self.operation_digest = operation_digest
        super().__init__("Execution requires enterprise authentication.")


@dataclass(frozen=True)
class AuthenticationExecutionContext:
    bind_operation: Callable[[str], Awaitable[None]]
    authenticated: Callable[[TokenRequest, AuthContext, str], Awaitable[None]]
    prepare_output: Callable[[], Awaitable[None]]


class AuthenticationExecutionLifecycle(Protocol):
    async def bind_operation(self, lease: ExecutionLease, operation_digest: Digest) -> None: ...
    async def suspend(self, lease: ExecutionLease, wait: AuthenticationWaitRequired) -> Execution: ...
    async def authenticated(
        self, lease: ExecutionLease, request: TokenRequest, context: AuthContext, operation_digest: Digest
    ) -> None: ...
    async def prepare_output(self, lease: ExecutionLease) -> None: ...


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
