"""Public-site URL fencing and polite, DNS-pinned HTTP reads."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import posixpath
import random
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import httpx
from protego import Protego
from pydantic import BaseModel, ConfigDict, Field, field_validator

BOT_NAME = "ShovelBot"
USER_AGENT = "ShovelBot/0.1 (public knowledge indexing; +https://github.com/hylinux/shovel)"
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class WebCrawlError(Exception):
    """A source or policy failure, safe to expose without response content."""


class WebCrawlLimit(WebCrawlError):
    """Stop issuing site requests, retaining already discovered content."""


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return public_address(str(address.ipv4_mapped))
    return address.is_global and not address.is_multicast and not address.is_reserved


def canonical_url(value: str) -> str:
    if len(value) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Use an HTTP(S) URL without whitespace, control characters or backslashes.")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid website URL.") from exc
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("Use an absolute HTTP(S) URL without embedded credentials.")
    if url.port not in {None, 80 if url.scheme == "http" else 443}:
        raise ValueError("Only the standard HTTP and HTTPS ports are allowed.")
    host = url.host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
        raise ValueError("Web Search can only access public websites.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not public_address(str(address)):
        raise ValueError("Web Search cannot access private, loopback or reserved addresses.")
    path = url.path
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if unquote(path) != path or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Website paths must not contain encoded traversal or backslashes.")
    return str(url.copy_with(host=host, fragment=None))


class WebSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_url: str = Field(
        description=(
            "Public website URL; crawl stays on its origin. HTML and extensionless page URLs crawl "
            "their containing directory; directory URLs ending in / crawl only that subtree."
        )
    )
    max_pages: int = Field(default=1000, ge=1, le=10000)
    max_depth: int = Field(default=10, ge=0, le=30)
    request_delay_seconds: float = Field(default=1, ge=1, le=60, allow_inf_nan=False)
    timeout_seconds: float = Field(default=20, ge=1, le=120, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_page_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_total_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    max_duration_seconds: int = Field(default=3600, ge=10, le=86400)
    include_query_urls: bool = Field(default=False, description="Opt in to query URLs, which may form traps.")

    @field_validator("start_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalized = canonical_url(value.strip())
        if httpx.URL(normalized).query:
            raise ValueError("Start with a website URL without a query string.")
        return normalized


class WebScope:
    def __init__(self, start_url: str) -> None:
        self.start_url = canonical_url(start_url)
        self.root = httpx.URL(self.start_url)
        path = unquote(self.root.path)
        if not path.endswith("/") and posixpath.splitext(path)[1].lower() in {"", ".html", ".htm", ".xhtml"}:
            path = posixpath.dirname(path)
        self.path = path.rstrip("/")

    def contains(self, value: str, *, origin_only: bool = False) -> bool:
        try:
            url = httpx.URL(canonical_url(value))
        except ValueError:
            return False
        if (url.scheme, url.host, url.port) != (self.root.scheme, self.root.host, self.root.port):
            return False
        decoded = unquote(url.path)
        return origin_only or not self.path or decoded == self.path or decoded.startswith(self.path + "/")

    def resolve(self, value: str, base: str, *, origin_only: bool = False) -> str | None:
        try:
            url = canonical_url(str(httpx.URL(base).join(value)))
        except ValueError, httpx.InvalidURL:
            return None
        return url if self.contains(url, origin_only=origin_only) else None

    @property
    def robots_url(self) -> str:
        return str(self.root.copy_with(path="/robots.txt", query=None))


class PublicWebTransport(httpx.AsyncBaseTransport):
    def __init__(self, scope: WebScope) -> None:
        self.scope = scope
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.scope.contains(str(request.url), origin_only=True):
            raise WebCrawlError("Request or redirect is outside the authorized website origin.")
        try:
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    request.url.host,
                    request.url.port or (443 if request.url.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
                timeout=10,
            )
        except (OSError, TimeoutError) as exc:
            raise httpx.ConnectError("Website DNS resolution failed.", request=request) from exc
        ips = tuple(dict.fromkeys(str(result[4][0]) for result in addresses))
        if not ips or not all(public_address(ip) for ip in ips):
            raise WebCrawlError("Website DNS resolved to a private, loopback or reserved address.")
        # Pin the socket destination, retaining the original Host and TLS verification name.
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=ips[0]),
            headers=request.headers,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self.inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass(frozen=True)
class WebResponse:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes


class WebReader:
    def __init__(self, config: WebSearchConfig) -> None:
        self.config = config
        self.scope = WebScope(config.start_url)
        self.client = httpx.AsyncClient(
            transport=PublicWebTransport(self.scope),
            trust_env=False,
            follow_redirects=False,
            timeout=config.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )
        self.robots: Protego | None = None
        self.delay = config.request_delay_seconds
        self.next_request = 0.0
        self.total_bytes = 0
        self.started = time.monotonic()
        self.retries = 0

    def remaining_seconds(self) -> float:
        return self.config.max_duration_seconds - (time.monotonic() - self.started)

    async def wait(self, seconds: float) -> None:
        if seconds >= self.remaining_seconds():
            raise WebCrawlLimit("Crawl duration limit reached; scan is incomplete.")
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def load_robots(self) -> None:
        response = await self.get(
            self.scope.robots_url, origin_only=True, robots_request=True, max_bytes=524288
        )
        if response.status in {404, 410}:
            self.robots = Protego.parse("")
        elif response.status == 200:
            if (
                response.headers.get("content-type", "").split(";", 1)[0].lower() == "text/html"
                or b"<html" in response.body[:512].lower()
                or b"<!doctype html" in response.body[:512].lower()
            ):
                raise WebCrawlError("robots.txt returned HTML; crawling is blocked until it is readable.")
            self.robots = Protego.parse(response.body.decode("utf-8-sig", errors="replace"))
        else:
            raise WebCrawlError(f"robots.txt is unavailable (HTTP {response.status}); crawling is blocked.")
        delay = self.robots.crawl_delay(BOT_NAME)
        rate = self.robots.request_rate(BOT_NAME)
        self.delay = max(self.delay, delay or 0, rate.seconds / rate.requests if rate else 0)
        if not math.isfinite(self.delay):
            raise WebCrawlError("robots.txt requests an unsupported crawl delay.")
        self.next_request = max(self.next_request, time.monotonic() + self.delay)

    async def get(
        self,
        url: str,
        *,
        origin_only: bool = False,
        robots_request: bool = False,
        max_bytes: int | None = None,
    ) -> WebResponse:
        current = url
        for _ in range(6):
            if not self.scope.contains(current, origin_only=origin_only):
                raise WebCrawlError("Redirect is outside the authorized website scope.")
            if not robots_request and (self.robots is None or not self.robots.can_fetch(current, BOT_NAME)):
                raise WebCrawlError("robots.txt disallows this URL.")
            response = await self._request(current, max_bytes or self.config.max_page_bytes)
            if response.status not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            target = self.scope.resolve(location, current, origin_only=origin_only) if location else None
            if target is None:
                raise WebCrawlError("Redirect is missing or outside the authorized website scope.")
            current = target
        raise WebCrawlError("Website redirect limit reached.")

    async def _request(self, url: str, max_bytes: int) -> WebResponse:
        for attempt in range(self.config.max_attempts):
            await self.wait(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.delay
            self.client.cookies.clear()
            try:
                async with asyncio.timeout(min(self.config.timeout_seconds, self.remaining_seconds())):
                    async with self.client.stream("GET", url) as response:
                        if response.status_code in _RETRY_STATUSES and attempt + 1 < self.config.max_attempts:
                            wait = retry_delay(response.headers.get("retry-after"), attempt)
                        else:
                            if response.status_code in _RETRY_STATUSES:
                                self.next_request = max(
                                    self.next_request,
                                    time.monotonic()
                                    + retry_delay(response.headers.get("retry-after"), attempt),
                                )
                            body = bytearray()
                            if response.status_code == 200:
                                if response.headers.get("content-encoding", "identity").lower() != "identity":
                                    raise WebCrawlError(
                                        "Server ignored identity encoding; compressed reads are blocked."
                                    )
                                async for part in response.aiter_raw(chunk_size=65536):
                                    self.total_bytes += len(part)
                                    if self.total_bytes > self.config.max_total_bytes:
                                        raise WebCrawlLimit(
                                            "Crawl download limit reached; scan is incomplete."
                                        )
                                    if len(body) + len(part) > max_bytes:
                                        raise WebCrawlError("Page exceeds the configured byte limit.")
                                    body.extend(part)
                            return WebResponse(url, response.status_code, response.headers, bytes(body))
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                TimeoutError,
            ) as exc:
                if attempt + 1 == self.config.max_attempts:
                    raise WebCrawlError(
                        f"Website request failed after {self.config.max_attempts} attempts "
                        f"({type(exc).__name__})."
                    ) from exc
                wait = retry_delay(None, attempt)
            self.retries += 1
            await self.wait(wait)
        raise AssertionError("Retry loop must return or raise.")

    async def aclose(self) -> None:
        await self.client.aclose()


def retry_delay(value: str | None, attempt: int) -> float:
    backoff = float(min(30.0, 2.0**attempt)) + random.uniform(0, 0.5)  # noqa: S311
    if value is None:
        return backoff
    try:
        seconds = (
            float(value)
            if value.isdecimal()
            else (parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)).total_seconds()
        )
    except ValueError, TypeError, OverflowError:
        return backoff
    if not math.isfinite(seconds) or seconds > 300:
        raise WebCrawlLimit("Server requested a long Retry-After; retry the scan later.")
    return max(backoff, seconds)


def url_path(value: str) -> str:
    return posixpath.basename(httpx.URL(value).path) or "index.html"
