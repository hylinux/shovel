from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping

import structlog
from anyio import CancelScope
from pydantic import JsonValue

from shovel.capability.domain import CapabilityKind, CapabilityScope
from shovel.execution.control import ExecutionWakeupService, TransientExecutionError
from shovel.execution.domain import (
    BudgetExceeded,
    BudgetUsage,
    ChildInvocation,
    Execution,
    ExecutionLease,
    ExecutionState,
    ExecutorResult,
    FrozenVersionUnavailable,
)
from shovel.execution.invocation import InvocationService
from shovel.execution.ports import CapabilityExecutor, ExecutorContext
from shovel.execution.validation import validate_data
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import SecurityDenied
from shovel.workflow.ports import WorkflowContext


class ExecutionEngine:
    def __init__(
        self,
        invocations: InvocationService,
        executors: Mapping[CapabilityKind, CapabilityExecutor],
        *,
        clock: Callable[[], float] = time.time,
        workflow_context: Callable[[ExecutionLease], WorkflowContext] | None = None,
        wakeups: ExecutionWakeupService | None = None,
    ) -> None:
        if set(executors) != set(CapabilityKind):
            raise ValueError("Exactly one executor is required for every capability kind.")
        self.invocations, self.repository, self.clock = invocations, invocations.repository, clock
        self._executors = dict(executors)
        self._workflow_context = workflow_context
        self.wakeups = wakeups
        self._active: dict[str, asyncio.Task[ExecutorResult]] = {}
        self._dispatches: set[asyncio.Future[None]] = set()
        self._log = structlog.get_logger("shovel.execution")

    async def start(self, scope: CapabilityScope, identifier: str) -> ExecutionLease:
        execution = await self.repository.get(scope, identifier)
        snapshot = execution.snapshot.capability
        resolved = await self.invocations.registry.resolve_exact(
            scope, snapshot.capability_id, snapshot.number
        )
        if resolved.content_hash != snapshot.content_hash:
            raise RuntimeFailure("Frozen version cannot be replaced.", status=409)
        await self.invocations.security.evaluate(execution)
        return await self.repository.start(scope, identifier)

    async def run(self, scope: CapabilityScope, identifier: str) -> Execution:
        return await self.dispatch(await self.start(scope, identifier))

    async def dispatch(self, lease: ExecutionLease) -> Execution:
        scope, identifier = lease.execution.snapshot.context.scope, lease.execution.id
        try:
            execution = await self.repository.dispatch(lease, self.invocations.security)
        except BudgetExceeded:
            return await self.fail(lease, error_code="budget_exceeded")
        except FrozenVersionUnavailable:
            self._log.error("execution.version_unavailable", execution_id=identifier)
            return await self.fail(lease, error_code="version_unavailable")
        except SecurityDenied:
            self._log.error("execution.security_denied", execution_id=identifier)
            return await self.fail(lease, error_code="security_denied")
        if execution.state != ExecutionState.RUNNING:
            return execution
        executor = self._executors[execution.snapshot.capability.kind]

        async def consume(delta: BudgetUsage) -> Execution:
            return await self.repository.consume(lease, delta)

        async def call(invocation: ChildInvocation) -> Execution:
            child = await self.invocations.invoke_child(lease, invocation)
            if child.state == ExecutionState.SUSPENDED:
                child = await self.resume(scope, child.id)
            if child.state == ExecutionState.QUEUED:
                return await self.run(scope, child.id)
            return child

        context = ExecutorContext(
            execution=execution,
            consume=consume,
            call=call,
            workflow=self._workflow_context(lease) if self._workflow_context else None,
        )
        completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._dispatches.add(completion)
        task = asyncio.create_task(executor.execute(context))
        self._active[identifier] = task
        try:
            if execution.deadline_at is None:
                raise RuntimeFailure("Running execution has no deadline.", status=409)
            remaining = execution.deadline_at - self.clock()
            if remaining <= 0:
                raise BudgetExceeded("duration")
            result = await asyncio.wait_for(task, timeout=remaining)
            if result.suspended:
                if result.wake_at is not None:
                    if self.wakeups is None:
                        raise RuntimeFailure("Execution timers are not configured.", status=503)
                    return await self.wakeups.suspend(lease, result.wake_at)
                return await self.suspend(lease, result.output)
            return await self.succeed(lease, result.output)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                current = await self.repository.get(scope, identifier)
                if current.state == ExecutionState.RUNNING:
                    await self.cancel(scope, identifier)
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            return await self.repository.get(scope, identifier)
        except BudgetExceeded, TimeoutError:
            return await self.fail(lease, error_code="budget_exceeded")
        except TransientExecutionError:
            self._log.warning("execution.transient_failure", execution_id=identifier)
            if self.wakeups is None:
                return await self.fail(lease, error_code="transient_failure")
            return await self.wakeups.retry(
                lease, safe=self.invocations.registry.retry_safe(execution.snapshot.capability.content)
            )
        except Exception as exc:
            self._log.error("execution.failed", execution_id=identifier, error_type=type(exc).__name__)
            return await self.fail(lease, error_code="executor_failed")
        finally:
            if not task.done():
                task.cancel()
                with CancelScope(shield=True):
                    await asyncio.gather(task, return_exceptions=True)
            if self._active.get(identifier) is task:
                self._active.pop(identifier, None)
            self._dispatches.discard(completion)
            if not completion.done():
                completion.set_result(None)

    async def succeed(self, lease: ExecutionLease, output: JsonValue = None) -> Execution:
        current = await self.repository.get(lease.execution.snapshot.context.scope, lease.execution.id)
        validate_data(current.snapshot.capability.content.output_schema, output)
        return await self.repository.finish(lease, ExecutionState.SUCCEEDED, ExecutorResult(output=output))

    async def suspend(self, lease: ExecutionLease, checkpoint: JsonValue = None) -> Execution:
        execution = await self.repository.finish(
            lease,
            ExecutionState.SUSPENDED,
            ExecutorResult(output=checkpoint, suspended=True),
        )
        task = self._active.get(execution.id)
        if task is not None and not task.done():
            task.cancel()
        return execution

    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution:
        return await self.repository.resume(scope, identifier)

    async def fail(self, lease: ExecutionLease, *, error_code: str = "executor_failed") -> Execution:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise RuntimeFailure("Execution error code must be a bounded identifier.")
        current = await self.repository.get(lease.execution.snapshot.context.scope, lease.execution.id)
        if current.state.terminal:
            return current
        result = await self.repository.finish(
            lease,
            ExecutionState.FAILED,
            ExecutorResult(),
            error=error_code,
        )
        await self._interrupt_cancelled(lease.execution.snapshot.context.scope)
        return result

    async def _interrupt_cancelled(self, scope: CapabilityScope) -> None:
        for identifier, task in tuple(self._active.items()):
            try:
                execution = await self.repository.get(scope, identifier)
            except NotFound:
                continue
            if execution.state in {ExecutionState.CANCELLED, ExecutionState.FAILED} and not task.done():
                task.cancel()

    async def cancel(self, scope: CapabilityScope, identifier: str) -> Execution:
        cancelled = await self.repository.cancel(scope, identifier)
        for item in cancelled:
            task = self._active.get(item)
            if task is not None and not task.done():
                task.cancel()
        return await self.repository.get(scope, identifier)

    async def run_once(self, identifier: str | None = None) -> bool:
        execution = await self.repository.queued(identifier)
        if execution is None:
            return False
        scope = execution.snapshot.context.scope
        try:
            await self.run(scope, execution.id)
        except RuntimeFailure as exc:
            current = await self.repository.get(scope, execution.id)
            if current.state == ExecutionState.QUEUED:
                await self.repository.reject(scope, execution.id, "dispatch_rejected")
                self._log.error(
                    "execution.rejected",
                    execution_id=execution.id,
                    error_type=type(exc).__name__,
                )
        return True

    async def close(self) -> None:
        tasks = tuple(self._active.values())
        dispatches = tuple(self._dispatches)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*dispatches, *tasks, return_exceptions=True)
