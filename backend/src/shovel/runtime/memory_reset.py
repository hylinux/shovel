"""Explicit, owner-scoped Memory erasure on the existing durable worker."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update

from shovel.db.base import now_ts
from shovel.db.model.memory import (
    MemoryExperience,
    MemoryIndexOutbox,
    MemoryIndexState,
    MemoryLink,
    MemoryRevision,
    MemoryWorking,
    SessionSummary,
)
from shovel.db.model.runtime import ChatMessage, MemoryEpoch, Operation
from shovel.db.model.user_profile import UserProfile
from shovel.db.unit_of_work import sqlite_write
from shovel.memory.domain import ExtractionCancelled
from shovel.memory.sqlite_repository import _TABLES
from shovel.memory.zvec_index import memory_index_root, remove_memory_partition
from shovel.runtime.errors import RuntimeFailure

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


class MemoryResetService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._confirmations: dict[str, tuple[int, int]] = {}

    def _owned(self, table: Any) -> Any:
        owner = self.runtime.memory.owner
        return (table.workspace_id == owner.workspace_id) & (table.user_id == owner.user_id)

    async def prepare(self) -> dict[str, Any]:
        from shovel.runtime.client import workspace_id

        now = now_ts()
        self._confirmations = {key: value for key, value in self._confirmations.items() if value[1] > now}
        if len(self._confirmations) >= 64:
            raise RuntimeFailure("Too many pending Memory confirmations.", status=429)
        async with self.runtime.sessions() as session:
            generation = await self.runtime.memory_tasks.generation(session)
            models: list[Any] = [*_TABLES.values(), MemoryWorking, SessionSummary, MemoryExperience]
            counts = {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model).where(self._owned(model)))
                    or 0
                )
                for model in models
            }
        token = secrets.token_urlsafe(24)
        self._confirmations[token] = generation, now + 300
        return {
            "confirmation_id": token,
            "expires_at": now + 300,
            "confirmation_text": "DELETE ALL MEMORY",
            "workspace_id": workspace_id(self.runtime.settings),
            "counts": counts,
        }

    async def confirm(self, token: str, phrase: str, request_key: str | None) -> Operation:
        intent = {"confirmation_id": token}
        replay = await self.runtime.memory_tasks.replay("memory.purge", intent, request_key)
        if replay is not None:
            return replay
        if phrase != "DELETE ALL MEMORY":
            raise RuntimeFailure("Type DELETE ALL MEMORY to confirm erasure.", status=422)
        receipt = self._confirmations.pop(token, None)
        async with self.runtime.sessions() as session:
            generation = await self.runtime.memory_tasks.generation(session)
        if receipt is None or receipt[1] <= now_ts() or receipt[0] != generation:
            raise RuntimeFailure("Memory confirmation expired or changed. Review again.", status=409)
        operation = await self.runtime.operations.enqueue(
            "memory.purge",
            {
                **intent,
                "workspace_id": self.runtime.memory.owner.workspace_id,
                "user_id": self.runtime.memory.owner.user_id,
                "generation": generation,
            },
            request_key=request_key,
        )
        self.runtime.memory_wake.set()
        return operation

    async def run(self, identifier: str) -> None:
        from shovel.runtime.client import workspace_id

        memory = self.runtime.memory
        async with memory._maintenance_lock, sqlite_write(self.runtime.sessions) as session:
            operation = await session.get(Operation, identifier)
            epoch = await session.scalar(select(MemoryEpoch).where(self._owned(MemoryEpoch)))
            if (
                operation is None
                or operation.kind != "memory.purge"
                or operation.state != "running"
                or not operation.payload.get("confirmation_id")
                or epoch is None
                or epoch.generation != operation.payload["generation"]
                or operation.payload["workspace_id"] != memory.owner.workspace_id
                or operation.payload["user_id"] != memory.owner.user_id
            ):
                raise ExtractionCancelled()
            # Keep SQLite authoritative on filesystem failure. No model or remote I/O under this reservation.
            if memory.index is not None:
                await memory.index.purge()
            else:
                await asyncio.to_thread(
                    remove_memory_partition, memory_index_root(memory.settings.memory, memory.owner)
                )
            linked_models: list[Any] = [*_TABLES.values(), MemoryWorking, MemoryExperience]
            for model in linked_models:
                ids = select(model.id).where(self._owned(model))
                await session.execute(delete(MemoryLink).where(MemoryLink.memory_id.in_(ids)))
                await session.execute(delete(MemoryRevision).where(MemoryRevision.memory_id.in_(ids)))
            models: list[Any] = [
                *_TABLES.values(),
                MemoryWorking,
                SessionSummary,
                MemoryExperience,
                MemoryIndexOutbox,
                MemoryIndexState,
            ]
            for model in models:
                await session.execute(delete(model).where(self._owned(model)))
            profile = await session.get(UserProfile, (memory.owner.workspace_id, memory.owner.user_id))
            if profile is not None:
                profile.nickname = profile.gender = profile.occupation = profile.bio = ""
                profile.revision += 1
                profile.updated_at = now_ts()
            old_operations = (
                self._owned_payload(),
                Operation.kind.like("memory.%"),
                Operation.id != identifier,
                Operation.capability_version_id.is_(None),
                Operation.execution_data.is_(None),
            )
            await session.execute(
                update(ChatMessage)
                .where(ChatMessage.memory_operation_id.in_(select(Operation.id).where(*old_operations)))
                .values(memory_operation_id=None)
            )
            # Retain only non-content replay fences so lost old requests cannot recreate erased facts.
            await session.execute(
                update(Operation)
                .where(*old_operations)
                .values(
                    payload={"workspace_id": memory.owner.workspace_id, "user_id": memory.owner.user_id},
                    result={"purged": True},
                    request_key=None,
                    request_fingerprint=None,
                    error=None,
                    state="cancelled",
                    finished_at=now_ts(),
                )
            )
            epoch.generation += 1
            operation.state, operation.finished_at = "succeeded", now_ts()
            operation.result = {
                "committed": True,
                "purged": True,
                "workspace_id": workspace_id(self.runtime.settings),
            }
            operation.error = None
            await session.commit()
        memory._index_error = None
        self._confirmations.clear()

    def _owned_payload(self) -> Any:
        owner = self.runtime.memory.owner
        return (func.json_extract(Operation.payload, "$.workspace_id") == owner.workspace_id) & (
            func.json_extract(Operation.payload, "$.user_id") == owner.user_id
        )
