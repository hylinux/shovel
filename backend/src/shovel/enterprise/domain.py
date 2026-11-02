import hashlib
import json
import re
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from shovel.capability.domain import (
    CapabilityScope,
    Digest,
    Identifier,
    LockedDependency,
    NonEmptyText,
    Value,
)
from shovel.domain.safe_data import ensure_no_credentials
from shovel.identity.domain import (
    AuthenticationMode,
    HttpsEndpoint,
    IdentityProfile,
    Revision,
    TenantId,
    TokenRequest,
    UnixSeconds,
)


class EnterpriseValue(Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class Cloud(StrEnum):
    AZURE_GLOBAL = "azure_global"
    AZURE_CHINA = "azure_china"


class ProviderSetting(EnterpriseValue):
    name: Identifier
    value: str | int | float | bool | None = Field(repr=False)

    @model_validator(mode="after")
    def non_secret_setting(self) -> ProviderSetting:
        ensure_no_credentials({self.name: self.value})
        if any(
            part in {"cookie", "cookies", "token", "secret", "password"}
            for part in re.split(r"[_.-]", self.name.casefold())
        ):
            raise ValueError("Provider configuration cannot contain credentials or cookies.")
        return self


class ProviderEnvironment(EnterpriseValue):
    cloud: Cloud
    environment: Identifier
    authority_origins: tuple[HttpsEndpoint, ...] = Field(min_length=1)
    endpoints: tuple[HttpsEndpoint, ...] = Field(min_length=1)
    audiences: tuple[NonEmptyText, ...] = Field(min_length=1)
    permitted_scopes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def origins_only(self) -> ProviderEnvironment:
        if any(urlsplit(origin).path not in {"", "/"} for origin in self.authority_origins):
            raise ValueError("Authority origins must not contain tenant or resource paths.")
        return self


class ProviderManifest(EnterpriseValue):
    format_version: Literal[1] = 1
    provider_id: Identifier
    provider_version: NonEmptyText
    supported_connection_types: tuple[Identifier, ...] = Field(min_length=1)
    environments: tuple[ProviderEnvironment, ...] = Field(min_length=1)
    authentication_modes: tuple[AuthenticationMode, ...] = (AuthenticationMode.WAM,)
    capability_definitions: tuple[LockedDependency, ...] = ()
    function_implementations: tuple[Identifier, ...] = ()
    classifications_emitted: tuple[Identifier, ...] = ()
    protection_metadata_supported: tuple[Identifier, ...] = ()
    permitted_settings: tuple[Identifier, ...] = ()
    availability_probe: Identifier
    health_check: Identifier
    minimum_runtime_version: NonEmptyText

    @model_validator(mode="after")
    def unique_bindings(self) -> ProviderManifest:
        keys = [(item.cloud, item.environment) for item in self.environments]
        if len(keys) != len(set(keys)):
            raise ValueError("Manifest environment bindings must be unique.")
        return self

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class EnterpriseConnectionProfile(EnterpriseValue):
    format_version: Literal[1] = 1
    id: Identifier
    display_name: NonEmptyText = Field(repr=False)
    provider_id: Identifier
    connection_type: Identifier
    environment: Identifier
    cloud: Cloud
    endpoint: HttpsEndpoint
    tenant_binding: TenantId
    resource_scope: tuple[NonEmptyText, ...] = Field(min_length=1)
    identity_profile_ref: Identifier
    protection_profile_ref: Identifier
    provider_settings: tuple[ProviderSetting, ...] = ()
    enabled: bool = Field(default=False, strict=True)
    status: Literal["draft", "validated", "disabled", "misconfigured", "unavailable"] = "draft"
    created_at: UnixSeconds
    updated_at: UnixSeconds
    revision: Revision = 1

    @model_validator(mode="after")
    def valid_profile(self) -> EnterpriseConnectionProfile:
        if self.updated_at < self.created_at:
            raise ValueError("Profile update precedes creation.")
        names = [setting.name for setting in self.provider_settings]
        if len(names) != len(set(names)):
            raise ValueError("Provider settings must be unique.")
        return self

    def validate_manifest(self, manifest: ProviderManifest, *, require_enabled: bool = True) -> None:
        profile = EnterpriseConnectionProfile.model_validate(self)
        manifest = ProviderManifest.model_validate(manifest)
        compatible = any(
            item.cloud == profile.cloud
            and item.environment == profile.environment
            and profile.endpoint in item.endpoints
            for item in manifest.environments
        )
        if (
            profile.provider_id != manifest.provider_id
            or profile.connection_type not in manifest.supported_connection_types
            or not compatible
            or not {item.name for item in profile.provider_settings}.issubset(manifest.permitted_settings)
        ):
            raise ValueError("Connection is incompatible with the installed manifest.")
        if require_enabled and (not profile.enabled or profile.status != "validated"):
            raise ValueError("Connection is not enabled and validated.")


class ConnectionBinding(EnterpriseValue):
    format_version: Literal[1] = 1
    connection_profile_id: Identifier
    connection_revision: Revision
    provider_id: Identifier
    provider_version: NonEmptyText
    manifest_digest: Digest
    identity_profile_id: Identifier
    tenant_id: TenantId
    endpoint: HttpsEndpoint


class ConnectionDraft(EnterpriseValue):
    """Editable metadata only; lifecycle, identity and policy approval remain server-owned."""

    display_name: NonEmptyText = Field(repr=False)
    provider_id: Identifier
    connection_type: Identifier
    environment: Identifier
    cloud: Cloud
    endpoint: HttpsEndpoint
    tenant_binding: TenantId
    resource_scope: tuple[NonEmptyText, ...] = Field(min_length=1)
    identity_profile_ref: Identifier
    protection_profile_ref: Identifier
    provider_settings: tuple[ProviderSetting, ...] = ()


class ConnectionRecord(EnterpriseValue):
    format_version: Literal[1] = 1
    scope: CapabilityScope
    profile: EnterpriseConnectionProfile
    manifest_version: NonEmptyText
    manifest_digest: Digest

    @model_validator(mode="after")
    def disabled_kernel(self) -> ConnectionRecord:
        if self.profile.enabled or self.profile.status not in {"draft", "disabled"}:
            raise ValueError("Connection management does not authorize enterprise execution.")
        return self


class ConnectionValidation(EnterpriseValue):
    identifier: Identifier
    revision: Revision
    manifest_digest: Digest
    configuration_valid: Literal[True] = True
    executable: Literal[False] = False
    pending_requirements: tuple[Literal["identity", "protection", "transport"], ...] = (
        "identity",
        "protection",
        "transport",
    )


class EnterpriseResourceRef(EnterpriseValue):
    format_version: Literal[1] = 1
    provider_id: Identifier
    connection_profile_id: Identifier
    resource_type: Identifier
    resource_id: NonEmptyText = Field(repr=False)
    parent_resource_id: NonEmptyText | None = Field(default=None, repr=False)
    tenant_id: TenantId
    source_version: NonEmptyText = Field(repr=False)
    etag: NonEmptyText | None = Field(default=None, repr=False)

    @property
    def version_key(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def validate_token_request(
    request: TokenRequest,
    profile: EnterpriseConnectionProfile,
    manifest: ProviderManifest,
    identity: IdentityProfile,
) -> None:
    request = TokenRequest.model_validate(request)
    profile = EnterpriseConnectionProfile.model_validate(profile)
    identity = IdentityProfile.model_validate(identity)
    manifest = ProviderManifest.model_validate(manifest)
    profile.validate_manifest(manifest)
    authority = urlsplit(request.authority)
    origin = f"{authority.scheme}://{authority.netloc}"
    compatible = any(
        environment.cloud == profile.cloud
        and environment.environment == profile.environment
        and profile.endpoint in environment.endpoints
        and origin in environment.authority_origins
        and request.audience in environment.audiences
        and set(request.allowed_scopes).issubset(environment.permitted_scopes)
        for environment in manifest.environments
    )
    if (
        not identity.enabled
        or request.connection_profile_id != profile.id
        or request.connection_revision != profile.revision
        or request.provider_id != manifest.provider_id
        or request.provider_version != manifest.provider_version
        or request.manifest_digest != manifest.content_hash
        or request.identity.identity_profile_id != identity.id
        or request.identity.identity_revision != identity.revision
        or profile.identity_profile_ref != identity.id
        or profile.tenant_binding != identity.tenant_binding
        or request.identity.tenant_id != profile.tenant_binding
        or identity.authentication_mode not in manifest.authentication_modes
        or not compatible
    ):
        raise ValueError("Token request does not match approved connection, identity and manifest metadata.")
