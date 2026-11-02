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
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.model.protection import EnterpriseMetadata as MetadataRow
from shovel.db.model.protection import EnterpriseMetadataSource as SourceRow
from shovel.db.model.protection import EnterpriseSourceFence as FenceRow
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.protection_audit import SQLitePolicyAudit
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.enums import DeletionScope
from shovel.execution.domain import BudgetExceeded, ExecutionLease
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
from shovel.security.domain import SecurityDenied
from shovel.security.ports import ExecutionGate


class SQLiteProtectionRepository:
    """Metadata only. Stored snapshots and vector hits never supply read authority."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        policy: ProtectionPolicy,
        *,
        source_authority: SourceChangeAuthority | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sessions = sessions
        self._gate = ProtectionService(policy, clock=clock)
        self._source_authority = source_authority
        self._clock = clock

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with sqlite_write(self._sessions) as session:
                yield session
        except SQLAlchemyError:
            raise ProtectionAdapterError("Enterprise metadata transaction failed.") from None

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
            if key in fences:
                continue
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
        row = await executions._find(session, lease.execution.snapshot.context.scope, lease.execution.id)
        executions._leased(row, lease)
        for ancestor in await executions._lineage(session, row):
            if ancestor.state != "running":
                raise SecurityDenied("Execution is no longer active.")
            if ancestor.execution_deadline is None or self._clock() >= ancestor.execution_deadline:
                raise BudgetExceeded("duration")
        evaluation = await gate.evaluate(_record(row))
        if evaluation.approval.required:
            approval = await SQLiteApprovalRepository.latest(session, row.id)
            if (
                approval is None
                or approval.state != "consumed"
                or approval.binding_hash != evaluation.binding_hash
                or approval.expires_at <= self._clock()
            ):
                raise SecurityDenied("Current execution approval is not usable.")

    async def check_execution(self, lease: ExecutionLease, gate: ExecutionGate) -> None:
        async with self._transaction() as session:
            await self._active(session, lease, gate)

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
            async with self._transaction() as session:
                await self._active(session, lease, gate)
                await self._source_fences(
                    session, scope, content.metadata.protection_context.sources, allow_new=True
                )
                with audit.bind(session, scope, lease.execution.id):
                    decision = self._gate.policy.evaluate(content.metadata.protection_context, binding)
                await session.commit()
            # Audit is durable before release; a new locked read fences local revocation/cancel.
            async with self._transaction() as session:
                await self._active(session, lease, gate)
                await self._source_fences(
                    session, scope, content.metadata.protection_context.sources, allow_new=True
                )
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
            return record

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
        scope = change.scope
        key = source_key(change.resource)
        digest = content_digest(change.model_dump(mode="json"))
        async with self._transaction() as session:
            fence = await session.get(FenceRow, (scope.workspace_id, scope.user_id, key))
            if fence is not None and fence.event_id == change.event_id:
                if fence.event_digest != digest:
                    raise RuntimeFailure("Source change identifier has different arguments.", status=409)
                return fence.generation
            generation = fence.generation if fence is not None else 0
            if generation != change.expected_generation:
                raise RuntimeFailure("Source generation changed.", status=409)
            if fence is None:
                fence = FenceRow(
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    source_key=key,
                )
                session.add(fence)
            fence.generation = generation + 1
            fence.blocked = True
            fence.event_id = change.event_id
            fence.event_digest = digest
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
            await session.commit()
            return fence.generation
