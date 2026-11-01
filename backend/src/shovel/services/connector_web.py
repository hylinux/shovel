"""Read-only website discovery feeding the existing document scan pipeline."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import BaseModel, SecretStr

from shovel.domain.enums import DocClass
from shovel.exceptions.pipeline import ParseError
from shovel.exceptions.resource import ConnectorDiscoveryError
from shovel.pipeline.parsers.html_doc import HTMLContent, robots_directives
from shovel.pipeline.parsers.registry import WEB_MIME_PARSERS
from shovel.services.connector_base import (
    ConnectorCapability,
    ConnectorSpec,
    DiscoveredItem,
    DiscoveryScope,
    VerifyResult,
)
from shovel.services.web import BOT_NAME, WebCrawlError, WebCrawlLimit, WebReader, WebSearchConfig, url_path

_SKIP_MIME = "application/x-shovel-skipped"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_ASSET_SUFFIXES = frozenset(
    {
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".mp3",
        ".mp4",
        ".zip",
        ".exe",
    }
)


@dataclass(frozen=True)
class _CachedPage:
    item: DiscoveredItem
    path: Path | None = None
    error: str | None = None


class WebSearchConnector:
    spec: ClassVar[ConnectorSpec] = ConnectorSpec(
        kind="web_search",
        display_name="Web Search",
        description=(
            "Crawl public website pages and documents within an explicit URL scope, respecting robots rules."
        ),
        config_model=WebSearchConfig,
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVER,
                ConnectorCapability.INCREMENTAL,
                ConnectorCapability.RANDOM_READ,
            }
        ),
        default_authority=0.5,
    )

    def __init__(self, config: BaseModel, credential: SecretStr | None = None) -> None:
        if not isinstance(config, WebSearchConfig):
            raise TypeError("WebSearchConnector requires WebSearchConfig.")
        self.config = config
        self.reader = WebReader(config)
        self._temporary: TemporaryDirectory[str] | None = None
        self._pages: dict[str, _CachedPage] = {}
        self._warnings: set[str] = set()
        self._counts: Counter[str] = Counter()

    @property
    def discovery_warnings(self) -> tuple[str, ...]:
        summaries = tuple(
            f"Web crawl skipped {count} URL(s): {reason}." for reason, count in self._counts.items()
        )
        retries = (f"Web crawl retried {self.reader.retries} request(s).",) if self.reader.retries else ()
        return tuple(sorted(self._warnings)) + summaries + retries

    async def aclose(self) -> None:
        try:
            await self.reader.aclose()
        finally:
            self._pages.clear()
            if self._temporary is not None:
                await asyncio.to_thread(self._temporary.cleanup)
                self._temporary = None

    async def verify(self) -> VerifyResult:
        try:
            await self.reader.load_robots()
            response = await self.reader.get(self.config.start_url)
            if response.status != 200:
                raise WebCrawlError(f"Website returned HTTP {response.status}.")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                not in WEB_MIME_PARSERS
            ):
                raise WebCrawlError("Website does not serve a supported document Content-Type.")
            return VerifyResult.success(
                "Public website is readable. Crawl stays within the selected origin and path; "
                "robots rules and configured limits apply. Missing pages are not automatically deleted.",
                details={"request_delay_seconds": self.reader.delay},
            )
        except WebCrawlError as exc:
            return VerifyResult.failure(str(exc))
        finally:
            await self.aclose()

    def _item(
        self,
        url: str,
        *,
        mime: str = "text/html",
        title: str | None = None,
        content_hash: str | None = None,
        size: int | None = None,
        modified: int | None = None,
        encoding: str | None = None,
        skip_reason: str | None = None,
    ) -> DiscoveredItem:
        return DiscoveredItem(
            external_id=url,
            uri=url,
            title=title or url_path(url),
            size_bytes=size,
            content_hash=content_hash,
            modified_at=modified,
            doc_class=DocClass.HTML if mime in {"text/html", "application/xhtml+xml"} else DocClass.UNKNOWN,
            extra={
                "content_format": "web-document-v1",
                "mime": mime,
                "encoding": encoding,
                "skip_reason": skip_reason,
            },
        )

    def _skip(self, url: str, reason: str) -> DiscoveredItem:
        self._counts[reason] += 1
        return self._item(url, mime=_SKIP_MIME, skip_reason=reason)

    async def discover(self, scope: DiscoveryScope) -> AsyncIterator[DiscoveredItem]:
        self._warnings.clear()
        self._counts.clear()
        self._pages.clear()
        self._temporary = TemporaryDirectory(prefix="shovel-web-")
        try:
            await self.reader.load_robots()
        except WebCrawlError as exc:
            raise ConnectorDiscoveryError(self.config.start_url, str(exc)) from exc
        frontier: deque[tuple[str, int]] = deque()
        scheduled: set[str] = set()
        visited: set[str] = set()

        def enqueue(value: str, base: str, depth: int) -> None:
            url = self.reader.scope.resolve(value, base)
            if url is None or url in scheduled:
                return
            if any(fnmatch.fnmatchcase(url, pattern) for pattern in scope.exclude):
                return
            if not self.config.include_query_urls and httpx.URL(url).query:
                self._counts["query URLs disabled"] += 1
                return
            if Path(url_path(url)).suffix.lower() in _ASSET_SUFFIXES:
                return
            if depth > self.config.max_depth:
                self._warnings.add("Web crawl depth limit reached; linked content remains unvisited.")
                return
            if len(scheduled) >= self.config.max_pages:
                self._warnings.add("Web crawl page limit reached; linked content remains unvisited.")
                return
            scheduled.add(url)
            frontier.append((url, depth))

        if scope.target_ids:
            for identifier in scope.target_ids:
                if self.reader.scope.resolve(identifier, self.config.start_url) is None:
                    raise ConnectorDiscoveryError(
                        self.config.start_url, "Target URL is outside the website scope."
                    )
                enqueue(identifier, self.config.start_url, 0)
        else:
            enqueue(self.config.start_url, self.config.start_url, 0)
            try:
                async for page_url in self._sitemap_urls():
                    enqueue(page_url, self.config.start_url, 0)
            except WebCrawlLimit as exc:
                self._warnings.add(str(exc))
                return

        while frontier:
            url, depth = frontier.popleft()
            if url in visited:
                continue
            visited.add(url)
            assert self.reader.robots is not None
            if not self.reader.robots.can_fetch(url, BOT_NAME):
                yield self._skip(url, "robots.txt disallow")
                continue
            try:
                response = await self.reader.get(
                    url,
                    max_bytes=min(
                        self.config.max_page_bytes,
                        scope.max_file_bytes or self.config.max_page_bytes,
                    ),
                )
                if response.url in visited and response.url != url:
                    continue
                visited.add(response.url)
                if response.status != 200:
                    raise WebCrawlError(f"Website returned HTTP {response.status}.")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in WEB_MIME_PARSERS:
                    yield self._skip(response.url, "unsupported Content-Type")
                    continue
                encoding = httpx.Response(200, headers=response.headers).charset_encoding
                directives = robots_directives(response.headers.get_list("x-robots-tag"))
                content = None
                if mime in {"text/html", "application/xhtml+xml"}:
                    content = await asyncio.to_thread(HTMLContent.read, response.body, encoding)
                    directives.update(content.directives)
                if content is not None and "nofollow" not in directives and not scope.target_ids:
                    try:
                        base = (
                            str(httpx.URL(response.url).join(content.base)) if content.base else response.url
                        )
                    except httpx.InvalidURL as exc:
                        raise WebCrawlError("HTML contains an invalid base URL.") from exc
                    for link in content.links:
                        enqueue(link, base, depth + 1)
                if "noindex" in directives:
                    yield self._skip(response.url, "noindex")
                    continue
                if mime.startswith("application/vnd.openxmlformats-officedocument."):
                    await asyncio.to_thread(_validate_office_archive, response.body)
                if scope.include and not any(
                    fnmatch.fnmatchcase(response.url, pattern) for pattern in scope.include
                ):
                    continue
                modified = _modified_at(response.headers.get("last-modified"))
                if scope.time_window_days is not None and (
                    modified is None or modified < time.time() - scope.time_window_days * 86400
                ):
                    continue
                item = self._item(
                    response.url,
                    mime=mime,
                    title=content.title if content else None,
                    content_hash=hashlib.sha256(response.body).hexdigest(),
                    size=len(response.body),
                    modified=modified,
                    encoding=encoding,
                )
                path = Path(self._temporary.name) / str(len(self._pages))
                await asyncio.to_thread(path.write_bytes, response.body)
                self._pages[item.external_id] = _CachedPage(item, path)
                yield item
            except WebCrawlLimit as exc:
                self._warnings.add(str(exc))
                break
            except (WebCrawlError, UnicodeError, LookupError) as exc:
                message = str(exc) if isinstance(exc, WebCrawlError) else "Cannot decode website HTML."
                item = self._item(url)
                self._pages[url] = _CachedPage(item, error=message)
                yield item

    async def _sitemap_urls(self) -> AsyncIterator[str]:
        assert self.reader.robots is not None
        maps: deque[str] = deque()
        scheduled: set[str] = set()

        def enqueue_sitemap(value: str, base: str) -> None:
            url = self.reader.scope.resolve(value, base, origin_only=True)
            if url is None or url in scheduled:
                return
            if len(scheduled) >= 20:
                self._warnings.add("Web crawl sitemap limit reached; discovery is incomplete.")
                return
            scheduled.add(url)
            maps.append(url)

        initial = tuple(self.reader.robots.sitemaps)
        for value in initial or (str(self.reader.scope.root.copy_with(path="/sitemap.xml", query=None)),):
            enqueue_sitemap(value, self.config.start_url)
        visited: set[str] = set()
        while maps:
            url = maps.popleft()
            if url in visited:
                continue
            visited.add(url)
            if not self.reader.robots.can_fetch(url, BOT_NAME):
                self._counts["robots.txt disallow sitemap"] += 1
                continue
            try:
                response = await self.reader.get(url, origin_only=True)
                if response.status in {404, 410}:
                    continue
                if response.status != 200:
                    raise WebCrawlError(f"Sitemap returned HTTP {response.status}.")
                lower = response.body.lower()
                if b"<!doctype" in lower or b"<!entity" in lower or b"\x00" in lower:
                    raise WebCrawlError("Sitemap contains forbidden XML declarations.")
                root = await asyncio.to_thread(ET.fromstring, response.body)
                kind = root.tag.rsplit("}", 1)[-1]
                if kind not in {"sitemapindex", "urlset"}:
                    raise WebCrawlError("Sitemap must contain a sitemapindex or urlset.")
                for element in root:
                    location = next(
                        (child.text for child in element if child.tag.rsplit("}", 1)[-1] == "loc"), None
                    )
                    if location:
                        if kind == "sitemapindex":
                            enqueue_sitemap(location.strip(), response.url)
                        else:
                            yield location.strip()
            except WebCrawlLimit:
                raise
            except (WebCrawlError, ET.ParseError) as exc:
                self._warnings.add(f"Sitemap discovery failed: {exc}")

    async def open(self, item: DiscoveredItem) -> AsyncIterator[bytes]:
        cached = self._pages.get(item.external_id)
        if cached is None or item != cached.item:
            raise ParseError(item.uri, "Page was not discovered by this connector instance.")
        if cached.error:
            raise ParseError(item.uri, cached.error)
        if cached.path is None:
            raise ParseError(item.uri, "Discovered page has no cached content.")
        yield await asyncio.to_thread(cached.path.read_bytes)


def _modified_at(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except ValueError, TypeError, OverflowError:
        return None


def _validate_office_archive(payload: bytes) -> None:
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(entry.file_size for entry in entries) > _MAX_ARCHIVE_BYTES:
                raise WebCrawlError("Office document exceeds the uncompressed archive limit.")
            if any(entry.flag_bits & 1 for entry in entries):
                raise WebCrawlError("Encrypted Office documents are not supported.")
    except BadZipFile as exc:
        raise WebCrawlError("Website returned an invalid Office document archive.") from exc
