#---------------------------------------------------------------------
# 定义在应用生命周期中使用的DI 容器
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from dependency_injector import containers, providers

from shovel.db.database import (
    create_database_engine,
    create_session_factory,
)


class AppContainer(containers.DeclarativeContainer):

    # Settings
    config = providers.Configuration(strict=True)

    # Resource
    # 需要SQLite的DB
    db_engine = providers.Resource(
        create_database_engine,
        url = config.database.url,
        echo = config.database.echo,
    )

    # 数据库的session
    session_factory = providers.Resource(
        create_session_factory,
        engine = db_engine,
    )


