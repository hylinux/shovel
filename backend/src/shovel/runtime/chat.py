from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from time import perf_counter
from typing import TYPE_CHECKING

import structlog
from ag_ui.core import BaseEvent, CustomEvent, EventType, RunErrorEvent, RunFinishedEvent, RunStartedEvent
from agent_framework import Message
from anyio import CancelScope
from sqlalchemy import func, select, update

from shovel.db.base import now_ts
from shovel.db.model.runtime import ChatMessage, Conversation
from shovel.runtime.agent import EventSink, ShovelAgent
from shovel.runtime.diagnostics import trace, trace_context
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.schemas import MessageInput

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


class ChatService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.agent = ShovelAgent(runtime)
        self._locks: dict[str, asyncio.Lock] = {}
        self._log = structlog.get_logger("shovel.chat")
        self._active_runs = 0
        self._restarting = False
        self._restart_failed = False

    @property
    def agent_state(self) -> str:
        if self._restarting:
            return "restarting"
        if self._restart_failed:
            return "restart_failed"
        return "loaded" if self.agent.extensions.mcp_revision is not None else "not_loaded"

    async def restart_agent(self) -> None:
        if self.runtime.stopping.is_set():
            raise RuntimeFailure("Host 正在停止。", status=503)
        if self._active_runs or self._restarting:
            raise RuntimeFailure("Agent 正在处理会话或重启,请完成后重试。", status=409)
        # Admission and the run counter change without awaits on the Host event loop.
        self._restarting = self._restart_failed = True
        trace("agent.restart.started")
        try:
            await self.agent.close()
            self.agent = ShovelAgent(self.runtime)
            self._restart_failed = False
            trace("agent.restart.completed")
        except Exception as exc:
            self._log.error("agent.restart.failed", error_type=type(exc).__name__)
            raise RuntimeFailure(
                "Agent 重启失败,请重试;如果仍失败请重启 Host。",
                status=503,
            ) from exc
        finally:
            self._restarting = False

    async def recover(self) -> None:
        async with self.runtime.sessions() as session:
            await session.execute(
                update(ChatMessage)
                .where(ChatMessage.state == "pending")
                .values(state="failed", content="上次 Host 退出中断了模型请求,请重新发送。")
            )
            await session.commit()

    async def conversation(self, identifier: str) -> Conversation:
        async with self.runtime.sessions() as session:
            result = await session.get(Conversation, identifier)
        if result is None:
            raise NotFound("对话", identifier)
        return result

    async def maintain(self, identifier: str, *, title: str | None = None) -> None:
        lock = self._locks.setdefault(identifier, asyncio.Lock())
        if lock.locked():
            raise RuntimeFailure("此对话正在生成回答,完成后才能修改或删除。", status=409)
        async with lock, self.runtime.sessions() as session:
            conversation = await session.get(Conversation, identifier)
            if conversation is None:
                raise NotFound("对话", identifier)
            if title is None:
                await self.runtime.memory_tasks.invalidate(conversation_id=identifier)
                if conversation.has_short_memory:
                    await self.runtime.memory.forget_conversation(identifier)
                await session.delete(conversation)
            else:
                conversation.title = title
                conversation.updated_at = now_ts()
            await session.commit()

    async def messages(
        self, identifier: str, *, limit: int = 100, before: int | None = None
    ) -> list[ChatMessage]:
        await self.conversation(identifier)
        async with self.runtime.sessions() as session:
            statement = select(ChatMessage).where(ChatMessage.conversation_id == identifier)
            if before is not None:
                statement = statement.where(ChatMessage.sequence < before)
            rows = list(await session.scalars(statement.order_by(ChatMessage.sequence.desc()).limit(limit)))
            return list(reversed(rows))

    async def send(
        self,
        identifier: str,
        request: MessageInput,
        *,
        run_id: str | None = None,
        on_event: EventSink | None = None,
    ) -> ChatMessage:
        await self.conversation(identifier)
        if not self.runtime.settings.model.is_configured:
            raise RuntimeFailure("尚未配置对话模型,请设置 [model];关键词检索仍可使用。", status=503)
        if request.memory_enabled and not self.runtime.memory.configured:
            raise RuntimeFailure("本次对话启用了 Memory,但 Memory 尚未配置。", status=503)
        if self.runtime.stopping.is_set():
            raise RuntimeFailure("Host 正在停止。", status=503)
        lock = self._locks.setdefault(identifier, asyncio.Lock())
        if lock.locked():
            raise RuntimeFailure("此对话正在生成回答,请等待完成后再发送。", status=409)
        async with lock:
            if self._restarting or self._restart_failed:
                raise RuntimeFailure("Agent 正在重启或重启失败,请稍后重试重启。", status=503)
            self._active_runs += 1
            try:
                with trace_context(conversation_id=identifier, run_id=run_id):
                    return await self._send(identifier, request, run_id=run_id, on_event=on_event)
            finally:
                self._active_runs -= 1

    async def stream(self, identifier: str, request: MessageInput, run_id: str) -> AsyncGenerator[BaseEvent]:
        # Backpressure bounds memory while retaining cancellation and durable failure handling.
        queue: asyncio.Queue[BaseEvent | None] = asyncio.Queue(maxsize=32)

        async def emit(event: BaseEvent) -> None:
            if event.type != EventType.RUN_STARTED:
                await queue.put(event)

        async def generate() -> None:
            with trace_context(conversation_id=identifier, run_id=run_id):
                trace("agent.stream.started")
                try:
                    saved = await self.send(identifier, request, run_id=run_id, on_event=emit)
                    from shovel.runtime.api import message_view

                    await queue.put(CustomEvent(name="shovel.message", value=message_view(saved)))
                    await queue.put(RunFinishedEvent(thread_id=identifier, run_id=run_id))
                    trace("agent.stream.completed", message_id=saved.id)
                except asyncio.CancelledError:
                    trace("agent.stream.cancelled")
                    raise
                except Exception as exc:
                    trace("agent.stream.failed", error_type=type(exc).__name__)
                    message = (
                        exc.message if isinstance(exc, RuntimeFailure) else "Agent 执行失败,请检查服务日志。"
                    )
                    if not isinstance(exc, RuntimeFailure):
                        self._log.error("agent.stream.failed", error_type=type(exc).__name__)
                    await queue.put(RunErrorEvent(message=self.runtime.redact(message)))
                await queue.put(None)

        yield RunStartedEvent(thread_id=identifier, run_id=run_id)
        task = asyncio.create_task(generate())
        try:
            while (event := await queue.get()) is not None:
                yield event
            await task
        finally:
            if not task.done():
                task.cancel()
            with CancelScope(shield=True), suppress(asyncio.CancelledError):
                await task

    async def _send(
        self,
        identifier: str,
        request: MessageInput,
        *,
        run_id: str | None = None,
        on_event: EventSink | None = None,
    ) -> ChatMessage:
        started = perf_counter()
        trace("chat.started", memory_enabled=request.memory_enabled, input_chars=len(request.content))
        persistence_started = perf_counter()
        async with self.runtime.sessions() as session:
            conversation = await session.get(Conversation, identifier)
            if conversation is None:
                raise NotFound("对话", identifier)
            generation = await self.runtime.memory_tasks.generation(session)
            history = list(
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == identifier, ChatMessage.state == "completed")
                    .order_by(ChatMessage.sequence.desc())
                    .limit(20)
                )
            )
            sequence = int(
                await session.scalar(
                    select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
                        ChatMessage.conversation_id == identifier
                    )
                )
                or 0
            )
            user = ChatMessage(
                conversation_id=identifier, role="user", content=request.content, sequence=sequence + 1
            )
            answer = ChatMessage(
                conversation_id=identifier,
                role="assistant",
                content="",
                state="pending",
                sequence=sequence + 2,
            )
            session.add_all([user, answer])
            if conversation.title == "新对话":
                conversation.title = request.content[:60]
            conversation.updated_at = now_ts()
            if request.memory_enabled:
                conversation.has_short_memory = True
            await session.commit()
        trace(
            "chat.pending.persisted",
            user_message_id=user.id,
            assistant_message_id=answer.id,
            history_messages=len(history),
            elapsed_ms=round((perf_counter() - persistence_started) * 1000, 2),
        )
        try:
            result = await self.agent.respond(
                identifier,
                request,
                [Message(message.role, [message.content]) for message in reversed(history)],
                run_id=run_id,
                on_event=on_event,
            )
            persistence_started = perf_counter()
            async with self.runtime.sessions() as session:
                saved = await session.get(ChatMessage, answer.id)
                assert saved is not None
                saved.state, saved.content = "completed", result.content
                saved.citations, saved.warnings = result.sources, result.warnings
                if result.extract_long_term:
                    saved.memory_operation_id = await self.runtime.memory_tasks.enqueue(
                        session, user, generation=generation, run_id=run_id
                    )
                await session.execute(
                    update(Conversation).where(Conversation.id == identifier).values(updated_at=now_ts())
                )
                await session.commit()
                if saved.memory_operation_id:
                    self.runtime.memory_wake.set()
                    trace("memory.extraction.queued", operation_id=saved.memory_operation_id)
                trace(
                    "chat.answer.persisted",
                    message_id=saved.id,
                    output_chars=len(saved.content),
                    elapsed_ms=round((perf_counter() - persistence_started) * 1000, 2),
                )
                trace(
                    "chat.completed",
                    citations=len(saved.citations),
                    warnings=len(saved.warnings),
                    elapsed_ms=round((perf_counter() - started) * 1000, 2),
                )
                return saved
        except BaseException as exc:
            trace("chat.failed", error_type=type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                failure_message = "模型请求被中断,请重新发送。"
            elif isinstance(exc, RuntimeFailure):
                failure_message = exc.message
            else:
                failure_message = f"对话处理失败 ({type(exc).__name__})。"
                self._log.error("chat.failed", conversation_id=identifier, error_type=type(exc).__name__)
            with CancelScope(shield=True):
                async with self.runtime.sessions() as session:
                    await session.execute(
                        update(ChatMessage)
                        .where(ChatMessage.id == answer.id)
                        .values(state="failed", content=self.runtime.redact(failure_message))
                    )
                    await session.commit()
                    trace("chat.failure.persisted", message_id=answer.id)
            raise

    async def close(self) -> None:
        await self.agent.close()
