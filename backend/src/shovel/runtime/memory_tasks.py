from __future__ import annotations

import asyncio
from time import perf_counter
from typing import TYPE_CHECKING

from anyio import CancelScope
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shovel.db.base import now_ts
from shovel.db.model.runtime import ChatMessage, MemoryEpoch, Operation
from shovel.runtime.diagnostics import trace, trace_context
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.memory import ExtractionCancelled
from shovel.runtime.requests import request_fingerprint, request_operation_id

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


class MemoryTaskService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._execute_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._active: Operation | None = None

    async def generation(self, session: AsyncSession) -> int:
        value = await session.scalar(select(MemoryEpoch.generation).where(MemoryEpoch.id == 1))
        if value is None:
            raise RuntimeFailure("Memory 任务版本尚未初始化。", status=503)
        return value

    async def enqueue(
        self, session: AsyncSession, user: ChatMessage, *, generation: int, run_id: str | None
    ) -> str:
        key = f"memory.extract:{user.id}:v1"
        payload = {
            "user_message_id": user.id,
            "conversation_id": user.conversation_id,
            "workspace_id": self.runtime.memory.scope()["agent_id"],
            "generation": generation,
            "run_id": run_id,
        }
        current = generation == await self.generation(session)
        operation = Operation(
            id=request_operation_id(key),
            kind="memory.extract",
            request_key=key,
            request_fingerprint=request_fingerprint("memory.extract", payload, None),
            payload=payload,
            state="queued" if current else "cancelled",
            error=None if current else "Memory 任务因忘记操作失效。",
            finished_at=None if current else now_ts(),
        )
        previous = await session.get(Operation, operation.id)
        if previous is not None:
            if previous.request_fingerprint != operation.request_fingerprint:
                raise RuntimeFailure("记忆任务的源消息已用于不同的提取参数。", status=409)
            return previous.id
        session.add(operation)
        return operation.id

    async def invalidate(self, *, conversation_id: str | None = None) -> None:
        async with self._control_lock:
            if self._active is not None and (
                conversation_id is None or self._active.payload["conversation_id"] == conversation_id
            ):
                self.runtime.memory.cancel_extraction()
            async with self.runtime.sessions() as session:
                condition = [
                    Operation.kind == "memory.extract",
                    Operation.state.in_(("queued", "running", "failed")),
                ]
                if conversation_id is None:
                    await session.execute(
                        update(MemoryEpoch)
                        .where(MemoryEpoch.id == 1)
                        .values(generation=MemoryEpoch.generation + 1)
                    )
                else:
                    condition.append(
                        func.json_extract(Operation.payload, "$.conversation_id") == conversation_id
                    )
                await session.execute(
                    update(Operation)
                    .where(*condition)
                    .values(
                        state="cancelled", finished_at=now_ts(), error="Memory 任务因删除或忘记操作失效。"
                    )
                )
                await session.commit()

    async def retry(self, operation: Operation) -> Operation:
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, operation.id)
            assert row is not None
            if row.state != "failed":
                raise RuntimeFailure("只能重试失败的记忆任务。", status=409)
            if row.result.get("write_started"):
                raise RuntimeFailure("任务可能已部分写入,请先在 Settings 检查记忆;不能自动重放。", status=409)
            if row.payload["generation"] != await self.generation(session):
                raise RuntimeFailure("任务已因忘记操作失效。", status=409)
            if await self._newer_written(session, row.id):
                raise RuntimeFailure("较新的记忆任务已写入,不能重放旧事实。", status=409)
            if await session.get(ChatMessage, row.payload["user_message_id"]) is None:
                raise RuntimeFailure("任务的源消息已删除。", status=409)
            row.state, row.error, row.finished_at = "queued", None, None
            row.result = {}
            await session.commit()
        self.runtime.memory_wake.set()
        return row

    async def _before_write(self, identifier: str, *, write: bool = True) -> None:
        if self.runtime.stopping.is_set():
            raise ExtractionCancelled()
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            if (
                row is None
                or row.state != "running"
                or row.payload["generation"] != await self.generation(session)
                or await session.get(ChatMessage, row.payload["user_message_id"]) is None
                or await self._newer_written(session, identifier)
            ):
                raise ExtractionCancelled()
            if write and not row.result.get("write_started"):
                row.result = {**row.result, "write_started": True}
                await session.commit()

    async def _newer_written(self, session: AsyncSession, identifier: str) -> bool:
        return (
            await session.scalar(
                text(
                    "SELECT id FROM operation WHERE kind = 'memory.extract' "
                    "AND rowid > (SELECT rowid FROM operation WHERE id = :id) "
                    "AND json_extract(result, '$.write_started') = 1 LIMIT 1"
                ).bindparams(id=identifier)
            )
            is not None
        )

    async def run_once(self) -> bool:
        async with self._execute_lock:
            async with self.runtime.sessions() as session:
                operation = await session.scalar(
                    select(Operation)
                    .where(Operation.kind == "memory.extract", Operation.state == "queued")
                    .order_by(Operation.queued_at, text("operation.rowid"))
                    .limit(1)
                )
                if operation is None:
                    return False
                if operation.result.get("retry_at", 0) > now_ts():
                    return False
                claimed = await session.execute(
                    update(Operation)
                    .where(Operation.id == operation.id, Operation.state == "queued")
                    .values(state="running", started_at=now_ts(), attempts=Operation.attempts + 1)
                    .returning(Operation.id)
                )
                if claimed.scalar_one_or_none() is None:
                    return True
                await session.commit()
                await session.refresh(operation)
            self._active = operation
            started = perf_counter()
            state, error = "succeeded", None
            cancelled = False
            try:
                async with self.runtime.sessions() as session:
                    source = await session.get(ChatMessage, operation.payload["user_message_id"])
                    if (
                        source is None
                        or source.role != "user"
                        or source.conversation_id != operation.payload["conversation_id"]
                        or operation.payload["workspace_id"] != self.runtime.memory.scope()["agent_id"]
                        or operation.payload["generation"] != await self.generation(session)
                    ):
                        raise ExtractionCancelled()
                loop = asyncio.get_running_loop()

                def before_write() -> None:
                    asyncio.run_coroutine_threadsafe(self._before_write(operation.id), loop).result()

                def validate() -> None:
                    asyncio.run_coroutine_threadsafe(
                        self._before_write(operation.id, write=False), loop
                    ).result()

                with trace_context(
                    operation_id=operation.id,
                    run_id=operation.payload.get("run_id"),
                    conversation_id=source.conversation_id,
                ):
                    trace("memory.extraction.started", attempt=operation.attempts)
                    await self.runtime.memory.extract(
                        source.content,
                        operation_id=operation.id,
                        before_write=before_write,
                        validate=validate,
                    )
            except ExtractionCancelled:
                state = "queued" if self.runtime.stopping.is_set() else "cancelled"
                error = "Memory 任务已停止或失效。"
            except asyncio.CancelledError:
                state, error, cancelled = "queued", "Host 停止,任务等待恢复。", True
            except Exception as exc:
                state = "queued" if self.runtime.stopping.is_set() else "failed"
                error = f"长期记忆提取失败 ({type(exc).__name__})。"
            finally:
                self._active = None
            with CancelScope(shield=True):
                await self._finish(operation.id, state, error)
            async with self.runtime.sessions() as session:
                row = await session.get(Operation, operation.id)
                assert row is not None
            trace(
                "memory.extraction.completed",
                operation_id=operation.id,
                state=row.state,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            )
            if cancelled:
                raise asyncio.CancelledError
            return True

    async def _finish(self, identifier: str, state: str, error: str | None) -> None:
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            assert row is not None
            if row.state != "running":
                return
            if state == "queued" and row.result.get("write_started"):
                state, error = "failed", "任务被中断且可能已部分写入,请检查记忆。"
            if state == "failed" and not row.result.get("write_started") and row.attempts < 3:
                state = "queued"
                row.result = {**row.result, "retry_at": now_ts() + 2**row.attempts}
            row.state, row.error = state, error
            row.finished_at = None if state == "queued" else now_ts()
            await session.commit()
