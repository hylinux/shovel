from collections.abc import Callable
from typing import Protocol

from pydantic import JsonValue, model_validator

from shovel.capability.domain import Identifier
from shovel.enterprise.domain import ConnectionBinding, EnterpriseResourceRef, EnterpriseValue
from shovel.identity.domain import AuthContext, TokenRequest
from shovel.protection.domain import ProtectedContent


class ProviderRequest(EnterpriseValue):
    operation_id: Identifier
    connection: ConnectionBinding
    authentication: TokenRequest
    resource: EnterpriseResourceRef
    parameter_refs: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def exact_target(self) -> ProviderRequest:
        request, connection, resource = self.authentication, self.connection, self.resource
        if (
            request.connection_profile_id != connection.connection_profile_id
            or request.connection_revision != connection.connection_revision
            or request.provider_id != connection.provider_id
            or request.provider_version != connection.provider_version
            or request.manifest_digest != connection.manifest_digest
            or request.identity.identity_profile_id != connection.identity_profile_id
            or request.identity.tenant_id != connection.tenant_id
            or resource.connection_profile_id != connection.connection_profile_id
            or resource.provider_id != connection.provider_id
            or resource.tenant_id != connection.tenant_id
        ):
            raise ValueError("Provider operation must bind the exact resolved authentication and source.")
        return self


class AuthenticatedTransport(Protocol):
    """Trusted transport supplies response bytes only to the trusted protection-aware Provider decoder."""

    async def send(
        self,
        request: ProviderRequest,
        authentication: AuthContext,
        *,
        decode: Callable[[bytes], ProtectedContent[JsonValue]],
    ) -> ProtectedContent[JsonValue]: ...
