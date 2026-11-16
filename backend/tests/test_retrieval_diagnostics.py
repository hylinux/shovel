from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from typer.testing import CliRunner

from shovel.cli.app import app
from shovel.config.embedding_config import EmbeddingSettings
from shovel.db.model.config import Resource
from shovel.db.model.content import Chunk, Document
from shovel.domain.enums import DocumentPipeState, Granularity, ResourceState, VectorState
from shovel.pipeline.sink import VectorRecord, ZvecChunkSink
from shovel.runtime.client import diagnose_retrieval, diagnostic_configuration_id, workspace_id
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.locking import endpoint_file
from shovel.runtime.schemas import RetrievalDiagnosticInput
from shovel.runtime.service import Runtime
from shovel.vector.store import init_vector_store
from test_hosted_runtime import api as api
from test_hosted_runtime import index_notes
from test_hosted_runtime import runtime as runtime


def diagnostic_request(runtime: Runtime, **kwargs: Any) -> RetrievalDiagnosticInput:
    return RetrievalDiagnosticInput(
        query="Shovel private-query",
        workspace_id=workspace_id(runtime.settings),
        configuration_id=diagnostic_configuration_id(runtime.settings),
        **kwargs,
    )


async def connected() -> bool:
    return False


@pytest_asyncio.fixture
async def indexed_runtime(runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    await index_notes(runtime, tmp_path / "private-source")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="not-a-live-provider")

    async def embed(_query: str) -> tuple[list[float], str]:
        return [1.0, 0.0, 0.0, 0.0], "private-model-version"

    monkeypatch.setattr(runtime.retrieval, "embed_query", embed)
    init_vector_store(runtime.settings.zvec)
    async with runtime.sessions() as session:
        chunk = await session.scalar(select(Chunk).where(Chunk.granularity == Granularity.CHUNK).limit(1))
        assert chunk is not None
        chunk.vector_state, chunk.model_version = VectorState.INDEXED, "private-model-version"
        await session.execute(
            update(Document).values(
                pipe_state=DocumentPipeState.INDEXED,
                indexed_hash=Document.content_hash,
            )
        )
        await session.commit()
        record = VectorRecord(
            id=chunk.id,
            vector=[1.0, 0.0, 0.0, 0.0],
            text=chunk.text_,
            resource_id=chunk.resource_id,
            doc_uri=chunk.doc_uri,
            granularity="chunk",
            chunk_index=0,
            model_version="private-model-version",
        )
    await (await runtime.sink()).upsert([record])
    return runtime


async def test_real_sdk_diagnostic_is_serial_sanitized_and_uses_actual_metadata(
    indexed_runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runtime = indexed_runtime
    sink = await runtime.sink()
    search, snapshot = sink.search, sink.diagnostic_snapshot
    events: list[str] = []
    vectors: list[int] = []
    embedded: list[str] = []

    async def embedding(query: str) -> tuple[list[float], str]:
        embedded.append(query)
        return [1.0, 0.0, 0.0, 0.0], "private-model-version"

    async def query(vector: list[float], **kwargs: Any) -> Any:
        events.append("search")
        vectors.append(id(vector))
        assert kwargs["limit"] == 60 and kwargs["resource_ids"]
        return await search(vector, **kwargs)

    async def metadata() -> dict[str, Any]:
        events.append("metadata")
        return await snapshot()

    monkeypatch.setattr(sink, "search", query)
    monkeypatch.setattr(sink, "diagnostic_snapshot", metadata)
    monkeypatch.setattr(runtime.retrieval, "embed_query", embedding)
    request = diagnostic_request(runtime, repetitions=2, comparison_queries=["Shovel different-query"])
    with development_logging():
        response = await api.post("/api/v1/diagnostics/retrieval", json=request.model_dump())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    report = response.json()
    assert report["status"] == "complete" and len(report["samples"]) == 6
    assert events == ["search"] * 6 + ["metadata"]
    assert embedded == ["Shovel private-query", "Shovel different-query"]
    assert len(set(vectors[:3])) == len(set(vectors[3:])) == 1
    assert report["collection"]["dimension"] == 4 and report["collection"]["doc_count"] == 1
    assert "HNSW" in report["collection"]["vector_index"]
    assert all("INVERT" in index for index in report["collection"]["filter_indexes"].values())
    assert report["collection"]["segment_count_available"] is False
    assert report["sqlite"]["current_model_indexed_chunks"] == 1
    assert report["summaries"][0]["repeat_count"] == 2
    for sample in report["samples"]:
        native = sample["native"]
        assert native["native_topk"] == 60 and native["filter_chars"] > 0
        assert native["total_ms"] >= native["native_ms"] >= native["query_ms"]
        if sample["kind"] == "native_repeat":
            assert sample["validated_candidate_count"] == 1
        else:
            assert sample["final_hit_count"] == 1
    assert set(report["samples"][0]["phases_ms"]) >= {
        "embedding_ms",
        "index_ready_ms",
        "vector_search_ms",
        "revalidation_ms",
        "log_ms",
        "total_ms",
    }
    logged = capsys.readouterr().out
    assert "index.query.call.completed" in logged and "operation=search" in logged
    for secret in (
        "private-query",
        "different-query",
        "private-model-version",
        str(tmp_path),
        "private-source",
    ):
        assert secret not in response.text and secret not in logged
    another = await runtime.retrieval_diagnostics.run(
        diagnostic_request(runtime, repetitions=0),
        disconnected=connected,
    )
    assert report["host_id"] == another["host_id"] and report["diagnostic_id"] != another["diagnostic_id"]


async def test_default_scope_can_freeze_more_resources_than_explicit_request_limit(
    indexed_runtime: Runtime,
) -> None:
    runtime = indexed_runtime
    async with runtime.sessions() as session:
        session.add_all(
            [
                Resource(
                    id=f"resource-{i}",
                    name=f"Resource {i}",
                    connector_kind="local_fs",
                    state=ResourceState.PAUSED,
                    config={},
                )
                for i in range(200)
            ]
        )
        await session.commit()
    report = await runtime.retrieval_diagnostics.run(
        diagnostic_request(runtime, repetitions=0),
        disconnected=connected,
    )
    assert report["status"] == "complete" and report["resource_count"] == 201
    assert report["samples"][0]["native"]["resource_count"] == 201


@pytest.mark.parametrize("binding", ["workspace_id", "configuration_id"])
async def test_api_rejects_mismatched_host_binding_before_diagnostic_work(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A mismatched request must not run a diagnostic.")

    monkeypatch.setattr(runtime.retrieval_diagnostics, "run", forbidden)
    payload = diagnostic_request(runtime).model_dump()
    payload[binding] = "0" * 64
    assert (await api.post("/api/v1/diagnostics/retrieval", json=payload)).status_code == 409


async def test_budget_stops_additional_samples_without_cancelling_the_first(
    indexed_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = indexed_runtime
    now = [0.0]
    monkeypatch.setattr("shovel.runtime.retrieval_diagnostics.perf_counter", lambda: now[0])
    sink = await runtime.sink()
    search = sink.search
    calls = 0

    async def query(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        found = await search(*args, **kwargs)
        now[0] = 2.0
        return found

    monkeypatch.setattr(sink, "search", query)
    report = await runtime.retrieval_diagnostics.run(
        diagnostic_request(runtime, budget_seconds=1),
        disconnected=connected,
    )
    assert report["status"] == "partial" and report["stop_reason"] == "budget_exhausted"
    assert calls == len(report["samples"]) == 1
    assert report["collection"] is not None


async def test_revocation_prevents_later_native_samples(
    indexed_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = indexed_runtime
    sink = await runtime.sink()
    search = sink.search
    calls = 0

    async def revoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        matches = await search(*args, **kwargs)
        async with runtime.sessions() as session:
            await session.execute(update(Resource).values(state=ResourceState.REVOKED))
            await session.commit()
        return matches

    monkeypatch.setattr(sink, "search", revoke)
    with pytest.raises(RuntimeFailure, match="所选资源"):
        await runtime.retrieval_diagnostics.run(diagnostic_request(runtime), disconnected=connected)
    assert calls == 1


async def test_cancellation_drains_native_io_before_releasing_diagnostic_gate(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await index_notes(runtime, tmp_path / "notes")
    runtime.settings.embedding = EmbeddingSettings(provider="openai", model="fixture")
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    closed: list[bool] = []

    def native_query(**_kwargs: Any) -> list[Any]:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        assert not closed
        return []

    sink = ZvecChunkSink(SimpleNamespace(query=native_query, close=lambda: closed.append(True)))

    async def open_sink() -> ZvecChunkSink:
        return sink

    async def embed(_query: str) -> tuple[list[float], str]:
        return [1.0, 0.0, 0.0, 0.0], "test"

    monkeypatch.setattr(runtime, "sink", open_sink)
    monkeypatch.setattr(runtime, "ready_sink", lambda: sink)
    monkeypatch.setattr(runtime.retrieval, "embed_query", embed)
    task = asyncio.create_task(
        runtime.retrieval_diagnostics.run(
            diagnostic_request(runtime),
            disconnected=connected,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(RuntimeFailure, match="already running"):
            await runtime.retrieval_diagnostics.run(diagnostic_request(runtime), disconnected=connected)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await sink.aclose()
    assert closed == [True] and not runtime.retrieval_diagnostics._lock.locked()


@pytest.mark.parametrize("stop", ["disconnect", "shutdown"])
async def test_terminal_conditions_stop_before_any_embedding(
    indexed_runtime: Runtime, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    runtime = indexed_runtime

    async def forbidden(_query: str) -> Any:
        pytest.fail("No embedding work is allowed after disconnect or shutdown.")

    async def disconnected() -> bool:
        return stop == "disconnect"

    monkeypatch.setattr(runtime.retrieval, "embed_query", forbidden)
    if stop == "shutdown":
        runtime.stopping.set()
    report = await runtime.retrieval_diagnostics.run(
        diagnostic_request(runtime),
        disconnected=disconnected,
    )
    assert report["status"] == "partial" and not report["samples"] and report["collection"] is None


async def test_cli_client_reuses_matching_host_and_rejects_config_mismatch(
    indexed_runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = indexed_runtime
    metadata = endpoint_file(runtime.settings)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({"url": "http://127.0.0.1:9999"}), encoding="utf-8")
    actual_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return actual_client(transport=api._transport, **kwargs)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Diagnostics must never create a one-shot Host.")

    monkeypatch.setattr("shovel.runtime.client.httpx.AsyncClient", client)
    monkeypatch.setattr("shovel.runtime.client.build_cli_host", forbidden)
    request = diagnostic_request(runtime, repetitions=0)
    report = await diagnose_retrieval(runtime.settings, request)
    assert report["host_id"] == runtime.retrieval_diagnostics.host_id
    other = runtime.settings.model_copy(deep=True)
    other.embedding.model = "different-config"
    mismatch = request.model_copy(update={"configuration_id": diagnostic_configuration_id(other)})
    with pytest.raises(RuntimeFailure, match="configuration does not match"):
        await diagnose_retrieval(other, mismatch)


async def test_missing_host_never_falls_back_to_local_runtime(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A missing Host must not create another Runtime.")

    monkeypatch.setattr("shovel.runtime.client.build_cli_host", forbidden)
    with pytest.raises(RuntimeFailure, match="already running"):
        await diagnose_retrieval(runtime.settings, diagnostic_request(runtime))


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_cli_writes_new_sanitized_report_and_returns_partial_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from shovel.cli.commands import diagnose
    from test_hosted_runtime import settings_at

    settings = settings_at(tmp_path)
    payloads: list[RetrievalDiagnosticInput] = []

    async def run(_settings: Any, request: RetrievalDiagnosticInput) -> dict[str, Any]:
        payloads.append(request)
        return {"report_version": 1, "status": status, "samples": []}

    monkeypatch.setattr(diagnose, "load_settings", lambda _path: settings)
    monkeypatch.setattr(diagnose, "diagnose_retrieval", run)
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "diagnose",
            "retrieval",
            "private-query",
            "--resource",
            "resource",
            "--compare-query",
            "other",
            "--repetitions",
            "2",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == (0 if status == "complete" else 1), result.output
    assert payloads[0].resource_ids == ["resource"] and payloads[0].comparison_queries == ["other"]
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == status
    again = CliRunner().invoke(
        app,
        [
            "diagnose",
            "retrieval",
            "private-query",
            "--output",
            str(output),
        ],
    )
    assert again.exit_code != 0 and "already exists" in again.output and len(payloads) == 1
