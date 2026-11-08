import asyncio
import json

import pytest
from pydantic import ValidationError
from scripts.synthetic_storage import synthetic_governance
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from shovel.capability.domain import CapabilityScope
from shovel.db.migrations import upgrade_database
from shovel.db.model.protection import PROTECTION_DDL
from shovel.db.model.protection import EnterpriseMetadata as MetadataRow
from shovel.db.model.protection import EnterpriseMetadataSource as SourceRow
from shovel.db.model.protection import EnterpriseSourceFence as FenceRow
from shovel.db.model.retrieval import DeletionOutbox, SchemaMigration
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.domain.enums import DeletionScope
from shovel.exceptions.pipeline import VectorSinkError
from shovel.pipeline.cleanup import IndexCleanupService
from shovel.pipeline.sink import NullVectorSink
from shovel.protection.domain import (
    DataAction,
    DataDestination,
    DependencyKind,
    DerivedDataRecord,
    Lineage,
    LineageEdge,
    ModelBinding,
    PolicyOutcome,
    ProtectionAdapterError,
    ProtectionDeniedError,
    ReferenceUnavailableError,
    SourceContentRef,
    content_digest,
)
from shovel.protection.persistence import (
    ENTERPRISE_COLLECTION,
    EmbeddingMetadata,
    EnterpriseMetadata,
    MetadataCandidate,
    SourceChange,
    source_key,
)
from shovel.protection.policy import ProtectionPolicy
from shovel.runtime.errors import RuntimeFailure
from test_capability_registry import SCOPE, Catalog
from test_capability_registry import catalog as catalog
from test_data_protection_contracts import source
from test_enterprise_connection_contracts import resource
from test_protection_policy import Audit, Provider, reviewed_context, state_for, use_binding


def record(*, derived=False, embedding=False, sources=None, scope=SCOPE, identifier="metadata"):
    sources = sources or (source(),)
    ctx = reviewed_context(sources=sources)
    if derived:
        ctx = ctx.model_copy(
            update={
                "derived": True,
                "derivation_operation": "embedding" if embedding else "summarize",
                "model_profile_ref": "approved-model" if embedding else None,
            }
        )
    anchor = use_binding(ctx, action=DataAction.PERSIST, destination=DataDestination.LOCAL_SQLITE)
    anchor = anchor.model_copy(
        update={
            "identities": tuple(
                identity.model_copy(update={"workspace_id": scope.workspace_id, "user_id": scope.user_id})
                for identity in anchor.identities
            )
        }
    )
    lineage = Lineage(
        edges=tuple(
            LineageEdge(source=item, kind=DependencyKind.DATA if index == 0 else DependencyKind.CONTROL)
            for index, item in enumerate(sources)
        )
    )
    digest = content_digest((0.1, 0.2, 0.3, 0.4) if embedding else "BODY-MUST-NOT-BE-STORED")
    return EnterpriseMetadata(
        id=identifier,
        scope=scope,
        context=ctx,
        lineage=lineage,
        identities=anchor.identities,
        connections=anchor.connections,
        snapshots=tuple(item.snapshot for item in state_for(ctx, anchor).sources),
        content_digest=digest,
        body_recovery="unavailable" if derived else "source_reference",
        source_reference=None if derived else SourceContentRef(resource=sources[0].resource),
        derivation=(
            DerivedDataRecord(
                id=f"derived-{identifier}",
                output_type="embedding" if embedding else "summary",
                lineage=lineage,
                protection_context_ref=ctx.id,
                operation_type=ctx.derivation_operation,
                executor_identity="executor",
                execution_id="execution",
                capability_id="capability",
                capability_version=1,
                model_profile_ref=ctx.model_profile_ref,
                created_at=1,
                policy_decision_ref="decision",
            )
            if derived
            else None
        ),
        embedding=(
            EmbeddingMetadata(
                model=ModelBinding(
                    model_profile_id="approved-model",
                    revision=1,
                    provider="synthetic",
                    model_name="test-embedding",
                    endpoint="https://model.example",
                    environment="test",
                ),
                dimension=4,
                vector_digest=digest,
                index_generation=0,
            )
            if embedding
            else None
        ),
        created_at=1,
    )


def use(value, *, write=False, **changes):
    return use_binding(
        value.context,
        identities=value.identities,
        connections=value.connections,
        content_digest=value.digest,
        action=DataAction.PERSIST if write else DataAction.READ,
        destination=DataDestination.LOCAL_SQLITE if write else DataDestination.WORKFLOW_STATE,
        **changes,
    )


class Authority:
    def __init__(self):
        self.changes = set()

    def verify(self, change):
        if change not in self.changes:
            raise ProtectionDeniedError("Source change has not been verified.")


class Harness:
    def __init__(self, sessions):
        self.now = 10
        self.provider = Provider()
        self.audit = Audit()
        self.authority = Authority()
        self.policy = ProtectionPolicy(self.audit, provider=self.provider, clock=lambda: self.now)
        self.sessions = sessions
        self.governance, self.storage_authority = synthetic_governance(SCOPE, lambda: self.now)
        self.store = self.restart()

    def restart(self):
        return SQLiteProtectionRepository(
            self.sessions,
            self.policy,
            source_authority=self.authority,
            clock=lambda: self.now,
            governance=self.governance,
        )

    def scoped_store(self, scope):
        return SQLiteProtectionRepository(
            self.sessions,
            self.policy,
            source_authority=self.authority,
            clock=lambda: self.now,
            governance=synthetic_governance(scope, lambda: self.now)[0],
        )

    def approve(self, value, *, write=False):
        binding = use(value, write=write)
        self.provider.states[binding.digest] = state_for(value.context, binding)
        return binding

    def change(self, value, *, index=0, **changes):
        result = SourceChange.model_validate(
            {
                "event_id": "verified-event",
                "scope": value.scope,
                "resource": value.context.sources[index].resource,
                "expected_generation": 0,
                "reason": "source_revoked",
                **changes,
            }
        )
        self.authority.changes.add(result)
        return result


@pytest.fixture
def harness(catalog: Catalog):
    return Harness(catalog.sessions)


async def counts(sessions):
    async with sessions() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (MetadataRow, FenceRow, SourceRow, DeletionOutbox)
            ]
        )


async def test_default_deny_and_wrong_destination_make_no_persistent_writes(harness):
    value = record()
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(value, use(value, write=True))
    binding = harness.approve(value, write=True)
    wrong = binding.model_copy(update={"destination": DataDestination.KNOWLEDGE_STORE})
    harness.provider.states[wrong.digest] = state_for(value.context, wrong)
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(value, wrong)
    with pytest.raises(ProtectionDeniedError):
        await harness.store.invalidate(
            SourceChange(
                event_id="unverified",
                scope=SCOPE,
                resource=value.context.sources[0].resource,
                expected_generation=0,
                reason="source_deleted",
            )
        )
    assert await counts(harness.sessions) == (0, 0, 0, 0)


@pytest.mark.parametrize("generations", [(0, 1), (1, 0)])
async def test_same_logical_source_cannot_hide_a_conflicting_lineage_generation(harness, generations):
    sources = tuple(
        source(resource=resource(source_version=f"v{index}"), source_generation=generation)
        for index, generation in enumerate(generations)
    )
    with pytest.raises(ProtectionDeniedError):
        await harness.store.check_sources(SCOPE, sources)
    assert await counts(harness.sessions) == (0, 0, 0, 0)


async def test_authorized_metadata_roundtrip_restart_idempotency_and_no_legacy_body(harness):
    value = record()
    write = harness.approve(value, write=True)
    await harness.store.put(value, write)
    await harness.store.put(value, write)
    assert await counts(harness.sessions) == (1, 1, 1, 0)
    read = harness.approve(value)
    assert await harness.restart().read(SCOPE, value.id, read) == value
    assert value.require_source_reference().resource == value.context.sources[0].resource
    async with harness.sessions() as session:
        row = await session.get(MetadataRow, (SCOPE.workspace_id, SCOPE.user_id, value.id))
        assert row.vector_id == value.vector_id
        assert row.admission_binding == write.model_dump(mode="json")
        serialized = json.dumps(row.payload)
        assert "BODY-MUST-NOT-BE-STORED" not in serialized
        assert "vector" not in row.payload
        for table in ("chunk", "document", "memory_belief", "document_derived"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert "BODY-MUST-NOT-BE-STORED" not in json.dumps(
        [item.model_dump(mode="json") for item in harness.audit.events]
    )


async def test_derived_metadata_explicitly_cannot_recover_body_after_restart(harness):
    value = record(derived=True)
    await harness.store.put(value, harness.approve(value, write=True))
    restored = await harness.restart().read(SCOPE, value.id, harness.approve(value))
    assert restored.body_recovery == "unavailable"
    assert restored.source_reference is None
    assert restored.derivation == value.derivation
    with pytest.raises(ReferenceUnavailableError, match="replay is not recovery"):
        restored.require_source_reference()


@pytest.mark.parametrize("field", ["body", "text", "vector", "artifact", "transient_handle"])
def test_unapproved_body_and_handle_fields_are_not_metadata(field):
    data = record().model_dump(mode="json")
    data[field] = "BODY-MUST-NOT-BE-STORED"
    with pytest.raises(ValidationError):
        EnterpriseMetadata.model_validate(data)


@pytest.mark.parametrize(
    "alter",
    [
        lambda value: {
            "lineage": Lineage(
                edges=(
                    LineageEdge(
                        source=source(resource=resource(resource_id="other")), kind=DependencyKind.DATA
                    ),
                )
            )
        },
        lambda value: {"source_reference": SourceContentRef(resource=resource(source_version="different"))},
        lambda value: {"body_recovery": "unavailable"},
        lambda value: {
            "source_reference": {
                "kind": "transient",
                "handle_id": "handle",
                "process_instance_id": "previous-process",
                "expires_at": 20,
            }
        },
        lambda value: {"identities": (value.identities[0].model_copy(update={"user_id": "other"}),)},
        lambda value: {"snapshots": (value.snapshots[0].model_copy(update={"identity_revision": 2}),)},
        lambda value: {
            "snapshots": (
                value.snapshots[0].model_copy(update={"resource_scope": (resource(resource_id="other"),)}),
            )
        },
    ],
)
def test_incomplete_or_cross_bound_metadata_is_rejected(alter):
    value = record()
    with pytest.raises(ValidationError):
        EnterpriseMetadata.model_validate(value.model_copy(update=alter(value)))


async def test_scope_and_payload_digest_cannot_be_substituted(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(
            CapabilityScope(workspace_id="workspace", user_id="another"), value.id, harness.approve(value)
        )
    different = record(identifier="different")
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(different, harness.approve(value, write=True))
    other_scope = CapabilityScope(workspace_id="other", user_id="owner")
    other = record(scope=other_scope)
    other_store = harness.scoped_store(other_scope)
    await other_store.put(other, harness.approve(other, write=True))
    assert await other_store.read(other_scope, other.id, harness.approve(other)) == other
    assert other.vector_id != value.vector_id
    assert await counts(harness.sessions) == (2, 2, 2, 0)


async def test_same_id_cannot_replace_an_immutable_record_even_with_new_write_permission(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    changed = value.model_copy(update={"created_at": 2})
    with pytest.raises(RuntimeFailure, match="immutable record"):
        await harness.store.put(changed, harness.approve(changed, write=True))
    assert await harness.store.read(SCOPE, value.id, harness.approve(value)) == value
    assert await counts(harness.sessions) == (1, 1, 1, 0)


async def test_lifecycle_requires_configured_authority_and_sanitizes_adapter_errors(harness):
    value = record()
    change = harness.change(value)
    unavailable = SQLiteProtectionRepository(harness.sessions, harness.policy, clock=lambda: 10)
    with pytest.raises(ProtectionDeniedError, match="verification is unavailable"):
        await unavailable.invalidate(change)

    class BrokenAuthority:
        def verify(self, _change):
            raise RuntimeError("PRIVATE-SDK-ERROR")

    broken = SQLiteProtectionRepository(
        harness.sessions, harness.policy, source_authority=BrokenAuthority(), clock=lambda: 10
    )
    with pytest.raises(ProtectionAdapterError, match="verification failed") as error:
        await broken.invalidate(change)
    assert "PRIVATE-SDK-ERROR" not in str(error.value)
    assert await counts(harness.sessions) == (0, 0, 0, 0)


async def test_read_requires_new_live_authority_not_stored_allow_snapshot(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    binding = harness.approve(value)
    state = harness.provider.states[binding.digest]
    harness.provider.states[binding.digest] = state.model_copy(update={"rules": ()})
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, binding)
    harness.provider.states[binding.digest] = state.model_copy(
        update={"sources": (state.sources[0].model_copy(update={"authentication_valid": False}),)}
    )
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, binding)
    harness.provider.states[binding.digest] = state
    harness.now = 41
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, binding)


async def test_original_snapshot_is_provenance_not_a_cached_read_grant(harness):
    original = record()
    value = original.model_copy(
        update={"snapshots": (original.snapshots[0].model_copy(update={"revalidate_at": 11}),)}
    )
    await harness.store.put(value, harness.approve(value, write=True))
    harness.now = 20
    assert await harness.store.read(SCOPE, value.id, harness.approve(value)) == value
    state_binding = use(value)
    state = harness.provider.states[state_binding.digest]
    harness.provider.states[state_binding.digest] = state.model_copy(
        update={"sources": (state.sources[0].model_copy(update={"snapshot": value.snapshots[0]}),)}
    )
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, state_binding)


async def test_expired_admission_snapshots_and_slow_sql_cannot_commit(harness):
    value = record()
    stale = value.model_copy(
        update={"snapshots": (value.snapshots[0].model_copy(update={"revalidate_at": 9}),)}
    )
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(stale, harness.approve(stale, write=True))
    binding = harness.approve(value, write=True)
    engine = harness.sessions.kw["bind"]

    def delay(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO enterprise_metadata_source"):
            harness.now = 25

    event.listen(engine.sync_engine, "before_cursor_execute", delay)
    try:
        with pytest.raises(ProtectionDeniedError):
            await harness.store.put(value, binding)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", delay)
    assert await counts(harness.sessions) == (0, 0, 0, 0)
    assert harness.audit.events[-1].outcome != PolicyOutcome.ALLOW


async def test_control_source_invalidates_all_derived_dependents_and_queues_exact_ids(harness):
    original = record(identifier="original")
    control = source(resource=resource(resource_id="control"))
    derived = record(derived=True, sources=(source(), control), identifier="derived")
    for value in (original, derived):
        await harness.store.put(value, harness.approve(value, write=True))
    change = harness.change(derived, index=1)
    assert await harness.store.invalidate(change) == 1
    assert await harness.restart().invalidate(change) == 1
    assert await harness.store.read(SCOPE, original.id, harness.approve(original)) == original
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, derived.id, harness.approve(derived))
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(derived, harness.approve(derived, write=True))
    async with harness.sessions() as session:
        row = await session.get(MetadataRow, ("workspace", "owner", derived.id))
        assert row.state == "invalid" and row.revision == 2
        intents = list(await session.scalars(select(DeletionOutbox)))
        assert len(intents) == 1
        assert intents[0].scope == DeletionScope.CHUNK_IDS
        assert intents[0].collection == ENTERPRISE_COLLECTION
        assert intents[0].filter == {"ids": [derived.vector_id]}
    assert await IndexCleanupService(harness.sessions, NullVectorSink()).drain() == 0


async def test_tombstone_before_first_write_fences_versions_parents_and_late_results(harness):
    value = record()
    await harness.store.invalidate(harness.change(value, reason="source_deleted"))
    newer = source(
        resource=resource(source_version="new-version", etag="new-etag", parent_resource_id="new-parent"),
        source_generation=1,
    )
    for candidate in (value, record(sources=(newer,), identifier="late")):
        with pytest.raises(ProtectionDeniedError):
            await harness.store.put(candidate, harness.approve(candidate, write=True))
    assert source_key(newer.resource) == source_key(value.context.sources[0].resource)
    assert await counts(harness.sessions) == (0, 1, 0, 0)


async def test_generations_and_event_arguments_are_monotonic_and_scope_bound(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    first = harness.change(value)
    assert await harness.store.invalidate(first) == 1
    changed = harness.change(value, reason="source_deleted")
    with pytest.raises(RuntimeFailure, match="different arguments"):
        await harness.store.invalidate(changed)
    second = harness.change(value, event_id="delete-event", reason="source_deleted", expected_generation=1)
    assert await harness.store.invalidate(second) == 2
    with pytest.raises(RuntimeFailure, match="generation changed"):
        await harness.store.invalidate(first)
    async with harness.sessions() as session:
        row = await session.get(MetadataRow, ("workspace", "owner", value.id))
        assert row.state == "deleted"
        assert row.revision == 3
    other = record(scope=CapabilityScope(workspace_id="other", user_id="owner"))
    other_store = harness.scoped_store(other.scope)
    await other_store.put(other, harness.approve(other, write=True))
    assert await other_store.read(other.scope, other.id, harness.approve(other)) == other


async def test_invalidation_and_cleanup_intent_roll_back_together_on_sql_failure(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    change = harness.change(value)
    engine = harness.sessions.kw["bind"]

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO deletion_outbox"):
            raise OperationalError(statement, parameters, RuntimeError("PRIVATE-SDK-ERROR"))

    event.listen(engine.sync_engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(ProtectionAdapterError, match="transaction failed") as error:
            await harness.store.invalidate(change)
        assert "PRIVATE-SDK-ERROR" not in str(error.value)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject)
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, harness.approve(value))
    async with harness.sessions() as session:
        fence = await session.get(FenceRow, ("workspace", "owner", source_key(change.resource)))
        assert fence.generation == 0 and not fence.blocked
        assert await session.scalar(select(func.count()).select_from(DeletionOutbox)) == 0
    assert await harness.store.invalidate(change) == 1


async def test_concurrent_late_write_and_revocation_never_leave_an_active_record(harness):
    value = record()
    write = harness.approve(value, write=True)
    change = harness.change(value)
    results = await asyncio.gather(
        harness.store.put(value, write), harness.store.invalidate(change), return_exceptions=True
    )
    assert results[1] == 1
    assert results[0] is None or isinstance(results[0], ProtectionDeniedError)
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, harness.approve(value))


@pytest.mark.parametrize(
    "field", ["metadata_digest", "revision", "model", "index_generation", "vector_digest"]
)
async def test_vector_candidates_require_exact_metadata_model_and_generations(harness, field):
    value = record(derived=True, embedding=True)
    await harness.store.put(value, harness.approve(value, write=True))
    candidate = MetadataCandidate(
        record_id=value.id, revision=1, metadata_digest=value.digest, embedding=value.embedding
    )
    binding = harness.approve(value)
    assert await harness.store.read(SCOPE, value.id, binding, candidate=candidate) == value
    if field in {"metadata_digest", "revision"}:
        candidate = candidate.model_copy(update={field: "b" * 64 if field == "metadata_digest" else 2})
    else:
        candidate = candidate.model_copy(
            update={
                "embedding": candidate.embedding.model_copy(
                    update={
                        field: (
                            candidate.embedding.model.model_copy(update={"revision": 2})
                            if field == "model"
                            else 1
                            if field == "index_generation"
                            else "b" * 64
                        )
                    }
                )
            }
        )
    with pytest.raises(ProtectionDeniedError, match="candidate"):
        await harness.store.read(SCOPE, value.id, binding, candidate=candidate)


class Deleter:
    def __init__(self, sessions):
        self.fail = True
        self.calls = []
        self.sessions = sessions

    async def delete_ids(self, identifiers):
        self.calls.append(identifiers)
        async with self.sessions() as session:
            assert await session.scalar(select(DeletionOutbox.done_at)) is None
        if self.fail:
            raise RuntimeError("PRIVATE-SDK-ERROR")


async def test_enterprise_cleanup_is_safe_retryable_and_does_not_consume_personal_intents(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    await harness.store.invalidate(harness.change(value))
    async with harness.sessions() as session:
        session.add(DeletionOutbox(scope=DeletionScope.RESOURCE, resource_id="personal"))
        await session.commit()
    sink = Deleter(harness.sessions)
    cleanup = IndexCleanupService(harness.sessions, enterprise_sink=sink)
    with pytest.raises(VectorSinkError) as error:
        await cleanup.drain()
    assert "PRIVATE-SDK-ERROR" not in str(error.value)
    async with harness.sessions() as session:
        intent = await session.scalar(
            select(DeletionOutbox).where(DeletionOutbox.collection == ENTERPRISE_COLLECTION)
        )
        assert intent.done_at is None and intent.attempts == 1
        assert intent.last_error == "enterprise_vector_delete_failed"
    sink.fail = False
    assert await cleanup.drain() == 1
    assert await cleanup.drain() == 0
    assert sink.calls == [(value.vector_id,), (value.vector_id,)]
    async with harness.sessions() as session:
        legacy = await session.scalar(select(DeletionOutbox).where(DeletionOutbox.collection.is_(None)))
        assert legacy.done_at is None and legacy.attempts == 0


async def test_malformed_enterprise_deletion_is_not_dispatched_or_acknowledged(harness):
    async with harness.sessions() as session:
        session.add(
            DeletionOutbox(
                scope=DeletionScope.CHUNK_IDS,
                collection=ENTERPRISE_COLLECTION,
                filter={"ids": ["not-a-vector-id"], "body": "PRIVATE-SDK-ERROR"},
            )
        )
        await session.commit()
    sink = Deleter(harness.sessions)
    with pytest.raises(VectorSinkError):
        await IndexCleanupService(harness.sessions, enterprise_sink=sink).drain()
    assert sink.calls == []
    async with harness.sessions() as session:
        intent = await session.scalar(select(DeletionOutbox))
        assert intent.done_at is None
        assert intent.last_error == "enterprise_vector_delete_failed"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE enterprise_metadata SET state='active', revision=3",
        "UPDATE enterprise_metadata SET payload='{}', revision=3, state='deleted'",
        "DELETE FROM enterprise_metadata",
        "UPDATE enterprise_source_fence SET blocked=0, generation=0",
        "DELETE FROM enterprise_source_fence",
        "UPDATE enterprise_metadata_source SET generation=1",
        "DELETE FROM enterprise_metadata_source",
    ],
)
async def test_sql_guards_preserve_immutable_lineage_and_tombstones(harness, statement):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    await harness.store.invalidate(harness.change(value))
    async with harness.sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text(statement))
        await session.rollback()


async def test_migration_seventeen_upgrades_old_store_repeatably_without_legacy_changes(catalog: Catalog):
    engine = catalog.sessions.kw["bind"]
    async with engine.begin() as connection:
        for table in ("enterprise_metadata_source", "enterprise_metadata", "enterprise_source_fence"):
            await connection.exec_driver_sql(f"DROP TABLE {table}")
        await connection.exec_driver_sql("DELETE FROM schema_migration WHERE version=17")
        await connection.exec_driver_sql(
            """INSERT INTO operation(id,kind,state,payload,result,queued_at,attempts,request_key)
            VALUES ('legacy-es7','cleanup','succeeded','{}','{"preserved":true}',1,1,'original-key')"""
        )
    await upgrade_database(engine)
    await upgrade_database(engine)
    async with catalog.sessions() as session:
        assert list(
            await session.scalars(select(SchemaMigration.version).order_by(SchemaMigration.version))
        ) == list(range(1, 23))
        triggers = set(await session.scalars(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
        assert {statement.split("EXISTS ", 1)[1].split()[0] for statement in PROTECTION_DDL} <= triggers
        assert (
            await session.scalar(text("SELECT request_key FROM operation WHERE id='legacy-es7'"))
            == "original-key"
        )
        assert (
            await session.scalar(text("SELECT result FROM operation WHERE id='legacy-es7'"))
            == '{"preserved":true}'
        )
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
