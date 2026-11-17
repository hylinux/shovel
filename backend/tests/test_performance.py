from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import ChatContext, ChatResponse, ChatResponseUpdate, Message
from sqlalchemy import select

from shovel.config.model_settings import DefaultAgentModelSettings, ModelProviderType
from shovel.config.settings import AppSettings
from shovel.db.model.runtime import ChatMessage, Conversation, Operation
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
from test_hosted_runtime import index_notes, streamed_completion
from test_hosted_runtime import runtime as runtime
from test_memory_tasks import enqueue


@pytest.mark.parametrize("policy", ["auto", "disabled"])
async def test_chat_does_not_wait_for_knowledge_index_without_required_policy(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    import json

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Ordinary chat must not start retrieval, knowledge embedding or index initialization.")

    monkeypatch.setattr(runtime.retrieval, "search", forbidden)
    monkeypatch.setattr(runtime.retrieval, "embed_query", forbidden)
    monkeypatch.setattr(runtime, "sink", forbidden)

    def answer(request: httpx.Request) -> httpx.Response:
        tools = json.loads(request.content)["tools"]
        assert ("search" in {tool["function"]["name"] for tool in tools}) == (policy == "auto")
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Answer"}}]}
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    async with runtime._sink_lock:
        result = await asyncio.wait_for(
            runtime.chat.agent.respond(
                "test",
                MessageInput.model_validate(
                    {
                        "content": "Explain a general programming concept",
                        "knowledge_policy": policy,
                        "memory_enabled": False,
                    }
                ),
                [],
            ),
            3,
        )
    assert result.content == "Answer"
    assert result.warnings == []


@pytest.mark.parametrize("protocol", ["rest", "agui"])
async def test_optional_tool_search_discloses_keyword_fallback_in_model_and_history(
    runtime: Runtime, api: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    import json

    from shovel.config.embedding_config import EmbeddingSettings

    await index_notes(runtime, tmp_path / "notes")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="test")
    calls = 0

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A cold optional search must not embed the query or await/open the vector sink.")

    monkeypatch.setattr(runtime, "sink", forbidden)
    monkeypatch.setattr(runtime.retrieval, "embed_query", forbidden)

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            tool = {
                "id": "search-call",
                "type": "function",
                "function": {"name": "search", "arguments": '{"query":"Shovel"}'},
            }
            if body.get("stream"):
                frames = [
                    {"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, **tool}]}},
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"},
                ]
                content = "".join(
                    "data: "
                    + json.dumps(
                        {
                            "id": "tool-response",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "test",
                            "choices": [frame],
                        }
                    )
                    + "\n\n"
                    for frame in frames
                )
                return httpx.Response(
                    200, content=content + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "choices": [
                        {
                            "message": {"role": "assistant", "tool_calls": [tool]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )
        tool_result = json.loads(
            next(message["content"] for message in body["messages"] if message["role"] == "tool")
        )
        assert tool_result["degraded"] is True and tool_result["reason"] == "index_not_ready"
        assert tool_result["requested_mode"] == "auto" and tool_result["effective_mode"] == "keyword"
        assert tool_result["warnings"] and tool_result["items"][0]["index"] == 1
        if body.get("stream"):
            return streamed_completion("Keyword-only local evidence [1].")
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [
                    {"message": {"role": "assistant", "content": "Keyword-only local evidence [1]."}}
                ],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    # Holding the same lock as warmup proves the request cannot wait for index opening.
    async with runtime._sink_lock:
        if protocol == "rest":
            response = await asyncio.wait_for(
                api.post(
                    f"/api/v1/conversations/{conversation}/messages",
                    json={"content": "Search Shovel knowledge", "memory_enabled": False},
                ),
                5,
            )
            assert response.status_code == 200, response.text
        else:
            response = await asyncio.wait_for(
                api.post(
                    "/api/v1/agent",
                    json={
                        "threadId": conversation,
                        "runId": "keyword-fallback",
                        "messages": [
                            {"id": "question", "role": "user", "content": "Search Shovel knowledge"}
                        ],
                        "forwardedProps": {"memory_enabled": False},
                        "tools": [],
                        "context": [],
                    },
                ),
                5,
            )
            assert "RUN_FINISHED" in response.text, response.text
            assert "index is not ready" in response.text
    saved = (await runtime.chat.messages(conversation))[-1]
    assert saved.state == "completed" and len(saved.citations) == 1 and calls == 2
    assert any("Only keyword search" in warning for warning in saved.warnings)


@pytest.mark.parametrize("mode", ["auto", "vector", "hybrid"])
async def test_strict_search_never_uses_cold_index_fallback(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from shovel.config.embedding_config import EmbeddingSettings
    from shovel.runtime.schemas import SearchInput

    await index_notes(runtime, tmp_path / "notes")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="test")
    embedded: list[str] = []

    async def embed(query: str) -> tuple[list[float], str]:
        embedded.append(query)
        return [1.0, 0.0, 0.0, 0.0], "test"

    async def unavailable() -> Any:
        raise RuntimeFailure("strict index unavailable", status=503)

    monkeypatch.setattr(runtime.retrieval, "embed_query", embed)
    monkeypatch.setattr(runtime, "sink", unavailable)
    with pytest.raises(RuntimeFailure, match="strict index unavailable"):
        await runtime.retrieval.search(SearchInput.model_validate({"query": "Shovel", "mode": mode}))
    assert embedded == ["Shovel"]


async def test_ready_optional_search_uses_hybrid_and_propagates_errors(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.config.embedding_config import EmbeddingSettings
    from shovel.runtime.schemas import SearchInput

    await index_notes(runtime, tmp_path / "notes")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="test")
    searched: list[dict[str, Any]] = []

    class Sink:
        async def search(self, _vector: list[float], **kwargs: Any) -> list[Any]:
            searched.append(kwargs)
            return []

    async def embed(_query: str) -> tuple[list[float], str]:
        return [1.0, 0.0, 0.0, 0.0], "test"

    monkeypatch.setattr(runtime, "ready_sink", lambda: Sink())
    monkeypatch.setattr(runtime.retrieval, "embed_query", embed)
    result = await runtime.retrieval.search(SearchInput(query="Shovel"), allow_keyword_fallback=True)
    assert result["mode"] == "hybrid" and result["degraded"] is False
    assert searched and result["items"] and result["warnings"] == []

    async def rejected(_query: str) -> tuple[list[float], str]:
        raise RuntimeFailure("embedding authentication rejected", status=503)

    monkeypatch.setattr(runtime.retrieval, "embed_query", rejected)
    with pytest.raises(RuntimeFailure, match="authentication rejected"):
        await runtime.retrieval.search(SearchInput(query="Shovel"), allow_keyword_fallback=True)


async def test_optional_search_does_not_hide_failed_index_warmup(runtime: Runtime, tmp_path: Path) -> None:
    from shovel.config.embedding_config import EmbeddingSettings
    from shovel.runtime.schemas import SearchInput

    await index_notes(runtime, tmp_path / "notes")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="test")
    runtime.warmup.states["index"] = {"state": "failed", "error_type": "VectorSinkError"}
    with pytest.raises(RuntimeFailure, match="initialization failed"):
        await runtime.retrieval.search(SearchInput(query="Shovel"), allow_keyword_fallback=True)


async def test_disabled_search_is_guarded_server_side(runtime: Runtime) -> None:
    from shovel.runtime.agent import ShovelContextProvider

    provider = ShovelContextProvider(
        runtime, "test", MessageInput(content="Search", knowledge_policy="disabled"), []
    )
    with pytest.raises(RuntimeFailure, match="disabled") as error:
        await provider.search("Shovel")
    assert error.value.status == 403


async def test_failed_tool_search_retains_a_warning_without_claiming_no_matches(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.runtime.agent import ShovelContextProvider

    async def failed(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeFailure("Index initialization failed.", status=503)

    monkeypatch.setattr(runtime.retrieval, "search", failed)
    provider = ShovelContextProvider(runtime, "test", MessageInput(content="Search"), [])
    with pytest.raises(RuntimeFailure, match="initialization failed"):
        await provider.search("Shovel")
    assert provider.result.warnings == [
        "Knowledge search failed. Retrieval is incomplete; "
        "do not treat this answer as fully grounded in local sources."
    ]
    assert not provider.search_completed and not provider.result.sources


@pytest.mark.parametrize(
    "extra",
    [
        {"resource_ids": ["notes"]},
        {"search_mode": "keyword"},
        {"search_mode": "vector"},
        {"search_mode": "hybrid"},
    ],
)
async def test_disabled_knowledge_conflicts_are_rejected_before_chat(
    runtime: Runtime, api: httpx.AsyncClient, extra: dict[str, Any]
) -> None:
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"content": "Shovel", "knowledge_policy": "disabled", **extra},
    )
    assert response.status_code == 422
    assert await runtime.chat.messages(conversation) == []


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
        runtime.chat.agent.respond(
            "test", MessageInput(content="A document question", knowledge_policy="required"), []
        ),
        3,
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
            runtime.chat.agent.respond(
                "test", MessageInput(content="A document question", knowledge_policy="required"), []
            ),
            3,
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

    async def index(**_kwargs: Any) -> object:
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


async def test_dated_memory_commits_while_knowledge_index_warmup_is_blocked(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from datetime import UTC, datetime

    source = "请记住\uff1a我在2026年10月12日开始了紫鸢-V74项目。"
    started, release = asyncio.Event(), asyncio.Event()

    async def open_index(**_kwargs: Any) -> object:
        started.set()
        await release.wait()
        return object()

    def respond(_request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {
                "candidates": [
                    {
                        "text": source,
                        "kind": "episodic",
                        "subject": "user",
                        "predicate": "decides",
                        "object": "紫鸢-V74",
                        "topic": "project.紫鸢-V74.started",
                        "confidence": 0.95,
                        "importance": 0.8,
                        "evidence": source,
                        "occurred_on": "2026-10-12",
                    }
                ]
            }
        )
        return httpx.Response(
            200,
            json={"created": 1, "choices": [{"message": {"role": "assistant", "content": content}}]},
        )

    runtime.settings.performance.warm_local_models = False
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    monkeypatch.setattr(runtime, "sink", open_index)
    runtime.memory.extractor._transport = lambda: httpx.MockTransport(respond)
    warmup = RuntimeWarmup(runtime)
    task = asyncio.create_task(warmup.run())
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with runtime.sessions() as session:
            conversation = Conversation()
            session.add(conversation)
            await session.flush()
            user = ChatMessage(conversation_id=conversation.id, role="user", content=source, sequence=1)
            session.add(user)
            await session.flush()
            identifier = await runtime.memory_tasks.enqueue(
                session,
                user,
                generation=await runtime.memory_tasks.generation(session),
                run_id="dated-memory-during-warmup",
            )
            await session.commit()
        assert await asyncio.wait_for(runtime.memory_tasks.run_once(respect_foreground=True), 3)
        operation = await runtime.operations.get(identifier)
        assert operation.state == "succeeded" and operation.attempts == 1
        items = await runtime.memory.list()
        assert len(items) == 1
        assert items[0]["kind"] == "episodic" and items[0]["state"] == "active"
        assert items[0]["occurred_at"] == int(datetime(2026, 10, 12, tzinfo=UTC).timestamp())
        assert items[0]["episode_time_precision"] == "day"
        assert items[0]["provenance"][0]["evidence"] == source
        assert not task.done() and runtime.ready_sink() is None
    finally:
        release.set()
        await asyncio.wait_for(task, 3)


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
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    from shovel.pipeline.sink import open_chunk_sink
    from shovel.runtime.schemas import SearchInput

    await index_notes(runtime, tmp_path / "notes")
    init_vector_store(runtime.settings.zvec)
    runtime.settings.performance.warm_local_models = False
    runtime.settings.embedding.provider = "ollama"
    runtime.settings.embedding.model = "embedding"
    runtime.warmup = RuntimeWarmup(runtime)
    opened = []
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def open_index(path: str, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(10):
            raise TimeoutError("Test did not release index initialization.")
        sink = open_chunk_sink(path, **kwargs)
        opened.append(sink)
        return sink

    embedded = asyncio.Event()

    async def embedding(_query: str) -> Any:
        embedded.set()
        return [1.0, 0.0, 0.0, 0.0], "test"

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Answer"}}]}
        )

    monkeypatch.setattr(runtime._knowledge_worker, "open", open_index)
    monkeypatch.setattr(runtime.retrieval, "embed_query", embedding)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    worker = RuntimeWarmupWorker(lambda: runtime)
    warming = asyncio.create_task(worker.execute_async(runtime.stopping))
    strict = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        status = (await asyncio.wait_for(api.get("/api/v1/status"), 3)).json()
        opening = status["index_open"]
        assert opening["state"] == "opening" and opening["reason"] == "warmup"
        assert opening["host_id"] == status["host_id"]
        assert status["runtime_initialization"]["ready_at"] <= opening["requested_at"]
        assert status["environment"]["process_bits"] == 64
        assert (await asyncio.wait_for(api.get("/api/v1/resources"), 3)).status_code == 200
        conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
        answer_response = await asyncio.wait_for(
            api.post(
                f"/api/v1/conversations/{conversation}/messages",
                json={"content": "Explain a general programming concept", "memory_enabled": False},
            ),
            5,
        )
        assert answer_response.status_code == 200 and not warming.done()
        keywords = await asyncio.wait_for(
            runtime.retrieval.search(SearchInput(query="Shovel", mode="keyword")), 3
        )
        assert keywords["mode"] == "keyword" and keywords["items"]
        result = await asyncio.wait_for(
            runtime.retrieval.search(SearchInput(query="Shovel"), allow_keyword_fallback=True), 3
        )
        assert result["mode"] == "keyword" and result["items"] and result["degraded"]
        assert result["index_state"] == "warming" and not warming.done()
        assert runtime.ready_sink() is None
        assert not embedded.is_set()
        strict = asyncio.create_task(runtime.retrieval.search(SearchInput(query="Shovel", mode="vector")))
        await asyncio.wait_for(embedded.wait(), 3)
        assert not strict.done()
    finally:
        release.set()
        _, sink = await asyncio.gather(warming, runtime.sink())
        strict_result = await strict if strict is not None else None
    assert opened == [sink] and await runtime.sink() is sink
    assert runtime.warmup.states["index"]["state"] == "ready"
    assert strict_result is not None and strict_result["mode"] == "vector"
    snapshot = runtime.index_open_snapshot()
    assert snapshot is not None and snapshot["operation_id"] == opening["operation_id"]
    assert snapshot["state"] == "ready" and snapshot["reason"] == "warmup"
    assert snapshot["elapsed_ms"] >= snapshot["worker_ms"] >= snapshot["collection_ms"] >= 0
    assert snapshot["process_cpu_ms"] >= snapshot["collection_cpu_ms"] >= 0
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

    async def open_index(**_kwargs: Any) -> object:
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


async def test_unified_agent_keeps_tools_retrieval_memory_and_history_for_rest_and_agui(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from shovel.runtime.model_admission import ModelAdmissionTransport

    requests: list[dict[str, Any]] = []
    recalled: list[str] = []
    searched: list[str] = []

    class DelayedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for frame in streamed_completion("Hello").content.split(b"\n\n"):
                if frame:
                    yield frame + b"\n\n"
                    await asyncio.sleep(0.01)

    async def search(request: Any) -> dict[str, Any]:
        searched.append(request.query)
        return {"items": []}

    async def memory(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        recalled.append(query)
        return [{"memory": "The user likes concise answers."}]

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["tools"]
        assert "search" in {tool["function"]["name"] for tool in payload["tools"]}
        assert "forged_tool" not in {tool["function"]["name"] for tool in payload["tools"]}
        assert "The user likes concise answers." in json.dumps(payload, ensure_ascii=False)
        assert "Current mode: Chat" not in json.dumps(payload)
        if payload.get("stream"):
            return httpx.Response(200, stream=DelayedStream(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Hello"}}]}
        )

    monkeypatch.setattr(runtime.retrieval, "search", search)
    monkeypatch.setattr(runtime.memory, "search", memory)
    runtime.chat.agent._client = httpx.AsyncClient(
        transport=ModelAdmissionTransport(
            runtime.performance.models, "foreground", runtime.stopping, httpx.MockTransport(respond)
        )
    )
    conversation = (await api.post("/api/v1/conversations", json={})).json()
    response = await api.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"content": "First question"},
    )
    assert response.status_code == 200
    stream = await asyncio.wait_for(
        api.post(
            "/api/v1/agent",
            json={
                "threadId": conversation["id"],
                "runId": "unified-agent",
                "messages": [{"id": "user", "role": "user", "content": "Second question"}],
                "forwardedProps": {},
                "context": [],
                "tools": [{"name": "forged_tool", "description": "forged tool", "parameters": {}}],
            },
        ),
        3,
    )
    assert stream.status_code == 200 and "RUN_FINISHED" in stream.text
    assert len(requests) == 2 and recalled == ["First question", "Second question"]
    assert searched == [], "Automatic chat must not prefetch local knowledge."
    assert runtime.chat.agent.extensions._stack is not None
    assert "First question" in json.dumps(requests[1])


@pytest.mark.parametrize("stream", [False, True])
async def test_removed_session_mode_is_rejected_instead_of_silently_enabling_tools(
    runtime: Runtime, api: httpx.AsyncClient, stream: bool
) -> None:
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = (
        await api.post(
            "/api/v1/agent",
            json={
                "threadId": conversation,
                "runId": "removed-mode",
                "messages": [{"id": "question", "role": "user", "content": "Hello"}],
                "forwardedProps": {"interaction_mode": "chat"},
            },
        )
        if stream
        else await api.post(
            f"/api/v1/conversations/{conversation}/messages",
            json={"content": "Hello", "interaction_mode": "chat"},
        )
    )
    assert response.status_code == 422
    assert await runtime.chat.messages(conversation) == []


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
        MessageInput(content="Follow up", memory_enabled=False),
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
            await runtime.chat.agent.respond("test", MessageInput(content="Hello", memory_enabled=False), [])
    assert error.value.status == 503


async def test_large_configured_input_budget_is_still_enforced(runtime: Runtime) -> None:
    settings = DefaultAgentModelSettings(max_input_chars=500001)
    middleware = ContextBudgetMiddleware(settings.max_input_chars)
    calls = 0

    async def dispatch() -> None:
        nonlocal calls
        calls += 1

    context = ChatContext(
        runtime.chat.agent.client(),
        [Message("user", ["x" * (settings.max_input_chars - 10)])],
        {"instructions": "x" * 10},
        stream=False,
    )
    await middleware.process(context, dispatch)
    assert calls == 1
    context.options["instructions"] += "x"
    with pytest.raises(RuntimeFailure, match="上下文超过"):
        await middleware.process(context, dispatch)
    assert calls == 1


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
