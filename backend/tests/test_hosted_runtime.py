from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text, update

from shovel.config.settings import AppSettings
from shovel.db.database import create_async_database_engine, create_async_session_factory, init_database
from shovel.db.migrations import upgrade_database
from shovel.db.model.config import Resource
from shovel.db.model.content import Document
from shovel.db.model.execution import Job
from shovel.db.model.retrieval import DeletionOutbox, SchemaMigration
from shovel.db.model.runtime import ChatMessage, Conversation, Operation
from shovel.domain.enums import JobState, ScanMode
from shovel.pipeline.embedder import build_embedder
from shovel.pipeline.scan_service import ScanService
from shovel.runtime.api import configure_api
from shovel.runtime.chat import HttpChatModel
from shovel.runtime.client import execute_command
from shovel.runtime.diagnostics import development_logging, trace, trace_context
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import build_cli_host, build_host, mount_frontend, resolve_runtime
from shovel.runtime.locking import RuntimeLock
from shovel.runtime.memory import MemoryService
from shovel.runtime.requests import request_operation_id
from shovel.runtime.schemas import MessageInput, ResourceInput, ScanInput, SearchInput
from shovel.runtime.service import Runtime, create_runtime
from shovel.services.resource_service import ResourceDraft, ResourceService
from shovel.vector.store import init_vector_store


def settings_at(path: Path) -> AppSettings:
    return AppSettings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{(path / 'runtime.db').as_posix()}"},
            "zvec": {"knowledge_path": str(path / "vectors"), "dim": 4, "log_dir": str(path / "logs")},
            "model": {
                "model_provider_type": "openai",
                "base_endpoint": "http://model.invalid/v1",
                "model_name": "test-model",
                "security_key": "test-private-key",
            },
        }
    )


@pytest_asyncio.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = settings_at(tmp_path)
    context = create_async_database_engine(settings.database.url)
    engine = await anext(context)
    sessions = create_async_session_factory(engine)
    managed = create_runtime(settings, engine, sessions, ResourceService(sessions))
    try:
        yield await anext(managed)
    finally:
        await managed.aclose()
        await context.aclose()


@pytest_asyncio.fixture
async def api(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
    ) as client:
        yield client


async def add_notes(runtime: Runtime, path: Path, name: str = "notes") -> str:
    path.mkdir()
    (path / "notes.txt").write_text(
        "Shovel uses a durable background queue. 本地知识库支持恢复索引。", encoding="utf-8"
    )
    operation = await runtime.operations.create_resource(
        ResourceInput(
            name=name,
            connector_kind="local_fs",
            config={"root_path": str(path)},
        )
    )
    assert await runtime.operations.run_once()
    result = await runtime.operations.get(operation.id)
    assert result.state == "succeeded", result.error
    assert result.resource_id is not None
    return result.resource_id


async def index_notes(runtime: Runtime, path: Path, name: str = "notes") -> str:
    resource_id = await add_notes(runtime, path, name)
    operation = await runtime.operations.scan(resource_id, ScanInput(embed=False))
    assert await runtime.operations.run_once()
    assert (await runtime.operations.get(operation.id)).state == "succeeded"
    return resource_id


@pytest.mark.parametrize("development", [False, True])
async def test_dev_diagnostics_trace_runtime_without_logging_payloads(
    runtime: Runtime,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    development: bool,
) -> None:
    directory = tmp_path / "private-source-path"
    directory.mkdir()
    (directory / "note.txt").write_text("Shovel private-document-body", encoding="utf-8")
    runtime.settings.log_level = "ERROR" if development else "DEBUG"
    runtime.settings.memory.embedder.model = "test-memory"
    runtime.memory._memory = FakeMemory()
    runtime.chat.model._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "private-model-answer [1]"}}]}
            )
        )
    )
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings, development=development)
    with development_logging() if development else nullcontext():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
        ) as client:
            created = await client.post(
                "/api/v1/resources",
                json={
                    "name": "private-resource-name",
                    "connector_kind": "local_fs",
                    "config": {"root_path": str(directory)},
                },
                headers={"Authorization": "Bearer private-header", "Idempotency-Key": "private-key"},
            )
            assert created.status_code == 202
            assert await runtime.operations.run_once()
            resource_id = created.json()["resource_id"]
            scan = await client.post(f"/api/v1/resources/{resource_id}/scans", json={"embed": False})
            assert scan.status_code == 202
            assert await runtime.operations.run_once()
            assert (await runtime.operations.get(scan.json()["id"])).state == "succeeded"
            conversation = (await client.post("/api/v1/conversations", json={})).json()["id"]
            answer = await client.post(
                f"/api/v1/conversations/{conversation}/messages?private-query-value",
                json={"content": "Shovel private-user-message", "memory_enabled": True},
            )
            assert answer.status_code == 200, answer.text
            assert answer.json()["citations"]
            rejected = await client.post("/api/v1/credentials", json={"secret": "private-secret", "extra": 1})
            assert rejected.status_code == 422
    trace("outside.dev.scope")
    output = capsys.readouterr().out
    events = (
        "http.started",
        "http.completed",
        "operation.queued",
        "operation.started",
        "operation.completed",
        "resource.verify.started",
        "resource.verify.completed",
        "scan.discovery.completed",
        "document.fetch",
        "document.parse",
        "document.chunk",
        "document.persist",
        "document.completed",
        "scan.completed",
        "retrieval.completed",
        "model.completed",
        "memory.completed",
        "chat.completed",
    )
    if development:
        for event in events:
            assert event in output
        assert f"job_id={scan.json()['job_id']}" in output
        assert "elapsed_ms=" in output and "status=422" in output
        assert "/api/v1/resources/{ref}/scans" in output
        request_id = created.headers["x-shovel-request-id"]
        queued = next(line for line in output.splitlines() if "operation.queued" in line)
        assert request_id in queued
    else:
        assert not any(event in output for event in events)
        assert "x-shovel-request-id" not in created.headers
    assert "private-" not in output
    assert "User prefers Chinese" not in output
    assert "test-private-key" not in output
    assert "outside.dev.scope" not in output


@pytest.mark.parametrize("fail", [False, True])
def test_dev_entry_scopes_logs_without_affecting_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fail: bool
) -> None:
    from shovel.cli.commands import run as run_module

    modes: list[bool] = []

    async def serve() -> None:
        trace("production.must.not.trace")

    def host(_settings: AppSettings, *, development: bool, frontend: Path | None = None) -> Any:
        modes.append(development)
        return SimpleNamespace(run_async=serve, exit_code=0)

    async def supervise(*_args: Any, **_kwargs: Any) -> int:
        trace("development.entry.trace")
        if fail:
            raise RuntimeError("test shutdown")
        return 0

    monkeypatch.setattr(run_module, "_load_settings_or_exit", lambda: settings_at(tmp_path))
    monkeypatch.setattr(run_module, "_resolve_frontend_dir", lambda _override: tmp_path)
    monkeypatch.setattr(run_module, "build_host", host)
    monkeypatch.setattr(run_module, "_supervise", supervise)
    if fail:
        with pytest.raises(RuntimeError, match="test shutdown"):
            asyncio.run(run_module._run_dev_async(tmp_path))
    else:
        asyncio.run(run_module._run_dev_async(tmp_path))
    assert "development.entry.trace" in capsys.readouterr().out
    run_module.run_prd(frontend_dir=tmp_path)
    trace("after.dev.must.not.trace")
    assert "must.not.trace" not in capsys.readouterr().out
    assert modes == [True, False]


async def test_concurrent_dev_requests_keep_separate_log_contexts(
    runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings, development=True)
    with development_logging():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
        ) as client:
            responses = await asyncio.gather(client.get("/api/v1/status"), client.get("/api/v1/connectors"))
        with trace_context(component="test"):
            trace("after.requests")
    lines = capsys.readouterr().out.splitlines()
    identifiers = [response.headers["x-shovel-request-id"] for response in responses]
    assert len(set(identifiers)) == 2
    for identifier in identifiers:
        assert len([line for line in lines if "http." in line and identifier in line]) == 2
    assert not any(identifier in lines[-1] for identifier in identifiers)


async def test_profile_api_retains_scope_and_rejects_changes_during_scans(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    resource_id = await add_notes(runtime, tmp_path / "profiles")
    path = f"/api/v1/resources/{resource_id}/profiles/text"
    saved = await api.put(path, json={"include": ["*.txt"]}, headers={"Idempotency-Key": "profile-v1"})
    assert saved.status_code == 202
    await runtime.operations.run_once()
    assert (await runtime.operations.get(saved.json()["id"])).state == "succeeded"
    assert len((await api.get(f"/api/v1/resources/{resource_id}/profiles")).json()["items"]) == 2
    assert (await api.put(path, json={"pipeline": {"parser": {"invalid": 2}}})).status_code == 422
    scan = await runtime.operations.scan(resource_id, ScanInput(profile_name="text", embed=False))
    changed = await api.put(path, json={"include": ["*.md"]})
    await runtime.operations.run_once()
    await runtime.operations.run_once()
    assert (await runtime.operations.get(scan.id)).state == "succeeded"
    assert (await runtime.operations.get(changed.json()["id"])).state == "succeeded"
    # A running Job cannot have its configuration changed by another writer.
    async with runtime.sessions() as session:
        job = await session.get(Job, scan.job_id)
        assert job is not None
        job.state = JobState.RUNNING
        await session.commit()
    blocked = await api.put(path, json={"exclude": ["*.txt"]})
    await runtime.operations.run_once()
    assert (await runtime.operations.get(blocked.json()["id"])).state == "failed"
    async with runtime.sessions() as session:
        job = await session.get(Job, scan.job_id)
        assert job is not None
        job.state = JobState.SUCCEEDED
        await session.commit()
    removed = await api.delete(path)
    await runtime.operations.run_once()
    failed_delete = await runtime.operations.get(removed.json()["id"])
    assert failed_delete.state == "failed" and failed_delete.result["profile_deleted"]
    recreated = await api.put(path, json={"include": ["*.txt"]})
    await runtime.operations.run_once()
    replacement = await runtime.operations.get(recreated.json()["id"])
    assert replacement.state == "succeeded"
    assert replacement.result["id"] != failed_delete.result["profile_id"]
    init_vector_store(runtime.settings.zvec)
    await runtime.operations.retry(failed_delete.id)
    await runtime.operations.run_once()
    assert (await runtime.operations.get(removed.json()["id"])).state == "succeeded"
    replay = await api.put(path, json={"include": ["*.txt"]}, headers={"Idempotency-Key": "profile-v1"})
    assert replay.json()["id"] == saved.json()["id"]
    assert len((await api.get(f"/api/v1/resources/{resource_id}/profiles")).json()["items"]) == 2


async def test_conversation_paging_rename_delete_and_busy_guard(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    async with runtime.sessions() as session:
        session.add_all(
            [
                ChatMessage(
                    conversation_id=identifier,
                    role="user",
                    content=str(sequence),
                    sequence=sequence,
                )
                for sequence in range(1, 106)
            ]
        )
        await session.commit()
    latest = (await api.get(f"/api/v1/conversations/{identifier}/messages?limit=100")).json()
    assert len(latest["items"]) == 100
    assert latest["items"][0]["sequence"] == 6 and latest["next_before"] == 6
    older = (await api.get(f"/api/v1/conversations/{identifier}/messages?before=6")).json()
    assert [row["sequence"] for row in older["items"]] == [1, 2, 3, 4, 5]
    assert (
        await api.patch(f"/api/v1/conversations/{identifier}", json={"title": "renamed"})
    ).status_code == 200
    lock = runtime.chat._locks.setdefault(identifier, asyncio.Lock())
    async with lock:
        assert (await api.delete(f"/api/v1/conversations/{identifier}")).status_code == 409
    assert (await api.delete(f"/api/v1/conversations/{identifier}")).status_code == 200
    async with runtime.sessions() as session:
        assert not list(
            await session.scalars(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == identifier,
                )
            )
        )


async def test_history_prune_preserves_request_identity_and_active_work(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    from shovel.db.base import now_ts
    from shovel.db.model.retrieval import RetrievalLog

    past = now_ts() - 90 * 86400
    async with runtime.sessions() as session:
        session.add_all(
            [
                Operation(id="old-unkeyed", kind="cleanup", state="succeeded", finished_at=past),
                Operation(
                    id="old-keyed",
                    kind="cleanup",
                    state="succeeded",
                    request_key="retained",
                    finished_at=past,
                ),
                Operation(id="old-active", kind="cleanup", state="running", finished_at=past),
                RetrievalLog(query_text="expired", queried_at=past),
            ]
        )
        await session.commit()
    response = await api.post("/api/v1/history/prune", json={"keep_days": 30})
    # Do not let the deliberately running fixture be picked as new work.
    await runtime.operations.run_once()
    receipt = await runtime.operations.get(response.json()["id"])
    assert receipt.state == "succeeded"
    assert receipt.result["operations"] == 1 and receipt.result["retrievals"] == 1
    async with runtime.sessions() as session:
        assert await session.get(Operation, "old-unkeyed") is None
        assert await session.get(Operation, "old-keyed") is not None
        assert await session.get(Operation, "old-active") is not None


@pytest.mark.parametrize(
    "status,content",
    [
        (202, b"not-json"),
        (202, b"[]"),
        (202, b"{}"),
        (503, b'{"error":"unavailable"}'),
    ],
)
async def test_uncertain_cli_responses_always_preserve_recovery_id(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content: bytes,
) -> None:
    actual_client = httpx.AsyncClient

    async def remote(_settings: AppSettings, _client: httpx.AsyncClient) -> str:
        return "http://127.0.0.1:9999"

    monkeypatch.setattr("shovel.runtime.client._remote", remote)
    monkeypatch.setattr(
        "shovel.runtime.client.httpx.AsyncClient",
        lambda **kwargs: actual_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, content=content)),
            **kwargs,
        ),
    )
    identifier = request_operation_id("uncertain-response")
    with pytest.raises(RuntimeFailure, match=identifier):
        await execute_command(runtime.settings, "cleanup", {}, request_key="uncertain-response")


async def test_legacy_operation_migration_preserves_rows_and_is_repeatable(tmp_path: Path) -> None:
    context = create_async_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    engine = await anext(context)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""CREATE TABLE operation(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                resource_id TEXT, job_id TEXT, payload TEXT NOT NULL, result TEXT NOT NULL,
                error TEXT, queued_at INTEGER NOT NULL, started_at INTEGER,
                finished_at INTEGER, attempts INTEGER NOT NULL)""")
            )
            await connection.execute(
                text("""INSERT INTO operation VALUES
                ('legacy', 'cleanup', 'succeeded', NULL, NULL, '{}', :result,
                 NULL, 1, 1, 2, 1)"""),
                {"result": json.dumps({"completed": 1})},
            )
        await upgrade_database(engine)
        await upgrade_database(engine)
        async with create_async_session_factory(engine)() as session:
            row = await session.get(Operation, "legacy")
            assert row is not None and row.result == {"completed": 1} and row.request_key is None
            assert list(await session.scalars(select(SchemaMigration.version))) == [1, 2, 3]
    finally:
        await context.aclose()


async def test_worker_failure_is_visible_and_requests_host_shutdown(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shovel.runtime.hosting import RuntimeWorker

    async def fail() -> bool:
        raise RuntimeError("injected-worker-crash")

    monkeypatch.setattr(runtime.operations, "run_once", fail)
    worker = RuntimeWorker(lambda: runtime)
    worker._runtime = runtime
    stopping = asyncio.Event()
    with pytest.raises(RuntimeError, match="injected-worker-crash"):
        await worker.execute_async(stopping)
    assert stopping.is_set() and runtime.worker_state == "failed"
    assert (await api.get("/api/v1/status")).status_code == 503


async def test_resource_scan_search_and_cleanup_api(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "hello.md").write_text(
        "# Shovel\nHost 管理本地知识库。 Background queue persists.", encoding="utf-8"
    )
    response = await api.post(
        "/api/v1/resources",
        json={
            "name": "documents",
            "connector_kind": "local_fs",
            "config": {"root_path": str(root)},
        },
    )
    assert response.status_code == 202
    operation = response.json()
    assert operation["state"] == "queued"
    assert (await api.get("/api/v1/resources")).json()["items"] == []
    await runtime.operations.run_once()
    rows = (await api.get("/api/v1/resources")).json()["items"]
    assert rows[0]["state"] == "active"
    resource_id = rows[0]["id"]
    response = await api.post(f"/api/v1/resources/{resource_id}/scans", json={"embed": False})
    queued = response.json()
    assert response.status_code == 202
    async with runtime.sessions() as session:
        job = await session.get(Job, queued["job_id"])
        assert job and job.state == JobState.QUEUED
    await runtime.operations.run_once()
    job_response = (await api.get(f"/api/v1/jobs/{queued['job_id']}")).json()
    assert job_response["state"] == "succeeded"
    assert job_response["discovered"] == 1
    assert job_response["progress"] == 1.0
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
    for query in ("background", "本地知识库", "知识"):
        response = await api.post("/api/v1/search", json={"query": query, "mode": "keyword"})
        assert response.status_code == 200, response.text
        results = response.json()
        assert results["mode"] == "keyword"
        assert results["items"][0]["resource_id"] == resource_id
        assert "Host" in results["items"][0]["text"]
    events = (await api.get(f"/api/v1/jobs/{queued['job_id']}/events")).json()["items"]
    assert isinstance(events, list)
    init_vector_store(runtime.settings.zvec)
    response = await api.delete(f"/api/v1/resources/{resource_id}")
    deletion = response.json()
    await runtime.operations.run_once()
    receipt = (await api.get(f"/api/v1/operations/{deletion['id']}")).json()
    assert receipt["state"] == "succeeded", receipt
    assert receipt["result"]["metadata_deleted"]
    assert receipt["result"]["cleanup_complete"]
    assert (await api.get(f"/api/v1/resources/{resource_id}")).status_code == 404
    assert (await api.post("/api/v1/search", json={"query": "Shovel"})).json()["items"] == []
    assert (await api.get(f"/api/v1/operations/{deletion['id']}")).status_code == 200


async def test_idempotent_requests_replay_receipts_and_reject_changed_intent(
    runtime: Runtime, api: httpx.AsyncClient, tmp_path: Path
) -> None:
    root = tmp_path / "idempotent"
    root.mkdir()
    (root / "one.txt").write_text("持久操作只能执行一次。", encoding="utf-8")
    payload = {"name": "idempotent", "connector_kind": "local_fs", "config": {"root_path": str(root)}}
    headers = {"Idempotency-Key": "create-once"}
    first, second = await asyncio.gather(
        api.post("/api/v1/resources", json=payload, headers=headers),
        api.post("/api/v1/resources", json=payload, headers=headers),
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] == request_operation_id("create-once")
    await runtime.operations.run_once()
    resource_id = first.json()["resource_id"]
    repeated = await api.post("/api/v1/resources", json=payload, headers=headers)
    assert repeated.json()["state"] == "succeeded"
    conflict = await api.post("/api/v1/resources", json={**payload, "name": "different"}, headers=headers)
    assert conflict.status_code == 409
    scan_headers = {"Idempotency-Key": "scan-once"}
    path = f"/api/v1/resources/{resource_id}/scans"
    first, second = await asyncio.gather(
        api.post(path, json={"embed": False}, headers=scan_headers),
        api.post(path, json={"embed": False}, headers=scan_headers),
    )
    assert first.json()["job_id"] == second.json()["job_id"]
    await runtime.operations.run_once()
    init_vector_store(runtime.settings.zvec)
    delete_headers = {"Idempotency-Key": "delete-once"}
    deleted = await api.delete(f"/api/v1/resources/{resource_id}", headers=delete_headers)
    await runtime.operations.run_once()
    replayed = await api.delete(f"/api/v1/resources/{resource_id}", headers=delete_headers)
    assert replayed.status_code == 202
    assert replayed.json()["id"] == deleted.json()["id"]
    assert replayed.json()["result"]["cleanup_complete"]
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 3


async def test_cli_lost_submission_response_can_be_queried_and_resubmitted_safely(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shovel.runtime.client as client_module

    resource_id = await add_notes(runtime, tmp_path / "reconnect")
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings)
    transport = httpx.ASGITransport(app=app)
    original_client = httpx.AsyncClient
    lost = True

    async def remote(*_args: Any) -> str:
        return "http://127.0.0.1:9999"

    async def deliver(request: httpx.Request) -> httpx.Response:
        nonlocal lost
        response = await transport.handle_async_request(request)
        if lost and request.method == "POST" and request.url.path.endswith("/scans"):
            lost = False
            await response.aclose()
            raise httpx.ReadError("submission response was lost", request=request)
        return response

    monkeypatch.setattr(client_module, "_remote", remote)
    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(deliver)),
    )
    with pytest.raises(RuntimeFailure, match=request_operation_id("lost-response")):
        await execute_command(
            runtime.settings, "scan", {"embed": False}, ref=resource_id, request_key="lost-response"
        )
    await runtime.operations.run_once()
    recovered = await client_module.manage_operation(
        runtime.settings, request_operation_id("lost-response"), wait=True
    )
    repeated = await execute_command(
        runtime.settings, "scan", {"embed": False}, ref=resource_id, request_key="lost-response"
    )
    assert repeated["id"] == recovered["id"]
    assert repeated["state"] == "succeeded"
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_queue_recovery_preserves_job_and_request(runtime: Runtime, tmp_path: Path) -> None:
    resource_id = await add_notes(runtime, tmp_path / "notes")
    queued = await runtime.operations.scan(
        resource_id,
        ScanInput(embed=False, force=True, target_ids=["notes.txt"], limit=1),
    )
    async with runtime.sessions() as session:
        operation = await session.get(Operation, queued.id)
        job = await session.get(Job, queued.job_id)
        assert operation and job
        operation.state = "running"
        job.state = JobState.RUNNING
        await session.commit()
    await runtime.operations.recover()
    restored = await runtime.operations.get(queued.id)
    assert restored.state == "queued"
    assert restored.payload["force"]
    assert restored.payload["limit"] == 1
    assert restored.payload["target_ids"] == ["notes.txt"]
    await runtime.operations.run_once()
    completed = await runtime.operations.get(queued.id)
    assert completed.state == "succeeded", completed.error
    assert completed.job_id == queued.job_id
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_creation_recovery_does_not_duplicate_resource_or_initial_scan(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.txt").write_text("Recoverable local knowledge", encoding="utf-8")
    operation = await runtime.operations.create_resource(
        ResourceInput(
            name="recover-create",
            connector_kind="local_fs",
            config={"root_path": str(root)},
            scan=True,
            embed=False,
        )
    )
    # Simulate a crash after execution committed its effects but before the receipt.
    await runtime.operations._execute(operation)
    async with runtime.sessions() as session:
        await session.execute(update(Operation).where(Operation.id == operation.id).values(state="running"))
        await session.commit()
    await runtime.operations.recover()
    while await runtime.operations.run_once():
        pass
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Resource)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_cancelled_queued_job_is_not_executed(runtime: Runtime, tmp_path: Path) -> None:
    identifier = await add_notes(runtime, tmp_path / "notes")
    operation = await runtime.operations.scan(identifier, ScanInput(embed=False))
    assert operation.job_id
    job = await runtime.operations.cancel_job(operation.job_id)
    assert job.state == JobState.CANCELLED
    assert (await runtime.operations.get(operation.id)).state == "cancelled"
    assert not await runtime.operations.run_once()


async def test_shutdown_requeues_same_scan_without_claiming_user_cancellation(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    identifier = await add_notes(runtime, tmp_path / "notes")
    operation = await runtime.operations.scan(identifier, ScanInput(embed=False))
    runtime.stopping.set()
    await runtime.operations.run_once()
    receipt = await runtime.operations.get(operation.id)
    assert receipt.state == "queued"
    async with runtime.sessions() as session:
        job = await session.get(Job, operation.job_id)
        assert job and job.state == JobState.QUEUED
        assert not job.is_cancel_requested
    runtime.stopping.clear()
    await runtime.operations.run_once()
    assert (await runtime.operations.get(operation.id)).state == "succeeded"


async def test_failed_cleanup_has_durable_receipt_and_can_retry(runtime: Runtime, tmp_path: Path) -> None:
    identifier = await index_notes(runtime, tmp_path / "notes")
    operation = await runtime.operations.resource_action(identifier, "delete")
    await runtime.operations.run_once()
    failed = await runtime.operations.get(operation.id)
    assert failed.state == "failed"
    assert failed.result["metadata_deleted"]
    async with runtime.sessions() as session:
        assert await session.get(Resource, identifier) is None
        assert await session.scalar(select(DeletionOutbox.id).where(DeletionOutbox.done_at.is_(None)))
    init_vector_store(runtime.settings.zvec)
    await runtime.operations.retry(operation.id)
    await runtime.operations.run_once()
    assert (await runtime.operations.get(operation.id)).result["cleanup_complete"]


async def test_scope_filtering_and_revocation(runtime: Runtime, tmp_path: Path) -> None:
    first = await index_notes(runtime, tmp_path / "one", "one")
    second = await index_notes(runtime, tmp_path / "two", "two")
    result = await runtime.retrieval.search(SearchInput(query="Shovel", resource_ids=[first]))
    assert {item["resource_id"] for item in result["items"]} == {first}
    await runtime.resources.revoke(second)
    result = await runtime.retrieval.search(SearchInput(query="Shovel"))
    assert {item["resource_id"] for item in result["items"]} == {first}
    with pytest.raises(RuntimeFailure, match="已吊销"):
        await runtime.retrieval.search(SearchInput(query="Shovel", resource_ids=[second]))


async def test_keyword_index_tracks_reindex_and_deletion(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "notes"
    identifier = await index_notes(runtime, root)
    (root / "notes.txt").write_text("Completely replaced content, newword.", encoding="utf-8")
    operation = await runtime.operations.scan(identifier, ScanInput(embed=False, force=True))
    await runtime.operations.run_once()
    assert (await runtime.operations.get(operation.id)).state == "succeeded"
    assert (await runtime.retrieval.search(SearchInput(query="durable")))["items"] == []
    assert (await runtime.retrieval.search(SearchInput(query="newword")))["items"]


async def test_real_vector_search_reads_authoritative_scoped_chunks(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.settings.embedding.provider = "openai"
    runtime.settings.embedding.model = "fake-embedding"
    from pydantic import SecretStr

    runtime.settings.embedding.api_key = SecretStr("test")
    runtime.settings.embedding.base_url = "http://embedding.invalid/v1"
    init_vector_store(runtime.settings.zvec)
    identifier = await add_notes(runtime, tmp_path / "notes")

    def response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [0.1, 0.2, 0.3, 0.4]}
                    for index, _ in enumerate(payload["input"])
                ]
            },
        )

    from shovel.pipeline.embedder import HttpEmbedder

    original = HttpEmbedder._ensure_client

    def client(self: HttpEmbedder) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        return self._client

    monkeypatch.setattr(HttpEmbedder, "_ensure_client", client)
    embedder = build_embedder(runtime.settings.embedding, expected_dimension=4)
    try:
        await ScanService(
            runtime.sessions, runtime.resources, embedder=embedder, sink=await runtime.sink()
        ).scan(identifier)
    finally:
        await embedder.aclose()
    result = await runtime.retrieval.search(
        SearchInput(query="queue", mode="vector", resource_ids=[identifier])
    )
    assert result["items"], result
    assert result["items"][0]["resource_id"] == identifier
    assert "durable" in result["items"][0]["text"]
    monkeypatch.setattr(HttpEmbedder, "_ensure_client", original)


async def test_chat_citations_persist_without_memory(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = await index_notes(runtime, tmp_path / "notes")
    captured: list[dict[str, Any]] = []

    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {runtime.settings.model.security_key}"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Shovel uses a durable queue [1]."}}]}
        )

    runtime.chat.model._client = httpx.AsyncClient(transport=httpx.MockTransport(response))

    async def no_memory(_query: str) -> list[dict[str, Any]]:
        raise AssertionError("Memory must not run without opt-in")

    monkeypatch.setattr(runtime.memory, "search", no_memory)
    conversation = (await api.post("/api/v1/conversations", json={})).json()
    response = await api.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={
            "content": "How does the Shovel queue work?",
            "resource_ids": [identifier],
            "search_mode": "keyword",
            "memory_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["state"] == "completed"
    assert answer["citations"][0]["resource_id"] == identifier
    assert answer["citations"][0]["index"] == 1
    assert "durable" in captured[0]["messages"][-2]["content"]
    assert captured[0]["messages"][-1]["role"] == "user"
    history = (await api.get(f"/api/v1/conversations/{conversation['id']}/messages")).json()["items"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[-1]["citations"] == answer["citations"]


async def test_invalid_model_citations_are_explicit_persisted_failure(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    identifier = await index_notes(runtime, tmp_path / "notes")
    runtime.chat.model._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"choices": [{"message": {"content": "Invented [99]."}}]}
            )
        )
    )
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.commit()
    with pytest.raises(RuntimeFailure, match="引用编号"):
        await runtime.chat.send(
            conversation.id, MessageInput(content="Shovel", resource_ids=[identifier], search_mode="keyword")
        )
    history = await runtime.chat.messages(conversation.id)
    assert history[-1].state == "failed"
    assert not history[-1].citations


class FakeMemory:
    def __init__(self) -> None:
        self.rows = [
            {"id": "owned", "memory": "User prefers Chinese", "user_id": "local-user", "agent_id": "shovel"}
        ]
        self.messages: list[dict[str, str]] = []
        self.threads: set[int] = set()
        self.closed = False
        self.vector_store = SimpleNamespace(client=SimpleNamespace(close=lambda: None))

    def search(self, query: str, *, filters: dict[str, str], top_k: int) -> dict[str, Any]:
        self.threads.add(threading.get_ident())
        assert filters == {"user_id": "local-user", "agent_id": "shovel"}
        return {"results": self.rows[:top_k]}

    def get_all(self, *, filters: dict[str, str], top_k: int) -> dict[str, Any]:
        self.threads.add(threading.get_ident())
        assert filters["agent_id"] == "shovel"
        return {"results": self.rows[:top_k]}

    def add(self, messages: list[dict[str, str]], **scope: str) -> None:
        self.threads.add(threading.get_ident())
        self.messages = messages
        assert scope["user_id"] == "local-user"

    def get(self, identifier: str) -> dict[str, Any] | None:
        return next((row for row in self.rows if row["id"] == identifier), None)

    def delete(self, identifier: str) -> None:
        self.rows = [row for row in self.rows if row["id"] != identifier]

    def close(self) -> None:
        self.closed = True


async def test_memory_opt_in_recall_write_list_forget(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    identifier = await index_notes(runtime, tmp_path / "notes")
    fake = FakeMemory()
    runtime.settings.memory.embedder.model = "memory-test"
    runtime.memory._memory = fake
    prompts: list[dict[str, Any]] = []

    def response(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "持久队列 [1]。"}}]})

    runtime.chat.model._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    conversation = (await api.post("/api/v1/conversations")).json()
    response = await api.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={
            "content": "Tell me about Shovel",
            "resource_ids": [identifier],
            "memory_enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    assert "User prefers Chinese" in prompts[0]["messages"][-2]["content"]
    assert fake.messages == [{"role": "user", "content": "Tell me about Shovel"}]
    assert len(fake.threads) == 1
    assert (await api.get("/api/v1/memories")).json()["items"][0]["id"] == "owned"
    assert (await api.delete("/api/v1/memories/owned")).json()["deleted"]
    assert (await api.get("/api/v1/memories")).json()["items"] == []
    fake.rows = [{"id": "foreign", "memory": "other", "user_id": "other", "agent_id": "shovel"}]
    assert (await api.delete("/api/v1/memories/foreign")).status_code == 404


async def test_local_api_protection_and_safe_errors(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/status", headers={"Host": "evil.example"})).status_code == 403
    assert (await api.get("/api/v1/status", headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await api.get("/api/v1/status", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await api.get("/api/v1/status", headers={"Origin": "http://localhost:3000"})).status_code == 200
    assert (await api.post("/api/v1/search", json={"query": " "})).status_code == 422
    result = await api.post(
        "/api/v1/resources",
        json={
            "name": "bad",
            "connector_kind": "local_fs",
            "config": {"password": "do-not-echo-this"},
        },
    )
    assert result.status_code == 422
    assert "do-not-echo-this" not in result.text
    assert (await api.get("/api/v1/does-not-exist")).status_code == 404


async def test_static_frontend_does_not_capture_api_errors(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<html>Shovel workbench</html>", encoding="utf-8")
    app = FastAPI()
    configure_api(app, lambda: runtime, runtime.settings)
    mount_frontend(app, directory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
    ) as client:
        assert "Shovel workbench" in (await client.get("/")).text
        assert "Shovel workbench" in (await client.get("/conversation")).text
        response = await client.get("/api/v1/missing")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "workbench" not in response.text
        assert (await client.get("/missing.js")).status_code == 404


async def test_existing_database_upgrade_and_fts_backfill(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_database(settings.database.url)
    context = create_async_database_engine(settings.database.url)
    engine = await anext(context)
    try:
        sessions = create_async_session_factory(engine)
        root = tmp_path / "legacy"
        root.mkdir()
        (root / "old.txt").write_text("Legacy corpus supports migration.", encoding="utf-8")
        resources = ResourceService(sessions)
        resource = await resources.create(
            ResourceDraft(name="legacy", connector_kind="local_fs", config={"root_path": str(root)})
        )
        await resources.verify(resource.id)
        await ScanService(sessions, resources).scan(resource.id)
        await upgrade_database(engine)
        await upgrade_database(engine)
        async with create_async_session_factory(engine)() as session:
            assert list(await session.scalars(select(SchemaMigration.version))) == [1, 2, 3]
            assert await session.scalar(text("SELECT count(*) FROM chunk_search")) == 1
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM chunk_search WHERE chunk_search MATCH '\"Legacy\"'")
                )
                == 1
            )
    finally:
        await context.aclose()


def test_one_writer_across_database_and_vector_identities(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with RuntimeLock(settings):
        with pytest.raises(RuntimeFailure, match="已有"), RuntimeLock(settings):
            pass
        other = settings.model_copy(deep=True)
        other.database.url = f"sqlite+aiosqlite:///{(tmp_path / 'other.db').as_posix()}"
        with pytest.raises(RuntimeFailure, match="已有"), RuntimeLock(other):
            pass
    with RuntimeLock(settings):
        pass


async def test_one_shot_host_lifecycle_and_cli_submission(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    # The Host does not require model or embedding setup to manage resources.
    settings.model.model_name = ""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "text.txt").write_text("Offline CLI background operations", encoding="utf-8")
    receipt = await execute_command(
        settings,
        "resource.create",
        {
            "name": "offline",
            "connector_kind": "local_fs",
            "config": {"root_path": str(root)},
        },
    )
    assert receipt["state"] == "succeeded"
    result = await execute_command(settings, "scan", {"embed": False}, ref="offline")
    assert result["state"] == "succeeded"
    assert result["result"]["discovered"] == 1
    host, provider = build_cli_host(settings)
    await host.start_async()
    try:
        runtime = await resolve_runtime(provider)
        assert (await runtime.retrieval.search(SearchInput(query="Offline")))["items"]
    finally:
        host.lifetime.stop_application()
        await host.stop_async()


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("openai", "https://api.openai.com/v1/chat/completions"),
        ("ollama", "http://127.0.0.1:11434/api/chat"),
        ("deepseek", "https://api.deepseek.com/v1/chat/completions"),
    ],
)
def test_chat_model_provider_endpoints(provider: str, expected: str) -> None:
    from shovel.config.model_settings import DefaultAgentModelSettings

    settings = DefaultAgentModelSettings.model_validate(
        {
            "model_provider_type": provider,
            "model_name": "test",
        }
    )
    assert HttpChatModel(settings)._request([{"role": "user", "content": "hello"}])[0] == expected


async def test_completed_job_receipt_recovers_without_reexecution(runtime: Runtime, tmp_path: Path) -> None:
    resource_id = await add_notes(runtime, tmp_path / "notes")
    queued = await runtime.operations.scan(resource_id, ScanInput(embed=False))
    await runtime.operations.run_once()
    completed = await runtime.operations.get(queued.id)
    async with runtime.sessions() as session:
        await session.execute(
            update(Operation).where(Operation.id == queued.id).values(state="running", result={})
        )
        await session.commit()
    await runtime.operations.recover()
    restored = await runtime.operations.get(queued.id)
    assert restored.state == "succeeded"
    assert restored.result["job_id"] == queued.job_id
    assert restored.result["chunks_written"] == completed.result["chunks_written"]
    assert restored.result["parsed"] == 1
    assert restored.attempts == completed.attempts
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.kind == "scan", Operation.state == "queued")
            )
            == 0
        )


async def test_replayed_full_sweep_rebuilds_observed_set(runtime: Runtime, tmp_path: Path) -> None:
    resource_id = await index_notes(runtime, tmp_path / "notes")
    queued = await runtime.operations.scan(resource_id, ScanInput(embed=False, mode=ScanMode.FULL_SWEEP))
    async with runtime.sessions() as session:
        await session.execute(
            update(Document).where(Document.resource_id == resource_id).values(last_seen_run=queued.job_id)
        )
        await session.execute(update(Job).where(Job.id == queued.job_id).values(state=JobState.RUNNING))
        await session.execute(update(Operation).where(Operation.id == queued.id).values(state="running"))
        await session.commit()
    (tmp_path / "notes" / "notes.txt").unlink()
    await runtime.operations.recover()
    await runtime.operations.run_once()
    restored = await runtime.operations.get(queued.id)
    assert restored.state == "succeeded"
    assert restored.result["tombstoned"] == 1
    assert restored.job_id == queued.job_id
    assert (await runtime.retrieval.search(SearchInput(query="Shovel")))["items"] == []


async def test_changed_source_scope_hides_old_knowledge_and_resets_watermark(
    runtime: Runtime, tmp_path: Path
) -> None:
    resource_id = await index_notes(runtime, tmp_path / "notes")
    assert (await runtime.retrieval.search(SearchInput(query="Shovel")))["items"]
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    entry = replacement / "new.txt"
    entry.write_text("Replacement library has different knowledge.", encoding="utf-8")
    previous_mtime = (tmp_path / "notes" / "notes.txt").stat().st_mtime - 3600
    os.utime(entry, (previous_mtime, previous_mtime))
    changed = await runtime.operations.resource_action(
        resource_id, "update", {"config": {"root_path": str(replacement)}}
    )
    await runtime.operations.run_once()
    assert (await runtime.operations.get(changed.id)).state == "succeeded"
    await runtime.operations.resource_action(resource_id, "verify")
    await runtime.operations.run_once()
    assert (await runtime.retrieval.search(SearchInput(query="Shovel")))["items"] == []
    await runtime.operations.scan(resource_id, ScanInput(embed=False))
    await runtime.operations.run_once()
    assert (await runtime.retrieval.search(SearchInput(query="Replacement")))["items"]
    assert (await runtime.retrieval.search(SearchInput(query="Shovel")))["items"] == []


async def test_context_budget_counts_escaped_evidence_and_bounds_history(
    runtime: Runtime, tmp_path: Path
) -> None:
    resource_id = await index_notes(runtime, tmp_path / "notes")
    runtime.settings.model.max_input_chars = 2000
    prompts: list[list[dict[str, str]]] = []

    async def search(_request: SearchInput) -> dict[str, Any]:
        return {
            "items": [
                {
                    "chunk_id": "chunk",
                    "resource_id": resource_id,
                    "resource_name": "notes",
                    "doc_uri": "notes.txt",
                    "title": "Budget",
                    "score": 1.0,
                    "text": ('Shovel "\\\\\n' * 2000),
                }
            ]
        }

    async def complete(messages: list[dict[str, str]]) -> str:
        prompts.append(messages)
        return "Bounded local evidence [1]."

    from unittest.mock import patch

    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.commit()
    with (
        patch.object(runtime.retrieval, "search", search),
        patch.object(runtime.chat.model, "complete", complete),
    ):
        for _ in range(3):
            answer = await runtime.chat.send(conversation.id, MessageInput(content="Shovel"))
            assert answer.citations
        assert all(sum(len(message["content"]) for message in prompt) <= 2000 for prompt in prompts)
        evidence = json.loads(prompts[-1][-2]["content"].split("\n", 1)[1])
        assert answer.citations[0]["text"] == evidence["sources"][0]["text"]
        with pytest.raises(RuntimeFailure, match="上下文预算"):
            await runtime.chat.send(conversation.id, MessageInput(content="x" * 1900))
        assert (await runtime.chat.messages(conversation.id))[-1].state == "failed"


async def test_real_mem0_local_qdrant_recall_persistence_and_forget(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    from mem0.utils.factory import EmbedderFactory, LlmFactory

    class Embedder:
        def embed(self, _text: str, _action: str | None = None) -> list[float]:
            return [1.0, 0.0, 0.0, 0.0]

        def embed_batch(self, texts: list[str], _action: str | None = None) -> list[list[float]]:
            return [self.embed(value) for value in texts]

    class Llm:
        def generate_response(self, **_kwargs: Any) -> str:
            return json.dumps(
                {
                    "facts": ["User prefers tea."],
                    "memory": [{"id": "0", "text": "User prefers tea.", "event": "ADD"}],
                }
            )

    monkeypatch.setattr(EmbedderFactory, "create", lambda *_args, **_kwargs: Embedder())
    monkeypatch.setattr(LlmFactory, "create", lambda *_args, **_kwargs: Llm())
    runtime.settings.memory.dim = 4
    runtime.settings.memory.embedder.model = "test-memory"
    runtime.settings.memory.qdrant.path = str(tmp_path / "memory")
    runtime.settings.memory.history_db_path = str(tmp_path / "history.db")
    from shovel.memory.store import init_memory_store

    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    info = await asyncio.to_thread(init_memory_store, runtime.settings.memory)
    assert info.created
    assert os.environ["MEM0_TELEMETRY"] == "false"
    await runtime.memory.remember("I prefer tea.")
    rows = await runtime.memory.list()
    assert len(rows) == 1
    identifier = rows[0]["id"]
    assert (await runtime.memory.search("tea"))[0]["id"] == identifier
    await runtime.memory.close()
    runtime.memory = MemoryService(runtime.settings)
    assert (await runtime.memory.list())[0]["id"] == identifier
    await runtime.memory.delete(identifier)
    assert await runtime.memory.list() == []


@pytest.mark.parametrize("development", [False, True])
async def test_real_web_host_static_api_and_online_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], development: bool
) -> None:
    settings = settings_at(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        settings.http_port = probe.getsockname()[1]
    settings.dev_http_port = settings.http_port
    settings.http_host = "127.0.0.1"
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<html>Hosted Shovel</html>", encoding="utf-8")
    root = tmp_path / "notes"
    root.mkdir()
    (root / "host.txt").write_text("Online CLI uses the shared background queue.", encoding="utf-8")
    with development_logging() if development else nullcontext():
        host = build_host(settings, development=development, frontend=directory)
        await host.start_async()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.http_port}") as client:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            response = await client.get("/api/v1/status")
                            if response.status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.05)
                assert ("x-shovel-request-id" in response.headers) == development
                assert "Hosted Shovel" in (await client.get("/")).text
                assert (await client.get("/api/missing")).status_code == 404
                created = await execute_command(
                    settings,
                    "resource.create",
                    {
                        "name": "online",
                        "connector_kind": "local_fs",
                        "config": {"root_path": str(root)},
                    },
                )
                assert created["state"] == "succeeded"
                scanned = await execute_command(settings, "scan", {"embed": False}, ref="online")
                assert scanned["state"] == "succeeded"
                assert scanned["result"]["parsed"] == 1
                result = await client.post("/api/v1/search", json={"query": "shared", "mode": "keyword"})
                assert result.json()["items"][0]["resource_name"] == "online"
                assert len((await client.get("/api/v1/operations")).json()["items"]) == 2
        finally:
            host.lifetime.stop_application()
            await host.stop_async()
    output = capsys.readouterr().out
    for event in ("runtime.ready", "worker.started", "document.parse", "http.completed", "runtime.stopped"):
        assert (event in output) == development
