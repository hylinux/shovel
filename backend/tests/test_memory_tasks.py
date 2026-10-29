from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, text

from shovel.db.migrations import upgrade_database
from shovel.db.model.retrieval import SchemaMigration
from shovel.db.model.runtime import ChatMessage, Conversation, MemoryEpoch, Operation
from shovel.runtime.agent import AgentResult
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.memory import ExtractionCancelled
from shovel.runtime.schemas import MessageInput
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


async def real_memory(runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0"))
    import mem0.memory.main
    import mem0.memory.setup
    from mem0.utils.factory import EmbedderFactory, LlmFactory

    monkeypatch.setattr(mem0.memory.main, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(mem0.memory.main, "mem0_dir", str(tmp_path / "mem0"))
    monkeypatch.setattr(mem0.memory.setup, "mem0_dir", str(tmp_path / "mem0"))

    class Embedder:
        def embed(self, *_args: Any, **_kwargs: Any) -> list[float]:
            return [1.0, 0.0, 0.0, 0.0]

        def embed_batch(self, texts: list[str], *_args: Any) -> list[list[float]]:
            return [self.embed(text) for text in texts]

    class Llm:
        def generate_response(self, **_kwargs: Any) -> str:
            return json.dumps({"memory": [{"id": "0", "text": "User prefers tea.", "event": "ADD"}]})

    monkeypatch.setattr(EmbedderFactory, "create", lambda *_args, **_kwargs: Embedder())
    monkeypatch.setattr(LlmFactory, "create", lambda *_args, **_kwargs: Llm())
    runtime.settings.memory.dim = 4
    runtime.settings.memory.embedder.model = "test"
    runtime.settings.memory.qdrant.path = str(tmp_path / "memory")
    runtime.settings.memory.history_db_path = str(tmp_path / "history.db")


@pytest.mark.parametrize("cancel", [False, True])
async def test_blocked_real_mem0_extraction_does_not_block_recall_or_short_write(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    await real_memory(runtime, tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def generate(**_kwargs: Any) -> str:
        started.set()
        assert release.wait(10)
        return json.dumps({"memory": [{"id": "0", "text": "User prefers tea.", "event": "ADD"}]})

    monkeypatch.setattr(runtime.memory, "_generate", generate)
    writes: list[int] = []
    task = asyncio.create_task(
        runtime.memory.extract(
            "I prefer tea.",
            operation_id="extract-test",
            before_write=lambda: writes.append(threading.get_ident()),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 10)
        async with asyncio.timeout(3):
            await runtime.memory.remember("A new message", conversation_id="second", extract_long_term=False)
            rows = await runtime.memory.search("context", conversation_id="second")
            assert rows and rows[0]["memory"] == "A new message"
        assert not task.done()
        if cancel:
            runtime.memory.cancel_extraction()
        release.set()
        if cancel:
            with pytest.raises(ExtractionCancelled):
                await task
            assert writes == []
            assert await runtime.memory.list() == []
        else:
            await task
            assert (await runtime.memory.list())[0]["memory"] == "User prefers tea."
            assert writes and set(writes) == {runtime.memory._owner_thread}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def enqueue(runtime: Runtime, *, generation: int | None = None) -> str:
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.flush()
        user = ChatMessage(
            conversation_id=conversation.id, role="user", content="I prefer coffee.", sequence=1
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


@pytest.mark.parametrize("fail_enqueue", [False, True])
async def test_answer_and_memory_task_commit_or_rollback_together(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, fail_enqueue: bool
) -> None:
    async def respond(*_args: Any, **_kwargs: Any) -> AgentResult:
        return AgentResult(content="Saved answer", extract_long_term=True)

    monkeypatch.setattr(runtime.chat.agent, "respond", respond)
    if fail_enqueue:
        original = runtime.memory_tasks.enqueue

        async def fail(*args: Any, **kwargs: Any) -> str:
            await original(*args, **kwargs)
            raise RuntimeFailure("Simulated transaction failure")

        monkeypatch.setattr(runtime.memory_tasks, "enqueue", fail)
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.commit()
    request = MessageInput(content="I prefer coffee.", memory_enabled=False)
    if fail_enqueue:
        with pytest.raises(RuntimeFailure, match="transaction"):
            await runtime.chat.send(conversation.id, request)
    else:
        answer = await runtime.chat.send(conversation.id, request)
        assert answer.state == "completed" and answer.memory_operation_id
    async with runtime.sessions() as session:
        tasks = list(await session.scalars(select(Operation).where(Operation.kind == "memory.extract")))
        assert len(tasks) == (0 if fail_enqueue else 1)
        if tasks:
            assert tasks[0].state == "queued"
            assert "coffee" not in json.dumps(tasks[0].payload)
            assert not await runtime.operations.run_once()
    assert (await runtime.chat.messages(conversation.id))[-1].state == (
        "failed" if fail_enqueue else "completed"
    )


@pytest.mark.parametrize("write_started", [False, True])
async def test_interrupted_tasks_recover_only_before_any_possible_write(
    runtime: Runtime, write_started: bool
) -> None:
    identifier = await enqueue(runtime)
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None
        row.state, row.attempts = "running", 1
        row.result = {"write_started": write_started}
        await session.commit()
    await runtime.operations.recover()
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None and row.state == ("failed" if write_started else "queued")
    if write_started:
        with pytest.raises(RuntimeFailure, match="部分"):
            await runtime.operations.retry(identifier)


@pytest.mark.parametrize("forget", [False, True])
async def test_delete_or_forget_cancels_active_extraction_and_fences_late_writes(
    runtime: Runtime, api: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forget: bool
) -> None:
    await real_memory(runtime, tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def generate(**_kwargs: Any) -> str:
        started.set()
        assert release.wait(10)
        return json.dumps({"memory": [{"text": "User prefers coffee."}]})

    monkeypatch.setattr(runtime.memory, "_generate", generate)
    await runtime.memory.remember("I prefer tea.", conversation_id="seed")
    remembered = (await runtime.memory.list())[0]["id"]
    identifier = await enqueue(runtime)
    worker = asyncio.create_task(runtime.memory_tasks.run_once())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        async with asyncio.timeout(3):
            if forget:
                response = await api.delete(f"/api/v1/memories/{remembered}")
                assert response.status_code == 200
            else:
                async with runtime.sessions() as session:
                    row = await session.get(Operation, identifier)
                    assert row is not None
                    conversation_id = row.payload["conversation_id"]
                await runtime.chat.maintain(conversation_id)
        release.set()
        assert await worker
        async with runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            assert row is not None and row.state == "cancelled"
        facts = await runtime.memory.list()
        assert len(facts) == (0 if forget else 1)
        assert all("coffee" not in fact["memory"] for fact in facts)
        if forget:
            late = await enqueue(runtime, generation=0)
            async with runtime.sessions() as session:
                row = await session.get(Operation, late)
                assert row is not None and row.state == "cancelled"
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("bad_response", ["not-json", '{"memory": "bad"}'])
async def test_invalid_extraction_output_is_not_silently_reported_as_success(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_response: str
) -> None:
    await real_memory(runtime, tmp_path, monkeypatch)
    monkeypatch.setattr(runtime.memory, "_generate", lambda **_kwargs: bad_response)
    identifier = await enqueue(runtime)
    for _ in range(3):
        async with runtime.sessions() as session:
            row = await session.get(Operation, identifier)
            assert row is not None
            row.result = {}
            await session.commit()
        assert await runtime.memory_tasks.run_once()
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None and row.state == "failed" and row.attempts == 3
        assert not row.result.get("write_started")
    assert await runtime.memory.list() == []


async def test_real_background_llm_process_is_reusable_and_has_a_bounded_deadline(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await real_memory(runtime, tmp_path, monkeypatch)
    blocked, release = threading.Event(), threading.Event()
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal requests
            self.rfile.read(int(self.headers["Content-Length"]))
            requests += 1
            if requests == 2:
                blocked.set()
                assert release.wait(10)
            body = json.dumps(
                {
                    "id": "test",
                    "created": 1,
                    "model": "test",
                    "choices": [{"message": {"role": "assistant", "content": '{"memory": []}'}}],
                }
            ).encode()
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime.settings.memory.llm.provider = "openai"
    runtime.settings.memory.llm.model = "test"
    runtime.settings.memory.llm.api_key = "synthetic"
    runtime.settings.memory.llm.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    runtime.settings.memory.llm.timeout_seconds = 15
    try:
        await runtime.memory.extract("first", operation_id="first", before_write=lambda: None)
        runtime.settings.memory.llm.timeout_seconds = 1
        job = asyncio.create_task(
            runtime.memory.extract("second", operation_id="second", before_write=lambda: None)
        )
        assert await asyncio.to_thread(blocked.wait, 5)
        async with asyncio.timeout(3):
            with pytest.raises(Exception, match="超时"):
                await job
        assert runtime.memory._llm_pool is None
        release.set()
        runtime.settings.memory.llm.timeout_seconds = 15
        await runtime.memory.extract("third", operation_id="third", before_write=lambda: None)
        assert requests == 3
    finally:
        release.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(2)


async def test_mem0_swallowed_storage_failure_requires_review_not_success_or_replay(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await real_memory(runtime, tmp_path, monkeypatch)
    from mem0.vector_stores.qdrant import Qdrant

    def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("private-fact-provider-error")

    monkeypatch.setattr(Qdrant, "insert", fail_insert)
    monkeypatch.setattr(
        runtime.memory, "_generate", lambda **_kwargs: '{"memory":[{"text":"User prefers tea."}]}'
    )
    identifier = await enqueue(runtime)
    assert await runtime.memory_tasks.run_once()
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None and row.state == "failed"
        assert row.attempts == 1 and row.result["write_started"]
    with pytest.raises(RuntimeFailure, match="部分"):
        await runtime.operations.retry(identifier)
    assert "private-fact-provider-error" not in caplog.text


async def test_memory_worker_shutdown_requeues_prewrite_task_and_restart_finishes_it(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.runtime.hosting import MemoryExtractionWorker

    await real_memory(runtime, tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def generate(**_kwargs: Any) -> str:
        started.set()
        assert release.wait(10)
        return '{"memory": [{"text": "User prefers coffee."}]}'

    monkeypatch.setattr(runtime.memory, "_generate", generate)
    identifier = await enqueue(runtime)
    stopping = asyncio.Event()
    worker = MemoryExtractionWorker(lambda: runtime)
    await worker.start(stopping)
    try:
        assert await asyncio.to_thread(started.wait, 10)
        stopping.set()
        release.set()
        async with asyncio.timeout(3):
            await worker.stop(stopping)
    finally:
        release.set()
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None and row.state == "queued" and not row.result.get("write_started")
    runtime.stopping = asyncio.Event()
    assert await runtime.memory_tasks.run_once()
    async with runtime.sessions() as session:
        row = await session.get(Operation, identifier)
        assert row is not None and row.state == "succeeded" and row.attempts == 2
    assert (await runtime.memory.list())[0]["memory"] == "User prefers coffee."


async def test_old_chat_store_adds_memory_task_reference_without_losing_messages(runtime: Runtime) -> None:
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.flush()
        message = ChatMessage(
            conversation_id=conversation.id, sequence=1, role="assistant", content="Legacy answer"
        )
        session.add(message)
        await session.commit()
        await session.execute(text("ALTER TABLE chat_message DROP COLUMN memory_operation_id"))
        await session.execute(delete(SchemaMigration).where(SchemaMigration.version == 5))
        await session.execute(delete(MemoryEpoch))
        await session.commit()
        engine = session.bind
        assert engine is not None
    await upgrade_database(engine)
    await upgrade_database(engine)
    async with runtime.sessions() as session:
        saved = await session.get(ChatMessage, message.id)
        assert saved is not None and saved.content == "Legacy answer" and saved.memory_operation_id is None
        assert await runtime.memory_tasks.generation(session) == 0
