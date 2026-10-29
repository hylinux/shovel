"""Owned Zvec handles, versioned generations and atomic rebuild cutover."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text

from shovel.config.memory_config import MemorySettings
from shovel.db.base import memory_vector_id
from shovel.db.model.memory import MemoryIndexOutbox, MemoryIndexState
from shovel.memory.domain import MemoryRecord, MemoryScope, MemoryType, VectorHit
from shovel.memory.sqlite_repository import SQLiteMemoryRepository
from shovel.pipeline.embedder import Embedder
from shovel.pipeline.embedding_cache import EmbeddingCache
from shovel.pipeline.sink import _ensure_ok
from shovel.vector.schema import memory_collection_schema
from shovel.vector.store import ensure_collection, init_zvec_runtime


def _literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ZvecMemoryIndex:
    def __init__(
        self,
        settings: MemorySettings,
        scope: MemoryScope,
        repository: SQLiteMemoryRepository,
        embedder: Embedder,
        *,
        cache_entries: int = 128,
        cache_seconds: float = 300,
    ) -> None:
        self.settings, self.scope, self.repository, self.embedder = settings, scope, repository, embedder
        partition = hashlib.sha256(f"{scope.workspace_id}|{scope.user_id}".encode()).hexdigest()[:24]
        self.root = settings.index_path.expanduser().resolve() / partition
        self._active: Any = None
        self._building: Any = None
        self._lock = asyncio.Lock()
        self._native_lock = threading.RLock()
        self._initialized = False
        self._query_cache = EmbeddingCache(cache_entries, cache_seconds, settings.dim)

    @property
    def model_version(self) -> str:
        return self.embedder.model_version

    async def _io[T](self, action: Callable[[], T]) -> T:
        def run() -> T:
            with self._native_lock:
                return action()

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _open(self, generation: str) -> Any:
        init_zvec_runtime()
        return ensure_collection(
            self.root / generation,
            memory_collection_schema(self.settings.collection, dim=self.settings.dim),
        ).collection

    async def initialize(self, *, fresh_build: bool = False) -> None:
        async with self._lock:
            if self._initialized:
                return
            try:
                await self._initialize_locked(fresh_build=fresh_build)
            finally:
                if not self._initialized:
                    await self._release_collections()

    async def _initialize_locked(self, *, fresh_build: bool) -> None:
        async with self.repository.sessions() as session:
            state = await session.get(MemoryIndexState, (self.scope.workspace_id, self.scope.user_id))
            reindex = fresh_build
            if state is None:
                state = MemoryIndexState(
                    workspace_id=self.scope.workspace_id,
                    user_id=self.scope.user_id,
                    generation=uuid4().hex,
                    model_version=self.model_version,
                    state="building",
                )
                session.add(state)
                reindex = True
            reindex = reindex or state.state == "building"
            if fresh_build:
                state.building_generation = uuid4().hex
                state.building_model_version = self.model_version
                state.state = "building"
            active_path = self.root / state.generation
            active_exists = active_path.is_dir() and any(active_path.iterdir())
            if state.model_version == self.model_version and not fresh_build:
                self._active = await self._io(lambda: self._open(state.generation))
                reindex = reindex or not active_exists
                if not active_exists:
                    state.state = "building"
            if state.model_version != self.model_version and not state.building_generation:
                state.building_generation = uuid4().hex
                state.building_model_version = self.model_version
                state.state = "building"
                reindex = True
            if state.building_generation:
                # A second model change abandons, but never deletes, the old derived generation.
                if state.building_model_version != self.model_version:
                    state.building_generation = uuid4().hex
                    state.building_model_version = self.model_version
                    reindex = True
                building = self.root / state.building_generation
                reindex = reindex or not building.is_dir() or not any(building.iterdir())
                self._building = await self._io(lambda: self._open(state.building_generation or ""))
            await session.commit()
        if reindex:
            await self.repository.reindex(self.scope)
        self._initialized = True

    async def start_rebuild(self) -> None:
        await self.initialize(fresh_build=True)
        async with self._lock:
            async with self.repository.sessions() as session:
                state = await session.get(MemoryIndexState, (self.scope.workspace_id, self.scope.user_id))
                assert state is not None
                if not state.building_generation:
                    state.building_generation = uuid4().hex
                    state.building_model_version = self.model_version
                    state.state = "building"
                    self._building = await self._io(lambda: self._open(state.building_generation or ""))
                    await session.commit()
            await self.repository.reindex(self.scope)

    async def finish_rebuild(self) -> None:
        async with self._lock, self.repository.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            pending = await session.scalar(
                select(func.count())
                .select_from(MemoryIndexOutbox)
                .where(
                    MemoryIndexOutbox.workspace_id == self.scope.workspace_id,
                    MemoryIndexOutbox.user_id == self.scope.user_id,
                    MemoryIndexOutbox.done_at.is_(None),
                )
            )
            if pending:
                return
            state = await session.get(MemoryIndexState, (self.scope.workspace_id, self.scope.user_id))
            if state is None:
                return
            old = None
            if self._building is not None:
                old = self._active
                assert state.building_generation is not None
                state.generation = state.building_generation
                state.model_version = self.model_version
                state.building_generation = state.building_model_version = None
            state.state, state.last_error = "ready", None
            await session.commit()
            if self._building is not None:
                self._active, self._building = self._building, None
            if old is not None:
                await self._io(old.close)

    async def search(self, query: str, scope: MemoryScope, limit: int) -> list[VectorHit]:
        from zvec import Query

        await self.initialize()
        key = self._query_cache.key(query, self.model_version)
        vector = self._query_cache.get(key)
        if vector is None:
            vector = (await self.embedder.embed([query]))[0]
            self._query_cache.put(key, vector)
        expression = (
            f"workspace_id = {_literal(scope.workspace_id)} and user_id = {_literal(scope.user_id)} "
            f"and model_version = {_literal(self.model_version)} and "
            f'(scope_kind in ("user", "workspace") or '
            f'(scope_kind = "agent" and agent_id = {_literal(scope.agent_id)}) or '
            f'(scope_kind = "session" and agent_id = {_literal(scope.agent_id)} '
            f'and session_id = {_literal(scope.session_id)} and session_id != ""))'
        )
        async with self._lock:
            if self._active is None:
                return []
            rows = await self._io(
                lambda: self._active.query(
                    queries=Query(field_name="dense", vector=vector),
                    topk=limit,
                    filter=expression,
                    output_fields=["memory_id", "kind", "revision"],
                )
            )
            # COSINE returns distance, not similarity.
            return [
                VectorHit(
                    str(r.fields["memory_id"]),
                    MemoryType(r.fields["kind"]),
                    int(r.fields["revision"]),
                    max(0.0, min(1.0, 1.0 - float(r.score))),
                )
                for r in rows
            ]

    async def upsert(self, record: MemoryRecord) -> None:
        from zvec import Doc

        await self.initialize()
        vector = (await self.embedder.embed([record.text]))[0]
        scope = record.scope
        doc = Doc(
            id=memory_vector_id(record.kind, record.id, record.revision),
            vectors={"dense": vector},
            fields={
                "memory_id": record.id,
                "kind": record.kind.value,
                "workspace_id": scope.workspace_id,
                "user_id": scope.user_id,
                "scope_kind": scope.kind.value,
                "agent_id": scope.agent_id,
                "session_id": scope.session_id,
                "model_version": self.model_version,
                "revision": record.revision,
            },
        )
        async with self._lock:
            target = self._building if self._building is not None else self._active
            if target is None:
                raise RuntimeError("Memory vector index is not open.")

            def write() -> None:
                _ensure_ok(target.upsert([doc]), "Memory upsert")
                target.flush()

            await self._io(write)

    async def delete(self, identifier: str, kind: str, revision: int) -> None:
        await self.initialize()
        vector_id = memory_vector_id(kind, identifier, revision)
        async with self._lock:
            for target in (self._active, self._building):
                if target is not None:

                    def remove(collection: Any = target) -> None:
                        _ensure_ok(collection.delete([vector_id]), "Memory delete")
                        collection.flush()

                    await self._io(remove)

    async def _release_collections(self) -> None:
        active, building = self._active, self._building
        self._active = self._building = None
        self._initialized = False
        try:
            if active is not None:
                await self._io(active.close)
        finally:
            if building is not None:
                await self._io(building.close)

    async def close(self) -> None:
        async with self._lock:
            try:
                await self._release_collections()
            finally:
                await self.embedder.aclose()
