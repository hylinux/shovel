"""Durable, idempotent deletion of knowledge vectors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.base import now_ts
from shovel.db.model.retrieval import DeletionOutbox
from shovel.domain.enums import DeletionScope
from shovel.exceptions.pipeline import VectorSinkError
from shovel.protection.persistence import ENTERPRISE_COLLECTION, EnterpriseVectorDeletion
from shovel.runtime.diagnostics import trace

if TYPE_CHECKING:
    from shovel.pipeline.sink import ChunkVectorSink, EnterpriseVectorDeletionSink


class IndexCleanupService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        sink: ChunkVectorSink | None = None,
        *,
        enterprise_sink: EnterpriseVectorDeletionSink | None = None,
    ) -> None:
        self._sessions = sessions
        self._sink = sink
        self._enterprise_sink = enterprise_sink
        if sink is None and enterprise_sink is None:
            raise ValueError("Index cleanup requires an explicitly configured sink.")

    async def drain(self, *, resource_id: str | None = None) -> int:
        """Retry persisted deletions; acknowledge only after the sink flushes."""
        completed = 0
        trace("cleanup.started", resource_id=resource_id)
        routes = []
        if self._sink is not None:
            routes.append(
                and_(
                    DeletionOutbox.scope.in_((DeletionScope.DOC, DeletionScope.RESOURCE)),
                    DeletionOutbox.collection.is_(None),
                )
            )
        if self._enterprise_sink is not None and resource_id is None:
            routes.append(
                and_(
                    DeletionOutbox.scope == DeletionScope.CHUNK_IDS,
                    DeletionOutbox.collection == ENTERPRISE_COLLECTION,
                )
            )
        if not routes:
            raise ValueError("No cleanup sink is configured for the requested resource scope.")
        while True:
            stmt = (
                select(DeletionOutbox)
                .where(
                    DeletionOutbox.done_at.is_(None),
                    or_(*routes),
                )
                .order_by(DeletionOutbox.id)
                .limit(1)
            )
            if resource_id is not None:
                stmt = stmt.where(DeletionOutbox.resource_id == resource_id)

            async with self._sessions() as session:
                intent = (await session.execute(stmt)).scalar_one_or_none()
                if intent is None:
                    trace("cleanup.completed", resource_id=resource_id, completed=completed)
                    return completed
                intent.attempts += 1
                await session.commit()

                trace(
                    "cleanup.deleting",
                    deletion_id=intent.id,
                    resource_id=intent.resource_id,
                    scope=intent.scope.value,
                    attempt=intent.attempts,
                )
                try:
                    if intent.collection == ENTERPRISE_COLLECTION:
                        if self._enterprise_sink is None:
                            raise VectorSinkError("Enterprise vector deletion sink is unavailable.")
                        deletion = EnterpriseVectorDeletion.model_validate(intent.filter)
                        await self._enterprise_sink.delete_ids(tuple(str(item) for item in deletion.ids))
                    else:
                        sink = self._sink
                        if sink is None:
                            raise VectorSinkError("Knowledge vector deletion sink is unavailable.")
                        if intent.resource_id is None:
                            raise VectorSinkError(f"Deletion {intent.id} has no resource_id.")
                        if intent.scope is DeletionScope.RESOURCE:
                            await sink.delete_resource(intent.resource_id)
                        elif intent.doc_uri is not None:
                            await sink.delete_document(intent.resource_id, intent.doc_uri)
                        else:
                            raise VectorSinkError(f"Deletion {intent.id} has no doc_uri.")
                except Exception as exc:
                    enterprise = intent.collection == ENTERPRISE_COLLECTION
                    if enterprise:
                        intent.last_error = "enterprise_vector_delete_failed"
                    elif isinstance(exc, VectorSinkError):
                        intent.last_error = exc.details or exc.message
                    else:
                        raise
                    await session.commit()
                    trace("cleanup.failed", deletion_id=intent.id, error_type=type(exc).__name__)
                    if enterprise:
                        raise VectorSinkError("Enterprise vector deletion failed; intent retained.") from None
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
