from __future__ import annotations

import asyncio

import structlog

from shovel.automation.domain import NormalizedEvent
from shovel.automation.ports import EventHandler
from shovel.domain.safe_data import ensure_no_credentials
from shovel.runtime.errors import RuntimeFailure


class InProcessEventBus:
    """Bounded, process-local transport; publish acknowledges admission, not durability."""

    def __init__(self, *, concurrency: int = 4, capacity: int = 128, stop_timeout: float = 30) -> None:
        if not 1 <= concurrency <= 64 or capacity < 1 or stop_timeout <= 0:
            raise ValueError("Event bus limits must be positive and concurrency at most 64.")
        self._concurrency, self._timeout = concurrency, stop_timeout
        self._queue: asyncio.Queue[NormalizedEvent] = asyncio.Queue(capacity)
        self._handlers: list[EventHandler] = []
        self._workers: list[asyncio.Task[None]] = []
        self._admission = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._accepting = False
        self._closed = asyncio.Event()
        self._closed.set()
        self.failures = 0
        self._log = structlog.get_logger("shovel.events")

    def subscribe(self, handler: EventHandler) -> None:
        if self._workers:
            raise RuntimeFailure("Event subscriptions must be registered before start.")
        if handler not in self._handlers:
            self._handlers.append(handler)

    async def start(self) -> None:
        async with self._lifecycle:
            if self._workers:
                return
            self._workers = [asyncio.create_task(self._run()) for _ in range(self._concurrency)]
            self._closed.clear()
            self._accepting = True

    async def publish(self, event: NormalizedEvent) -> None:
        ensure_no_credentials(event)
        event = NormalizedEvent.model_validate(event.model_dump(mode="json"))
        async with self._admission:
            if not self._accepting:
                raise RuntimeFailure("Event bus is not accepting events.")
            enqueue = asyncio.create_task(self._queue.put(event))
            closing = asyncio.create_task(self._closed.wait())
            try:
                await asyncio.wait((enqueue, closing), return_when=asyncio.FIRST_COMPLETED)
                if self._closed.is_set():
                    raise RuntimeFailure("Event bus stopped during admission.")
                await enqueue
            finally:
                enqueue.cancel()
                closing.cancel()
                await asyncio.gather(enqueue, closing, return_exceptions=True)

    async def join(self) -> None:
        await self._queue.join()

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                for handler in self._handlers:
                    try:
                        await handler(event.model_copy(deep=True))
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        if task is not None and task.cancelling():
                            raise
                        self.failures += 1
                        self._log.error(
                            "event.handler_failed", event_id=event.id, error_type="CancelledError"
                        )
                    except Exception as exc:
                        self.failures += 1
                        self._log.error(
                            "event.handler_failed", event_id=event.id, error_type=type(exc).__name__
                        )
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        async with self._lifecycle:
            # Close admission before draining; blocked publishers must observe shutdown.
            self._accepting = False
            self._closed.set()
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._admission:
                        await self.join()
            except TimeoutError:
                self._log.error("event.stop_timeout")
                raise
            finally:
                for worker in self._workers:
                    worker.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)
                self._workers.clear()
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()
