from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from shovel.config.performance_config import PerformanceSettings
from shovel.config.settings import AppSettings, load_settings
from shovel.db.model.execution import Job
from shovel.domain.enums import JobState
from shovel.pipeline.embedder import HttpEmbedder
from shovel.pipeline.scan_service import ScanService
from shovel.runtime.hosting import RuntimeWorker
from shovel.runtime.schemas import ScanInput, SearchInput
from shovel.vector.store import init_vector_store
from test_hosted_runtime import add_notes
from test_hosted_runtime import runtime as runtime


def control_scans(monkeypatch, operations):
    started = {operation.job_id: asyncio.Event() for operation in operations}
    release = {operation.job_id: asyncio.Event() for operation in operations}
    active = set()
    peak = [0]
    original = ScanService._process

    async def process(service, **kwargs):
        resource = kwargs["resource"].id
        assert resource not in active, "The same resource must not scan concurrently."
        active.add(resource)
        peak[0] = max(peak[0], len(active))
        started[kwargs["job_id"]].set()
        try:
            await release[kwargs["job_id"]].wait()
            return await original(service, **kwargs)
        finally:
            active.remove(resource)

    monkeypatch.setattr(ScanService, "_process", process)
    return started, release, peak


def start_worker(runtime):
    worker = RuntimeWorker(lambda: runtime)
    worker._runtime = runtime
    return asyncio.create_task(worker.execute_async(runtime.stopping))


async def stop_worker(runtime, task):
    runtime.stopping.set()
    runtime.wake.set()
    await asyncio.wait_for(task, 10)


async def completed(runtime, operations):
    async def wait():
        while True:
            receipts = [await runtime.operations.get(operation.id) for operation in operations]
            if all(receipt.state in {"succeeded", "failed", "cancelled"} for receipt in receipts):
                return receipts
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), 10)


@pytest.mark.parametrize("limit", [1, 2, None])
async def test_scan_jobs_overlap_with_configured_capacity(runtime, tmp_path, monkeypatch, limit):
    if limit is not None:
        runtime.settings.performance.max_concurrent_scans = limit
    capacity = runtime.settings.performance.max_concurrent_scans
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}")
        for index in range(capacity + 1)
    ]
    operations = [await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources]
    started, release, peak = control_scans(monkeypatch, operations)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(
            asyncio.gather(*(started[operation.job_id].wait() for operation in operations[:capacity])), 10
        )
        async with runtime.sessions() as session:
            jobs = list(await session.scalars(select(Job).where(Job.state == JobState.RUNNING)))
        assert len(jobs) == capacity
        assert (await runtime.operations.get(operations[-1].id)).state == "queued"
        release[operations[0].job_id].set()
        await asyncio.wait_for(started[operations[-1].job_id].wait(), 10)
        for event in release.values():
            event.set()
        assert all(receipt.state == "succeeded" for receipt in await completed(runtime, operations))
        assert peak[0] == capacity
    finally:
        await stop_worker(runtime, worker)


async def test_same_resource_waits_without_blocking_other_resources(runtime, tmp_path, monkeypatch):
    first = await add_notes(runtime, tmp_path / "first", name="first")
    second = await add_notes(runtime, tmp_path / "second", name="second")
    operations = [
        await runtime.operations.scan(resource, ScanInput(embed=False, force=True))
        for resource in (first, first, second)
    ]
    started, release, peak = control_scans(monkeypatch, operations)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(
            asyncio.gather(started[operations[0].job_id].wait(), started[operations[2].job_id].wait()), 10
        )
        assert (await runtime.operations.get(operations[1].id)).state == "queued"
        release[operations[0].job_id].set()
        await asyncio.wait_for(started[operations[1].job_id].wait(), 10)
        for event in release.values():
            event.set()
        assert all(receipt.state == "succeeded" for receipt in await completed(runtime, operations))
        assert peak[0] == 2
    finally:
        await stop_worker(runtime, worker)


async def test_scans_arriving_after_an_empty_queue_read_still_run_concurrently(
    runtime, tmp_path, monkeypatch
):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    started = {resource: asyncio.Event() for resource in resources}
    release = asyncio.Event()
    operations = []
    next_queued = runtime.operations.next_queued
    process = ScanService._process

    async def enqueue_after_empty_read(**kwargs):
        candidate = await next_queued(**kwargs)
        if candidate is None and not operations:
            for resource in resources:
                operations.append(await runtime.operations.scan(resource, ScanInput(embed=False)))
        return candidate

    async def blocked_process(service, **kwargs):
        started[kwargs["resource"].id].set()
        await release.wait()
        return await process(service, **kwargs)

    monkeypatch.setattr(runtime.operations, "next_queued", enqueue_after_empty_read)
    monkeypatch.setattr(ScanService, "_process", blocked_process)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 10)
        release.set()
        assert all(receipt.state == "succeeded" for receipt in await completed(runtime, operations))
    finally:
        await stop_worker(runtime, worker)


async def test_non_scan_operation_drains_scans_and_blocks_later_admission(runtime, tmp_path, monkeypatch):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(3)
    ]
    operations = [
        await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources[:2]
    ]
    barrier = await runtime.operations.enqueue("cleanup", {})
    operations.append(await runtime.operations.scan(resources[2], ScanInput(embed=False)))
    started, release, _ = control_scans(monkeypatch, operations)
    barrier_started, barrier_release = asyncio.Event(), asyncio.Event()
    execute = runtime.operations._execute

    async def execute_with_barrier(operation):
        if operation.id == barrier.id:
            receipts = [await runtime.operations.get(scan.id) for scan in operations[:2]]
            assert all(receipt.state == "succeeded" for receipt in receipts)
            barrier_started.set()
            await barrier_release.wait()
            return {}, "succeeded"
        return await execute(operation)

    monkeypatch.setattr(runtime.operations, "_execute", execute_with_barrier)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(
            asyncio.gather(started[operations[0].job_id].wait(), started[operations[1].job_id].wait()), 10
        )
        release[operations[0].job_id].set()
        assert (await completed(runtime, operations[:1]))[0].state == "succeeded"
        assert (await runtime.operations.get(barrier.id)).state == "queued"
        assert (await runtime.operations.get(operations[2].id)).state == "queued"
        release[operations[1].job_id].set()
        await asyncio.wait_for(barrier_started.wait(), 10)
        assert (await runtime.operations.get(operations[2].id)).state == "queued"
        barrier_release.set()
        await asyncio.wait_for(started[operations[2].job_id].wait(), 10)
        release[operations[2].job_id].set()
        assert all(receipt.state == "succeeded" for receipt in await completed(runtime, operations))
    finally:
        barrier_release.set()
        await stop_worker(runtime, worker)


async def test_shutdown_requeues_all_active_scans_with_original_job_ids(runtime, tmp_path, monkeypatch):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    operations = [await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources]
    started, release, _ = control_scans(monkeypatch, operations)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 10)
    finally:
        await stop_worker(runtime, worker)
    for operation in operations:
        receipt = await runtime.operations.get(operation.id)
        assert receipt.state == "queued" and receipt.job_id == operation.job_id
        async with runtime.sessions() as session:
            job = await session.get(Job, operation.job_id)
        assert job.state == JobState.QUEUED and not job.is_cancel_requested
    runtime.stopping.clear()
    for event in release.values():
        event.set()
    worker = start_worker(runtime)
    try:
        receipts = await completed(runtime, operations)
        assert all(receipt.state == "succeeded" and receipt.attempts == 2 for receipt in receipts)
    finally:
        await stop_worker(runtime, worker)


async def test_cancelling_one_scan_does_not_cancel_another(runtime, tmp_path, monkeypatch):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    operations = [await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources]
    started, release, _ = control_scans(monkeypatch, operations)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 10)
        await runtime.operations.cancel_job(operations[0].job_id)
        release[operations[0].job_id].set()
        assert (await completed(runtime, operations[:1]))[0].state == "cancelled"
        assert (await runtime.operations.get(operations[1].id)).state == "running"
        release[operations[1].job_id].set()
        assert (await completed(runtime, operations[1:]))[0].state == "succeeded"
    finally:
        await stop_worker(runtime, worker)


async def test_parallel_embeddings_persist_and_retrieve_both_resources(runtime, tmp_path, monkeypatch):
    runtime.settings.embedding.provider = "azure_openai"
    runtime.settings.embedding.base_url = "https://embedding.example.invalid"
    runtime.settings.embedding.api_version = "2025-04-01-preview"
    runtime.settings.embedding.api_key = SecretStr("synthetic-key")
    runtime.settings.embedding.model = "synthetic-embedding"
    runtime.settings.embedding.dimensions = 4
    init_vector_store(runtime.settings.zvec)
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    both_requests = asyncio.Event()
    requests = []

    async def response(request):
        payload = json.loads(request.content)
        assert payload["dimensions"] == 4
        requests.append(payload)
        if len(requests) == 2:
            both_requests.set()
        await asyncio.wait_for(both_requests.wait(), 10)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [0.1, 0.2, 0.3, 0.4]}
                    for index in range(len(payload["input"]))
                ]
            },
        )

    def client(embedder):
        if embedder._client is None:
            embedder._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        return embedder._client

    monkeypatch.setattr(HttpEmbedder, "_ensure_client", client)
    operations = [await runtime.operations.scan(resource, ScanInput()) for resource in resources]
    worker = start_worker(runtime)
    try:
        receipts = await completed(runtime, operations)
        assert all(receipt.state == "succeeded" for receipt in receipts), [row.error for row in receipts]
        assert both_requests.is_set()
        result = await runtime.retrieval.search(SearchInput(query="queue", mode="vector", limit=10))
        assert {hit["resource_id"] for hit in result["items"]} == set(resources)
    finally:
        await stop_worker(runtime, worker)


async def test_failed_scan_does_not_fail_the_worker_or_other_scans(runtime, tmp_path, monkeypatch):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    operations = [await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources]
    started, release, _ = control_scans(monkeypatch, operations)
    process = ScanService._process

    async def fail_one(service, **kwargs):
        if kwargs["job_id"] == operations[0].job_id:
            await started[operations[1].job_id].wait()
            raise RuntimeError("synthetic-scan-failure")
        return await process(service, **kwargs)

    monkeypatch.setattr(ScanService, "_process", fail_one)
    worker = start_worker(runtime)
    try:
        assert (await completed(runtime, operations[:1]))[0].state == "failed"
        assert runtime.worker_state == "running"
        assert (await runtime.operations.get(operations[1].id)).state == "running"
        release[operations[1].job_id].set()
        assert (await completed(runtime, operations[1:]))[0].state == "succeeded"
    finally:
        await stop_worker(runtime, worker)


async def test_dispatch_failure_is_visible_and_drains_other_scans(runtime, tmp_path, monkeypatch):
    resources = [
        await add_notes(runtime, tmp_path / f"notes-{index}", name=f"notes-{index}") for index in range(2)
    ]
    operations = [await runtime.operations.scan(resource, ScanInput(embed=False)) for resource in resources]
    started, release, _ = control_scans(monkeypatch, operations)
    run_once = runtime.operations.run_once

    async def fail_dispatch(*, identifier=None):
        result = await run_once(identifier=identifier)
        if identifier == operations[0].id:
            raise RuntimeError("synthetic-dispatch-failure")
        return result

    monkeypatch.setattr(runtime.operations, "run_once", fail_dispatch)
    worker = start_worker(runtime)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 10)
        release[operations[0].job_id].set()
        with pytest.raises(RuntimeError, match="synthetic-dispatch-failure"):
            await asyncio.wait_for(worker, 10)
        assert runtime.stopping.is_set() and runtime.worker_state == "failed"
        assert (await runtime.operations.get(operations[1].id)).state == "queued"
    finally:
        if not worker.done():
            await stop_worker(runtime, worker)


def test_scan_capacity_can_be_configured_and_environment_overridden(tmp_path, monkeypatch):
    config = tmp_path / "settings.toml"
    config.write_text("[performance]\nmax_concurrent_scans = 2\n", encoding="utf-8")
    assert AppSettings.load(config).performance.max_concurrent_scans == 2
    monkeypatch.setenv("SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS", "3")
    assert load_settings(config).performance.max_concurrent_scans == 3
    for invalid in (0, -1, 65):
        with pytest.raises(ValidationError):
            PerformanceSettings(max_concurrent_scans=invalid)
