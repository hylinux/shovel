from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic, perf_counter
from urllib.parse import urlsplit

import httpx
from anyio import CancelScope

from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import RuntimeFailure


def local_ollama(provider: str, endpoint: str, model: str) -> bool:
    target = urlsplit(endpoint or "http://127.0.0.1:11434")
    return (
        provider == "ollama"
        and target.scheme == "http"
        and target.hostname in {"127.0.0.1", "localhost", "::1"}
        and not model.endswith(("-cloud", ":cloud"))
    )


@dataclass
class _Waiter:
    ready: asyncio.Future[None]
    priority: int
    queued: float
    sequence: int
    granted: bool = False


class LocalModelAdmission:
    def __init__(self, limit: int, timeout: float) -> None:
        self.limit, self.timeout = limit, timeout
        self.running = 0
        self._queue: list[_Waiter] = []
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._source: ContextVar[str | None] = ContextVar("shovel_model_source", default=None)

    @contextmanager
    def source(self, name: str) -> Iterator[None]:
        token = self._source.set(name)
        try:
            yield
        finally:
            self._source.reset(token)

    def _dispatch(self) -> None:
        while self._queue and self.running < self.limit:
            now = monotonic()
            aged = [waiter for waiter in self._queue if now - waiter.queued >= 30]
            waiter = (
                min(aged, key=lambda item: item.sequence)
                if aged
                else min(self._queue, key=lambda item: (item.priority, item.sequence))
            )
            self._queue.remove(waiter)
            waiter.granted = True
            self.running += 1
            waiter.ready.set_result(None)

    @asynccontextmanager
    async def admit(self, default_source: str, stopping: asyncio.Event) -> AsyncIterator[None]:
        source = self._source.get() or default_source
        priority = {"foreground": 0, "platform": 1, "memory": 2, "warmup": 3}[source]
        started = perf_counter()
        waiter = _Waiter(asyncio.get_running_loop().create_future(), priority, monotonic(), self._sequence)
        self._sequence += 1
        async with self._lock:
            if stopping.is_set():
                raise RuntimeFailure("Host 正在停止。", status=503)
            if len(self._queue) >= 64:
                raise RuntimeFailure("本地模型队列已满,请稍后重试。", status=503)
            self._queue.append(waiter)
            self._dispatch()

        async def wait_for_stop() -> None:
            await stopping.wait()

        stop = asyncio.create_task(wait_for_stop())
        try:
            done, _ = await asyncio.wait(
                [waiter.ready, stop], timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if stopping.is_set():
                raise RuntimeFailure("Host 正在停止。", status=503)
            if not done:
                raise RuntimeFailure("本地模型等待超时,请稍后重试。", status=503)
            await waiter.ready
            trace(
                "model.admitted",
                source=source,
                queue_ms=round((perf_counter() - started) * 1000, 2),
                running=self.running,
                limit=self.limit,
            )
            yield
        finally:

            async def leave() -> None:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
                async with self._lock:
                    if waiter.granted:
                        self.running -= 1
                    else:
                        self._queue.remove(waiter)
                        waiter.ready.cancel()
                    self._dispatch()

            with CancelScope(shield=True):
                cleanup = asyncio.create_task(leave())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise


class _LeaseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        lease: AbstractAsyncContextManager[None],
        closed: Callable[[_LeaseStream], None],
    ) -> None:
        self._stream, self._lease = stream, lease
        self._on_closed = closed
        self._closing: asyncio.Task[None] | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        async def close() -> None:
            try:
                await self._stream.aclose()
            finally:
                try:
                    await self._lease.__aexit__(None, None, None)
                finally:
                    self._on_closed(self)

        with CancelScope(shield=True):
            if self._closing is None:
                self._closing = asyncio.create_task(close())
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError:
                await self._closing
                raise


class ModelAdmissionTransport(httpx.AsyncBaseTransport):
    """Hold a slot for the HTTP body, never across an Agent's tool invocation."""

    def __init__(
        self,
        admission: LocalModelAdmission,
        source: str,
        stopping: asyncio.Event,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._admission, self._source, self._stopping = admission, source, stopping
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._streams: set[_LeaseStream] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        lease = self._admission.admit(self._source, self._stopping)
        await lease.__aenter__()
        try:
            response = await self._inner.handle_async_request(request)
            if response.is_stream_consumed:
                await lease.__aexit__(None, None, None)
                return response
            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise RuntimeFailure("本地模型返回了无效的异步响应流。", status=502)
            stream = _LeaseStream(response.stream, lease, self._streams.discard)
            self._streams.add(stream)
            response.stream = stream
            return response
        except BaseException:
            await lease.__aexit__(None, None, None)
            raise

    async def aclose(self) -> None:
        try:
            await asyncio.gather(*(stream.aclose() for stream in tuple(self._streams)))
        finally:
            await self._inner.aclose()
