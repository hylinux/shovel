"""Organization governance and installed storage authority; no local encryption claims."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier, NonEmptyText
from shovel.enterprise.organization import OrganizationAnchor, verify_signed_document
from shovel.identity.domain import Generation, Revision, UnixSeconds
from shovel.protection.domain import ProtectionDeniedError, ProtectionValue, content_digest

type DataClass = Literal[
    "body", "metadata", "vector", "summary", "control_lineage", "artifact", "cache", "audit"
]
type Erasure = Literal["logical", "file_sanitization", "cryptographic", "media"]
type StorageOperation = Literal["persist", "read", "retire", "backup", "restore", "sanitize"]

DATA_CLASSES: tuple[DataClass, ...] = (
    "body",
    "metadata",
    "vector",
    "summary",
    "control_lineage",
    "artifact",
    "cache",
    "audit",
)


class RetentionRule(ProtectionValue):
    kind: DataClass
    storage: Literal["deny", "encrypted"]
    retention_seconds: int = Field(gt=0, strict=True)
    offline_seconds: int = Field(ge=0, strict=True)
    on_revocation: Literal["purge", "retain_until_expiry"]
    on_delete: Literal["purge", "retain_until_expiry"]
    erasure: Erasure
    backup: Literal["deny", "encrypted"]
    backup_retention_seconds: int = Field(ge=0, strict=True)
    retention_basis: NonEmptyText

    @model_validator(mode="after")
    def bounded(self) -> RetentionRule:
        if (
            self.offline_seconds > self.retention_seconds
            or self.backup_retention_seconds > self.retention_seconds
            or (self.backup == "deny" and self.backup_retention_seconds != 0)
            or (self.backup == "encrypted" and self.backup_retention_seconds == 0)
            or (
                self.kind not in {"audit", "control_lineage"}
                and (self.on_revocation != "purge" or self.on_delete != "purge")
            )
        ):
            raise ValueError("Governance retention, offline and deletion obligations conflict.")
        return self


class GovernancePolicy(ProtectionValue):
    purpose: Literal["shovel.organization.governance.v1"]
    organization_id: Identifier
    scope: CapabilityScope
    revision: Revision
    issued_at: UnixSeconds
    expires_at: UnixSeconds
    revoked: bool = False
    retention_policy_refs: tuple[Identifier, ...] = Field(min_length=1)
    protection_policy_refs: tuple[Identifier, ...] = Field(min_length=1)
    storage_id: Identifier
    key_authority_id: Identifier
    evidence_max_age_seconds: int = Field(gt=0, le=30, strict=True)
    rules: tuple[RetentionRule, ...] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def complete(self) -> GovernancePolicy:
        if (
            {rule.kind for rule in self.rules} != set(DATA_CLASSES)
            or len(set(self.retention_policy_refs)) != len(self.retention_policy_refs)
            or len(set(self.protection_policy_refs)) != len(self.protection_policy_refs)
            or self.issued_at >= self.expires_at
        ):
            raise ValueError("A complete, bounded organization governance matrix is required.")
        return self


class StorageRequest(ProtectionValue):
    nonce: Identifier
    scope: CapabilityScope
    organization_id: Identifier
    policy_digest: Digest
    policy_revision: Revision
    storage_id: Identifier
    key_authority_id: Identifier
    store_binding: Digest
    destination_binding: Digest | None
    kind: DataClass
    operation: StorageOperation
    record_digest: Digest
    source_keys: tuple[Digest, ...]
    retirement_scope: Literal["record", "source"]
    expected_generation: Generation | None
    expires_at: UnixSeconds
    offline_seconds: int = Field(ge=0, strict=True)
    erasure: Erasure
    backup_retention_seconds: int = Field(ge=0, strict=True)
    offline: bool

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class StorageEvidence(ProtectionValue):
    request_digest: Digest
    storage_id: Identifier
    key_authority_id: Identifier
    store_binding: Digest
    generation: Generation
    observed_at: UnixSeconds
    expires_at: UnixSeconds
    state: Literal["active", "retired"]
    encrypted_surfaces: tuple[
        Literal["sqlite", "wal", "shm", "temp", "vector", "artifact", "cache", "backup"], ...
    ]
    erasure_capabilities: tuple[Erasure, ...]
    non_exportable_keys: Literal[True]
    consumer_bound: Literal[True]
    rollback_protected: Literal[True]
    control_recovery_safe: Literal[True]
    retention_enforced: Literal[True]
    backup_inventory_complete: Literal[True]
    evidence_digest: Digest


class StorageAuthority(Protocol):
    """Installed TCB only, not configuration flags or an application-owned restore ledger.

    Authorize checks CURRENT organization revision, key status, registered backup inventory,
    source revocations, H2/control recovery fences and retention/offline limits outside the
    restorable SQLite/vector set. An ordinary KMS or volume-encryption flag is insufficient.
    Retire is monotonic and idempotent by scope/store/record/sources, including lost replies.
    Evidence attests all requested obligations, not just BitLocker or keyring availability.
    """

    def authorize(self, request: StorageRequest) -> StorageEvidence: ...

    def retire(self, request: StorageRequest) -> StorageEvidence: ...


type GovernanceLoader = Callable[[], tuple[OrganizationAnchor, bytes] | None]


class GovernanceService:
    def __init__(
        self,
        loader: GovernanceLoader | None = None,
        authority: StorageAuthority | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.loader, self.authority, self.clock = loader, authority, clock

    def policy(self) -> tuple[GovernancePolicy, str]:
        try:
            supplied = None if self.loader is None else self.loader()
            if supplied is None or self.authority is None:
                raise ProtectionDeniedError("Organization storage governance is unavailable.")
            anchor, envelope = supplied
            now = self.clock()
            value, digest, certificate_expiry = verify_signed_document(envelope, anchor, now=now)
            policy = GovernancePolicy.model_validate(value)
            if (
                policy.organization_id != anchor.organization_id
                or policy.revision != anchor.revision
                or not policy.issued_at <= now < policy.expires_at <= certificate_expiry
                or policy.revoked
            ):
                raise ProtectionDeniedError(
                    "Organization governance is revoked, expired or differently bound."
                )
            return policy, digest
        except ProtectionDeniedError:
            raise
        except Exception:
            raise ProtectionDeniedError("Organization storage governance verification failed.") from None

    def require(
        self,
        *,
        scope: CapabilityScope,
        store_binding: str,
        kind: DataClass,
        record_digest: str,
        created_at: float,
        operation: StorageOperation,
        source_keys: tuple[str, ...] = (),
        expected_generation: int | None = None,
        expires_at: float | None = None,
        retention_policy_ref: str | None = None,
        organization_id: str | None = None,
        protection_policy_ref: str | None = None,
        offline: bool = False,
        retirement_scope: Literal["record", "source"] = "record",
        destination_binding: str | None = None,
        backup_created_at: float | None = None,
    ) -> tuple[StorageEvidence, float, str]:
        policy, digest = self.policy()
        now = self.clock()
        rule = next(rule for rule in policy.rules if rule.kind == kind)
        deadline = min(
            created_at + rule.retention_seconds,
            policy.expires_at,
            policy.expires_at if expires_at is None else expires_at,
        )
        if operation in {"backup", "restore"}:
            backup_start = created_at if backup_created_at is None else backup_created_at
            deadline = min(deadline, backup_start + rule.backup_retention_seconds)
        if (
            scope != policy.scope
            or (organization_id is not None and organization_id != policy.organization_id)
            or (
                protection_policy_ref is not None
                and protection_policy_ref not in policy.protection_policy_refs
            )
            or (retention_policy_ref is not None and retention_policy_ref not in policy.retention_policy_refs)
            or (rule.storage != "encrypted" and operation not in {"retire", "sanitize"})
            or created_at > now
            or (operation not in {"retire", "sanitize"} and deadline <= now)
            or (offline and rule.offline_seconds == 0)
            or (operation in {"backup", "restore"} and rule.backup != "encrypted")
            or (operation in {"backup", "restore"} and destination_binding is None)
        ):
            raise ProtectionDeniedError("Organization retention, offline or storage policy denies this use.")
        request = StorageRequest(
            nonce=uuid4().hex,
            scope=scope,
            organization_id=policy.organization_id,
            policy_digest=digest,
            policy_revision=policy.revision,
            storage_id=policy.storage_id,
            key_authority_id=policy.key_authority_id,
            store_binding=store_binding,
            destination_binding=destination_binding,
            kind=kind,
            operation=operation,
            record_digest=record_digest,
            source_keys=source_keys,
            retirement_scope=retirement_scope,
            expected_generation=expected_generation,
            expires_at=deadline,
            offline_seconds=rule.offline_seconds,
            erasure=rule.erasure,
            backup_retention_seconds=rule.backup_retention_seconds,
            offline=offline,
        )
        try:
            authority = self.authority
            if authority is None:
                raise ProtectionDeniedError("Trusted storage authority is unavailable.")
            evidence = StorageEvidence.model_validate(
                authority.retire(request) if operation == "retire" else authority.authorize(request)
            )
        except ProtectionDeniedError:
            raise
        except Exception:
            raise ProtectionDeniedError("Trusted storage/key evidence is unavailable.") from None
        surfaces = {"sqlite", "wal", "shm", "temp"}
        if kind in {"vector", "artifact", "cache"}:
            surfaces.add(kind)
        if operation in {"backup", "restore"} or rule.backup == "encrypted":
            surfaces.add("backup")
        checked_at = self.clock()
        _, current_digest = self.policy()
        if (
            evidence.request_digest != request.digest
            or evidence.storage_id != request.storage_id
            or evidence.key_authority_id != request.key_authority_id
            or evidence.store_binding != request.store_binding
            or not evidence.observed_at <= checked_at < evidence.expires_at
            or checked_at - evidence.observed_at > policy.evidence_max_age_seconds
            or evidence.expires_at - evidence.observed_at > policy.evidence_max_age_seconds
            or not policy.issued_at <= checked_at < policy.expires_at
            or (operation not in {"retire", "sanitize"} and request.expires_at <= checked_at)
            or current_digest != digest
            or not surfaces <= set(evidence.encrypted_surfaces)
            or rule.erasure not in evidence.erasure_capabilities
            or evidence.state != ("retired" if operation == "retire" else "active")
            or (
                expected_generation is not None
                and operation != "retire"
                and evidence.generation != expected_generation
            )
            or (
                expected_generation is not None
                and operation == "retire"
                and evidence.generation < expected_generation
            )
        ):
            raise ProtectionDeniedError("Storage, key, cleanup or anti-rollback evidence is insufficient.")
        return evidence, request.expires_at, digest


_installed: GovernanceService | None = None


def configure_governance_host(loader: GovernanceLoader, authority: StorageAuthority) -> None:
    import sys

    from shovel.identity.native_wam import installed_host

    global _installed
    if _installed is not None or not installed_host() or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Protected installed bootstrap is required for storage governance.")
    _installed = GovernanceService(loader, authority)


def installed_governance() -> GovernanceService:
    return _installed if _installed is not None else GovernanceService()
