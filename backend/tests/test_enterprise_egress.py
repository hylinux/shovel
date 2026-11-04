import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from ag_ui.core import TextMessageContentEvent
from agent_framework import Agent, Message
from agent_framework.exceptions import ChatClientException
from sqlalchemy import select

from shovel.config.model_settings import ModelProviderType
from shovel.db.model.runtime import ChatMessage, Conversation
from shovel.memory.agent_framework import AgentFrameworkMemoryExtractor
from shovel.memory.sqlite_repository import SQLiteMemoryRepository
from shovel.memory.zvec_index import ZvecMemoryIndex
from shovel.pipeline.embedder import HttpEmbedder
from shovel.pipeline.embedding_cache import EmbeddingCache
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import ZvecChunkSink
from shovel.protection.domain import ContentRight, DataAction, DataDestination, ProtectionDeniedError
from shovel.protection.egress import EgressChannel, egress_scope, require_ordinary
from shovel.protection.service import ProtectionService
from shovel.runtime.agent import AgentResult, ToolDiagnosticsMiddleware, create_framework_client
from shovel.runtime.operations import OperationService
from shovel.runtime.schemas import MessageInput
from test_agent_extensions import completion, tool_call
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_protection_policy import BODY, kernel, reviewed_context, state_for, use_binding, wrapped


@pytest.mark.parametrize("channel", tuple(EgressChannel))
def test_read_never_grants_a_raw_or_derived_legacy_exit(channel):
    ctx = reviewed_context().model_copy(update={"rights": (ContentRight.READ,)})
    use = use_binding(ctx, action=DataAction.READ, destination=DataDestination.WORKFLOW_STATE)
    policy, _, _ = kernel(state_for(ctx, use))
    with egress_scope():
        raw = ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, lambda value: value)
        with pytest.raises(ProtectionDeniedError, match="protected egress adapter"):
            require_ordinary(channel, f"Derived: {raw['body']}")
    # A different ordinary request is not classified as enterprise by the previous one.
    with egress_scope():
        require_ordinary(channel, "ordinary content")


async def test_child_task_ingress_contains_parent_and_siblings_without_cross_request_taint():
    async def enterprise_request():
        with egress_scope():
            await asyncio.create_task(asyncio.to_thread(lambda: wrapped(reviewed_context())))
            with pytest.raises(ProtectionDeniedError):
                require_ordinary(EgressChannel.MODEL, "a derived answer")
            with egress_scope(), pytest.raises(ProtectionDeniedError):
                require_ordinary(EgressChannel.CLIPBOARD, "a derived answer")

    async def ordinary_request():
        with egress_scope():
            await asyncio.sleep(0)
            require_ordinary(EgressChannel.MODEL, "ordinary content")

    await asyncio.gather(enterprise_request(), ordinary_request())


def test_serialized_reference_cannot_enter_ordinary_json_without_a_live_flow():
    reference = wrapped(reviewed_context()).metadata.reference.model_dump(mode="json")
    with pytest.raises(ProtectionDeniedError):
        require_ordinary(EgressChannel.PERSISTENCE, {"nested": [reference]})


async def test_real_maf_tool_round_cannot_stringify_enterprise_results_into_model_requests(runtime):
    requests = []

    def model(request):
        requests.append(json.loads(request.content))
        return completion(calls=[tool_call("enterprise_result", {})])

    def enterprise_result():
        """Return synthetic protected content, never ordinary JSON."""
        return wrapped(reviewed_context())

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(runtime.settings.model, http, max_iterations=2)
        agent = Agent(client=client, tools=[enterprise_result], middleware=[ToolDiagnosticsMiddleware()])
        try:
            with egress_scope(), pytest.raises((ProtectionDeniedError, ChatClientException)):
                await agent.run("Read the test source.")
        finally:
            await sdk.close()
    assert len(requests) == 1
    assert BODY["body"] not in json.dumps(requests)


@pytest.mark.parametrize(
    "provider,endpoint",
    [("openai", "https://model.example.invalid/v1"), ("ollama", "http://127.0.0.1:11434")],
)
async def test_sdk_boundary_blocks_calls_without_chat_middleware_even_for_local_models(
    runtime, provider, endpoint
):
    runtime.settings.model.model_provider_type = ModelProviderType(provider)
    runtime.settings.model.base_endpoint = endpoint
    sent = []

    def model(request):
        sent.append(request)
        return completion(content="unexpected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(runtime.settings.model, http, max_iterations=1)
        try:
            with egress_scope():
                wrapped(reviewed_context())
                with pytest.raises((ProtectionDeniedError, ChatClientException)):
                    await client.get_response([Message("user", ["derived content"])])
        finally:
            await sdk.close()
    assert not sent


async def test_real_chat_tool_denial_is_a_safe_403_not_an_sdk_connection_failure(runtime, api, monkeypatch):
    requests = []

    def model(request):
        requests.append(json.loads(request.content))
        return completion(calls=[tool_call("enterprise_result", {})])

    def enterprise_result():
        """Return synthetic enterprise content."""
        return wrapped(reviewed_context())

    async def load(_client):
        runtime.chat.agent.extensions.tools = [enterprise_result]

    monkeypatch.setattr(runtime.chat.agent.extensions, "load", load)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    conversation = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"content": "Read the synthetic source.", "memory_enabled": False},
    )
    assert response.status_code == 403
    assert len(requests) == 1
    assert BODY["body"] not in json.dumps(requests) + response.text


@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_chat_never_streams_or_persists_unapproved_enterprise_body(
    runtime, api, monkeypatch, streaming
):
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.commit()

    async def respond(*args, on_event=None, **kwargs):
        wrapped(reviewed_context())
        if on_event is not None:
            await on_event(TextMessageContentEvent(message_id="synthetic", delta=BODY["body"]))
        return AgentResult(content=BODY["body"], extract_long_term=True)

    monkeypatch.setattr(runtime.chat.agent, "respond", respond)
    request = MessageInput(content="Read the synthetic source.", memory_enabled=False)
    if streaming:
        events = [
            event.model_dump(mode="json")
            async for event in runtime.chat.stream(conversation.id, request, "synthetic-run")
        ]
        assert any(event["type"] == "RUN_ERROR" for event in events)
        assert not any(event["type"] == "RUN_FINISHED" for event in events)
        assert BODY["body"] not in json.dumps(events)
    else:
        response = await api.post(
            f"/api/v1/conversations/{conversation.id}/messages", json=request.model_dump(mode="json")
        )
        assert response.status_code == 403
        assert BODY["body"] not in response.text
    async with runtime.sessions() as session:
        messages = list(await session.scalars(select(ChatMessage)))
        assert messages[-1].state == "failed"
        assert BODY["body"] not in " ".join(message.content for message in messages)
        assert all(message.memory_operation_id is None for message in messages)


async def test_actual_legacy_sinks_deny_before_io_or_enqueue(runtime):
    # Uninitialized adapters make any accidental I/O fail rather than mask a missed gate.
    embedding = object.__new__(HttpEmbedder)
    knowledge = object.__new__(ScanService)
    index = object.__new__(ZvecChunkSink)
    memory_index = object.__new__(ZvecMemoryIndex)
    extractor = object.__new__(AgentFrameworkMemoryExtractor)
    memory = SQLiteMemoryRepository(runtime.sessions)
    operations = OperationService(runtime)
    cache = EmbeddingCache(2, 60)
    with egress_scope():
        wrapped(reviewed_context())

        async def unapproved_tool():
            pytest.fail("The tool must not dispatch.")

        calls = [
            lambda: embedding.embed(["derived text"]),
            lambda: index.upsert([]),
            lambda: memory_index.upsert(SimpleNamespace(text="derived text")),
            lambda: knowledge.scan("synthetic"),
            lambda: memory.consolidate(None, [], None),
            lambda: extractor.extract("derived text"),
            lambda: extractor.summarize("", [], 100),
            lambda: operations.enqueue("memory.create", {"text": "derived text"}),
            lambda: ToolDiagnosticsMiddleware().process(
                SimpleNamespace(arguments={"text": "derived text"}), unapproved_tool
            ),
            lambda: runtime.chat.agent.extensions.load(None),
            lambda: runtime.memory.search("derived text"),
            lambda: runtime.retrieval.search(SimpleNamespace(query="derived text")),
        ]
        for call in calls:
            with pytest.raises(ProtectionDeniedError):
                await call()
        with pytest.raises(ProtectionDeniedError):
            cache.put("synthetic", [0.1])


async def test_sqlite_backstop_rejects_plaintext_chat_write_after_enterprise_ingress(runtime):
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.commit()
    with egress_scope():
        wrapped(reviewed_context())
        async with runtime.sessions() as session:
            session.add(
                ChatMessage(
                    conversation_id=conversation.id, sequence=1, role="assistant", content=BODY["body"]
                )
            )
            with pytest.raises(ProtectionDeniedError):
                await session.commit()
            await session.rollback()
    async with runtime.sessions() as session:
        assert not list(await session.scalars(select(ChatMessage)))
