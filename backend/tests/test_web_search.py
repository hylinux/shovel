from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shovel.config.settings import AppSettings
from shovel.db.migrations import upgrade_database
from shovel.db.model.content import Document
from shovel.db.model.execution import Job
from shovel.domain.enums import DocClass, DocumentLifecycleState, JobState, ScanMode
from shovel.exceptions.pipeline import ParseError
from shovel.pipeline.parsers import parse_bytes
from shovel.pipeline.parsers.base import ParseHint
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import VectorRecord
from shovel.runtime.api import configure_api
from shovel.runtime.retrieval import RetrievalService
from shovel.runtime.schemas import SearchInput
from shovel.runtime.service import Runtime
from shovel.services import connector_registry
from shovel.services.connector_base import ConnectorCapability, DiscoveryScope, connector_session
from shovel.services.connector_web import WebSearchConnector
from shovel.services.credentials import InMemoryCredentialStore
from shovel.services.resource_service import ResourceDraft, ResourceService
from shovel.services.web import (
    PublicWebTransport,
    WebCrawlError,
    WebCrawlLimit,
    WebReader,
    WebScope,
    WebSearchConfig,
    retry_delay,
)

SITE = "https://docs.example.com"
CONFIG = {"start_url": SITE + "/"}


@dataclass
class Site:
    routes: dict[str, list[tuple[int, bytes, dict[str, str]]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    waits: list[float] = field(default_factory=list)

    def add(self, path: str, body: str | bytes, *, status: int = 200, **headers: str) -> None:
        payload = body.encode() if isinstance(body, str) else body
        self.routes[path] = [(status, payload, {"content-type": "text/html", **headers})]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode()
        responses = self.routes.get(path, [(404, b"", {})])
        status, body, headers = responses[0]
        if len(responses) > 1:
            responses.pop(0)
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    @property
    def paths(self) -> list[str]:
        return [request.url.raw_path.decode() for request in self.requests]


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Site:
    site = Site()
    site.add("/robots.txt", "User-agent: *\nDisallow:", **{"content-type": "text/plain"})
    site.add(
        "/",
        "<html><head><title>Documentation</title></head><body>"
        "<h1>Guide</h1><p>Budget approved.</p></body></html>",
    )
    monkeypatch.setattr(
        "shovel.services.web.PublicWebTransport", lambda _scope: httpx.MockTransport(site.handle)
    )

    async def wait(_reader: WebReader, seconds: float) -> None:
        site.waits.append(seconds)

    monkeypatch.setattr(WebReader, "wait", wait)
    return site


@pytest_asyncio.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = AppSettings.model_validate(
        {
            "database": {"url": "sqlite+aiosqlite:///" + str(tmp_path / "web.sqlite")},
            "agent": {"workspace_path": str(tmp_path / "workspace")},
            "zvec": {"knowledge_path": str(tmp_path / "knowledge")},
            "memory": {"index_path": str(tmp_path / "memory")},
        }
    )
    engine = create_async_engine(settings.database.url)
    await upgrade_database(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    runtime = Runtime(settings, sessions, ResourceService(sessions, InMemoryCredentialStore()))
    try:
        yield runtime
    finally:
        await runtime.close()
        await engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "https://docs.example.com:8443/",
        "https://user:pass@docs.example.com/",
        "https://docs.example.com/?token=secret",
        "https://docs.example.com/%2e%2e/private",
        "https://docs.example.com/%252e%252e/private",
        "http://printer.local/",
        "https://docs.example.com\\@127.0.0.1/",
    ],
)
def test_config_rejects_unsafe_or_ambiguous_websites(url: str) -> None:
    with pytest.raises(ValidationError):
        WebSearchConfig(start_url=url)


def test_scope_rejects_origin_and_path_escapes_and_deduplicates_fragments() -> None:
    scope = WebScope(SITE + "/docs/")
    assert scope.resolve("guide#section", SITE + "/docs/") == SITE + "/docs/guide"
    for url in (
        SITE + "/docs-other",
        SITE + "/other",
        "https://sub.docs.example.com/docs/",
        "http://docs.example.com/docs/",
    ):
        assert not scope.contains(url)
    assert not scope.contains(SITE + "/docs/%2e%2e/admin")
    assert not scope.contains(SITE + "/docs/%252e%252e/admin")


@pytest.mark.parametrize("filename", ["ts-tutorial.html", "index.htm", "index.xhtml", "INDEX.HTML"])
def test_html_page_scope_includes_its_directory_not_other_tutorials(filename: str) -> None:
    start = SITE + "/typescript/" + filename
    scope = WebScope(start)
    assert scope.start_url == start
    assert scope.resolve("ts-intro.html#intro", start) == SITE + "/typescript/ts-intro.html"
    assert scope.contains(SITE + "/typescript/chapters/types.html")
    for url in (
        SITE + "/typescript-other/page.html",
        SITE + "/js/js-tutorial.html",
        "https://other.example.com/typescript/ts-intro.html",
        "http://docs.example.com/typescript/ts-intro.html",
        SITE + "/typescript/%2e%2e/private",
    ):
        assert not scope.contains(url)


def test_explicit_directory_and_non_html_document_scopes_are_not_broadened() -> None:
    directory = WebScope(SITE + "/docs/guide.html/")
    assert directory.contains(SITE + "/docs/guide.html/chapter")
    assert not directory.contains(SITE + "/docs/other.html")
    document = WebScope(SITE + "/docs/guide.pdf")
    assert document.contains(SITE + "/docs/guide.pdf")
    assert not document.contains(SITE + "/docs/other.pdf")


async def test_transport_rejects_mixed_private_dns_and_pins_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = PublicWebTransport(WebScope(SITE))
    ips = ["93.184.216.34", "127.0.0.1"]

    async def resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(handle)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(WebCrawlError, match="private"):
            await client.get(SITE + "/")
        assert not seen
        ips.pop()
        assert (await client.get(SITE + "/")).status_code == 200
        assert seen[0].url.host == "93.184.216.34"
        assert seen[0].headers["host"] == "docs.example.com"
        assert seen[0].extensions["sni_hostname"] == "docs.example.com"
        with pytest.raises(WebCrawlError, match="origin"):
            await client.get("https://other.example.com/")
        assert len(seen) == 1


async def test_robots_longest_rule_wildcards_and_politeness(site: Site) -> None:
    site.add(
        "/robots.txt",
        "User-agent: *\nDisallow: /private\nDisallow: /*.pdf$\n"
        "Allow: /private/public\nCrawl-delay: 3\nRequest-rate: 1/5",
        **{"content-type": "text/plain"},
    )
    site.add(
        "/",
        '<a href="/private/secret">Secret</a><a href="/private/public">Public</a><a href="/a.pdf">PDF</a>',
    )
    site.add("/private/public", "<p>Public text</p>")
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope())]
        assert "/private/secret" not in site.paths and "/a.pdf" not in site.paths
        assert "/private/public" in site.paths
        assert connector.reader.delay == 5
        assert sum(item.extra["mime"] == "application/x-shovel-skipped" for item in items) == 2
        assert any("robots.txt disallow" in warning for warning in connector.discovery_warnings)
        assert all(request.headers["user-agent"].startswith("ShovelBot/") for request in site.requests)
        assert all(request.headers["accept-encoding"] == "identity" for request in site.requests)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_robots_unavailable_fails_closed(site: Site, status: int) -> None:
    site.add("/robots.txt", "", status=status)
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    result = await connector.verify()
    assert not result.ok and "robots.txt" in result.message
    assert "/" not in site.paths


async def test_missing_robots_allows_crawl_but_html_robots_is_rejected(site: Site) -> None:
    site.add("/robots.txt", "", status=404)
    assert (await WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).verify()).ok
    site.add("/robots.txt", "<!doctype html><html>not robots</html>")
    assert not (await WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).verify()).ok


async def test_retry_after_transient_errors_and_no_retry_for_forbidden(site: Site) -> None:
    site.routes["/"] = [
        (429, b"", {"retry-after": "7"}),
        (503, b"", {}),
        (200, b"<p>Recovered</p>", {"content-type": "text/html"}),
    ]
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    assert (await connector.verify()).ok
    assert site.paths.count("/") == 3
    assert any(wait >= 7 for wait in site.waits)
    site.add("/", "", status=403)
    assert not (await WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).verify()).ok
    assert site.paths.count("/") == 4
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    assert retry_delay(future, 0) >= 18
    with pytest.raises(WebCrawlLimit, match="Retry-After"):
        retry_delay("3600", 0)


@pytest.mark.parametrize("retry_after", ["7", "301"])
async def test_final_retry_after_still_limits_subsequent_pages(site: Site, retry_after: str) -> None:
    site.add("/", '<p>Root</p><a href="/limited">Limited</a><a href="/next">Next</a>')
    site.add("/limited", "", status=429, **{"retry-after": retry_after})
    site.add("/next", "<p>Next page</p>")
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/", max_attempts=1))
    async with connector_session(connector):
        _ = [item async for item in connector.discover(DiscoveryScope())]
        assert site.paths.count("/limited") == 1
        if retry_after == "7":
            assert "/next" in site.paths
            assert any(wait >= 6.9 for wait in site.waits)
        else:
            assert "/next" not in site.paths
            assert any("Retry-After" in warning for warning in connector.discovery_warnings)


async def test_redirects_never_fetch_other_origins_private_hosts_or_robot_denied_paths(site: Site) -> None:
    for location in ("http://127.0.0.1/", "https://other.example.com/", SITE + "/private"):
        site.add("/robots.txt", "User-agent: *\nDisallow: /private", **{"content-type": "text/plain"})
        site.add("/", "", status=302, location=location)
        connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
        assert not (await connector.verify()).ok
    assert set(site.paths) == {"/robots.txt", "/"}


async def test_html_sitemap_noindex_nofollow_scope_and_scan_cache(site: Site) -> None:
    site.add(
        "/",
        '<head><title>guide.pdf</title><base href="/docs/"></head><h1>Getting started</h1>'
        '<p>Visible text</p><script>hidden script</script><a href="one">One</a>'
        '<a rel="nofollow" href="/secret">Secret</a><a href="https://other.example.com/">Other</a>'
        '<a href="/query?page=1">Query</a>',
    )
    site.add(
        "/docs/one", '<meta name="robots" content="noindex"><p>Do not index this</p><a href="/two">Two</a>'
    )
    site.add("/two", '<meta name="robots" content="nofollow"><p>Index this</p><a href="/never">Never</a>')
    site.add(
        "/sitemap.xml",
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{SITE}/orphan</loc></url></urlset>',
        **{"content-type": "application/xml"},
    )
    site.add("/orphan", "<p>Sitemap-only content</p>")
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope())]
        assert set(site.paths) == {"/robots.txt", "/", "/docs/one", "/two", "/orphan", "/sitemap.xml"}
        assert (
            next(item for item in items if item.uri.endswith("/docs/one")).extra["skip_reason"] == "noindex"
        )
        root = next(item for item in items if item.uri == SITE + "/")
        payload = b"".join([part async for part in connector.open(root)])
        parsed = parse_bytes(
            payload, ParseHint(root.uri, root.title or "", mime="text/html", content_format="web-document-v1")
        )
        assert parsed.title == "guide.pdf"
        assert "Visible text" in " ".join(block.text for block in parsed.blocks)
        assert "hidden script" not in " ".join(block.text for block in parsed.blocks)
        with pytest.raises(ParseError) as error:
            _ = [part async for part in WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).open(root)]
        assert "not discovered" in error.value.reason
        temporary = connector._temporary
        assert temporary is not None
        directory = Path(temporary.name)
    assert not directory.exists()


async def test_page_depth_byte_limits_and_fetch_errors_are_visible(site: Site) -> None:
    site.add("/", '<p>Root</p><a href="/one">One</a><a href="/two">Two</a>')
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/", max_pages=1))
    async with connector_session(connector):
        assert len([item async for item in connector.discover(DiscoveryScope())]) == 1
        assert any("page limit" in warning for warning in connector.discovery_warnings)
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/", max_depth=0))
    async with connector_session(connector):
        _ = [item async for item in connector.discover(DiscoveryScope())]
        assert any("depth limit" in warning for warning in connector.discovery_warnings)
    site.add("/", "x" * 2000)
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/", max_page_bytes=1024))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope())]
        with pytest.raises(ParseError) as error:
            _ = [part async for part in connector.open(items[0])]
        assert "byte limit" in error.value.reason
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/", max_total_bytes=1024))
    async with connector_session(connector):
        _ = [item async for item in connector.discover(DiscoveryScope())]
        assert any("download limit" in warning for warning in connector.discovery_warnings)


async def test_network_retry_and_compressed_body_failure(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/":
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("connection interrupted", request=request)
        return site.handle(request)

    monkeypatch.setattr("shovel.services.web.PublicWebTransport", lambda _scope: httpx.MockTransport(handle))
    assert (await WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).verify()).ok
    assert attempts == 3
    site.add("/", b"compressed", **{"content-encoding": "gzip"})
    result = await WebSearchConnector(WebSearchConfig.model_validate(CONFIG)).verify()
    assert not result.ok and "compressed reads" in result.message


async def test_targeted_crawl_and_external_html_base_do_not_expand_scope(site: Site) -> None:
    site.add(
        "/docs/page", '<base href="https://outside.example.com/"><p>Content</p><a href="invented">Link</a>'
    )
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/docs/"))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope(target_ids=(SITE + "/docs/page",)))]
        assert len(items) == 1
        assert set(site.paths) == {"/robots.txt", "/docs/page"}
    site.requests.clear()
    site.add("/docs/", '<base href="https://outside.example.com/"><p>Content</p><a href="invented">Link</a>')
    connector = WebSearchConnector(WebSearchConfig(start_url=SITE + "/docs/"))
    async with connector_session(connector):
        _ = [item async for item in connector.discover(DiscoveryScope())]
    assert "/docs/invented" not in site.paths and "/invented" not in site.paths


async def test_sitemap_index_headers_and_forbidden_xml_are_respected(site: Site) -> None:
    site.add("/", "<p>Landing page</p>", **{"x-robots-tag": "max-snippet: 0, noindex"})
    site.add("/sitemap.xml", f"<sitemapindex><sitemap><loc>{SITE}/nested.xml</loc></sitemap></sitemapindex>")
    site.add("/nested.xml", f"<urlset><url><loc>{SITE}/orphan</loc></url></urlset>")
    site.add("/orphan", "<p>Public orphan</p>")
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope())]
        assert any(item.uri.endswith("/orphan") for item in items)
        assert next(item for item in items if item.uri == SITE + "/").extra["skip_reason"] == "noindex"
    site.add(
        "/sitemap.xml",
        '<!DOCTYPE urlset [<!ENTITY x SYSTEM "http://127.0.0.1/private">]><urlset>&x;</urlset>',
    )
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    async with connector_session(connector):
        _ = [item async for item in connector.discover(DiscoveryScope())]
        assert any("forbidden XML" in warning for warning in connector.discovery_warnings)
        assert not any(request.url.host == "127.0.0.1" for request in site.requests)


async def test_remote_office_zip_expansion_is_bounded(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shovel.services.connector_web._MAX_ARCHIVE_BYTES", 1024)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("document.xml", "x" * 2000)
    site.add("/", '<p>Root</p><a href="/document">Office file</a>')
    site.add(
        "/document",
        buffer.getvalue(),
        **{"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    )
    connector = WebSearchConnector(WebSearchConfig.model_validate(CONFIG))
    async with connector_session(connector):
        items = [item async for item in connector.discover(DiscoveryScope())]
        document = next(item for item in items if item.uri.endswith("/document"))
        with pytest.raises(ParseError) as error:
            _ = [part async for part in connector.open(document)]
        assert "archive limit" in error.value.reason


def test_html_parser_preserves_headings_encoding_and_drops_inert_hidden_content() -> None:
    html = (
        '<html lang="zh"><head><meta charset="gb18030"><title>指南</title></head>'
        "<h1>预算</h1><p>审批已完成</p><div hidden>hidden</div><script>script</script>"
        "<style>style</style><pre>code\nline</pre></html>"
    )
    parsed = parse_bytes(html.encode("gb18030"), ParseHint(SITE, "document.html", doc_class=DocClass.HTML))
    assert parsed.title == "指南" and parsed.lang == "zh"
    assert "审批已完成" in " ".join(block.text for block in parsed.blocks)
    assert parsed.blocks[1].path == ("预算",)
    assert "hidden" not in " ".join(block.text for block in parsed.blocks)
    assert parsed.blocks[-1].text == "code\nline"
    assert parse_bytes(html.encode("gb18030"), ParseHint(SITE, "x.html", max_chars=5)).truncated


class Embedder:
    model_version = "web-test"
    dimension = 4

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _text in texts]


class Sink:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        self.records.extend(records)
        return len(records)

    async def delete_document(self, resource_id: str, doc_uri: str) -> int:
        self.records = [
            record
            for record in self.records
            if (record.resource_id, record.doc_uri) != (resource_id, doc_uri)
        ]
        return 0

    async def delete_resource(self, resource_id: str) -> int:
        self.records = [record for record in self.records if record.resource_id != resource_id]
        return 0


async def test_tutorial_page_recursively_crawls_and_indexes_linked_chapters(
    runtime: Runtime, site: Site
) -> None:
    site.add(
        "/typescript/ts-tutorial.html",
        '<h1>TypeScript Tutorial</h1><nav><a href="ts-intro.html">Introduction</a>'
        '<a href="ts-intro.html#overview">Introduction fragment</a></nav>'
        '<a href="/js/js-tutorial.html">Other tutorial</a>'
        '<a href="/typescript-other/page.html">Other directory</a>'
        '<a href="https://other.example.com/typescript/page.html">External site</a>'
        '<a rel="nofollow" href="never.html">Do not follow</a>',
    )
    site.add(
        "/typescript/ts-intro.html",
        "<h1>TypeScript Introduction</h1><p>Tutorial introduction content.</p>"
        '<a href="chapters/types.html">Types</a><a href="ts-tutorial.html">Start</a>',
    )
    site.add(
        "/typescript/chapters/types.html",
        "<h1>TypeScript Types</h1><p>Tutorial types content.</p>"
        '<a href="../ts-intro.html#overview">Introduction</a>',
    )
    resource = await runtime.resources.create(
        ResourceDraft(
            name="tutorial",
            connector_kind="web_search",
            config={"start_url": SITE + "/typescript/ts-tutorial.html"},
        )
    )
    assert (await runtime.resources.verify(resource.id)).ok
    site.requests.clear()
    sink = Sink()
    result = await ScanService(runtime.sessions, runtime.resources, embedder=Embedder(), sink=sink).scan(
        resource.id
    )
    expected = {
        SITE + "/typescript/ts-tutorial.html",
        SITE + "/typescript/ts-intro.html",
        SITE + "/typescript/chapters/types.html",
    }
    assert result.state is JobState.SUCCEEDED and result.parsed == result.embedded == 3
    assert {record.doc_uri for record in sink.records} == expected
    assert len(site.paths) == len(set(site.paths))
    assert set(site.paths) == {"/robots.txt", "/sitemap.xml"} | {httpx.URL(url).path for url in expected}
    hits = await RetrievalService(runtime).search(SearchInput(query="Tutorial", mode="keyword"))
    assert {hit["doc_uri"] for hit in hits["items"]} == expected


async def test_website_scan_keyword_vectors_recovery_policy_and_no_deletion(
    runtime: Runtime, site: Site
) -> None:
    spec = connector_registry.get_spec("web_search")
    assert not spec.supports(ConnectorCapability.DELETE_DETECT)
    resource = await runtime.resources.create(
        ResourceDraft(name="docs", connector_kind="web_search", config=CONFIG)
    )
    assert (await runtime.resources.verify(resource.id)).ok
    service = ScanService(runtime.sessions, runtime.resources)
    assert (await service.scan(resource.id)).parsed == 1
    hits = await RetrievalService(runtime).search(SearchInput(query="Budget", mode="keyword"))
    assert hits["items"][0]["doc_uri"] == SITE + "/"
    sink = Sink()
    indexed = ScanService(runtime.sessions, runtime.resources, embedder=Embedder(), sink=sink)
    result = await indexed.scan(resource.id)
    assert result.embedded == 1 and result.vectors_written > 0
    assert all(record.granularity == "chunk" for record in sink.records)
    assert (await indexed.scan(resource.id)).skipped_unchanged == 1
    site.add("/", '<meta name="robots" content="noindex"><p>Budget approved.</p>')
    result = await indexed.scan(resource.id, mode=ScanMode.FULL_SWEEP)
    assert result.warnings and result.tombstoned == 0
    assert not sink.records
    async with runtime.sessions() as session:
        document = (await session.scalars(select(Document))).one()
        assert document.lifecycle_state is DocumentLifecycleState.SKIPPED
        assert document.skip_reason == "noindex"
    assert not (await RetrievalService(runtime).search(SearchInput(query="Budget", mode="keyword")))["items"]
    site.add("/", "<h1>Guide</h1><p>Budget approved.</p>")
    result = await indexed.scan(resource.id)
    assert result.parsed == 1 and result.vectors_written > 0


async def test_failed_page_does_not_discard_successful_pages(runtime: Runtime, site: Site) -> None:
    site.add("/", '<p>Budget approved.</p><a href="/failure">Failure</a>')
    site.add("/failure", "", status=503)
    resource = await runtime.resources.create(
        ResourceDraft(name="docs", connector_kind="web_search", config=CONFIG)
    )
    await runtime.resources.verify(resource.id)
    result = await ScanService(runtime.sessions, runtime.resources).scan(resource.id)
    assert result.state is JobState.COMPLETED_WITH_ERRORS and result.parsed == 1 and result.failed_docs == 1
    assert site.paths.count("/failure") == 3


async def test_resource_api_admission_uses_existing_durable_operations(runtime: Runtime, site: Site) -> None:
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
    ) as client:
        connectors = (await client.get("/api/v1/connectors")).json()["items"]
        assert any(connector["kind"] == "web_search" for connector in connectors)
        response = await client.post(
            "/api/v1/resources",
            json={
                "name": "docs",
                "connector_kind": "web_search",
                "config": CONFIG,
                "verify": True,
                "scan": False,
            },
            headers={"Idempotency-Key": "web-create"},
        )
        assert response.status_code == 202
        assert await runtime.operations.run_once()
        resources = (await client.get("/api/v1/resources")).json()["items"]
        assert resources[0]["config"]["start_url"] == SITE + "/"
        assert resources[0]["state"] == "active"
        response = await client.post(f"/api/v1/resources/{resources[0]['id']}/scans", json={"embed": False})
        assert response.status_code == 202
        assert await runtime.operations.run_once()


async def test_cancel_interrupts_crawl_wait_and_closes_scan_owned_resources(
    runtime: Runtime,
    site: Site,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await runtime.resources.create(
        ResourceDraft(name="docs", connector_kind="web_search", config=CONFIG)
    )
    await runtime.resources.verify(resource.id)
    entered = asyncio.Event()
    closed = asyncio.Event()
    original_close = WebSearchConnector.aclose

    async def wait(_reader: WebReader, _seconds: float) -> None:
        entered.set()
        await asyncio.Event().wait()

    async def close(connector: WebSearchConnector) -> None:
        await original_close(connector)
        closed.set()

    monkeypatch.setattr(WebReader, "wait", wait)
    monkeypatch.setattr(WebSearchConnector, "aclose", close)
    service = ScanService(runtime.sessions, runtime.resources)
    job = await service.prepare_job(resource.id)
    async with runtime.sessions() as session:
        session.add(job)
        await session.commit()
    job_id = job.id
    task = asyncio.create_task(service.scan(resource.id, job_id=job_id))
    await asyncio.wait_for(entered.wait(), 5)
    await runtime.operations.cancel_job(job_id)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert closed.is_set()
    async with runtime.sessions() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.state is JobState.CANCELLED
