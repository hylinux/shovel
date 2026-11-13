from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from agent_framework import MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from shovel.config.agent_config import AgentSettings
from shovel.exceptions.resource import CredentialBackendUnavailableError
from shovel.runtime.agent_extensions import AgentExtensions, MCPServerSettings, MCPSettings
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.mcp_auth import MCPCredentials, MCPHTTPStatus, MCPOAuthStorage
from shovel.runtime.mcp_config import MCPConfigurationService
from shovel.runtime.mcp_oauth import MCPOAuthService
from shovel.services.credentials import CredentialRef, InMemoryCredentialStore

SERVICE_ID = "12b9ae42-c658-4177-a3bb-f8b9b616edaf"


def configuration(**changes: Any) -> dict[str, Any]:
    return {
        "url": "https://mcp.example.test/mcp",
        "credential_id": SERVICE_ID,
        "auth": {"type": "bearer", "credential_ref": "env:MCP_TEST_TOKEN"},
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"credential_id": None},
        {"auth": {"type": "bearer", "credential_ref": "literal-secret"}},
        {"auth": {"type": "bearer", "credential_ref": "env:BAD-NAME"}},
        {"auth": {"type": "api_key", "header": "Host", "credential_ref": "env:TOKEN"}},
        {"auth": {"type": "api_key", "header": "X-Key\r\nBad", "credential_ref": "env:TOKEN"}},
        {"headers": {"authorization": "legacy-secret"}},
        {"headers": {"X-Key": "one", "x-key": "two"}},
        {
            "auth": {"type": "api_key", "header": "X-Key", "credential_ref": "env:TOKEN"},
            "headers": {"x-key": "conflict"},
        },
        {"auth": {"type": "oauth_user", "client_id": "id", "scopes": ["read write"]}},
        {"auth": {"type": "oauth_user", "client_id": "id", "scopes": ["read", "read"]}},
        {"auth": {"type": "oauth_user", "client_id": "id", "issuer": "http://remote.example.test"}},
        {"url": "http://remote.example.test/mcp"},
        {"url": "https://user:password@mcp.example.test/mcp"},
        {"url": "https://mcp.example.test/mcp?token=secret"},
        {"url": "https://mcp.example.test/mcp#fragment"},
        {"env_credentials": {"TOKEN": "env:TOKEN"}},
        {"url": "", "command": "python"},
        {"auth": {"type": "oauth_client_credentials", "client_id": "id", "client_secret_ref": "secret"}},
    ],
)
def test_invalid_authentication_configuration(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MCPServerSettings.model_validate(configuration(**changes))


def test_stdio_credentials_and_service_ids_are_unambiguous() -> None:
    stdio = {
        "command": "python",
        "credential_id": SERVICE_ID,
        "env_credentials": {"TOKEN": "env:MCP_TEST_TOKEN"},
    }
    MCPServerSettings.model_validate(stdio)
    for changes in ({"env": {"token": "conflict"}}, {"env_credentials": {"BAD=NAME": "env:TOKEN"}}):
        with pytest.raises(ValidationError):
            MCPServerSettings.model_validate({**stdio, **changes})
    with pytest.raises(ValidationError):
        MCPSettings.model_validate({"mcpServers": {"one": stdio, "two": stdio}})


async def test_keyring_namespace_and_binding_isolation(tmp_path: Path) -> None:
    store = InMemoryCredentialStore()
    server = MCPServerSettings.model_validate(
        configuration(auth={"type": "bearer", "credential_ref": "keyring:token"})
    )
    credentials = MCPCredentials(tmp_path, UUID(SERVICE_ID), server.credential_binding(), store)
    await credentials.write("keyring:token", "fixture-token")
    assert await credentials.get("keyring:token") == "fixture-token"
    assert credentials.storage_ref("keyring:token").account.startswith("mcp_")
    assert not store._values.get("keyring:token")
    for other in (
        MCPCredentials(tmp_path / "other", UUID(SERVICE_ID), server.credential_binding(), store),
        MCPCredentials(tmp_path, uuid4(), server.credential_binding(), store),
        MCPCredentials(
            tmp_path, UUID(SERVICE_ID), {**server.credential_binding(), "url": "https://other"}, store
        ),
    ):
        with pytest.raises(RuntimeFailure, match="missing"):
            await other.get("keyring:token")
    extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), credentials=store)
    _, headers = await extensions._mcp_credentials(server)
    assert headers == {"Authorization": "Bearer fixture-token"}


async def test_environment_endpoint_changes_cannot_reuse_keyring_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryCredentialStore()
    server = MCPServerSettings.model_validate(
        configuration(url="${MCP_TEST_URL}", auth={"type": "bearer", "credential_ref": "keyring:token"})
    )
    original = "https://first.example.test/mcp"
    binding = {**server.credential_binding(), "url": original}
    await MCPCredentials(tmp_path, UUID(SERVICE_ID), binding, store).write("keyring:token", "fixture-token")
    extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), credentials=store)
    monkeypatch.setenv("MCP_TEST_URL", original)
    assert (await extensions._mcp_credentials(server))[1]["Authorization"] == "Bearer fixture-token"
    monkeypatch.setenv("MCP_TEST_URL", "https://other.example.test/mcp")
    with pytest.raises(RuntimeFailure, match="missing"):
        await extensions._mcp_credentials(server)


@pytest.mark.parametrize("value", [None, "", "line\r\ninjection", "with space", "non-ascii-\u00e9", "nul\0"])
async def test_missing_and_invalid_bearer_values_fail_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("MCP_TEST_TOKEN", value) if "\0" not in value else None
    store = InMemoryCredentialStore()
    if value is not None and "\0" in value:
        store.set(CredentialRef.parse("env:MCP_TEST_TOKEN"), SecretStr(value))
    extensions = AgentExtensions(
        AgentSettings(workspace_path=tmp_path),
        credentials=store if value is not None and "\0" in value else None,
    )
    with pytest.raises(RuntimeFailure) as failure:
        await extensions._mcp_credentials(MCPServerSettings.model_validate(configuration()))
    if value:
        assert value not in str(failure.value)


class UnavailableStore(InMemoryCredentialStore):
    def get(self, ref: CredentialRef) -> SecretStr:
        raise CredentialBackendUnavailableError("fixture-private-backend-details")

    def set(self, ref: CredentialRef, secret: SecretStr) -> None:
        raise CredentialBackendUnavailableError("fixture-private-backend-details")


async def test_oauth_storage_preserves_both_records_and_clears_secrets(tmp_path: Path) -> None:
    credentials = MCPCredentials(
        tmp_path, UUID(SERVICE_ID), {"issuer": "https://issuer.test"}, InMemoryCredentialStore()
    )
    storage = MCPOAuthStorage(credentials)
    assert await storage.get_tokens() is None
    tokens = OAuthToken(access_token="fixture-access", token_type="Bearer", refresh_token="fixture-refresh")
    client = OAuthClientInformationFull(
        client_id="fixture-client", redirect_uris=["http://127.0.0.1/callback"]
    )
    await asyncio.gather(storage.set_tokens(tokens), storage.set_client_info(client))
    assert await storage.get_tokens() == tokens
    assert await storage.get_client_info() == client
    await storage.clear()
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None
    assert "fixture-access" not in (await credentials.get("keyring:oauth-state"))
    await credentials.write("keyring:oauth-state", "invalid-private-state")
    with pytest.raises(RuntimeFailure, match="invalid") as failure:
        await storage.get_tokens()
    assert "private-state" not in str(failure.value)


async def test_unavailable_store_never_falls_back_or_claims_clear_success(tmp_path: Path) -> None:
    credentials = MCPCredentials(tmp_path, UUID(SERVICE_ID), {}, UnavailableStore())
    for operation in (credentials.get("keyring:token"), MCPOAuthStorage(credentials).clear()):
        with pytest.raises(RuntimeFailure, match="unavailable") as failure:
            await operation
        assert "private-backend" not in str(failure.value)
    assert not list(tmp_path.iterdir())


async def test_settings_credential_write_reports_store_failure_without_secrets(tmp_path: Path) -> None:
    config = MCPConfigurationService(tmp_path)
    view = config.save(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": configuration(auth={"type": "bearer", "credential_ref": "keyring:token"})
                }
            }
        ),
        config.read()["revision"],
    )
    service = MCPOAuthService(config, credentials=UnavailableStore())
    with pytest.raises(RuntimeFailure, match="unavailable") as failure:
        await service.save_credential("fixture", view["revision"], "keyring:token", "fixture-new-secret")
    assert "fixture-new-secret" not in str(failure.value)
    assert "private-backend" not in str(failure.value)
    with pytest.raises(RuntimeFailure) as invalid:
        await service.save_credential("fixture", view["revision"], "keyring:token", "invalid\nsecret")
    assert invalid.value.status == 422 and "invalid\nsecret" not in str(invalid.value)
    await service.close()


async def test_cancelled_settings_credential_write_drains_before_configuration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MCPConfigurationService(tmp_path)
    view = config.save(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": configuration(auth={"type": "bearer", "credential_ref": "keyring:token"})
                }
            }
        ),
        config.read()["revision"],
    )
    store = InMemoryCredentialStore()
    service = MCPOAuthService(config, credentials=store)
    started, release = asyncio.Event(), asyncio.Event()
    original = MCPCredentials.write

    async def blocked(self: MCPCredentials, reference: str, value: str) -> None:
        started.set()
        await release.wait()
        await original(self, reference, value)

    monkeypatch.setattr(MCPCredentials, "write", blocked)
    task = asyncio.create_task(
        service.save_credential("fixture", view["revision"], "keyring:token", "fixture-new-secret")
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    changing = asyncio.create_task(service.save_configuration("{}", view["revision"]))
    await asyncio.sleep(0)
    assert not changing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await changing
    server = MCPSettings.model_validate_json(view["content"]).mcpServers["fixture"]
    credentials = MCPCredentials(tmp_path, server.credential_id, server.resolved_credential_binding(), store)
    assert await credentials.get("keyring:token") == "fixture-new-secret"
    await service.close()


def test_locked_sdk_oauth_and_pre_registered_storage_contract(tmp_path: Path) -> None:
    storage = MCPOAuthStorage(MCPCredentials(tmp_path, UUID(SERVICE_ID), {}, InMemoryCredentialStore()))
    metadata = OAuthClientMetadata(redirect_uris=["http://127.0.0.1/callback"])
    user = OAuthClientProvider("https://mcp.example.test/mcp", metadata, storage)
    application = ClientCredentialsOAuthProvider(
        "https://mcp.example.test/mcp", storage, "id", "fixture-secret", issuer="https://issuer.example.test"
    )
    assert isinstance(user, httpx.Auth)
    assert isinstance(application, httpx.Auth)


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "oauth_user", "client_id": "id"},
        {
            "type": "oauth_client_credentials",
            "client_id": "id",
            "client_secret_ref": "env:APP_SECRET",
            "issuer": "https://issuer.example.test",
        },
    ],
)
async def test_oauth_contract_is_saveable_but_never_connects_unauthenticated(
    tmp_path: Path, auth: dict[str, str]
) -> None:
    service = MCPConfigurationService(tmp_path)
    saved = service.save(
        json.dumps({"mcpServers": {"test": configuration(auth=auth)}}), service.read()["revision"]
    )
    assert saved["validation_error"] is None
    async with AsyncOpenAI(api_key="fixture") as sdk:
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        with pytest.raises(RuntimeFailure, match="Host authentication service"):
            await extensions.load(OpenAIChatCompletionClient(model="fixture", async_client=sdk))
        assert extensions.tools == []
        await extensions.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_protected_http_failure_is_explicit_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "fixture-private-token")
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"protected": configuration()}}))
    clients: list[httpx.AsyncClient] = []
    async with AsyncOpenAI(api_key="fixture") as sdk:

        def authenticated_client(observed: MCPHTTPStatus) -> httpx.AsyncClient:
            http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text="fixture-private-response")
                ),
                event_hooks={"response": [observed.observe]},
            )
            clients.append(http_client)
            return http_client

        monkeypatch.setattr("shovel.runtime.agent_extensions.authenticated_http_client", authenticated_client)
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path))
        with pytest.raises(RuntimeFailure, match=f"HTTP {status}") as failure:
            await extensions.load(OpenAIChatCompletionClient(model="fixture", async_client=sdk))
        assert "fixture-private" not in str(failure.value)
        assert "protected" in str(failure.value)
        assert extensions.tools == []
        await extensions.close()
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example.test/mcp",
        "http://first.example.test/mcp",
        "https://first.example.test/normalized",
    ],
)
async def test_static_authentication_headers_are_origin_bound(location: str) -> None:
    seen: list[tuple[str, str | None]] = []

    def response(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-api-key")))
        if request.url.path == "/mcp":
            return httpx.Response(307, headers={"location": location})
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=True) as client:
        connection = MCPStreamableHTTPTool(
            name="fixture",
            url="https://first.example.test/mcp",
            static_headers={"X-API-Key": "fixture-secret"},
            http_client=client,
        )
        with pytest.raises(Exception):
            async with connection:
                pytest.fail("The redirected server must not accept authentication.")
    assert seen
    if location == "https://first.example.test/normalized":
        assert any(url == location for url, _ in seen)
    else:
        assert all(url != location for url, _ in seen)
    assert all(value == "fixture-secret" for _, value in seen)


async def test_configuration_roundtrip_contains_references_not_resolved_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "fixture-private-secret")
    service = MCPConfigurationService(tmp_path)
    content = json.dumps({"mcpServers": {"test": configuration()}})
    saved = service.save(content, service.read()["revision"])
    assert saved["content"] == content
    assert saved["warnings"] == []
    assert "fixture-private-secret" not in json.dumps(service.read())
    with pytest.raises(RuntimeFailure) as failure:
        service.save(content, "stale-revision")
    assert failure.value.status == 409
    assert service.path.read_text() == content
    legacy = json.dumps(
        {"mcpServers": {"legacy": {"url": "http://localhost/mcp", "headers": {"Authorization": "legacy"}}}}
    )
    saved = service.save(legacy, saved["revision"])
    assert saved["warnings"]
    assert saved["content"] == legacy


async def test_disabled_credentials_do_not_resolve_or_start_processes(tmp_path: Path) -> None:
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"disabled": {**configuration(), "disabled": True}}})
    )
    async with AsyncOpenAI(api_key="fixture") as sdk:
        extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), credentials=UnavailableStore())
        await extensions.load(OpenAIChatCompletionClient(model="fixture", async_client=sdk))
        assert extensions.tools == []
        await extensions.close()
