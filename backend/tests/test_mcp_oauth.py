from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from fastapi import FastAPI, Request
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI
from starlette.responses import JSONResponse, RedirectResponse, Response

from shovel.config.agent_config import AgentSettings
from shovel.config.settings import AppSettings
from shovel.runtime.agent_extensions import AgentExtensions
from shovel.runtime.api import configure_api, current_runtime
from shovel.runtime.diagnostics import DevelopmentRequestLogging, development_logging
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.mcp_auth import MCPCredentials, MCPHTTPStatus, authenticated_http_client
from shovel.runtime.mcp_config import MCPConfigurationService
from shovel.runtime.mcp_oauth import MCPOAuthService
from shovel.runtime.mcp_oauth_http import OAuthTransport
from shovel.services.credentials import ChainedCredentialStore, EnvCredentialStore, InMemoryCredentialStore
from test_agent_extensions import completion, tool_call

BASE = "http://127.0.0.1:8765"
CALLBACK = "http://127.0.0.1:8080/api/v1/agent/mcp/oauth/callback"


class OAuthFixture:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.server = FastMCP("oauth-fixture", stateless_http=True, json_response=True)
        self.forms: list[dict[str, str]] = []
        self.authorizations: dict[str, dict[str, str]] = {}
        self.valid_tokens: set[str] = set()
        self.revoked: list[str] = []
        self.tool_calls = 0
        self.rejected_calls = 0
        self.deny_tools = False
        self.fail_token = False
        self.token_started = asyncio.Event()
        self.token_release = asyncio.Event()
        self.token_release.set()
        self.metadata_changes: dict[str, Any] = {}
        self.protected_changes: dict[str, Any] = {}
        self.revoke_status = 200
        self.token_changes: dict[str, Any] = {}
        self.authorization_metadata_status = 200
        self.issuer = BASE + "/issuer"

        @self.server.tool()
        def echo(value: str) -> str:
            self.tool_calls += 1
            return "oauth marker: " + value

        mcp_app = self.server.streamable_http_app()

        @self.app.middleware("http")
        async def authenticate(request: Request, call_next: Any) -> Response:
            if request.url.path == "/mcp":
                value = request.headers.get("authorization", "")
                if value.removeprefix("Bearer ") not in self.valid_tokens or self.deny_tools:
                    self.rejected_calls += 1
                    return JSONResponse(
                        {"error": "fixture-no-secret-echo"},
                        status_code=401,
                        headers={
                            "WWW-Authenticate": (
                                f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp",'
                                " "
                                'scope="tools.read"'
                            )
                        },
                    )
            return await call_next(request)

        @self.app.get("/.well-known/oauth-protected-resource/mcp")
        async def resource() -> dict[str, Any]:
            return {
                "resource": BASE + "/mcp",
                "authorization_servers": [self.issuer],
                "scopes_supported": ["tools.read"],
                **self.protected_changes,
            }

        @self.app.get("/.well-known/oauth-authorization-server/issuer")
        @self.app.get("/issuer/.well-known/openid-configuration")
        async def metadata(request: Request) -> JSONResponse:
            if (
                request.url.path.startswith("/.well-known/oauth-authorization-server")
                and self.authorization_metadata_status != 200
            ):
                return JSONResponse({}, status_code=self.authorization_metadata_status)
            return JSONResponse(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": BASE + "/authorize",
                    "token_endpoint": BASE + "/token",
                    "revocation_endpoint": BASE + "/revoke",
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["tools.read"],
                    "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
                    "token_endpoint_auth_methods_supported": [
                        "none",
                        "client_secret_basic",
                        "client_secret_post",
                    ],
                    **self.metadata_changes,
                }
            )

        @self.app.get("/authorize")
        async def authorize(request: Request) -> RedirectResponse:
            query = dict(request.query_params)
            code = f"fixture-code-{len(self.authorizations)}"
            self.authorizations[code] = query
            return RedirectResponse(
                query["redirect_uri"]
                + "?"
                + urlencode(
                    {
                        "code": code,
                        "state": query["state"],
                        "iss": self.issuer,
                    }
                )
            )

        @self.app.post("/token")
        async def token(request: Request) -> JSONResponse:
            data = {key: value[0] for key, value in parse_qs((await request.body()).decode()).items()}
            self.forms.append(data)
            self.token_started.set()
            await self.token_release.wait()
            if self.fail_token:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "fixture-private"}, status_code=400
                )
            assert data["resource"] == BASE + "/mcp"
            if data["grant_type"] == "authorization_code":
                original = self.authorizations.pop(data["code"])
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                assert challenge == original["code_challenge"]
                assert data["redirect_uri"] == original["redirect_uri"]
            elif data["grant_type"] == "client_credentials":
                expected = "Basic " + base64.b64encode(b"fixture-client:fixture-app-secret").decode()
                if (
                    request.headers.get("authorization") != expected
                    and data.get("client_secret") != "fixture-app-secret"
                ):
                    return JSONResponse({"error": "invalid_client"}, status_code=401)
            access = f"fixture-access-{len(self.forms)}"
            self.valid_tokens.add(access)
            return JSONResponse(
                {
                    "access_token": access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "tools.read",
                    "refresh_token": f"fixture-refresh-{len(self.forms)}",
                    **self.token_changes,
                }
            )

        @self.app.post("/revoke")
        async def revoke(request: Request) -> Response:
            data = parse_qs((await request.body()).decode())
            self.revoked.append(data["token"][0])
            return Response(status_code=self.revoke_status)

        self.app.mount("/", mcp_app)

    def configuration(self, kind: str = "oauth_user", **changes: Any) -> dict[str, Any]:
        auth = {
            "type": kind,
            "client_id": "fixture-client",
            "issuer": self.issuer,
            "scopes": ["tools.read"],
            **({"client_secret_ref": "env:MCP_APP_SECRET"} if kind == "oauth_client_credentials" else {}),
            **changes,
        }
        return {
            "mcpServers": {
                "fixture": {
                    "url": BASE + "/mcp",
                    "credential_id": "12b9ae42-c658-4177-a3bb-f8b9b616edaf",
                    "auth": auth,
                }
            }
        }

    async def consent(self, result: dict[str, Any]) -> dict[str, str]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
            response = await client.get(result["authorization_url"])
        return {
            key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()
        }


@pytest_asyncio.fixture
async def provider(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OAuthFixture]:
    fixture = OAuthFixture()

    class LocalTransport(OAuthTransport):
        def __init__(self, server_url: str, guard: Any = None) -> None:
            super().__init__(server_url, guard)
            self.inner = httpx.ASGITransport(app=fixture.app)

    monkeypatch.setattr("shovel.runtime.mcp_oauth_http.OAuthTransport", LocalTransport)
    monkeypatch.setenv("MCP_APP_SECRET", "fixture-app-secret")
    ready, stop = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        async with fixture.server.session_manager.run():
            ready.set()
            await stop.wait()

    worker = asyncio.create_task(run())
    await ready.wait()
    try:
        yield fixture
    finally:
        stop.set()
        await worker


def service_at(
    tmp_path: Path, provider: OAuthFixture, kind: str = "oauth_user", **changes: Any
) -> MCPOAuthService:
    config = MCPConfigurationService(tmp_path)
    config.save(json.dumps(provider.configuration(kind, **changes)), config.read()["revision"])
    return MCPOAuthService(
        config, credentials=ChainedCredentialStore(EnvCredentialStore(), InMemoryCredentialStore())
    )


async def authorize(service: MCPOAuthService, provider: OAuthFixture) -> dict[str, str]:
    revision = service.configuration.read()["revision"]
    start = await service.start("fixture", revision, CALLBACK)
    params = await provider.consent(start)
    assert await service.callback(params, CALLBACK) == {"state": "authorized"}
    return params


async def test_application_acquisition_concurrency_expiry_and_restart(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider, "oauth_client_credentials")
    entry = await service.entry("fixture")
    values = await asyncio.gather(*(service.token(entry) for _ in range(8)))
    assert len(set(values)) == 1 and len(provider.forms) == 1
    record = await entry.storage.read_record()
    record.expires_at = time.time() - 1
    await entry.storage.write_record(record)
    assert await service.token(entry) != values[0]
    assert provider.forms[-1]["grant_type"] == "client_credentials"
    await service.close()
    restarted = MCPOAuthService(service.configuration, credentials=service.credentials)
    assert await restarted.token(await restarted.entry("fixture")) == "fixture-access-2"
    assert len(provider.forms) == 2
    await restarted.close()


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_application_methods_and_oidc_fallback(
    tmp_path: Path, provider: OAuthFixture, method: str
) -> None:
    provider.authorization_metadata_status = 404
    provider.metadata_changes = {"authorization_endpoint": None}
    service = service_at(tmp_path, provider, "oauth_client_credentials", token_endpoint_auth_method=method)
    assert await service.token(await service.entry("fixture")) == "fixture-access-1"
    await service.close()


async def test_application_failed_concurrent_acquisition_is_bounded(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    provider.fail_token = True
    service = service_at(tmp_path, provider, "oauth_client_credentials")
    entry = await service.entry("fixture")
    results = await asyncio.gather(*(service.token(entry) for _ in range(6)), return_exceptions=True)
    assert all(isinstance(result, RuntimeFailure) for result in results)
    assert len(provider.forms) == 1
    await service.close()


async def test_user_login_refresh_rotation_disconnect_and_no_secret_projection(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    assert (await service.status("fixture"))["state"] == "not_authorized"
    assert provider.forms == []
    callback = await authorize(service, provider)
    entry = await service.entry("fixture")
    record = await entry.storage.read_record()
    record.expires_at = time.time() - 1
    await entry.storage.write_record(record)
    tokens = await asyncio.gather(*(service.token(entry) for _ in range(5)))
    assert len(set(tokens)) == 1 and len(provider.forms) == 2
    assert provider.forms[-1]["refresh_token"] == "fixture-refresh-1"
    assert (await entry.storage.read_record()).tokens.refresh_token == "fixture-refresh-2"
    with pytest.raises(RuntimeFailure):
        await service.callback(callback, CALLBACK)
    projection = json.dumps(await service.status("fixture"))
    assert "fixture-access" not in projection and "fixture-refresh" not in projection
    assert (await service.disconnect("fixture", entry.revision))["provider_revocation"] == "completed"
    assert set(provider.revoked) == {"fixture-access-2", "fixture-refresh-2"}
    assert (await entry.storage.read_record()).tokens is None
    with pytest.raises(RuntimeFailure, match="invalidated"):
        await service.token(entry)
    await service.close()


async def test_oauth_through_real_mcp_discovery_tool_call_and_explicit_rejection(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    await authorize(service, provider)
    extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), oauth=service)
    async with AsyncOpenAI(api_key="fixture") as sdk:
        try:
            await extensions.load(OpenAIChatCompletionClient(model="fixture", async_client=sdk))
            assert extensions.mcp_connections["fixture"].connection_state == "connected"
            result = await extensions.tools[0].call_tool("echo", value="hello")
            assert any("oauth marker: hello" in (item.text or "") for item in result)
            assert provider.tool_calls == 1
            provider.deny_tools = True
            before = len(provider.forms)
            with pytest.raises(Exception):
                await extensions.tools[0].call_tool("echo", value="do-not-replay")
            assert provider.tool_calls == 1 and len(provider.forms) == before
            assert (await service.status("fixture"))["state"] == "reauthorization_required"
            assert extensions.mcp_connections["fixture"].connection_state == "authentication_failed"
        finally:
            await extensions.close()
    await service.close()


@pytest.mark.parametrize(
    ("kind", "method"),
    [
        ("oauth_user", "none"),
        ("oauth_client_credentials", "client_secret_basic"),
        ("oauth_client_credentials", "client_secret_post"),
    ],
)
async def test_oauth_credentials_stay_out_of_model_projections_errors_and_logs(
    tmp_path: Path,
    provider: OAuthFixture,
    kind: str,
    method: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changes = {"token_endpoint_auth_method": method} if kind == "oauth_client_credentials" else {}
    service = service_at(tmp_path, provider, kind, **changes)
    entry = await service.entry("fixture")
    app = FastAPI()
    configure_api(app, lambda: None, AppSettings(http_port=8080))
    app.dependency_overrides[current_runtime] = lambda: SimpleNamespace(mcp_auth=service)
    params: dict[str, str] = {}
    if kind == "oauth_user":
        started = await service.start("fixture", entry.revision, CALLBACK)
        params = await provider.consent(started)
    caplog.clear()
    requests: list[dict[str, Any]] = []
    projections: list[str] = []

    def model(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            name = body["tools"][0]["function"]["name"]
            return completion(calls=[tool_call(name, {"value": "safe-input"})])
        results = [message["content"] for message in body["messages"] if message["role"] == "tool"]
        assert any("oauth marker: safe-input" in text for text in results)
        return completion(content="OAuth tool completed.")

    extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), oauth=service)
    try:
        with caplog.at_level(logging.DEBUG), development_logging():
            if params:
                # Call the inbound ASGI transport directly: HTTPX caller logs are not Host logs.
                async with httpx.ASGITransport(app=DevelopmentRequestLogging(app)) as transport:
                    response = await transport.handle_async_request(
                        httpx.Request("GET", CALLBACK, params=params)
                    )
                    await response.aread()
                    assert response.status_code == 200
                    projections.append(response.text)
            else:
                await service.token(entry)
            record = await entry.storage.read_record()
            record.expires_at = time.time() - 1
            await entry.storage.write_record(record)
            async with AsyncOpenAI(
                api_key="fixture-model",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)),
            ) as sdk:
                client = OpenAIChatCompletionClient(model="fixture", async_client=sdk)
                await extensions.load(client)
                agent = Agent(client, tools=extensions.tools)
                assert (await agent.run("Echo safe-input.")).text == "OAuth tool completed."
            assert provider.tool_calls == 1 and len(requests) == 2
            assert len(provider.forms) == 2
            assert provider.forms[-1]["grant_type"] == (
                "refresh_token" if kind == "oauth_user" else "client_credentials"
            )
            projections.append(json.dumps(await service.status("fixture")))
            projections.append(json.dumps(service.configuration.read()))
            record = await entry.storage.read_record()
            record.expires_at = time.time() - 1
            await entry.storage.write_record(record)
            provider.fail_token = True
            with pytest.raises(RuntimeFailure) as failure:
                await service.token(entry)
            projections.append(str(failure.value))
            projections.append(json.dumps(await service.status("fixture")))
    finally:
        await extensions.close()
        await service.close()
    assert caplog.records
    captured = capsys.readouterr()
    if params:
        assert "http.completed" in captured.out
    surfaces = json.dumps(requests) + "".join(projections) + caplog.text + captured.out + captured.err
    secrets = [
        "fixture-app-secret",
        base64.b64encode(b"fixture-client:fixture-app-secret").decode(),
        "fixture-private",
        *provider.valid_tokens,
        *(f"fixture-refresh-{index}" for index in (1, 2)),
        *(params[key] for key in ("code", "state") if key in params),
        *(form["code_verifier"] for form in provider.forms if "code_verifier" in form),
    ]
    assert all(secret not in surfaces for secret in secrets)


async def test_user_authorization_change_projects_connection_restart(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    await authorize(service, provider)
    extensions = AgentExtensions(AgentSettings(workspace_path=tmp_path), oauth=service)
    async with AsyncOpenAI(api_key="fixture") as sdk:
        try:
            await extensions.load(OpenAIChatCompletionClient(model="fixture", async_client=sdk))
            await service.start("fixture", service.configuration.read()["revision"], CALLBACK)
            assert extensions.mcp_connections["fixture"].connection_state == "restart_required"
            before = provider.tool_calls
            with pytest.raises(Exception):
                await extensions.tools[0].call_tool("echo", value="old-authorization")
            assert provider.tool_calls == before
        finally:
            await extensions.close()
    await service.close()


@pytest.mark.parametrize("kind", ["bearer", "api_key", "stdio", "oauth_client_credentials"])
async def test_write_only_credential_api_uses_configured_scope_and_revision(
    tmp_path: Path, provider: OAuthFixture, kind: str
) -> None:
    service = service_at(tmp_path, provider)
    server = provider.configuration("oauth_client_credentials")["mcpServers"]["fixture"]
    reference = "keyring:fixture-secret"
    if kind == "stdio":
        server.pop("url")
        server.pop("auth")
        server.update(command="python", args=["-V"], env_credentials={"TOKEN": reference})
    elif kind == "oauth_client_credentials":
        server["auth"]["client_secret_ref"] = reference
    else:
        server["auth"] = {"type": kind, "credential_ref": reference}
        if kind == "api_key":
            server["auth"]["header"] = "X-API-Key"
    view = service.configuration.save(
        json.dumps({"mcpServers": {"fixture": server}}), service.configuration.read()["revision"]
    )
    app = FastAPI()
    configure_api(app, lambda: None, AppSettings())
    app.dependency_overrides[current_runtime] = lambda: SimpleNamespace(mcp_auth=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        path = "/api/v1/agent/mcp/fixture/credentials"
        body = {"revision": view["revision"], "reference": reference, "value": "fixture-new-secret"}
        response = await client.put(path, json=body)
        assert response.status_code == 200 and response.json() == {"saved": True, "restart_required": True}
        assert response.headers["cache-control"] == "no-store"
        assert "fixture-new-secret" not in response.text
        assert "fixture-new-secret" not in json.dumps(service.configuration.read())
        from shovel.runtime.agent_extensions import MCPSettings

        settings = MCPSettings.model_validate_json(view["content"]).mcpServers["fixture"]
        credentials = MCPCredentials(
            tmp_path, settings.credential_id, settings.resolved_credential_binding(), service.credentials
        )
        assert await credentials.get(reference) == "fixture-new-secret"
        assert (await client.put(path, json={**body, "revision": "0" * 64})).status_code == 409
        for invalid in ["keyring:other-service", "env:MCP_APP_SECRET", "keyring:oauth-state"]:
            rejected = await client.put(path, json={**body, "reference": invalid})
            assert rejected.status_code == 422 and "fixture-new-secret" not in rejected.text
        malformed = await client.put(path, json={**body, "unexpected": "fixture-new-secret"})
        assert malformed.status_code == 422 and "fixture-new-secret" not in malformed.text
        denied = await client.put(path, json=body, headers={"Origin": "https://untrusted.example"})
        assert denied.status_code == 403
    await service.close()


async def test_credential_rotation_fences_cached_http_and_preserves_no_secret_readback(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider, "oauth_client_credentials")
    entry = await service.entry("fixture")
    await service.token(entry)
    status = MCPHTTPStatus()
    async with await service.client("fixture", entry.server, status):
        config = provider.configuration("oauth_client_credentials", client_secret_ref="keyring:app")
        await service.save_configuration(json.dumps(config), entry.revision)
        revision = service.configuration.read()["revision"]
        fresh = await service.entry("fixture")
        await service.save_credential("fixture", revision, "keyring:app", "fixture-app-secret")
        assert not fresh.active and status.connection_state == "restart_required"
        assert (await fresh.storage.read_record()).tokens is None
    static_status = MCPHTTPStatus()
    static_status.is_current = lambda: False
    async with authenticated_http_client(static_status) as client:
        with pytest.raises(RuntimeFailure, match="restart"):
            await client.get(BASE + "/mcp")
    await service.close()


@pytest.mark.parametrize("change", ["issuer", "state", "redirect", "expired", "denied", "cancelled"])
async def test_callback_validation(tmp_path: Path, provider: OAuthFixture, change: str) -> None:
    service = service_at(tmp_path, provider)
    revision = service.configuration.read()["revision"]
    started = await service.start("fixture", revision, CALLBACK)
    params = await provider.consent(started)
    target = CALLBACK
    if change == "issuer":
        params["iss"] = "https://wrong.example"
    elif change == "state":
        params["state"] = "wrong-state"
    elif change == "redirect":
        target = CALLBACK.replace(":8080", ":9999")
    elif change == "expired":
        (await service.entry("fixture")).attempt.expires_at = 0
    elif change == "denied":
        params["error"] = "access_denied"
    elif change == "cancelled":
        await service.cancel("fixture", revision, started["authorization_id"])
    with pytest.raises(RuntimeFailure):
        await service.callback(params, target)
    assert provider.forms == []
    await service.close()


async def test_refresh_failure_requires_reauthorization_without_retry(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    await authorize(service, provider)
    entry = await service.entry("fixture")
    record = await entry.storage.read_record()
    record.expires_at = 0
    await entry.storage.write_record(record)
    provider.fail_token = True
    with pytest.raises(RuntimeFailure, match="invalid_grant") as failure:
        await service.token(entry)
    assert "fixture-private" not in str(failure.value)
    assert len(provider.forms) == 2
    assert (await service.status("fixture"))["state"] == "reauthorization_required"
    with pytest.raises(RuntimeFailure, match="required"):
        await service.token(entry)
    assert len(provider.forms) == 2
    await service.close()


@pytest.mark.parametrize("action", ["cancel", "disconnect", "configuration", "close"])
async def test_inflight_code_exchange_cannot_restore_invalidated_authorization(
    tmp_path: Path, provider: OAuthFixture, action: str
) -> None:
    service = service_at(tmp_path, provider)
    revision = service.configuration.read()["revision"]
    start = await service.start("fixture", revision, CALLBACK)
    params = await provider.consent(start)
    entry = await service.entry("fixture")
    provider.token_release.clear()
    callback = asyncio.create_task(service.callback(params, CALLBACK))
    await asyncio.wait_for(provider.token_started.wait(), 2)
    if action == "cancel":
        control = asyncio.create_task(service.cancel("fixture", revision, start["authorization_id"]))
    elif action == "disconnect":
        control = asyncio.create_task(service.disconnect("fixture", revision))
    elif action == "configuration":
        replacement = provider.configuration()
        replacement["mcpServers"]["fixture"]["disabled"] = True
        control = asyncio.create_task(service.save_configuration(json.dumps(replacement), revision))
    else:
        control = asyncio.create_task(service.close())
    for _ in range(100):
        if not entry.active:
            break
        await asyncio.sleep(0.01)
    assert not entry.active
    provider.token_release.set()
    with pytest.raises(RuntimeFailure):
        await callback
    await control
    assert (await entry.storage.read_record()).tokens is None
    await service.close()


@pytest.mark.parametrize(
    "metadata",
    [
        {"issuer": "https://wrong.example"},
        {"token_endpoint": "http://169.254.169.254/token"},
        {"token_endpoint": "https://other.example/token"},
        {"code_challenge_methods_supported": ["plain"]},
        {"scopes_supported": ["different"]},
    ],
)
async def test_discovery_rejects_untrusted_or_incompatible_metadata(
    tmp_path: Path, provider: OAuthFixture, metadata: dict[str, Any]
) -> None:
    provider.metadata_changes = metadata
    service = service_at(tmp_path, provider)
    with pytest.raises(RuntimeFailure):
        await service.start("fixture", service.configuration.read()["revision"], CALLBACK)
    assert provider.forms == []
    await service.close()


async def test_revocation_failure_still_clears_local_authorization(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    await authorize(service, provider)
    entry = await service.entry("fixture")
    provider.revoke_status = 500
    with pytest.raises(RuntimeFailure, match="cleared"):
        await service.disconnect("fixture", entry.revision)
    assert (await entry.storage.read_record()).tokens is None
    await service.close()


async def test_cancelled_refresh_does_not_retry_a_rotated_token(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    await authorize(service, provider)
    entry = await service.entry("fixture")
    record = await entry.storage.read_record()
    record.expires_at = 0
    await entry.storage.write_record(record)
    provider.token_started.clear()
    provider.token_release.clear()
    task = asyncio.create_task(service.token(entry))
    await asyncio.wait_for(provider.token_started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider.token_release.set()
    assert (await service.status("fixture"))["state"] == "reauthorization_required"
    assert len(provider.forms) == 2
    await service.close()


async def test_configuration_save_never_resolves_oauth_credentials(
    tmp_path: Path, provider: OAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_at(tmp_path, provider)
    entry = await service.entry("fixture")

    async def unavailable() -> None:
        pytest.fail("Saving configuration must not access the credential store")

    monkeypatch.setattr(entry.storage, "read_record", unavailable)
    view = service.configuration.read()
    await service.save_configuration("{}", view["revision"])
    assert not entry.active
    await service.close()


@pytest.mark.parametrize("changes", [{"expires_in": 0}, {"scope": "other"}, {"access_token": "bad\r\nvalue"}])
async def test_invalid_token_responses_are_not_persisted(
    tmp_path: Path, provider: OAuthFixture, changes: dict[str, Any]
) -> None:
    service = service_at(tmp_path, provider, "oauth_client_credentials")
    provider.token_changes = changes
    entry = await service.entry("fixture")
    with pytest.raises(RuntimeFailure):
        await service.token(entry)
    assert (await entry.storage.read_record()).tokens is None
    await service.close()


async def test_authentication_rest_callback_csrf_revision_and_query_redaction(
    tmp_path: Path, provider: OAuthFixture
) -> None:
    service = service_at(tmp_path, provider)
    app = FastAPI()
    settings = AppSettings(http_host="127.0.0.1", http_port=8080)
    configure_api(app, lambda: None, settings)
    app.dependency_overrides[current_runtime] = lambda: SimpleNamespace(mcp_auth=service)
    observed_queries: list[bytes] = []

    async def captured(scope: Any, receive: Any, send: Any) -> None:
        await app(scope, receive, send)
        if scope.get("path", "").endswith("/oauth/callback"):
            observed_queries.append(scope["query_string"])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=captured), base_url="http://127.0.0.1:8080"
    ) as client:
        path = "/api/v1/agent/mcp/fixture/auth"
        revision = service.configuration.read()["revision"]
        assert (await client.get(path)).json()["state"] == "not_authorized"
        assert provider.forms == []
        assert (await client.post(path + "/start", json={"revision": "0" * 64})).status_code == 409
        denied = await client.post(
            path + "/start", json={"revision": revision}, headers={"sec-fetch-site": "cross-site"}
        )
        assert denied.status_code == 403
        start = (await client.post(path + "/start", json={"revision": revision})).json()
        params = await provider.consent(start)
        response = await client.get(CALLBACK, params=params, headers={"sec-fetch-site": "cross-site"})
        assert response.status_code == 200 and response.json()["state"] == "authorized"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert observed_queries == [b""]
        assert "fixture-access" not in response.text and "fixture-refresh" not in response.text
        duplicate = await client.get(CALLBACK + "?state=a&state=b")
        assert duplicate.status_code == 400
        assert duplicate.headers["cache-control"] == "no-store"
        replay = await client.get(CALLBACK, params=params)
        assert replay.status_code == 400
        wrong_port = await client.get(CALLBACK.replace(":8080", ":8081"), params=params)
        assert wrong_port.status_code == 400
        wrong_host = await client.get(CALLBACK, params=params, headers={"host": "untrusted.example"})
        assert wrong_host.status_code == 403
        assert wrong_host.headers["cache-control"] == "no-store"
        assert observed_queries == [b""] * 5
        result = await client.post(path + "/disconnect", json={"revision": revision})
        assert result.status_code == 200 and result.json()["local_cleared"]
        second = (await client.post(path + "/start", json={"revision": revision})).json()
        cancelled = await client.post(
            path + "/cancel", json={"revision": revision, "authorization_id": second["authorization_id"]}
        )
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "not_authorized"
    await service.close()


async def test_dns_private_addresses_are_denied_and_public_post_body_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = OAuthTransport("https://mcp.example.test/mcp")
    observed: list[httpx.Request] = []

    async def response(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert await request.aread() == b"grant_type=client_credentials"
        return httpx.Response(200, json={})

    transport.inner = httpx.MockTransport(response)
    ip = "169.254.169.254"

    async def dns(*args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeFailure, match="non-public"):
            await client.post("https://issuer.example.test/token", data={"grant_type": "client_credentials"})
        assert observed == []
        ip = "8.8.8.8"
        await client.post("https://issuer.example.test/token", data={"grant_type": "client_credentials"})
    assert observed[0].url.host == ip
    assert observed[0].headers["host"] == "issuer.example.test"
    assert observed[0].extensions["sni_hostname"] == "issuer.example.test"
