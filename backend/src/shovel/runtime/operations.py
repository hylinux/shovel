from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, text, update

from shovel.db.base import new_id, now_ts
from shovel.db.model.config import Resource
from shovel.db.model.execution import Job, JobEvent
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.model.runtime import Operation
from shovel.domain.enums import ErrorKind, JobState, LogLevel, ResourceState
from shovel.exceptions.base import ShovelError
from shovel.exceptions.resource import DuplicateResourceNameError
from shovel.pipeline.cleanup import IndexCleanupService
from shovel.pipeline.embedder import build_embedder
from shovel.pipeline.scan_service import ScanOutcome, ScanService
from shovel.pipeline.sink import NullVectorSink
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.schemas import ResourceInput, ResourceUpdate, ScanInput
from shovel.services import connector_registry
from shovel.services.credentials import CredentialRef
from shovel.services.resource_service import ResourceDraft

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime

TERMINAL = frozenset({"succeeded", "completed_with_errors", "failed", "cancelled"})


class OperationService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._submit_lock = asyncio.Lock()
        self._log = structlog.get_logger("shovel.operations")

    async def get(self, identifier: str) -> Operation:
        async with self.runtime.sessions() as session:
            operation = await session.get(Operation, identifier)
        if operation is None:
            raise NotFound("操作", identifier)
        return operation

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        resource_id: str | None = None,
    ) -> Operation:
        if self.runtime.stopping.is_set():
            raise RuntimeFailure("Host 正在停止,暂不接受新任务。", status=503)
        operation = Operation(kind=kind, payload=payload, resource_id=resource_id)
        async with self.runtime.sessions() as session:
            session.add(operation)
            await session.commit()
        self.runtime.wake.set()
        return operation

    async def create_resource(self, request: ResourceInput) -> Operation:
        spec = connector_registry.get_spec(request.connector_kind)
        spec.config_model.model_validate(request.config)
        if request.identity_ref:
            CredentialRef.parse(request.identity_ref)
        if spec.requires_identity and not request.identity_ref:
            raise RuntimeFailure("此连接器需要 identity_ref;请先安全保存凭据。")
        if request.scan and request.embed and not self.runtime.settings.embedding.is_configured:
            raise RuntimeFailure("首次扫描请求了 embedding,但 [embedding] 尚未配置。")
        async with self._submit_lock, self.runtime.sessions() as session:
            duplicate = await session.scalar(select(Resource.id).where(Resource.name == request.name))
            pending = await session.scalars(
                select(Operation).where(
                    Operation.kind == "resource.create", Operation.state.in_(("queued", "running"))
                )
            )
            if duplicate or any(item.payload["name"] == request.name for item in pending):
                raise DuplicateResourceNameError(request.name)
            operation = Operation(
                kind="resource.create",
                resource_id=new_id("res"),
                payload=request.model_dump(mode="json"),
            )
            session.add(operation)
            await session.commit()
        self.runtime.wake.set()
        return operation

    async def scan(
        self,
        ref: str,
        request: ScanInput,
        *,
        operation_id: str | None = None,
    ) -> Operation:
        if operation_id is not None:
            async with self.runtime.sessions() as session:
                previous = await session.get(Operation, operation_id)
            if previous is not None:
                return previous
        if request.embed and not self.runtime.settings.embedding.is_configured:
            raise RuntimeFailure(
                "尚未配置 embedding 模型。",
                hint="配置 [embedding],或使用 embed=false / --no-embed。",
            )
        job = await ScanService(self.runtime.sessions, self.runtime.resources).prepare_job(
            ref, mode=request.mode, profile_name=request.profile_name, target_ids=request.target_ids
        )
        if self.runtime.stopping.is_set():
            raise RuntimeFailure("Host 正在停止。", status=503)
        async with self.runtime.sessions() as session:
            session.add(job)
            await session.flush()
            operation = Operation(
                kind="scan",
                resource_id=job.resource_id,
                job_id=job.id,
                payload=request.model_dump(mode="json"),
            )
            if operation_id is not None:
                operation.id = operation_id
            session.add(operation)
            await session.commit()
        self.runtime.wake.set()
        return operation

    async def resource_action(
        self, ref: str, action: str, payload: dict[str, Any] | None = None
    ) -> Operation:
        if action not in {"verify", "pause", "resume", "revoke", "delete", "update"}:
            raise RuntimeFailure(f"不支持的资源操作: {action}")
        resource = await self.runtime.resources.resolve(ref)
        data = dict(payload or {})
        if action == "update":
            request = ResourceUpdate.model_validate(data)
            if request.config is not None:
                connector_registry.get_spec(resource.connector_kind).config_model.model_validate(
                    request.config
                )
        if action in {"pause", "revoke", "delete", "update"}:
            async with self.runtime.sessions() as session:
                await session.execute(
                    update(Job)
                    .where(Job.resource_id == resource.id, Job.state.in_((JobState.QUEUED, JobState.RUNNING)))
                    .values(is_cancel_requested=True)
                )
                await session.commit()
        return await self.enqueue(f"resource.{action}", data, resource_id=resource.id)

    async def cancel_job(self, job_id: str) -> Job:
        async with self.runtime.sessions() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise NotFound("扫描作业", job_id)
            if not job.state.is_terminal:
                job.is_cancel_requested = True
                if job.state == JobState.QUEUED:
                    job.state = JobState.CANCELLED
                    job.finished_at = now_ts()
                    await session.execute(
                        update(Operation)
                        .where(Operation.job_id == job_id, Operation.state == "queued")
                        .values(state="cancelled", finished_at=now_ts())
                    )
                await session.commit()
            return job

    async def retry(self, identifier: str) -> Operation:
        async with self.runtime.sessions() as session:
            operation = await session.get(Operation, identifier)
            if operation is None:
                raise NotFound("操作", identifier)
            if operation.state not in {"failed", "cancelled", "completed_with_errors"}:
                raise RuntimeFailure("只能重试失败、已取消或部分失败的操作。", status=409)
            operation.state = "queued"
            operation.error = None
            operation.finished_at = None
            if operation.job_id:
                job = await session.get(Job, operation.job_id)
                if job is None:
                    raise RuntimeFailure("关联资源或扫描作业已删除,无法重试。", status=409)
                job.state = JobState.QUEUED
                job.is_cancel_requested = False
            await session.commit()
        self.runtime.wake.set()
        return operation

    async def recover(self) -> None:
        async with self.runtime.sessions() as session:
            interrupted = list(await session.scalars(select(Operation).where(Operation.state == "running")))
            for operation in interrupted:
                job = await session.get(Job, operation.job_id) if operation.job_id else None
                interrupted_shutdown = (
                    job is not None and job.state == JobState.CANCELLED and not job.is_cancel_requested
                )
                if job is not None and job.state.is_terminal and not interrupted_shutdown:
                    operation.state = job.state.value
                    operation.error = self.runtime.redact(job.error_detail) if job.error_detail else None
                    resource = await session.get(Resource, job.resource_id)
                    outcome = ScanOutcome(
                        job_id=job.id,
                        state=job.state,
                        scan_mode=job.scan_mode,
                        resource_name=resource.name if resource else job.resource_id,
                        error_kind=job.error_kind,
                        error_detail=job.error_detail,
                        vectors_written=int(job.stats.get("vectors_written", 0)),
                        tombstoned=int(job.stats.get("tombstoned", 0)),
                        warnings=list(job.stats.get("warnings", [])),
                    )
                    for name in (
                        "discovered",
                        "skipped_unchanged",
                        "fetched",
                        "parsed",
                        "chunked",
                        "embedded",
                        "failed_docs",
                        "chunks_written",
                        "bytes_read",
                    ):
                        setattr(outcome, name, getattr(job, name))
                    operation.result = asdict(outcome)
                    operation.finished_at = now_ts()
                else:
                    operation.state = "queued"
                    operation.error = "上次 Host 非正常退出,等待恢复执行。"
                    if job is not None:
                        job.state = JobState.QUEUED
                        session.add(
                            JobEvent(
                                job_id=job.id,
                                level=LogLevel.WARN,
                                code="JOB_RECOVERED",
                                message="Host 重启后恢复同一个扫描作业。",
                            )
                        )
            await session.execute(
                update(Resource)
                .where(Resource.state == ResourceState.VERIFYING)
                .values(state=ResourceState.DEGRADED, state_reason="上次验证被中断,需要重新验证。")
            )
            referenced_jobs = select(Operation.job_id).where(Operation.job_id.is_not(None))
            await session.execute(
                update(Job)
                .where(Job.state == JobState.RUNNING, Job.id.not_in(referenced_jobs))
                .values(
                    state=JobState.FAILED,
                    finished_at=now_ts(),
                    error_kind=ErrorKind.INTERNAL,
                    error_detail="旧版扫描被中断且缺少持久请求参数,请重新提交扫描。",
                )
            )
            pending_cleanup = await session.scalar(
                select(DeletionOutbox.id).where(DeletionOutbox.done_at.is_(None)).limit(1)
            )
            queued_cleanup = await session.scalar(
                select(Operation.id)
                .where(Operation.kind == "cleanup", Operation.state.in_(("queued", "running")))
                .limit(1)
            )
            if pending_cleanup is not None and queued_cleanup is None:
                session.add(Operation(kind="cleanup", payload={}))
            await session.commit()

    async def run_once(self) -> bool:
        async with self.runtime.sessions() as session:
            identifier = await session.scalar(
                select(Operation.id)
                .where(Operation.state == "queued")
                .order_by(Operation.queued_at, text("operation.rowid"))
                .limit(1)
            )
            if identifier is None:
                return False
            claimed = await session.execute(
                update(Operation)
                .where(Operation.id == identifier, Operation.state == "queued")
                .values(state="running", started_at=now_ts(), attempts=Operation.attempts + 1)
                .returning(Operation.id)
            )
            if claimed.scalar_one_or_none() is None:
                return True
            await session.commit()
        operation = await self.get(identifier)
        result: dict[str, Any] = {}
        state = "succeeded"
        error: str | None = None
        propagate_cancellation = False
        try:
            result, state = await self._execute(operation)
            if state in {"failed", "completed_with_errors"}:
                error = self.runtime.redact(
                    str(
                        result.get("error_detail")
                        or result.get("message")
                        or f"部分文档处理失败 ({result.get('failed_docs', 0)})。"
                    )
                )
        except asyncio.CancelledError:
            task = asyncio.current_task()
            propagate_cancellation = bool(task and task.cancelling())
            state = "queued" if self.runtime.stopping.is_set() else "cancelled"
        except Exception as exc:
            error = self.runtime.redact(
                exc.message if isinstance(exc, ShovelError) else f"{type(exc).__name__}: {exc}"
            )
            state = "failed"
            self._log.error("operation.failed", operation_id=identifier, kind=operation.kind, error=error)
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            if row is not None:
                row.state = state
                row.result = {**row.result, **result}
                row.error = error
                row.finished_at = None if state == "queued" else now_ts()
            if operation.job_id:
                job = await session.get(Job, operation.job_id)
                if job is not None:
                    if state == "queued" and not job.is_cancel_requested:
                        job.state = JobState.QUEUED
                        job.finished_at = None
                    elif state == "failed" and not job.state.is_terminal:
                        job.state = JobState.FAILED
                        job.error_kind = ErrorKind.INTERNAL
                        job.error_detail = error
                        job.finished_at = now_ts()
                    elif job.is_cancel_requested and state == "queued":
                        if row is not None:
                            row.state = "cancelled"
                            row.finished_at = now_ts()
                        job.state = JobState.CANCELLED
                        job.finished_at = now_ts()
            await session.commit()
        if propagate_cancellation:
            raise asyncio.CancelledError
        return True

    async def _execute(self, operation: Operation) -> tuple[dict[str, Any], str]:
        resources = self.runtime.resources
        ref = operation.resource_id or ""
        if operation.kind == "scan":
            request = ScanInput.model_validate(operation.payload)
            embedder = build_embedder(
                self.runtime.settings.embedding,
                expected_dimension=self.runtime.settings.zvec.dim,
                enabled=request.embed,
            )
            try:
                sink = await self.runtime.sink() if request.embed else NullVectorSink()
                outcome = await ScanService(
                    self.runtime.sessions,
                    resources,
                    embedder=embedder,
                    sink=sink,
                    stopping=self.runtime.stopping,
                ).scan(
                    ref,
                    mode=request.mode,
                    profile_name=request.profile_name,
                    target_ids=request.target_ids,
                    limit=request.limit,
                    force=request.force,
                    job_id=operation.job_id,
                )
                result = asdict(outcome)
                if outcome.error_detail:
                    result["error_detail"] = self.runtime.redact(outcome.error_detail)
                return result, outcome.state.value
            finally:
                await embedder.aclose()
        if operation.kind == "resource.create":
            creation = ResourceInput.model_validate(operation.payload)
            async with self.runtime.sessions() as session:
                existing = await session.get(Resource, ref)
            resource = existing or await resources.create(
                ResourceDraft(
                    name=creation.name,
                    connector_kind=creation.connector_kind,
                    config=creation.config,
                    description=creation.description,
                    sensitivity=creation.sensitivity,
                    authority=creation.authority,
                    identity_ref=creation.identity_ref,
                ),
                resource_id=ref,
            )
            result = {"resource_id": resource.id}
            if creation.verify:
                verification = await resources.verify(ref)
                result["verification"] = asdict(verification)
                if not verification.ok:
                    return {
                        **result,
                        "verified": False,
                        "message": verification.message,
                    }, "completed_with_errors"
            if creation.scan:
                child = await self.scan(
                    ref,
                    ScanInput(embed=creation.embed),
                    operation_id=f"{operation.id}_scan",
                )
                result.update({"scan_operation_id": child.id, "job_id": child.job_id or ""})
            return result, "succeeded"
        if operation.kind == "cleanup":
            count = await IndexCleanupService(self.runtime.sessions, await self.runtime.sink()).drain(
                resource_id=operation.payload.get("resource_id"),
            )
            return {"completed": count}, "succeeded"
        if operation.kind == "resource.delete":
            async with self.runtime.sessions() as session:
                deleted_resource = await session.get(Resource, ref)
            if deleted_resource is not None:
                await resources.delete(ref)
            async with self.runtime.sessions() as session:
                await session.execute(
                    update(Operation)
                    .where(Operation.id == operation.id)
                    .values(result={"resource_id": ref, "metadata_deleted": True, "cleanup_complete": False})
                )
                await session.commit()
            completed = await IndexCleanupService(self.runtime.sessions, await self.runtime.sink()).drain(
                resource_id=ref
            )
            return {
                "resource_id": ref,
                "metadata_deleted": True,
                "cleanup_complete": True,
                "completed": completed,
            }, "succeeded"
        if operation.kind == "resource.verify":
            verification = await resources.verify(ref)
            return asdict(verification), "succeeded" if verification.ok else "completed_with_errors"
        if operation.kind == "resource.update":
            change = ResourceUpdate.model_validate(operation.payload)
            if change.config is not None:
                await resources.update_config(ref, change.config)
            await resources.update_metadata(
                ref,
                description=change.description,
                sensitivity=change.sensitivity,
                authority=change.authority,
            )
            return {"resource_id": ref}, "succeeded"
        target_states = {
            "resource.pause": ResourceState.PAUSED,
            "resource.resume": ResourceState.ACTIVE,
            "resource.revoke": ResourceState.REVOKED,
        }
        if operation.kind in target_states:
            current = await resources.get(ref)
            if current.state != target_states[operation.kind]:
                if operation.kind == "resource.pause":
                    await resources.pause(ref, operation.payload.get("reason"))
                elif operation.kind == "resource.resume":
                    await resources.resume(ref)
                else:
                    await resources.revoke(ref, operation.payload.get("reason"))
            return {"resource_id": ref}, "succeeded"
        raise RuntimeFailure(f"未知后台操作: {operation.kind}")
