from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.database import session_storage_binding
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.model.enterprise import EnterpriseConnectionProfile as ConnectionRow
from shovel.db.model.governance import EnterpriseRetention as RetentionRow
from shovel.db.model.identity import IdentityProfile as IdentityRow
from shovel.db.model.protection import EnterpriseMetadata as MetadataRow
from shovel.db.model.protection import EnterpriseMetadataSource as SourceRow
from shovel.db.model.protection import EnterpriseSourceFence as FenceRow
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.protection_audit import SQLitePolicyAudit
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.enums import DeletionScope
from shovel.execution.domain import ExecutionLease
from shovel.protection.domain import (
    DataAction,
    DataDestination,
    DataUseBinding,
    ObligationProof,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionAdapterError,
    ProtectionDeniedError,
    SourceBinding,
    TransientContentHandle,
    content_digest,
)
from shovel.protection.governance import GovernanceService, StorageEvidence, installed_governance
from shovel.protection.persistence import (
    ENTERPRISE_COLLECTION,
    EnterpriseMetadata,
    MetadataCandidate,
    SourceChange,
    SourceChangeAuthority,
    source_key,
)
from shovel.protection.policy import ProtectionPolicy
from shovel.protection.service import ProtectionService
from shovel.runtime.errors import RuntimeFailure
from shovel.security.ports import ExecutionGate


class SQLiteProtectionRepository:
    """Metadata only. Stored snapshots and vector hits never supply read authority."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        policy: ProtectionPolicy,
        *,
        source_authority: SourceChangeAuthority | None = None,
        governance: GovernanceService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sessions = sessions
        self._gate = ProtectionService(policy, clock=clock)
        self._source_authority = source_authority
        self._clock = clock
        self.governance = governance if governance is not None else installed_governance()

    @property
    def store_binding(self) -> str:
        return session_storage_binding(self._sessions)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with sqlite_write(self._sessions, durable=True) as session:
                yield session
        except SQLAlchemyError:
            raise ProtectionAdapterError("Enterprise metadata transaction failed.") from None

    def _govern_record(
        self,
        record: EnterpriseMetadata,
        retention: RetentionRow | None,
        *,
        retire: bool = False,
    ) -> tuple[StorageEvidence, float, str, str]:
        current, _ = self.governance.policy()
        if retention is not None and (
            retention.state != "active"
            or retention.store_binding != self.store_binding
            or retention.storage_id != current.storage_id
            or retention.key_authority_id != current.key_authority_id
        ):
            raise ProtectionDeniedError("Enterprise retention is retired or differently stored.")
        deadline = None if retention is None else retention.expires_at
        generation = None if retention is None else retention.authority_generation
        receipts = []
        policy_digest = None
        for kind in record.storage_classes:
            evidence, limit, digest = self.governance.require(
                scope=record.scope,
                store_binding=self.store_binding,
                kind=kind,
                record_digest=content_digest({"id": record.id, "metadata": record.digest}),
                created_at=record.created_at,
                operation="retire" if retire else ("persist" if retention is None else "read"),
                source_keys=tuple(source_key(source.resource) for source in record.context.sources),
                expected_generation=generation,
                expires_at=None if retention is None else retention.expires_at,
                retention_policy_ref=record.context.retention_policy_ref,
                protection_policy_ref=record.context.policy_source_ref,
            )
            if policy_digest is not None and digest != policy_digest:
                raise ProtectionDeniedError("Governance changed across a derived storage admission.")
            if retention is not None and (
                evidence.storage_id != retention.storage_id
                or evidence.key_authority_id != retention.key_authority_id
            ):
                raise ProtectionDeniedError("The original storage/key authority cannot be substituted.")
            if generation is not None and not retire and evidence.generation != generation:
                raise ProtectionDeniedError("Storage authority changed across a derived admission.")
            generation = evidence.generation
            deadline = limit if deadline is None else min(deadline, limit)
            policy_digest = digest
            receipts.append(evidence.evidence_digest)
        if generation is None or policy_digest is None or deadline is None:
            raise ProtectionDeniedError("Derived storage governance evidence is unavailable.")
        return evidence, deadline, policy_digest, content_digest(receipts)

    def _authorize(
        self,
        record: EnterpriseMetadata,
        binding: DataUseBinding,
        action: DataAction,
        destination: DataDestination,
        proofs: tuple[ObligationProof, ...],
    ) -> None:
        if (
            binding.action != action
            or binding.destination != destination
            or binding.identities != record.identities
            or binding.connections != record.connections
            or binding.content_digest != record.digest
        ):
            raise ProtectionDeniedError("Metadata requires its exact owner, payload and destination binding.")
        now = self._clock()
        record.context.require_known(now)
        if record.created_at > now:
            raise ProtectionDeniedError("Metadata creation time is not valid.")
        metadata = ProtectedContentMetadata(
            reference=ProtectedContentRef(
                reference=TransientContentHandle(
                    handle_id=uuid4().hex,
                    process_instance_id=self._gate.process_instance_id,
                    expires_at=now + 30,
                ),
                protection_context_ref=record.context.id,
                content_digest=record.digest,
                sources=record.context.sources,
            ),
            protection_context=record.context,
            provenance=record.lineage,
            authorization_snapshot_refs=tuple(snapshot.id for snapshot in record.snapshots),
            created_at=now,
            lifecycle_state="active",
        )
        self._gate.use(
            ProtectedContent(record.model_dump(mode="json"), metadata), binding, lambda _: None, proofs=proofs
        )

    async def _fences(
        self, session: AsyncSession, record: EnterpriseMetadata, *, allow_new: bool = False
    ) -> dict[str, FenceRow]:
        return await self._source_fences(session, record.scope, record.context.sources, allow_new=allow_new)

    async def _source_fences(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        sources: tuple[SourceBinding, ...],
        *,
        allow_new: bool = False,
    ) -> dict[str, FenceRow]:
        fences: dict[str, FenceRow] = {}
        for source in sources:
            key = source_key(source.resource)
            fence = fences.get(key)
            if fence is None:
                fence = await session.get(FenceRow, (scope.workspace_id, scope.user_id, key))
            if fence is None and allow_new and source.source_generation == 0:
                fence = FenceRow(
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    source_key=key,
                    generation=0,
                    blocked=False,
                )
            if fence is None or fence.blocked or fence.generation != source.source_generation:
                raise ProtectionDeniedError("Source is unavailable, revoked or has a different generation.")
            fences[key] = fence
        return fences

    async def check_sources(self, scope: CapabilityScope, sources: tuple[SourceBinding, ...]) -> None:
        scope = CapabilityScope.model_validate(scope)
        sources = tuple(SourceBinding.model_validate(source) for source in sources)
        if not sources:
            raise ProtectionDeniedError("A protected source binding is required.")
        async with self._transaction() as session:
            await self._source_fences(session, scope, sources, allow_new=True)

    async def _active(self, session: AsyncSession, lease: ExecutionLease, gate: ExecutionGate) -> None:
        executions = SQLiteExecutionRepository(self._sessions, clock=self._clock)
        await executions._active(session, lease, gate)

    async def check_execution(self, lease: ExecutionLease, gate: ExecutionGate) -> None:
        async with self._transaction() as session:
            await self._active(session, lease, gate)

    async def _profiles(self, session: AsyncSession, binding: DataUseBinding) -> None:
        for identity in binding.identities:
            row = await session.get(IdentityRow, identity.identity_profile_id)
            if (
                row is None
                or (row.workspace_id, row.user_id) != (identity.workspace_id, identity.user_id)
                or row.revision != identity.identity_revision
                or row.configuration["tenant_binding"] != identity.tenant_id
            ):
                raise ProtectionDeniedError("Protected identity was changed or disabled.")
        for connection in binding.connections:
            profile = await session.get(ConnectionRow, connection.connection_profile_id)
            bound_identity = next(
                (
                    item
                    for item in binding.identities
                    if item.identity_profile_id == connection.identity_profile_id
                ),
                None,
            )
            if (
                profile is None
                or bound_identity is None
                or profile.status == "disabled"
                or (profile.workspace_id, profile.user_id)
                != (bound_identity.workspace_id, bound_identity.user_id)
                or profile.revision != connection.connection_revision
                or profile.manifest_digest != connection.manifest_digest
                or profile.manifest_version != connection.provider_version
                or profile.configuration["provider_id"] != connection.provider_id
                or profile.configuration["identity_profile_ref"] != bound_identity.identity_profile_id
                or profile.configuration["tenant_binding"] != connection.tenant_id
                or profile.configuration["endpoint"] != connection.endpoint
            ):
                raise ProtectionDeniedError("Protected connection was changed or disabled.")

    async def use_bound[T, R](
        self,
        lease: ExecutionLease,
        content: ProtectedContent[T],
        binding: DataUseBinding,
        consumer: Callable[[T], R],
        *,
        gate: ExecutionGate,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> R:
        scope = lease.execution.snapshot.context.scope
        binding = DataUseBinding.model_validate(binding)
        if (
            binding.execution_id != lease.execution.id
            or binding.actor_id != lease.execution.snapshot.context.actor_id
            or any(
                (item.workspace_id, item.user_id) != (scope.workspace_id, scope.user_id)
                for item in binding.identities
            )
            or binding.action not in {DataAction.READ, DataAction.DISPLAY}
        ):
            raise ProtectionDeniedError("Protected release requires an exact live execution binding.")
        audit = self._gate.policy.audit
        if not isinstance(audit, SQLitePolicyAudit):
            raise ProtectionDeniedError("Durable protection audit is unavailable.")
        decision = None
        try:

            def authorize_audit() -> None:
                self.governance.require(
                    scope=scope,
                    store_binding=self.store_binding,
                    kind="audit",
                    record_digest=binding.digest,
                    created_at=self._clock(),
                    operation="persist",
                    source_keys=tuple(
                        source_key(source.resource) for source in content.metadata.protection_context.sources
                    ),
                    retention_policy_ref=content.metadata.protection_context.retention_policy_ref,
                    protection_policy_ref=content.metadata.protection_context.policy_source_ref,
                )

            async with self._transaction() as session:
                await self._active(session, lease, gate)
                await self._profiles(session, binding)
                await self._source_fences(
                    session, scope, content.metadata.protection_context.sources, allow_new=True
                )
                authorize_audit()
                with audit.bind(session, scope, lease.execution.id):
                    decision = self._gate.policy.evaluate(content.metadata.protection_context, binding)
                await session.flush()
                authorize_audit()
                await session.commit()
            # Audit is durable before release; a new locked read fences local revocation/cancel.
            async with self._transaction() as session:
                await self._active(session, lease, gate)
                await self._profiles(session, binding)
                await self._source_fences(
                    session, scope, content.metadata.protection_context.sources, allow_new=True
                )
                authorize_audit()
                return self._gate.use_decision(content, binding, decision, consumer, proofs=proofs)
        finally:
            if decision is not None:
                self._gate.policy.revoke(decision.id)

    def _fresh_snapshots(self, record: EnterpriseMetadata) -> None:
        now = self._clock()
        for source in record.context.sources:
            identity = next(
                item for item in record.identities if item.identity_profile_id == source.identity_profile_id
            )
            snapshot = next(
                item
                for item in record.snapshots
                if item.identity_profile_id == source.identity_profile_id
                and item.principal_ref == source.principal_ref
                and source.resource in item.resource_scope
            )
            snapshot.require_fresh(identity, source.resource, record.context.policy_version or "", now)

    async def put(
        self,
        record: EnterpriseMetadata,
        binding: DataUseBinding,
        *,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> None:
        record = EnterpriseMetadata.model_validate(record)
        binding = DataUseBinding.model_validate(binding)
        async with self._transaction() as session:
            fences = await self._fences(session, record, allow_new=True)
            self._fresh_snapshots(record)
            self._authorize(record, binding, DataAction.PERSIST, DataDestination.LOCAL_SQLITE, proofs)
            row = await session.get(MetadataRow, (record.scope.workspace_id, record.scope.user_id, record.id))
            retention = await session.get(
                RetentionRow, (record.scope.workspace_id, record.scope.user_id, record.id)
            )
            if row is not None and retention is None:
                raise ProtectionDeniedError("Legacy enterprise metadata has no governed admission.")
            evidence, deadline, policy_digest, _ = self._govern_record(record, retention)
            if row is not None:
                if (
                    row.state != "active"
                    or row.metadata_digest != record.digest
                    or row.payload != record.model_dump(mode="json")
                ):
                    raise RuntimeFailure(
                        "Metadata identifier conflicts with an immutable record.", status=409
                    )
            else:
                session.add_all(fences.values())
                await session.flush()
                session.add(
                    MetadataRow(
                        workspace_id=record.scope.workspace_id,
                        user_id=record.scope.user_id,
                        id=record.id,
                        payload=record.model_dump(mode="json"),
                        metadata_digest=record.digest,
                        admission_binding=binding.model_dump(mode="json"),
                        vector_id=record.vector_id,
                    )
                )
                await session.flush()
                retention = RetentionRow(
                    workspace_id=record.scope.workspace_id,
                    user_id=record.scope.user_id,
                    metadata_id=record.id,
                    policy_digest=policy_digest,
                    store_binding=self.store_binding,
                    authority_generation=evidence.generation,
                    storage_id=evidence.storage_id,
                    key_authority_id=evidence.key_authority_id,
                    admitted_at=self._clock(),
                    expires_at=deadline,
                )
                session.add(retention)
                session.add_all(
                    SourceRow(
                        workspace_id=record.scope.workspace_id,
                        user_id=record.scope.user_id,
                        metadata_id=record.id,
                        source_key=key,
                        generation=fence.generation,
                    )
                    for key, fence in fences.items()
                )
                await session.flush()
            # SQL awaits must not extend an expired/revoked admission into a committed write.
            self._fresh_snapshots(record)
            self._authorize(record, binding, DataAction.PERSIST, DataDestination.LOCAL_SQLITE, proofs)
            await session.flush()
            self._govern_record(record, retention)
            await session.commit()

    async def read(
        self,
        scope: CapabilityScope,
        record_id: str,
        binding: DataUseBinding,
        *,
        candidate: MetadataCandidate | None = None,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> EnterpriseMetadata:
        scope = CapabilityScope.model_validate(scope)
        binding = DataUseBinding.model_validate(binding)
        if candidate is not None:
            candidate = MetadataCandidate.model_validate(candidate)
        async with self._transaction() as session:
            row = await session.get(MetadataRow, (scope.workspace_id, scope.user_id, record_id))
            if row is None or row.state != "active":
                raise ProtectionDeniedError("Metadata is unavailable for this owner.")
            retention = await session.get(RetentionRow, (scope.workspace_id, scope.user_id, record_id))
            if retention is None:
                raise ProtectionDeniedError("Legacy enterprise metadata has no governed admission.")
            try:
                record = EnterpriseMetadata.model_validate(row.payload)
            except ValidationError:
                raise ProtectionAdapterError("Stored enterprise metadata is invalid.") from None
            if (
                record.scope != scope
                or record.id != record_id
                or record.digest != row.metadata_digest
                or record.vector_id != row.vector_id
            ):
                raise ProtectionAdapterError("Stored enterprise metadata binding is invalid.")
            if candidate is not None and (
                candidate.record_id != record.id
                or candidate.revision != row.revision
                or candidate.metadata_digest != record.digest
                or candidate.embedding != record.embedding
            ):
                raise ProtectionDeniedError("Vector candidate is stale or differently bound.")
            await self._fences(session, record)
            self._authorize(record, binding, DataAction.READ, DataDestination.WORKFLOW_STATE, proofs)
            self._govern_record(record, retention)
            return record

    async def purge_retention(self) -> int:
        """Retire externally, redact locally and queue vectors atomically; not media erasure."""
        policy, _ = self.governance.policy()
        scope, purged = policy.scope, 0
        rules = {rule.kind: rule for rule in policy.rules}
        async with self._transaction() as session:
            rows = await session.scalars(
                select(MetadataRow, RetentionRow)
                .join(
                    RetentionRow,
                    (RetentionRow.workspace_id == MetadataRow.workspace_id)
                    & (RetentionRow.user_id == MetadataRow.user_id)
                    & (RetentionRow.metadata_id == MetadataRow.id),
                )
                .where(
                    MetadataRow.workspace_id == scope.workspace_id,
                    MetadataRow.user_id == scope.user_id,
                    RetentionRow.state == "active",
                )
            )
            for row in rows:
                retention = await session.get(RetentionRow, (scope.workspace_id, scope.user_id, row.id))
                if retention is None:
                    raise ProtectionAdapterError("Enterprise retention fact is unavailable.")
                record = EnterpriseMetadata.model_validate(row.payload)
                current_deadline = min(
                    record.created_at + rules[kind].retention_seconds for kind in record.storage_classes
                )
                storage_denied = any(rules[kind].storage == "deny" for kind in record.storage_classes)
                if (
                    row.state == "active"
                    and not storage_denied
                    and self._clock() < min(retention.expires_at, current_deadline)
                ):
                    continue
                _, _, _, evidence_digest = self._govern_record(record, retention, retire=True)
                retention.state = "purged"
                retention.retirement_digest = evidence_digest
                retention.purged_at = self._clock()
                await session.flush()
                row.state, row.payload, row.admission_binding = "deleted", {}, {}
                row.revision += 1
                session.add(
                    DeletionOutbox(
                        scope=DeletionScope.CHUNK_IDS,
                        collection=ENTERPRISE_COLLECTION,
                        filter={"ids": [row.vector_id]},
                        requested_at=int(self._clock()),
                    )
                )
                await session.flush()
                purged += 1
            await session.commit()
        return purged

    async def invalidate(self, change: SourceChange) -> int:
        change = SourceChange.model_validate(change)
        if self._source_authority is None:
            raise ProtectionDeniedError("Trusted source change verification is unavailable.")
        try:
            self._source_authority.verify(change)
        except ProtectionDeniedError:
            raise
        except Exception:
            raise ProtectionAdapterError("Source change verification failed.") from None
        async with self._transaction() as session:
            generation = await self._invalidate(session, change)
            await session.commit()
            return generation

    async def _invalidate(self, session: AsyncSession, change: SourceChange) -> int:
        """Shared transactional adapter primitive; callers must verify their authority first."""
        scope, key = change.scope, source_key(change.resource)
        digest = content_digest(change.model_dump(mode="json"))
        fence = await session.get(FenceRow, (scope.workspace_id, scope.user_id, key))
        if fence is not None and fence.event_id == change.event_id:
            if fence.event_digest != digest:
                raise RuntimeFailure("Source change identifier has different arguments.", status=409)
            return fence.generation
        generation = fence.generation if fence is not None else 0
        if generation != change.expected_generation:
            raise RuntimeFailure("Source generation changed.", status=409)
        if self.governance.authority is not None:
            self.governance.require(
                scope=scope,
                store_binding=self.store_binding,
                kind="control_lineage",
                record_digest=content_digest({"source": key}),
                source_keys=(key,),
                created_at=self._clock(),
                operation="retire",
                retirement_scope="source",
            )
        if fence is None:
            fence = FenceRow(workspace_id=scope.workspace_id, user_id=scope.user_id, source_key=key)
            session.add(fence)
        fence.generation = generation + 1
        fence.blocked, fence.event_id, fence.event_digest = True, change.event_id, digest
        await session.flush()
        rows = list(
            await session.scalars(
                select(MetadataRow)
                .join(
                    SourceRow,
                    (SourceRow.workspace_id == MetadataRow.workspace_id)
                    & (SourceRow.user_id == MetadataRow.user_id)
                    & (SourceRow.metadata_id == MetadataRow.id),
                )
                .where(
                    SourceRow.workspace_id == scope.workspace_id,
                    SourceRow.user_id == scope.user_id,
                    SourceRow.source_key == key,
                    MetadataRow.state.in_(
                        ("active", "invalid") if change.reason == "source_deleted" else ("active",)
                    ),
                )
            )
        )
        for row in rows:
            row.state = "deleted" if change.reason == "source_deleted" else "invalid"
            row.revision += 1
            session.add(
                DeletionOutbox(
                    scope=DeletionScope.CHUNK_IDS,
                    collection=ENTERPRISE_COLLECTION,
                    filter={"ids": [row.vector_id]},
                    requested_at=int(self._clock()),
                )
            )
        return fence.generation
