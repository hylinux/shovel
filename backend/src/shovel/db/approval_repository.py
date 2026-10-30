from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.execution_repository import _record
from shovel.db.model.runtime import Operation
from shovel.db.model.security import ApprovalRequest as ApprovalRow
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.domain import Execution, ExecutionState
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import ApprovalRequest, ApprovalState, GateEvaluation, SecurityDenied
from shovel.security.ports import ExecutionGate


def _approval(row: ApprovalRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        execution_id=row.execution_id,
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        user_id=row.user_id,
        number=row.number,
        binding_hash=row.binding_hash,
        reasons=tuple(row.reasons),
        state=ApprovalState(row.state),
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
        consumed_attempt=row.consumed_attempt,
    )


class SQLiteApprovalRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sessions, self.clock = sessions, clock

    @staticmethod
    async def latest(session: AsyncSession, execution_id: str) -> ApprovalRow | None:
        return await session.scalar(
            select(ApprovalRow)
            .where(
                ApprovalRow.execution_id == execution_id,
            )
            .order_by(ApprovalRow.number.desc())
            .limit(1)
        )

    @staticmethod
    def expire_due(row: ApprovalRow, now: float) -> None:
        if row.state in {"pending", "approved"} and now >= row.expires_at:
            row.state = ApprovalState.EXPIRED.value
            row.decided_at = row.decided_at if row.decided_at is not None else now

    @classmethod
    async def for_dispatch(
        cls,
        session: AsyncSession,
        execution: Execution,
        evaluation: GateEvaluation,
        now: float,
    ) -> ApprovalRequest | None:
        latest = await cls.latest(session, execution.id)
        if latest is not None:
            cls.expire_due(latest, now)
            if latest.state in {"rejected", "expired"}:
                return _approval(latest)
        if not evaluation.approval.required:
            return None
        if (
            latest is None
            or latest.binding_hash != evaluation.binding_hash
            or latest.state == ApprovalState.CONSUMED.value
        ):
            latest = ApprovalRow(
                execution_id=execution.id,
                workspace_id=execution.snapshot.context.scope.workspace_id,
                user_id=execution.snapshot.context.scope.user_id,
                number=latest.number + 1 if latest is not None else 1,
                binding_hash=evaluation.binding_hash,
                reasons=list(evaluation.approval.reasons),
                state=ApprovalState.PENDING.value,
                created_at=now,
                expires_at=now + 300,
            )
            session.add(latest)
            await session.flush()
        if latest.state == ApprovalState.APPROVED.value:
            latest.state, latest.consumed_at = ApprovalState.CONSUMED.value, now
            latest.consumed_attempt = execution.attempts
        return _approval(latest)

    async def _find(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
    ) -> tuple[ApprovalRow, Execution]:
        if user_id != scope.user_id:
            raise SecurityDenied("Approval user does not match the execution owner.")
        row = await session.get(ApprovalRow, request_id)
        if (
            row is None
            or row.workspace_id != scope.workspace_id
            or row.user_id != user_id
            or row.execution_id != execution_id
        ):
            raise NotFound("Approval request", request_id)
        operation = await session.get(Operation, execution_id)
        if (
            operation is None
            or operation.execution_data is None
            or _record(operation).snapshot.context.scope != scope
        ):
            raise NotFound("Execution", execution_id)
        return row, _record(operation)

    async def get(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
    ) -> ApprovalRequest:
        async with sqlite_write(self.sessions) as session:
            row, _ = await self._find(session, scope, execution_id, request_id, user_id)
            self.expire_due(row, self.clock())
            await session.commit()
            return _approval(row)

    async def decide(
        self,
        scope: CapabilityScope,
        execution_id: str,
        request_id: str,
        user_id: str,
        action: Literal["approve", "reject", "expire"],
        gate: ExecutionGate,
    ) -> ApprovalRequest:
        async with sqlite_write(self.sessions) as session:
            row, execution = await self._find(session, scope, execution_id, request_id, user_id)
            before = row.state
            self.expire_due(row, self.clock())
            if row.state == ApprovalState.EXPIRED.value:
                await session.commit()
                if action == "expire" and before != ApprovalState.EXPIRED.value:
                    return _approval(row)
                raise RuntimeFailure("Approval request has expired.", status=409)
            if action == "expire":
                raise RuntimeFailure("Approval request is not due for expiration.", status=409)
            if row.state != ApprovalState.PENDING.value:
                raise RuntimeFailure("Approval decision cannot be replayed.", status=409)
            latest = await self.latest(session, execution_id)
            if latest is None or latest.id != request_id:
                raise RuntimeFailure("Approval request was superseded.", status=409)
            if execution.state != ExecutionState.SUSPENDED:
                raise RuntimeFailure("Execution is not awaiting approval.", status=409)
            if action == "approve":
                if execution.deadline_at is not None and self.clock() >= execution.deadline_at:
                    raise RuntimeFailure("Execution duration budget has expired.", status=409)
                evaluation = await gate.evaluate(execution)
                if evaluation.binding_hash != row.binding_hash:
                    raise RuntimeFailure(
                        "Approval context has changed; request a fresh approval.", status=409
                    )
                row.state = ApprovalState.APPROVED.value
            else:
                row.state = ApprovalState.REJECTED.value
            row.decided_at = self.clock()
            await session.commit()
            return _approval(row)
