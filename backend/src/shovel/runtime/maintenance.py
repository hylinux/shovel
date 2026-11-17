from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from shovel.config.settings import AppSettings
from shovel.db.base import now_ts
from shovel.db.model.execution import Job
from shovel.db.model.retrieval import DeletionOutbox, RetrievalLog
from shovel.db.model.runtime import Operation
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.enums import JobState
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.schemas import HistoryPruneInput, IndexOptimizeInput

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


def knowledge_index_identity(settings: AppSettings) -> str:
    identity = [
        settings.database.url,
        str(Path(settings.zvec.knowledge_path).expanduser().resolve()),
        settings.zvec.dim,
        settings.zvec.chunk_collection,
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


async def optimize_index(runtime: Runtime, operation: Operation) -> dict[str, Any]:
    request = IndexOptimizeInput.model_validate(
        {key: value for key, value in operation.payload.items() if key != "index_identity"}
    )
    if operation.payload.get("index_identity") != knowledge_index_identity(runtime.settings):
        raise RuntimeFailure(
            "Knowledge index identity changed; submit a new maintenance request.", status=409
        )

    async def checkpoint(result: dict[str, Any]) -> None:
        async with sqlite_write(runtime.sessions, durable=True) as session:
            row = await session.get(Operation, operation.id)
            if row is None:
                raise NotFound("操作", operation.id)
            row.result = result
            await session.commit()

    async def dispatch(before: dict[str, Any]) -> None:
        if runtime.stopping.is_set():
            raise RuntimeFailure("Host is stopping; knowledge maintenance was not dispatched.", status=503)
        await checkpoint({"phase": "dispatched", "concurrency": request.concurrency, "before": before})

    with runtime.knowledge_maintenance(operation.id):
        await checkpoint({"phase": "draining", "concurrency": request.concurrency})
        sink = await runtime.sink(maintenance_operation_id=operation.id)
        result = await sink.optimize(concurrency=request.concurrency, on_dispatch=dispatch)
        result["phase"] = "completed"
        await checkpoint(result)
        return result


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
