from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from threading import Lock
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from shovel.capability.domain import Digest, Identifier, NonEmptyText, Value
from shovel.domain.safe_data import ensure_no_credentials
from shovel.enterprise.domain import ConnectionBinding, EnterpriseResourceRef
from shovel.identity.domain import (
    Ephemeral,
    Generation,
    IdentityBinding,
    Revision,
    TenantId,
    UnixSeconds,
    https_endpoint,
)
from shovel.protection.ports import PolicyAuthority, ProtectionMergePolicy


class ProtectionDeniedError(ValueError):
    pass


class ProtectionAdapterError(RuntimeError):
    pass


class ReferenceUnavailableError(ValueError):
    pass


class ProtectionValue(Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class DataAction(StrEnum):
    READ = "read"
    DISPLAY = "display"
    PROCESS = "process"
    SUMMARIZE = "summarize"
    EXTRACT = "extract"
    TRANSFORM = "transform"
    SEND_TO_MODEL = "send_to_model"
    EMBED = "embed"
    CACHE = "cache"
    INDEX = "index"
    PERSIST = "persist"
    WRITE_TO_MEMORY = "write_to_memory"
    WRITE_TO_KNOWLEDGE = "write_to_knowledge"
    EXPORT = "export"
    SHARE = "share"
    COPY_TO_CLIPBOARD = "copy_to_clipboard"
    WRITE_TO_FILE = "write_to_file"
    SEND_TO_PROVIDER = "send_to_provider"


class DataDestination(StrEnum):
    USER_SCREEN = "user_screen"
    WINDOWS_CLIPBOARD = "windows_clipboard"
    LOCAL_SQLITE = "local_sqlite"
    LOCAL_ZVEC = "local_zvec"
    LOCAL_ARTIFACT_STORE = "local_artifact_store"
    LOCAL_FILE_SYSTEM = "local_file_system"
    ENTERPRISE_MODEL = "enterprise_model"
    EXTERNAL_MODEL = "external_model"
    ENTERPRISE_PROVIDER = "enterprise_provider"
    NETWORK_ENDPOINT = "network_endpoint"
    MEMORY_STORE = "memory_store"
    KNOWLEDGE_STORE = "knowledge_store"
    WORKFLOW_STATE = "workflow_state"
    APPLICATION_LOG = "application_log"
    AUDIT_STORE = "audit_store"


class ContentRight(StrEnum):
    READ = "read"
    DISPLAY = "display"
    COPY = "copy"
    EXTRACT = "extract"
    TRANSFORM = "transform"
    SUMMARIZE = "summarize"
    EMBED = "embed"
    PERSIST = "persist"
    EXPORT = "export"
    SHARE = "share"
    SEND_TO_MODEL = "send_to_model"


class SourceBinding(ProtectionValue):
    resource: EnterpriseResourceRef
    identity_profile_id: Identifier
    principal_ref: Identifier
    source_generation: Generation


class DependencyKind(StrEnum):
    DATA = "data"
    CONTROL = "control"


class LineageEdge(ProtectionValue):
    source: SourceBinding
    kind: DependencyKind


class Lineage(ProtectionValue):
    edges: tuple[LineageEdge, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_edges(self) -> Lineage:
        if len(set(self.edges)) != len(self.edges):
            raise ValueError("Lineage edges must be unique.")
        return self

    @property
    def sources(self) -> tuple[SourceBinding, ...]:
        return tuple(dict.fromkeys(edge.source for edge in self.edges))


class ProtectionContext(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    sources: tuple[SourceBinding, ...] = Field(min_length=1)
    classification_id: Identifier | None = None
    classification_policy_ref: Identifier | None = None
    classification_rank: int | None = None
    sensitivity_label_id: Identifier | None = None
    sensitivity_label_name: NonEmptyText | None = Field(default=None, repr=False)
    protection_status: Literal["unknown", "unprotected", "protected"] = "unknown"
    encrypted: bool | None = Field(default=None, strict=True)
    rights: tuple[ContentRight, ...] | None = None
    policy_source_ref: Identifier | None = None
    policy_version: Digest | None = None
    dlp_applicable: bool | None = Field(default=None, strict=True)
    dlp_decision_ref: Identifier | None = None
    restrictions: tuple[Identifier, ...] = ()
    retention_policy_ref: Identifier | None = None
    expires_at: UnixSeconds | None = None
    source_bound: Literal[True] = True
    delete_with_source: bool = Field(default=True, strict=True)
    revalidate_at: UnixSeconds | None = None
    derived: bool = Field(default=False, strict=True)
    derivation_operation: Identifier | None = None
    model_profile_ref: Identifier | None = None

    @model_validator(mode="after")
    def explicit_sources(self) -> ProtectionContext:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("Protection sources must be unique.")
        if self.derived and self.derivation_operation is None:
            raise ValueError("Derived protection requires an operation reference.")
        return self

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def require_known(self, now: float) -> None:
        if (
            self.classification_id is None
            or self.policy_source_ref is None
            or self.policy_version is None
            or self.protection_status == "unknown"
            or self.encrypted is None
            or self.rights is None
            or self.dlp_applicable is None
            or (self.dlp_applicable and self.dlp_decision_ref is None)
            or not 0 <= now < float("inf")
            or (self.expires_at is not None and now >= self.expires_at)
            or (self.revalidate_at is not None and now >= self.revalidate_at)
        ):
            raise ProtectionDeniedError("Protection metadata is unknown, expired or requires revalidation.")

    def merge(
        self, *others: ProtectionContext, lineage: Lineage, policy: ProtectionMergePolicy
    ) -> ProtectionContext:
        contexts = tuple(ProtectionContext.model_validate(context) for context in (self, *others))
        lineage = Lineage.model_validate(lineage)
        expected = set(source for context in contexts for source in context.sources)
        if expected != set(lineage.sources):
            raise ProtectionDeniedError("Merge lineage does not cover all data and control sources.")
        merged = ProtectionContext.model_validate(policy.merge(contexts, lineage))
        if set(merged.sources) != expected or not merged.derived:
            raise ProtectionDeniedError("Merge policy omitted source protection or derived provenance.")
        return merged


class AuthorizationSnapshot(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    workspace_id: Identifier
    user_id: Identifier
    principal_ref: Identifier
    identity_profile_id: Identifier
    identity_revision: Revision
    tenant_id: TenantId
    resource_scope: tuple[EnterpriseResourceRef, ...] = Field(min_length=1)
    decision: Literal["allow", "deny", "unknown"] = "unknown"
    captured_at: UnixSeconds
    policy_version: Digest
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    revalidate_at: UnixSeconds

    @model_validator(mode="after")
    def consistent_snapshot(self) -> AuthorizationSnapshot:
        if self.revalidate_at <= self.captured_at or any(
            source.tenant_id != self.tenant_id for source in self.resource_scope
        ):
            raise ValueError("Authorization snapshot tenant or validity is inconsistent.")
        return self

    def require_fresh(
        self, identity: IdentityBinding, source: EnterpriseResourceRef, policy: str, now: float
    ) -> None:
        snapshot = AuthorizationSnapshot.model_validate(self)
        identity = IdentityBinding.model_validate(identity)
        source = EnterpriseResourceRef.model_validate(source)
        if (
            snapshot.decision != "allow"
            or snapshot.freshness != "fresh"
            or not snapshot.captured_at <= now < snapshot.revalidate_at
            or identity.workspace_id != snapshot.workspace_id
            or identity.user_id != snapshot.user_id
            or identity.principal_ref != snapshot.principal_ref
            or identity.identity_profile_id != snapshot.identity_profile_id
            or identity.identity_revision != snapshot.identity_revision
            or identity.tenant_id != snapshot.tenant_id
            or policy != snapshot.policy_version
            or source not in snapshot.resource_scope
        ):
            raise ProtectionDeniedError("Authorization snapshot is not usable for this source and identity.")


def model_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return https_endpoint("https" + value[4:]).replace("https", "http", 1)
    return https_endpoint(value)


type ModelEndpoint = Annotated[NonEmptyText, AfterValidator(model_endpoint)]


class ModelBinding(ProtectionValue):
    model_profile_id: Identifier
    revision: Revision
    provider: Identifier
    model_name: NonEmptyText
    endpoint: ModelEndpoint
    environment: Identifier


class ModelProfile(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    revision: Revision = 1
    provider: Identifier
    model_name: NonEmptyText
    endpoint: ModelEndpoint
    environment: Identifier
    enterprise_approved: bool = Field(default=False, strict=True)
    approval_policy_ref: Identifier | None = None
    permitted_classifications: tuple[Identifier, ...] = ()
    permitted_actions: tuple[DataAction, ...] = ()
    permitted_tenants: tuple[TenantId, ...] = ()
    retention_characteristics_ref: Identifier
    data_boundary_ref: Identifier
    embedding_allowed: bool = Field(default=False, strict=True)
    generation_allowed: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=False, strict=True)

    def require_allowed(
        self, binding: ModelBinding, context: ProtectionContext, action: DataAction, *, now: float
    ) -> None:
        profile = ModelProfile.model_validate(self)
        binding = ModelBinding.model_validate(binding)
        context = ProtectionContext.model_validate(context)
        context.require_known(now)
        expected = ModelBinding(
            model_profile_id=profile.id,
            revision=profile.revision,
            provider=profile.provider,
            model_name=profile.model_name,
            endpoint=profile.endpoint,
            environment=profile.environment,
        )
        if (
            not profile.enabled
            or not profile.enterprise_approved
            or profile.approval_policy_ref is None
            or expected != binding
            or context.classification_id not in profile.permitted_classifications
            or action not in profile.permitted_actions
            or any(source.resource.tenant_id not in profile.permitted_tenants for source in context.sources)
            or (action == DataAction.EMBED and not profile.embedding_allowed)
            or (action == DataAction.SEND_TO_MODEL and not profile.generation_allowed)
            or action not in {DataAction.SEND_TO_MODEL, DataAction.EMBED}
        ):
            raise ProtectionDeniedError("Model use is not approved for this exact source, model and action.")


class DataUseBinding(ProtectionValue):
    format_version: Literal[1] = 1
    execution_id: Identifier
    step_id: Identifier
    actor_id: Identifier
    identities: tuple[IdentityBinding, ...] = Field(min_length=1)
    connections: tuple[ConnectionBinding, ...] = Field(min_length=1)
    sources: tuple[SourceBinding, ...] = Field(min_length=1)
    protection_context_ref: Identifier
    protection_context_digest: Digest
    content_digest: Digest
    action: DataAction
    destination: DataDestination
    model: ModelBinding | None = None
    policy_version: Digest

    @model_validator(mode="after")
    def complete_binding(self) -> DataUseBinding:
        if len({(item.workspace_id, item.user_id) for item in self.identities}) != 1:
            raise ValueError("Data use must bind one workspace and user.")
        if len({item.identity_profile_id for item in self.identities}) != len(self.identities) or len(
            {item.connection_profile_id for item in self.connections}
        ) != len(self.connections):
            raise ValueError("Identity and connection revisions must be unambiguous.")
        for values in (self.identities, self.connections, self.sources):
            if len(values) != len(set(values)):
                raise ValueError("Data use bindings must be unique.")
        for source in self.sources:
            if not any(
                identity.identity_profile_id == source.identity_profile_id
                and identity.principal_ref == source.principal_ref
                and identity.tenant_id == source.resource.tenant_id
                for identity in self.identities
            ) or not any(
                connection.connection_profile_id == source.resource.connection_profile_id
                and connection.provider_id == source.resource.provider_id
                and connection.tenant_id == source.resource.tenant_id
                and connection.identity_profile_id == source.identity_profile_id
                for connection in self.connections
            ):
                raise ValueError("Source lacks an exact identity and connection binding.")
        if self.action in {DataAction.SEND_TO_MODEL, DataAction.EMBED} and (
            self.model is None
            or self.destination not in {DataDestination.ENTERPRISE_MODEL, DataDestination.EXTERNAL_MODEL}
        ):
            raise ValueError("Model egress requires an exact model and destination.")
        if self.destination in {DataDestination.ENTERPRISE_MODEL, DataDestination.EXTERNAL_MODEL} and (
            self.action not in {DataAction.SEND_TO_MODEL, DataAction.EMBED}
        ):
            raise ValueError("Model destinations require an explicit model egress action.")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_PROTECTION = "require_protection"
    REQUIRE_REAUTHENTICATION = "require_reauthentication"
    REQUIRE_SOURCE_REVALIDATION = "require_source_revalidation"
    REQUIRE_USER_ACTION = "require_user_action"


class ObligationKind(StrEnum):
    REAUTHENTICATE = "reauthenticate"
    REVALIDATE_SOURCE = "revalidate_source"
    PROTECTED_DISPLAY = "protected_display"
    ENCRYPT_AT_REST = "encrypt_at_rest"
    USER_ACTION = "user_action"
    AUDIT = "audit"


class PolicyObligation(ProtectionValue):
    id: Identifier
    kind: ObligationKind
    target_ref: Identifier


class ObligationProof(ProtectionValue):
    obligation_id: Identifier
    kind: ObligationKind
    target_ref: Identifier
    evidence_ref: Identifier
    verifier_ref: Identifier
    binding_digest: Digest
    policy_version: Digest
    fulfilled_at: UnixSeconds


class PolicyDecision(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    outcome: PolicyOutcome
    binding: DataUseBinding
    rule_refs: tuple[Identifier, ...] = Field(min_length=1)
    reason_codes: tuple[Identifier, ...] = Field(min_length=1)
    explanation: NonEmptyText = Field(repr=False)
    obligations: tuple[PolicyObligation, ...] = ()
    policy_version: Digest
    evaluated_at: UnixSeconds
    revalidate_at: UnixSeconds
    audit_ref: Identifier

    @model_validator(mode="after")
    def valid_decision(self) -> PolicyDecision:
        if self.policy_version != self.binding.policy_version or self.revalidate_at <= self.evaluated_at:
            raise ValueError("Decision version or validity is inconsistent.")
        identifiers = [item.id for item in self.obligations]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Obligations must have unique IDs.")
        return self


class PolicyPermit(Ephemeral):
    """An authenticated, one-use binding; authority enforcement belongs to the later Runtime."""

    __slots__ = ("_authority", "_consumed", "_decision", "_lock", "_proofs")

    def __init__(
        self,
        decision: PolicyDecision,
        *,
        proofs: tuple[ObligationProof, ...] = (),
        authority: PolicyAuthority,
        now: float,
    ) -> None:
        self._decision = PolicyDecision.model_validate(decision)
        self._proofs = tuple(ObligationProof.model_validate(proof) for proof in proofs)
        self._authority = authority
        self._consumed = False
        self._lock = Lock()
        self._validate(self._decision.binding, now)

    def _validate(self, binding: DataUseBinding, now: float) -> None:
        decision = self._decision
        if (
            decision.outcome != PolicyOutcome.ALLOW
            or DataUseBinding.model_validate(binding) != decision.binding
            or not decision.evaluated_at <= now < decision.revalidate_at
        ):
            raise ProtectionDeniedError("Policy permit is denied, expired, consumed or differently bound.")
        self._authority.validate_decision(decision)
        obligations = {item.id: item for item in decision.obligations}
        if len(self._proofs) != len(obligations) or {item.obligation_id for item in self._proofs} != set(
            obligations
        ):
            raise ProtectionDeniedError("All protection obligations require verified evidence.")
        for proof in self._proofs:
            obligation = obligations[proof.obligation_id]
            if (
                proof.kind != obligation.kind
                or proof.target_ref != obligation.target_ref
                or proof.binding_digest != binding.digest
                or proof.policy_version != decision.policy_version
                or not decision.evaluated_at <= proof.fulfilled_at <= now
            ):
                raise ProtectionDeniedError("Obligation evidence does not match this data use.")
            self._authority.validate_proof(proof, decision)

    def consume(self, binding: DataUseBinding, *, now: float) -> PolicyDecision:
        with self._lock:
            if self._consumed:
                raise ProtectionDeniedError("Policy permit has already been consumed.")
            self._consumed = True
        self._validate(binding, now)
        return self._decision


class ContentDurability(StrEnum):
    TRANSIENT = "transient"
    SOURCE_RESOLVABLE = "source_resolvable"
    PERSISTED_ARTIFACT = "persisted_artifact"


class TransientContentHandle(ProtectionValue):
    kind: Literal["transient"] = "transient"
    handle_id: Identifier
    process_instance_id: Identifier
    expires_at: UnixSeconds


class SourceContentRef(ProtectionValue):
    kind: Literal["source"] = "source"
    resource: EnterpriseResourceRef


class ArtifactRef(ProtectionValue):
    kind: Literal["artifact"] = "artifact"
    artifact_id: Identifier
    protection_context_ref: Identifier
    content_digest: Digest


type ContentReference = Annotated[
    TransientContentHandle | SourceContentRef | ArtifactRef, Field(discriminator="kind")
]


class ProtectedContentRef(ProtectionValue):
    format_version: Literal[1] = 1
    reference: ContentReference
    protection_context_ref: Identifier
    content_digest: Digest
    sources: tuple[SourceBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def matching_reference(self) -> ProtectedContentRef:
        if isinstance(self.reference, SourceContentRef) and self.reference.resource not in tuple(
            source.resource for source in self.sources
        ):
            raise ValueError("Source reference is missing from provenance.")
        if isinstance(self.reference, ArtifactRef) and (
            self.reference.content_digest != self.content_digest
            or self.reference.protection_context_ref != self.protection_context_ref
        ):
            raise ValueError("Artifact reference does not match content protection.")
        return self

    @property
    def durability(self) -> ContentDurability:
        if isinstance(self.reference, TransientContentHandle):
            return ContentDurability.TRANSIENT
        if isinstance(self.reference, SourceContentRef):
            return ContentDurability.SOURCE_RESOLVABLE
        return ContentDurability.PERSISTED_ARTIFACT

    def require_resumable(self) -> None:
        if self.durability == ContentDurability.TRANSIENT:
            raise ReferenceUnavailableError("Transient data cannot satisfy a durable workflow dependency.")

    def require_available(
        self, *, available: bool, now: float, process_instance_id: str | None = None
    ) -> None:
        if not available or not 0 <= now < float("inf"):
            raise ReferenceUnavailableError("Protected reference is unavailable; replay is not recovery.")
        if isinstance(self.reference, TransientContentHandle) and (
            self.reference.process_instance_id != process_instance_id or now >= self.reference.expires_at
        ):
            raise ReferenceUnavailableError("Transient reference belongs to an expired process lifetime.")


class DerivedDataRecord(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    output_type: Identifier
    lineage: Lineage
    protection_context_ref: Identifier
    operation_type: Identifier
    executor_identity: Identifier
    execution_id: Identifier
    capability_id: Identifier
    capability_version: Revision
    model_profile_ref: Identifier | None = None
    created_at: UnixSeconds
    policy_decision_ref: Identifier
    lifecycle_state: Literal["active", "invalid", "revalidation_required", "deleted"] = (
        "revalidation_required"
    )


class ProtectedContentMetadata(ProtectionValue):
    format_version: Literal[1] = 1
    reference: ProtectedContentRef
    protection_context: ProtectionContext
    provenance: Lineage
    authorization_snapshot_refs: tuple[Identifier, ...] = Field(min_length=1)
    created_at: UnixSeconds
    lifecycle_state: Literal["active", "invalid", "revalidation_required", "deleted"] = (
        "revalidation_required"
    )

    @model_validator(mode="after")
    def complete_protection(self) -> ProtectedContentMetadata:
        if (
            self.reference.protection_context_ref != self.protection_context.id
            or set(self.reference.sources) != set(self.protection_context.sources)
            or set(self.provenance.sources) != set(self.protection_context.sources)
        ):
            raise ValueError("Content reference, protection and lineage must bind the same sources.")
        return self


def content_digest(value: object) -> str:
    ensure_no_credentials(value)
    if isinstance(value, bytes):
        encoded = b"bytes:" + value
    else:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except TypeError, ValueError:
            raise ValueError("Content requires a supported canonical representation.") from None
    return hashlib.sha256(encoded).hexdigest()


class ProtectedContent[T](Ephemeral):
    __slots__ = ("_lock", "_metadata", "_released", "_value")

    def __init__(self, value: T, metadata: ProtectedContentMetadata) -> None:
        metadata = ProtectedContentMetadata.model_validate(metadata)
        if content_digest(value) != metadata.reference.content_digest:
            raise ValueError("Content does not match its digest.")
        self._value = value
        self._metadata = metadata
        self._released = False
        self._lock = Lock()

    @property
    def metadata(self) -> ProtectedContentMetadata:
        return self._metadata

    def release(self, permit: PolicyPermit, binding: DataUseBinding, *, now: float) -> T:
        with self._lock:
            if self._released:
                raise ProtectionDeniedError("Protected content has already been consumed.")
            self._released = True
        metadata = self._metadata
        metadata.protection_context.require_known(now)
        if (
            metadata.lifecycle_state != "active"
            or (
                isinstance(metadata.reference.reference, TransientContentHandle)
                and now >= metadata.reference.reference.expires_at
            )
            or binding.protection_context_ref != metadata.reference.protection_context_ref
            or binding.protection_context_digest != metadata.protection_context.content_hash
            or binding.content_digest != metadata.reference.content_digest
            or set(binding.sources) != set(metadata.reference.sources)
            or binding.policy_version != metadata.protection_context.policy_version
            or content_digest(self._value) != metadata.reference.content_digest
        ):
            raise ProtectionDeniedError("Content is unavailable or differs from the authorized binding.")
        permit.consume(binding, now=now)
        if content_digest(self._value) != metadata.reference.content_digest:
            raise ProtectionDeniedError("Content changed during protection revalidation.")
        return self._value
