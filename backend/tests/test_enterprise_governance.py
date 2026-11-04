import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import zvec
from pydantic import ValidationError
from scripts.synthetic_storage import sign_governance, synthetic_governance
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from shovel.config.settings import AppSettings
from shovel.db.governance_maintenance import GovernedSQLiteMaintenance
from shovel.db.migrations import upgrade_database
from shovel.db.model.governance import EnterpriseRetention
from shovel.db.model.protection import EnterpriseMetadata as MetadataRow
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.enterprise.kusto import parse_metadata
from shovel.enterprise.organization_service import OrganizationService
from shovel.pipeline.cleanup import IndexCleanupService
from shovel.pipeline.sink import ZvecEnterpriseDeletionSink
from shovel.protection.domain import ProtectionAdapterError, ProtectionDeniedError
from shovel.protection.governance import GovernancePolicy, GovernanceService
from shovel.protection.persistence import MetadataCandidate
from shovel.runtime.locking import RuntimeLock
from test_capability_registry import SCOPE
from test_capability_registry import catalog as catalog
from test_data_protection_contracts import source
from test_enterprise_connection_contracts import resource
from test_enterprise_persistence import counts, record
from test_enterprise_persistence import harness as harness
from test_enterprise_vector import enterprise_collection as enterprise_collection
from test_enterprise_vector import insert_fixture
from test_execution_engine import kernel as kernel
from test_external_effects import effects as effects
from test_organization_security import policy_document, response_body
from test_organization_security import signer as signer


def revise(service, *, kind="metadata", **changes):
    policy = service.fixture_policy
    rules = tuple(rule.model_copy(update=changes) if rule.kind == kind else rule for rule in policy.rules)
    updated = GovernancePolicy.model_validate(
        policy.model_copy(update={"rules": rules, "revision": policy.revision + 1})
    )
    service.fixture_holder[0] = sign_governance(updated)
    service.fixture_policy = updated


def requirement(service, **changes):
    arguments = {
        "scope": SCOPE,
        "store_binding": "a" * 64,
        "kind": "metadata",
        "record_digest": "b" * 64,
        "created_at": service.clock(),
        "operation": "read",
        "retention_policy_ref": "retention",
    }
    return service.require(**(arguments | changes))


def maintenance(harness, tmp_path, *, authorize=None):
    settings = AppSettings()
    settings.database.url = str(harness.sessions.kw["bind"].url)
    settings.zvec.knowledge_path = str(tmp_path / "knowledge")
    settings.memory.index_path = tmp_path / "memory"
    return GovernedSQLiteMaintenance(
        settings, harness.governance, authorize or (lambda _operation, _source, _target: None)
    )


async def test_h1_persist_allow_does_not_supply_missing_storage_authority(harness):
    value = record()
    store = SQLiteProtectionRepository(
        harness.sessions, harness.policy, governance=GovernanceService(), clock=lambda: harness.now
    )
    with pytest.raises(ProtectionDeniedError, match="governance is unavailable"):
        await store.put(value, harness.approve(value, write=True))
    assert await counts(harness.sessions) == (0, 0, 0, 0)


def test_governance_signature_purpose_complete_matrix_and_duplicate_fields():
    service, _ = synthetic_governance(SCOPE, lambda: 10)
    policy = service.fixture_policy
    assert service.policy()[0] == policy
    payload = policy.model_dump_json().encode().replace(b'"revision":1', b'"revision":1,"revision":1')
    service.fixture_holder[0] = sign_governance(policy, payload=payload)
    with pytest.raises(ProtectionDeniedError):
        service.policy()
    for changes in (
        {"purpose": "shovel.organization.metadata.v1"},
        {"rules": policy.rules[:-1]},
        {"rules": (policy.rules[0],) * 8},
        {"expires_at": 0},
    ):
        with pytest.raises(ValidationError):
            GovernancePolicy.model_validate(policy.model_copy(update=changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"offline_seconds": 3601},
        {"backup_retention_seconds": 3601},
        {"backup": "deny"},
        {"on_revocation": "retain_until_expiry"},
    ],
)
def test_conflicting_retention_matrix_is_rejected(changes):
    service, _ = synthetic_governance(SCOPE, lambda: 10)
    with pytest.raises(ValidationError):
        revise(service, **changes)


@pytest.mark.parametrize(
    "alter",
    [
        {"request_digest": "0" * 64},
        {"store_binding": "0" * 64},
        {"storage_id": "untrusted-storage"},
        {"key_authority_id": "untrusted-keys"},
        {"observed_at": 0, "expires_at": 1},
        {"observed_at": 11},
        {"expires_at": 100},
        {"state": "retired"},
        {"non_exportable_keys": False},
        {"consumer_bound": False},
        {"rollback_protected": False},
        {"control_recovery_safe": False},
        {"backup_inventory_complete": False},
        {"retention_enforced": False},
        {"encrypted_surfaces": ("sqlite",)},
        {"erasure_capabilities": ("logical",)},
    ],
)
def test_unbound_stale_or_partial_storage_evidence_is_denied(alter):
    service, authority = synthetic_governance(SCOPE, lambda: 10)
    authority.alter = lambda evidence: evidence.model_copy(update=alter)
    with pytest.raises(ProtectionDeniedError):
        requirement(service)


def test_crypto_erasure_offline_and_backups_require_independent_capabilities():
    service, _ = synthetic_governance(SCOPE, lambda: 10)
    with pytest.raises(ProtectionDeniedError):
        requirement(service, offline=True)
    with pytest.raises(ProtectionDeniedError):
        requirement(service, operation="backup")
    revise(service, erasure="cryptographic")
    with pytest.raises(ProtectionDeniedError, match="insufficient"):
        requirement(service)


def test_policy_rotation_during_probe_and_old_evidence_replay_are_denied():
    service, authority = synthetic_governance(SCOPE, lambda: 10)
    old_evidence = requirement(service)[0]
    authority.alter = lambda _evidence: old_evidence
    with pytest.raises(ProtectionDeniedError):
        requirement(service)

    def rotate(evidence):
        revise(service, retention_seconds=1800)
        return evidence

    authority.alter = rotate
    with pytest.raises(ProtectionDeniedError):
        requirement(service)


async def test_key_unavailability_and_policy_rollback_cannot_reuse_sqlite_allow(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    harness.storage_authority.unavailable = True
    with pytest.raises(ProtectionDeniedError):
        await harness.restart().read(SCOPE, value.id, harness.approve(value))
    harness.storage_authority.unavailable = False
    harness.storage_authority.minimum_revision = 2
    with pytest.raises(ProtectionDeniedError):
        await harness.restart().read(SCOPE, value.id, harness.approve(value))


async def test_new_storage_authority_cannot_silently_adopt_old_metadata_or_backups(harness, tmp_path):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "authority-bound.db"
    receipt = maint.backup(backup)
    policy = harness.governance.fixture_policy.model_copy(
        update={"revision": 2, "key_authority_id": "SYNTHETIC-DIFFERENT-KEY-AUTHORITY"}
    )
    harness.governance.fixture_holder[0] = sign_governance(policy)
    with pytest.raises(ProtectionDeniedError):
        await harness.restart().read(SCOPE, value.id, harness.approve(value))
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, receipt)


async def test_shorter_retention_blocks_read_then_redacts_without_rewriting_published_payloads(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    harness.now = 20
    revise(harness.governance, retention_seconds=15, backup_retention_seconds=15)
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, harness.approve(value))
    assert await harness.store.purge_retention() == 1
    assert await harness.store.purge_retention() == 0
    async with harness.sessions() as session:
        row = await session.get(MetadataRow, (SCOPE.workspace_id, SCOPE.user_id, value.id))
        receipt = await session.get(EnterpriseRetention, (SCOPE.workspace_id, SCOPE.user_id, value.id))
        assert row.state == "deleted" and row.payload == {} and row.admission_binding == {}
        assert row.metadata_digest == value.digest
        assert receipt.state == "purged" and receipt.retirement_digest
        assert receipt.expires_at == 3601
        assert await session.scalar(select(DeletionOutbox.id)) is not None


async def test_storage_disable_still_allows_retirement_of_previously_admitted_metadata(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    revise(harness.governance, storage="deny")
    with pytest.raises(ProtectionDeniedError):
        await harness.store.read(SCOPE, value.id, harness.approve(value))
    assert await harness.store.purge_retention() == 1


async def test_control_lineage_or_vector_storage_denial_cannot_be_hidden_by_metadata_allow(harness):
    value = record(derived=True, embedding=True)
    revise(harness.governance, kind="vector", storage="deny")
    with pytest.raises(ProtectionDeniedError):
        await harness.store.put(value, harness.approve(value, write=True))
    assert await counts(harness.sessions) == (0, 0, 0, 0)


async def test_new_backup_policy_applies_to_old_backup_receipts(harness, tmp_path):
    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "backup-policy.db"
    receipt = maint.backup(backup)
    revise(harness.governance, backup="deny", backup_retention_seconds=0)
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, receipt)


async def test_vector_backup_denial_prevents_copy_even_when_metadata_backup_is_allowed(harness, tmp_path):
    value = record(derived=True, embedding=True)
    await harness.store.put(value, harness.approve(value, write=True))
    revise(harness.governance, kind="vector", backup="deny", backup_retention_seconds=0)
    target = tmp_path / "vector-backup-denied.db"
    with pytest.raises(ProtectionDeniedError):
        maintenance(harness, tmp_path).backup(target)
    assert not target.exists()


async def test_remote_retirement_survives_local_purge_crash_and_retry(harness):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    harness.now = 20
    revise(harness.governance, retention_seconds=15, backup_retention_seconds=15)
    engine = harness.sessions.kw["bind"]

    def fail(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("UPDATE enterprise_metadata SET"):
            raise OperationalError(statement, parameters, RuntimeError("PRIVATE-SQLITE-ERROR"))

    event.listen(engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(ProtectionAdapterError, match="transaction failed"):
            await harness.store.purge_retention()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail)
    async with harness.sessions() as session:
        receipt = await session.get(EnterpriseRetention, (SCOPE.workspace_id, SCOPE.user_id, value.id))
        assert receipt.state == "active"
        assert await session.scalar(select(DeletionOutbox.id)) is None
    with pytest.raises(ProtectionDeniedError):
        await harness.restart().read(SCOPE, value.id, harness.approve(value))
    assert await harness.store.purge_retention() == 1


async def test_wal_repacking_removes_fixture_bytes_but_is_not_media_erasure(harness, tmp_path):
    sentinel = "H4-SYNTHETIC-METADATA-" + "Q" * 200
    value = record(sources=(source(resource=resource(resource_id=sentinel)),))
    await harness.store.put(value, harness.approve(value, write=True))
    maint = maintenance(harness, tmp_path)
    paths = (maint.path, Path(str(maint.path) + "-wal"))
    assert any(sentinel.encode() in path.read_bytes() for path in paths if path.exists())
    await harness.store.invalidate(harness.change(value, reason="source_deleted"))
    assert await harness.store.purge_retention() == 1
    await harness.sessions.kw["bind"].dispose()
    result = maint.sanitize()
    assert result.database_repacked and result.wal_truncated and result.integrity_checked
    assert not result.media_erasure_verified and not result.key_destruction_verified
    assert not result.backup_copies_erased and not result.vector_files_sanitized
    assert all(sentinel.encode() not in path.read_bytes() for path in paths if path.exists())
    with sqlite3.connect(maint.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT count(*) FROM enterprise_source_fence WHERE blocked=1").fetchone()[
            0
        ]


async def test_busy_wal_never_produces_a_successful_sanitization_report(harness, tmp_path):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    maint = maintenance(harness, tmp_path)
    reader = sqlite3.connect(maint.path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM enterprise_metadata").fetchone()
        newer = record(identifier="newer")
        await harness.store.put(newer, harness.approve(newer, write=True))
        with pytest.raises(ProtectionDeniedError, match="WAL is busy"):
            maint.sanitize()
    finally:
        reader.close()


async def test_backup_restore_requires_current_keys_inventory_and_nonrestorable_revocations(
    harness, tmp_path
):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    calls = []
    maint = maintenance(harness, tmp_path, authorize=lambda *arguments: calls.append(arguments))
    backup = tmp_path / "governed-backup.db"
    receipt = maint.backup(backup)
    assert receipt.expires_at == harness.now + 300
    maint.inspect_restore(backup, receipt)
    assert {call[0] for call in calls} == {"backup", "restore"}
    await harness.store.invalidate(harness.change(value))
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, receipt)
    assert backup.exists()
    assert all(call[1] == maint.binding for call in calls)


async def test_forged_backup_receipt_is_not_restore_authority(harness, tmp_path):
    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "backup.db"
    receipt = maint.backup(backup)
    with backup.open("ab") as output:
        output.write(b"tampered")
    forged = receipt.model_copy(update={"content_digest": hashlib.sha256(backup.read_bytes()).hexdigest()})
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, forged)


async def test_unavailable_key_authority_blocks_restore_before_reading_backup_bytes(
    harness, tmp_path, monkeypatch
):
    from shovel.db import governance_maintenance

    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "unavailable-key.db"
    receipt = maint.backup(backup)
    harness.storage_authority.unavailable = True

    def unexpected_read(_path):
        pytest.fail("Backup bytes were read before storage/key authorization.")

    monkeypatch.setattr(governance_maintenance, "_file_digest", unexpected_read)
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, receipt)


async def test_h3_source_revocation_survives_old_journal_restore_without_metadata_rows(
    harness, tmp_path, signer
):
    harness.now = 1000
    supplied = signer(policy_document())
    service = OrganizationService(
        harness.sessions,
        SimpleNamespace(),
        SimpleNamespace(),
        runtime_version="0.1.2",
        loader=lambda: supplied,
        governance=harness.governance,
        clock=lambda: harness.now,
    )
    await service.refresh_policy()
    verified = service._trusted()
    connection = verified.policy.connections[0]
    observation = parse_metadata(response_body(), connection, harness.now)
    await service.journal.observe(verified, connection, observation)
    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "source-journal-backup.db"
    receipt = maint.backup(backup)
    maint.inspect_restore(backup, receipt)
    await service.journal.revoke(verified, connection)
    with pytest.raises(ProtectionDeniedError):
        maint.inspect_restore(backup, receipt)
    with pytest.raises(ProtectionDeniedError):
        await service.journal.observe(verified, connection, observation)


async def test_old_backup_cannot_discard_a_later_uncertain_external_effect(harness, effects, tmp_path):
    maint = maintenance(harness, tmp_path)
    backup = tmp_path / "before-first-effect.db"
    receipt = maint.backup(backup)
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)
    assert (await effects.facts(queued.id))[0].state == "uncertain"
    with pytest.raises(ProtectionDeniedError, match="dedicated recovery adapter"):
        maint.inspect_restore(backup, receipt)


async def test_operator_or_runtime_lock_denial_prevents_backup_and_sanitization(harness, tmp_path):
    def deny(*_arguments):
        raise ProtectionDeniedError("Operator maintenance authority missing.")

    maint = maintenance(harness, tmp_path, authorize=deny)
    target = tmp_path / "denied.db"
    with pytest.raises(ProtectionDeniedError, match="Operator"):
        maint.backup(target)
    assert not target.exists()
    with pytest.raises(ProtectionDeniedError):
        maint.sanitize()
    maint = maintenance(harness, tmp_path)
    from shovel.runtime.errors import RuntimeFailure

    with RuntimeLock(maint.settings), pytest.raises(RuntimeFailure):
        maint.sanitize()


async def test_restore_cannot_auto_adopt_pre_h4_metadata(harness, tmp_path):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    engine = harness.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE enterprise_retention"))
        await connection.execute(text("DELETE FROM schema_migration WHERE version=21"))
    await upgrade_database(engine)
    with pytest.raises(ProtectionDeniedError, match="Legacy"):
        await harness.restart().read(SCOPE, value.id, harness.approve(value))
    with pytest.raises(ProtectionDeniedError, match="Legacy"):
        maintenance(harness, tmp_path).backup(tmp_path / "legacy.db")
    assert not (tmp_path / "legacy.db").exists()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE enterprise_retention SET expires_at=expires_at+1",
        "DELETE FROM enterprise_retention",
        "INSERT OR REPLACE INTO enterprise_retention SELECT * FROM enterprise_retention",
        "INSERT OR REPLACE INTO enterprise_metadata SELECT * FROM enterprise_metadata",
        "INSERT OR REPLACE INTO enterprise_source_fence SELECT * FROM enterprise_source_fence",
    ],
)
async def test_retention_and_security_tombstones_cannot_be_extended_or_replaced(harness, statement):
    value = record()
    await harness.store.put(value, harness.approve(value, write=True))
    async with harness.sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text(statement))
        await session.rollback()


async def test_h4_native_vector_cleanup_survives_reopen_but_does_not_certify_file_erasure(
    harness, enterprise_collection, tmp_path
):
    value = record(derived=True, embedding=True)
    await harness.store.put(value, harness.approve(value, write=True))
    insert_fixture(enterprise_collection, value)
    await harness.store.invalidate(harness.change(value))
    assert await harness.store.purge_retention() == 1
    cleanup = IndexCleanupService(
        harness.sessions, enterprise_sink=ZvecEnterpriseDeletionSink(enterprise_collection)
    )
    assert await cleanup.drain() == 2
    enterprise_collection.close()
    reopened = zvec.open(str(tmp_path / "enterprise"))
    try:
        assert reopened.fetch([value.vector_id]) == {}
    finally:
        reopened.close()
    with pytest.raises(ProtectionDeniedError):
        await harness.restart().read(
            SCOPE,
            value.id,
            harness.approve(value),
            candidate=MetadataCandidate(
                record_id=value.id, revision=1, metadata_digest=value.digest, embedding=value.embedding
            ),
        )


async def test_signed_h3_catalog_without_h4_storage_authority_stays_closed(catalog, signer):
    supplied = signer(policy_document())
    service = OrganizationService(
        catalog.sessions,
        SimpleNamespace(),
        SimpleNamespace(),
        runtime_version="0.1.2",
        loader=lambda: supplied,
        governance=GovernanceService(),
        clock=lambda: 1000,
    )
    await service.refresh_policy()
    from shovel.runtime.errors import RuntimeFailure

    with pytest.raises(RuntimeFailure, match="trust is unavailable"):
        service.catalog.list()
    async with catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM organization_revision")) == 0
        assert await session.scalar(text("SELECT count(*) FROM enterprise_metadata")) == 0
