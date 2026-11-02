import asyncio
import threading
from types import SimpleNamespace

import pytest
import zvec
from sqlalchemy import select

from shovel.db.database import create_async_database_engine, create_async_session_factory, init_database
from shovel.db.model.protection import PROTECTION_DDL
from shovel.db.model.retrieval import DeletionOutbox
from shovel.exceptions.pipeline import VectorSinkError
from shovel.pipeline.cleanup import IndexCleanupService
from shovel.pipeline.sink import ZvecEnterpriseDeletionSink
from shovel.protection.domain import DataAction, DataDestination, ProtectionDeniedError, content_digest
from shovel.protection.policy import ProtectionPolicy
from shovel.protection.service import ProtectionService
from shovel.vector.schema import (
    DENSE_VECTOR,
    chunk_collection_schema,
    enterprise_collection_schema,
    memory_collection_schema,
)
from shovel.vector.store import init_zvec_runtime
from test_capability_registry import catalog as catalog
from test_enterprise_persistence import Harness, record
from test_protection_policy import Audit, Provider, reviewed_context, state_for, use_binding, wrapped


@pytest.fixture
def enterprise_collection(tmp_path):
    init_zvec_runtime()
    collection = zvec.create_and_open(str(tmp_path / "enterprise"), enterprise_collection_schema(dim=4))
    try:
        yield collection
    finally:
        collection.close()


def insert_fixture(collection, value):
    embedding = value.embedding
    fields = {
        "metadata_id": value.id,
        "workspace_id": value.scope.workspace_id,
        "user_id": value.scope.user_id,
        "tenant_id": value.identities[0].tenant_id,
        "identity_binding_digest": content_digest(
            [item.model_dump(mode="json") for item in value.identities]
        ),
        "connection_binding_digest": content_digest(
            [item.model_dump(mode="json") for item in value.connections]
        ),
        "classification_id": value.context.classification_id,
        "lifecycle": "active",
        "source_set_digest": content_digest([item.model_dump(mode="json") for item in value.context.sources]),
        "protection_context_ref": value.context.id,
        "protection_context_digest": value.context.content_hash,
        "metadata_digest": value.digest,
        "content_digest": value.content_digest,
        "policy_version": value.context.policy_version,
        "model_binding_digest": content_digest(embedding.model.model_dump(mode="json")),
        "revision": 1,
        "index_generation": embedding.index_generation,
        "model_revision": embedding.model.revision,
        "dimension": embedding.dimension,
        "indexed_at": 10.0,
    }
    results = collection.upsert(
        [zvec.Doc(id=value.vector_id, fields=fields, vectors={DENSE_VECTOR: [0.1, 0.2, 0.3, 0.4]})]
    )
    assert all(result.ok for result in results)
    collection.flush()
    return fields


def test_real_vector_collection_stays_empty_when_read_is_allowed_but_index_is_denied(enterprise_collection):
    value = record(derived=True, embedding=True)
    ctx = reviewed_context()
    read = use_binding(ctx, action=DataAction.READ, destination=DataDestination.WORKFLOW_STATE)
    index = use_binding(ctx, action=DataAction.INDEX, destination=DataDestination.LOCAL_ZVEC)
    read_state = state_for(ctx, read)
    provider = Provider(read_state, state_for(ctx, index, rules=read_state.rules))
    policy = ProtectionPolicy(Audit(), provider=provider, clock=lambda: 10)
    service = ProtectionService(policy, clock=lambda: 10)
    assert service.use(wrapped(ctx), read, lambda body: body)
    with pytest.raises(ProtectionDeniedError):
        service.use(wrapped(ctx), index, lambda _body: insert_fixture(enterprise_collection, value))
    enterprise_collection.flush()
    assert enterprise_collection.fetch([value.vector_id]) == {}


async def test_real_enterprise_collection_contains_only_metadata_and_deletes_exact_ids(
    enterprise_collection, tmp_path
):
    first = record(derived=True, embedding=True, identifier="first")
    second = record(derived=True, embedding=True, identifier="second")
    fields = insert_fixture(enterprise_collection, first)
    insert_fixture(enterprise_collection, second)
    actual = enterprise_collection.fetch([first.vector_id])[first.vector_id].fields
    assert actual == fields
    assert "text" not in actual and "body" not in actual
    sink = ZvecEnterpriseDeletionSink(enterprise_collection)
    await sink.delete_ids((first.vector_id.upper(),))
    await sink.delete_ids((first.vector_id,))
    assert set(enterprise_collection.fetch([first.vector_id, second.vector_id])) == {second.vector_id}
    await sink.aclose()
    reopened = zvec.open(str(tmp_path / "enterprise"))
    try:
        assert set(reopened.fetch([first.vector_id, second.vector_id])) == {second.vector_id}
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["knowledge", "memory", "disguised"])
def test_enterprise_deletion_refuses_personal_or_text_bearing_collection(tmp_path, kind):
    init_zvec_runtime()
    if kind == "memory":
        schema = memory_collection_schema(dim=4)
    else:
        schema = chunk_collection_schema(
            name=enterprise_collection_schema(dim=4).name if kind == "disguised" else "personal", dim=4
        )
    collection = zvec.create_and_open(str(tmp_path / kind), schema)
    try:
        with pytest.raises(VectorSinkError, match="schema is incompatible"):
            ZvecEnterpriseDeletionSink(collection)
    finally:
        collection.close()


async def test_real_cleanup_requires_native_flush_before_ack_and_safe_retry(catalog, enterprise_collection):
    harness = Harness(catalog.sessions)
    value = record(derived=True, embedding=True)
    await harness.store.put(value, harness.approve(value, write=True))
    insert_fixture(enterprise_collection, value)
    await harness.store.invalidate(harness.change(value))

    class FlushGate:
        schema = enterprise_collection.schema
        fail = True

        def delete(self, identifiers):
            return enterprise_collection.delete(identifiers)

        def flush(self):
            if self.fail:
                raise RuntimeError("PRIVATE-SDK-ERROR")
            enterprise_collection.flush()

    collection = FlushGate()
    cleanup = IndexCleanupService(catalog.sessions, enterprise_sink=ZvecEnterpriseDeletionSink(collection))
    with pytest.raises(VectorSinkError) as error:
        await cleanup.drain()
    assert "PRIVATE-SDK-ERROR" not in str(error.value)
    async with catalog.sessions() as session:
        intent = await session.scalar(select(DeletionOutbox))
        assert intent.done_at is None and intent.attempts == 1
    collection.fail = False
    assert await cleanup.drain() == 1
    assert enterprise_collection.fetch([value.vector_id]) == {}
    async with catalog.sessions() as session:
        intent = await session.scalar(select(DeletionOutbox))
        assert intent.done_at is not None and intent.attempts == 2 and intent.last_error is None


async def test_rejected_status_and_invalid_ids_never_claim_success():
    class RejectingCollection:
        schema = enterprise_collection_schema(dim=4)
        calls = 0

        def delete(self, identifiers):
            self.calls += 1
            return SimpleNamespace(ok=False)

        def flush(self):
            pytest.fail("Rejected deletion was flushed")

    collection = RejectingCollection()
    sink = ZvecEnterpriseDeletionSink(collection)
    with pytest.raises(VectorSinkError):
        await sink.delete_ids(("arbitrary-resource-path",))
    assert collection.calls == 0
    with pytest.raises(VectorSinkError):
        await sink.delete_ids((record().vector_id,))
    assert collection.calls == 1


async def test_cancelled_native_enterprise_deletion_finishes_before_close():
    entered, finish = threading.Event(), threading.Event()
    operations = []

    class BlockingCollection:
        schema = enterprise_collection_schema(dim=4)

        def delete(self, identifiers):
            entered.set()
            assert finish.wait(5)
            operations.append("delete")
            return SimpleNamespace(ok=True)

        def flush(self):
            operations.append("flush")

        def close(self):
            operations.append("close")

    sink = ZvecEnterpriseDeletionSink(BlockingCollection())
    task = asyncio.create_task(sink.delete_ids((record().vector_id,)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sink.aclose()
    await sink.aclose()
    assert operations == ["delete", "flush", "close"]


async def test_init_database_installs_protection_guards_without_upgrade(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'initialized.db').as_posix()}"
    init_database(url)
    context = create_async_database_engine(url)
    engine = await anext(context)
    try:
        sessions = create_async_session_factory(engine)
        async with sessions() as session:
            from sqlalchemy import text

            triggers = set(await session.scalars(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
            assert {statement.split("EXISTS ", 1)[1].split()[0] for statement in PROTECTION_DDL} <= triggers
    finally:
        await context.aclose()
