from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from shovel.exceptions.resource import (
    CredentialBackendUnavailableError,
    CredentialNotFoundError,
    InvalidCredentialRefError,
)
from shovel.runtime.errors import RuntimeFailure
from shovel.services.credentials import (
    CredentialRef,
    CredentialScheme,
    CredentialStore,
    build_credential_store,
)

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "cookie",
    "proxy-authorization",
    "mcp-session-id",
    "mcp-protocol-version",
}


def credential_reference(value: str) -> str:
    try:
        return str(CredentialRef.parse(value))
    except InvalidCredentialRefError:
        raise ValueError("Expected an env: or keyring: credential reference.") from None


def header_name(value: str) -> str:
    if not _HEADER_NAME.fullmatch(value) or value.lower() in _RESERVED_HEADERS:
        raise ValueError("Invalid or reserved authentication header name.")
    return value


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @field_validator("issuer", check_fields=False)
    @classmethod
    def valid_issuer(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                authenticated_url(value)
            except RuntimeFailure as exc:
                raise ValueError(exc.message) from None
        return value

    @field_validator("scopes", check_fields=False)
    @classmethod
    def valid_scopes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not scope or not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope) for scope in values
        ):
            raise ValueError("OAuth scopes must be distinct, nonempty scope tokens.")
        return values


class NoAuth(AuthSettings):
    type: Literal["none"]


class APIKeyAuth(AuthSettings):
    type: Literal["api_key"]
    header: str
    credential_ref: str

    _header = field_validator("header")(header_name)
    _ref = field_validator("credential_ref")(credential_reference)


class BearerAuth(AuthSettings):
    type: Literal["bearer"]
    credential_ref: str

    _ref = field_validator("credential_ref")(credential_reference)


class OAuthUserAuth(AuthSettings):
    type: Literal["oauth_user"]
    client_id: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    issuer: str | None = None


class OAuthApplicationAuth(AuthSettings):
    type: Literal["oauth_client_credentials"]
    client_id: str = Field(min_length=1)
    client_secret_ref: str
    scopes: list[str] = Field(default_factory=list)
    issuer: str
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = Field(
        default="client_secret_basic"
    )

    _ref = field_validator("client_secret_ref")(credential_reference)


MCPAuth = Annotated[
    NoAuth | APIKeyAuth | BearerAuth | OAuthUserAuth | OAuthApplicationAuth,
    Field(discriminator="type"),
]


def authenticated_url(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise RuntimeFailure("Invalid authenticated MCP URL.", status=422) from None
    if (
        not parsed.host
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.host in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise RuntimeFailure(
            "Authenticated MCP requires HTTPS (HTTP only on loopback), without URL credentials, "
            "query or fragment.",
            status=422,
        )


def secret_header(value: str, *, bearer: bool = False) -> str:
    valid = bool(value.strip()) and all(32 <= ord(char) < 127 for char in value)
    if bearer:
        valid = valid and re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", value) is not None
    if not valid:
        raise RuntimeFailure("MCP credential is not a valid HTTP header value.", status=503)
    return f"Bearer {value}" if bearer else value


class MCPCredentials:
    """Resolve explicit references without exposing underlying backend errors."""

    def __init__(
        self,
        workspace: Path,
        service_id: UUID,
        binding: dict[str, object],
        store: CredentialStore | None = None,
    ) -> None:
        identity = json.dumps(
            [str(workspace.resolve()), str(service_id), binding], sort_keys=True, separators=(",", ":")
        )
        self.namespace = "mcp_" + hashlib.sha256(identity.encode()).hexdigest()
        self.store = store if store is not None else build_credential_store()

    def storage_ref(self, reference: str) -> CredentialRef:
        ref = CredentialRef.parse(reference)
        if ref.scheme is CredentialScheme.KEYRING:
            label = hashlib.sha256(ref.account.encode()).hexdigest()
            return CredentialRef(ref.scheme, f"{self.namespace}_{label}")
        return ref

    async def read(self, reference: str) -> str | None:
        try:
            secret = await asyncio.to_thread(self.store.get, self.storage_ref(reference))
        except CredentialNotFoundError:
            return None
        except CredentialBackendUnavailableError:
            raise RuntimeFailure("MCP credential store is unavailable.", status=503) from None
        value = secret.get_secret_value()
        if not value or "\0" in value:
            raise RuntimeFailure("MCP credential is empty or contains a NUL character.", status=503)
        return value

    async def get(self, reference: str) -> str:
        value = await self.read(reference)
        if value is None:
            raise RuntimeFailure(
                "MCP credential is missing.",
                status=503,
                hint="Provision the configured environment variable or MCP-scoped keyring credential.",
            )
        return value

    async def write(self, reference: str, value: str) -> None:
        ref = self.storage_ref(reference)
        if not ref.is_writable:
            raise RuntimeFailure("MCP environment credentials are read-only.", status=422)
        try:
            await asyncio.to_thread(self.store.set, ref, SecretStr(value))
        except CredentialBackendUnavailableError:
            raise RuntimeFailure("MCP credential store is unavailable.", status=503) from None


class OAuthRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tokens: OAuthToken | None = Field(default=None, repr=False)
    client_info: OAuthClientInformationFull | None = Field(default=None, repr=False)
    expires_at: float | None = None
    issuer: str | None = None
    resource: str | None = None
    client_id: str | None = None
    authorization_id: str | None = None
    reauthorization_required: bool = False
    revocation_endpoint: str | None = None


class MCPOAuthStorage:
    """SDK TokenStorage adapter; no login, refresh or network activity."""

    def __init__(self, credentials: MCPCredentials) -> None:
        self.credentials = credentials
        self._lock = asyncio.Lock()
        self._reference = "keyring:oauth-state"

    async def _read(self) -> OAuthRecord:
        value = await self.credentials.read(self._reference)
        if value is None:
            return OAuthRecord()
        try:
            return OAuthRecord.model_validate_json(value)
        except ValidationError:
            raise RuntimeFailure("Stored MCP OAuth state is invalid.", status=503) from None

    async def get_tokens(self) -> OAuthToken | None:
        async with self._lock:
            state = await self._read()
            if state.tokens is None:
                return None
            remaining = max(0, int(state.expires_at - time.time())) if state.expires_at is not None else 0
            return state.tokens.model_copy(
                update={"expires_in": remaining if state.tokens.expires_in is not None else None}
            )

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            state = await self._read()
            state.tokens = tokens
            state.expires_at = time.time() + tokens.expires_in if tokens.expires_in is not None else None
            await self.credentials.write(self._reference, state.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            return (await self._read()).client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            state = await self._read()
            state.client_info = client_info
            await self.credentials.write(self._reference, state.model_dump_json())

    async def clear(self) -> None:
        async with self._lock:
            # Replace secrets atomically; the shared keyring delete API tolerates backend errors.
            await self.credentials.write(self._reference, OAuthRecord().model_dump_json())

    async def read_record(self) -> OAuthRecord:
        async with self._lock:
            return await self._read()

    async def write_record(self, record: OAuthRecord) -> None:
        async with self._lock:
            await self.credentials.write(self._reference, record.model_dump_json())


class MCPHTTPStatus:
    def __init__(self) -> None:
        self.denied: int | None = None

    async def observe(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            self.denied = response.status_code


def authenticated_http_client(status: MCPHTTPStatus) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=300),
        follow_redirects=False,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        event_hooks={"response": [status.observe]},
    )
