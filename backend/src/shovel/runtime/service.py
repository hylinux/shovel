from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shovel.config.settings import AppSettings
from shovel.db.migrations import upgrade_database
from shovel.exceptions.pipeline import VectorSinkError
from shovel.pipeline.sink import ZvecChunkSink, open_chunk_sink
from shovel.runtime.locking import RuntimeLock
from shovel.services.resource_service import ResourceService

if TYPE_CHECKING:
    from shovel.runtime.chat import ChatService
    from shovel.runtime.memory import MemoryService
    from shovel.runtime.operations import OperationService
    from shovel.runtime.retrieval import RetrievalService


class Runtime:
    def __init__(
        self,
        settings: AppSettings,
        sessions: async_sessionmaker[AsyncSession],
        resources: ResourceService,
    ) -> None:
        from shovel.runtime.chat import ChatService
        from shovel.runtime.memory import MemoryService
        from shovel.runtime.operations import OperationService
        from shovel.runtime.retrieval import RetrievalService

        self.settings = settings
        self.sessions = sessions
        self.resources = resources
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self._sink: ZvecChunkSink | None = None
        self._sink_lock = asyncio.Lock()
        self.operations: OperationService = OperationService(self)
        self.retrieval: RetrievalService = RetrievalService(self)
        self.memory: MemoryService = MemoryService(settings)
        self.chat: ChatService = ChatService(self)

    def redact(self, message: str) -> str:
        keys = (
            self.settings.model.security_key,
            self.settings.embedding.api_key.get_secret_value(),
            self.settings.memory.llm.api_key,
            self.settings.memory.embedder.api_key,
            self.settings.memory.qdrant.api_key,
        )
        for key in keys:
            if key:
                message = message.replace(key, "[redacted]")
        return message[:4000]

    async def sink(self) -> ZvecChunkSink:
        async with self._sink_lock:
            if self._sink is None:
                task = asyncio.create_task(
                    asyncio.to_thread(open_chunk_sink, self.settings.zvec.knowledge_path)
                )
                try:
                    self._sink = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self._sink = await task
                    raise
                except FileNotFoundError as exc:
                    raise VectorSinkError(
                        "知识库向量集合不存在,清理意图仍保留。",
                        hint="运行 shovel init 初始化向量集合;不配置模型也可以使用资源管理和关键词检索。",
                    ) from exc
            return self._sink

    async def close(self) -> None:
        try:
            await self.chat.close()
        finally:
            try:
                await self.memory.close()
            finally:
                if self._sink is not None:
                    await self._sink.aclose()
                    self._sink = None


async def create_runtime(
    settings: AppSettings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    resources: ResourceService,
) -> AsyncGenerator[Runtime]:
    with RuntimeLock(settings):
        await asyncio.to_thread(_prepare_database_path, str(engine.url.database or ""))
        await upgrade_database(engine)
        runtime = Runtime(settings, sessions, resources)
        try:
            await runtime.operations.recover()
            await runtime.chat.recover()
            yield runtime
        finally:
            await runtime.close()


def _prepare_database_path(database_url_path: str) -> None:
    if database_url_path and database_url_path != ":memory:":
        Path(database_url_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
