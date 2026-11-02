import time
from collections.abc import Callable

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.model.identity import IdentityProfile as IdentityRow
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.identity.domain import IdentityDraft, IdentityProfile, IdentityRecord, Revision
from shovel.runtime.errors import NotFound, RuntimeFailure


def _record(row: IdentityRow) -> IdentityRecord:
    return IdentityRecord(
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        profile=IdentityProfile(
            **row.configuration,
            id=row.id,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        ),
    )


class SQLiteIdentityRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sessions, self.clock = sessions, clock

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> IdentityRow:
        row = await session.scalar(
            select(IdentityRow).where(
                IdentityRow.id == identifier,
                IdentityRow.workspace_id == scope.workspace_id,
                IdentityRow.user_id == scope.user_id,
            )
        )
        if row is None:
            raise NotFound("Enterprise identity", identifier)
        return row

    async def create(self, scope: CapabilityScope, draft: IdentityDraft) -> IdentityRecord:
        draft = IdentityDraft.model_validate(draft)
        async with sqlite_write(self.sessions) as session:
            now = self.clock()
            row = IdentityRow(
                id=new_id("identity"),
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
                configuration=draft.model_dump(mode="json"),
                revision=1,
                created_at=now,
                updated_at=now,
            )
            record = _record(row)
            session.add(row)
            await session.commit()
            return record

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        draft: IdentityDraft,
        *,
        expected_revision: int,
    ) -> IdentityRecord:
        draft = IdentityDraft.model_validate(draft)
        expected_revision = TypeAdapter(Revision).validate_python(expected_revision)
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != expected_revision:
                raise RuntimeFailure("Identity revision conflict.", status=409)
            row.configuration = draft.model_dump(mode="json")
            row.revision += 1
            row.updated_at = max(row.updated_at, self.clock())
            record = _record(row)
            await session.commit()
            return record

    async def disable(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        expected_revision: int,
    ) -> IdentityRecord:
        profile = (await self.get(scope, identifier)).profile
        draft = IdentityDraft.model_validate(
            {name: getattr(profile, name) for name in IdentityDraft.model_fields}
        )
        return await self.update(scope, identifier, draft, expected_revision=expected_revision)

    async def get(self, scope: CapabilityScope, identifier: str) -> IdentityRecord:
        async with self.sessions() as session:
            return _record(await self._find(session, scope, identifier))

    async def list(self, scope: CapabilityScope) -> list[IdentityRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(IdentityRow)
                .where(
                    IdentityRow.workspace_id == scope.workspace_id,
                    IdentityRow.user_id == scope.user_id,
                )
                .order_by(IdentityRow.id)
            )
            return [_record(row) for row in rows]
