from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from shovel.db.base import now_ts
from shovel.db.model.execution import Job
from shovel.db.model.retrieval import DeletionOutbox, RetrievalLog
from shovel.db.model.runtime import Operation
from shovel.domain.enums import JobState
from shovel.runtime.errors import NotFound
from shovel.runtime.schemas import HistoryPruneInput

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


async def prune_history(runtime: Runtime, request: HistoryPruneInput, operation_id: str) -> dict[str, int]:
    cutoff = now_ts() - request.keep_days * 86400
    async with runtime.sessions() as session:
        jobs = list(
            await session.scalars(
                select(Job.id)
                .where(
                    Job.finished_at < cutoff,
                    Job.state.in_(
                        (
                            JobState.SUCCEEDED,
                            JobState.COMPLETED_WITH_ERRORS,
                            JobState.FAILED,
                            JobState.CANCELLED,
                        )
                    ),
                    ~select(Operation.id)
                    .where(
                        Operation.job_id == Job.id,
                        Operation.state.in_(("queued", "running")),
                    )
                    .exists(),
                )
                .order_by(Job.finished_at, Job.id)
                .limit(request.limit)
            )
        )
        operations = list(
            await session.scalars(
                select(Operation.id)
                .where(
                    Operation.capability_version_id.is_(None),
                    Operation.finished_at < cutoff,
                    Operation.request_key.is_(None),
                    Operation.state.in_(("succeeded", "completed_with_errors", "failed", "cancelled")),
                    Operation.id != operation_id,
                )
                .order_by(Operation.finished_at, Operation.id)
                .limit(request.limit)
            )
        )
        retrievals = list(
            await session.scalars(
                select(RetrievalLog.id)
                .where(
                    RetrievalLog.queried_at < cutoff,
                )
                .order_by(RetrievalLog.queried_at, RetrievalLog.id)
                .limit(request.limit)
            )
        )
        cleanup = list(
            await session.scalars(
                select(DeletionOutbox.id)
                .where(
                    DeletionOutbox.done_at < cutoff,
                )
                .order_by(DeletionOutbox.done_at, DeletionOutbox.id)
                .limit(request.limit)
            )
        )
        await session.execute(delete(Job).where(Job.id.in_(jobs)))
        await session.execute(delete(Operation).where(Operation.id.in_(operations)))
        await session.execute(delete(RetrievalLog).where(RetrievalLog.id.in_(retrievals)))
        await session.execute(delete(DeletionOutbox).where(DeletionOutbox.id.in_(cleanup)))
        counts = {
            "jobs": len(jobs),
            "operations": len(operations),
            "retrievals": len(retrievals),
            "cleanup": len(cleanup),
        }
        operation = await session.get(Operation, operation_id)
        if operation is None:
            raise NotFound("操作", operation_id)
        operation.result = counts
        operation.state = "succeeded"
        operation.finished_at = now_ts()
        await session.commit()
    return counts
