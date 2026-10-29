from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from anyio import CancelScope
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from shovel.db.base import now_ts
from shovel.db.model.runtime import ChatMessage, MemoryEpoch, Operation
from shovel.memory.domain import ExtractionCancelled
from shovel.runtime.diagnostics import trace, trace_context
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.requests import request_fingerprint, request_operation_id

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


class MemoryTaskService:
    """Reuse durable Operations; native mutations and receipts commit atomically."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._execute_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._active: Operation | None = None

    async def generation(self, session: AsyncSession) -> int:
        value = await session.scalar(
            select(MemoryEpoch.generation).where(
                MemoryEpoch.workspace_id == self.runtime.memory.owner.workspace_id,
                MemoryEpoch.user_id == self.runtime.memory.owner.user_id,
            )
        )
        if value is None:
            raise RuntimeFailure("Memory 任务版本尚未初始化。", status=503)
        return value

    async def enqueue(
        self,
        session: AsyncSession,
        user: ChatMessage,
        *,
        generation: int,
        run_id: str | None,
        kind: str = "memory.extract",
    ) -> str:
        key = f"{kind}:{user.id}:native-v1"
        payload = {
            "user_message_id": user.id,
            "conversation_id": user.conversation_id,
            "workspace_id": self.runtime.memory.owner.workspace_id,
            "user_id": self.runtime.memory.owner.user_id,
            "generation": generation,
            "run_id": run_id,
        }
        operation = Operation(
            id=request_operation_id(key),
            kind=kind,
            request_key=key,
            request_fingerprint=request_fingerprint(kind, payload, None),
            payload=payload,
            state="queued" if generation == await self.generation(session) else "cancelled",
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
                conversation_id is None or self._active.payload.get("conversation_id") == conversation_id
            ):
                self.runtime.memory.cancel_extraction()
            async with self.runtime.sessions() as session:
                condition: list[ColumnElement[bool]] = [
                    Operation.kind.like("memory.%"),
                    Operation.state.in_(("queued", "running", "failed")),
                    func.json_extract(Operation.payload, "$.workspace_id")
                    == self.runtime.memory.owner.workspace_id,
                    func.json_extract(Operation.payload, "$.user_id") == self.runtime.memory.owner.user_id,
                ]
                if conversation_id is None:
                    await session.execute(
                        update(MemoryEpoch)
                        .where(
                            MemoryEpoch.workspace_id == self.runtime.memory.owner.workspace_id,
                            MemoryEpoch.user_id == self.runtime.memory.owner.user_id,
                        )
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
                        state="cancelled",
                        finished_at=now_ts(),
                        error="Memory 任务因删除或忘记操作失效。",
                    )
                )
                await session.commit()

    async def retry(self, operation: Operation) -> Operation:
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, operation.id)
            assert row is not None
            if row.state != "failed":
                raise RuntimeFailure("只能重试失败的记忆任务。", status=409)
            if (
                row.payload["workspace_id"] != self.runtime.memory.owner.workspace_id
                or row.payload["user_id"] != self.runtime.memory.owner.user_id
            ):
                raise RuntimeFailure("此 Memory 任务属于其他 Scope。", status=404)
            if row.payload["generation"] != await self.generation(session):
                raise RuntimeFailure("任务已因忘记操作失效。", status=409)
            if (
                row.payload.get("user_message_id")
                and await session.get(ChatMessage, row.payload["user_message_id"]) is None
            ):
                raise RuntimeFailure("任务的源消息已删除。", status=409)
            if row.kind == "memory.extract" and await session.scalar(
                text(
                    "SELECT id FROM operation WHERE kind = 'memory.extract' AND state = 'succeeded' "
                    "AND rowid > (SELECT rowid FROM operation WHERE id = :id) LIMIT 1"
                ).bindparams(id=row.id)
            ):
                raise RuntimeFailure("较新的提取已完成,不能重放旧事实。", status=409)
            row.state, row.error, row.finished_at = "queued", None, None
            row.result = {}
            await session.commit()
        self.runtime.memory_wake.set()
        return row

    async def run_once(self, *, respect_foreground: bool = False) -> bool:
        if respect_foreground and self.runtime.performance.background_delay() > 0:
            return False
        async with self._execute_lock:
            async with self.runtime.sessions() as session:
                operation = await session.scalar(
                    select(Operation)
                    .where(
                        Operation.kind.like("memory.%"),
                        Operation.state == "queued",
                        func.json_extract(Operation.payload, "$.workspace_id")
                        == self.runtime.memory.owner.workspace_id,
                        func.json_extract(Operation.payload, "$.user_id")
                        == self.runtime.memory.owner.user_id,
                    )
                    .order_by(Operation.queued_at, text("operation.rowid"))
                    .limit(1)
                )
                if operation is None:
                    return await self.runtime.memory.maintain()
                if operation.result.get("retry_at", 0) > now_ts():
                    return await self.runtime.memory.maintain()
                operation.state, operation.started_at = "running", now_ts()
                operation.attempts += 1
                await session.commit()
            self._active = operation
            state, error, cancelled = "succeeded", None, False
            try:
                if respect_foreground:
                    await self.runtime.performance.wait_for_idle(self.runtime.stopping)
                async with self.runtime.sessions() as session:
                    if operation.payload[
                        "workspace_id"
                    ] != self.runtime.memory.owner.workspace_id or operation.payload[
                        "generation"
                    ] != await self.generation(session):
                        raise ExtractionCancelled()
                    source = (
                        await session.get(ChatMessage, operation.payload["user_message_id"])
                        if (operation.payload.get("user_message_id"))
                        else None
                    )
                    if operation.kind in ("memory.extract", "memory.summarize") and (
                        source is None
                        or source.role != "user"
                        or source.conversation_id != operation.payload["conversation_id"]
                    ):
                        raise ExtractionCancelled()
                with trace_context(
                    operation_id=operation.id,
                    run_id=operation.payload.get("run_id"),
                    conversation_id=operation.payload.get("conversation_id"),
                ):
                    if operation.kind == "memory.extract":
                        assert source is not None
                        await self.runtime.memory.extract(
                            source.content,
                            operation_id=operation.id,
                            generation=operation.payload["generation"],
                            message_id=source.id,
                            conversation_id=source.conversation_id,
                        )
                    elif operation.kind == "memory.summarize":
                        await self.runtime.memory.summarize(
                            operation.payload["conversation_id"],
                            operation.id,
                            operation.payload["generation"],
                        )
                    elif operation.kind == "memory.rebuild":
                        await self.runtime.memory.rebuild()
                    elif operation.kind in ("memory.create", "memory.update"):
                        await self.runtime.memory.write(operation)
                    else:
                        raise RuntimeFailure("未知的 Memory 操作。")
            except ExtractionCancelled:
                state, error = "cancelled", "Memory 来源或任务版本已失效。"
            except asyncio.CancelledError:
                cancelled = self.runtime.stopping.is_set()
                state = "queued" if cancelled else "cancelled"
                error = "Memory 任务已停止。"
            except Exception as exc:
                state = "failed"
                error = self.runtime.redact(
                    exc.message
                    if isinstance(exc, RuntimeFailure)
                    else f"Memory 处理失败 ({type(exc).__name__})。"
                )
            finally:
                self._active = None
            with CancelScope(shield=True):
                await self._finish(operation.id, state, error)
            trace("memory.operation.completed", operation_id=operation.id, state=state)
            if cancelled:
                raise asyncio.CancelledError
            return True

    async def _finish(self, identifier: str, state: str, error: str | None) -> None:
        async with self.runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            assert row is not None
            # Extraction/consolidation may already have committed the receipt with the facts.
            if row.state != "running":
                return
            if state == "failed" and row.attempts < 3:
                state = "queued"
                row.result = {"retry_at": now_ts() + 2**row.attempts}
            row.state, row.error = state, error
            row.finished_at = None if state == "queued" else now_ts()
            await session.commit()
