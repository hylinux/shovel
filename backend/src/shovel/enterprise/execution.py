from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue, model_validator

from shovel.capability.domain import FunctionImplementation, Permission
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.enterprise.domain import (
    EnterpriseConnectionProfile,
    EnterpriseValue,
    ProviderManifest,
    validate_token_request,
)
from shovel.enterprise.transport import AuthenticatedTransport, ProviderRequest
from shovel.execution.domain import Execution, ExecutorResult
from shovel.execution.ports import ExecutorContext
from shovel.identity.domain import AuthenticationState, IdentityProfile
from shovel.identity.errors import AuthenticationDeadlineError, AuthenticationError
from shovel.identity.service import AuthenticationService
from shovel.identity.waits import AuthenticationWaitRequired
from shovel.protection.domain import (
    DataAction,
    DataDestination,
    DataUseBinding,
    DependencyKind,
    ProtectedContent,
    ProtectionAdapterError,
    ProtectionDeniedError,
    SourceBinding,
    SourceContentRef,
    content_digest,
)
from shovel.protection.egress import record_displayed_receipt
from shovel.security.domain import SecurityDenied
from shovel.security.ports import ExecutionGate


class EnterpriseReadPlan(EnterpriseValue):
    request: ProviderRequest
    profile: EnterpriseConnectionProfile
    manifest: ProviderManifest
    identity: IdentityProfile
    source: SourceBinding

    @model_validator(mode="after")
    def exact_bindings(self) -> EnterpriseReadPlan:
        self.profile.validate_manifest(self.manifest)
        validate_token_request(self.request.authentication, self.profile, self.manifest, self.identity)
        connection = self.request.connection
        if (
            self.request.resource != self.source.resource
            or self.source.identity_profile_id != self.identity.id
            or self.source.principal_ref != self.request.authentication.identity.principal_ref
            or connection.connection_profile_id != self.profile.id
            or connection.connection_revision != self.profile.revision
            or connection.endpoint != self.profile.endpoint
            or connection.manifest_digest != self.manifest.content_hash
        ):
            raise ValueError("Enterprise read plan requires exact trusted profile and source bindings.")
        return self

    @property
    def digest(self) -> str:
        request = self.request.model_dump(mode="json")
        authentication = self.request.authentication.model_dump(
            mode="json", exclude={"request_id", "claims_challenge_ref"}
        )
        request["authentication"] = authentication
        return content_digest({"request": request, "source": self.source.model_dump(mode="json")})

    def validate_execution(self, execution: Execution) -> None:
        content = execution.snapshot.capability.content
        implementation = content.implementation
        permission = Permission(action="enterprise.read", resource=self.profile.id)
        request = self.request.authentication
        if (
            not isinstance(implementation, FunctionImplementation)
            or implementation.implementation_id not in self.manifest.function_implementations
            or self.request.operation_id != implementation.implementation_id
            or content.side_effect_profile.kind != "none"
            or len(content.connection_requirements) != 1
            or content.connection_requirements[0].connection_ref != self.profile.id
            or permission not in content.required_permissions
            or permission not in execution.snapshot.granted_permissions
            or request.execution_id != execution.id
            or request.correlation_id != execution.correlation_id
            or request.identity.workspace_id != execution.snapshot.context.scope.workspace_id
            or request.identity.user_id != execution.snapshot.context.scope.user_id
            or request.interaction_mode != "silent"
            or request.force_refresh
        ):
            raise SecurityDenied("Enterprise read is not bound to this frozen, read-only execution.")


class EnterpriseConnectionResolver(Protocol):
    async def resolve(self, execution: Execution) -> EnterpriseReadPlan: ...


class EnterpriseDecoder(Protocol):
    def decode(self, plan: EnterpriseReadPlan, response: bytes) -> ProtectedContent[JsonValue]: ...


class ProtectedDisplay(Protocol):
    def display(self, execution_id: str, value: JsonValue) -> None: ...


class EnterpriseReadHandler:
    """Trusted, read-only Function handler. No registration, provider or display is installed by default."""

    def __init__(
        self,
        resolver: EnterpriseConnectionResolver,
        authentication: AuthenticationService,
        transport: AuthenticatedTransport,
        decoder: EnterpriseDecoder,
        protection: SQLiteProtectionRepository,
        display: ProtectedDisplay,
        gate: ExecutionGate,
        *,
        max_response_bytes: int = 262144,
    ) -> None:
        if not 1 <= max_response_bytes <= 262144:
            raise ValueError("Enterprise response limit must be within 1..262144 bytes.")
        self.resolver, self.authentication, self.transport, self.decoder = (
            resolver,
            authentication,
            transport,
            decoder,
        )
        self.protection, self.display, self.gate = protection, display, gate
        self.max_response_bytes = max_response_bytes

    async def _current(
        self, execution: Execution, expected: EnterpriseReadPlan | None = None
    ) -> EnterpriseReadPlan:
        plan = EnterpriseReadPlan.model_validate(await self.resolver.resolve(execution))
        plan.validate_execution(execution)
        if expected is not None and plan.digest != expected.digest:
            raise SecurityDenied("Enterprise bindings changed during execution.")
        await self.protection.check_sources(execution.snapshot.context.scope, (plan.source,))
        return plan

    @staticmethod
    def _binding(
        execution: Execution,
        plan: EnterpriseReadPlan,
        content: ProtectedContent[JsonValue],
        action: DataAction,
    ) -> DataUseBinding:
        metadata = content.metadata
        return DataUseBinding(
            execution_id=execution.id,
            step_id=f"enterprise.{action.value}",
            actor_id=execution.snapshot.context.actor_id,
            identities=(plan.request.authentication.identity,),
            connections=(plan.request.connection,),
            sources=metadata.protection_context.sources,
            protection_context_ref=metadata.protection_context.id,
            protection_context_digest=metadata.protection_context.content_hash,
            content_digest=metadata.reference.content_digest,
            action=action,
            destination=DataDestination.USER_SCREEN
            if action == DataAction.DISPLAY
            else DataDestination.WORKFLOW_STATE,
            policy_version=metadata.protection_context.policy_version or "0" * 64,
        )

    async def __call__(self, context: ExecutorContext) -> ExecutorResult:
        if context.authentication is None or context.lease is None:
            raise SecurityDenied("Enterprise execution lifecycle is unavailable.")
        execution = context.execution
        plan = await self._current(execution)
        await context.authentication.bind_operation(plan.digest)
        try:
            credential = await self.authentication.authenticate(plan.request.authentication)
        except AuthenticationDeadlineError:
            raise
        except AuthenticationError as error:
            if error.state in {
                AuthenticationState.INTERACTION_REQUIRED,
                AuthenticationState.REAUTHENTICATION_REQUIRED,
                AuthenticationState.CLAIMS_CHALLENGE_REQUIRED,
                AuthenticationState.ACCOUNT_UNAVAILABLE,
                AuthenticationState.UNAVAILABLE,
                AuthenticationState.AUTHORIZATION_REQUIRED,
                AuthenticationState.DEVICE_OR_POLICY_BLOCKED,
            }:
                raise AuthenticationWaitRequired(
                    plan.request.authentication, error.state, plan.digest
                ) from None
            raise
        try:
            await self._current(execution, plan)
            await context.authentication.authenticated(plan.request.authentication, credential, plan.digest)
            await self._current(execution, plan)
            credential.validate(plan.request.authentication, now=self.authentication.clock())
            await self.protection.check_execution(context.lease, self.gate)
            decoded: ProtectedContent[JsonValue] | None = None

            def decode(response: bytes) -> ProtectedContent[JsonValue]:
                nonlocal decoded
                if (
                    decoded is not None
                    or not isinstance(response, bytes)
                    or len(response) > self.max_response_bytes
                ):
                    raise ProtectionDeniedError("Enterprise response is unsupported, repeated or too large.")
                value = self.decoder.decode(plan, response)
                if not isinstance(value, ProtectedContent):
                    raise ProtectionAdapterError("Enterprise decoder did not return protected content.")
                metadata = value.metadata
                if (
                    not isinstance(metadata.reference.reference, SourceContentRef)
                    or metadata.reference.reference.resource != plan.source.resource
                    or metadata.protection_context.derived
                    or set(metadata.protection_context.sources) != {plan.source}
                    or not any(
                        edge.source == plan.source and edge.kind == DependencyKind.DATA
                        for edge in metadata.provenance.edges
                    )
                ):
                    raise ProtectionDeniedError("Enterprise response differs from the frozen source.")
                decoded = value
                return value

            content = await self.transport.send(plan.request, credential, decode=decode)
            if decoded is None or content is not decoded:
                raise ProtectionAdapterError("Enterprise transport bypassed its protected decoder.")
            await self._current(execution, plan)
            read = self._binding(execution, plan, content, DataAction.READ)
            display_content = await self.protection.use_bound(
                context.lease,
                content,
                read,
                lambda value: ProtectedContent(value, content.metadata),
                gate=self.gate,
            )
            await self._current(execution, plan)
            await context.authentication.prepare_output()
            await self._current(execution, plan)
            display = self._binding(execution, plan, display_content, DataAction.DISPLAY)
            await self.protection.use_bound(
                context.lease,
                display_content,
                display,
                lambda value: self.display.display(execution.id, value),
                gate=self.gate,
            )
            record_displayed_receipt()
            return ExecutorResult(output={"status": "displayed"})
        finally:
            credential.close()
