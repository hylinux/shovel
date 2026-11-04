"""Organization-signed, metadata-only authority; never loaded from personal settings."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from pydantic import Field, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier, NonEmptyText
from shovel.enterprise.domain import (
    Cloud,
    EnterpriseConnectionProfile,
    EnterpriseValue,
    ProviderEnvironment,
    ProviderManifest,
    validate_token_request,
)
from shovel.identity.domain import IdentityProfile, Revision, TenantId, TokenRequest, UnixSeconds
from shovel.protection.domain import ContentRight, ProtectionDeniedError

PROVIDER_ID = "kusto.metadata"
OPERATION_ID = "kusto.metadata.read"


def unique_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("Duplicate JSON fields.")
        return result

    return json.loads(data, object_pairs_hook=pairs)


class OrganizationAnchor(EnterpriseValue):
    organization_id: Identifier
    revision: Revision
    certificate_sha256: Digest
    envelope_sha256: Digest


class MetadataLabel(EnterpriseValue):
    id: Identifier
    classification_id: Identifier
    rank: int = Field(strict=True, ge=0)
    protection_status: Literal["unprotected"]
    encrypted: Literal[False]
    dlp_applicable: Literal[False]
    retention_policy_ref: Literal["ephemeral_metadata_only"]
    rights: tuple[Literal[ContentRight.READ, ContentRight.DISPLAY], ...] = ()
    restrictions: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def supported_metadata_rights(self) -> MetadataLabel:
        if len(set(self.rights)) != len(self.rights) or not set(self.restrictions).issubset(
            {"metadata_only", "no_model", "no_persist", "no_export"}
        ):
            raise ValueError("Metadata rights or restrictions are unsupported.")
        return self


class MetadataConnection(EnterpriseValue):
    profile: EnterpriseConnectionProfile
    identity: IdentityProfile
    authentication: TokenRequest
    database: NonEmptyText = Field(max_length=128, repr=False)
    table: NonEmptyText = Field(max_length=128, repr=False)
    label_id: Identifier
    active: bool = Field(default=False, strict=True)


class OrganizationPolicy(EnterpriseValue):
    purpose: Literal["shovel.organization.metadata.v1"]
    organization_id: Identifier
    scope: CapabilityScope
    tenant_id: TenantId
    revision: Revision
    issued_at: UnixSeconds
    expires_at: UnixSeconds
    revoked: bool = Field(default=False, strict=True)
    metadata_max_age_seconds: int = Field(default=5, strict=True, ge=1, le=30)
    labels: tuple[MetadataLabel, ...] = ()
    connections: tuple[MetadataConnection, ...] = ()

    @property
    def manifest(self) -> ProviderManifest:
        environments = []
        for cloud in Cloud:
            items = [item for item in self.connections if item.profile.cloud == cloud]
            if not items:
                continue
            environments.append(
                ProviderEnvironment(
                    cloud=cloud,
                    environment="organization",
                    endpoints=tuple(dict.fromkeys(item.profile.endpoint for item in items)),
                    authority_origins=tuple(
                        dict.fromkeys(
                            f"https://{urlsplit(item.authentication.authority).netloc}" for item in items
                        )
                    ),
                    audiences=tuple(dict.fromkeys(item.authentication.audience for item in items)),
                    permitted_scopes=tuple(
                        dict.fromkeys(scope for item in items for scope in item.authentication.allowed_scopes)
                    ),
                )
            )
        return ProviderManifest(
            provider_id=PROVIDER_ID,
            provider_version="1.0.0",
            supported_connection_types=("table_metadata",),
            environments=tuple(environments),
            function_implementations=(OPERATION_ID,),
            classifications_emitted=tuple(dict.fromkeys(label.classification_id for label in self.labels)),
            protection_metadata_supported=("organization_label", "read_display_rights", "schema_version"),
            availability_probe="kusto.metadata.schema",
            health_check="kusto.metadata.schema",
            minimum_runtime_version="0.1.2",
        )

    @model_validator(mode="after")
    def exact_scope(self) -> OrganizationPolicy:
        if self.expires_at <= self.issued_at or len({label.id for label in self.labels}) != len(self.labels):
            raise ValueError("Organization policy validity or labels are inconsistent.")
        ids = [item.profile.id for item in self.connections]
        if len(ids) != len(set(ids)) or len(self.connections) > 128:
            raise ValueError("Organization connection IDs must be unique and bounded.")
        labels = {item.id for item in self.labels}
        for item in self.connections:
            profile, request = item.profile, item.authentication
            parsed = urlsplit(profile.endpoint)
            suffix, authority = (
                (".kusto.chinacloudapi.cn", "login.chinacloudapi.cn")
                if profile.cloud == Cloud.AZURE_CHINA
                else (".kusto.windows.net", "login.microsoftonline.com")
            )
            if (
                parsed.hostname is None
                or not parsed.hostname.endswith(suffix)
                or parsed.port not in (None, 443)
                or parsed.path not in ("", "/")
                or urlsplit(request.authority).hostname != authority
                or profile.environment != "organization"
                or profile.connection_type != "table_metadata"
                or profile.provider_settings
                or profile.resource_scope != (item.database, item.table)
                or profile.tenant_binding != self.tenant_id
                or (request.identity.workspace_id, request.identity.user_id)
                != (self.scope.workspace_id, self.scope.user_id)
                or item.label_id not in labels
                or profile.protection_profile_ref != item.label_id
                or request.interaction_mode != "silent"
                or request.force_refresh
                or request.claims_challenge_ref is not None
                or any(ord(char) < 32 for char in item.database + item.table)
            ):
                raise ValueError("Organization metadata binding is unsupported or inconsistently scoped.")
        # Manifest digests are validated when resolving, after the signed catalog is constructed.
        return self

    def validate_connection(self, item: MetadataConnection) -> None:
        if self.revoked or not item.active or item not in self.connections:
            raise ProtectionDeniedError("Organization connection is not active.")
        validate_token_request(item.authentication, item.profile, self.manifest, item.identity)


@dataclass(frozen=True)
class VerifiedOrganizationPolicy:
    policy: OrganizationPolicy
    digest: str


def verify_signed_document(
    envelope: bytes, anchor: OrganizationAnchor, *, now: float
) -> tuple[object, str, float]:
    """Verify pinned organization bytes without changing either signed document format."""
    try:
        if len(envelope) > 1024 * 1024 or hashlib.sha256(envelope).hexdigest() != anchor.envelope_sha256:
            raise ValueError("Organization envelope pin mismatch.")
        root = unique_json(envelope)
        if not isinstance(root, dict) or set(root) != {"payload", "signature", "certificate"}:
            raise ValueError("Invalid organization envelope.")
        payload, signature, der = (
            base64.b64decode(root[name], validate=True) for name in ("payload", "signature", "certificate")
        )
        certificate = x509.load_der_x509_certificate(der)
        key = certificate.public_key()
        try:
            usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            usage = None
        instant = datetime.fromtimestamp(now, UTC)
        if (
            certificate.fingerprint(hashes.SHA256()).hex() != anchor.certificate_sha256
            or not certificate.not_valid_before_utc <= instant < certificate.not_valid_after_utc
            or ExtendedKeyUsageOID.CODE_SIGNING
            not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            or not isinstance(key, rsa.RSAPublicKey)
            or key.key_size < 2048
            or (usage is not None and not usage.digital_signature)
        ):
            raise ValueError("Organization signing authority rejected.")
        key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
        return (
            unique_json(payload),
            hashlib.sha256(payload).hexdigest(),
            certificate.not_valid_after_utc.timestamp(),
        )
    except Exception:
        raise ProtectionDeniedError("Organization document signature is unavailable.") from None


def verify_policy(envelope: bytes, anchor: OrganizationAnchor, *, now: float) -> VerifiedOrganizationPolicy:
    try:
        document, digest, certificate_expiry = verify_signed_document(envelope, anchor, now=now)
        policy = OrganizationPolicy.model_validate(document)
        if (
            policy.organization_id != anchor.organization_id
            or policy.revision != anchor.revision
            or not policy.issued_at <= now < policy.expires_at
            or policy.expires_at > certificate_expiry
        ):
            raise ValueError("Organization policy scope or validity rejected.")
        if not policy.revoked:
            for connection in policy.connections:
                if connection.active:
                    policy.validate_connection(connection)
        return VerifiedOrganizationPolicy(policy, digest)
    except Exception:
        raise ProtectionDeniedError(
            "Organization policy signature, scope or freshness is unavailable."
        ) from None


type OrganizationLoader = Callable[[], tuple[OrganizationAnchor, bytes] | None]
_installed_loader: OrganizationLoader | None = None


def configure_organization_host(loader: OrganizationLoader) -> None:
    import sys

    from shovel.identity.native_wam import installed_host

    global _installed_loader
    if (
        _installed_loader is not None
        or not installed_host()
        or not sys.flags.isolated
        or not sys.flags.no_site
    ):
        raise RuntimeError("Protected installed bootstrap is required for organization trust.")
    _installed_loader = loader


def installed_organization_loader() -> OrganizationLoader | None:
    return _installed_loader


def load_verified(loader: OrganizationLoader) -> VerifiedOrganizationPolicy | None:
    item = loader()
    return None if item is None else verify_policy(item[1], item[0], now=time.time())
