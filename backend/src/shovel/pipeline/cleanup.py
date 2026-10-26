"""Durable, idempotent deletion of knowledge vectors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.base import now_ts
from shovel.db.model.retrieval import DeletionOutbox
from shovel.domain.enums import DeletionScope
from shovel.exceptions.pipeline import VectorSinkError

if TYPE_CHECKING:
    from shovel.pipeline.sink import ChunkVectorSink


class IndexCleanupService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        sink: ChunkVectorSink,
    ) -> None:
        self._sessions = sessions
        self._sink = sink

    async def drain(self, *, resource_id: str | None = None) -> int:
        """Retry persisted deletions; acknowledge only after the sink flushes."""
        completed = 0
        while True:
            stmt = (
                select(DeletionOutbox)
                .where(
                    DeletionOutbox.done_at.is_(None),
                    DeletionOutbox.scope.in_((DeletionScope.DOC, DeletionScope.RESOURCE)),
                    DeletionOutbox.collection.is_(None),
                )
                .order_by(DeletionOutbox.id)
                .limit(1)
            )
            if resource_id is not None:
                stmt = stmt.where(DeletionOutbox.resource_id == resource_id)

            async with self._sessions() as session:
                intent = (await session.execute(stmt)).scalar_one_or_none()
                if intent is None:
                    return completed
                intent.attempts += 1
                await session.commit()

                try:
                    if intent.resource_id is None:
                        raise VectorSinkError(f"Deletion {intent.id} has no resource_id.")
                    if intent.scope is DeletionScope.RESOURCE:
                        await self._sink.delete_resource(intent.resource_id)
                    elif intent.doc_uri is not None:
                        await self._sink.delete_document(intent.resource_id, intent.doc_uri)
                    else:
                        raise VectorSinkError(f"Deletion {intent.id} has no doc_uri.")
                except VectorSinkError as exc:
                    intent.last_error = exc.details or exc.message
                    await session.commit()
                    raise

                intent.done_at = now_ts()
                intent.last_error = None
                await session.commit()
                completed += 1


async def cleanup_index(
    database_url: str,
    knowledge_path: str,
    *,
    resource_id: str | None = None,
    echo: bool = False,
) -> int:
    """CLI entry point, including cleanup for resources already removed."""
    from shovel.db.database import create_async_database_engine, create_async_session_factory
    from shovel.pipeline.sink import open_chunk_sink

    engine_ctx = create_async_database_engine(database_url, echo=echo)
    engine = await anext(engine_ctx)
    try:
        try:
            sink = open_chunk_sink(knowledge_path)
        except FileNotFoundError as exc:
            raise VectorSinkError(
                "知识库向量集合不存在, 清理意图仍保留在数据库中。",
                hint="确认 knowledge_path 配置, 初始化集合后运行 `shovel scan cleanup` 重试。",
            ) from exc
        try:
            return await IndexCleanupService(create_async_session_factory(engine), sink).drain(
                resource_id=resource_id,
            )
        finally:
            await sink.aclose()
    finally:
        await engine_ctx.aclose()
