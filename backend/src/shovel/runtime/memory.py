from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial, wraps
from time import perf_counter
from typing import Any

from anyio import CancelScope

from shovel.config.settings import AppSettings
from shovel.memory.config import build_memory_config, memory_paths
from shovel.memory.store import open_memory
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import NotFound, RuntimeFailure


class ExtractionCancelled(RuntimeFailure):
    def __init__(self) -> None:
        super().__init__("长期记忆任务已失效或取消。", status=409)


class ExtractionGuard:
    def __init__(self, before_write: Callable[[], None], validate: Callable[[], None] | None) -> None:
        self.cancelled = threading.Event()
        self.before_write = before_write
        self.validate = validate
        self.failure: BaseException | None = None

    def check(self) -> None:
        if self.cancelled.is_set():
            raise ExtractionCancelled()
        if self.failure is not None:
            raise RuntimeFailure("长期记忆存储写入失败,任务可能已部分执行。", status=502) from self.failure


_extraction: ContextVar[ExtractionGuard | None] = ContextVar("memory_extraction", default=None)
_WRITES = frozenset(
    {
        "insert",
        "update",
        "delete",
        "delete_all",
        "delete_col",
        "reset",
        "add_history",
        "batch_add_history",
        "save_messages",
    }
)


def _generate_memory_response(config: dict[str, Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    from mem0.utils.factory import LlmFactory

    try:
        model = LlmFactory.create(config["provider"], config["config"])
        return model.generate_response(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"Memory provider failed ({type(exc).__name__})") from None


class _OwnedStorage:
    def __init__(self, target: Any, owner: Callable[[Callable[[], Any]], Any]) -> None:
        self._target, self._owner = target, owner

    def __getattr__(self, name: str) -> Any:
        value = self._owner(lambda: getattr(self._target, name))
        if not callable(value):
            return value

        @wraps(value)
        def call(*args: Any, **kwargs: Any) -> Any:
            def invoke() -> Any:
                guard = _extraction.get()
                if guard is not None:
                    guard.check()
                    if name in _WRITES:
                        guard.before_write()
                        guard.check()
                try:
                    return value(*args, **kwargs)
                except BaseException as exc:
                    if guard is not None and name in _WRITES:
                        guard.failure = exc
                        raise RuntimeFailure(
                            f"长期记忆存储写入失败 ({type(exc).__name__})。", status=502
                        ) from None
                    raise

            return self._owner(invoke)

        return call


class _BackgroundLlm:
    def __init__(self, target: Any, generate: Callable[..., Any]) -> None:
        self._target, self._generate = target, generate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        if _extraction.get() is not None:
            from mem0.memory.utils import extract_json, remove_code_blocks

            response = self._generate(*args, **kwargs)
            try:
                cleaned = remove_code_blocks(response)
                try:
                    parsed = json.loads(cleaned, strict=False)
                except json.JSONDecodeError:
                    parsed = json.loads(extract_json(cleaned), strict=False)
                facts = parsed.get("memory") if isinstance(parsed, dict) else None
                if not isinstance(facts, list) or any(
                    not isinstance(fact, dict)
                    or not isinstance(fact.get("text"), str)
                    or not fact["text"].strip()
                    for fact in facts
                ):
                    raise ValueError("Invalid memory extraction schema")
            except (TypeError, ValueError, AttributeError) as exc:
                raise RuntimeFailure("长期记忆模型返回的提取结果无效。", status=502) from exc
            return response
        return self._target.generate_response(*args, **kwargs)


class _TimedMemoryComponent:
    def __init__(self, target: Any, phase: str, methods: set[str]) -> None:
        self._target, self._phase, self._methods = target, phase, methods

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if name not in self._methods or not callable(value):
            return value

        @wraps(value)
        def call(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            trace(f"memory.{self._phase}.started", action=name)
            try:
                result = value(*args, **kwargs)
            except BaseException as exc:
                guard = _extraction.get()
                if guard is not None and self._phase == "embedding" and name == "embed":
                    guard.failure = exc
                trace(
                    f"memory.{self._phase}.failed",
                    action=name,
                    error_type=type(exc).__name__,
                    elapsed_ms=round((perf_counter() - started) * 1000, 2),
                )
                raise
            trace(
                f"memory.{self._phase}.completed",
                action=name,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )
            return result

        return call


class MemoryService:
    """One lazy mem0 client; Qdrant/history access stays on one owning thread."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._memory: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shovel-memory")
        self._inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shovel-extraction")
        self._owner_thread: int | None = None
        self._llm_pool: ProcessPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._guard: ExtractionGuard | None = None

    def _storage(self, action: Callable[[], Any]) -> Any:
        if threading.get_ident() == self._owner_thread:
            return action()
        return self._executor.submit(copy_context().run, action).result()

    def _entity_store(self, get: Callable[[], Any]) -> Any:
        return _OwnedStorage(self._storage(get), self._storage)

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        guard = _extraction.get()
        assert guard is not None
        guard.check()
        try:
            with self._pool_lock:
                guard.check()
                if self._llm_pool is None:
                    self._llm_pool = ProcessPoolExecutor(max_workers=1)
                pool = self._llm_pool
                config = build_memory_config(self.settings.memory, self.settings.model)["llm"]
                future = pool.submit(_generate_memory_response, config, args, kwargs)
            return future.result(timeout=self.settings.memory.llm.timeout_seconds)
        except TimeoutError as exc:
            self._terminate_llm()
            raise RuntimeFailure("长期记忆模型请求超时。", status=504) from exc
        except Exception as exc:
            self._terminate_llm()
            guard.check()
            raise RuntimeFailure(f"长期记忆模型调用失败 ({type(exc).__name__})。", status=502) from exc

    def _terminate_llm(self) -> None:
        with self._pool_lock:
            pool, self._llm_pool = self._llm_pool, None
        if pool is not None:
            pool.terminate_workers()

    def cancel_extraction(self) -> None:
        if self._guard is not None:
            self._guard.cancelled.set()
        self._terminate_llm()

    async def extract(
        self,
        content: str,
        *,
        operation_id: str,
        before_write: Callable[[], None],
        validate: Callable[[], None] | None = None,
    ) -> None:
        memory = await self._call(self._get)
        guard = ExtractionGuard(before_write, validate)
        self._guard = guard

        def infer() -> None:
            token = _extraction.set(guard)
            try:
                guard.check()
                if guard.validate is not None:
                    guard.validate()
                guard.check()
                memory.add(
                    [{"role": "user", "content": content}],
                    metadata={"shovel_operation_id": operation_id},
                    **self.scope(),
                )
                guard.check()
            finally:
                _extraction.reset(token)

        future = asyncio.get_running_loop().run_in_executor(self._inference, copy_context().run, infer)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel_extraction()
            with CancelScope(shield=True):
                try:
                    await asyncio.shield(future)
                except Exception:
                    trace("memory.extraction.cancelled")
            raise
        finally:
            self._guard = None

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
            self._owner_thread = threading.get_ident()
            started = perf_counter()
            trace("memory.client.started")
            for path in memory_paths(self.settings.memory):
                path.mkdir(parents=True, exist_ok=True)
            self._memory = open_memory(
                self.settings.memory, self.settings.model, entity_store=self._entity_store
            )
            for name in ("vector_store", "db"):
                component = getattr(self._memory, name, None)
                if component is not None:
                    setattr(self._memory, name, _OwnedStorage(component, self._storage))
            self._memory.llm = _BackgroundLlm(self._memory.llm, self._generate)
            for name, phase, methods in (
                ("llm", "llm", {"generate_response"}),
                ("embedding_model", "embedding", {"embed", "embed_batch"}),
                ("vector_store", "vector", {"search", "insert", "update", "delete", "get"}),
            ):
                component = getattr(self._memory, name, None)
                if component is not None:
                    setattr(self._memory, name, _TimedMemoryComponent(component, phase, methods))
            trace("memory.client.ready", elapsed_ms=round((perf_counter() - started) * 1000, 2))
        return self._memory

    async def _call[T](self, action: Callable[[], T]) -> T:
        started = perf_counter()
        trace("memory.started", action=action.__name__)
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                self._executor, copy_context().run, action
            )
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
            arguments = self._scope_arguments(memory.search, 10 if conversation_id is not None else 5)
            if conversation_id is not None:
                # One recall embeds the query once, without widening the workspace/session boundary.
                arguments.pop("agent_id", None)
                arguments["filters"] = {
                    "user_id": self.scope()["user_id"],
                    "OR": [self.scope(), self.scope(conversation_id)],
                }
            rows = self._items(memory.search(query, **arguments))
            trace("memory.recall.completed", items=len(rows), short_term=conversation_id is not None)
            return rows

        return await self._call(search)

    async def remember(
        self, content: str, *, conversation_id: str | None = None, extract_long_term: bool = True
    ) -> None:
        def add() -> None:
            memory = self._get()
            messages = [{"role": "user", "content": content}]
            if conversation_id is not None:
                started = perf_counter()
                trace("memory.short_term.started", inference=False)
                # Short-term recall keeps the original user text; only long-term facts need inference.
                memory.add(messages, infer=False, **self.scope(conversation_id))
                trace("memory.short_term.completed", elapsed_ms=round((perf_counter() - started) * 1000, 2))
            if not extract_long_term:
                trace("memory.long_term.deferred")
                return
            started = perf_counter()
            trace("memory.long_term.started", inference=True)
            memory.add(messages, **self.scope())
            trace("memory.long_term.completed", elapsed_ms=round((perf_counter() - started) * 1000, 2))

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

    async def owned(self, identifier: str) -> None:
        def verify() -> None:
            item = self._get().get(identifier)
            if not isinstance(item, dict) or any(
                item.get(key) != value for key, value in self.scope().items()
            ):
                raise NotFound("记忆", identifier)

        await self._call(verify)

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
        self.cancel_extraction()
        await asyncio.to_thread(partial(self._inference.shutdown, wait=True))

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
