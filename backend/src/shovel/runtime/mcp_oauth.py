from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlencode

import httpx
from anyio import CancelScope
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)
from mcp.shared.auth import OAuthToken, ProtectedResourceMetadata
from mcp.shared.auth_utils import resource_url_from_server_url
from pydantic import AnyHttpUrl, BaseModel, ValidationError

from shovel.runtime.agent_extensions import MCPServerSettings, MCPSettings, _environment
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.mcp_auth import (
    APIKeyAuth,
    BearerAuth,
    MCPCredentials,
    MCPHTTPStatus,
    MCPOAuthStorage,
    OAuthApplicationAuth,
    OAuthRecord,
    OAuthUserAuth,
    authenticated_url,
    secret_header,
)
from shovel.runtime.mcp_config import MCPConfigurationService
from shovel.runtime.mcp_oauth_http import OAuthHTTP, origin
from shovel.services.credentials import CredentialStore

OAuthSettings = OAuthUserAuth | OAuthApplicationAuth


class OAuthEndpoints(BaseModel):
    issuer: str
    token_endpoint: AnyHttpUrl
    authorization_endpoint: AnyHttpUrl | None = None
    revocation_endpoint: AnyHttpUrl | None = None
    scopes_supported: list[str] | None = None
    grant_types_supported: list[str] | None = None
    token_endpoint_auth_methods_supported: list[str] | None = None
    code_challenge_methods_supported: list[str] | None = None
    response_types_supported: list[str] = ["code"]


@dataclass(repr=False)
class Authorization:
    id: str
    state: str
    verifier: str
    redirect_uri: str
    expires_at: float
    consumed: bool = False


@dataclass(repr=False)
class OAuthEntry:
    name: str
    revision: str
    server: MCPServerSettings
    auth: OAuthSettings
    url: str
    credentials: MCPCredentials
    storage: MCPOAuthStorage
    http: OAuthHTTP
    active: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    metadata: OAuthEndpoints | None = None
    issuer: str = ""
    scopes: list[str] = field(default_factory=list)
    attempt: Authorization | None = None
    retry_at: float = 0
    error_message: str = ""
    error_status: int = 503

    @property
    def resource(self) -> str:
        return resource_url_from_server_url(self.url)


class OAuthBearer(httpx.Auth):
    def __init__(self, service: MCPOAuthService, entry: OAuthEntry) -> None:
        self.service = service
        self.entry = entry

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if origin(request.url) != origin(self.entry.url):
            raise RuntimeFailure("OAuth MCP request left its bound origin.", status=403)
        token = await self.service.token(self.entry)
        await self.service.check(self.entry)
        request.headers["Authorization"] = secret_header(token, bearer=True)
        response = yield request
        if response.status_code == 401:
            await self.service.rejected(self.entry, token)
        # A dispatched tool call is never automatically replayed, including after 401/403.


class MCPOAuthService:
    def __init__(
        self, configuration: MCPConfigurationService, *, credentials: CredentialStore | None = None
    ) -> None:
        self.configuration = configuration
        self.credentials = credentials
        self._entries: dict[str, OAuthEntry] = {}
        self._control = asyncio.Lock()
        self._closed = False
        self._changing = False
        self.credential_generations: dict[str, int] = {}

    async def _configuration(self, name: str, revision: str | None = None) -> tuple[str, MCPServerSettings]:
        if self._closed or self._changing:
            raise RuntimeFailure("MCP authentication is stopping or changing configuration.", status=503)
        view = await asyncio.to_thread(self.configuration.read)
        if revision is not None and revision != view["revision"]:
            raise RuntimeFailure("MCP configuration changed; reload before authorizing.", status=409)
        config = MCPSettings.model_validate_json(view["content"])
        server = config.mcpServers.get(name)
        if server is None or server.disabled:
            raise RuntimeFailure("MCP service is missing or disabled.", status=409)
        if not isinstance(server.auth, (OAuthUserAuth, OAuthApplicationAuth)):
            raise RuntimeFailure("MCP service does not use OAuth.", status=422)
        return view["revision"], server

    async def check(self, entry: OAuthEntry) -> None:
        if not entry.active:
            raise RuntimeFailure("MCP authorization was invalidated; reconnect explicitly.", status=409)
        await self._configuration(entry.name, entry.revision)
        if not entry.active:
            raise RuntimeFailure("MCP authorization was invalidated.", status=409)

    async def entry(self, name: str, revision: str | None = None) -> OAuthEntry:
        async with self._control:
            return await self._entry(name, revision)

    async def _entry(self, name: str, revision: str | None = None) -> OAuthEntry:
        current, server = await self._configuration(name, revision)
        existing = self._entries.get(name)
        if existing is not None and existing.active and existing.revision == current:
            return existing
        if existing is not None:
            existing.active = False
            existing.attempt = None
        assert server.credential_id is not None
        assert isinstance(server.auth, (OAuthUserAuth, OAuthApplicationAuth))
        url = _environment(server.url)
        authenticated_url(url)
        binding = server.resolved_credential_binding()
        credentials = MCPCredentials(
            self.configuration.path.parent, server.credential_id, binding, self.credentials
        )
        result = OAuthEntry(
            name,
            current,
            server,
            server.auth,
            url,
            credentials,
            MCPOAuthStorage(credentials),
            OAuthHTTP(url),
        )

        async def guard() -> None:
            await self.check(result)

        result.http.guard = guard
        self._entries[name] = result
        return result

    async def client(self, name: str, server: MCPServerSettings, status: MCPHTTPStatus) -> httpx.AsyncClient:
        entry = await self.entry(name)
        if server != entry.server:
            raise RuntimeFailure("MCP configuration changed; restart the Agent.", status=409)
        client = entry.http.client(auth=OAuthBearer(self, entry))
        current = status.is_current
        status.is_current = lambda: entry.active and current()
        client.event_hooks["response"].append(status.observe)
        return client

    async def discover(self, entry: OAuthEntry) -> None:
        if entry.metadata is not None:
            return
        await self.check(entry)
        challenge_status, _, challenge_headers = await entry.http.request("GET", entry.url)
        challenge = httpx.Response(challenge_status, headers=challenge_headers)
        resource_hint = extract_resource_metadata_from_www_auth(challenge)
        protected: ProtectedResourceMetadata | None = None
        advertised: list[str] = []
        for url in build_protected_resource_metadata_discovery_urls(resource_hint, entry.url):
            if origin(url) != origin(entry.url):
                raise RuntimeFailure("MCP resource metadata must stay on the MCP origin.", status=422)
            status, payload, _ = await entry.http.request("GET", url)
            if status in {404, 405}:
                continue
            if status != 200:
                raise RuntimeFailure(f"MCP resource discovery failed (HTTP {status}).", status=502)
            try:
                protected = ProtectedResourceMetadata.model_validate(payload)
                advertised = [str(value) for value in payload["authorization_servers"]]
            except ValidationError, KeyError, TypeError:
                raise RuntimeFailure("Invalid MCP protected resource metadata.", status=502) from None
            if str(protected.resource).rstrip("/") != entry.resource.rstrip("/"):
                raise RuntimeFailure(
                    "MCP metadata resource does not match the configured service.", status=422
                )
            break
        if protected is None:
            raise RuntimeFailure("MCP protected resource metadata is unavailable.", status=502)
        issuer = entry.auth.issuer
        if issuer is None:
            if len(advertised) != 1:
                raise RuntimeFailure("Configure an issuer to select an MCP authorization server.", status=422)
            issuer = advertised[0]
        if issuer not in advertised:
            raise RuntimeFailure("Configured OAuth issuer is not advertised by this MCP service.", status=422)
        authenticated_url(issuer)
        metadata: OAuthEndpoints | None = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(issuer, entry.url):
            status, payload, _ = await entry.http.request("GET", url)
            if status in {404, 405}:
                continue
            if status != 200:
                raise RuntimeFailure(f"OAuth discovery failed (HTTP {status}).", status=502)
            if payload.get("issuer") != issuer:
                raise RuntimeFailure("OAuth metadata issuer mismatch.", status=422)
            try:
                metadata = OAuthEndpoints.model_validate(payload)
            except ValidationError:
                raise RuntimeFailure("Invalid OAuth server metadata.", status=502) from None
            for endpoint in (
                metadata.authorization_endpoint,
                metadata.token_endpoint,
                metadata.revocation_endpoint,
            ):
                if endpoint is not None:
                    authenticated_url(str(endpoint))
                    if origin(str(endpoint)) != origin(issuer):
                        raise RuntimeFailure("OAuth endpoints must stay on the issuer origin.", status=422)
            break
        if metadata is None:
            raise RuntimeFailure("OAuth server metadata is unavailable.", status=502)
        scopes = entry.auth.scopes or (extract_scope_from_www_auth(challenge) or "").split()
        if not scopes:
            scopes = protected.scopes_supported or []
        if metadata.scopes_supported is not None and not set(scopes) <= set(metadata.scopes_supported):
            raise RuntimeFailure("Requested OAuth scopes are not supported.", status=422)
        if isinstance(entry.auth, OAuthUserAuth):
            if metadata.authorization_endpoint is None:
                raise RuntimeFailure("OAuth server has no authorization endpoint.", status=422)
            if "S256" not in (metadata.code_challenge_methods_supported or []):
                raise RuntimeFailure("OAuth server must advertise PKCE S256.", status=422)
            if "code" not in metadata.response_types_supported:
                raise RuntimeFailure("OAuth server does not support authorization codes.", status=422)
        else:
            if (
                metadata.grant_types_supported is not None
                and "client_credentials" not in metadata.grant_types_supported
            ):
                raise RuntimeFailure("OAuth server does not support client credentials.", status=422)
            methods = metadata.token_endpoint_auth_methods_supported or ["client_secret_basic"]
            if entry.auth.token_endpoint_auth_method not in methods:
                raise RuntimeFailure("OAuth client authentication method is unsupported.", status=422)
        await self.check(entry)
        entry.issuer, entry.metadata, entry.scopes = issuer, metadata, scopes

    async def _save(self, entry: OAuthEntry, record: OAuthRecord) -> None:
        await self.check(entry)
        # A keyring thread cannot be cancelled. Drain it before disconnect can clear the same record.
        task = asyncio.create_task(entry.storage.write_record(record))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                await task
            raise

    async def _client_auth(self, entry: OAuthEntry, data: dict[str, str]) -> httpx.Auth | None:
        if isinstance(entry.auth, OAuthApplicationAuth):
            secret = await entry.credentials.get(entry.auth.client_secret_ref)
            method = entry.auth.token_endpoint_auth_method
            if method == "client_secret_basic":
                return httpx.BasicAuth(quote_plus(entry.auth.client_id), quote_plus(secret))
            data.update(client_id=entry.auth.client_id, client_secret=secret)
        else:
            data["client_id"] = entry.auth.client_id
        return None

    async def _exchange(
        self,
        entry: OAuthEntry,
        data: dict[str, str],
        *,
        previous: OAuthRecord | None = None,
        authorization_id: str | None = None,
    ) -> OAuthRecord:
        assert entry.metadata is not None
        data["resource"] = entry.resource
        auth = await self._client_auth(entry, data)
        await self.check(entry)
        status, payload, _ = await entry.http.request(
            "POST", str(entry.metadata.token_endpoint), data=data, auth=auth
        )
        await self.check(entry)
        if status != 200:
            code = payload.get("error")
            reason = (
                code
                if code in {"invalid_grant", "invalid_client", "invalid_scope", "access_denied"}
                else "request_failed"
            )
            raise RuntimeFailure(f"OAuth token request failed: {reason} (HTTP {status}).", status=502)
        try:
            tokens = OAuthToken.model_validate(payload)
        except ValidationError:
            raise RuntimeFailure("OAuth server returned invalid tokens.", status=502) from None
        secret_header(tokens.access_token, bearer=True)
        if tokens.expires_in is None or tokens.expires_in <= 0:
            raise RuntimeFailure("OAuth token must include a positive expires_in.", status=502)
        if tokens.scope is not None and set(tokens.scope.split()) != set(entry.scopes):
            raise RuntimeFailure("OAuth granted scopes differ from the requested scopes.", status=403)
        if previous is not None and not tokens.refresh_token and previous.tokens is not None:
            tokens.refresh_token = previous.tokens.refresh_token
        record = OAuthRecord(
            tokens=tokens,
            expires_at=time.time() + tokens.expires_in,
            issuer=entry.issuer,
            resource=entry.resource,
            client_id=entry.auth.client_id,
            authorization_id=authorization_id or (previous.authorization_id if previous else None),
            revocation_endpoint=str(entry.metadata.revocation_endpoint)
            if entry.metadata.revocation_endpoint
            else None,
        )
        await self._save(entry, record)
        return record

    def _bound(self, entry: OAuthEntry, record: OAuthRecord) -> bool:
        return (
            record.issuer == entry.issuer
            and record.resource == entry.resource
            and record.client_id == entry.auth.client_id
            and (isinstance(entry.auth, OAuthApplicationAuth) or record.authorization_id is not None)
        )

    async def token(self, entry: OAuthEntry) -> str:
        async with entry.lock:
            await self.check(entry)
            await self.discover(entry)
            record = await entry.storage.read_record()
            if self._bound(entry, record) and record.tokens is not None and record.expires_at is not None:
                margin = min(30, max(0, (record.tokens.expires_in or 0) / 10))
                if time.time() + margin < record.expires_at:
                    return record.tokens.access_token
            if isinstance(entry.auth, OAuthApplicationAuth):
                if time.monotonic() < entry.retry_at:
                    raise RuntimeFailure(entry.error_message, status=entry.error_status)
                data = {"grant_type": "client_credentials"}
                if entry.scopes:
                    data["scope"] = " ".join(entry.scopes)
                try:
                    record = await self._exchange(entry, data)
                except RuntimeFailure as exc:
                    entry.retry_at = time.monotonic() + 2
                    entry.error_message, entry.error_status = exc.message, exc.status
                    raise
            elif self._bound(entry, record) and record.tokens is not None and record.tokens.refresh_token:
                try:
                    record = await self._exchange(
                        entry,
                        {"grant_type": "refresh_token", "refresh_token": record.tokens.refresh_token},
                        previous=record,
                    )
                except RuntimeFailure, asyncio.CancelledError:
                    if entry.active:
                        with CancelScope(shield=True):
                            await self._save(entry, OAuthRecord(reauthorization_required=True))
                    raise
            else:
                raise RuntimeFailure("MCP user authorization is required; start it explicitly.", status=401)
            assert record.tokens is not None
            return record.tokens.access_token

    async def rejected(self, entry: OAuthEntry, token: str) -> None:
        async with entry.lock:
            await self.check(entry)
            record = await entry.storage.read_record()
            if record.tokens is not None and secrets.compare_digest(record.tokens.access_token, token):
                await self._save(
                    entry, OAuthRecord(reauthorization_required=isinstance(entry.auth, OAuthUserAuth))
                )

    async def _invalidate(self, entry: OAuthEntry, *, clear: bool) -> OAuthRecord:
        entry.active = False
        entry.attempt = None
        async with entry.lock:
            if not clear:
                return OAuthRecord()
            record = await entry.storage.read_record()
            task = asyncio.create_task(entry.storage.clear())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await task
                raise
            return record

    async def start(self, name: str, revision: str, redirect_uri: str) -> dict[str, Any]:
        authenticated_url(redirect_uri)
        redirect = httpx.URL(redirect_uri)
        if redirect.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeFailure("OAuth callback must use the loopback Host.", status=422)
        async with self._control:
            entry = await self._entry(name, revision)
            if not isinstance(entry.auth, OAuthUserAuth):
                raise RuntimeFailure("Application OAuth does not require browser authorization.", status=422)
            if (
                sum(
                    item.attempt is not None and item.attempt.expires_at > time.time()
                    for item in self._entries.values()
                )
                >= 32
            ):
                raise RuntimeFailure("Too many pending MCP authorizations.", status=429)
            await self._invalidate(entry, clear=True)
            entry = await self._entry(name, revision)
        async with entry.lock:
            await self.discover(entry)
            pkce = PKCEParameters.generate()
            attempt = Authorization(
                secrets.token_urlsafe(24),
                secrets.token_urlsafe(32),
                pkce.code_verifier,
                redirect_uri,
                time.time() + 300,
            )
            entry.attempt = attempt
            assert entry.metadata is not None
            assert entry.metadata.authorization_endpoint is not None
            query = {
                "response_type": "code",
                "client_id": entry.auth.client_id,
                "redirect_uri": redirect_uri,
                "state": attempt.state,
                "code_challenge": pkce.code_challenge,
                "code_challenge_method": "S256",
                "resource": entry.resource,
            }
            if entry.scopes:
                query["scope"] = " ".join(entry.scopes)
            return {
                "authorization_id": attempt.id,
                "authorization_url": str(entry.metadata.authorization_endpoint) + "?" + urlencode(query),
                "expires_at": attempt.expires_at,
                "issuer": entry.issuer,
                "scopes": entry.scopes,
            }

    async def callback(self, params: dict[str, str], redirect_uri: str) -> dict[str, str]:
        state = params.get("state", "")
        if not state or not state.isascii() or len(state) > 256:
            raise RuntimeFailure("OAuth callback state is invalid.", status=400)
        entry = next(
            (
                item
                for item in self._entries.values()
                if item.active
                and item.attempt is not None
                and secrets.compare_digest(item.attempt.state, state)
            ),
            None,
        )
        if entry is None or entry.attempt is None:
            raise RuntimeFailure("OAuth callback is invalid, cancelled or expired.", status=400)
        attempt = entry.attempt
        if attempt.consumed or time.time() >= attempt.expires_at:
            raise RuntimeFailure("OAuth callback has expired or was already consumed.", status=400)
        if redirect_uri != attempt.redirect_uri or params.get("iss") != entry.issuer:
            raise RuntimeFailure("OAuth callback redirect or issuer mismatch.", status=400)
        attempt.consumed = True
        async with entry.lock:
            try:
                await self.check(entry)
                if "error" in params:
                    raise RuntimeFailure("OAuth authorization was denied or cancelled.", status=400)
                code = params.get("code")
                if not code or len(code) > 4096:
                    raise RuntimeFailure("OAuth callback has no valid authorization code.", status=400)
                await self._exchange(
                    entry,
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": attempt.redirect_uri,
                        "code_verifier": attempt.verifier,
                    },
                    authorization_id=attempt.id,
                )
            finally:
                entry.attempt = None
        return {"state": "authorized"}

    async def cancel(self, name: str, revision: str, authorization_id: str) -> dict[str, str]:
        async with self._control:
            entry = await self._entry(name, revision)
            attempt = entry.attempt
            if attempt is None or not secrets.compare_digest(attempt.id, authorization_id):
                raise RuntimeFailure("OAuth authorization no longer exists.", status=409)
            await self._invalidate(entry, clear=True)
        return {"state": "not_authorized"}

    async def disconnect(self, name: str, revision: str) -> dict[str, Any]:
        async with self._control:
            entry = await self._entry(name, revision)
            record = await self._invalidate(entry, clear=True)
        endpoint = record.revocation_endpoint
        revoked = "unsupported"
        if endpoint is not None and record.tokens is not None:
            authenticated_url(endpoint)
            if record.issuer is None or origin(endpoint) != origin(record.issuer):
                raise RuntimeFailure(
                    "Local authorization cleared; invalid stored revocation endpoint.", status=502
                )
            revoked = "completed"
            for token in dict.fromkeys(
                filter(None, [record.tokens.refresh_token, record.tokens.access_token])
            ):
                data = {"token": token}
                try:
                    auth = await self._client_auth(entry, data)
                    status, _, _ = await OAuthHTTP(entry.url).request("POST", endpoint, data=data, auth=auth)
                except RuntimeFailure:
                    raise RuntimeFailure(
                        "Local MCP authorization cleared; provider revocation failed.", status=502
                    ) from None
                if status != 200:
                    raise RuntimeFailure(
                        "Local MCP authorization cleared; provider revocation failed.", status=502
                    )
        return {"state": "not_authorized", "local_cleared": True, "provider_revocation": revoked}

    async def status(self, name: str, revision: str | None = None) -> dict[str, Any]:
        entry = await self.entry(name, revision)
        async with entry.lock:
            await self.check(entry)
            record = await entry.storage.read_record()
            if entry.attempt is not None and time.time() >= entry.attempt.expires_at:
                entry.attempt = None
            state = "not_authorized"
            if record.reauthorization_required:
                state = "reauthorization_required"
            elif record.tokens is not None and record.expires_at is not None:
                if record.expires_at > time.time() or record.tokens.refresh_token:
                    state = "authorized"
                elif isinstance(entry.auth, OAuthUserAuth):
                    state = "reauthorization_required"
            if entry.attempt is not None:
                state = "authorizing"
            return {
                "state": state,
                "auth_type": entry.auth.type,
                "revision": entry.revision,
                "expires_at": record.expires_at,
                "authorization_id": entry.attempt.id if entry.attempt else None,
            }

    async def save_credential(self, name: str, revision: str, reference: str, value: str) -> dict[str, bool]:
        async with self._control:
            if self._closed or self._changing:
                raise RuntimeFailure("MCP authentication is stopping or changing configuration.", status=503)
            view = await asyncio.to_thread(self.configuration.read)
            if view["revision"] != revision:
                raise RuntimeFailure(
                    "MCP configuration changed; reload before saving credentials.", status=409
                )
            server = MCPSettings.model_validate_json(view["content"]).mcpServers.get(name)
            if server is None or server.credential_id is None:
                raise RuntimeFailure("MCP service has no configured credential identity.", status=422)
            if (
                not reference.startswith("keyring:")
                or reference == "keyring:oauth-state"
                or reference not in server.credential_references()
            ):
                raise RuntimeFailure(
                    "Only configured keyring credential references can be written.", status=422
                )
            if not value or "\0" in value:
                raise RuntimeFailure("MCP credential is empty or contains a NUL character.", status=422)
            if isinstance(server.auth, (APIKeyAuth, BearerAuth)):
                try:
                    secret_header(value, bearer=isinstance(server.auth, BearerAuth))
                except RuntimeFailure as exc:
                    raise RuntimeFailure(exc.message, status=422) from None
            credentials = MCPCredentials(
                self.configuration.path.parent,
                server.credential_id,
                server.resolved_credential_binding(),
                self.credentials,
            )
            if isinstance(server.auth, OAuthApplicationAuth):
                entry = await self._entry(name, revision)
                await self._invalidate(entry, clear=True)
            self.credential_generations[name] = self.credential_generations.get(name, 0) + 1
            task = asyncio.create_task(credentials.write(reference, value))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await task
                raise
        return {"saved": True, "restart_required": True}

    async def save_configuration(self, content: str, revision: str) -> dict[str, Any]:
        self.configuration._validate(content)
        async with self._control:
            current = await asyncio.to_thread(self.configuration.read)
            if current["revision"] != revision:
                raise RuntimeFailure("MCP configuration changed; reload before saving.", status=409)
            self._changing = True
            try:
                for entry in self._entries.values():
                    await self._invalidate(entry, clear=False)
                return await asyncio.to_thread(self.configuration.save, content, revision)
            finally:
                self._changing = False

    async def close(self) -> None:
        async with self._control:
            self._closed = True
            for entry in self._entries.values():
                entry.active = False
                entry.attempt = None
        for entry in self._entries.values():
            async with entry.lock:
                pass
