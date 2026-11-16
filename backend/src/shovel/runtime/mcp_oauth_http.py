from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.mcp_auth import authenticated_url
from shovel.services.web import public_address


def origin(value: str | httpx.URL) -> tuple[str, str, int | None]:
    url = httpx.URL(value)
    return url.scheme, url.host, url.port


class OAuthTransport(httpx.AsyncBaseTransport):
    """Public DNS-pinned requests, with only the configured loopback origin exempted."""

    def __init__(self, server_url: str, guard: Callable[[], Awaitable[None]] | None = None) -> None:
        server = httpx.URL(server_url)
        self.loopback = origin(server) if server.host in {"localhost", "127.0.0.1", "::1"} else None
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.guard = guard

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.guard is not None:
            await self.guard()
        authenticated_url(str(request.url))
        if self.loopback is not None and origin(request.url) == self.loopback:
            if request.url.host == "localhost":
                target = request.url.copy_with(host="127.0.0.1")
            else:
                target = request.url
        else:
            if request.url.scheme != "https":
                raise RuntimeFailure("OAuth discovery requires public HTTPS endpoints.", status=422)
            try:
                addresses = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        request.url.host, request.url.port or 443, type=socket.SOCK_STREAM
                    ),
                    timeout=10,
                )
            except OSError, TimeoutError:
                raise RuntimeFailure("OAuth endpoint DNS is unavailable.", status=503) from None
            ips = tuple(dict.fromkeys(str(row[4][0]) for row in addresses))
            if not ips or not all(public_address(ip) for ip in ips):
                raise RuntimeFailure("OAuth endpoint resolved to a non-public address.", status=422)
            target = request.url.copy_with(host=ips[0])
        pinned = httpx.Request(
            request.method,
            target,
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        if self.guard is not None:
            await self.guard()
        return await self.inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.inner.aclose()


class OAuthHTTP:
    def __init__(self, server_url: str) -> None:
        self.server_url = server_url
        self.guard: Callable[[], Awaitable[None]] | None = None

    def client(self, *, auth: httpx.Auth | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=OAuthTransport(self.server_url, self.guard),
            auth=auth,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(20, read=20),
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )

    async def request(
        self, method: str, url: str, *, data: dict[str, str] | None = None, auth: httpx.Auth | None = None
    ) -> tuple[int, dict[str, Any], httpx.Headers]:
        authenticated_url(url)
        try:
            async with (
                asyncio.timeout(30),
                self.client() as client,
                client.stream(
                    method, url, data=data, auth=auth, headers={"Accept": "application/json"}
                ) as response,
            ):
                # The initial resource GET is only a challenge probe; never consume an SSE body.
                if url == self.server_url and method == "GET":
                    return response.status_code, {}, response.headers
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 262144:
                        raise RuntimeFailure("OAuth response exceeds the size limit.", status=502)
                if response.status_code in {404, 405}:
                    return response.status_code, {}, response.headers
                try:
                    payload = json.loads(content) if content else {}
                except ValueError, UnicodeError:
                    raise RuntimeFailure("OAuth endpoint returned invalid JSON.", status=502) from None
                if not isinstance(payload, dict):
                    raise RuntimeFailure("OAuth endpoint returned an invalid object.", status=502)
                return response.status_code, payload, response.headers
        except httpx.HTTPError, TimeoutError:
            raise RuntimeFailure("OAuth endpoint request failed or timed out.", status=503) from None
