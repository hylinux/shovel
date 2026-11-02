import time
from collections.abc import Callable

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.model.enterprise import EnterpriseConnectionProfile as ConnectionRow
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.enterprise.domain import (
    ConnectionDraft,
    ConnectionRecord,
    EnterpriseConnectionProfile,
    ProviderManifest,
)
from shovel.identity.domain import Revision
from shovel.runtime.errors import NotFound, RuntimeFailure


def _record(row: ConnectionRow) -> ConnectionRecord:
    profile = EnterpriseConnectionProfile(
        **row.configuration,
        id=row.id,
        enabled=row.enabled,
        status=row.status,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    return ConnectionRecord(
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        profile=profile,
        manifest_version=row.manifest_version,
        manifest_digest=row.manifest_digest,
    )


class SQLiteConnectionRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> ConnectionRow:
        row = await session.scalar(
            select(ConnectionRow).where(
                ConnectionRow.id == identifier,
                ConnectionRow.workspace_id == scope.workspace_id,
                ConnectionRow.user_id == scope.user_id,
            )
        )
        if row is None:
            raise NotFound("Enterprise connection", identifier)
        return row

    async def create(
        self, scope: CapabilityScope, draft: ConnectionDraft, manifest: ProviderManifest
    ) -> ConnectionRecord:
        draft = ConnectionDraft.model_validate(draft)
        manifest = ProviderManifest.model_validate(manifest)
        async with sqlite_write(self.sessions) as session:
            now = self.clock()
            row = ConnectionRow(
                id=new_id("connection"),
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
                configuration=draft.model_dump(mode="json"),
                enabled=False,
                status="draft",
                revision=1,
                created_at=now,
                updated_at=now,
                manifest_version=manifest.provider_version,
                manifest_digest=manifest.content_hash,
            )
            record = _record(row)
            record.profile.validate_manifest(manifest, require_enabled=False)
            session.add(row)
            await session.commit()
            return record

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        draft: ConnectionDraft,
        manifest: ProviderManifest,
        *,
        expected_revision: int,
    ) -> ConnectionRecord:
        expected_revision = TypeAdapter(Revision).validate_python(expected_revision)
        draft = ConnectionDraft.model_validate(draft)
        manifest = ProviderManifest.model_validate(manifest)
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != expected_revision:
                raise RuntimeFailure("Connection revision conflict.", status=409)
            row.configuration, row.enabled, row.status = draft.model_dump(mode="json"), False, "draft"
            row.manifest_version, row.manifest_digest = manifest.provider_version, manifest.content_hash
            row.revision += 1
            row.updated_at = max(row.updated_at, self.clock())
            record = _record(row)
            record.profile.validate_manifest(manifest, require_enabled=False)
            await session.commit()
            return record

    async def disable(
        self, scope: CapabilityScope, identifier: str, *, expected_revision: int
    ) -> ConnectionRecord:
        expected_revision = TypeAdapter(Revision).validate_python(expected_revision)
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != expected_revision:
                raise RuntimeFailure("Connection revision conflict.", status=409)
            row.status, row.enabled = "disabled", False
            row.revision += 1
            row.updated_at = max(row.updated_at, self.clock())
            record = _record(row)
            await session.commit()
            return record

    async def get(self, scope: CapabilityScope, identifier: str) -> ConnectionRecord:
        async with self.sessions() as session:
            return _record(await self._find(session, scope, identifier))

    async def list(self, scope: CapabilityScope) -> list[ConnectionRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(ConnectionRow)
                .where(
                    ConnectionRow.workspace_id == scope.workspace_id,
                    ConnectionRow.user_id == scope.user_id,
                )
                .order_by(ConnectionRow.id)
            )
            return [_record(row) for row in rows]
