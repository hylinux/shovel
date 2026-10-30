from __future__ import annotations

import math
import time
from collections.abc import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.interaction_repository import notice
from shovel.db.model.control import ExecutionWakeup
from shovel.db.model.execution import ExecutionAttempt
from shovel.db.model.interaction import InputRequest, Interaction, NotificationDelivery
from shovel.db.model.runtime import Operation
from shovel.db.model.security import ApprovalRequest
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.control import RetryPolicy, SafeReplay, Wakeup
from shovel.execution.domain import Execution, ExecutionLease, ExecutionState, ExecutionStateMachine
from shovel.runtime.errors import RuntimeFailure


def _wakeup(row: ExecutionWakeup) -> Wakeup:
    return Wakeup(
        id=row.id, execution_id=row.execution_id, wake_at=row.wake_at, kind=row.kind, state=row.state
    )


class SQLiteExecutionControlRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock
        self.executions = SQLiteExecutionRepository(sessions, clock=clock)

    async def _finish(
        self, session: AsyncSession, row: Operation, state: ExecutionState, reason: str
    ) -> None:
        ExecutionStateMachine.check(ExecutionState(row.state), state)
        token = row.claim_token
        await self.executions._end_attempt(session, row, state)
        if token:
            attempt = await session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.claim_token == token)
            )
            assert attempt is not None
            attempt.reason = reason
        row.state, row.error = state.value, reason
        row.finished_at = int(self.clock()) if state.terminal else None
        row.row_version += 1
        if state.terminal:
            await self.executions._cancel_rows(session, await self.executions._children(session, row))

    async def _schedule(
        self, session: AsyncSession, row: Operation, wake_at: float, kind: str
    ) -> ExecutionWakeup:
        await self.executions.ensure_resumable(session, row)
        now = self.clock()
        if math.isfinite(wake_at) and wake_at < now and kind in {"resume", "recovery", "child_ready"}:
            wake_at = now
        if not math.isfinite(wake_at) or wake_at < now:
            raise RuntimeFailure("wake_at must be a finite UTC timestamp at or after now.")
        if row.execution_deadline is None:
            row.execution_deadline = self.clock() + _record(row).snapshot.budget.max_duration_seconds
        if wake_at >= row.execution_deadline:
            raise RuntimeFailure("Wakeup exceeds the frozen execution duration budget.", status=409)
        if row.state == ExecutionState.QUEUED.value:
            ExecutionStateMachine.check(ExecutionState.QUEUED, ExecutionState.SUSPENDED)
            row.state = ExecutionState.SUSPENDED.value
        if row.state != ExecutionState.SUSPENDED.value:
            raise RuntimeFailure(
                "Execution timers require a suspended or unclaimed queued execution.", status=409
            )
        if row.execution_control.get("wait_reason") == "interrupted" and not row.execution_control.get(
            "replay_safe"
        ):
            raise RuntimeFailure("Interrupted side effects require reconciliation, not replay.", status=409)
        previous = await session.scalar(
            select(ExecutionWakeup).where(
                ExecutionWakeup.execution_id == row.id,
                ExecutionWakeup.state == "pending",
            )
        )
        if previous is not None:
            if previous.wake_at == wake_at and previous.kind == kind:
                return previous
            previous.state = "cancelled"
            await session.flush()
        row.execution_control = {**row.execution_control, "wake_at": wake_at}
        if _record(row).wait_reason not in {
            "waiting_for_approval",
            "waiting_for_input",
            "waiting_for_child",
            "interrupted",
        }:
            row.execution_control = {
                **row.execution_control,
                "wait_reason": "retry_scheduled" if kind == "retry" else kind,
            }
        row.row_version += 1
        timer = ExecutionWakeup(execution_id=row.id, wake_at=wake_at, kind=kind, created_at=self.clock())
        session.add(timer)
        await session.flush()
        return timer

    async def timer(
        self,
        scope: CapabilityScope,
        identifier: str,
        wake_at: float,
        kind: str,
        *,
        interaction_id: str | None = None,
    ) -> Wakeup:
        async with sqlite_write(self.sessions) as session:
            row = await self.executions._find(session, scope, identifier)
            if interaction_id is not None:
                projection = await session.get(Interaction, interaction_id)
                if (
                    projection is None
                    or projection.execution_id != row.id
                    or projection.workspace_id != scope.workspace_id
                    or projection.user_id != scope.user_id
                    or projection.state != "open"
                    or "SNOOZE" not in projection.actions
                ):
                    raise RuntimeFailure("Interaction cannot be snoozed.", status=409)
                projection.snoozed_until = wake_at
            timer = await self._schedule(session, row, wake_at, kind)
            await session.commit()
            return _wakeup(timer)

    async def suspend(self, lease: ExecutionLease, wake_at: float) -> Execution:
        async with sqlite_write(self.sessions) as session:
            row = await self.executions._find(
                session, lease.execution.snapshot.context.scope, lease.execution.id
            )
            self.executions._leased(row, lease)
            await self._finish(session, row, ExecutionState.SUSPENDED, "delay")
            await self._schedule(session, row, wake_at, "delay")
            await session.commit()
            return _record(row)

    async def retry(self, lease: ExecutionLease, policy: RetryPolicy, safe: bool) -> Execution:
        async with sqlite_write(self.sessions) as session:
            row = await self.executions._find(
                session, lease.execution.snapshot.context.scope, lease.execution.id
            )
            self.executions._leased(row, lease)
            policy = RetryPolicy.model_validate(
                row.execution_control.get("retry_policy", policy.model_dump(mode="json"))
            )
            retries = int(row.execution_control.get("retry_count", 0))
            wake_at = self.clock() + policy.backoff(retries)
            if (
                not safe
                or retries + 1 >= policy.max_attempts
                or row.execution_deadline is None
                or wake_at >= row.execution_deadline
            ):
                reason = "unsafe_retry_denied" if not safe else "retry_exhausted"
                await self._finish(session, row, ExecutionState.FAILED, reason)
            else:
                await self._finish(session, row, ExecutionState.SUSPENDED, "transient_failure")
                row.execution_control = {
                    **row.execution_control,
                    "retry_policy": policy.model_dump(mode="json"),
                    "retry_count": retries + 1,
                    "wait_reason": "retry_scheduled",
                }
                await self._schedule(session, row, wake_at, "retry")
            await session.commit()
            return _record(row)

    async def _ready(self, session: AsyncSession, row: Operation, depth: int = 0) -> bool:
        if depth >= 32 or _record(row).state.terminal:
            return False
        wake_at = row.execution_control.get("wake_at")
        if isinstance(wake_at, int | float) and wake_at > self.clock():
            return False
        reason = _record(row).wait_reason
        if reason == "waiting_for_approval":
            request = await SQLiteApprovalRepository.latest(session, row.id)
            return request is not None and request.state == "approved" and request.expires_at > self.clock()
        if reason == "waiting_for_input":
            request_input = await session.scalar(
                select(InputRequest).where(
                    InputRequest.execution_id == row.id,
                    InputRequest.node_id == (row.workflow_data or {}).get("node_id"),
                )
            )
            return request_input is not None and request_input.state == "answered"
        if reason == "waiting_for_child":
            child = await session.get(Operation, (row.workflow_data or {}).get("child_execution_id"))
            return child is not None and (
                _record(child).state.terminal or await self._ready(session, child, depth + 1)
            )
        if reason == "interrupted":
            return bool(row.execution_control.get("replay_safe"))
        return row.state == ExecutionState.SUSPENDED.value

    async def _fail_expired(self, session: AsyncSession, row: Operation, reason: str) -> None:
        if row.state not in {"queued", "suspended"}:
            return
        await self._finish(session, row, ExecutionState.FAILED, reason)
        if row.parent_execution_id is not None:
            root = await session.get(Operation, row.root_execution_id)
            if (
                root is not None
                and root.state == "suspended"
                and root.execution_deadline is not None
                and root.execution_deadline > self.clock()
            ):
                pending = await session.scalar(
                    select(ExecutionWakeup.id).where(
                        ExecutionWakeup.execution_id == root.id,
                        ExecutionWakeup.state == "pending",
                    )
                )
                if pending is None and await self._ready(session, root):
                    await self._schedule(session, root, self.clock(), "child_ready")

    async def _resume_answered(self, session: AsyncSession, now: float) -> None:
        approved = select(ApprovalRequest.id).where(
            ApprovalRequest.execution_id == Operation.id,
            ApprovalRequest.state == "approved",
            ApprovalRequest.expires_at > now,
            func.json_extract(Operation.result, "$.approval_request_id") == ApprovalRequest.id,
        )
        answered = select(InputRequest.id).where(
            InputRequest.execution_id == Operation.id,
            InputRequest.state == "answered",
            func.json_extract(Operation.workflow_data, "$.node_id") == InputRequest.node_id,
        )
        held = select(ExecutionWakeup.id).where(
            ExecutionWakeup.execution_id.in_((Operation.id, Operation.root_execution_id)),
            ExecutionWakeup.state == "pending",
            ExecutionWakeup.wake_at > now,
        )
        rows = list(
            await session.scalars(
                select(Operation)
                .where(
                    Operation.state == "suspended",
                    or_(approved.exists(), answered.exists()),
                    ~held.exists(),
                )
                .order_by(Operation.id)
                .limit(100)
            )
        )
        for row in rows:
            root = row
            while root.parent_execution_id is not None:
                parent = await session.get(Operation, root.parent_execution_id)
                if parent is None or (parent.workflow_data or {}).get("child_execution_id") != root.id:
                    break
                root = parent
            if (
                root.parent_execution_id is not None
                or root.state != "suspended"
                or root.execution_control.get("replay_safe") is False
                or not await self._ready(session, root)
            ):
                continue
            pending = await session.scalar(
                select(ExecutionWakeup.id).where(
                    ExecutionWakeup.execution_id == root.id,
                    ExecutionWakeup.state == "pending",
                )
            )
            if pending is None:
                await self._schedule(session, root, now, "resume")

    async def tick(self) -> int:
        async with sqlite_write(self.sessions) as session:
            now, changed = self.clock(), 0
            approvals = list(
                await session.scalars(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.state.in_(("pending", "approved")),
                        ApprovalRequest.expires_at <= now,
                    )
                    .limit(100)
                )
            )
            for request in approvals:
                SQLiteApprovalRepository.expire_due(request, now)
            inputs = list(
                await session.scalars(
                    select(InputRequest)
                    .where(
                        InputRequest.state == "pending",
                        InputRequest.expires_at <= now,
                    )
                    .limit(100)
                )
            )
            for request_input in inputs:
                request_input.state = "expired"
            await session.flush()
            expired_approvals = list(
                await session.scalars(
                    select(ApprovalRequest)
                    .join(
                        Operation,
                        ApprovalRequest.execution_id == Operation.id,
                    )
                    .where(
                        ApprovalRequest.state.in_(("expired", "rejected")),
                        Operation.state.in_(("suspended", "queued")),
                        func.json_extract(Operation.result, "$.approval_request_id") == ApprovalRequest.id,
                    )
                    .limit(100)
                )
            )
            for request in expired_approvals:
                row = await session.get(Operation, request.execution_id)
                assert row is not None
                await self._fail_expired(session, row, f"approval_{request.state}")
            expired_inputs = list(
                await session.scalars(
                    select(InputRequest)
                    .join(
                        Operation,
                        InputRequest.execution_id == Operation.id,
                    )
                    .where(
                        InputRequest.state == "expired",
                        Operation.state.in_(("suspended", "queued")),
                        func.json_extract(Operation.workflow_data, "$.node_id") == InputRequest.node_id,
                    )
                    .limit(100)
                )
            )
            for request_input in expired_inputs:
                row = await session.get(Operation, request_input.execution_id)
                assert row is not None
                await self._fail_expired(session, row, "input_expired")
            deadlines = list(
                await session.scalars(
                    select(Operation)
                    .where(
                        Operation.execution_data.is_not(None),
                        Operation.state.in_(("suspended", "queued")),
                        Operation.execution_deadline <= now,
                    )
                    .limit(100)
                )
            )
            for row in deadlines:
                await self._fail_expired(session, row, "budget_exceeded")
            await self._resume_answered(session, now)
            parents = list(
                await session.scalars(
                    select(Operation)
                    .where(
                        Operation.state == "suspended",
                        func.json_extract(Operation.workflow_data, "$.wait_reason") == "waiting_for_child",
                    )
                    .limit(100)
                )
            )
            for parent in parents:
                if parent.execution_deadline is not None and parent.execution_deadline <= now:
                    await self._fail_expired(session, parent, "budget_exceeded")
                    continue
                child = await session.get(Operation, (parent.workflow_data or {}).get("child_execution_id"))
                if child is not None and _record(child).state.terminal:
                    pending = await session.scalar(
                        select(ExecutionWakeup.id).where(
                            ExecutionWakeup.execution_id == parent.id,
                            ExecutionWakeup.state == "pending",
                        )
                    )
                    if pending is None:
                        await self._schedule(session, parent, now, "child_ready")
            due_at = self.clock()
            timers = list(
                await session.scalars(
                    select(ExecutionWakeup)
                    .where(
                        ExecutionWakeup.state == "pending",
                        ExecutionWakeup.wake_at <= due_at,
                    )
                    .order_by(ExecutionWakeup.wake_at, ExecutionWakeup.id)
                    .limit(100)
                )
            )
            for timer in timers:
                row = await session.get(Operation, timer.execution_id)
                assert row is not None
                if _record(row).state.terminal:
                    timer.state = "cancelled"
                    continue
                root = await session.get(Operation, row.root_execution_id)
                assert root is not None
                if root.state == "running":
                    continue
                if await self._ready(session, root) and root.state == "suspended":
                    ExecutionStateMachine.check(ExecutionState.SUSPENDED, ExecutionState.QUEUED)
                    root.state, root.error = "queued", None
                    root.execution_control = {**root.execution_control, "wait_reason": None, "wake_at": None}
                    root.row_version += 1
                    changed += 1
                row.execution_control = {**row.execution_control, "wake_at": None}
                timer.state = "applied"
                reminders = list(
                    await session.scalars(
                        select(Interaction).where(
                            Interaction.execution_id == row.id,
                            Interaction.state == "open",
                            Interaction.snoozed_until.is_not(None),
                            Interaction.snoozed_until <= due_at,
                        )
                    )
                )
                for interaction in reminders:
                    interaction.snoozed_until = None
                    pending_delivery = await session.scalar(
                        select(NotificationDelivery.id)
                        .where(
                            NotificationDelivery.interaction_id == interaction.id,
                            NotificationDelivery.channel == "console",
                            NotificationDelivery.state.in_(("pending", "delivering")),
                        )
                        .limit(1)
                    )
                    if pending_delivery is None:
                        await notice(session, interaction, now, suffix=f".{timer.id}")
            await session.commit()
            return changed

    async def recover(self, safe: SafeReplay, policy: RetryPolicy) -> int:
        async with sqlite_write(self.sessions) as session:
            rows = list(
                await session.scalars(
                    select(Operation).where(
                        Operation.execution_data.is_not(None),
                        Operation.state == "running",
                    )
                )
            )
            for row in rows:
                replay_safe = safe(_record(row).snapshot.capability.content)
                await self._finish(session, row, ExecutionState.SUSPENDED, "interrupted")
                row.execution_control = {
                    **row.execution_control,
                    "wait_reason": "interrupted",
                    "replay_safe": replay_safe,
                }
            for row in rows:
                frozen_policy = RetryPolicy.model_validate(
                    row.execution_control.get("retry_policy", policy.model_dump(mode="json"))
                )
                descendants = await self.executions._children(session, row)
                replay_safe = row.execution_control["replay_safe"] and not any(
                    child.execution_control.get("wait_reason") == "interrupted"
                    and not child.execution_control.get("replay_safe")
                    for child in descendants
                )
                row.execution_control = {**row.execution_control, "replay_safe": bool(replay_safe)}
                retries = int(row.execution_control.get("retry_count", 0))
                if (
                    replay_safe
                    and retries + 1 < frozen_policy.max_attempts
                    and row.execution_deadline is not None
                    and self.clock() < row.execution_deadline
                ):
                    row.execution_control = {
                        **row.execution_control,
                        "retry_count": retries + 1,
                        "retry_policy": frozen_policy.model_dump(mode="json"),
                    }
                    await self._schedule(session, row, self.clock(), "recovery")
            deliveries = list(
                await session.scalars(
                    select(NotificationDelivery).where(NotificationDelivery.state == "delivering")
                )
            )
            for delivery in deliveries:
                delivery.state = "failed" if delivery.attempts >= 3 else "pending"
                delivery.token, delivery.next_at = None, self.clock()
                delivery.error = "delivery_interrupted"
            await session.commit()
            return len(rows)
