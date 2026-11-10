"""Owned candidate receipts and tombstones; no NativeMemory or vector mutations."""

from __future__ import annotations

from pydantic import JsonValue
from sqlalchemy import Select, false, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.base import now_ts
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.model.memory import MemoryExperience
from shovel.db.model.runtime import MemoryEpoch
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.domain import Execution
from shovel.memory.domain import MemoryScope
from shovel.memory.experience import ExperienceReview, source_digest
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import NotFound, RuntimeFailure


class SQLiteExperienceRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], scope: CapabilityScope, memory_scope: MemoryScope
    ) -> None:
        if scope.workspace_id != memory_scope.workspace_id:
            raise ValueError("Experience and Memory stores must belong to the same workspace.")
        self.sessions = sessions
        self.scope, self.memory_scope = scope, memory_scope
        self.sources = SQLiteExecutionRepository(sessions)

    def _query(self, scope: CapabilityScope) -> Select[MemoryExperience]:
        return select(MemoryExperience).where(
            true() if scope == self.scope else false(),
            MemoryExperience.workspace_id == scope.workspace_id,
            MemoryExperience.user_id == scope.user_id,
        )

    async def _generation(self, session: AsyncSession, scope: CapabilityScope) -> int:
        if scope != self.scope:
            raise NotFound("Experience scope", scope.workspace_id)
        generation = await session.scalar(
            select(MemoryEpoch.generation).where(
                # Platform ownership does not rename the legacy Memory epoch owner.
                MemoryEpoch.workspace_id == self.memory_scope.workspace_id,
                MemoryEpoch.user_id == self.memory_scope.user_id,
            )
        )
        if generation is None:
            raise RuntimeFailure("Memory generation is not initialized.", status=503)
        return generation

    async def generation(self, scope: CapabilityScope) -> int:
        async with self.sessions() as session:
            return await self._generation(session, scope)

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> MemoryExperience:
        row = await session.scalar(self._query(scope).where(MemoryExperience.id == identifier))
        if row is None:
            raise NotFound("Experience candidate", identifier)
        return row

    async def _invalid(
        self, session: AsyncSession, scope: CapabilityScope, row: MemoryExperience
    ) -> str | None:
        if row.generation != await self._generation(session, scope):
            return "memory_generation_changed"
        try:
            source = _record(await self.sources._find(session, scope, row.source_execution_id))
        except NotFound:
            return "source_unavailable"
        if source_digest(source) != row.source_digest:
            return "source_changed"
        return None

    async def _view(
        self, session: AsyncSession, scope: CapabilityScope, row: MemoryExperience
    ) -> dict[str, JsonValue]:
        invalid = await self._invalid(session, scope, row) if row.state in {"candidate", "reviewed"} else None
        result: dict[str, JsonValue] = {
            "id": row.id,
            "source_execution_id": row.source_execution_id,
            "state": "invalidated" if invalid else row.state,
            "invalid_reason": invalid,
            "revision": row.revision,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "evidence": row.data,
            "review": row.review_data,
            "recall_eligible": False,
        }
        require_ordinary(EgressChannel.DISPLAY, result)
        return result

    async def capture(
        self,
        scope: CapabilityScope,
        execution: Execution,
        submission_digest: str,
        data: dict[str, JsonValue],
        generation: int,
    ) -> dict[str, JsonValue]:
        require_ordinary(EgressChannel.MEMORY, (execution, data))
        require_ordinary(EgressChannel.PERSISTENCE, data)
        async with sqlite_write(self.sessions) as session:
            current = _record(await self.sources._find(session, scope, execution.id))
            if source_digest(current) != source_digest(execution):
                raise RuntimeFailure("The source changed before experience commit.", status=409)
            row = await session.scalar(
                self._query(scope).where(MemoryExperience.source_execution_id == execution.id)
            )
            if row is not None:
                if row.submission_digest != submission_digest or row.source_digest != source_digest(current):
                    raise RuntimeFailure(
                        "This source already has a different experience receipt.", status=409
                    )
                return await self._view(session, scope, row)
            if generation != await self._generation(session, scope):
                raise RuntimeFailure("Memory generation changed before experience commit.", status=409)
            count = await session.scalar(
                self._query(scope).with_only_columns(func.count()).where(MemoryExperience.state != "deleted")
            )
            if count is not None and count >= 1000:
                raise RuntimeFailure("Experience intake limit reached; forget unused candidates.", status=409)
            row = MemoryExperience(
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
                source_execution_id=execution.id,
                source_digest=source_digest(current),
                submission_digest=submission_digest,
                generation=generation,
                data=data,
            )
            session.add(row)
            await session.flush()
            result = await self._view(session, scope, row)
            await session.commit()
        return result

    async def list(self, scope: CapabilityScope) -> list[dict[str, JsonValue]]:
        require_ordinary(EgressChannel.DISPLAY)
        async with self.sessions() as session:
            rows = await session.scalars(
                self._query(scope)
                .order_by(MemoryExperience.created_at.desc(), MemoryExperience.id)
                .limit(100)
            )
            return [await self._view(session, scope, row) for row in rows]

    async def get(self, scope: CapabilityScope, identifier: str) -> dict[str, JsonValue]:
        require_ordinary(EgressChannel.DISPLAY)
        async with self.sessions() as session:
            return await self._view(session, scope, await self._find(session, scope, identifier))

    async def review(
        self, scope: CapabilityScope, identifier: str, revision: int, body: ExperienceReview
    ) -> dict[str, JsonValue]:
        require_ordinary(EgressChannel.MEMORY, body)
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != revision or row.state not in {"candidate", "reviewed"}:
                raise RuntimeFailure("Experience changed or is no longer reviewable.", status=409)
            if body.decision == "retain":
                if row.state != "candidate" or await self._invalid(session, scope, row):
                    raise RuntimeFailure(
                        "Experience source or Memory generation is no longer current.", status=409
                    )
                row.state = "reviewed"
            else:
                row.state = "rejected"
            row.review_data = {**body.model_dump(mode="json"), "reviewer_id": scope.user_id, "at": now_ts()}
            row.revision += 1
            row.updated_at = now_ts()
            result = await self._view(session, scope, row)
            await session.commit()
        trace("memory.experience.reviewed", candidate_id=identifier, state=result["state"])
        return result

    async def forget(self, scope: CapabilityScope, identifier: str, revision: int) -> dict[str, JsonValue]:
        require_ordinary(EgressChannel.MEMORY)
        async with sqlite_write(self.sessions, durable=True) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != revision or row.state == "deleted":
                raise RuntimeFailure("Experience changed; reload before forgetting.", status=409)
            row.state, row.data, row.review_data = "deleted", {}, {}
            row.revision += 1
            row.updated_at = now_ts()
            result = await self._view(session, scope, row)
            await session.commit()
        trace("memory.experience.forgotten", candidate_id=identifier)
        return result
