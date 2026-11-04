from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.model.effect import ExternalEffect, ExternalEffectEvent, record_effect
from shovel.db.model.execution import ExecutionAttempt
from shovel.db.model.runtime import Operation
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.execution.domain import Execution, ExecutionLease, ExecutionState, ExecutionStateMachine
from shovel.execution.effects import UNRESOLVED, EffectEvidence, EffectIntent, EffectReceipt
from shovel.protection.domain import content_digest
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import SecurityDenied
from shovel.security.ports import ExecutionGate


def _receipt(row: ExternalEffect) -> EffectReceipt:
    return EffectReceipt(
        id=row.id,
        execution_id=row.execution_id,
        intent=EffectIntent.model_validate(row.intent),
        state=row.state,
        revision=row.revision,
        attempt=row.attempt,
        resume_required=row.resume_required,
        conflicted=row.conflicted,
        evidence_digest=row.evidence_digest,
    )


class SQLiteEffectRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock
        self.executions = SQLiteExecutionRepository(sessions, clock=clock)

    async def get_execution(self, scope: CapabilityScope, identifier: str) -> Execution:
        return await self.executions.get(scope, identifier)

    async def prepare(
        self, lease: ExecutionLease, intent: EffectIntent, gate: ExecutionGate
    ) -> tuple[EffectReceipt, str]:
        execution = lease.execution
        identifier = content_digest({"execution": execution.id, "step": "external_effect"})
        effect_key = content_digest(
            {
                "scope": execution.snapshot.context.scope.model_dump(mode="json"),
                "operation": intent.implementation_id,
                "target": intent.target_digest,
                "request": intent.request_digest,
            }
        )
        fingerprint = content_digest(
            {
                "snapshot": execution.snapshot.model_dump(mode="json"),
                "input": execution.input,
                "intent": intent.model_dump(mode="json"),
                "execution": execution.id,
            }
        )
        async with sqlite_write(self.sessions, durable=True) as session:
            await self.executions._active(session, lease, gate)
            await self.executions._check_snapshot(
                session, execution.snapshot.capability, execution.snapshot.context.scope
            )
            competing = await session.scalar(
                select(ExternalEffect.id)
                .where(
                    ExternalEffect.effect_key == effect_key,
                    ExternalEffect.execution_id != execution.id,
                    ExternalEffect.state.in_(UNRESOLVED),
                )
                .limit(1)
            )
            if competing is not None:
                raise SecurityDenied(
                    "Reconcile the existing external effect instead of creating another request."
                )
            row = await session.get(ExternalEffect, identifier)
            if row is not None:
                if row.fingerprint != fingerprint:
                    raise RuntimeFailure("External-effect arguments or frozen bindings changed.", status=409)
                if row.resume_required or row.state in UNRESOLVED:
                    raise RuntimeFailure("External effect is not safe to redispatch.", status=409)
                if row.state in {"not_sent", "not_applied"}:
                    row.state, row.claim_token, row.attempt = "intent", lease.token, execution.attempts
                    row.evidence_digest = None
                    row.resume_required = True
                    record_effect(session, row, self.clock(), "intent")
            else:
                row = ExternalEffect(
                    id=identifier,
                    execution_id=execution.id,
                    intent=intent.model_dump(mode="json"),
                    fingerprint=fingerprint,
                    effect_key=effect_key,
                    idempotency_key=new_id("effect"),
                    state="intent",
                    revision=0,
                    attempt=execution.attempts,
                    claim_token=lease.token,
                    resume_required=True,
                    conflicted=False,
                )
                session.add(row)
                record_effect(session, row, self.clock(), "intent")
            await session.commit()
            return _receipt(row), row.idempotency_key

    async def dispatch(self, lease: ExecutionLease, identifier: str, gate: ExecutionGate) -> None:
        async with sqlite_write(self.sessions, durable=True) as session:
            await self.executions._active(session, lease, gate)
            await self.executions._check_snapshot(
                session, lease.execution.snapshot.capability, lease.execution.snapshot.context.scope
            )
            row = await session.get(ExternalEffect, identifier)
            if (
                row is None
                or row.execution_id != lease.execution.id
                or row.claim_token != lease.token
                or row.state != "intent"
            ):
                raise RuntimeFailure("External effect cannot enter the wire phase.", status=409)
            row.state = "dispatched"
            record_effect(session, row, self.clock(), "dispatch")
            await session.commit()

    async def not_sent(self, lease: ExecutionLease, identifier: str) -> bool:
        async with sqlite_write(self.sessions, durable=True) as session:
            row = await session.get(ExternalEffect, identifier)
            if row is None or row.execution_id != lease.execution.id or row.claim_token != lease.token:
                raise RuntimeFailure("External effect has no matching send intent.", status=409)
            if row.state != "intent":
                # A failed/ambiguous dispatch commit cannot prove that the wire phase was not entered.
                return False
            row.state = "not_sent"
            record_effect(session, row, self.clock(), "not_sent")
            await session.commit()
            return True

    async def received(
        self, lease: ExecutionLease, identifier: str, evidence: EffectEvidence
    ) -> EffectReceipt:
        async with sqlite_write(self.sessions, durable=True) as session:
            row = await session.get(ExternalEffect, identifier)
            if row is None or row.execution_id != lease.execution.id:
                raise RuntimeFailure("External-effect receipt has no matching intent.", status=409)
            owner = await session.get(Operation, row.execution_id)
            assert owner is not None
            attempt = await session.scalar(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.execution_id == owner.id,
                    ExecutionAttempt.claim_token == lease.token,
                )
            )
            dispatched = (
                await session.scalar(
                    select(ExternalEffectEvent.id)
                    .where(
                        ExternalEffectEvent.effect_id == row.id,
                        ExternalEffectEvent.attempt == attempt.number,
                        ExternalEffectEvent.state == "dispatched",
                    )
                    .limit(1)
                )
                if attempt is not None
                else None
            )
            if (
                _record(owner).snapshot.context.scope != lease.execution.snapshot.context.scope
                or dispatched is None
            ):
                raise RuntimeFailure("External-effect receipt has no matching wire attempt.", status=409)
            # A late response is a fact even after cancellation, but it never revives an Execution.
            conflict = row.claim_token != lease.token or (
                row.state in {"applied", "not_applied"} and evidence.outcome not in {"unknown", row.state}
            )
            row.conflicted = row.conflicted or conflict
            row.state = "uncertain" if row.conflicted or evidence.outcome == "unknown" else evidence.outcome
            row.evidence_digest = evidence.evidence_digest
            row.resume_required = row.state != "applied" or owner.claim_token != lease.token
            row.reconciliation_token, row.reconciliation_until = None, None
            assert attempt is not None
            record_effect(session, row, self.clock(), "received", source_attempt=attempt.number)
            await session.commit()
            return _receipt(row)

    async def inspect(self, scope: CapabilityScope, identifier: str) -> tuple[Execution, list[EffectReceipt]]:
        async with self.sessions() as session:
            row = await self.executions._find(session, scope, identifier)
            facts = await self.executions.effect_rows(session, row)
            return _record(row), [_receipt(fact) for fact in facts]

    async def claim_reconciliation(
        self, scope: CapabilityScope, identifier: str, receipt: EffectReceipt
    ) -> tuple[str, str]:
        async with sqlite_write(self.sessions, durable=True) as session:
            owner = await self.executions._find(session, scope, identifier)
            lineage = await self.executions._lineage(session, owner)
            tree = [owner, *await self.executions._children(session, owner)]
            if any(row.state in {"running", "queued"} for row in [*lineage, *tree]):
                raise RuntimeFailure("Stop active dispatches before reconciliation.", status=409)
            row = await session.get(ExternalEffect, receipt.id)
            if (
                row is None
                or row.execution_id not in {item.id for item in tree}
                or row.revision != receipt.revision
                or row.state not in UNRESOLVED
                or (row.reconciliation_until is not None and row.reconciliation_until > self.clock())
            ):
                raise RuntimeFailure(
                    "External-effect reconciliation changed or is already active.", status=409
                )
            row.reconciliation_token = new_id("reconcile")
            row.reconciliation_until = self.clock() + 60
            record_effect(session, row, self.clock(), "reconcile_claimed")
            await session.commit()
            return row.reconciliation_token, row.idempotency_key

    async def reconciled(
        self,
        scope: CapabilityScope,
        identifier: str,
        effect_id: str,
        token: str,
        evidence: EffectEvidence,
    ) -> None:
        async with sqlite_write(self.sessions, durable=True) as session:
            owner = await self.executions._find(session, scope, identifier)
            facts = await self.executions.effect_rows(session, owner)
            row = next((fact for fact in facts if fact.id == effect_id), None)
            if (
                row is None
                or row.reconciliation_token != token
                or row.reconciliation_until is None
                or self.clock() >= row.reconciliation_until
            ):
                raise RuntimeFailure("External-effect reconciliation claim is stale.", status=409)
            row.state = "uncertain" if row.conflicted or evidence.outcome == "unknown" else evidence.outcome
            row.evidence_digest, row.resume_required = evidence.evidence_digest, True
            row.reconciliation_token, row.reconciliation_until = None, None
            record_effect(session, row, self.clock(), "reconciled")
            await session.commit()

    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution:
        async with sqlite_write(self.sessions, durable=True) as session:
            row = await self.executions._find(session, scope, identifier)
            if row.parent_execution_id is not None:
                raise RuntimeFailure(
                    "Explicitly resume the root Execution, not an external-effect child.", status=409
                )
            ExecutionStateMachine.check(ExecutionState(row.state), ExecutionState.QUEUED)
            if row.state != "suspended":
                raise RuntimeFailure("Only a suspended Execution can resume external effects.", status=409)
            facts = await self.executions.effect_rows(session, row)
            if (
                not facts
                or any(fact.state in UNRESOLVED for fact in facts)
                or any(
                    fact.reconciliation_until is not None and self.clock() < fact.reconciliation_until
                    for fact in facts
                )
            ):
                raise RuntimeFailure("Reconcile all external effects before explicitly resuming.", status=409)
            for fact in facts:
                fact.resume_required = False
                record_effect(session, fact, self.clock(), "resume_authorized")
            for item in [row, *await self.executions._children(session, row)]:
                if item.state != "suspended":
                    continue
                if _record(item).snapshot.capability.kind == "workflow" or any(
                    fact.execution_id == item.id for fact in facts
                ):
                    item.execution_control = {
                        **item.execution_control,
                        "wait_reason": None,
                        "replay_safe": True,
                        "wake_at": None,
                    }
            await self.executions.ensure_resumable(session, row)
            if row.execution_deadline is None or self.clock() >= row.execution_deadline:
                raise RuntimeFailure("The original external-effect deadline has expired.", status=409)
            row.state, row.error = "queued", None
            row.execution_control = {**row.execution_control, "wait_reason": None, "wake_at": None}
            row.row_version += 1
            await session.commit()
            return _record(row)
