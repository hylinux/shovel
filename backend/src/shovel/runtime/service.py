from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shovel.config.settings import AppSettings
from shovel.db.migrations import upgrade_database
from shovel.exceptions.pipeline import VectorSinkError
from shovel.pipeline.sink import ZvecChunkSink, open_chunk_sink
from shovel.runtime.diagnostics import trace
from shovel.runtime.locking import RuntimeLock
from shovel.services.resource_service import ResourceService

if TYPE_CHECKING:
    from shovel.runtime.chat import ChatService
    from shovel.runtime.mcp_config import MCPConfigurationService
    from shovel.runtime.memory import MemoryService
    from shovel.runtime.memory_tasks import MemoryTaskService
    from shovel.runtime.operations import OperationService
    from shovel.runtime.performance import RuntimePerformance, RuntimeWarmup
    from shovel.runtime.retrieval import RetrievalService


class Runtime:
    def __init__(
        self,
        settings: AppSettings,
        sessions: async_sessionmaker[AsyncSession],
        resources: ResourceService,
    ) -> None:
        from shovel.runtime.chat import ChatService
        from shovel.runtime.mcp_config import MCPConfigurationService
        from shovel.runtime.memory import MemoryService
        from shovel.runtime.memory_tasks import MemoryTaskService
        from shovel.runtime.operations import OperationService
        from shovel.runtime.performance import RuntimePerformance, RuntimeWarmup
        from shovel.runtime.retrieval import RetrievalService

        self.settings = settings
        self.sessions = sessions
        self.resources = resources
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self.memory_wake = asyncio.Event()
        self.worker_state = "idle"
        self.worker_error: str | None = None
        self._sink: ZvecChunkSink | None = None
        self._sink_lock = asyncio.Lock()
        self.performance: RuntimePerformance = RuntimePerformance(settings)
        self.operations: OperationService = OperationService(self)
        self.retrieval: RetrievalService = RetrievalService(self)
        self.memory: MemoryService = MemoryService(settings)
        self.memory_tasks: MemoryTaskService = MemoryTaskService(self)
        self.chat: ChatService = ChatService(self)
        self.mcp: MCPConfigurationService = MCPConfigurationService(settings.agent.workspace)
        self.warmup: RuntimeWarmup = RuntimeWarmup(self)

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
        started = perf_counter()
        async with self._sink_lock:
            if self._sink is None:
                trace("index.opening", lock_wait_ms=round((perf_counter() - started) * 1000, 2))
                opening = perf_counter()

                def open_sink() -> ZvecChunkSink:
                    trace("index.worker.started", queue_ms=round((perf_counter() - opening) * 1000, 2))
                    return open_chunk_sink(self.settings.zvec.knowledge_path)

                task = asyncio.create_task(asyncio.to_thread(open_sink))
                try:
                    self._sink = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self._sink = await task
                    raise
                except FileNotFoundError as exc:
                    trace("index.open.failed", error_type=type(exc).__name__)
                    raise VectorSinkError(
                        "知识库向量集合不存在,清理意图仍保留。",
                        hint="运行 shovel init 初始化向量集合;不配置模型也可以使用资源管理和关键词检索。",
                    ) from exc
                trace("index.opened", elapsed_ms=round((perf_counter() - opening) * 1000, 2))
                self.warmup.states["index"] = {
                    "state": "ready",
                    "elapsed_ms": round((perf_counter() - opening) * 1000, 2),
                }
            self.warmup.states["index"]["state"] = "ready"
            self.warmup.states["index"].pop("error_type", None)
            return self._sink

    async def close(self) -> None:
        try:
            await self.chat.close()
        finally:
            try:
                await self.memory.close()
            finally:
                try:
                    await self.retrieval.close()
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
        trace("runtime.initializing")
        await asyncio.to_thread(_prepare_database_path, str(engine.url.database or ""))
        await upgrade_database(engine)
        runtime = Runtime(settings, sessions, resources)
        try:
            await runtime.operations.recover()
            await runtime.chat.recover()
            trace(
                "runtime.ready",
                model_configured=settings.model.is_configured,
                embedding_configured=settings.embedding.is_configured,
                memory_configured=runtime.memory.configured,
            )
            yield runtime
        finally:
            trace("runtime.stopping")
            await runtime.close()
            trace("runtime.stopped")


def _prepare_database_path(database_url_path: str) -> None:
    if database_url_path and database_url_path != ":memory:":
        Path(database_url_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
