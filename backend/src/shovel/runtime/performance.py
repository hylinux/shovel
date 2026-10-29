from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog

from shovel.config.model_settings import ModelProviderType
from shovel.config.settings import AppSettings
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import RuntimeFailure

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


class RuntimePerformance:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings.performance
        self.limit = self.settings.max_foreground_runs or (
            1 if settings.model.model_provider_type == ModelProviderType.OLLAMA else 4
        )
        self._slots = asyncio.Semaphore(self.limit)
        self.foreground = 0
        self.running = 0
        self.last_foreground = float("-inf")
        self.warming = False

    @asynccontextmanager
    async def admit(self, stopping: asyncio.Event) -> AsyncIterator[None]:
        started = perf_counter()
        self.foreground += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), self.settings.foreground_queue_seconds)
                acquired = True
            except TimeoutError as exc:
                raise RuntimeFailure("Agent 繁忙,请稍后重试。", status=503) from exc
            if stopping.is_set():
                raise RuntimeFailure("Host 正在停止。", status=503)
            self.running += 1
            trace("chat.admitted", queue_ms=round((perf_counter() - started) * 1000, 2))
            try:
                yield
            finally:
                self.running -= 1
        finally:
            if acquired:
                self._slots.release()
            self.foreground -= 1
            self.last_foreground = monotonic()

    def background_delay(self) -> float:
        if self.foreground or self.warming:
            return 1.0
        return max(0.0, self.settings.background_idle_seconds - (monotonic() - self.last_foreground))

    async def wait_for_idle(self, stopping: asyncio.Event) -> None:
        while (delay := self.background_delay()) > 0:
            if stopping.is_set():
                raise asyncio.CancelledError
            try:
                await asyncio.wait_for(stopping.wait(), min(delay, 1.0))
            except TimeoutError:
                continue
        if stopping.is_set():
            raise asyncio.CancelledError

    def snapshot(self) -> dict[str, int]:
        return {"limit": self.limit, "running": self.running, "waiting": self.foreground - self.running}


class RuntimeWarmup:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        settings = runtime.settings

        def local_ollama(provider: str, endpoint: str, model: str) -> bool:
            target = urlsplit(endpoint or "http://127.0.0.1:11434")
            return (
                provider == "ollama"
                and target.scheme == "http"
                and target.hostname in {"127.0.0.1", "localhost", "::1"}
                and not model.endswith(("-cloud", ":cloud"))
            )

        local_memory = local_ollama(
            settings.memory.embedder.provider,
            settings.memory.embedder.base_url,
            settings.memory.embedder.model,
        )
        self.states: dict[str, dict[str, Any]] = {
            "index": {
                "state": "cold"
                if settings.performance.warm_index and settings.embedding.is_configured
                else "disabled"
            },
            "memory_embedding": {
                "state": "cold"
                if settings.performance.warm_local_models and runtime.memory.configured and local_memory
                else "disabled"
            },
            "rag_embedding": {
                "state": "cold"
                if settings.performance.warm_local_models
                and settings.embedding.is_configured
                and local_ollama(
                    settings.embedding.provider, settings.embedding.base_url, settings.embedding.model
                )
                else "disabled"
            },
            "chat_model": {
                "state": "cold"
                if settings.performance.warm_local_models
                and settings.model.is_configured
                and local_ollama(
                    settings.model.model_provider_type,
                    settings.model.base_endpoint,
                    settings.model.model_name,
                )
                else "disabled"
            },
        }

    async def run(self) -> None:
        actions: dict[str, Callable[[], Awaitable[object]]] = {
            "index": self.runtime.sink,
            "memory_embedding": self.runtime.memory.warmup,
            "rag_embedding": lambda: self.runtime.retrieval.embed_query("Shovel warmup"),
            "chat_model": self.runtime.chat.warm_model,
        }
        for component, action in actions.items():
            if self.states[component]["state"] != "cold":
                continue
            await self.runtime.performance.wait_for_idle(self.runtime.stopping)
            started = perf_counter()
            self.states[component] = {"state": "warming"}
            self.runtime.performance.warming = True
            trace("runtime.warmup.started", component_name=component)
            try:
                await action()
            except asyncio.CancelledError:
                self.states[component] = {"state": "cancelled"}
                raise
            except Exception as exc:
                self.states[component] = {"state": "failed", "error_type": type(exc).__name__}
                structlog.get_logger("shovel.runtime").warning(
                    "runtime.warmup.failed", component_name=component, error_type=type(exc).__name__
                )
            else:
                self.states[component] = {"state": "ready"}
            finally:
                self.runtime.performance.warming = False
                elapsed = round((perf_counter() - started) * 1000, 2)
                self.states[component]["elapsed_ms"] = elapsed
                trace("runtime.warmup.completed", component_name=component, **self.states[component])
