import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agent_framework import Agent, FunctionTool
from agent_framework.exceptions import ChatClientException
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.policies import _authentication
from azure.identity import CredentialUnavailableError

from shovel.config.errors import ConfigError
from shovel.config.model_settings import DefaultAgentModelSettings
from shovel.runtime.agent import create_framework_client
from shovel.runtime.errors import RuntimeFailure
from shovel.services.health import HealthStatus, check_model
from test_agent_extensions import completion


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    value = Mock(spec=["get_token", "close"])
    value.get_token = AsyncMock()
    value.close = AsyncMock()
    factory = Mock(return_value=value)
    monkeypatch.setattr("shovel.runtime.azure_auth.AzureCliCredential", factory)
    return value, factory


def azure_settings(**overrides):
    return DefaultAgentModelSettings(
        **{
            "model_provider_type": "azure_openai",
            "base_endpoint": "https://example.openai.azure.com",
            "model_name": "chat-deployment",
            "api_version": "2025-04-01-preview",
            **overrides,
        }
    )


def foundry_settings(**overrides):
    return azure_settings(
        **{
            "model_provider_type": "azure_foundry",
            "base_endpoint": "https://example.services.ai.azure.com",
            "api_version": "",
            **overrides,
        }
    )


@pytest.mark.parametrize("key", ["", "test-foundry-key"])
async def test_foundry_v1_chat_uses_deployment_bearer_auth_and_correct_cli_scope(credential, key):
    value, factory = credential
    value.get_token.return_value = AccessToken("test-foundry-token", int(time.time()) + 3600)
    requests = []

    def model(request):
        requests.append(request)
        assert request.url.path == "/openai/v1/chat/completions"
        assert not request.url.query
        assert request.headers["authorization"] == f"Bearer {key or 'test-foundry-token'}"
        assert "api-key" not in request.headers
        assert json.loads(request.content)["model"] == "chat-deployment"
        return completion(content="Foundry response.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(foundry_settings(api_key=key), http, max_iterations=3)
        try:
            value.get_token.assert_not_awaited()
            assert (await client.get_response("Foundry test.")).text == "Foundry response."
            await client.get_response("Cached token.")
        finally:
            await sdk.close()
    assert len(requests) == 2
    if key:
        factory.assert_not_called()
    else:
        value.get_token.assert_awaited_once_with("https://ai.azure.com/.default")
        value.close.assert_awaited_once()


async def test_foundry_v1_stream_preserves_tool_definition(credential):
    _, factory = credential

    def model(request):
        data = json.loads(request.content)
        assert data["stream"] and data["tools"][0]["function"]["name"] == "lookup"
        update = {
            "id": "test-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "chat-deployment",
            "choices": [{"index": 0, "delta": {"content": "Foundry streamed"}, "finish_reason": None}],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(update)}\n\ndata: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, _ = create_framework_client(foundry_settings(api_key="test-key"), http, max_iterations=3)
        try:
            stream = await sdk.chat.completions.create(
                model="chat-deployment",
                messages=[{"role": "user", "content": "test"}],
                stream=True,
                tools=[
                    {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
                ],
            )
            assert (
                "".join([chunk.choices[0].delta.content or "" async for chunk in stream])
                == "Foundry streamed"
            )
        finally:
            await sdk.close()
    factory.assert_not_called()


def test_foundry_health_uses_v1_models_and_cli_not_legacy_azure_routes(credential, monkeypatch):
    from shovel.runtime.azure_auth import FoundryCliOpenAIClient

    value, _ = credential
    value.get_token.return_value = AccessToken("test-foundry-token", int(time.time()) + 3600)
    requests = []

    def response(request):
        requests.append(request)
        assert request.url.path == "/openai/v1/models" and not request.url.query
        assert request.headers["authorization"] == "Bearer test-foundry-token"
        return httpx.Response(200, json={"object": "list", "data": []})

    def client(**kwargs):
        return FoundryCliOpenAIClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)), **kwargs
        )

    monkeypatch.setattr("shovel.runtime.azure_auth.FoundryCliOpenAIClient", client)
    assert check_model(foundry_settings()).status is HealthStatus.OK
    assert len(requests) == 1
    value.get_token.assert_awaited_once_with("https://ai.azure.com/.default")
    value.close.assert_awaited_once()


async def test_foundry_cli_failure_has_no_fallback_and_does_not_expose_credentials(credential):
    value, _ = credential
    value.get_token.side_effect = CredentialUnavailableError("private credential details")
    response = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        sdk, _ = create_framework_client(foundry_settings(), http, max_iterations=3)
        try:
            with pytest.raises(RuntimeFailure, match="Azure CLI authentication failed") as failure:
                await sdk.chat.completions.create(
                    model="chat-deployment", messages=[{"role": "user", "content": "test"}]
                )
            assert "Microsoft Foundry" in failure.value.hint
            assert "private" not in str(failure.value)
        finally:
            await sdk.close()
    response.assert_not_called()
    value.close.assert_awaited_once()


@pytest.mark.parametrize("status", [200, 401])
def test_foundry_key_health_uses_bearer_and_implicit_version(credential, monkeypatch, status):
    _, factory = credential
    get = Mock(return_value=httpx.Response(status))
    monkeypatch.setattr(httpx, "get", get)
    result = check_model(foundry_settings(api_key="test-foundry-key"))
    assert result.status is (HealthStatus.OK if status == 200 else HealthStatus.FAILED)
    assert get.call_args.args[0] == "https://example.services.ai.azure.com/openai/v1/models"
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer test-foundry-key"}
    factory.assert_not_called()


async def test_foundry_memory_extraction_and_summary_share_v1_client(credential):
    from shovel.config.memory_config import MemoryModelSettings
    from shovel.memory.agent_framework import AgentFrameworkMemoryExtractor

    _, factory = credential
    calls = []

    def model(request):
        data = json.loads(request.content)
        calls.append(data)
        assert request.url.path == "/openai/v1/chat/completions" and not request.url.query
        assert request.headers["authorization"] == "Bearer test-memory-foundry"
        assert not data.get("tools")
        if len(calls) == 1:
            assert data["response_format"]["type"] == "json_schema"
            content = '{"candidates": []}'
        else:
            content = "The user greeted the assistant."
        return completion(content=content)

    settings = MemoryModelSettings(**foundry_settings(api_key="test-memory-foundry").model_dump())
    extractor = AgentFrameworkMemoryExtractor(settings, lambda: httpx.MockTransport(model))
    try:
        assert await extractor.extract("Hello.") == []
        assert await extractor.summarize("", [{"role": "user", "content": "Hello."}], 2000) == (
            "The user greeted the assistant."
        )
    finally:
        await extractor.close()
    assert len(calls) == 2
    factory.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"base_endpoint": "https://example.services.ai.azure.com/models"},
        {"base_endpoint": "https://example.services.ai.azure.com/api/projects/test"},
        {"base_endpoint": "https://example.services.ai.azure.com/openai/v1", "api_version": "2025-04-01"},
    ],
)
def test_foundry_rejects_incompatible_endpoint_and_version(fields):
    with pytest.raises(ConfigError):
        foundry_settings(**fields)


def responses_payload(text="Responses answer.", *, output=None):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "chat-deployment",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "metadata": {},
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        "output": output
        if output is not None
        else [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


@pytest.mark.parametrize(
    "provider,endpoint,version,key",
    [
        ("azure_foundry", "https://example.services.ai.azure.com", "", ""),
        ("azure_foundry", "https://example.services.ai.azure.com", "", "test-key"),
        ("azure_foundry", "https://example.cognitiveservices.azure.com", "2025-04-01-preview", ""),
        ("azure_openai", "https://example.openai.azure.com", "2025-04-01-preview", ""),
        ("azure_openai", "https://example.openai.azure.com", "2025-04-01-preview", "test-key"),
        ("openai", "https://example.test/v1", "", "test-key"),
    ],
)
async def test_responses_routing_auth_budget_and_local_history(credential, provider, endpoint, version, key):
    value, factory = credential
    value.get_token.return_value = AccessToken("responses-token", int(time.time()) + 3600)

    def model(request):
        assert request.url.path == (
            "/openai/responses"
            if version
            else ("/v1/responses" if provider == "openai" else "/openai/v1/responses")
        )
        assert request.url.params.get("api-version") == (version or None)
        if version and key:
            assert request.headers["api-key"] == key
        else:
            assert request.headers["authorization"] == f"Bearer {key or 'responses-token'}"
        data = json.loads(request.content)
        assert data["model"] == "chat-deployment"
        assert data["max_output_tokens"] == 32 and not data["store"]
        assert "previous_response_id" not in data and "conversation" not in data
        return httpx.Response(200, json=responses_payload())

    settings = DefaultAgentModelSettings(
        model_provider_type=provider,
        base_endpoint=endpoint,
        api_version=version,
        model_name="chat-deployment",
        api_key=key,
        api_mode="responses",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(settings, http, max_iterations=3)
        try:
            response = await client.get_response("hello", options={"max_tokens": 32, "store": True})
            assert response.text == "Responses answer."
            assert response.conversation_id is None
        finally:
            await sdk.close()
    if key:
        factory.assert_not_called()
    else:
        scope = "https://cognitiveservices.azure.com/.default" if version else "https://ai.azure.com/.default"
        value.get_token.assert_awaited_once_with(scope)
        value.close.assert_awaited_once()


async def test_responses_agent_completes_local_tool_loop_and_replays_reasoning():
    calls = []
    executed = []

    def lookup(value: str) -> str:
        executed.append(value)
        return "lookup-result"

    def model(request):
        data = json.loads(request.content)
        calls.append(data)
        assert request.url.path == "/openai/v1/responses"
        assert data["store"] is False and "previous_response_id" not in data
        if len(calls) == 1:
            assert data["tools"][0]["name"] == "lookup"
            return httpx.Response(
                200,
                json=responses_payload(
                    output=[
                        {
                            "type": "reasoning",
                            "id": "rs_test",
                            "summary": [],
                            "encrypted_content": "test-encrypted",
                        },
                        {
                            "type": "function_call",
                            "id": "fc_test",
                            "call_id": "call_test",
                            "name": "lookup",
                            "arguments": '{"value":"marker"}',
                            "status": "completed",
                        },
                    ]
                ),
            )
        results = [item for item in data["input"] if item["type"] == "function_call_output"]
        assert len(results) == 1 and results[0]["call_id"] == "call_test"
        assert "lookup-result" in results[0]["output"]
        assert any(item.get("encrypted_content") == "test-encrypted" for item in data["input"])
        return httpx.Response(200, json=responses_payload("Tool completed."))

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(
            foundry_settings(api_key="test-key", api_mode="responses"), http, max_iterations=3
        )
        try:
            agent = Agent(client=client, tools=[FunctionTool(name="lookup", func=lookup)])
            assert (await agent.run("Look up marker.")).text == "Tool completed."
        finally:
            await sdk.close()
    assert executed == ["marker"] and len(calls) == 2


async def test_responses_stream_translates_sse_into_framework_updates():
    def model(request):
        data = json.loads(request.content)
        assert data["stream"] and not data["store"]
        payload = responses_payload("Streamed answer.")
        events = [
            {"type": "response.created", "response": {**payload, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": payload["output"][0]},
            {
                "type": "response.output_text.delta",
                "item_id": "msg_test",
                "output_index": 0,
                "content_index": 0,
                "delta": "Streamed answer.",
            },
            {"type": "response.completed", "response": payload},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(
            foundry_settings(api_key="test-key", api_mode="responses"), http, max_iterations=3
        )
        try:
            updates = [update async for update in client.get_response("hello", stream=True)]
            assert "".join(update.text or "" for update in updates) == "Streamed answer."
            assert any(update.finish_reason == "stop" for update in updates)
        finally:
            await sdk.close()


async def test_responses_memory_translates_json_schema_and_summary():
    from shovel.memory.agent_framework import AgentFrameworkMemoryExtractor

    calls = []

    def model(request):
        data = json.loads(request.content)
        calls.append(data)
        assert not data["store"] and not data.get("tools")
        if len(calls) == 1:
            assert data["text"]["format"]["type"] == "json_schema"
            assert data["text"]["format"]["name"] == "memory_candidates"
            assert data["text"]["format"]["strict"]
            schema = data["text"]["format"]["schema"]["$defs"]["CandidateOutput"]
            assert set(schema["required"]) == set(schema["properties"])
            assert all("default" not in field for field in schema["properties"].values())
            return httpx.Response(200, json=responses_payload('{"candidates": []}'))
        return httpx.Response(200, json=responses_payload("The user greeted the assistant."))

    extractor = AgentFrameworkMemoryExtractor(
        foundry_settings(api_key="test-key", api_mode="responses"), lambda: httpx.MockTransport(model)
    )
    try:
        assert await extractor.extract("Hello.") == []
        assert await extractor.summarize("", [{"role": "user", "content": "Hello."}], 500)
    finally:
        await extractor.close()
    assert len(calls) == 2


async def test_responses_errors_do_not_retry_or_fall_back_and_remote_ids_are_rejected():
    calls = []

    def model(request):
        calls.append(request)
        return httpx.Response(
            400, json={"error": {"message": "unsupported", "type": "invalid_request_error"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(
            foundry_settings(api_key="test-key", api_mode="responses"), http, max_iterations=3
        )
        try:
            with pytest.raises(RuntimeFailure, match="local Session history"):
                await client.get_response("hello", options={"conversation_id": "resp_remote"})
            assert not calls
            with pytest.raises(ChatClientException):
                await client.get_response("hello")
        finally:
            await sdk.close()
    assert len(calls) == 1 and calls[0].url.path == "/openai/v1/responses"


@pytest.mark.parametrize(
    "provider,version",
    [
        ("azure_foundry", "2025-01-01-preview"),
        ("azure_openai", ""),
        ("azure_openai", "invalid"),
        ("ollama", ""),
        ("deepseek", ""),
    ],
)
def test_responses_rejects_unsupported_provider_or_version(provider, version):
    with pytest.raises(ConfigError):
        DefaultAgentModelSettings(
            model_provider_type=provider,
            model_name="deployment",
            base_endpoint="https://example.test",
            api_mode="responses",
            api_version=version,
        )


@pytest.fixture
def health_probe(monkeypatch):
    from shovel.runtime.azure_auth import AzureCliOpenAIClient

    response = Mock(return_value=httpx.Response(200, json={"object": "list", "data": []}))

    def client(**kwargs):
        return AzureCliOpenAIClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)), **kwargs
        )

    monkeypatch.setattr("shovel.runtime.azure_auth.AzureCliOpenAIClient", client)
    monkeypatch.setattr(
        httpx, "get", Mock(side_effect=AssertionError("Azure CLI health must not use anonymous HTTP."))
    )
    return response


def test_model_health_azure_cli_sends_bearer_token_and_closes_credential(credential, health_probe):
    value, _ = credential
    value.get_token.return_value = AccessToken("test-health-token", int(time.time()) + 3600)
    result = check_model(azure_settings())
    assert result.status is HealthStatus.OK
    health_probe.assert_called_once()
    request = health_probe.call_args.args[0]
    assert request.method == "GET"
    assert request.url.path == "/openai/models"
    assert request.url.params["api-version"] == "2025-04-01-preview"
    assert request.headers["authorization"] == "Bearer test-health-token"
    assert "api-key" not in request.headers
    value.close.assert_awaited_once()


def test_model_health_cli_login_failure_is_not_reported_as_missing_api_key(credential, health_probe):
    value, _ = credential
    value.get_token.side_effect = CredentialUnavailableError("private credential failure details")
    result = check_model(azure_settings())
    assert result.status is HealthStatus.FAILED
    assert "az login" in result.detail
    assert "model.api_key" not in result.detail
    assert "private credential failure details" not in result.detail
    health_probe.assert_not_called()
    value.close.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403])
def test_model_health_cli_rejection_reports_azure_access_without_leaking_response(
    credential, health_probe, status
):
    value, _ = credential
    value.get_token.return_value = AccessToken("test-health-token", int(time.time()) + 3600)
    health_probe.return_value = httpx.Response(
        status, json={"error": {"message": "private provider failure details"}}
    )
    result = check_model(azure_settings())
    assert result.status is HealthStatus.FAILED
    assert "Azure CLI" in result.detail and str(status) in result.detail
    assert "model.api_key" not in result.detail
    assert "private provider failure details" not in result.detail
    assert "test-health-token" not in result.detail
    health_probe.assert_called_once()
    value.close.assert_awaited_once()


@pytest.mark.parametrize("status", [200, 401])
def test_model_health_explicit_key_does_not_use_cli(credential, monkeypatch, status):
    _, factory = credential
    get = Mock(return_value=httpx.Response(status))
    monkeypatch.setattr(httpx, "get", get)
    result = check_model(azure_settings(api_key="test-api-key"))
    assert result.status is (HealthStatus.OK if status == 200 else HealthStatus.FAILED)
    assert get.call_args.kwargs["headers"] == {"api-key": "test-api-key"}
    if status == 401:
        assert "model.api_key" in result.detail
    factory.assert_not_called()


def test_model_health_requires_azure_api_version_before_network(credential, monkeypatch):
    _, factory = credential
    get = Mock()
    monkeypatch.setattr(httpx, "get", get)
    result = check_model(azure_settings(api_version=""))
    assert result.status is HealthStatus.FAILED
    assert "model.api_version" in result.detail
    get.assert_not_called()
    factory.assert_not_called()


def test_model_health_rejects_static_token_in_cli_mode(credential, monkeypatch):
    _, factory = credential
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "private-static-token")
    get = Mock()
    monkeypatch.setattr(httpx, "get", get)
    result = check_model(azure_settings())
    assert result.status is HealthStatus.FAILED
    assert "AZURE_OPENAI_AD_TOKEN" in result.detail
    assert "private-static-token" not in result.detail
    get.assert_not_called()
    factory.assert_not_called()


async def test_azure_cli_authentication_is_lazy_cached_and_refreshes_before_expiry(credential, monkeypatch):
    value, factory = credential
    clock = [1_000_000]
    monkeypatch.setattr(_authentication, "time", SimpleNamespace(time=lambda: clock[0]))
    value.get_token.side_effect = [
        AccessToken("test-token-one", clock[0] + 3600),
        AccessToken("test-token-two", clock[0] + 7200),
    ]
    requests = []

    def model(request):
        requests.append(request)
        assert request.url.path == "/openai/deployments/chat-deployment/chat/completions"
        assert request.url.params["api-version"] == "2025-04-01-preview"
        assert "api-key" not in request.headers
        return completion(content="Azure response.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(azure_settings(), http, max_iterations=3)
        try:
            factory.assert_called_once_with(process_timeout=30)
            value.get_token.assert_not_awaited()
            assert (await client.get_response("First request.")).text == "Azure response."
            clock[0] += 30
            await client.get_response("Cached request.")
            assert value.get_token.await_count == 1
            clock[0] += 3470
            await client.get_response("Refresh request.")
            assert value.get_token.await_count == 2
            assert [request.headers["authorization"] for request in requests] == [
                "Bearer test-token-one",
                "Bearer test-token-one",
                "Bearer test-token-two",
            ]
            assert all(
                call.args == ("https://cognitiveservices.azure.com/.default",)
                for call in value.get_token.await_args_list
            )
        finally:
            await sdk.close()
    value.close.assert_awaited_once()


@pytest.mark.parametrize("provider", ["azure_openai", "azure_foundry"])
async def test_explicit_azure_api_key_preserves_authentication_without_cli(credential, provider):
    value, factory = credential

    def model(request):
        assert request.headers["api-key"] == "test-api-key"
        assert "authorization" not in request.headers
        return completion(content="API key response.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(
            azure_settings(model_provider_type=provider, api_key="test-api-key"),
            http,
            max_iterations=3,
        )
        try:
            assert (await client.get_response("Use the configured key.")).text == "API key response."
        finally:
            await sdk.close()
    factory.assert_not_called()
    value.get_token.assert_not_awaited()
    value.close.assert_not_awaited()


@pytest.mark.parametrize("provider", ["openai", "deepseek", "qianwen"])
async def test_canonical_api_key_authenticates_openai_compatible_chat(credential, provider):
    _, factory = credential

    def model(request):
        assert request.headers["authorization"] == "Bearer test-chat-api-key"
        return completion(content="API key response.")

    settings = DefaultAgentModelSettings(
        model_provider_type=provider,
        base_endpoint="https://chat.example.test/v1",
        model_name="test-model",
        api_key="test-chat-api-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, client = create_framework_client(settings, http, max_iterations=3)
        try:
            assert (await client.get_response("Use the configured API key.")).text == "API key response."
        finally:
            await sdk.close()
    factory.assert_not_called()


@pytest.mark.parametrize("error", [CredentialUnavailableError, ClientAuthenticationError])
async def test_cli_authentication_failure_is_safe_and_does_not_fall_back_to_keys(
    credential, monkeypatch, error
):
    value, _ = credential
    value.get_token.side_effect = error("private credential failure details")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "unexpected-environment-key")
    requests = []

    def model(request):
        requests.append(request)
        return completion(content="Must not be called.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        sdk, _ = create_framework_client(azure_settings(), http, max_iterations=3)
        try:
            with pytest.raises(RuntimeFailure, match="Azure CLI authentication failed") as failure:
                await sdk.chat.completions.create(
                    model="chat-deployment", messages=[{"role": "user", "content": "Test."}]
                )
            assert failure.value.status == 503
            assert "az login" in failure.value.hint
            assert "private credential failure details" not in str(failure.value)
            assert not requests
        finally:
            await sdk.close()
    value.close.assert_awaited_once()


async def test_cli_authentication_cancellation_is_not_reported_as_login_failure(credential):
    value, _ = credential
    value.get_token.side_effect = asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion())) as http:
        sdk, _ = create_framework_client(azure_settings(), http, max_iterations=3)
        try:
            with pytest.raises(asyncio.CancelledError):
                await sdk.chat.completions.create(
                    model="chat-deployment", messages=[{"role": "user", "content": "Test."}]
                )
        finally:
            await sdk.close()
    value.close.assert_awaited_once()


async def test_static_ad_token_cannot_bypass_cli_token_refresh(credential, monkeypatch):
    _, factory = credential
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "static-token")
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeFailure, match="cannot be combined"):
            create_framework_client(azure_settings(), http, max_iterations=3)
    factory.assert_not_called()
