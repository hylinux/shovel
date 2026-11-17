from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from shovel.cli.app import app
from shovel.cli.context import cli_context
from shovel.config.embedding_config import EmbeddingSettings
from shovel.db.database import init_database
from shovel.db.model.config import Resource, ResourceState
from shovel.db.model.runtime import Operation
from shovel.exceptions.pipeline import KnowledgeIndexUnavailableError
from shovel.pipeline.sink import VectorRecord, ZvecChunkSink
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import IndexOptimizeInput
from shovel.runtime.service import Runtime
from shovel.vector.store import init_vector_store
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at


def record(identifier: str = "first") -> VectorRecord:
    return VectorRecord(
        id=identifier,
        vector=[1.0, 0.0, 0.0, 0.0],
        text="fixture",
        resource_id="fixture",
        doc_uri="fixture",
        granularity="chunk",
        chunk_index=0,
        model_version="fixture",
    )


async def seeded(runtime: Runtime) -> ZvecChunkSink:
    init_vector_store(runtime.settings.zvec)
    sink = await runtime.sink()
    await sink.upsert([record()])
    return sink


def block_native(
    sink: ZvecChunkSink, method: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, threading.Event]:
    # Invoke the real SDK after the barrier; only scheduling is controlled.
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    cls = type(sink._collection)
    original = getattr(cls, method)

    def blocked(collection: Any, *args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise TimeoutError("Native test barrier was not released.")
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(cls, method, blocked)
    return entered, release


async def query(sink: ZvecChunkSink) -> list[str]:
    return [
        identifier
        for identifier, _score in await sink.search(
            [1.0, 0.0, 0.0, 0.0], resource_ids=["fixture"], model_version="fixture", limit=1
        )
    ]


async def test_accepted_query_write_burst_drains_before_close_without_executor_starvation(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = await seeded(runtime)
    entered, release = block_native(sink, "query", monkeypatch)
    first = asyncio.create_task(query(sink))
    pending: list[asyncio.Task[Any]] = []
    closes: list[asyncio.Task[None]] = []
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pending = [asyncio.create_task(query(sink)) for _ in range(64)]
        pending.append(asyncio.create_task(sink.upsert([record("second")])))
        await asyncio.sleep(0)
        assert sink.admission_snapshot()["pending_io"] == 66
        cancelled_calls: list[bool] = []
        cancelled = asyncio.create_task(sink._io(lambda: cancelled_calls.append(True)))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not cancelled_calls and sink.admission_snapshot()["pending_io"] == 66
        assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 1)
        closes = [asyncio.create_task(sink.aclose()) for _ in range(2)]
        await asyncio.sleep(0)
        with pytest.raises(KnowledgeIndexUnavailableError):
            await query(sink)
        assert not any(task.done() for task in closes)
    finally:
        release.set()
        assert await first == ["first"]
        if pending:
            results = await asyncio.gather(*pending)
            assert results[:-1] == [["first"]] * 64 and results[-1] == 1
        await asyncio.gather(*closes)
    assert sink.admission_snapshot()["admission"] == "closed"
    await sink.aclose()


async def test_maintenance_closes_admission_then_drains_accepted_io(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = await seeded(runtime)
    entered, release = block_native(sink, "query", monkeypatch)
    first = asyncio.create_task(query(sink))
    dispatched = AsyncMock()
    maintenance = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        write = asyncio.create_task(sink.upsert([record("second")]))
        await asyncio.sleep(0)
        maintenance = asyncio.create_task(sink.optimize(on_dispatch=dispatched))
        await asyncio.sleep(0)
        assert sink.admission_snapshot()["maintenance"] == "draining"
        dispatched.assert_not_awaited()
        with pytest.raises(KnowledgeIndexUnavailableError, match="maintenance"):
            await query(sink)
    finally:
        release.set()
        await first
        assert await write == 1
        if maintenance is not None:
            result = await maintenance
            assert result["before"]["doc_count"] == result["after"]["doc_count"] == 2
    dispatched.assert_awaited_once()
    assert sink.admission_snapshot()["ann_complete"] is True
    await sink.upsert([record("third")])
    assert sink.admission_snapshot()["ann_complete"] is None


async def test_repeated_cancellation_and_close_wait_for_dispatched_native_maintenance(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = await seeded(runtime)
    entered, release = block_native(sink, "optimize", monkeypatch)
    maintenance = asyncio.create_task(sink.optimize(on_dispatch=AsyncMock()))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        maintenance.cancel()
        await asyncio.sleep(0)
        maintenance.cancel()
        closing = asyncio.create_task(sink.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not maintenance.done() and not closing.done()
        assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 1)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await maintenance
        if closing is not None:
            with pytest.raises(asyncio.CancelledError):
                await closing
    assert sink.admission_snapshot()["admission"] == "closed"


async def test_runtime_close_drains_inflight_open_and_prevents_reopening(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.pipeline.sink import open_chunk_sink

    init_vector_store(runtime.settings.zvec)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    opened: list[ZvecChunkSink] = []

    def blocked(path: str, **kwargs: Any) -> ZvecChunkSink:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        sink = open_chunk_sink(path, **kwargs)
        opened.append(sink)
        return sink

    monkeypatch.setattr(runtime._knowledge_worker, "open", blocked)
    opening = asyncio.create_task(runtime.sink())
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        opening.cancel()
        await asyncio.sleep(0)
        opening.cancel()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeFailure, match="closing"):
            await runtime.sink()
        assert not opening.done() and not closing.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        if closing is not None:
            await closing
    assert len(opened) == 1 and opened[0].admission_snapshot()["admission"] == "closed"
    assert runtime.ready_sink() is None
    with pytest.raises(RuntimeFailure, match="closing"):
        await runtime.sink()


async def test_api_acknowledgement_idempotency_exclusion_and_passive_status(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    before = (await api.get("/api/v1/index/status")).json()
    assert before["admission"] == "unopened" and before["ann_complete"] is None
    for body in (
        {},
        {"maintenance_window": False},
        {"maintenance_window": 1},
        {"maintenance_window": "true"},
        {"maintenance_window": True, "concurrency": 3},
    ):
        assert (await api.post("/api/v1/index/optimize", json=body)).status_code == 422
    body = {"maintenance_window": True, "concurrency": 1}
    headers = {"Idempotency-Key": "manual-maintenance"}
    first = await api.post("/api/v1/index/optimize", json=body, headers=headers)
    assert first.status_code == 202
    repeated = await api.post("/api/v1/index/optimize", json=body, headers=headers)
    assert repeated.json()["id"] == first.json()["id"]
    changed = await api.post("/api/v1/index/optimize", json={**body, "concurrency": 2}, headers=headers)
    assert changed.status_code == 409
    assert (await api.post("/api/v1/index/optimize", json=body)).status_code == 409
    assert runtime.ready_sink() is None
    assert (await api.get("/api/v1/status")).json()["knowledge_index"] == before


async def test_maintenance_waiting_for_open_rejects_new_admission_and_shutdown_prevents_dispatch(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shovel.pipeline.sink import open_chunk_sink

    init_vector_store(runtime.settings.zvec)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def blocked(path: str, **kwargs: Any) -> ZvecChunkSink:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return open_chunk_sink(path, **kwargs)

    monkeypatch.setattr(runtime._knowledge_worker, "open", blocked)
    operation = await runtime.operations.optimize_index(IndexOptimizeInput(maintenance_window=True))
    worker = asyncio.create_task(runtime.operations.run_once(identifier=operation.id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        status = runtime.knowledge_index_status()
        assert status["admission"] == "maintenance" and status["maintenance"] == "opening"
        assert status["operation_id"] == operation.id
        with pytest.raises(KnowledgeIndexUnavailableError, match="maintenance"):
            await runtime.sink()
        runtime.stopping.set()
    finally:
        release.set()
        await worker
    receipt = await runtime.operations.get(operation.id)
    assert receipt.state == "failed" and receipt.result["phase"] == "draining"
    assert not receipt.result.get("reconciliation_required")
    assert runtime.knowledge_index_status()["operation_id"] is None


async def test_operation_records_native_completion_despite_cancellation_and_preserves_memory(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = await seeded(runtime)
    entered, release = block_native(sink, "optimize", monkeypatch)
    async with runtime.sessions() as session:
        session.add(
            Resource(
                id="fixture",
                name="fixture",
                connector_kind="local_fs",
                state=ResourceState.ACTIVE,
                config={"root_path": "fixture"},
            )
        )
        await session.commit()
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="fixture")
    embed = AsyncMock(side_effect=AssertionError("Maintenance must reject before paid embedding."))
    monkeypatch.setattr(runtime.retrieval, "embed_query", embed)
    operation = await runtime.operations.optimize_index(IndexOptimizeInput(maintenance_window=True))
    worker = asyncio.create_task(runtime.operations.run_once(identifier=operation.id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        receipt = await runtime.operations.get(operation.id)
        assert receipt.state == "running" and receipt.result["phase"] == "dispatched"
        status = (await api.get("/api/v1/index/status")).json()
        assert status["admission"] == "maintenance" and status["ann_complete"] is None
        response = await api.post("/api/v1/search", json={"query": "fixture", "mode": "vector"})
        assert response.status_code == 503
        embed.assert_not_awaited()
        assert (
            await api.post("/api/v1/search", json={"query": "fixture", "mode": "keyword"})
        ).status_code == 200
        assert "memory" in (await api.get("/api/v1/status")).json()
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(0)
        assert not worker.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
    receipt = await runtime.operations.get(operation.id)
    assert receipt.state == "succeeded" and receipt.attempts == 1
    assert receipt.result["phase"] == "completed" and receipt.result["cancel_requested"]
    assert receipt.result["before"]["doc_count"] == receipt.result["after"]["doc_count"] == 1
    assert (await api.get("/api/v1/index/status")).json()["ann_complete"] is True


async def test_native_failure_is_explicit_and_never_blindly_retried(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = await seeded(runtime)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("native-maintenance-failure")

    monkeypatch.setattr(type(sink._collection), "optimize", fail)
    operation = await runtime.operations.optimize_index(IndexOptimizeInput(maintenance_window=True))
    await runtime.operations.run_once(identifier=operation.id)
    receipt = await runtime.operations.get(operation.id)
    assert receipt.state == "failed" and "native-maintenance-failure" in (receipt.error or "")
    assert receipt.result["phase"] == "dispatched" and receipt.result["reconciliation_required"]
    assert (await api.post(f"/api/v1/operations/{operation.id}/retry")).status_code == 409
    assert sink.admission_snapshot()["admission"] == "open"


@pytest.mark.parametrize(
    ("phase", "expected"), [("draining", "queued"), ("dispatched", "failed"), ("completed", "succeeded")]
)
async def test_restart_recovers_checkpoint_without_replaying_native_effects(
    runtime: Runtime, phase: str, expected: str
) -> None:
    async with runtime.sessions() as session:
        row = Operation(kind="index.optimize", state="running", result={"phase": phase})
        session.add(row)
        await session.commit()
        identifier = row.id
    await runtime.operations.recover()
    receipt = await runtime.operations.get(identifier)
    assert receipt.state == expected and receipt.result["phase"] == phase
    assert bool(receipt.result.get("reconciliation_required")) == (phase == "dispatched")
    assert runtime.ready_sink() is None


async def test_frozen_identity_prevents_maintenance_of_changed_store(runtime: Runtime) -> None:
    operation = await runtime.operations.optimize_index(IndexOptimizeInput(maintenance_window=True))
    runtime.settings.zvec.dim += 1
    await runtime.operations.run_once(identifier=operation.id)
    receipt = await runtime.operations.get(operation.id)
    assert receipt.state == "failed" and "identity changed" in (receipt.error or "")
    assert receipt.result.get("phase") != "dispatched" and runtime.ready_sink() is None


def test_cli_requires_explicit_window_and_uses_local_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_at(tmp_path)
    config = tmp_path / "settings.toml"
    settings.save(config)
    monkeypatch.setattr(cli_context, "get_default_config_file", lambda: config)
    runner = CliRunner()
    missing_ack = runner.invoke(app, ["index", "optimize"])
    assert missing_ack.exit_code == 1 and "maintenance window" in missing_ack.output
    assert runner.invoke(app, ["index", "optimize", "--yes", "--concurrency", "3"]).exit_code == 2
    init_database(settings.database.url)
    init_vector_store(settings.zvec)
    result = runner.invoke(app, ["index", "optimize", "--yes", "--concurrency", "1"])
    assert result.exit_code == 0, result.output
    assert '"kind": "index.optimize"' in result.output and '"phase": "completed"' in result.output
