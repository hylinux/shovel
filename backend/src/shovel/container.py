#---------------------------------------------------------------------
# 定义在应用生命周期中使用的DI 容器
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from dependency_injector import containers, providers

from shovel.db.database import (
    create_async_database_engine,
    create_async_session_factory,
)
from shovel.services.credentials import build_credential_store
from shovel.services.resource_service import ResourceService


class AppContainer(containers.DeclarativeContainer):

    # Settings
    config = providers.Configuration(strict=True)

    # Resource
    # 需要SQLite的DB
    db_engine = providers.Resource(
        create_async_database_engine,
        url = config.database.url,
        echo = config.database.echo,
    )

    # 数据库的session
    session_factory = providers.Resource(
        create_async_session_factory,
        engine = db_engine,
    )

    # 凭据存储: keyring 优先, env 兜底
    #
    # Singleton 而非 Resource —— 它没有需要释放的句柄, keyring 后端本身
    # 是进程级的。每次都重建只会让惰性 import 的开销白付一遍。
    credential_store = providers.Singleton(
        build_credential_store,
    )

    # 资源服务
    #
    # Factory 而非 Singleton: service 自己不持有状态, 每个方法开自己的
    # session。共享一个实例没有收益, 而独立实例让未来"按请求注入不同
    # credential store"(例如测试、dry-run)成为可能。
    resource_service = providers.Factory(
        ResourceService,
        session_factory = session_factory,
        credentials = credential_store,
    )


