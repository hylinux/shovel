from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shovel.config.settings import AppSettings
from shovel.db.migrations import upgrade_database
from shovel.db.model.content import Chunk, Document
from shovel.domain.enums import DocClass, DocumentPipeState, Granularity, JobState, ScanMode
from shovel.exceptions.pipeline import ParseError
from shovel.exceptions.resource import ConnectorDiscoveryError
from shovel.pipeline.parsers import can_parse, parse_bytes
from shovel.pipeline.parsers.base import ParseHint
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import VectorRecord
from shovel.runtime.api import configure_api
from shovel.runtime.retrieval import RetrievalService
from shovel.runtime.schemas import SearchInput
from shovel.runtime.service import Runtime
from shovel.services import connector_registry
from shovel.services.connector_base import ConnectorCapability, DiscoveryScope
from shovel.services.connector_outlook import OutlookConnector
from shovel.services.credentials import InMemoryCredentialStore
from shovel.services.outlook import (
    MAIL_FORMAT,
    OutlookConfig,
    OutlookError,
    OutlookItem,
    OutlookMail,
    OutlookSession,
    OutlookWorkerError,
)
from shovel.services.outlook_worker import OutlookReader, _version
from shovel.services.resource_service import ResourceDraft, ResourceService

CONFIG = {"store_id": "store", "folder_ids": ["inbox"]}


def mail_record(**changes: Any) -> OutlookMail:
    return OutlookMail.model_validate(
        {
            "store_id": "store",
            "entry_id": "entry",
            "version": "revision-1",
            "subject": "budget.pdf",
            "sender": "Alice <alice@example.test>",
            "to": "Bob",
            "cc": "",
            "sent_at": 1_700_000_000,
            "received_at": 1_700_000_001,
            "folder_path": "\\Mailbox\\Inbox",
            "thread_id": "conversation",
            "body": "预算审批已经完成。\n\nThe approved budget is 100.",
            "attachments": [{"name": "report.pdf", "size_bytes": 100}],
            **changes,
        }
    )


def discovered_record(**changes: Any) -> OutlookItem:
    return OutlookItem.model_validate(
        {
            "store_id": "store",
            "entry_id": "entry",
            "folder_id": "inbox",
            "folder_path": "\\Mailbox\\Inbox",
            "subject": "budget.pdf",
            "modified_at": 1_700_000_000,
            "received_at": 1_700_000_001,
            "size_bytes": 100,
            "version": "revision-1",
            **changes,
        }
    )


class FakeSession:
    def __init__(self) -> None:
        self.items = [discovered_record()]
        self.mail = mail_record()
        self.closed = False
        self.fail: str | None = None
        self.calls: list[str] = []

    async def request(self, command: str, **_payload: object) -> dict[str, Any]:
        self.calls.append(command)
        if self.fail == command:
            raise OutlookError("Outlook is unavailable.")
        if command == "verify":
            return {"folders": 1}
        if command == "discover":
            return {"items": [item.model_dump() for item in self.items], "done": False}
        if command == "next":
            return {"items": [], "done": True}
        if command == "open":
            return self.mail.model_dump()
        raise AssertionError(command)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    worker = FakeSession()
    monkeypatch.setattr("shovel.services.connector_outlook.OutlookSession", lambda _timeout: worker)
    return worker


@pytest_asyncio.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = AppSettings.model_validate(
        {"database": {"url": "sqlite+aiosqlite:///:memory:"}, "zvec": {"knowledge_path": str(tmp_path)}}
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


def test_outlook_is_registered_but_cannot_detect_deletions() -> None:
    spec = connector_registry.get_spec("outlook_local")
    assert not spec.supports(ConnectorCapability.DELETE_DETECT)
    assert spec.supports_mode(ScanMode.INCREMENTAL)
    assert not spec.requires_identity
    with pytest.raises(ValidationError):
        OutlookConfig.model_validate({**CONFIG, "folder_ids": []})
    with pytest.raises(ValidationError):
        OutlookConfig.model_validate({**CONFIG, "unknown": True})


def test_mail_format_wins_over_subject_extension_and_preserves_headers() -> None:
    hint = ParseHint("outlook-local://mail/id", "budget.pdf", content_format=MAIL_FORMAT)
    assert can_parse(hint)
    parsed = parse_bytes(mail_record().model_dump_json().encode(), hint)
    assert parsed.doc_class is DocClass.EMAIL
    assert parsed.meta["thread_id"] == "conversation"
    assert parsed.meta["attachments"][0]["name"] == "report.pdf"
    assert "alice@example.test" in parsed.blocks[0].text
    assert "预算审批" in "\n".join(block.text for block in parsed.blocks)
    assert parsed.blocks[-1].locator
    assert "From: Alice" in parsed.blocks[-1].path[1]
    assert not can_parse(ParseHint("file:///mail.msg", "mail.msg", doc_class=DocClass.EMAIL))


def test_mail_parser_rejects_empty_or_invalid_content_and_marks_truncation() -> None:
    hint = ParseHint("outlook-local://mail/id", "mail", max_chars=200, content_format=MAIL_FORMAT)
    for payload in (b"{}", mail_record(body=" ").model_dump_json().encode()):
        with pytest.raises(ParseError):
            parse_bytes(payload, hint)
    parsed = parse_bytes(mail_record(body="body " * 200).model_dump_json().encode(), hint)
    assert parsed.truncated
    assert sum(len(block.text) for block in parsed.blocks) <= 200
    assert parse_bytes(mail_record(truncated=True).model_dump_json().encode(), hint).truncated


async def test_connector_pages_filters_and_reads_only_discovered_mail(worker: FakeSession) -> None:
    connector = OutlookConnector(OutlookConfig.model_validate(CONFIG))
    worker.items.extend([discovered_record(), discovered_record(entry_id="large", size_bytes=1000)])
    items = [item async for item in connector.discover(DiscoveryScope(max_file_bytes=200, since=2**40))]
    assert len(items) == 1
    assert worker.calls == ["discover", "next"]
    assert items[0].uri.startswith("outlook-local://")
    assert "Alice" not in items[0].uri
    assert items[0].extra["content_format"] == MAIL_FORMAT
    payload = b"".join([part async for part in connector.open(items[0])])
    assert OutlookMail.model_validate_json(payload).body == worker.mail.body
    other = OutlookConnector(OutlookConfig.model_validate(CONFIG))
    with pytest.raises(ParseError) as error:
        _ = [part async for part in other.open(items[0])]
    assert "not discovered" in error.value.reason
    await connector.aclose()
    assert worker.closed


async def test_scope_and_revision_failures_are_explicit(worker: FakeSession) -> None:
    connector = OutlookConnector(OutlookConfig.model_validate(CONFIG))
    assert [item async for item in connector.discover(DiscoveryScope(include=("*other*",)))] == []
    assert [item async for item in connector.discover(DiscoveryScope(exclude=("*Inbox*",)))] == []
    assert [item async for item in connector.discover(DiscoveryScope(target_ids=("other",)))] == []
    assert [item async for item in connector.discover(DiscoveryScope(time_window_days=1))] == []
    items = [item async for item in connector.discover(DiscoveryScope())]
    worker.mail = mail_record(version="changed")
    with pytest.raises(ParseError) as error:
        _ = [part async for part in connector.open(items[0])]
    assert "different mail revision" in error.value.reason
    worker.items = [discovered_record(store_id="unauthorized")]
    with pytest.raises(ConnectorDiscoveryError):
        _ = [item async for item in connector.discover(DiscoveryScope())]
    await connector.aclose()


async def test_verify_failure_closes_worker(worker: FakeSession) -> None:
    worker.fail = "verify"
    connector = OutlookConnector(OutlookConfig.model_validate(CONFIG))
    result = await connector.verify()
    assert not result.ok and "Outlook" in result.message
    assert worker.closed


async def test_mail_scan_keyword_recovery_and_no_automatic_deletion(
    runtime: Runtime, worker: FakeSession
) -> None:
    resource = await runtime.resources.create(
        ResourceDraft(name="mail", connector_kind="outlook_local", config=CONFIG)
    )
    assert (await runtime.resources.verify(resource.id)).ok
    service = ScanService(runtime.sessions, runtime.resources)
    result = await service.scan(resource.id)
    assert result.state is JobState.SUCCEEDED and result.parsed == 1
    assert worker.closed
    assert (await service.scan(resource.id)).skipped_unchanged == 1
    hits = await RetrievalService(runtime).search(SearchInput(query="预算审批", mode="keyword"))
    assert len(hits["items"]) == 1
    assert hits["items"][0]["email"]["sender"] == worker.mail.sender

    worker.items = []
    result = await service.scan(resource.id, mode=ScanMode.FULL_SWEEP)
    assert result.tombstoned == 0 and result.warnings
    async with runtime.sessions() as session:
        document = (await session.scalars(select(Document))).one()
        assert document.pipe_state is DocumentPipeState.CHUNKED
        assert document.source_meta["thread_id"] == "conversation"
        assert document.source_meta["attachments"][0]["size_bytes"] == 100


async def test_scan_closes_on_discovery_error_and_per_mail_failure(
    runtime: Runtime, worker: FakeSession
) -> None:
    resource = await runtime.resources.create(
        ResourceDraft(name="mail", connector_kind="outlook_local", config=CONFIG)
    )
    assert (await runtime.resources.verify(resource.id)).ok
    service = ScanService(runtime.sessions, runtime.resources)
    worker.closed = False
    worker.fail = "discover"
    assert (await service.scan(resource.id)).state is JobState.FAILED
    assert worker.closed
    worker.closed = False
    worker.fail = "open"
    result = await service.scan(resource.id)
    assert result.state is JobState.COMPLETED_WITH_ERRORS and result.failed_docs == 1
    assert worker.closed


class FakeEmbedder:
    model_version = "mail-test"
    dimension = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeSink:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        self.records.extend(records)
        return len(records)

    async def delete_document(self, resource_id: str, doc_uri: str) -> int:
        self.records = [r for r in self.records if r.resource_id != resource_id or r.doc_uri != doc_uri]
        return 0

    async def delete_resource(self, resource_id: str) -> int:
        self.records = [r for r in self.records if r.resource_id != resource_id]
        return 0


async def test_keyword_mail_can_be_completed_with_vectors(runtime: Runtime, worker: FakeSession) -> None:
    resource = await runtime.resources.create(
        ResourceDraft(name="mail", connector_kind="outlook_local", config=CONFIG)
    )
    await runtime.resources.verify(resource.id)
    await ScanService(runtime.sessions, runtime.resources).scan(resource.id)
    sink = FakeSink()
    service = ScanService(runtime.sessions, runtime.resources, embedder=FakeEmbedder(), sink=sink)
    result = await service.scan(resource.id)
    assert result.embedded == 1 and result.vectors_written > 0
    assert all(record.granularity == "chunk" for record in sink.records)
    assert all("alice@example.test" in record.text for record in sink.records)
    async with runtime.sessions() as session:
        chunks = list(await session.scalars(select(Chunk).where(Chunk.granularity == Granularity.CHUNK)))
        assert len(chunks) == result.vectors_written
    assert (await service.scan(resource.id)).skipped_unchanged == 1


async def test_discovery_api_uses_local_access_guards(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stores() -> list[Any]:
        from shovel.services.outlook import OutlookStore

        return [OutlookStore(id="store", name="Mailbox")]

    async def folders(store_id: str, parent_id: str | None) -> list[Any]:
        from shovel.services.outlook import OutlookFolder

        assert store_id == "store" and parent_id == "parent"
        return [OutlookFolder(id="inbox", name="Inbox", path="\\Mailbox\\Inbox")]

    monkeypatch.setattr("shovel.services.outlook.outlook_stores", stores)
    monkeypatch.setattr("shovel.services.outlook.outlook_folders", folders)
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
    ) as client:
        assert (await client.get("/api/v1/outlook/stores")).json()["items"][0]["id"] == "store"
        response = await client.get(
            "/api/v1/outlook/folders", params={"store_id": "store", "parent_id": "parent"}
        )
        assert response.json()["items"][0]["id"] == "inbox"
        assert (await client.get("/api/v1/outlook/folders")).status_code == 422
        assert (
            await client.get("/api/v1/outlook/stores", headers={"Origin": "https://evil.invalid"})
        ).status_code == 403


class Process:
    def __init__(self, response: bytes | None) -> None:
        self.stdout = asyncio.StreamReader()
        if response is not None:
            self.stdout.feed_data(response)
        self.stdin = SimpleNamespace(write=lambda _data: None, drain=self.drain, close=lambda: None)
        self.killed = False

    async def drain(self) -> None:
        pass

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


async def test_worker_timeout_and_cancellation_release_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import shovel.services.outlook as module

    processes: list[Process] = []

    async def spawn(*_args: Any, **_kwargs: Any) -> Process:
        process = Process(None)
        processes.append(process)
        return process

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    session = OutlookSession()
    session.timeout = 0.01
    with pytest.raises(OutlookError, match="did not respond"):
        await session.request("stores")
    assert session._process is None
    session.timeout = 60
    task = asyncio.create_task(session.request("stores"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session._process is None


async def test_invalid_worker_response_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import shovel.services.outlook as module

    async def spawn(*_args: Any, **_kwargs: Any) -> Process:
        return Process(b"not-json\n")

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    session = OutlookSession()
    with pytest.raises(OutlookError, match="communication"):
        await session.request("stores")
    assert session._process is None


def test_worker_rechecks_revision_and_never_opens_undiscovered_mail() -> None:
    modified = datetime(2026, 1, 1, tzinfo=UTC)
    message = SimpleNamespace(
        EntryID="entry",
        LastModificationTime=modified,
        Size=100,
        Subject="subject",
        Parent=SimpleNamespace(EntryID="inbox", StoreID="store"),
        DownloadState=1,
        Body="body",
        SenderName="Alice",
        SenderEmailAddress="alice@example.test",
        To="Bob",
        CC="",
        SentOn=modified,
        ReceivedTime=modified,
        ConversationID="thread",
        Attachments=[],
    )
    reader = OutlookReader(SimpleNamespace(GetItemFromID=lambda *_args: message))
    with pytest.raises(ValueError, match="not discovered"):
        reader.handle({"command": "open", "store_id": "store", "entry_id": "entry", "version": "v"})
    reader._config = OutlookConfig.model_validate(CONFIG)
    version = _version("entry", modified, 100)
    reader._authorized[("store", "entry")] = discovered_record(version=version, subject="subject")
    request = {"command": "open", "store_id": "store", "entry_id": "entry", "version": version}
    assert reader.handle(request)["body"] == "body"
    message.Size = 101
    with pytest.raises(ValueError, match="changed after"):
        reader.handle(request)
    message.Size = 100
    message.DownloadState = 0
    with pytest.raises(ValueError, match="headers"):
        reader.handle(request)


def test_worker_batches_mail_tables_and_deduplicates_recursive_folders() -> None:
    modified = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "EntryID": f"entry-{index}",
            "Subject": "subject",
            "LastModificationTime": modified,
            "Size": 100,
            "MessageClass": "IPM.Note",
            "ReceivedTime": modified,
        }
        for index in range(101)
    ]
    rows.append({**rows[0], "EntryID": "appointment", "MessageClass": "IPM.Appointment"})

    class Table:
        Columns = SimpleNamespace(RemoveAll=lambda: None, Add=lambda _name: None)

        def __init__(self) -> None:
            self.index = 0

        @property
        def EndOfTable(self) -> bool:
            return self.index >= len(rows)

        def GetNextRow(self) -> dict[str, Any]:
            row = rows[self.index]
            self.index += 1
            return row

    child = SimpleNamespace(EntryID="child", StoreID="store", FolderPath="\\Inbox\\Child", Folders=[])
    child.GetTable = lambda: SimpleNamespace(EndOfTable=True, Columns=Table.Columns)
    folder = SimpleNamespace(
        EntryID="inbox", StoreID="store", FolderPath="\\Inbox", Folders=[child], GetTable=Table
    )
    namespace = SimpleNamespace(
        GetFolderFromID=lambda identifier, _store: {"inbox": folder, "child": child}[identifier]
    )
    reader = OutlookReader(namespace)
    first = reader.handle(
        {
            "command": "discover",
            "config": {**CONFIG, "folder_ids": ["inbox", "child"], "include_subfolders": True},
        }
    )
    assert len(first["items"]) == 100 and not first["done"]
    second = reader.handle({"command": "next"})
    assert len(second["items"]) == 1 and second["done"]
    assert len(reader._authorized) == 101
    assert all(item["entry_id"] != "appointment" for item in first["items"])
    reader.close()
    assert reader._pending is None and reader.namespace is None and not reader._authorized


async def test_worker_transport_failure_aborts_scan_instead_of_failing_all_mail(
    runtime: Runtime, worker: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = await runtime.resources.create(
        ResourceDraft(name="mail", connector_kind="outlook_local", config=CONFIG)
    )
    await runtime.resources.verify(resource.id)
    original = worker.request

    async def request(command: str, **payload: object) -> dict[str, Any]:
        if command == "open":
            raise OutlookWorkerError("Worker timed out.")
        return await original(command, **payload)

    monkeypatch.setattr(worker, "request", request)
    result = await ScanService(runtime.sessions, runtime.resources).scan(resource.id)
    assert result.state is JobState.FAILED
    assert "timed out" in (result.error_detail or "")
    assert worker.closed


async def test_mail_uses_existing_durable_operation_queue(runtime: Runtime, worker: FakeSession) -> None:
    from shovel.runtime.schemas import ResourceInput, ScanInput

    created = await runtime.operations.create_resource(
        ResourceInput(name="mail", connector_kind="outlook_local", config=CONFIG)
    )
    await runtime.operations.run_once()
    result = await runtime.operations.get(created.id)
    assert result.state == "succeeded", result.error
    scan = await runtime.operations.scan(result.resource_id or "", ScanInput(embed=False))
    await runtime.operations.run_once()
    assert (await runtime.operations.get(scan.id)).state == "succeeded"
    assert worker.closed


async def test_fatal_worker_response_and_spawn_failure_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import shovel.services.outlook as module

    async def spawn(*_args: Any, **_kwargs: Any) -> Process:
        return Process(b'{"error":"Outlook is not installed","fatal":true}\n')

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    session = OutlookSession()
    with pytest.raises(OutlookWorkerError, match="not installed"):
        await session.request("stores")
    assert session._process is None

    async def failed_spawn(*_args: Any, **_kwargs: Any) -> Process:
        raise OSError("spawn failed")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", failed_spawn)
    with pytest.raises(OutlookWorkerError, match="communication"):
        await session.request("stores")
    assert session._process is None


async def test_blocked_worker_is_killed_without_touching_outlook(monkeypatch: pytest.MonkeyPatch) -> None:
    class BlockedProcess(Process):
        async def wait(self) -> int:
            if not self.killed:
                await asyncio.sleep(30)
            return 0

    process = BlockedProcess(None)
    session = OutlookSession()
    monkeypatch.setattr(session, "_process", process)
    await session.aclose()
    assert process.killed and session._process is None


def test_outlook_cli_discovery_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from shovel.cli.commands.resource import app
    from shovel.services.outlook import OutlookFolder, OutlookStore

    async def stores() -> list[OutlookStore]:
        return [OutlookStore(id="store", name="Mailbox")]

    async def folders(store_id: str, parent_id: str | None) -> list[OutlookFolder]:
        assert store_id == "store" and parent_id == "parent"
        return [OutlookFolder(id="inbox", name="Inbox", path="\\Inbox")]

    monkeypatch.setattr("shovel.services.outlook.outlook_stores", stores)
    monkeypatch.setattr("shovel.services.outlook.outlook_folders", folders)
    runner = CliRunner()
    result = runner.invoke(app, ["outlook-stores"])
    assert result.exit_code == 0 and "Mailbox" in result.output
    result = runner.invoke(app, ["outlook-folders", "--store-id", "store", "--parent-id", "parent"])
    assert result.exit_code == 0 and "inbox" in result.output
