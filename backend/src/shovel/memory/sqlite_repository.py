"""SQLite transactions are the authority, including pending vector mutations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any

from sqlalchemy import and_, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.base import now_ts
from shovel.db.model.memory import (
    MemoryBelief,
    MemoryEpisode,
    MemoryIndexOutbox,
    MemoryIndexState,
    MemoryProcedure,
    NativeMemory,
)
from shovel.db.model.runtime import ChatMessage, MemoryEpoch, Operation
from shovel.domain.enums import MemoryPredicate, MemorySource, MemoryValidityState, VectorState
from shovel.memory.domain import (
    ExtractionCancelled,
    IndexIntent,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryState,
    MemoryType,
    Provenance,
    ScopeKind,
    conflicts,
    same_fact,
)
from shovel.runtime.errors import NotFound, RuntimeFailure

type Row = NativeMemory
_TABLES: dict[MemoryType, type[Row]] = {
    MemoryType.SEMANTIC: MemoryBelief,
    MemoryType.EPISODIC: MemoryEpisode,
    MemoryType.PROCEDURAL: MemoryProcedure,
}


def _visible(table: Any, scope: MemoryScope) -> Any:
    return and_(
        table.workspace_id == scope.workspace_id,
        table.user_id == scope.user_id,
        or_(
            table.scope_kind.in_(("user", "workspace")),
            and_(table.scope_kind == "agent", table.agent_id == scope.agent_id),
            and_(
                table.scope_kind == "session",
                table.agent_id == scope.agent_id,
                table.session_id == scope.session_id,
                table.session_id != "",
            ),
        ),
    )


def record(row: Row, kind: MemoryType) -> MemoryRecord:
    if isinstance(row, MemoryBelief):
        body = row.note or row.render()
        subject, predicate, obj, topic = (
            row.subject,
            row.predicate.value,
            row.object_,
            row.object_domain or "",
        )
    elif isinstance(row, MemoryEpisode):
        body, subject, predicate, obj, topic = row.summary, "", "", "", ""
    else:
        assert isinstance(row, MemoryProcedure)
        body, subject, predicate, obj, topic = row.instruction, "", "", "", row.trigger_pattern
    return MemoryRecord(
        id=row.id,
        kind=kind,
        text=body,
        scope=MemoryScope(
            row.workspace_id,
            row.user_id,
            row.agent_id or "shovel",
            row.session_id or "",
            ScopeKind(row.scope_kind),
        ),
        state=MemoryState(row.validity_state.value),
        fingerprint=row.fingerprint,
        revision=row.rev,
        recorded_at=row.recorded_at,
        confidence=row.confidence,
        importance=row.importance,
        access_count=row.access_count,
        last_accessed_at=row.last_accessed_at,
        expires_at=row.valid_until,
        subject=subject,
        predicate=predicate,
        object=obj,
        topic=topic,
        superseded_by=row.superseded_by,
        model_version=row.model_version,
        provenance=row.provenance,
        occurred_at=row.occurred_at if isinstance(row, MemoryEpisode) else None,
    )


def _intent(session: AsyncSession, row: Row, kind: MemoryType, action: str) -> None:
    session.add(
        MemoryIndexOutbox(
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            memory_id=row.id,
            kind=kind.value,
            revision=row.rev,
            action=action,
        )
    )


class SQLiteMemoryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def list_records(
        self,
        scope: MemoryScope,
        *,
        limit: int = 100,
        states: Sequence[MemoryState] = (MemoryState.ACTIVE,),
    ) -> list[MemoryRecord]:
        found: list[MemoryRecord] = []
        async with self.sessions() as session:
            for kind, table in _TABLES.items():
                rows = await session.scalars(
                    select(table)
                    .where(
                        _visible(table, scope),
                        table.validity_state.in_([s.value for s in states]),
                        or_(table.valid_until.is_(None), table.valid_until > now_ts())
                        if states == (MemoryState.ACTIVE,)
                        else true(),
                    )
                    .order_by(table.recorded_at.desc(), table.id)
                    .limit(limit)
                )
                found.extend(record(row, kind) for row in rows)
        return sorted(found, key=lambda row: (row.recorded_at, row.id), reverse=True)[:limit]

    async def _find(
        self, session: AsyncSession, scope: MemoryScope, identifier: str
    ) -> tuple[Row, MemoryType]:
        for kind, table in _TABLES.items():
            row = await session.scalar(select(table).where(table.id == identifier, _visible(table, scope)))
            if row is not None:
                return row, kind
        raise NotFound("Memory", identifier)

    async def get(self, scope: MemoryScope, identifier: str) -> MemoryRecord:
        async with self.sessions() as session:
            row, kind = await self._find(session, scope, identifier)
            return record(row, kind)

    async def consolidate(
        self,
        scope: MemoryScope,
        candidates: Sequence[MemoryCandidate],
        provenance: Provenance,
        *,
        operation_id: str | None = None,
        generation: int | None = None,
        replace_id: str | None = None,
    ) -> list[MemoryRecord]:
        results = []
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = None
            if operation_id is not None:
                operation = await session.get(Operation, operation_id)
                epoch = await session.scalar(
                    select(MemoryEpoch).where(
                        MemoryEpoch.workspace_id == scope.workspace_id,
                        MemoryEpoch.user_id == scope.user_id,
                    )
                )
                source = (
                    await session.get(ChatMessage, provenance.message_id) if provenance.message_id else None
                )
                if (
                    operation is None
                    or operation.state != "running"
                    or epoch is None
                    or epoch.generation != generation
                    or operation.payload.get("workspace_id") != scope.workspace_id
                    or operation.payload.get("user_id") != scope.user_id
                ):
                    raise ExtractionCancelled()
                if provenance.source_kind == "user_message" and (
                    source is None
                    or source.role != "user"
                    or source.conversation_id != provenance.conversation_id
                ):
                    raise ExtractionCancelled()
            for candidate in candidates:
                table = _TABLES[candidate.kind]
                agent = scope.agent_id if scope.kind in (ScopeKind.AGENT, ScopeKind.SESSION) else ""
                conversation = scope.session_id if scope.kind == ScopeKind.SESSION else ""
                rows = list(
                    await session.scalars(
                        select(table).where(
                            table.workspace_id == scope.workspace_id,
                            table.user_id == scope.user_id,
                            table.scope_kind == scope.kind.value,
                            table.agent_id == agent,
                            table.session_id == conversation,
                            table.validity_state.in_(("active", "deleted")),
                        )
                    )
                )
                for existing in rows:
                    if (
                        existing.validity_state == MemoryValidityState.ACTIVE
                        and existing.valid_until is not None
                        and existing.valid_until <= now_ts()
                    ):
                        existing.validity_state = MemoryValidityState.EXPIRED
                        existing.invalidated_at = now_ts()
                        _intent(session, existing, candidate.kind, "delete")
                rows = [
                    r
                    for r in rows
                    if r.validity_state in (MemoryValidityState.ACTIVE, MemoryValidityState.DELETED)
                ]
                duplicate = next((r for r in rows if same_fact(record(r, candidate.kind), candidate)), None)
                if duplicate is not None and duplicate.validity_state == MemoryValidityState.DELETED:
                    continue
                if candidate.merge_with:
                    duplicate = next((r for r in rows if r.id == candidate.merge_with), None)
                    if duplicate is None or duplicate.validity_state != MemoryValidityState.ACTIVE:
                        raise ExtractionCancelled()
                evidence = asdict(replace(provenance, evidence=candidate.evidence))
                if duplicate is not None and replace_id is None:
                    if duplicate.validity_state == MemoryValidityState.ACTIVE:
                        duplicate.confidence = max(duplicate.confidence, candidate.confidence)
                        duplicate.importance = max(duplicate.importance, candidate.importance)
                        if evidence not in duplicate.provenance:
                            duplicate.provenance = [*duplicate.provenance[-19:], evidence]
                        results.append(record(duplicate, candidate.kind))
                    continue
                common: dict[str, Any] = {
                    "workspace_id": scope.workspace_id,
                    "user_id": scope.user_id,
                    "agent_id": agent,
                    "session_id": conversation,
                    "scope_kind": scope.kind.value,
                    "fingerprint": candidate.fingerprint,
                    "confidence": candidate.confidence,
                    "importance": candidate.importance,
                    "provenance": [evidence],
                    "valid_until": candidate.expires_at,
                    "source": MemorySource.STATED,
                }
                if candidate.kind == MemoryType.SEMANTIC:
                    row: Row = MemoryBelief(
                        **common,
                        subject=candidate.subject,
                        predicate=MemoryPredicate(candidate.predicate),
                        object_=candidate.object or candidate.text,
                        object_domain=candidate.topic,
                        note=candidate.text,
                    )
                elif candidate.kind == MemoryType.EPISODIC:
                    row = MemoryEpisode(
                        **common, summary=candidate.text, occurred_at=candidate.occurred_at or now_ts()
                    )
                else:
                    row = MemoryProcedure(
                        **common,
                        name=candidate.topic or candidate.fingerprint,
                        trigger_pattern=candidate.topic,
                        instruction=candidate.text,
                    )
                replaced: list[Row] = []
                for previous in rows:
                    if previous.validity_state == MemoryValidityState.ACTIVE and (
                        previous.id == replace_id or conflicts(record(previous, candidate.kind), candidate)
                    ):
                        previous.validity_state = MemoryValidityState.SUPERSEDED
                        previous.invalidated_at = now_ts()
                        previous.supersede_reason = "Explicit user correction"
                        _intent(session, previous, candidate.kind, "delete")
                        replaced.append(previous)
                if replace_id is not None and not any(r.id == replace_id for r in replaced):
                    raise RuntimeFailure("只能更新同一 Scope 中的 active Memory。", status=409)
                await session.flush()
                session.add(row)
                await session.flush()
                for previous in replaced:
                    previous.superseded_by = row.id
                _intent(session, row, candidate.kind, "upsert")
                results.append(record(row, candidate.kind))
            if operation is not None:
                operation.state, operation.finished_at = "succeeded", now_ts()
                operation.result = {"committed": True, "memories": len(results)}
                operation.error = None
            await session.commit()
        return results

    async def transition(
        self, scope: MemoryScope, identifier: str, state: MemoryState, *, hard: bool = False
    ) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row, kind = await self._find(session, scope, identifier)
            if hard and state != MemoryState.DELETED:
                raise ValueError("Hard removal requires deleted state.")
            if state not in (MemoryState.DELETED, MemoryState.EXPIRED, MemoryState.REJECTED):
                raise ValueError("Use consolidation for activation and supersession.")
            row.validity_state = MemoryValidityState(state.value)
            row.invalidated_at = now_ts()
            _intent(session, row, kind, "delete")
            if hard:
                await session.delete(row)
            await session.commit()

    async def pending(self, scope: MemoryScope, *, force: bool = False) -> list[IndexIntent]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(MemoryIndexOutbox)
                .where(
                    MemoryIndexOutbox.workspace_id == scope.workspace_id,
                    MemoryIndexOutbox.user_id == scope.user_id,
                    MemoryIndexOutbox.done_at.is_(None),
                    true() if force else MemoryIndexOutbox.retry_at <= now_ts(),
                )
                .order_by(MemoryIndexOutbox.created_at, MemoryIndexOutbox.id)
                .limit(16)
            )
            return [IndexIntent(r.id, r.memory_id, MemoryType(r.kind), r.revision, r.action) for r in rows]

    async def acknowledge(self, intent: IndexIntent, model_version: str) -> None:
        async with self.sessions() as session:
            outbox = await session.get(MemoryIndexOutbox, intent.id)
            if outbox is None:
                return
            outbox.done_at, outbox.last_error = now_ts(), None
            row = await session.get(_TABLES[intent.kind], intent.memory_id)
            if (
                row is not None
                and row.rev == intent.revision
                and intent.action == "upsert"
                and row.validity_state == MemoryValidityState.ACTIVE
            ):
                row.vector_state, row.model_version = VectorState.INDEXED, model_version
            await session.commit()

    async def fail(self, intent: IndexIntent, error_type: str) -> None:
        async with self.sessions() as session:
            row = await session.get(MemoryIndexOutbox, intent.id)
            if row is not None:
                row.attempts += 1
                row.retry_at = now_ts() + min(300, 2 ** min(row.attempts, 8))
                row.last_error = error_type
                state = await session.get(MemoryIndexState, (row.workspace_id, row.user_id))
                if state is not None:
                    state.last_error = error_type
                await session.commit()

    async def expire_due(self, scope: MemoryScope) -> int:
        count = 0
        async with self.sessions() as session:
            for kind, table in _TABLES.items():
                rows = await session.scalars(
                    select(table)
                    .where(
                        table.workspace_id == scope.workspace_id,
                        table.user_id == scope.user_id,
                        table.validity_state == MemoryValidityState.ACTIVE,
                        table.valid_until.is_not(None),
                        table.valid_until <= now_ts(),
                    )
                    .limit(32)
                )
                for row in rows:
                    row.validity_state = MemoryValidityState.EXPIRED
                    row.invalidated_at = now_ts()
                    _intent(session, row, kind, "delete")
                    count += 1
            await session.commit()
        return count

    async def reindex(self, scope: MemoryScope) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            pending = list(
                await session.scalars(
                    select(MemoryIndexOutbox).where(
                        MemoryIndexOutbox.workspace_id == scope.workspace_id,
                        MemoryIndexOutbox.user_id == scope.user_id,
                        MemoryIndexOutbox.done_at.is_(None),
                    )
                )
            )
            keys = {(r.memory_id, r.kind, r.revision, r.action) for r in pending}
            for intent in pending:
                intent.retry_at = 0
            for kind, table in _TABLES.items():
                rows = await session.scalars(
                    select(table).where(
                        table.workspace_id == scope.workspace_id,
                        table.user_id == scope.user_id,
                        table.validity_state == MemoryValidityState.ACTIVE,
                    )
                )
                for row in rows:
                    row.vector_state = VectorState.PENDING
                    if (row.id, kind.value, row.rev, "upsert") not in keys:
                        _intent(session, row, kind, "upsert")
            await session.commit()

    async def touch(self, scope: MemoryScope, identifiers: Sequence[str]) -> None:
        async with self.sessions() as session:
            for table in _TABLES.values():
                await session.execute(
                    update(table)
                    .where(table.id.in_(identifiers), _visible(table, scope))
                    .values(access_count=table.access_count + 1, last_accessed_at=now_ts())
                )
            await session.commit()
