#---------------------------------------------------------
# 定义数据访问层
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


async def create_database_engine(
        url: str,
        *,
        echo: bool = False,
) -> AsyncIterator[AsyncEngine]:
    """
    创建并管理数据库 Engine 生命周期.
    """

    engine: AsyncEngine = create_async_engine(
        url,
        echo=echo,
    )

    try:
        yield engine
    finally:
        await engine.dispose()


def create_session_factory(
        engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """
    创建AsyncSession factory.
    """

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
