from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import ChatContext, ChatResponse, ChatResponseUpdate, Message
from sqlalchemy import select

from shovel.config.model_settings import ModelProviderType
from shovel.config.settings import AppSettings
from shovel.db.model.runtime import Operation
from shovel.pipeline.embedding_cache import EmbeddingCache
from shovel.runtime.agent import ContextBudgetMiddleware
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import RuntimeWarmupWorker
from shovel.runtime.performance import RuntimePerformance, RuntimeWarmup
from shovel.runtime.schemas import MessageInput
from shovel.runtime.service import Runtime
from shovel.vector.store import init_vector_store
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import streamed_completion
from test_memory_tasks import enqueue


@pytest.mark.parametrize("provider", [ModelProviderType.OPENAI, ModelProviderType.OLLAMA])
async def test_context_reads_start_together_and_preserve_budgeted_response(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, provider: ModelProviderType
) -> None:
    runtime.settings.model.model_provider_type = provider
    memory_started, retrieval_started = asyncio.Event(), asyncio.Event()

    async def memory(_query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        memory_started.set()
        await retrieval_started.wait()
        return []

    async def retrieval(_request: Any) -> dict[str, Any]:
        retrieval_started.set()
        await memory_started.wait()
        return {"items": []}

    monkeypatch.setattr(runtime.memory, "search", memory)
    monkeypatch.setattr(runtime.retrieval, "search", retrieval)

    def respond(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        key = "max_tokens" if provider == ModelProviderType.OLLAMA else "max_completion_tokens"
        assert payload[key] == runtime.settings.model.max_output_tokens
        assert ("max_completion_tokens" if key == "max_tokens" else "max_tokens") not in payload
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Answer"}}]}
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    result = await asyncio.wait_for(
        runtime.chat.agent.respond("test", MessageInput(content="A document question"), []), 3
    )
    assert result.content == "Answer"


async def test_context_failure_cancels_the_other_read(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def memory(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return []

    async def retrieval(_request: Any) -> dict[str, Any]:
        await started.wait()
        raise RuntimeFailure("retrieval unavailable", status=503)

    monkeypatch.setattr(runtime.memory, "search", memory)
    monkeypatch.setattr(runtime.retrieval, "search", retrieval)
    with pytest.raises(RuntimeFailure, match="retrieval unavailable"):
        await asyncio.wait_for(
            runtime.chat.agent.respond("test", MessageInput(content="A document question"), []), 3
        )
    assert cancelled.is_set()


async def test_query_vectors_reuse_client_and_cache_but_not_query_results(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    closed: list[bool] = []

    class Embedder:
        model_version = "test-version"
        dimension = 4

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        async def aclose(self) -> None:
            closed.append(True)

    instances: list[Embedder] = []

    def build(*_args: Any, **_kwargs: Any) -> Embedder:
        instance = Embedder()
        instances.append(instance)
        return instance

    monkeypatch.setattr("shovel.runtime.retrieval.build_embedder", build)
    first, _ = await runtime.retrieval.embed_query("same")
    first[0] = 9.0
    second, _ = await runtime.retrieval.embed_query("same")
    assert second[0] == 1.0
    assert calls == [["same"]] and len(instances) == 1 and closed == []
    await runtime.retrieval.embed_query("different")
    assert calls == [["same"], ["different"]]
    await runtime.retrieval.close()
    assert closed == [True]


def test_embedding_cache_is_bounded_expires_and_separates_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("shovel.pipeline.embedding_cache.monotonic", lambda: now[0])
    cache = EmbeddingCache(1, 5)
    search = cache.key(["text", "search"], {})
    add = cache.key(["text", "add"], {})
    cache.put(search, [1.0])
    assert cache.get(add) is None
    copy = cache.get(search)
    assert copy is not None
    copy[0] = 2.0
    assert cache.get(search) == [1.0]
    cache.put(add, [3.0])
    assert cache.get(search) is None
    now[0] += 5
    assert cache.get(add) is None


async def test_foreground_capacity_cancellation_and_idle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("shovel.runtime.performance.monotonic", lambda: now[0])
    settings = AppSettings(performance={"max_foreground_runs": 1, "background_idle_seconds": 5})
    scheduler = RuntimePerformance(settings)
    stopping = asyncio.Event()
    started, release = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        async with scheduler.admit(stopping):
            started.set()
            await release.wait()

    first = asyncio.create_task(run())
    await started.wait()
    second = asyncio.create_task(run())
    await asyncio.sleep(0)
    assert scheduler.snapshot() == {"limit": 1, "running": 1, "waiting": 1}
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert scheduler.background_delay() > 0
    release.set()
    await first
    assert scheduler.background_delay() == 5
    now[0] += 5
    await scheduler.wait_for_idle(stopping)
    async with scheduler.admit(stopping):
        assert scheduler.running == 1
    assert scheduler.foreground == 0


async def test_extraction_remains_queued_during_foreground_and_idle_window(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = await enqueue(runtime)
    runtime.performance.foreground = 1
    assert not await runtime.memory_tasks.run_once(respect_foreground=True)
    runtime.performance.foreground = 0
    from time import monotonic

    runtime.performance.last_foreground = monotonic()
    assert not await runtime.memory_tasks.run_once(respect_foreground=True)
    runtime.performance.last_foreground -= 6

    async def extract(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(runtime.memory, "extract", extract)
    assert await runtime.memory_tasks.run_once(respect_foreground=True)
    async with runtime.sessions() as session:
        operation = await session.scalar(select(Operation).where(Operation.id == identifier))
        assert operation is not None and operation.state == "succeeded" and operation.attempts == 1


async def test_warmup_uses_owned_resources_and_reports_failures_without_secrets(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime.settings.model.model_provider_type = ModelProviderType.OLLAMA
    runtime.settings.model.base_endpoint = "http://127.0.0.1:11434"
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    runtime.settings.memory.embedder.model = "memory"
    runtime.settings.memory.embedder.provider = "ollama"
    calls: list[str] = []

    async def index() -> object:
        calls.append("index")
        raise OSError("private-source-path")

    async def memory() -> None:
        calls.append("memory")

    async def embedding(_text: str) -> tuple[list[float], str]:
        calls.append("embedding")
        return [1.0], "test"

    async def model() -> None:
        calls.append("model")

    monkeypatch.setattr(runtime, "sink", index)
    monkeypatch.setattr(runtime.memory, "warmup", memory)
    monkeypatch.setattr(runtime.retrieval, "embed_query", embedding)
    monkeypatch.setattr(runtime.chat, "warm_model", model)
    runtime.warmup = RuntimeWarmup(runtime)
    await runtime.warmup.run()
    assert calls == ["model", "index", "memory", "embedding"]
    assert runtime.warmup.states["index"]["state"] == "failed"
    assert runtime.warmup.states["chat_model"]["state"] == "ready"
    assert not runtime.performance.warming
    assert "private-source-path" not in caplog.text


def test_remote_models_are_never_automatically_warmed(runtime: Runtime) -> None:
    runtime.settings.model.model_provider_type = ModelProviderType.OLLAMA
    runtime.settings.model.base_endpoint = "https://remote.invalid"
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    runtime.settings.embedding.base_url = "http://remote.invalid"
    runtime.settings.memory.embedder.model = "memory"
    warmup = RuntimeWarmup(runtime)
    assert warmup.states["chat_model"]["state"] == "disabled"
    assert warmup.states["memory_embedding"]["state"] == "disabled"
    assert warmup.states["rag_embedding"]["state"] == "disabled"

    runtime.settings.model.base_endpoint = "http://127.0.0.1:11434"
    runtime.settings.model.model_name = "chat:120b-cloud"
    runtime.settings.memory.embedder.model = "embedding:cloud"
    runtime.settings.embedding.base_url = "http://127.0.0.1:11434"
    runtime.settings.embedding.model = "embedding:cloud"
    warmup = RuntimeWarmup(runtime)
    assert warmup.states["chat_model"]["state"] == "disabled"
    assert warmup.states["memory_embedding"]["state"] == "disabled"
    assert warmup.states["rag_embedding"]["state"] == "disabled"


async def test_warmup_worker_and_foreground_share_one_real_index(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.pipeline.sink import open_chunk_sink

    init_vector_store(runtime.settings.zvec)
    runtime.settings.performance.warm_local_models = False
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    runtime.warmup = RuntimeWarmup(runtime)
    opened = []

    def open_index(path: str) -> Any:
        sink = open_chunk_sink(path)
        opened.append(sink)
        return sink

    monkeypatch.setattr("shovel.runtime.service.open_chunk_sink", open_index)
    worker = RuntimeWarmupWorker(lambda: runtime)
    _, sink = await asyncio.gather(worker.execute_async(runtime.stopping), runtime.sink())
    assert opened == [sink] and await runtime.sink() is sink
    assert runtime.warmup.states["index"]["state"] == "ready"
    assert (
        await sink.search([1.0, 0.0, 0.0, 0.0], resource_ids=["absent"], model_version="test", limit=1) == []
    )


async def test_cancelled_warmup_clears_background_admission(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    runtime.warmup = RuntimeWarmup(runtime)
    started = asyncio.Event()

    async def open_index() -> object:
        started.set()
        await asyncio.Event().wait()
        return object()

    monkeypatch.setattr(runtime, "sink", open_index)
    task = asyncio.create_task(runtime.warmup.run())
    await started.wait()
    assert runtime.performance.warming
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.warmup.states["index"]["state"] == "cancelled"
    assert not runtime.performance.warming


async def test_model_metrics_include_first_text_and_usage_not_content(
    runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    context = ChatContext(
        runtime.chat.agent.client(), [Message("user", ["private-question"])], None, stream=True
    )

    async def stream() -> None:
        update = ChatResponseUpdate(contents=Message("assistant", ["private-answer"]).contents)
        for hook in context.stream_transform_hooks:
            update = hook(update)
        response = ChatResponse(
            messages=[Message("assistant", ["private-answer"])],
            usage_details={"input_token_count": 12, "output_token_count": 3},
            raw_representation={"load_duration": 2_000_000, "private": "private-credential"},
        )
        for hook in context.stream_result_hooks:
            hook(response)

    with development_logging():
        await ContextBudgetMiddleware(4000).process(context, stream)
    output = capsys.readouterr().out
    assert "model.call.first_text" in output and "input_token_count=12" in output
    assert "provider_load_duration_ms=2.0" in output
    assert all(value not in output for value in ("private-answer", "private-question", "private-credential"))


@pytest.mark.parametrize("provider", [ModelProviderType.OPENAI, ModelProviderType.OLLAMA])
async def test_model_warmup_uses_maf_without_loading_extensions_or_saving_messages(
    runtime: Runtime, provider: ModelProviderType
) -> None:
    runtime.settings.model.model_provider_type = provider
    workspace = runtime.settings.agent.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "mcp.json").write_text(
        '{"mcpServers":{"must-not-launch":{"command":"not-a-real-executable"}}}', encoding="utf-8"
    )
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Ready"}}]}
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await runtime.chat.warm_model()
    assert len(requests) == 1
    key = "max_tokens" if provider == ModelProviderType.OLLAMA else "max_completion_tokens"
    assert requests[0][key] == 1
    assert ("max_completion_tokens" if key == "max_tokens" else "max_tokens") not in requests[0]
    assert runtime.chat.agent.extensions._stack is None
    assert runtime.chat._active_runs == 0
    from shovel.db.model.runtime import ChatMessage

    async with runtime.sessions() as session:
        assert await session.scalar(select(ChatMessage)) is None


async def test_pure_chat_is_enforced_for_rest_and_agui_without_loading_extensions(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from shovel.runtime.model_admission import ModelAdmissionTransport

    requests: list[dict[str, Any]] = []
    recalled: list[str] = []

    class DelayedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for frame in streamed_completion("Hello").content.split(b"\n\n"):
                if frame:
                    yield frame + b"\n\n"
                    await asyncio.sleep(0.01)

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Pure Chat must not load extensions or run RAG.")

    async def memory(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        recalled.append(query)
        return [{"memory": "The user likes concise answers."}]

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert not payload.get("tools")
        assert "The user likes concise answers." in json.dumps(payload, ensure_ascii=False)
        assert "Current mode: Chat" in json.dumps(payload)
        if payload.get("stream"):
            return httpx.Response(200, stream=DelayedStream(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Hello"}}]}
        )

    monkeypatch.setattr(runtime.retrieval, "search", forbidden)
    monkeypatch.setattr(runtime.chat.agent.extensions, "load", forbidden)
    monkeypatch.setattr(runtime.memory, "search", memory)
    runtime.chat.agent._client = httpx.AsyncClient(
        transport=ModelAdmissionTransport(
            runtime.performance.models, "foreground", runtime.stopping, httpx.MockTransport(respond)
        )
    )
    conversation = (await api.post("/api/v1/conversations", json={})).json()
    response = await api.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "First question", "interaction_mode": "chat"},
    )
    assert response.status_code == 200
    assert not any("本地" in warning for warning in response.json()["warnings"])
    stream = await asyncio.wait_for(
        api.post(
            "/api/v1/agent",
            json={
                "threadId": conversation["id"],
                "runId": "pure-chat",
                "messages": [{"id": "user", "role": "user", "content": "Second question"}],
                "forwardedProps": {"interaction_mode": "chat"},
                "context": [],
                "tools": [{"name": "search", "description": "forged tool", "parameters": {}}],
            },
        ),
        3,
    )
    assert stream.status_code == 200 and "RUN_FINISHED" in stream.text
    assert len(requests) == 2 and recalled == ["First question", "Second question"]
    assert "First question" in json.dumps(requests[1])
    invalid = await api.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "Question", "interaction_mode": "chat", "search_mode": "keyword"},
    )
    assert invalid.status_code == 422


async def test_compact_context_keeps_complete_recent_history_without_sync_summarization(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    runtime.settings.performance.context_profile = "compact"
    runtime.settings.model.max_input_chars = 12000
    payloads: list[dict[str, Any]] = []

    async def summary(_identifier: str) -> str:
        return "Persistent summary"

    monkeypatch.setattr(runtime.memory, "summary", summary)

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Answer"}}]}
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    result = await runtime.chat.agent.respond(
        "test",
        MessageInput(content="Follow up", interaction_mode="chat", memory_enabled=False),
        [
            Message("assistant", ["orphan"]),
            Message("user", ["older " * 1000]),
            Message("assistant", ["old reply"]),
            Message("user", ["recent question"]),
            Message("assistant", ["recent answer"]),
        ],
    )
    encoded = json.dumps(payloads[0])
    assert result.content == "Answer"
    assert "recent question" in encoded and "recent answer" in encoded and "Persistent summary" in encoded
    assert "orphan" not in encoded and "older older" not in encoded and "old reply" not in encoded


async def test_embedding_singleflight_retains_other_reader_and_clears_failures(
    runtime: Runtime,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Embedder:
        model_version = "test"
        dimension = 4

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts[0])
            started.set()
            await release.wait()
            if texts[0] == "bad":
                raise ValueError("embedding failed")
            return [[1.0, 0.0, 0.0, 0.0]]

        async def aclose(self) -> None:
            pass

    runtime.retrieval._embedder = Embedder()
    first = asyncio.create_task(runtime.retrieval.embed_query("same"))
    await started.wait()
    second = asyncio.create_task(runtime.retrieval.embed_query("same"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    vector, _ = await second
    assert vector == [1.0, 0.0, 0.0, 0.0] and calls == ["same"]
    for _ in range(2):
        with pytest.raises(ValueError, match="embedding failed"):
            await runtime.retrieval.embed_query("bad")
    assert calls == ["same", "bad", "bad"]


async def test_keyword_hydration_batches_metadata_queries_without_changing_results(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    from sqlalchemy import event

    from shovel.runtime.schemas import ResourceInput, ScanInput, SearchInput

    corpus = tmp_path / "query-corpus"
    corpus.mkdir()
    for index in range(8):
        (corpus / f"document-{index}.md").write_text(
            f"# Document {index}\n\nShovel query optimization.\n\n" + "This is searchable content.\n" * 20,
            encoding="utf-8",
        )
    created = await runtime.operations.create_resource(
        ResourceInput(name="query corpus", connector_kind="local_fs", config={"root_path": str(corpus)})
    )
    await runtime.operations.run_once()
    assert created.resource_id
    scan = await runtime.operations.scan(created.resource_id, ScanInput(embed=False))
    await runtime.operations.run_once()
    assert (await runtime.operations.get(scan.id)).state == "succeeded"
    statements: list[str] = []
    engine = runtime.sessions.kw["bind"].sync_engine

    def query(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", query)
    try:
        result = await runtime.retrieval.search(SearchInput(query="Shovel", mode="keyword", limit=6))
    finally:
        event.remove(engine, "before_cursor_execute", query)
    assert len(result["items"]) == 6
    assert len({hit["doc_uri"] for hit in result["items"]}) == 6
    assert all(
        hit["resource_id"] == created.resource_id and "searchable content" in hit["text"]
        for hit in result["items"]
    )
    assert len(statements) <= 6


async def test_last_embedding_reader_cancellation_stops_owned_network_work(runtime: Runtime) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Embedder:
        model_version = "test"
        dimension = 4

        async def embed(self, _texts: list[str]) -> list[list[float]]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return []

        async def aclose(self) -> None:
            pass

    runtime.retrieval._embedder = Embedder()
    reader = asyncio.create_task(runtime.retrieval.embed_query("cancel"))
    await started.wait()
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert cancelled.is_set()
    assert not runtime.retrieval._embedding_reads


async def test_sdk_preserves_friendly_local_queue_timeout(runtime: Runtime) -> None:
    from shovel.runtime.model_admission import ModelAdmissionTransport

    runtime.performance.models.timeout = 0.05

    def forbidden(_request: httpx.Request) -> httpx.Response:
        pytest.fail("A queued request must not reach the model.")

    runtime.chat.agent._client = httpx.AsyncClient(
        transport=ModelAdmissionTransport(
            runtime.performance.models, "foreground", runtime.stopping, httpx.MockTransport(forbidden)
        )
    )
    async with runtime.performance.models.admit("memory", runtime.stopping):
        with pytest.raises(RuntimeFailure, match="等待超时") as error:
            await runtime.chat.agent.respond(
                "test", MessageInput(content="Hello", interaction_mode="chat", memory_enabled=False), []
            )
    assert error.value.status == 503


async def test_final_context_budget_counts_maf_instructions_and_compact_tool_schema(runtime: Runtime) -> None:
    from agent_framework import tool

    async def forbidden() -> None:
        pytest.fail("An over-budget request must not reach the provider.")

    context = ChatContext(
        runtime.chat.agent.client(),
        [Message("user", ["question"])],
        {"instructions": "x" * 100},
        stream=False,
    )
    with pytest.raises(RuntimeFailure, match="上下文超过"):
        await ContextBudgetMiddleware(100).process(context, forbidden)
    function = tool(lambda value: value, name="echo", description="A test tool")
    context = ChatContext(
        runtime.chat.agent.client(), [Message("user", ["question"])], {"tools": [function]}, stream=False
    )
    with pytest.raises(RuntimeFailure, match="上下文超过"):
        await ContextBudgetMiddleware(100, include_tool_schema=True).process(context, forbidden)
