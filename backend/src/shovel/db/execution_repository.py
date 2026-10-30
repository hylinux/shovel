from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from copy import deepcopy

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.base import new_id
from shovel.db.model.capability import CapabilityDefinition, CapabilityVersion, CapabilityVersionAvailability
from shovel.db.model.execution import ExecutionAttempt as AttemptRow
from shovel.db.model.execution import ExecutionStep as StepRow
from shovel.db.model.runtime import Operation
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.safe_data import ensure_no_credentials
from shovel.execution.domain import (
    EXECUTION_KIND,
    BudgetExceeded,
    BudgetUsage,
    CapabilitySnapshot,
    Execution,
    ExecutionAttempt,
    ExecutionLease,
    ExecutionSnapshot,
    ExecutionState,
    ExecutionStateMachine,
    ExecutionStep,
    ExecutorResult,
    FrozenVersionUnavailable,
)
from shovel.execution.identity import execution_request_key
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import ApprovalState
from shovel.security.ports import ExecutionGate


def _record(row: Operation) -> Execution:
    if row.execution_data is None or row.root_execution_id is None or row.correlation_id is None:
        raise RuntimeFailure("Operation is not a versioned execution.", status=409)
    return Execution(
        id=row.id,
        root_execution_id=row.root_execution_id,
        parent_execution_id=row.parent_execution_id,
        correlation_id=row.correlation_id,
        child_key=row.child_key,
        snapshot=ExecutionSnapshot.model_validate(row.execution_data),
        input=row.payload,
        state=ExecutionState(row.state),
        usage=BudgetUsage.model_validate(row.execution_usage),
        output=row.result.get("output"),
        error=row.error,
        revision=row.row_version,
        attempts=row.attempts,
        created_at=row.queued_at,
        deadline_at=row.execution_deadline,
        approval_request_id=row.result.get("approval_request_id"),
        checkpoint=row.workflow_data,
        control=row.execution_control,
    )


class SQLiteExecutionRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sessions, self.clock = sessions, clock

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> Operation:
        row = await session.scalar(
            select(Operation).where(
                Operation.id == identifier,
                Operation.execution_data.is_not(None),
                func.json_extract(Operation.execution_data, "$.context.scope.workspace_id")
                == scope.workspace_id,
                func.json_extract(Operation.execution_data, "$.context.scope.user_id") == scope.user_id,
            )
        )
        if row is None or row.execution_data is None or _record(row).snapshot.context.scope != scope:
            raise NotFound("Execution", identifier)
        return row

    async def get(self, scope: CapabilityScope, identifier: str) -> Execution:
        async with self.sessions() as session:
            return _record(await self._find(session, scope, identifier))

    async def find_request(self, scope: CapabilityScope, key: str) -> Execution | None:
        async with self.sessions() as session:
            identifier = await session.scalar(
                select(Operation.id).where(
                    Operation.request_key == execution_request_key(scope, key),
                    Operation.execution_data.is_not(None),
                    func.json_extract(Operation.execution_data, "$.context.scope.workspace_id")
                    == scope.workspace_id,
                    func.json_extract(Operation.execution_data, "$.context.scope.user_id") == scope.user_id,
                )
            )
            return _record(await self._find(session, scope, identifier)) if identifier is not None else None

    async def queued(self, identifier: str | None = None) -> Execution | None:
        statement = select(Operation).where(
            Operation.kind == EXECUTION_KIND,
            Operation.capability_version_id.is_not(None),
            Operation.state == ExecutionState.QUEUED.value,
            Operation.parent_execution_id.is_(None),
        )
        if identifier is not None:
            statement = statement.where(Operation.id == identifier)
        async with self.sessions() as session:
            row = await session.scalar(statement.order_by(Operation.queued_at, Operation.id).limit(1))
            return _record(row) if row is not None else None

    async def _replay(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        fingerprint: str,
        *,
        request_key: str | None,
        parent_id: str | None,
        child_key: str | None,
    ) -> Execution | None:
        if request_key is not None:
            row = await session.scalar(
                select(Operation).where(
                    Operation.request_key == execution_request_key(scope, request_key),
                )
            )
        elif parent_id is not None and child_key is not None:
            row = await session.scalar(
                select(Operation).where(
                    Operation.parent_execution_id == parent_id,
                    Operation.child_key == child_key,
                )
            )
        else:
            return None
        if row is None:
            return None
        if (
            row.execution_data is None
            or _record(row).snapshot.context.scope != scope
            or row.request_fingerprint != fingerprint
        ):
            raise RuntimeFailure("Request identity is already used by another intent.", status=409)
        return _record(row)

    async def replay(
        self,
        scope: CapabilityScope,
        fingerprint: str,
        *,
        request_key: str | None = None,
        parent_id: str | None = None,
        child_key: str | None = None,
    ) -> Execution | None:
        async with self.sessions() as session:
            return await self._replay(
                session,
                scope,
                fingerprint,
                request_key=request_key,
                parent_id=parent_id,
                child_key=child_key,
            )

    async def _check_snapshot(
        self,
        session: AsyncSession,
        snapshot: CapabilitySnapshot,
        scope: CapabilityScope,
    ) -> None:
        references: list[tuple[str, int, str, str | None]] = [
            (snapshot.capability_id, snapshot.number, snapshot.content_hash, snapshot.version_id)
        ]
        references.extend(
            (item.capability_id, item.version, item.content_hash, None)
            for item in snapshot.content.dependency_lock.dependencies
        )
        for capability_id, number, digest, version_id in references:
            result = (
                await session.execute(
                    select(CapabilityVersion, CapabilityDefinition, CapabilityVersionAvailability)
                    .join(CapabilityDefinition, CapabilityVersion.capability_id == CapabilityDefinition.id)
                    .join(
                        CapabilityVersionAvailability,
                        CapabilityVersionAvailability.version_id == CapabilityVersion.id,
                    )
                    .where(
                        CapabilityDefinition.workspace_id == scope.workspace_id,
                        CapabilityDefinition.user_id == scope.user_id,
                        CapabilityDefinition.id == capability_id,
                        CapabilityVersion.number == number,
                    )
                )
            ).one_or_none()
            if result is None:
                raise FrozenVersionUnavailable("Frozen capability version is missing.", status=409)
            version, definition, availability = result
            if (
                version.published_at is None
                or version.content_hash != digest
                or (version_id is not None and version.id != version_id)
                or {definition.availability, availability.availability} & {"disabled", "archived"}
            ):
                raise FrozenVersionUnavailable(
                    "Frozen capability version is unavailable or has changed.", status=409
                )

    async def check_snapshot(self, snapshot: CapabilitySnapshot, scope: CapabilityScope) -> None:
        async with self.sessions() as session:
            await self._check_snapshot(session, snapshot, scope)

    async def _lineage(self, session: AsyncSession, row: Operation) -> list[Operation]:
        rows = [row]
        scope = _record(row).snapshot.context.scope
        while rows[-1].parent_execution_id is not None:
            parent_id = rows[-1].parent_execution_id
            assert parent_id is not None
            if len(rows) >= 32 or parent_id in {item.id for item in rows}:
                raise RuntimeFailure("Invalid execution ancestry.", status=409)
            rows.append(await self._find(session, scope, parent_id))
        return rows

    async def _charge(self, session: AsyncSession, row: Operation, delta: BudgetUsage) -> None:
        for item in await self._lineage(session, row):
            execution = _record(item)
            if execution.state != ExecutionState.RUNNING:
                raise RuntimeFailure("Execution ancestor is not running.", status=409)
            if execution.deadline_at is not None and self.clock() >= execution.deadline_at:
                raise BudgetExceeded("duration")
            usage = execution.usage.charge(delta, execution.snapshot.budget)
            item.execution_usage = usage.model_dump(mode="json")
            item.row_version += 1

    async def create(
        self,
        snapshot: ExecutionSnapshot,
        payload: dict[str, JsonValue],
        fingerprint: str,
        *,
        request_key: str | None = None,
        parent: ExecutionLease | None = None,
        child_key: str | None = None,
    ) -> Execution:
        ensure_no_credentials(snapshot)
        ensure_no_credentials(payload)
        payload = deepcopy(payload)
        snapshot = ExecutionSnapshot.model_validate(snapshot.model_dump(mode="json"))
        if snapshot.context.correlation_id is None or (parent is None) != (child_key is None):
            raise RuntimeFailure("Execution correlation and child identity are required.")
        scope = snapshot.context.scope
        async with sqlite_write(self.sessions) as session:
            previous = await self._replay(
                session,
                scope,
                fingerprint,
                request_key=request_key,
                parent_id=parent.execution.id if parent else None,
                child_key=child_key,
            )
            if previous is not None:
                return previous
            await self._check_snapshot(session, snapshot.capability, scope)
            stored_key = execution_request_key(scope, request_key) if request_key is not None else None
            identifier = (
                "op_execution_" + stored_key.removeprefix("capability/") if stored_key else new_id("op")
            )
            root_id, parent_id = identifier, None
            if parent is not None:
                parent_row = await self._find(session, scope, parent.execution.id)
                self._leased(parent_row, parent)
                current = _record(parent_row)
                if (
                    snapshot.context.actor_id != current.snapshot.context.actor_id
                    or not set(snapshot.context.permission_ceiling).issubset(
                        current.snapshot.context.permission_ceiling
                    )
                    or not set(snapshot.granted_permissions).issubset(current.snapshot.granted_permissions)
                    or snapshot.context.proactive_boundary != current.snapshot.context.proactive_boundary
                    or snapshot.context.workflow_boundary != current.snapshot.context.workflow_boundary
                    or snapshot.context.skill_boundary != current.snapshot.context.skill_boundary
                    or snapshot.context.proactive_preference != current.snapshot.context.proactive_preference
                    or snapshot.context.proactive_snapshot != current.snapshot.context.proactive_snapshot
                    or (
                        current.snapshot.context.workflow_approval_required
                        and not snapshot.context.workflow_approval_required
                    )
                    or snapshot.context.correlation_id != current.correlation_id
                    or not snapshot.budget.narrows(current.snapshot.budget)
                    or snapshot.capability_call_path[:-1] != current.snapshot.capability_call_path
                ):
                    raise RuntimeFailure("Child execution cannot change its inherited context.", status=403)
                await self._charge(session, parent_row, BudgetUsage(child_executions=1))
                root_id, parent_id = current.root_execution_id, current.id
                identifier = "op_child_" + hashlib.sha256(f"{parent_id}:{child_key}".encode()).hexdigest()
            row = Operation(
                id=identifier,
                kind=EXECUTION_KIND,
                request_key=stored_key,
                request_fingerprint=fingerprint,
                payload=payload,
                state=ExecutionState.QUEUED.value,
                capability_version_id=snapshot.capability.version_id,
                root_execution_id=root_id,
                parent_execution_id=parent_id,
                correlation_id=snapshot.context.correlation_id,
                child_key=child_key,
                execution_data=snapshot.model_dump(mode="json"),
                execution_usage={},
                queued_at=int(self.clock()),
            )
            session.add(row)
            await session.commit()
            return _record(row)

    @staticmethod
    def _leased(row: Operation, lease: ExecutionLease) -> None:
        if row.state != ExecutionState.RUNNING.value or row.claim_token != lease.token:
            raise RuntimeFailure("Stale execution lease.", status=409)

    async def start(self, scope: CapabilityScope, identifier: str) -> ExecutionLease:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            current = _record(row)
            ExecutionStateMachine.check(current.state, ExecutionState.RUNNING)
            await self.ensure_resumable(session, row)
            await self._check_snapshot(session, current.snapshot.capability, scope)
            now = self.clock()
            if row.execution_deadline is None:
                row.execution_deadline = now + current.snapshot.budget.max_duration_seconds
            for ancestor in (await self._lineage(session, row))[1:]:
                if ancestor.execution_deadline is not None:
                    row.execution_deadline = min(row.execution_deadline, ancestor.execution_deadline)
            row.state, row.claim_token = ExecutionState.RUNNING.value, new_id("claim")
            row.started_at = row.started_at if row.started_at is not None else int(now)
            row.attempts += 1
            await self._charge(session, row, BudgetUsage(steps=1))
            session.add(
                AttemptRow(
                    execution_id=row.id,
                    number=row.attempts,
                    claim_token=row.claim_token,
                    started_at=now,
                )
            )
            await session.commit()
            return ExecutionLease(execution=_record(row), token=row.claim_token)

    async def dispatch(self, lease: ExecutionLease, gate: ExecutionGate) -> Execution:
        from shovel.db.approval_repository import SQLiteApprovalRepository

        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, lease.execution.snapshot.context.scope, lease.execution.id)
            self._leased(row, lease)
            attempt = await session.scalar(select(AttemptRow).where(AttemptRow.claim_token == lease.token))
            if attempt is None or attempt.dispatched_at is not None:
                raise RuntimeFailure("Execution attempt was already dispatched.", status=409)
            await self._check_snapshot(
                session, _record(row).snapshot.capability, lease.execution.snapshot.context.scope
            )
            execution = _record(row)
            evaluation = await gate.evaluate(execution)
            approval = await SQLiteApprovalRepository.for_dispatch(
                session,
                execution,
                evaluation,
                self.clock(),
            )
            if approval is not None and approval.state != ApprovalState.CONSUMED:
                state = (
                    ExecutionState.SUSPENDED
                    if approval.state == ApprovalState.PENDING
                    else ExecutionState.FAILED
                )
                ExecutionStateMachine.check(execution.state, state)
                await self._charge(session, row, BudgetUsage())
                await self._end_attempt(session, row, state)
                row.state = state.value
                row.error = f"approval_{approval.state.value}"
                row.result = {**row.result, "approval_request_id": approval.id}
                row.finished_at = int(self.clock()) if state.terminal else None
                row.row_version += 1
                await session.commit()
                return _record(row)
            delta = (
                BudgetUsage(function_calls=1)
                if _record(row).snapshot.capability.kind == "function"
                else BudgetUsage()
            )
            await self._charge(session, row, delta)
            attempt.dispatched_at = self.clock()
            await session.flush()
            session.add(
                StepRow(
                    execution_id=row.id,
                    attempt_number=attempt.number,
                    state=ExecutionState.RUNNING.value,
                    started_at=attempt.dispatched_at,
                )
            )
            await session.commit()
            return _record(row)

    async def consume(self, lease: ExecutionLease, delta: BudgetUsage) -> Execution:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, lease.execution.snapshot.context.scope, lease.execution.id)
            self._leased(row, lease)
            await self._charge(session, row, delta)
            await session.commit()
            return _record(row)

    async def _children(self, session: AsyncSession, row: Operation) -> list[Operation]:
        found: list[Operation] = []
        pending = [row.id]
        seen = {row.id}
        while pending:
            children = list(
                await session.scalars(select(Operation).where(Operation.parent_execution_id.in_(pending)))
            )
            if any(item.id in seen for item in children):
                raise RuntimeFailure("Invalid execution ancestry.", status=409)
            seen.update(item.id for item in children)
            found.extend(children)
            pending = [item.id for item in children]
        return found

    async def _end_attempt(
        self, session: AsyncSession, row: Operation, state: ExecutionState, *, reason: str | None = None
    ) -> None:
        if row.claim_token:
            attempt = await session.scalar(
                select(AttemptRow).where(AttemptRow.claim_token == row.claim_token)
            )
            if attempt is not None:
                attempt.finished_at, attempt.outcome = self.clock(), state.value
                attempt.reason = reason
                step = await session.get(StepRow, (row.id, attempt.number))
                if step is not None:
                    step.state, step.finished_at = state.value, attempt.finished_at
            else:
                raise RuntimeFailure("Execution claim has no durable attempt.", status=409)
        row.claim_token = None

    async def _cancel_rows(self, session: AsyncSession, rows: list[Operation]) -> list[str]:
        cancelled: list[str] = []
        for row in rows:
            current = ExecutionState(row.state)
            if not current.terminal:
                ExecutionStateMachine.check(current, ExecutionState.CANCELLED)
                await self._end_attempt(session, row, ExecutionState.CANCELLED)
                row.state, row.finished_at = ExecutionState.CANCELLED.value, int(self.clock())
                row.row_version += 1
                cancelled.append(row.id)
        return cancelled

    async def finish(
        self,
        lease: ExecutionLease,
        state: ExecutionState,
        result: ExecutorResult,
        *,
        error: str | None = None,
    ) -> Execution:
        ensure_no_credentials(result)
        result = ExecutorResult.model_validate(result.model_dump(mode="json"))
        if state not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.SUSPENDED}:
            raise RuntimeFailure("Invalid execution completion state.", status=409)
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, lease.execution.snapshot.context.scope, lease.execution.id)
            self._leased(row, lease)
            ExecutionStateMachine.check(ExecutionState(row.state), state)
            children = await self._children(session, row)
            if state != ExecutionState.FAILED and any(
                not ExecutionState(item.state).terminal
                and not (state == ExecutionState.SUSPENDED and item.state == ExecutionState.SUSPENDED.value)
                for item in children
            ):
                raise RuntimeFailure("Execution has unfinished child executions.", status=409)
            if state == ExecutionState.FAILED:
                await self._cancel_rows(session, children)
            if state in {ExecutionState.SUCCEEDED, ExecutionState.SUSPENDED}:
                await self._charge(session, row, BudgetUsage())
            await self._end_attempt(session, row, state, reason=error)
            row.result = (
                {**row.result, "output": result.output}
                if state == ExecutionState.SUSPENDED
                else {"output": result.output}
            )
            row.state, row.error = state.value, error
            row.finished_at = int(self.clock()) if state.terminal else None
            row.row_version += 1
            await session.commit()
            return _record(row)

    async def cancel(self, scope: CapabilityScope, identifier: str) -> list[str]:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            cancelled = await self._cancel_rows(session, [row, *(await self._children(session, row))])
            await session.commit()
            return cancelled

    async def ensure_resumable(self, session: AsyncSession, row: Operation) -> None:
        if row.execution_control.get("wait_reason") == "interrupted" and not row.execution_control.get(
            "replay_safe"
        ):
            raise RuntimeFailure("Interrupted side effects require reconciliation, not replay.", status=409)
        execution = _record(row)
        if (
            execution.snapshot.capability.content.side_effect_profile.kind
            in {"external_write", "destructive"}
            and execution.snapshot.capability.kind != "workflow"
        ):
            dispatched = await session.scalar(
                select(AttemptRow.id)
                .where(
                    AttemptRow.execution_id == row.id,
                    AttemptRow.dispatched_at.is_not(None),
                )
                .limit(1)
            )
            if dispatched is not None:
                raise RuntimeFailure("A dispatched external effect cannot be blindly resumed.", status=409)

    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            wake_at = row.execution_control.get("wake_at")
            if isinstance(wake_at, int | float) and self.clock() < wake_at:
                raise RuntimeFailure("Execution wakeup is not due.", status=409)
            await self.ensure_resumable(session, row)
            ExecutionStateMachine.check(ExecutionState(row.state), ExecutionState.QUEUED)
            if row.execution_deadline is not None and self.clock() >= row.execution_deadline:
                raise BudgetExceeded("duration")
            row.state, row.error = ExecutionState.QUEUED.value, None
            row.execution_control = {**row.execution_control, "wait_reason": None, "wake_at": None}
            row.row_version += 1
            await session.commit()
            return _record(row)

    async def reject(self, scope: CapabilityScope, identifier: str, error: str) -> Execution:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            ExecutionStateMachine.check(ExecutionState(row.state), ExecutionState.FAILED)
            if row.state != ExecutionState.QUEUED.value:
                raise RuntimeFailure("Only an unclaimed execution can be rejected.", status=409)
            row.state, row.error, row.finished_at = ExecutionState.FAILED.value, error, int(self.clock())
            row.row_version += 1
            await session.commit()
            return _record(row)

    async def attempts(self, scope: CapabilityScope, identifier: str) -> list[ExecutionAttempt]:
        async with self.sessions() as session:
            await self._find(session, scope, identifier)
            rows = await session.scalars(
                select(AttemptRow).where(AttemptRow.execution_id == identifier).order_by(AttemptRow.number)
            )
            return [
                ExecutionAttempt(
                    execution_id=row.execution_id,
                    number=row.number,
                    started_at=row.started_at,
                    dispatched_at=row.dispatched_at,
                    finished_at=row.finished_at,
                    outcome=ExecutionState(row.outcome) if row.outcome else None,
                    reason=row.reason,
                )
                for row in rows
            ]

    async def steps(self, scope: CapabilityScope, identifier: str) -> list[ExecutionStep]:
        async with self.sessions() as session:
            await self._find(session, scope, identifier)
            rows = await session.scalars(
                select(StepRow).where(StepRow.execution_id == identifier).order_by(StepRow.attempt_number)
            )
            return [
                ExecutionStep(
                    execution_id=row.execution_id,
                    attempt_number=row.attempt_number,
                    state=ExecutionState(row.state),
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ]
