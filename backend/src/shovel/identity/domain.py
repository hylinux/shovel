from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from threading import Lock
from typing import Annotated, Literal, Never, SupportsIndex
from urllib.parse import urlsplit

from pydantic import AfterValidator, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier, NonEmptyText, Value

type TenantId = Annotated[
    str,
    StringConstraints(
        to_lower=True,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ),
]
type Revision = Annotated[int, Field(strict=True, ge=1)]
type Generation = Annotated[int, Field(strict=True, ge=0)]
type UnixSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def https_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("A credential-free HTTPS endpoint is required.") from None
    return value.rstrip("/")


type HttpsEndpoint = Annotated[NonEmptyText, AfterValidator(https_endpoint)]


class IdentityValue(Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class AuthenticationState(StrEnum):
    READY_SILENT = "ready_silent"
    INTERACTION_REQUIRED = "interaction_required"
    REAUTHENTICATION_REQUIRED = "reauthentication_required"
    CLAIMS_CHALLENGE_REQUIRED = "claims_challenge_required"
    AUTHORIZATION_REQUIRED = "authorization_required"
    DEVICE_OR_POLICY_BLOCKED = "device_or_policy_blocked"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    CONNECTION_MISCONFIGURED = "connection_misconfigured"
    UNAVAILABLE = "unavailable"


class AuthenticationMode(StrEnum):
    WAM = "wam"
    AZURE_CLI_DIAGNOSTIC = "azure_cli_diagnostic"
    DEVICE_CODE_DIAGNOSTIC = "device_code_diagnostic"


class BrokerType(StrEnum):
    UNAVAILABLE = "unavailable"
    PYTHON_WAM = "python_wam"
    NATIVE_WAM = "native_wam"


class IdentityProfile(IdentityValue):
    format_version: Literal[1] = 1
    id: Identifier
    display_name: NonEmptyText = Field(repr=False)
    identity_type: Literal["entra_delegated"] = "entra_delegated"
    account_type: Literal["work", "guest"] = "work"
    tenant_binding: TenantId
    authentication_mode: AuthenticationMode = AuthenticationMode.WAM
    broker_type: BrokerType = BrokerType.UNAVAILABLE
    account_selector: Identifier | None = None
    enabled: bool = Field(default=False, strict=True)
    status: AuthenticationState = AuthenticationState.UNAVAILABLE
    revision: Revision = 1
    created_at: UnixSeconds
    updated_at: UnixSeconds

    @model_validator(mode="after")
    def valid_lifecycle(self) -> IdentityProfile:
        if self.updated_at < self.created_at:
            raise ValueError("Profile update precedes creation.")
        if self.status == AuthenticationState.READY_SILENT and (
            not self.enabled or self.broker_type == BrokerType.UNAVAILABLE
        ):
            raise ValueError("An unavailable or disabled profile cannot be ready.")
        return self


class IdentityBinding(IdentityValue):
    format_version: Literal[1] = 1
    workspace_id: Identifier
    user_id: Identifier
    identity_profile_id: Identifier
    identity_revision: Revision
    principal_ref: Identifier
    tenant_id: TenantId


class IdentityDraft(IdentityValue):
    display_name: NonEmptyText = Field(repr=False)
    account_type: Literal["work", "guest"] = "work"
    tenant_binding: TenantId
    account_selector: Identifier | None = None


class IdentityRecord(IdentityValue):
    format_version: Literal[1] = 1
    scope: CapabilityScope
    profile: IdentityProfile

    @model_validator(mode="after")
    def unavailable_kernel(self) -> IdentityRecord:
        if self.profile.enabled or self.profile.broker_type != BrokerType.UNAVAILABLE:
            raise ValueError("Identity configuration does not install or enable a broker.")
        if self.profile.status != AuthenticationState.UNAVAILABLE:
            raise ValueError("Identity configuration cannot claim authentication readiness.")
        return self


class TokenRequest(IdentityValue):
    """Resolved metadata only; construction is not authorization to acquire a token."""

    format_version: Literal[1] = 1
    request_id: Identifier
    execution_id: Identifier
    connection_profile_id: Identifier
    connection_revision: Revision
    provider_id: Identifier
    provider_version: NonEmptyText
    manifest_digest: Digest
    identity: IdentityBinding
    authority: HttpsEndpoint
    audience: NonEmptyText
    allowed_scopes: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=64)
    claims_challenge_ref: Identifier | None = None
    correlation_id: Identifier
    interaction_mode: Literal["silent", "interactive"] = "silent"
    force_refresh: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def explicit_tenant(self) -> TokenRequest:
        if urlsplit(self.authority).path.strip("/") != self.identity.tenant_id:
            raise ValueError("Authority must bind the exact tenant.")
        if len(set(self.allowed_scopes)) != len(self.allowed_scopes):
            raise ValueError("Scopes must be unique.")
        return self

    def validate_ceiling(
        self, approved: TokenRequest, *, interactive_allowed: bool = False, refresh_allowed: bool = False
    ) -> None:
        request = TokenRequest.model_validate(self)
        approved = TokenRequest.model_validate(approved)
        current = request.model_dump(exclude={"interaction_mode", "force_refresh", "claims_challenge_ref"})
        expected = approved.model_dump(exclude={"interaction_mode", "force_refresh", "claims_challenge_ref"})
        if current != expected:
            raise ValueError("Token request differs from the approved binding.")
        if request.claims_challenge_ref != approved.claims_challenge_ref:
            raise ValueError("Claims challenge requires an approved transport binding.")
        if request.interaction_mode == "interactive" and not interactive_allowed:
            raise ValueError("Interactive authentication is not authorized.")
        if request.force_refresh and not refresh_allowed:
            raise ValueError("Force refresh is not authorized.")


class Ephemeral:
    __slots__ = ()

    def __repr__(self) -> str:
        return f"<{type(self).__name__}: redacted>"

    def __getstate__(self) -> None:
        raise TypeError("Ephemeral content cannot be serialized.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        raise TypeError("Ephemeral content cannot be serialized.")

    def __copy__(self) -> object:
        raise TypeError("Ephemeral content cannot be copied.")

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        raise TypeError("Ephemeral content cannot be copied.")


class AuthContext(Ephemeral):
    """One-use context for a trusted transport; not a Python process isolation boundary."""

    __slots__ = ("_credential", "_expires_at", "_lock", "_request")

    def __init__(self, request: TokenRequest, credential: SecretStr, *, expires_at: float) -> None:
        request = TokenRequest.model_validate(request)
        if not credential.get_secret_value() or not 0 < expires_at < float("inf"):
            raise ValueError("Authentication context is invalid.")
        self._request = request
        self._credential: SecretStr | None = credential
        self._expires_at = expires_at
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            self._credential = None

    def validate(self, request: TokenRequest, *, now: float) -> None:
        with self._lock:
            if (
                self._credential is None
                or not 0 <= now < self._expires_at
                or TokenRequest.model_validate(request) != self._request
            ):
                raise ValueError("Authentication context is unavailable or differently bound.")

    def consume[T](self, request: TokenRequest, *, now: float, transport: Callable[[SecretStr], T]) -> T:
        with self._lock:
            credential = self._credential
            self._credential = None
        try:
            if (
                credential is None
                or not 0 <= now < self._expires_at
                or TokenRequest.model_validate(request) != self._request
            ):
                raise ValueError("Authentication context is expired, consumed or differently bound.")
            return transport(credential)
        finally:
            self.close()


class AuthenticationResolution(IdentityValue):
    approved_request: TokenRequest
    profile: IdentityProfile
    deadline_at: UnixSeconds
    refresh_allowed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def exact_profile(self) -> AuthenticationResolution:
        binding = self.approved_request.identity
        if (
            binding.identity_profile_id != self.profile.id
            or binding.identity_revision != self.profile.revision
            or binding.tenant_id != self.profile.tenant_binding
        ):
            raise ValueError("Authentication resolution is not bound to this profile.")
        return self


class AuthenticationRequest(IdentityValue):
    format_version: Literal[1] = 1
    id: Identifier
    scope: CapabilityScope
    request: TokenRequest
    binding_digest: Digest
    reason: AuthenticationState
    state: Literal["pending", "cancelled", "expired"]
    requires_fresh_challenge: bool = Field(default=False, strict=True)
    created_at: UnixSeconds
    expires_at: UnixSeconds

    @model_validator(mode="after")
    def safe_wait(self) -> AuthenticationRequest:
        if (
            self.request.claims_challenge_ref is not None
            or self.reason == AuthenticationState.READY_SILENT
            or self.request.interaction_mode != "silent"
            or self.request.force_refresh
            or self.expires_at <= self.created_at
            or self.scope.workspace_id != self.request.identity.workspace_id
            or self.scope.user_id != self.request.identity.user_id
        ):
            raise ValueError("Authentication waits require a bounded, credential-free binding.")
        return self
