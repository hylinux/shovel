import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.policies import _authentication
from azure.identity import CredentialUnavailableError

from shovel.config.model_settings import DefaultAgentModelSettings
from shovel.runtime.agent import create_framework_client
from shovel.runtime.errors import RuntimeFailure
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
            azure_settings(model_provider_type=provider, security_key="test-api-key"),
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
