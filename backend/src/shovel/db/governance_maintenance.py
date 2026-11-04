"""Offline, operator-authorized maintenance of the existing SQLite store.

No HTTP/Agent entry point or production storage authority is installed by this module.
Successful file maintenance is explicitly NOT a media-erasure certificate.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import make_url

from shovel.capability.domain import CapabilityScope, Digest, Identifier
from shovel.config.settings import AppSettings
from shovel.db.database import sqlite_storage_binding
from shovel.enterprise.domain import EnterpriseResourceRef
from shovel.identity.domain import Generation, UnixSeconds
from shovel.protection.domain import ProtectionDeniedError, ProtectionValue, content_digest
from shovel.protection.governance import GovernanceService, StorageOperation
from shovel.protection.persistence import EnterpriseMetadata, source_key
from shovel.runtime.locking import RuntimeLock


class SQLiteSanitization(ProtectionValue):
    database_repacked: bool
    wal_truncated: bool
    integrity_checked: bool
    media_erasure_verified: Literal[False] = False
    key_destruction_verified: Literal[False] = False
    backup_copies_erased: Literal[False] = False
    vector_files_sanitized: Literal[False] = False


class BackupReceipt(ProtectionValue):
    content_digest: Digest
    destination_binding: Digest
    policy_digest: Digest
    storage_id: Identifier
    key_authority_id: Identifier
    authority_generation: Generation
    created_at: UnixSeconds
    expires_at: UnixSeconds


class GovernedSQLiteMaintenance:
    def __init__(
        self,
        settings: AppSettings,
        governance: GovernanceService,
        authorize: Callable[[StorageOperation, str, str], None],
    ) -> None:
        database = make_url(settings.database.url).database
        if not database or database == ":memory:":
            raise ProtectionDeniedError("Maintenance requires an existing bound SQLite file.")
        self.path = Path(database).expanduser().resolve(strict=True)
        self.binding = sqlite_storage_binding(self.path)
        self.settings, self.governance, self.authorize = settings, governance, authorize

    def _permit(self, operation: StorageOperation, target: Path, *, record_digest: str | None = None) -> None:
        self.authorize(operation, self.binding, sqlite_storage_binding(target))
        if operation == "restore" and record_digest is None:
            raise ProtectionDeniedError("Restore requires an inventoried backup identity.")
        policy, _ = self.governance.policy()
        self.governance.require(
            scope=policy.scope,
            store_binding=self.binding,
            kind="control_lineage",
            record_digest=(
                record_digest
                if record_digest is not None
                else content_digest({"target": sqlite_storage_binding(target)})
            ),
            created_at=self.governance.clock(),
            operation=operation,
            destination_binding=sqlite_storage_binding(target),
        )

    def _inspect(
        self,
        connection: sqlite3.Connection,
        scope: CapabilityScope,
        *,
        operation: Literal["backup", "restore"],
        target: Path,
        backup_created_at: float,
    ) -> float:
        policy, _ = self.governance.policy()
        deadline = policy.expires_at
        if operation == "restore" and connection.execute("SELECT 1 FROM external_effect LIMIT 1").fetchone():
            raise ProtectionDeniedError("External-effect journals require a dedicated recovery adapter.")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ProtectionDeniedError("SQLite integrity validation failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ProtectionDeniedError("SQLite ownership/lineage validation failed.")
        owners = connection.execute(
            "SELECT workspace_id,user_id FROM enterprise_metadata UNION "
            "SELECT workspace_id,user_id FROM organization_source UNION "
            "SELECT workspace_id,user_id FROM organization_revision UNION "
            "SELECT workspace_id,user_id FROM enterprise_source_fence UNION "
            "SELECT workspace_id,user_id FROM enterprise_retention UNION "
            "SELECT workspace_id,user_id FROM protection_audit"
        )
        if any(tuple(row) != (scope.workspace_id, scope.user_id) for row in owners):
            raise ProtectionDeniedError("Maintenance cannot export another enterprise owner's store.")
        import json

        if connection.execute(
            "SELECT 1 FROM enterprise_metadata WHERE state!='active' AND payload!='{}' LIMIT 1"
        ).fetchone():
            raise ProtectionDeniedError("Pending enterprise redaction prevents backup/restore admission.")
        for resource, blocked in connection.execute("SELECT resource,blocked FROM organization_source"):
            if blocked:
                raise ProtectionDeniedError("Retained source tombstones require storage-authority cleanup.")
            source = EnterpriseResourceRef.model_validate(json.loads(resource))
            evidence, limit, _ = self.governance.require(
                scope=scope,
                store_binding=self.binding,
                kind="control_lineage",
                record_digest=content_digest({"source": source_key(source)}),
                source_keys=(source_key(source),),
                created_at=self.governance.clock(),
                operation=operation,
                destination_binding=sqlite_storage_binding(target),
                backup_created_at=backup_created_at,
            )
            if (
                evidence.storage_id != policy.storage_id
                or evidence.key_authority_id != policy.key_authority_id
            ):
                raise ProtectionDeniedError("Backup key authority changed during inspection.")
            deadline = min(deadline, limit)
        rows = connection.execute(
            """SELECT m.payload,m.metadata_digest,r.authority_generation,r.expires_at,r.store_binding,
            r.state,r.storage_id,r.key_authority_id
            FROM enterprise_metadata m LEFT JOIN enterprise_retention r
            ON m.workspace_id=r.workspace_id AND m.user_id=r.user_id AND m.id=r.metadata_id
            WHERE m.state='active'"""
        )
        for payload, digest, generation, expiry, binding, state, storage_id, key_authority_id in rows:
            if (
                generation is None
                or binding != self.binding
                or state != "active"
                or storage_id != policy.storage_id
                or key_authority_id != policy.key_authority_id
            ):
                raise ProtectionDeniedError("Legacy or differently stored metadata remains quarantined.")
            record = EnterpriseMetadata.model_validate(json.loads(payload))
            if record.scope != scope or record.digest != digest:
                raise ProtectionDeniedError("Backup metadata ownership/digest is invalid.")
            for kind in record.storage_classes:
                evidence, limit, _ = self.governance.require(
                    scope=scope,
                    store_binding=self.binding,
                    kind=kind,
                    record_digest=content_digest({"id": record.id, "metadata": record.digest}),
                    source_keys=tuple(source_key(source.resource) for source in record.context.sources),
                    created_at=record.created_at,
                    operation=operation,
                    destination_binding=sqlite_storage_binding(target),
                    backup_created_at=backup_created_at,
                    expected_generation=generation,
                    expires_at=expiry,
                    retention_policy_ref=record.context.retention_policy_ref,
                    protection_policy_ref=record.context.policy_source_ref,
                )
                if evidence.storage_id != storage_id or evidence.key_authority_id != key_authority_id:
                    raise ProtectionDeniedError("Backup key authority changed during inspection.")
                deadline = min(deadline, limit)
        auxiliary_classes: list[Literal["control_lineage", "audit"]] = ["control_lineage"]
        if connection.execute("SELECT 1 FROM protection_audit LIMIT 1").fetchone():
            auxiliary_classes.append("audit")
        for kind in auxiliary_classes:
            _, limit, _ = self.governance.require(
                scope=scope,
                store_binding=self.binding,
                kind=kind,
                record_digest=content_digest({"inspection": kind}),
                created_at=self.governance.clock(),
                operation=operation,
                destination_binding=sqlite_storage_binding(target),
                backup_created_at=backup_created_at,
            )
            deadline = min(deadline, limit)
        return deadline

    def backup(self, destination: Path) -> BackupReceipt:
        target = destination.expanduser().resolve()
        if target == self.path or target.exists():
            raise ProtectionDeniedError("Backup destination must be a new, explicitly authorized file.")
        self._permit("backup", target)
        policy, initial_policy_digest = self.governance.policy()
        created_at = self.governance.clock()
        with RuntimeLock(self.settings), closing(sqlite3.connect(self.path)) as source:
            source_deadline = self._inspect(
                source, policy.scope, operation="backup", target=target, backup_created_at=created_at
            )
            # Exclusive create prevents an existing target from being overwritten.
            with target.open("xb"):
                pass
            with closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
                copy_deadline = self._inspect(
                    copy, policy.scope, operation="backup", target=target, backup_created_at=created_at
                )
            with target.open("r+b") as output:
                os.fsync(output.fileno())
            digest = _file_digest(target)
            evidence, deadline, policy_digest = self.governance.require(
                scope=policy.scope,
                store_binding=self.binding,
                kind="metadata",
                record_digest=digest,
                created_at=created_at,
                operation="backup",
                destination_binding=sqlite_storage_binding(target),
            )
            if policy_digest != initial_policy_digest:
                raise ProtectionDeniedError("Backup governance changed; the copy remains quarantined.")
            return BackupReceipt(
                content_digest=digest,
                destination_binding=sqlite_storage_binding(target),
                policy_digest=policy_digest,
                storage_id=evidence.storage_id,
                key_authority_id=evidence.key_authority_id,
                authority_generation=evidence.generation,
                created_at=created_at,
                expires_at=min(deadline, source_deadline, copy_deadline),
            )

    def inspect_restore(self, backup: Path, receipt: BackupReceipt) -> None:
        """Validate without copying/activating; every actual content use still needs H1."""
        target = backup.expanduser().resolve(strict=True)
        receipt = BackupReceipt.model_validate(receipt)
        self._permit("restore", target, record_digest=receipt.content_digest)
        if (
            _file_digest(target) != receipt.content_digest
            or sqlite_storage_binding(target) != receipt.destination_binding
            or self.governance.clock() >= receipt.expires_at
        ):
            raise ProtectionDeniedError("Backup identity or retention is invalid; restore quarantined.")
        policy, initial_policy_digest = self.governance.policy()
        if receipt.storage_id != policy.storage_id or receipt.key_authority_id != policy.key_authority_id:
            raise ProtectionDeniedError("Backup storage/key authority changed; explicit migration required.")
        self.governance.require(
            scope=policy.scope,
            store_binding=self.binding,
            kind="metadata",
            record_digest=receipt.content_digest,
            created_at=receipt.created_at,
            operation="restore",
            destination_binding=sqlite_storage_binding(target),
            expected_generation=receipt.authority_generation,
            expires_at=receipt.expires_at,
        )
        with (
            RuntimeLock(self.settings),
            closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as copy,
        ):
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as current:
                if current.execute("SELECT 1 FROM external_effect LIMIT 1").fetchone():
                    raise ProtectionDeniedError(
                        "External-effect journals require a dedicated recovery adapter."
                    )
            self._inspect(
                copy,
                policy.scope,
                operation="restore",
                target=target,
                backup_created_at=receipt.created_at,
            )
            if (
                self.governance.policy()[1] != initial_policy_digest
                or _file_digest(target) != receipt.content_digest
            ):
                raise ProtectionDeniedError("Backup or governance changed; restore remains quarantined.")

    def sanitize(self) -> SQLiteSanitization:
        self._permit("sanitize", self.path)
        with (
            RuntimeLock(self.settings),
            closing(sqlite3.connect(self.path, isolation_level=None)) as connection,
        ):
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA synchronous=FULL")
            before = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if before is None or before[0] != 0:
                raise ProtectionDeniedError("SQLite WAL is busy; sanitization was not completed.")
            connection.execute("VACUUM")
            after = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if after != (0, 0, 0):
                raise ProtectionDeniedError("SQLite WAL truncation was not completed.")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ProtectionDeniedError("SQLite integrity failed after repacking.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ProtectionDeniedError("SQLite lineage failed after repacking.")
            with self.path.open("r+b") as output:
                os.fsync(output.fileno())
        return SQLiteSanitization(database_repacked=True, wal_truncated=True, integrity_checked=True)


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
