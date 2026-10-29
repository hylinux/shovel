"""Composition and runtime ownership of the native memory application."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from time import perf_counter
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.config.settings import AppSettings
from shovel.db.base import now_ts
from shovel.db.model.memory import MemoryIndexOutbox, MemoryIndexState, SessionSummary
from shovel.db.model.runtime import ChatMessage, Conversation, MemoryEpoch, Operation
from shovel.exceptions.pipeline import EmbeddingError, VectorSinkError
from shovel.memory.agent_framework import AgentFrameworkMemoryExtractor
from shovel.memory.domain import (
    ExtractionCancelled,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryState,
    Provenance,
    ScopeKind,
)
from shovel.memory.service import MemoryService as NativeMemoryService
from shovel.memory.service import view
from shovel.memory.sqlite_repository import SQLiteMemoryRepository
from shovel.memory.zvec_index import ZvecMemoryIndex
from shovel.pipeline.embedder import build_embedder
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import NotFound, RuntimeFailure

__all__ = ["ExtractionCancelled", "MemoryService"]
_INDEX_ERRORS = (EmbeddingError, VectorSinkError, OSError, RuntimeError, ValueError)


class MemoryService:
    def __init__(self, settings: AppSettings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.settings, self.sessions = settings, sessions
        workspace = hashlib.sha256(str(settings.agent.workspace.resolve()).encode()).hexdigest()[:24]
        self.owner = MemoryScope(workspace)
        self.repository = SQLiteMemoryRepository(sessions)
        model = settings.memory.llm if settings.memory.llm.is_configured else settings.model
        self.extractor = AgentFrameworkMemoryExtractor(model)
        self.index = (
            ZvecMemoryIndex(
                settings.memory,
                self.owner,
                self.repository,
                build_embedder(settings.memory.embedder, expected_dimension=settings.memory.dim),
                cache_entries=settings.performance.embedding_cache_entries,
                cache_seconds=settings.performance.embedding_cache_seconds,
            )
            if settings.memory.embedder.is_configured
            else None
        )
        self.application = NativeMemoryService(
            self.repository,
            self.index,
            self.extractor,
            self.owner,
            min_similarity=settings.memory.min_similarity,
            min_confidence=settings.memory.min_confidence,
            max_chars=settings.memory.context_max_chars,
            max_items=settings.memory.max_items,
        )
        self._active: asyncio.Task[Any] | None = None
        self._index_error: str | None = None
        self._maintenance_lock = asyncio.Lock()
        self._log = structlog.get_logger("shovel.memory")

    @property
    def configured(self) -> bool:
        return True

    async def initialize(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            previous = await session.scalar(
                select(MemoryEpoch).where(
                    MemoryEpoch.workspace_id == self.owner.workspace_id,
                    MemoryEpoch.user_id == self.owner.user_id,
                )
            )
            if previous is None:
                session.add(
                    MemoryEpoch(
                        workspace_id=self.owner.workspace_id, user_id=self.owner.user_id, generation=0
                    )
                )
            await session.commit()

    async def warnings(self) -> list[str]:
        state = await self.status()
        if self.index is None:
            return ["Memory 已保存于 SQLite;未配置 [memory.embedder],当前不提供语义回忆。"]
        if state["index_state"] != "ready":
            return ["Memory 语义索引尚未就绪或正在恢复;SQLite 中的记忆不受影响。"]
        return []

    async def status(self) -> dict[str, Any]:
        async with self.sessions() as session:
            state = await session.get(MemoryIndexState, (self.owner.workspace_id, self.owner.user_id))
            pending = await session.scalar(
                select(func.count())
                .select_from(MemoryIndexOutbox)
                .where(
                    MemoryIndexOutbox.workspace_id == self.owner.workspace_id,
                    MemoryIndexOutbox.user_id == self.owner.user_id,
                    MemoryIndexOutbox.done_at.is_(None),
                )
            )
        return {
            "provider": "native",
            "source_of_truth": "sqlite",
            "index_state": "disabled"
            if self.index is None
            else (
                "failed"
                if self._index_error or (state and state.last_error)
                else "pending"
                if pending
                else state.state
                if state
                else "pending"
            ),
            "pending_index_operations": int(pending or 0),
            "last_error": self._index_error or (state.last_error if state else None),
        }

    async def search(self, query: str, *, conversation_id: str = "") -> list[dict[str, Any]]:
        started = perf_counter()
        try:
            result = await self.application.retrieve(query, conversation_id)
            self._index_error = None
            trace(
                "memory.recall.completed",
                count=len(result),
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )
            return result
        except _INDEX_ERRORS as exc:
            self._index_error = type(exc).__name__
            self._log.warning("memory.retrieval.unavailable", error_type=self._index_error)
            return []

    async def list(
        self,
        limit: int = 100,
        *,
        include_history: bool = False,
        conversation_id: str = "",
    ) -> list[dict[str, Any]]:
        states = tuple(MemoryState) if include_history else (MemoryState.ACTIVE,)
        scope = replace(self.owner, session_id=conversation_id)
        return [view(r) for r in await self.repository.list_records(scope, limit=limit, states=states)]

    async def owned(self, identifier: str, *, conversation_id: str = "") -> MemoryRecord:
        return await self.repository.get(replace(self.owner, session_id=conversation_id), identifier)

    async def delete(self, identifier: str, *, hard: bool = False, conversation_id: str = "") -> None:
        await self.repository.transition(
            replace(self.owner, session_id=conversation_id), identifier, MemoryState.DELETED, hard=hard
        )

    async def forget_conversation(self, identifier: str) -> None:
        scope = replace(self.owner, session_id=identifier)
        for row in await self.repository.list_records(scope, limit=10000, states=tuple(MemoryState)):
            if row.scope.kind == ScopeKind.SESSION:
                await self.repository.transition(scope, row.id, MemoryState.DELETED)

    async def write(self, operation: Operation) -> None:
        from shovel.memory.domain import _SECRET, MemoryType
        from shovel.runtime.schemas import MemoryInput

        payload = MemoryInput.model_validate(operation.payload["memory"])
        if _SECRET.search(payload.text + payload.object + payload.topic):
            raise RuntimeFailure("不能将凭据保存到 Memory。")
        scope = replace(
            self.owner,
            kind=ScopeKind(payload.scope),
            session_id=payload.session_id,
        )
        if scope.kind == ScopeKind.SESSION:
            async with self.sessions() as session:
                if await session.get(Conversation, scope.session_id) is None:
                    raise NotFound("对话", scope.session_id)
        candidate = MemoryCandidate(
            text=payload.text,
            kind=MemoryType(payload.kind),
            subject=payload.subject,
            predicate=payload.predicate,
            object=payload.object,
            topic=payload.topic,
            confidence=1,
            importance=payload.importance,
            expires_at=payload.expires_at,
            occurred_at=payload.occurred_at,
        )
        await self.repository.consolidate(
            scope,
            [candidate],
            Provenance("manual"),
            operation_id=operation.id,
            generation=operation.payload["generation"],
            replace_id=operation.payload.get("replace_id"),
        )

    def cancel_extraction(self) -> None:
        if self._active is not None:
            self._active.cancel()

    async def extract(
        self,
        source: str,
        *,
        operation_id: str,
        generation: int,
        message_id: str,
        conversation_id: str,
    ) -> None:
        self._active = asyncio.current_task()
        try:
            try:
                related = await self.application.related(source)
            except _INDEX_ERRORS as exc:
                related = []
                self._index_error = type(exc).__name__
                self._log.warning("memory.consolidation.index_unavailable", error_type=self._index_error)
            await self.application.extract(
                source,
                Provenance("user_message", conversation_id, message_id),
                operation_id=operation_id,
                generation=generation,
                related=related,
            )
        finally:
            self._active = None

    async def warmup(self) -> None:
        if self.index is not None:
            await self.index.embedder.embed(["Shovel memory warmup"])

    async def maintain(self) -> bool:
        expired = await self.repository.expire_due(self.owner)
        if self.index is None:
            return bool(expired)
        async with self._maintenance_lock:
            try:
                await self.index.initialize()
                intents = await self.repository.pending(self.owner)
                for intent in intents:
                    try:
                        # Index maintenance may cover all agents in this owned workspace partition.
                        from shovel.memory.sqlite_repository import _TABLES, record

                        async with self.sessions() as session:
                            row = await session.get(_TABLES[intent.kind], intent.memory_id)
                            item = (
                                record(row, intent.kind)
                                if (
                                    row is not None
                                    and row.workspace_id == self.owner.workspace_id
                                    and row.user_id == self.owner.user_id
                                )
                                else None
                            )
                        if (
                            intent.action == "upsert"
                            and item is not None
                            and item.revision == intent.revision
                            and item.state == MemoryState.ACTIVE
                            and (item.expires_at is None or item.expires_at > now_ts())
                        ):
                            await self.index.upsert(item)
                        else:
                            await self.index.delete(intent.memory_id, intent.kind, intent.revision)
                        await self.repository.acknowledge(intent, self.index.model_version)
                    except _INDEX_ERRORS as exc:
                        self._index_error = type(exc).__name__
                        await self.repository.fail(intent, self._index_error)
                        self._log.warning("memory.index.retry", error_type=self._index_error)
                if not await self.repository.pending(self.owner, force=True):
                    await self.index.finish_rebuild()
                    self._index_error = None
                return bool(intents)
            except _INDEX_ERRORS as exc:
                self._index_error = type(exc).__name__
                self._log.error("memory.index.unavailable", error_type=self._index_error)
                return False

    async def rebuild(self) -> None:
        if self.index is None:
            raise RuntimeFailure("重建 Memory 索引需要配置 [memory.embedder]。", status=503)
        async with self._maintenance_lock:
            await self.index.start_rebuild()
        while await self.maintain():
            pass
        pending = await self.repository.pending(self.owner, force=True)
        if pending or self._index_error:
            raise RuntimeFailure("Memory 索引重建尚未完成,待处理项已保留供后台重试。", status=503)

    async def summary(self, conversation_id: str) -> str:
        async with self.sessions() as session:
            row = await session.get(SessionSummary, conversation_id)
            if row is None:
                return ""
            if (row.workspace_id, row.user_id, row.agent_id) != (
                self.owner.workspace_id,
                self.owner.user_id,
                self.owner.agent_id,
            ):
                return ""
            return row.content

    async def summarize(self, conversation_id: str, operation_id: str, generation: int) -> None:
        async with self.sessions() as session:
            previous = await session.get(SessionSummary, conversation_id)
            if previous is not None and (previous.workspace_id, previous.user_id, previous.agent_id) != (
                self.owner.workspace_id,
                self.owner.user_id,
                self.owner.agent_id,
            ):
                raise RuntimeFailure("Session Summary 属于其他 Memory Scope。", status=409)
            through = previous.through_sequence if previous else 0
            latest = list(
                await session.scalars(
                    select(ChatMessage.sequence)
                    .where(
                        ChatMessage.conversation_id == conversation_id,
                        ChatMessage.state == "completed",
                    )
                    .order_by(ChatMessage.sequence.desc())
                    .limit(self.settings.memory.history_messages)
                )
            )
            if len(latest) < self.settings.memory.history_messages:
                return
            rows = list(
                await session.scalars(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == conversation_id,
                        ChatMessage.state == "completed",
                        ChatMessage.sequence > through,
                        ChatMessage.sequence < min(latest),
                    )
                    .order_by(ChatMessage.sequence)
                    .limit(40)
                )
            )
            if not rows:
                return
            messages = [{"role": r.role, "content": r.content} for r in rows]
            old_text = previous.content if previous else ""
        self._active = asyncio.current_task()
        try:
            # Keep source text out of summaries when it might contain credentials or quoted instructions.
            from shovel.memory.domain import _SECRET

            messages = [
                {**m, "content": "[sensitive message omitted]"} if _SECRET.search(m["content"]) else m
                for m in messages
            ]
            selected = []
            budget = self.extractor.settings.max_input_chars - len(old_text) - 1500
            for message in messages:
                used = len(json.dumps(message, ensure_ascii=False)) + 2
                if used > budget:
                    break
                selected.append(message)
                budget -= used
            if not selected:
                raise ValueError("A source message exceeds the summarizer input budget.")
            content = await self.extractor.summarize(
                old_text, selected, self.settings.memory.summary_max_chars
            )
            if _SECRET.search(content):
                raise ValueError("Session summary may contain sensitive content.")
            async with self.sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                epoch = await session.scalar(
                    select(MemoryEpoch).where(
                        MemoryEpoch.workspace_id == self.owner.workspace_id,
                        MemoryEpoch.user_id == self.owner.user_id,
                    )
                )
                operation = await session.get(Operation, operation_id)
                current = await session.get(SessionSummary, conversation_id)
                if (
                    epoch is None
                    or epoch.generation != generation
                    or operation is None
                    or operation.state != "running"
                    or await session.get(Conversation, conversation_id) is None
                    or (current.through_sequence if current else 0) != through
                ):
                    raise ExtractionCancelled()
                if current is None:
                    current = SessionSummary(
                        conversation_id=conversation_id,
                        workspace_id=self.owner.workspace_id,
                        user_id=self.owner.user_id,
                        agent_id=self.owner.agent_id,
                        content=content,
                        through_sequence=rows[len(selected) - 1].sequence,
                    )
                    session.add(current)
                else:
                    current.content, current.through_sequence = content, rows[len(selected) - 1].sequence
                    current.updated_at = now_ts()
                more = current.through_sequence < min(latest) - 1
                operation.state = "queued" if more else "succeeded"
                operation.finished_at = None if more else now_ts()
                operation.result = {"through_sequence": current.through_sequence}
                await session.commit()
        finally:
            self._active = None

    async def close(self) -> None:
        self.cancel_extraction()
        try:
            await self.extractor.close()
        finally:
            if self.index is not None:
                await self.index.close()
