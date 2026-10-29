from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from shovel.db.model.memory import MemoryBelief, SessionSummary
from shovel.db.model.runtime import ChatMessage, Conversation, Operation
from shovel.memory.domain import MemoryCandidate
from shovel.runtime.agent import AgentResult
from shovel.runtime.schemas import MessageInput
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


async def enqueue(runtime: Runtime, *, generation: int | None = None) -> str:
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.flush()
        user = ChatMessage(
            conversation_id=conversation.id, role="user", content="I prefer green tea.", sequence=1
        )
        session.add(user)
        await session.flush()
        identifier = await runtime.memory_tasks.enqueue(
            session,
            user,
            generation=generation
            if generation is not None
            else await runtime.memory_tasks.generation(session),
            run_id="background-memory-test",
        )
        assert (
            await runtime.memory_tasks.enqueue(
                session,
                user,
                generation=generation
                if generation is not None
                else await runtime.memory_tasks.generation(session),
                run_id="background-memory-test",
            )
            == identifier
        )
        await session.commit()
        return identifier


async def extraction(_text: str) -> list[MemoryCandidate]:
    return [
        MemoryCandidate(
            "User prefers green tea.", object="green tea", topic="drink", evidence="I prefer green tea."
        )
    ]


async def test_answer_and_memory_task_commit_or_rollback_together(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def respond(*_args: Any, **_kwargs: Any) -> AgentResult:
        return AgentResult(content="Answer", extract_long_term=True)

    monkeypatch.setattr(runtime.chat.agent, "respond", respond)
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    answer = await runtime.chat.send(conversation, MessageInput(content="I prefer green tea."))
    assert answer.state == "completed" and answer.memory_operation_id

    async def failed_enqueue(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("transaction aborted")

    monkeypatch.setattr(runtime.memory_tasks, "enqueue", failed_enqueue)
    with pytest.raises(ValueError):
        await runtime.chat.send(conversation, MessageInput(content="I prefer coffee."))
    messages = await runtime.chat.messages(conversation)
    assert messages[-1].state == "failed"
    async with runtime.sessions() as session:
        operations = list(await session.scalars(select(Operation).where(Operation.kind == "memory.extract")))
        assert len(operations) == 1


async def test_native_facts_outbox_and_receipt_are_atomic(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime.memory.extractor, "extract", extraction)
    identifier = await enqueue(runtime)
    assert await runtime.memory_tasks.run_once()
    operation = await runtime.operations.get(identifier)
    assert operation.state == "succeeded" and operation.result["committed"]
    assert len(await runtime.memory.list()) == 1
    assert await runtime.memory.repository.pending(runtime.memory.owner, force=True)
    await runtime.operations.recover()
    assert (await runtime.operations.get(identifier)).state == "succeeded"


async def test_atomic_consolidation_rolls_back_all_candidates_on_failure(runtime: Runtime) -> None:
    from shovel.memory.domain import Provenance

    valid = (await extraction(""))[0]
    invalid = MemoryCandidate("Invalid predicate value.", predicate="not-a-predicate", object="bad")
    with pytest.raises(ValueError):
        await runtime.memory.repository.consolidate(
            runtime.memory.owner, [valid, invalid], Provenance("manual")
        )
    assert await runtime.memory.list() == []
    assert not await runtime.memory.repository.pending(runtime.memory.owner, force=True)


@pytest.mark.parametrize("delete_source", [False, True])
async def test_forget_or_source_delete_fences_late_extraction(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, delete_source: bool
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow(text: str) -> list[MemoryCandidate]:
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            # A provider may finish even after client cancellation.
            await finish.wait()
        return await extraction(text)

    monkeypatch.setattr(runtime.memory.extractor, "extract", slow)
    identifier = await enqueue(runtime)
    worker = asyncio.create_task(runtime.memory_tasks.run_once())
    await entered.wait()
    if delete_source:
        operation = await runtime.operations.get(identifier)
        await runtime.memory_tasks.invalidate(conversation_id=operation.payload["conversation_id"])
        async with runtime.sessions() as session:
            source = await session.get(ChatMessage, operation.payload["user_message_id"])
            assert source is not None
            await session.delete(source)
            await session.commit()
    else:
        await runtime.memory_tasks.invalidate()
    finish.set()
    assert await asyncio.wait_for(worker, 3)
    assert (await runtime.operations.get(identifier)).state == "cancelled"
    assert await runtime.memory.list() == []


async def test_restart_recovers_uncommitted_native_extraction(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime.memory.extractor, "extract", extraction)
    identifier = await enqueue(runtime)
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None
        row.state = "running"
        await session.commit()
    await runtime.operations.recover()
    assert (await runtime.operations.get(identifier)).state == "queued"
    assert await runtime.memory_tasks.run_once()
    assert len(await runtime.memory.list()) == 1


async def test_extraction_failure_retries_without_lying_about_success(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def invalid(_text: str) -> list[MemoryCandidate]:
        raise ValueError("private-invalid-json")

    monkeypatch.setattr(runtime.memory.extractor, "extract", invalid)
    identifier = await enqueue(runtime)
    for _ in range(3):
        async with runtime.sessions() as session:
            operation = await session.get(Operation, identifier)
            assert operation is not None
            operation.result = {}
            await session.commit()
        assert await runtime.memory_tasks.run_once()
    operation = await runtime.operations.get(identifier)
    assert operation.state == "failed" and operation.attempts == 3
    assert "private-invalid-json" not in (operation.error or "")
    assert await runtime.memory.list() == []


async def test_forget_does_not_invalidate_other_workspace_tasks(runtime: Runtime) -> None:
    from shovel.runtime.memory import MemoryService
    from shovel.runtime.memory_tasks import MemoryTaskService

    identifier = await enqueue(runtime)
    settings = runtime.settings.model_copy(deep=True)
    settings.agent.workspace_path = settings.agent.workspace / "other"
    other = MemoryService(settings, runtime.sessions)
    await other.initialize()
    original = runtime.memory
    try:
        runtime.memory = other
        tasks = MemoryTaskService(runtime)
        async with runtime.sessions() as session:
            assert await tasks.generation(session) == 0
        await tasks.invalidate()
        async with runtime.sessions() as session:
            assert await tasks.generation(session) == 1
        assert (await runtime.operations.get(identifier)).state == "queued"
        assert not await tasks.run_once()
    finally:
        runtime.memory = original
        await other.close()
    async with runtime.sessions() as session:
        assert await runtime.memory_tasks.generation(session) == 0


async def test_summary_is_separate_and_deleted_with_conversation(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    runtime.settings.memory.history_messages = 4
    async with runtime.sessions() as session:
        for i in range(8):
            session.add(
                ChatMessage(
                    conversation_id=identifier,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}",
                    sequence=i + 1,
                )
            )
        await session.flush()
        user = await session.scalar(
            select(ChatMessage).where(ChatMessage.conversation_id == identifier, ChatMessage.sequence == 7)
        )
        assert user is not None
        operation_id = await runtime.memory_tasks.enqueue(
            session,
            user,
            generation=await runtime.memory_tasks.generation(session),
            run_id=None,
            kind="memory.summarize",
        )
        await session.commit()

    async def summarize(previous: str, messages: list[dict[str, str]], limit: int) -> str:
        assert previous == "" and [m["content"] for m in messages] == [f"Message {i}" for i in range(4)]
        return "Session decisions, not user facts."

    monkeypatch.setattr(runtime.memory.extractor, "summarize", summarize)
    assert await runtime.memory_tasks.run_once()
    assert (await runtime.operations.get(operation_id)).state == "succeeded"
    assert await runtime.memory.summary(identifier) == "Session decisions, not user facts."
    assert await runtime.memory.list() == []
    assert (await api.delete(f"/api/v1/conversations/{identifier}")).status_code == 200
    async with runtime.sessions() as session:
        assert await session.get(SessionSummary, identifier) is None
        assert not list(await session.scalars(select(MemoryBelief)))
