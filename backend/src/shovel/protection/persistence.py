from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import UUID5, Field, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier
from shovel.enterprise.domain import ConnectionBinding, EnterpriseResourceRef
from shovel.identity.domain import Generation, IdentityBinding, Revision, UnixSeconds
from shovel.protection.domain import (
    AuthorizationSnapshot,
    DependencyKind,
    DerivedDataRecord,
    Lineage,
    ModelBinding,
    ProtectionContext,
    ProtectionValue,
    ReferenceUnavailableError,
    SourceContentRef,
    content_digest,
)

ENTERPRISE_COLLECTION = "shovel_enterprise_metadata_v1"

if TYPE_CHECKING:
    from shovel.protection.governance import DataClass


def source_key(resource: EnterpriseResourceRef) -> str:
    resource = EnterpriseResourceRef.model_validate(resource)
    # Versions, ETags and parent moves share one revocation fence.
    return content_digest(
        {
            key: value
            for key, value in resource.model_dump(mode="json").items()
            if key not in {"source_version", "etag", "parent_resource_id"}
        }
    )


class EmbeddingMetadata(ProtectionValue):
    model: ModelBinding
    dimension: int = Field(strict=True, ge=1)
    vector_digest: Digest
    index_generation: Generation


class EnterpriseMetadata(ProtectionValue):
    format_version: Literal[1] = 1
    id: Identifier
    scope: CapabilityScope
    context: ProtectionContext
    lineage: Lineage
    identities: tuple[IdentityBinding, ...] = Field(min_length=1)
    connections: tuple[ConnectionBinding, ...] = Field(min_length=1)
    snapshots: tuple[AuthorizationSnapshot, ...] = Field(min_length=1)
    content_digest: Digest
    body_recovery: Literal["source_reference", "unavailable"]
    source_reference: SourceContentRef | None = None
    derivation: DerivedDataRecord | None = None
    embedding: EmbeddingMetadata | None = None
    created_at: UnixSeconds

    @model_validator(mode="after")
    def reference_only(self) -> EnterpriseMetadata:
        from shovel.protection.domain import DataAction, DataDestination, DataUseBinding

        if set(self.lineage.sources) != set(self.context.sources):
            raise ValueError("Metadata must retain every data and control source.")
        if len({source.resource.tenant_id for source in self.context.sources}) != 1:
            raise ValueError("Cross-tenant metadata is not supported.")
        generations: dict[str, int] = {}
        for source in self.context.sources:
            key = source_key(source.resource)
            if key in generations and generations[key] != source.source_generation:
                raise ValueError("Source generations must be unambiguous.")
            generations[key] = source.source_generation
        if any(
            (identity.workspace_id, identity.user_id) != (self.scope.workspace_id, self.scope.user_id)
            for identity in self.identities
        ):
            raise ValueError("Metadata identities must belong to its owner.")
        DataUseBinding(
            execution_id="metadata-validation",
            step_id="metadata-validation",
            actor_id="metadata-validation",
            identities=self.identities,
            connections=self.connections,
            sources=self.context.sources,
            protection_context_ref=self.context.id,
            protection_context_digest=self.context.content_hash,
            content_digest=self.content_digest,
            action=DataAction.PERSIST,
            destination=DataDestination.LOCAL_SQLITE,
            policy_version=self.context.policy_version or "0" * 64,
        )
        if {identity.identity_profile_id for identity in self.identities} != {
            source.identity_profile_id for source in self.context.sources
        } or {connection.connection_profile_id for connection in self.connections} != {
            source.resource.connection_profile_id for source in self.context.sources
        }:
            raise ValueError("Metadata cannot introduce unrelated identity or connection bindings.")
        if len({snapshot.id for snapshot in self.snapshots}) != len(self.snapshots) or any(
            (snapshot.workspace_id, snapshot.user_id) != (self.scope.workspace_id, self.scope.user_id)
            for snapshot in self.snapshots
        ):
            raise ValueError("Snapshot references must be unique and scoped.")
        for snapshot in self.snapshots:
            identity = next(
                (
                    item
                    for item in self.identities
                    if item.identity_profile_id == snapshot.identity_profile_id
                ),
                None,
            )
            covered = {
                source.resource
                for source in self.context.sources
                if source.identity_profile_id == snapshot.identity_profile_id
                and source.principal_ref == snapshot.principal_ref
            }
            if (
                identity is None
                or identity.principal_ref != snapshot.principal_ref
                or identity.identity_revision != snapshot.identity_revision
                or identity.tenant_id != snapshot.tenant_id
                or snapshot.policy_version != self.context.policy_version
                or not set(snapshot.resource_scope) <= covered
            ):
                raise ValueError("Snapshots must bind only the recorded identities and source versions.")
        if any(
            not any(
                snapshot.identity_profile_id == source.identity_profile_id
                and snapshot.principal_ref == source.principal_ref
                and source.resource in snapshot.resource_scope
                for snapshot in self.snapshots
            )
            for source in self.context.sources
        ):
            raise ValueError("Every source requires an authorization snapshot reference.")
        if self.context.derived:
            if (
                self.body_recovery != "unavailable"
                or self.source_reference is not None
                or self.derivation is None
                or self.derivation.protection_context_ref != self.context.id
                or self.derivation.lineage != self.lineage
                or self.derivation.operation_type != self.context.derivation_operation
                or self.derivation.model_profile_ref != self.context.model_profile_ref
                or self.derivation.created_at != self.created_at
            ):
                raise ValueError("Derived metadata cannot claim source-resolvable content.")
        elif (
            self.body_recovery != "source_reference"
            or self.derivation is not None
            or self.embedding is not None
            or self.source_reference is None
            or {edge.source.resource for edge in self.lineage.edges if edge.kind == DependencyKind.DATA}
            != {self.source_reference.resource}
        ):
            raise ValueError("Original metadata requires its exact data source reference.")
        if self.embedding is not None and (
            self.embedding.vector_digest != self.content_digest
            or self.embedding.model.model_profile_id != self.context.model_profile_ref
        ):
            raise ValueError("Embedding metadata must bind the derived content and model.")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))

    @property
    def storage_classes(self) -> tuple[DataClass, ...]:
        kinds: list[DataClass] = ["metadata", "control_lineage"]
        if self.embedding is not None:
            kinds.append("vector")
        if self.derivation is not None and self.derivation.output_type == "summary":
            kinds.append("summary")
        return tuple(kinds)

    @property
    def vector_id(self) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                f"shovel:enterprise:v1:{self.scope.workspace_id}:{self.scope.user_id}:{self.id}:{self.digest}",
            )
        )

    def require_source_reference(self) -> SourceContentRef:
        if self.source_reference is None:
            raise ReferenceUnavailableError(
                "Derived metadata has no recoverable body; replay is not recovery."
            )
        return self.source_reference


class MetadataCandidate(ProtectionValue):
    record_id: Identifier
    revision: Revision
    metadata_digest: Digest
    embedding: EmbeddingMetadata


class SourceChange(ProtectionValue):
    event_id: Identifier
    scope: CapabilityScope
    resource: EnterpriseResourceRef
    expected_generation: Generation
    reason: Literal[
        "source_changed", "source_revoked", "source_deleted", "classification_changed", "retention_changed"
    ]


class SourceChangeAuthority(Protocol):
    def verify(self, change: SourceChange) -> None: ...


class EnterpriseVectorDeletion(ProtectionValue):
    ids: tuple[UUID5, ...] = Field(min_length=1, max_length=1024)
