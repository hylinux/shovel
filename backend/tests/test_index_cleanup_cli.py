"""CLI deletion uses durable intent and the real vector collection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from shovel.cli.app import app
from shovel.cli.context import cli_context
from shovel.config.settings import AppSettings
from shovel.db.base import chunk_vector_id
from shovel.db.database import (
    create_async_database_engine,
    create_async_session_factory,
    init_database,
)
from shovel.db.model.config import Resource
from shovel.db.model.retrieval import DeletionOutbox
from shovel.exceptions.pipeline import VectorSinkError
from shovel.pipeline.sink import VectorRecord, ZvecChunkSink, open_chunk_sink
from shovel.services.resource_service import ResourceDraft, ResourceService
from shovel.vector.store import init_vector_store, open_collection


@pytest.mark.parametrize("interrupt_cleanup", [False, True])
def test_remove_and_retry_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_cleanup: bool,
) -> None:
    settings = AppSettings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{(tmp_path / 'shovel.db').as_posix()}"},
            "zvec": {
                "knowledge_path": str(tmp_path / "knowledge"),
                "log_dir": str(tmp_path / "logs"),
                "dim": 4,
            },
        }
    )
    config_path = tmp_path / "settings.toml"
    settings.save(config_path)
    monkeypatch.setattr(cli_context, "get_default_config_file", lambda: config_path)
    init_database(settings.database.url)
    init_vector_store(settings.zvec)
    uri = (tmp_path / "source.txt").as_uri()

    async def prepare() -> tuple[str, str]:
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        sink = open_chunk_sink(settings.zvec.knowledge_path)
        try:
            service = ResourceService(create_async_session_factory(engine))
            ids = []
            for name in ("remove-me", "keep-me"):
                resource = await service.create(
                    ResourceDraft(
                        name=name,
                        connector_kind="local_fs",
                        config={"root_path": str(tmp_path)},
                    )
                )
                ids.append(resource.id)
                await sink.upsert(
                    [
                        VectorRecord(
                            id=chunk_vector_id(uri, "hash", "2", "chunk", 0, resource_id=resource.id),
                            vector=[0.1] * 4,
                            text="example",
                            resource_id=resource.id,
                            doc_uri=uri,
                            granularity="chunk",
                            chunk_index=0,
                        )
                    ]
                )
            return ids[0], ids[1]
        finally:
            await sink.aclose()
            await context.aclose()

    removed_id, kept_id = asyncio.run(prepare())
    runner = CliRunner()
    original = ZvecChunkSink.delete_resource

    async def fail(self: ZvecChunkSink, resource_id: str) -> int:
        raise VectorSinkError("simulated deletion failure")

    if interrupt_cleanup:
        monkeypatch.setattr(ZvecChunkSink, "delete_resource", fail)

    result = runner.invoke(app, ["resource", "remove", "remove-me", "--yes"])
    assert result.exit_code == (1 if interrupt_cleanup else 0), result.output

    async def check_queue(*, done: bool) -> None:
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        try:
            async with create_async_session_factory(engine)() as session:
                assert await session.get(Resource, removed_id) is None
                assert await session.get(Resource, kept_id) is not None
                intent = (await session.scalars(select(DeletionOutbox))).one()
                assert (intent.done_at is not None) is done
                if not done:
                    assert intent.last_error == "simulated deletion failure"
        finally:
            await context.aclose()

    asyncio.run(check_queue(done=not interrupt_cleanup))
    monkeypatch.setattr(ZvecChunkSink, "delete_resource", original)
    retry = runner.invoke(app, ["scan", "cleanup", "--resource-id", removed_id])
    assert retry.exit_code == 0, retry.output
    asyncio.run(check_queue(done=True))

    collection = open_collection(settings.zvec.knowledge_path)
    try:
        removed_vector = chunk_vector_id(uri, "hash", "2", "chunk", 0, resource_id=removed_id)
        kept_vector = chunk_vector_id(uri, "hash", "2", "chunk", 0, resource_id=kept_id)
        assert removed_vector not in collection.fetch([removed_vector])
        assert kept_vector in collection.fetch([kept_vector])
    finally:
        collection.close()


def test_recovery_options_are_exposed() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["scan", "run", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output
    result = runner.invoke(app, ["scan", "cleanup", "--help"])
    assert result.exit_code == 0
    assert "--resource-id" in result.output
