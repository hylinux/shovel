from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from time import perf_counter
from typing import Any

from shovel.config.settings import AppSettings
from shovel.memory.config import memory_paths
from shovel.memory.store import open_memory
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import NotFound, RuntimeFailure


class MemoryService:
    """One lazy mem0 client on one owning thread, including embedded Qdrant."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._memory: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shovel-memory")

    def scope(self, conversation_id: str | None = None) -> dict[str, str]:
        from pathlib import Path

        workspace = self.settings.agent.workspace
        default = (Path.home() / ".shovel" / "workspaces" / "default").resolve()
        # Preserve ownership of memories created before configurable workspaces.
        agent_id = (
            "shovel"
            if workspace == default
            else f"shovel:{hashlib.sha256(str(workspace).encode()).hexdigest()}"
        )
        result = {"user_id": "local-user", "agent_id": agent_id}
        if conversation_id is not None:
            result.update(agent_id=f"{agent_id}:short", run_id=conversation_id)
        return result

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.memory.embedder.model
            and (self.settings.model.is_configured or self.settings.memory.llm.model)
        )

    def _get(self) -> Any:
        if not self.configured:
            raise RuntimeFailure(
                "Memory 尚未配置。",
                status=503,
                hint="配置 [memory.embedder] 的模型和 [model] 或 [memory.llm];未启用记忆的对话不受影响。",
            )
        if self._memory is None:
            for path in memory_paths(self.settings.memory):
                path.mkdir(parents=True, exist_ok=True)
            self._memory = open_memory(self.settings.memory, self.settings.model)
        return self._memory

    async def _call[T](self, action: Callable[[], T]) -> T:
        started = perf_counter()
        trace("memory.started", action=action.__name__)
        try:
            result = await asyncio.get_running_loop().run_in_executor(self._executor, action)
            trace(
                "memory.completed",
                action=action.__name__,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )
            return result
        except RuntimeFailure as exc:
            trace("memory.failed", action=action.__name__, error_type=type(exc).__name__)
            raise
        except Exception as exc:
            trace("memory.failed", action=action.__name__, error_type=type(exc).__name__)
            raise RuntimeFailure(
                f"Memory 操作失败 ({type(exc).__name__})。",
                status=502,
                hint="检查 Memory 的模型、向量维度和 Qdrant 配置;本次操作未报告成功。",
            ) from exc

    def _scope_arguments(
        self, method: Callable[..., Any], limit: int, conversation_id: str | None = None
    ) -> dict[str, Any]:
        parameters = inspect.signature(method).parameters
        # mem0 2.x moved entity arguments into filters and renamed limit to top_k.
        scope = self.scope(conversation_id)
        result: dict[str, Any] = scope if "user_id" in parameters else {"filters": scope}
        result["top_k" if "top_k" in parameters else "limit"] = limit
        return result

    @staticmethod
    def _items(response: Any) -> list[dict[str, Any]]:
        rows = response.get("results") if isinstance(response, dict) else response
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise RuntimeFailure("Memory 返回了不支持的数据格式。", status=502)
        return rows

    async def search(self, query: str, *, conversation_id: str | None = None) -> list[dict[str, Any]]:
        def search() -> list[dict[str, Any]]:
            memory = self._get()
            rows = self._items(memory.search(query, **self._scope_arguments(memory.search, 5)))
            if conversation_id is not None:
                short = self._items(
                    memory.search(query, **self._scope_arguments(memory.search, 5, conversation_id))
                )
                rows = short + rows
            return rows

        return await self._call(search)

    async def remember(self, content: str, *, conversation_id: str | None = None) -> None:
        def add() -> None:
            memory = self._get()
            messages = [{"role": "user", "content": content}]
            if conversation_id is not None:
                memory.add(messages, **self.scope(conversation_id))
            memory.add(messages, **self.scope())

        await self._call(add)

    async def forget_conversation(self, conversation_id: str) -> None:
        def forget() -> None:
            self._get().delete_all(**self.scope(conversation_id))

        await self._call(forget)

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        def list_memories() -> list[dict[str, Any]]:
            memory = self._get()
            return self._items(memory.get_all(**self._scope_arguments(memory.get_all, limit)))

        return await self._call(list_memories)

    async def delete(self, identifier: str) -> None:
        def remove() -> None:
            memory = self._get()
            item = memory.get(identifier)
            if not isinstance(item, dict) or any(
                item.get(key) != value for key, value in self.scope().items()
            ):
                raise NotFound("记忆", identifier)
            memory.delete(identifier)

        await self._call(remove)

    async def close(self) -> None:
        def close() -> None:
            if self._memory is None:
                return
            memory, self._memory = self._memory, None
            try:
                close_method = getattr(memory, "close", None)
                if callable(close_method):
                    close_method()
                elif getattr(memory, "db", None) is not None:
                    memory.db.close()
            finally:
                vector_store = getattr(memory, "vector_store", None)
                client = getattr(vector_store, "client", None)
                if client is not None:
                    client.close()

        try:
            await self._call(close)
        finally:
            await asyncio.to_thread(partial(self._executor.shutdown, wait=True))
