"""Synthetic sample/test authorities; never imported by the production Runtime.

The encrypted/cleanup evidence below is deliberately fictitious, not OS/KMS attestation.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from shovel.capability.domain import CapabilityScope
from shovel.enterprise.organization import OrganizationAnchor
from shovel.protection.domain import ProtectionDeniedError, content_digest
from shovel.protection.governance import (
    DATA_CLASSES,
    GovernancePolicy,
    GovernanceService,
    RetentionRule,
    StorageEvidence,
    StorageRequest,
)


@lru_cache(maxsize=1)
def signing_key() -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SYNTHETIC H4 ONLY")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(datetime.fromtimestamp(0, UTC))
        .not_valid_after(datetime.fromtimestamp(100000, UTC))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate.public_bytes(serialization.Encoding.DER)


def sign_governance(
    policy: GovernancePolicy, *, payload: bytes | None = None
) -> tuple[OrganizationAnchor, bytes]:
    key, der = signing_key()
    payload = payload if payload is not None else policy.model_dump_json().encode()
    envelope = json.dumps(
        {
            "payload": base64.b64encode(payload).decode(),
            "signature": base64.b64encode(key.sign(payload, padding.PKCS1v15(), hashes.SHA256())).decode(),
            "certificate": base64.b64encode(der).decode(),
        }
    ).encode()
    return (
        OrganizationAnchor(
            organization_id=policy.organization_id,
            revision=policy.revision,
            certificate_sha256=hashlib.sha256(der).hexdigest(),
            envelope_sha256=hashlib.sha256(envelope).hexdigest(),
        ),
        envelope,
    )


class SyntheticStorageAuthority:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.retired: set[tuple[CapabilityScope, str, str]] = set()
        self.retired_sources: set[tuple[CapabilityScope, str, str]] = set()
        self.minimum_revision = 1
        self.calls: list[StorageRequest] = []
        self.backups: set[tuple[CapabilityScope, str, str]] = set()
        self.unavailable = False
        self.alter: Callable[[StorageEvidence], StorageEvidence] = lambda evidence: evidence

    def authorize(self, request: StorageRequest) -> StorageEvidence:
        self.calls.append(request)
        key = (request.scope, request.store_binding, request.record_digest)
        sources = {(request.scope, request.store_binding, source) for source in request.source_keys}
        if (
            self.unavailable
            or request.policy_revision < self.minimum_revision
            or key in self.retired
            or sources & self.retired_sources
            or (request.operation == "restore" and key not in self.backups)
        ):
            raise ProtectionDeniedError("SYNTHETIC storage/key authority denies this request.")
        if request.operation == "backup":
            self.backups.add(key)
        return self.evidence(request, "active")

    def retire(self, request: StorageRequest) -> StorageEvidence:
        self.calls.append(request)
        if self.unavailable:
            raise RuntimeError("PRIVATE-KMS-ERROR")
        self.retired.add((request.scope, request.store_binding, request.record_digest))
        if request.retirement_scope == "source":
            self.retired_sources.update(
                (request.scope, request.store_binding, key) for key in request.source_keys
            )
        return self.evidence(request, "retired")

    def evidence(self, request: StorageRequest, state: Literal["active", "retired"]) -> StorageEvidence:
        return self.alter(
            StorageEvidence(
                request_digest=request.digest,
                storage_id=request.storage_id,
                key_authority_id=request.key_authority_id,
                store_binding=request.store_binding,
                generation=0,
                observed_at=self.clock(),
                expires_at=self.clock() + 5,
                state=state,
                encrypted_surfaces=("sqlite", "wal", "shm", "temp", "vector", "artifact", "cache", "backup"),
                erasure_capabilities=("logical", "file_sanitization"),
                non_exportable_keys=True,
                consumer_bound=True,
                rollback_protected=True,
                control_recovery_safe=True,
                retention_enforced=True,
                backup_inventory_complete=True,
                evidence_digest=content_digest({"synthetic": request.digest, "state": state}),
            )
        )


class SyntheticGovernance(GovernanceService):
    def __init__(
        self, policy: GovernancePolicy, authority: SyntheticStorageAuthority, clock: Callable[[], float]
    ) -> None:
        self.fixture_policy = policy
        self.fixture_holder: list[tuple[OrganizationAnchor, bytes] | None] = [sign_governance(policy)]
        super().__init__(lambda: self.fixture_holder[0], authority, clock=clock)


def synthetic_governance(
    scope: CapabilityScope,
    clock: Callable[[], float],
    *,
    retention_policy_refs: tuple[str, ...] = ("retention", "ephemeral_metadata_only"),
    protection_policy_refs: tuple[str, ...] = ("organization", "organization-policy"),
) -> tuple[SyntheticGovernance, SyntheticStorageAuthority]:
    policy = GovernancePolicy(
        purpose="shovel.organization.governance.v1",
        organization_id="organization",
        scope=scope,
        revision=1,
        issued_at=0,
        expires_at=90000,
        retention_policy_refs=retention_policy_refs,
        protection_policy_refs=protection_policy_refs,
        storage_id="SYNTHETIC-ENCRYPTED-STORAGE",
        key_authority_id="SYNTHETIC-KEY-AUTHORITY",
        evidence_max_age_seconds=5,
        rules=tuple(
            RetentionRule(
                kind=kind,
                storage="encrypted",
                retention_seconds=3600,
                offline_seconds=0,
                on_revocation="purge",
                on_delete="purge",
                erasure="file_sanitization",
                backup="encrypted",
                backup_retention_seconds=300,
                retention_basis="SYNTHETIC acceptance only; not an organization-approved policy",
            )
            for kind in DATA_CLASSES
        ),
    )
    authority = SyntheticStorageAuthority(clock)
    service = SyntheticGovernance(policy, authority, clock)
    return service, authority
