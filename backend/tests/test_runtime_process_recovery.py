from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import zvec
from sqlalchemy import func, select

from shovel.config.settings import AppSettings
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.model.content import Chunk, Document
from shovel.db.model.execution import Job
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.model.runtime import Operation
from shovel.domain.enums import DocumentLifecycleState, Granularity, ScanMode
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import VectorRecord, ZvecChunkSink
from shovel.runtime.client import manage_operation
from shovel.runtime.hosting import build_cli_host, resolve_runtime
from shovel.runtime.locking import RuntimeLock
from shovel.runtime.operations import TERMINAL, OperationService
from shovel.runtime.requests import request_operation_id
from shovel.runtime.schemas import ResourceInput, ScanInput
from shovel.runtime.service import Runtime
from shovel.vector.store import init_vector_store


class StaticEmbedder:
    dimension = 4
    model_version = "process-test@1"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def aclose(self) -> None:
        return None


def process_settings(root: Path) -> AppSettings:
    return AppSettings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{(root / 'runtime.db').as_posix()}"},
            "zvec": {"knowledge_path": str(root / "vectors"), "log_dir": str(root / "logs"), "dim": 4},
            "embedding": {"provider": "openai", "model": "process-test", "base_url": "http://unused.invalid"},
        }
    )


async def _wait(runtime: Runtime, identifier: str) -> Operation:
    while True:
        operation = await runtime.operations.get(identifier)
        if operation.state in TERMINAL:
            assert operation.state == "succeeded", operation.error
            return operation
        await asyncio.sleep(0.01)


async def crash_process(root: Path, point: str) -> None:
    import shovel.runtime.operations as operations_module

    settings = process_settings(root)
    init_vector_store(settings.zvec)
    operations_module.build_embedder = lambda *_args, **_kwargs: StaticEmbedder()  # type: ignore[assignment]
    source = root / "notes"
    source.mkdir()
    (source / "notes.txt").write_text("进程退出后仍可恢复的知识。", encoding="utf-8")
    host, provider = build_cli_host(settings)
    await host.start_async()
    runtime = await resolve_runtime(provider)
    created = await runtime.operations.create_resource(
        ResourceInput(name="notes", connector_kind="local_fs", config={"root_path": str(source)})
    )
    await _wait(runtime, created.id)
    assert created.resource_id is not None
    armed = False
    execute = OperationService._execute
    discover = ScanService._discover

    async def execute_then_exit(self: OperationService, operation: Operation) -> Any:
        result = await execute(self, operation)
        if armed and point == "job-terminal" and operation.kind == "scan":
            os._exit(73)
        return result

    async def flush_then_exit(
        self: ScanService, records: list[VectorRecord], *_args: Any, **_kwargs: Any
    ) -> int:
        await self._sink.upsert(records)
        os._exit(73)

    async def discover_then_exit(self: ScanService, **kwargs: Any) -> Any:
        result = await discover(self, **kwargs)
        if armed and point == "discovery":
            os._exit(73)
        return result

    delete_resource = ZvecChunkSink.delete_resource

    async def delete_then_exit(self: ZvecChunkSink, resource_id: str) -> int:
        result = await delete_resource(self, resource_id)
        if armed and point == "cleanup":
            os._exit(73)
        return result

    OperationService._execute = execute_then_exit
    ScanService._discover = discover_then_exit
    ZvecChunkSink.delete_resource = delete_then_exit
    if point in {"cleanup", "discovery"}:
        seed = await runtime.operations.scan(created.resource_id, ScanInput())
        await _wait(runtime, seed.id)
    if point == "vector-flushed":
        ScanService._flush = flush_then_exit
    armed = True
    if point == "cleanup":
        operation = await runtime.operations.resource_action(
            created.resource_id, "delete", request_key=f"crash-{point}"
        )
    else:
        operation = await runtime.operations.scan(
            created.resource_id, ScanInput(mode=ScanMode.FULL_SWEEP), request_key=f"crash-{point}"
        )
    if point == "submitted":
        os._exit(73)
    await _wait(runtime, operation.id)
    raise AssertionError(f"Crash point was not reached: {point}")


@pytest.mark.parametrize("point", ["submitted", "discovery", "vector-flushed", "job-terminal", "cleanup"])
async def test_real_process_exit_recovers_durable_state_and_releases_native_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    import shovel.runtime.operations as operations_module

    script = (
        "import asyncio,sys; from pathlib import Path; "
        "from backend.tests.test_runtime_process_recovery import crash_process; "
        "asyncio.run(crash_process(Path(sys.argv[1]),sys.argv[2]))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        point,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 73, (stdout.decode(), stderr.decode())
    if point == "discovery":
        (tmp_path / "notes" / "notes.txt").unlink()
    monkeypatch.setattr(operations_module, "build_embedder", lambda *_args, **_kwargs: StaticEmbedder())
    settings = process_settings(tmp_path)
    receipt = await asyncio.wait_for(
        manage_operation(settings, request_operation_id(f"crash-{point}"), wait=True), timeout=45
    )
    assert receipt["state"] == "succeeded", receipt
    if point == "job-terminal":
        assert receipt["attempts"] == 1
    if point == "discovery":
        assert receipt["result"]["tombstoned"] == 1
    with RuntimeLock(settings):
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        try:
            async with create_async_session_factory(engine)() as session:
                pending = await session.scalar(
                    select(func.count()).select_from(DeletionOutbox).where(DeletionOutbox.done_at.is_(None))
                )
                assert pending == 0
                identifiers = set(
                    await session.scalars(
                        select(Chunk.id)
                        .join(
                            Document,
                            (Document.uri == Chunk.doc_uri) & (Document.resource_id == Chunk.resource_id),
                        )
                        .where(
                            Document.lifecycle_state == DocumentLifecycleState.ACTIVE,
                            Chunk.granularity == Granularity.CHUNK,
                        )
                    )
                )
                if point == "cleanup":
                    assert await session.scalar(select(func.count()).select_from(Job)) == 0
                else:
                    job = await session.get(Job, receipt["job_id"])
                    assert job is not None and job.id == receipt["result"]["job_id"]
            collection = zvec.open(str(settings.zvec.knowledge_path))
            try:
                assert int(collection.stats.doc_count) == len(identifiers)
                if identifiers:
                    assert set(collection.fetch(list(identifiers))) == identifiers
            finally:
                collection.close()
        finally:
            await context.aclose()
