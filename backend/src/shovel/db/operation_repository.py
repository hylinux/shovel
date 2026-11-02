from __future__ import annotations

import builtins
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.model.runtime import Operation
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.operation_reads import operation_view


class SQLiteOperationReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get(self, identifier: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = await session.scalar(
                select(Operation).where(
                    Operation.id == identifier,
                    Operation.capability_version_id.is_(None),
                    Operation.execution_data.is_(None),
                )
            )
            if row is None:
                raise NotFound("Operation", identifier)
            return operation_view(row)

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        state: str | None,
        resource_id: str | None,
    ) -> list[dict[str, Any]]:
        items, _ = await self._page(limit=limit, offset=offset, state=state, resource_id=resource_id)
        return items

    async def page(
        self,
        *,
        limit: int,
        offset: int,
        state: str | None,
        resource_id: str | None,
    ) -> dict[str, Any]:
        items, total = await self._page(limit=limit, offset=offset, state=state, resource_id=resource_id)
        return {"items": items, "total": total}

    async def _page(
        self,
        *,
        limit: int,
        offset: int,
        state: str | None,
        resource_id: str | None,
    ) -> tuple[builtins.list[dict[str, Any]], int]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise RuntimeFailure("Invalid operation pagination.")
        statement = (
            select(Operation)
            .where(
                Operation.capability_version_id.is_(None),
                Operation.execution_data.is_(None),
            )
            .order_by(Operation.queued_at.desc(), Operation.id)
        )
        if state is not None:
            statement = statement.where(Operation.state == state)
        if resource_id is not None:
            statement = statement.where(Operation.resource_id == resource_id)
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            assert total is not None
            items = [
                operation_view(row) for row in await session.scalars(statement.limit(limit).offset(offset))
            ]
            return items, total
