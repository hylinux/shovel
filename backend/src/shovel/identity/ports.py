from typing import Protocol

from shovel.identity.domain import (
    AuthContext,
    AuthenticationResolution,
    AuthenticationState,
    IdentityProfile,
    TokenRequest,
)


class WindowsAuthenticationBroker(Protocol):
    async def availability(self, profile: IdentityProfile) -> AuthenticationState: ...
    async def acquire(self, request: TokenRequest) -> AuthContext: ...
    async def cancel(self, request_id: str) -> None: ...
    async def disconnect(self, profile: IdentityProfile) -> None: ...


class EnterpriseAuthService(Protocol):
    async def authenticate(self, request: TokenRequest) -> AuthContext: ...


class AuthenticationResolver(Protocol):
    """Trusted resolver checks caller authority, exact manifest/connection/account and current deadline."""

    async def resolve(self, request: TokenRequest) -> AuthenticationResolution: ...
