from __future__ import annotations

import asyncio
import contextlib
import inspect
import signal
import sys
from collections.abc import Callable, Coroutine
from enum import StrEnum
from signal import Signals
from time import monotonic
from types import FrameType

from py_generic_host.di.protocols import ResourceContainer
from py_generic_host.hosting.host import Host, HostOptions
from py_generic_host.hosting.hosted_service import IHostedService
from py_generic_host.hosting.lifetime import ApplicationLifetime
from py_generic_host.hosting.stop_reason import StopReason


async def drain_task[T](task: asyncio.Future[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ShutdownOutcome(StrEnum):
    NOT_STARTED = "not_started"
    STOPPING = "stopping"
    CLEAN = "clean"
    OVER_BUDGET = "over_budget"
    FAILED = "failed"


class RuntimeHost(Host):  # type: ignore[misc]  # The pinned framework does not ship py.typed.
    """Keep the pinned Host's startup, but own Shovel's cancellation-safe shutdown."""

    def __init__(
        self,
        container: ResourceContainer,
        services: list[IHostedService],
        lifetime: ApplicationLifetime,
        options: HostOptions | None = None,
        *,
        on_stopping: Callable[[float], None],
        shutdown_failed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(container, services, lifetime, options)
        self._on_stopping = on_stopping
        self._failure_check = shutdown_failed
        self._stop_task: asyncio.Task[None] | None = None
        self._shutdown_started: float | None = None
        self.shutdown_deadline: float | None = None
        self.shutdown_outcome = ShutdownOutcome.NOT_STARTED
        self._over_budget = False
        self._shutdown_failed = False
        self._phase = "not_started"
        self._service: str | None = None

    @property
    def exit_code(self) -> int:
        return int(super().exit_code or self._over_budget or self._shutdown_failed)

    async def run_async(self) -> None:
        if sys.platform != "win32":
            await super().run_async()
            return
        loop = asyncio.get_running_loop()

        def received(number: int, _frame: FrameType | None) -> None:
            loop.call_soon_threadsafe(self._on_signal, Signals(number))

        previous = [(sig, signal.getsignal(sig)) for sig in (signal.SIGINT, signal.SIGTERM)]
        try:
            for sig, _handler in previous:
                signal.signal(sig, received)
            await super().run_async()
        finally:
            for sig, handler in previous:
                if handler is not None and signal.getsignal(sig) is received:
                    signal.signal(sig, handler)

    def request_shutdown(self) -> None:
        if self._shutdown_started is None:
            self._shutdown_started = asyncio.get_running_loop().time()
            self.shutdown_deadline = self._shutdown_started + self.options.shutdown_timeout
            self.shutdown_outcome = ShutdownOutcome.STOPPING
            self._on_stopping(self.shutdown_deadline)
        self.lifetime.stop_application()

    def _on_signal(self, sig: Signals) -> None:
        super()._on_signal(sig)
        self.request_shutdown()

    async def stop_async(self) -> None:
        self.request_shutdown()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="runtime-host-stop")
        await drain_task(self._stop_task)

    def _mark_over_budget(self) -> None:
        if self._over_budget:
            return
        self._over_budget = True
        self.shutdown_outcome = ShutdownOutcome.OVER_BUDGET
        self._log.error(
            "host.shutdown.over_budget",
            phase=self._phase,
            service=self._service,
            pending_services=len(self._started_services),
            timeout=self.options.shutdown_timeout,
            elapsed_seconds=self._elapsed(),
        )

    def _elapsed(self) -> float:
        assert self._shutdown_started is not None
        return round(asyncio.get_running_loop().time() - self._shutdown_started, 3)

    async def _watch_deadline(self) -> None:
        assert self.shutdown_deadline is not None
        await asyncio.sleep(max(0.0, self.shutdown_deadline - asyncio.get_running_loop().time()))
        self._mark_over_budget()

    async def _stop_phase(self, work: Coroutine[object, object, None], *, cancel: bool) -> bool:
        assert self.shutdown_deadline is not None
        task = asyncio.create_task(work)
        remaining = max(0.0, self.shutdown_deadline - asyncio.get_running_loop().time())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            self._mark_over_budget()
            if cancel:
                task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if done:
                self._record_failure("CancelledError")
            return False
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            return False
        return True

    def _record_failure(self, error_type: str) -> None:
        self._shutdown_failed = True
        self._log.error(
            "host.shutdown.failed", phase=self._phase, service=self._service, error_type=error_type
        )

    async def _close_resources(self) -> None:
        result = self.container.shutdown_resources()
        if inspect.isawaitable(result):
            await result
        self._resource_shutdown = True

    async def _rollback_started_services(self) -> None:
        await self.stop_async()

    async def _shutdown_resources(self, phase: str = "shutdown") -> None:
        await self.stop_async()

    async def _stop(self) -> None:
        self._stopping = True
        self._adopt_external_stop_reason()
        self._set_stop_reason(StopReason.GRACEFUL)
        self._log.info("host.stopping", reason=self.stop_reason)
        monitor = asyncio.create_task(self._watch_deadline(), name="runtime-host-stop-deadline")
        try:
            self._phase = "services"
            for service in reversed(list(self._started_services)):
                self._service = type(service).__name__
                if await self._stop_phase(service.stop(self.lifetime.stopping), cancel=True):
                    self._started_services.remove(service)
                    self._log.info("host.service.stopped", service=self._service)
                else:
                    self._log.error("host.service.stop.incomplete", service=self._service)
            self._service = None
            self._phase = "unwire"
            try:
                self.container.unwire()
            except Exception as exc:
                self._record_failure(type(exc).__name__)
            self._phase = "resources"
            if not self._resource_shutdown:
                # Finalizers own native handles and writer locks; never abandon them at the deadline.
                await self._stop_phase(self._close_resources(), cancel=False)
            assert self.shutdown_deadline is not None
            if asyncio.get_running_loop().time() > self.shutdown_deadline:
                self._mark_over_budget()
            if self._failure_check is not None and self._failure_check():
                self._record_failure("KnowledgeWorkerInterrupted")
            self.shutdown_outcome = (
                ShutdownOutcome.FAILED
                if self._shutdown_failed
                else ShutdownOutcome.OVER_BUDGET
                if self._over_budget
                else ShutdownOutcome.CLEAN
            )
            self._phase = "complete"
            self.lifetime.stopped.set()
            log = self._log.error if self.exit_code else self._log.info
            log(
                "host.stopped",
                reason=self.stop_reason,
                shutdown_outcome=self.shutdown_outcome,
                exit_code=self.exit_code,
                pending_services=len(self._started_services),
                elapsed_seconds=self._elapsed(),
                uptime_seconds=(
                    round(monotonic() - self._started_at, 3) if self._started_at is not None else None
                ),
            )
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
