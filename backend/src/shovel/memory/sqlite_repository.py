"""SQLite transactions are the authority, including pending vector mutations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any

from sqlalchemy import and_, case, column, false, func, or_, select, text, true, update
from sqlalchemy import table as sql_table
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
from shovel.db.model.user_profile import UserProfile
from shovel.domain.enums import MemoryPredicate, MemorySource, MemoryValidityState, VectorState
from shovel.memory.domain import (
    PROFILE_FIELDS,
    PROFILE_TOPICS,
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
from shovel.memory.relevance import literal_match, literal_pattern, query_terms
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.errors import NotFound, RuntimeFailure

type Row = NativeMemory
_TABLES: dict[MemoryType, type[Row]] = {
    MemoryType.SEMANTIC: MemoryBelief,
    MemoryType.EPISODIC: MemoryEpisode,
    MemoryType.PROCEDURAL: MemoryProcedure,
}
_FTS = sql_table("memory_search", column("memory_id"), column("kind"), column("rank"))


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
        occurred_at=(
            row.occurred_at
            if isinstance(row, MemoryEpisode) and row.meta.get("event_time_source") != "recorded"
            else None
        ),
        source=row.source.value,
        episode_time_source=(
            str(row.meta.get("event_time_source", "legacy_unspecified"))
            if isinstance(row, MemoryEpisode)
            else None
        ),
        episode_time_precision=(
            str(row.meta.get("event_time_precision", "unknown")) if isinstance(row, MemoryEpisode) else None
        ),
    )


def queue_index_intent(session: AsyncSession, row: Row, kind: MemoryType, action: str) -> None:
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

    async def keyword(self, scope: MemoryScope, query: str, *, limit: int) -> list[MemoryRecord]:
        terms = query_terms(query)
        long, short = [t for t in terms if len(t) >= 3], [t for t in terms if len(t) < 3]
        found: dict[str, tuple[MemoryRecord, float]] = {}
        async with self.sessions() as session:
            for kind, model in _TABLES.items():
                body = (
                    func.coalesce(MemoryBelief.note, MemoryBelief.object_)
                    if kind == MemoryType.SEMANTIC
                    else MemoryEpisode.summary
                    if kind == MemoryType.EPISODIC
                    else MemoryProcedure.instruction
                )
                base = select(model).where(
                    _visible(model, scope),
                    model.validity_state == MemoryValidityState.ACTIVE,
                    or_(model.valid_until.is_(None), model.valid_until > now_ts()),
                    or_(
                        *(
                            body.regexp_match(pattern)
                            if (pattern := literal_pattern(term))
                            else body.icontains(term, autoescape=True)
                            for term in terms
                        )
                    )
                    if terms
                    else false(),
                )
                if long:
                    expression = " OR ".join(f'"{term}"' for term in long)
                    fts_rows = await session.execute(
                        base.add_columns(_FTS.c.rank)
                        .join(_FTS, and_(_FTS.c.memory_id == model.id, _FTS.c.kind == kind.value))
                        .where(text("memory_search MATCH :query"))
                        .order_by(_FTS.c.rank, model.id)
                        .limit(limit),
                        {"query": expression},
                    )
                    for row, score in fts_rows:
                        found[row.id] = (record(row, kind), float(score))
                if short:
                    rows = await session.scalars(
                        base.where(or_(*(body.icontains(term, autoescape=True) for term in short)))
                        .order_by(model.recorded_at.desc(), model.id)
                        .limit(limit)
                    )
                    for row in rows:
                        found.setdefault(row.id, (record(row, kind), 0.0))
        return [
            item
            for item, _ in sorted(found.values(), key=lambda pair: (pair[1], pair[0].id))
            if any(literal_match(term, item.text) for term in terms)
        ][:limit]

    async def profile(self, scope: MemoryScope, *, limit: int) -> list[MemoryRecord]:
        table = MemoryBelief
        asserted_at = func.coalesce(
            case(
                (
                    func.json_type(table.provenance, "$[#-1].asserted_at") == "integer",
                    func.json_extract(table.provenance, "$[#-1].asserted_at"),
                )
            ),
            select(ChatMessage.created_at)
            .where(ChatMessage.id == func.json_extract(table.provenance, "$[#-1].message_id"))
            .correlate(table)
            .scalar_subquery(),
            table.recorded_at,
        )
        ranked = (
            select(
                table.id,
                asserted_at.label("asserted_at"),
                func.row_number()
                .over(partition_by=table.predicate, order_by=(asserted_at.desc(), table.id))
                .label("position"),
            )
            .where(
                _visible(table, scope),
                table.subject == "user",
                table.predicate.in_([MemoryPredicate(p) for p in PROFILE_TOPICS]),
                table.validity_state == MemoryValidityState.ACTIVE,
                or_(table.valid_until.is_(None), table.valid_until > now_ts()),
            )
            .subquery()
        )
        async with self.sessions() as session:
            rows = await session.execute(
                select(table, ranked.c.asserted_at)
                .join(ranked, table.id == ranked.c.id)
                .where(ranked.c.position == 1)
                .order_by(ranked.c.asserted_at.desc(), table.id)
                .limit(limit)
            )
            return [
                replace(record(row, MemoryType.SEMANTIC), asserted_at=timestamp) for row, timestamp in rows
            ]

    async def assertion_time(self, scope: MemoryScope, source: str) -> int | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(ChatMessage.created_at)
                .where(
                    ChatMessage.conversation_id == scope.session_id,
                    ChatMessage.role == "user",
                    ChatMessage.content == source,
                )
                .order_by(ChatMessage.sequence.desc())
                .limit(1)
            )

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
        review_required: bool = False,
    ) -> list[MemoryRecord]:
        require_ordinary(EgressChannel.MEMORY, candidates)
        results = []
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = None
            source = None
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
            profile = await session.get(UserProfile, (scope.workspace_id, scope.user_id))
            if profile is not None and replace_id is not None:
                target, _ = await self._find(session, scope, replace_id)
                if any(source.get("source_kind") == "user_settings" for source in target.provenance):
                    raise RuntimeFailure("Edit this Profile field in General settings.", status=409)
            for candidate in candidates:
                if (
                    profile is not None
                    and candidate.kind == MemoryType.SEMANTIC
                    and candidate.subject == "user"
                    and candidate.predicate in PROFILE_FIELDS
                ):
                    if replace_id is not None or provenance.source_kind != "user_message":
                        raise RuntimeFailure("Edit this Profile field in General settings.", status=409)
                    continue
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
                            table.validity_state.in_(("active", "candidate", "deleted", "rejected")),
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
                        queue_index_intent(session, existing, candidate.kind, "delete")
                rows = [
                    r
                    for r in rows
                    if r.validity_state
                    in (
                        MemoryValidityState.ACTIVE,
                        MemoryValidityState.CANDIDATE,
                        MemoryValidityState.DELETED,
                        MemoryValidityState.REJECTED,
                    )
                ]
                duplicate = next((r for r in rows if same_fact(record(r, candidate.kind), candidate)), None)
                if duplicate is not None and duplicate.validity_state in (
                    MemoryValidityState.DELETED,
                    MemoryValidityState.REJECTED,
                ):
                    continue
                if candidate.merge_with:
                    duplicate = next((r for r in rows if r.id == candidate.merge_with), None)
                    if duplicate is None or duplicate.validity_state != MemoryValidityState.ACTIVE:
                        raise ExtractionCancelled()
                asserted_at = (
                    source.created_at
                    if source is not None
                    else operation.queued_at
                    if operation is not None
                    else provenance.asserted_at
                    if provenance.asserted_at is not None
                    else now_ts()
                )
                evidence = asdict(replace(provenance, evidence=candidate.evidence, asserted_at=asserted_at))
                if duplicate is not None and replace_id is None:
                    if duplicate.validity_state in (
                        MemoryValidityState.ACTIVE,
                        MemoryValidityState.CANDIDATE,
                    ):
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
                    "validity_state": (
                        MemoryValidityState.CANDIDATE if review_required else MemoryValidityState.ACTIVE
                    ),
                    "review_data": {"replaces": candidate.replaces, "generation": generation},
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
                        **common,
                        summary=candidate.text,
                        occurred_at=candidate.occurred_at if candidate.occurred_at is not None else now_ts(),
                        meta={
                            "event_time_source": "explicit"
                            if candidate.occurred_at is not None
                            else "recorded",
                            "event_time_precision": candidate.time_precision
                            if candidate.occurred_at is not None
                            else "unknown",
                        },
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
                    if (
                        not review_required
                        and previous.validity_state == MemoryValidityState.ACTIVE
                        and (
                            previous.id == replace_id
                            or conflicts(record(previous, candidate.kind), candidate)
                        )
                    ):
                        previous.validity_state = MemoryValidityState.SUPERSEDED
                        previous.invalidated_at = now_ts()
                        previous.supersede_reason = "Explicit user correction"
                        queue_index_intent(session, previous, candidate.kind, "delete")
                        replaced.append(previous)
                if replace_id is not None and not any(r.id == replace_id for r in replaced):
                    raise RuntimeFailure("只能更新同一 Scope 中的 active Memory。", status=409)
                await session.flush()
                session.add(row)
                await session.flush()
                for previous in replaced:
                    previous.superseded_by = row.id
                if not review_required:
                    queue_index_intent(session, row, candidate.kind, "upsert")
                results.append(record(row, candidate.kind))
            if operation is not None:
                operation.state, operation.finished_at = "succeeded", now_ts()
                operation.result = {
                    "committed": True,
                    "memories": len(results),
                    "candidates": sum(r.state == MemoryState.CANDIDATE for r in results),
                }
                operation.error = None
            await session.commit()
        return results

    async def review(
        self, scope: MemoryScope, identifier: str, decision: str, *, operation_id: str, generation: int
    ) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = await session.get(Operation, operation_id)
            epoch = await session.scalar(
                select(MemoryEpoch).where(
                    MemoryEpoch.workspace_id == scope.workspace_id, MemoryEpoch.user_id == scope.user_id
                )
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
            row, kind = await self._find(session, scope, identifier)
            if (
                isinstance(row, MemoryBelief)
                and row.subject == "user"
                and row.predicate in PROFILE_FIELDS
                and await session.get(UserProfile, (scope.workspace_id, scope.user_id)) is not None
            ):
                raise RuntimeFailure("Edit this Profile field in General settings.", status=409)
            if row.validity_state not in (MemoryValidityState.ACTIVE, MemoryValidityState.CANDIDATE):
                raise RuntimeFailure("只能审核 candidate 或 active Memory。", status=409)
            if decision == "approve":
                if row.validity_state != MemoryValidityState.CANDIDATE:
                    raise RuntimeFailure("只能批准 candidate Memory。", status=409)
                if row.review_data.get("generation") not in (None, generation):
                    raise ExtractionCancelled()
                if row.valid_until is not None and row.valid_until <= now_ts():
                    raise RuntimeFailure("Memory 候选已过期。", status=409)
                for source in row.provenance:
                    if source.get("source_kind") == "user_message":
                        message = await session.get(ChatMessage, source.get("message_id"))
                        if (
                            message is None
                            or message.role != "user"
                            or message.conversation_id != source.get("conversation_id")
                            or not source.get("evidence")
                            or str(source["evidence"]) not in message.content
                        ):
                            raise ExtractionCancelled()
                current = record(row, kind)
                candidate = MemoryCandidate(
                    current.text,
                    kind=kind,
                    subject=current.subject,
                    predicate=current.predicate,
                    object=current.object,
                    topic=current.topic,
                    occurred_at=current.occurred_at,
                    replaces=bool(row.review_data.get("replaces")),
                )
                model = _TABLES[kind]
                previous_rows = await session.scalars(
                    select(model).where(
                        model.id != row.id,
                        model.workspace_id == row.workspace_id,
                        model.user_id == row.user_id,
                        model.scope_kind == row.scope_kind,
                        model.agent_id == row.agent_id,
                        model.session_id == row.session_id,
                        model.validity_state.in_(("active", "deleted", "rejected")),
                    )
                )
                for previous in previous_rows:
                    prior = record(previous, kind)
                    if same_fact(prior, candidate):
                        raise RuntimeFailure("等价记忆已存在或已被忘记/拒绝,请重新检查候选。", status=409)
                    if previous.validity_state == MemoryValidityState.ACTIVE and conflicts(prior, candidate):
                        old_time = previous.provenance[-1].get("asserted_at") if previous.provenance else None
                        new_time = row.provenance[-1].get("asserted_at") if row.provenance else None
                        if isinstance(old_time, int) and isinstance(new_time, int) and old_time > new_time:
                            raise RuntimeFailure("候选纠正早于当前事实,请重新确认最新事实。", status=409)
                        previous.validity_state = MemoryValidityState.SUPERSEDED
                        previous.invalidated_at = now_ts()
                        previous.superseded_by = row.id
                        previous.supersede_reason = "Human review of explicit correction"
                        queue_index_intent(session, previous, kind, "delete")
                row.validity_state = MemoryValidityState.ACTIVE
                queue_index_intent(session, row, kind, "upsert")
            elif decision == "reject":
                row.validity_state = MemoryValidityState.REJECTED
                row.invalidated_at = now_ts()
                queue_index_intent(session, row, kind, "delete")
            else:
                raise ValueError("Unsupported Memory review decision.")
            row.review_data = {**row.review_data, "decision": decision, "reviewed_at": now_ts()}
            operation.state, operation.finished_at = "succeeded", now_ts()
            operation.result = {"committed": True, "decision": decision, "memory_id": identifier}
            operation.error = None
            await session.commit()

    async def transition(
        self, scope: MemoryScope, identifier: str, state: MemoryState, *, hard: bool = False
    ) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row, kind = await self._find(session, scope, identifier)
            if any(source.get("source_kind") == "user_settings" for source in row.provenance):
                raise RuntimeFailure("Edit this Profile field in General settings.", status=409)
            if hard and state != MemoryState.DELETED:
                raise ValueError("Hard removal requires deleted state.")
            if state not in (MemoryState.DELETED, MemoryState.EXPIRED, MemoryState.REJECTED):
                raise ValueError("Use consolidation for activation and supersession.")
            row.validity_state = MemoryValidityState(state.value)
            row.invalidated_at = now_ts()
            queue_index_intent(session, row, kind, "delete")
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
                    queue_index_intent(session, row, kind, "delete")
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
                        queue_index_intent(session, row, kind, "upsert")
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
