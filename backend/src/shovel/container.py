#---------------------------------------------------------------------
# 定义在应用生命周期中使用的DI 容器
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from dependency_injector import containers, providers

from shovel.config.embedding_config import EmbeddingSettings
from shovel.db.database import (
    create_async_database_engine,
    create_async_session_factory,
)
from shovel.pipeline.embedder import build_embedder
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import open_chunk_sink
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

    # embedding 的配置对象
    #
    # 容器的 config 是被 dump 成 dict 的, 而 build_embedder 要的是带校验
    # 的 pydantic 模型 —— 这里再 validate 一次, 让"维度对不上""base_url
    # 写错"这类问题在装配阶段就炸, 而不是等第一篇文档送进去才炸。
    embedding_settings = providers.Factory(
        EmbeddingSettings.model_validate,
        config.embedding,
    )

    # 向量化器
    #
    # 把 zvec.dim 传进去做期望维度校验: 两边一旦不一致, 写进 collection
    # 的向量要么被拒要么被截断, 而这在检索阶段才会表现为"召回莫名其妙"。
    embedder = providers.Factory(
        build_embedder,
        embedding_settings,
        expected_dimension = config.zvec.dim,
    )

    # 向量落库口
    #
    # Singleton: 它背后是一个打开的 zvec collection, 同一个进程里重复打开
    # 同一个目录既浪费句柄也容易撞上文件锁。
    chunk_sink = providers.Singleton(
        open_chunk_sink,
        config.zvec.knowledge_path,
    )

    # 扫描服务
    #
    # Factory: 每次扫描是一次独立的作业, 服务本身不跨作业持有状态。
    # 注意 embedder 与 sink 的释放不归容器管 —— 调用方跑完要各自 aclose。
    scan_service = providers.Factory(
        ScanService,
        session_factory,
        resource_service,
        embedder = embedder,
        sink = chunk_sink,
    )


