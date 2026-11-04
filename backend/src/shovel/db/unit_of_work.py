from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def sqlite_write(
    sessions: async_sessionmaker[AsyncSession], *, durable: bool = False
) -> AsyncIterator[AsyncSession]:
    """Serialize a write unit of work. Explicit commit is required; otherwise roll back."""
    session = sessions()
    try:
        if durable:
            await session.execute(text("PRAGMA synchronous=FULL"))
            await session.execute(text("PRAGMA secure_delete=ON"))
        await session.execute(text("BEGIN IMMEDIATE"))
        yield session
    finally:
        with CancelScope(shield=True):
            await session.close()
