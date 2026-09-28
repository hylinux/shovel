#---------------------------------------------------------
# 定义数据访问层
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

#: Per-connection pragmas. journal_mode is persistent (stored in the file
#: header); the rest must be re-applied on every connection.
PRAGMAS = {
    "journal_mode": "WAL",       # concurrent readers while a scan writes
    "foreign_keys": "ON",        # OFF by default in SQLite -- cascades silently fail
    "synchronous": "NORMAL",     # WAL makes FULL unnecessary for our durability needs
    "busy_timeout": "10000",     # scanner and UI do collide; wait rather than fail
    "temp_store": "MEMORY",
    "cache_size": "-64000",      # ~64 MB: helpful, still polite on a laptop
    "mmap_size": "268435456",    # 256 MB
}



def create_database_engine(url: str,
                  *,
                  echo: bool | None = False,
                  ) -> Engine:
    if not url:
        raise ValueError(
            "database url 为空。请检查 settings.toml 中 database.url 是否配置, "
            "以及 AppContainer.config 是否已 from_dict(settings.model_dump(mode='json'))。"
        )

    engine: Engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False},
    )

    event.listen(engine, "connect", _apply_pragmas)

    return engine





async def create_async_database_engine(
        url: str,
        *,
        echo: bool | None = False,      # config 可能注入 None
) -> AsyncIterator[AsyncEngine]:
    """
    创建并管理数据库 Engine 生命周期.
    """

    # strict=True 只拦"key 不存在"; 这里拦"值为空/无效",
    # 两道防线互补, 且把排查方向写进报错信息
    if not url:
        raise ValueError(
            "database url 为空。请检查 settings.toml 中 database.url 是否配置, "
            "以及 AppContainer.config 是否已 from_dict(settings.model_dump(mode='json'))。"
        )

    engine: AsyncEngine = create_async_engine(
        url,
        echo=echo,
    )

    event.listen(engine.sync_engine, "connect", _apply_pragmas)

    try:
        yield engine
    finally:
        await engine.dispose()


def create_async_session_factory(
        engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """
    创建AsyncSession factory.

    注意: 在 AppContainer 中必须注册为 providers.Resource 而非 Singleton,
    否则 shutdown_resources() 之后会缓存住已 dispose 的 engine。
    """

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)



def _apply_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    for key, value in PRAGMAS.items():
        cur.execute(f"PRAGMA {key}={value}")
    cur.close()

