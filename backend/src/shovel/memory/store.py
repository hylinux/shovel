#---------------------------------------------------------------------
# 记忆存储的生命周期(mem0 + 本地 Qdrant)
#
# 这里刻意分成两个入口:
#
#   init_memory_store()  —— 只建 Qdrant collection, 不碰 LLM/embedder
#   open_memory()        —— 构造完整的 mem0 Memory, 需要模型可用
#
# 分开的理由很实在: 'shovel init' 应该在用户还没填 API Key 时就能跑完。
# mem0 的 Memory() 在构造期就会实例化 embedder, 缺 key 直接抛异常 ——
# 若 init 走 Memory(), 用户就被迫先配模型才能完成初始化, 而初始化本
# 该是配模型之前的那一步。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shovel.config.memory_config import MemorySettings
from shovel.config.model_settings import DefaultAgentModelSettings
from shovel.exceptions.dependency import MissingDependencyError

from .config import build_memory_config, memory_paths

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注, 运行期不导入 mem0
    from mem0 import Memory


_INSTALL_HINT = "uv sync  # 或: uv add mem0ai qdrant-client"


def _require_mem0() -> Any:
    """延迟导入 mem0。

    延迟而非模块级导入: mem0 会连带拉起 openai / qdrant-client / sqlite 等一堆
    依赖, 让 'shovel version' 这种命令也为此付出启动时间是不划算的。
    """

    try:
        from mem0 import Memory
    except ImportError as e:
        raise MissingDependencyError(
            "记忆子系统需要 mem0, 但它没有安装。",
            hint=_INSTALL_HINT,
            details=str(e),
        ) from e

    return Memory


def _require_qdrant_client() -> Any:
    try:
        from qdrant_client import QdrantClient
    except ImportError as e:
        raise MissingDependencyError(
            "记忆子系统需要 qdrant-client, 但它没有安装。",
            hint=_INSTALL_HINT,
            details=str(e),
        ) from e

    return QdrantClient


def _require_mem0_qdrant() -> Any:
    try:
        from mem0.vector_stores.qdrant import Qdrant
    except ImportError as e:
        raise MissingDependencyError(
            "记忆子系统需要 mem0 与 qdrant-client, 但它们没有安装。",
            hint=_INSTALL_HINT,
            details=str(e),
        ) from e

    return Qdrant


@dataclass(slots=True)
class MemoryStoreInfo:
    """记忆存储的初始化结果。"""

    collection: str
    location: str        # local 模式是目录, 否则是 host:port / url
    mode: str            # local / server / cloud
    dim: int
    created: bool        # True = 本次新建, False = 已存在
    history_db: Path


def _connection_kwargs(settings: MemorySettings) -> dict[str, Any]:
    """探测用的 QdrantClient 连接参数(不含 collection 相关配置)。"""

    qdrant = settings.qdrant

    if qdrant.mode == "local":
        return {"path": qdrant.path}

    if qdrant.mode == "server":
        return {"host": qdrant.host, "port": qdrant.port}

    return {"url": qdrant.url, "api_key": qdrant.api_key}


def _location_of(settings: MemorySettings) -> str:
    qdrant = settings.qdrant

    if qdrant.mode == "local":
        return qdrant.path

    if qdrant.mode == "server":
        return f"{qdrant.host}:{qdrant.port}"

    return str(qdrant.url)


def _collection_exists(settings: MemorySettings) -> bool:
    """先探一次, 只为了能如实告诉用户"新建"还是"已存在"。

    探测必须在 mem0 建库之前完成并关闭连接: 本地 Qdrant 以目录为单位
    加锁, 同一目录被两个 client 同时打开会直接报 "already accessed by
    another instance"。
    """

    client_cls = _require_qdrant_client()

    client = client_cls(**_connection_kwargs(settings))

    try:
        return bool(client.collection_exists(settings.collection))
    finally:
        client.close()


def init_memory_store(settings: MemorySettings) -> MemoryStoreInfo:
    """确保记忆用的 Qdrant collection 就绪。幂等: 已存在时不重建。

    重建等于丢掉全部记忆 —— 而记忆和知识不同, 它没有可以重新抽取的源文件,
    删了就是真的没了。

    collection 交给 mem0 自己的 Qdrant 封装来建, 不手写 create_collection:
    mem0 除 dense 向量外还要一个名为 ``bm25`` 的稀疏向量槽, 我们自己建的
    collection 会缺这个槽, 之后 mem0 写入时才会报 "Not existing vector name"。
    """

    for path in memory_paths(settings):
        path.mkdir(parents=True, exist_ok=True)

    existed = _collection_exists(settings)

    qdrant_cls = _require_mem0_qdrant()

    store = qdrant_cls(
        **settings.qdrant.client_kwargs(
            collection=settings.collection,
            dim=settings.dim,
        )
    )

    # init 只负责"建好", 不长期持有句柄:
    # 本地模式下不关闭会一直锁着目录, 紧接着的 'shovel run' 就打不开了。
    store.client.close()

    return MemoryStoreInfo(
        collection=settings.collection,
        location=_location_of(settings),
        mode=settings.qdrant.mode,
        dim=settings.dim,
        created=not existed,
        history_db=Path(settings.history_db_path).expanduser(),
    )


def open_memory(
        settings: MemorySettings,
        model: DefaultAgentModelSettings,
) -> Memory:
    """构造可读写的 mem0 Memory。

    与 init 不同, 这里会实例化 LLM 与 embedder, 因此要求模型配置可用。
    """

    memory_cls = _require_mem0()

    return memory_cls.from_config(build_memory_config(settings, model))


__all__ = ["MemoryStoreInfo", "init_memory_store", "open_memory"]
