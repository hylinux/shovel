#---------------------------------------------------------------------
# Zvec 运行时与 collection 的生命周期
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zvec
from zvec import Collection, CollectionSchema

from shovel.config.zvec_config import ZvecSettings

from .schema import chunk_collection_schema

#: zvec.init() 进程内只允许调用一次, 第二次会抛 RuntimeError。
#: CLI 里 init / run 可能都想初始化, 所以在这里做一次幂等收口。
_runtime_ready = False


@dataclass(slots=True)
class CollectionInfo:
    """一个 collection 的初始化结果。"""

    name: str
    path: Path
    created: bool          # True = 本次新建, False = 已存在直接打开
    collection: Collection


def init_zvec_runtime(
        *,
        log_dir: Path | str | None = None,
        log_level: zvec.LogLevel | None = None,
) -> None:
    """初始化 Zvec 运行时。幂等: 重复调用是空操作。

    必须在任何 collection 操作之前调用。
    """

    global _runtime_ready

    if _runtime_ready:
        return

    kwargs: dict[str, Any] = {}

    if log_dir is not None:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        kwargs["log_type"] = zvec.LogType.FILE
        kwargs["log_dir"] = str(path)
        kwargs["log_basename"] = "zvec.log"

    if log_level is not None:
        kwargs["log_level"] = log_level

    # 同一进程里已经 init 过(例如被 run 命令先初始化), 不是错误
    with contextlib.suppress(RuntimeError):
        zvec.init(**kwargs)

    _runtime_ready = True


def _exists(path: Path) -> bool:
    """collection 目录已经装了东西, 就认为它已经建过了。

    只判断目录存在是不够的: 用户可能先手工建了空目录, 那种情况下
    create_and_open 仍然应该跑。
    """
    return path.is_dir() and any(path.iterdir())


def ensure_collection(path: Path | str, schema: CollectionSchema) -> CollectionInfo:
    """打开 collection; 不存在则按 schema 创建。

    这是 init 命令用的入口 —— 已存在时绝不重建, 因为重建等于丢掉全部向量,
    而重新 embedding 是整条流水线里最贵的一步。
    """

    target = Path(path).expanduser()

    if _exists(target):
        collection = zvec.open(str(target))
        return CollectionInfo(schema.name, target, created=False, collection=collection)

    target.parent.mkdir(parents=True, exist_ok=True)
    collection = zvec.create_and_open(str(target), schema)

    return CollectionInfo(schema.name, target, created=True, collection=collection)


def open_collection(path: Path | str, *, read_only: bool = False) -> Collection:
    """打开一个已存在的 collection。"""

    target = Path(path).expanduser()

    if not _exists(target):
        raise FileNotFoundError(
            f"向量集合不存在: {target}\n"
            f"  执行 'shovel init' 完成初始化。"
        )

    return zvec.open(str(target), zvec.CollectionOption(read_only=read_only))


def init_vector_store(settings: ZvecSettings) -> list[CollectionInfo]:
    """按配置初始化 Zvec 运行时与知识库 collection。

    记忆不在此处: 它由 mem0 写入本地 Qdrant, 见 :mod:`shovel.memory`。

    返回每个 collection 的初始化结果, 由调用方(CLI)决定怎么展示。
    """

    init_zvec_runtime(log_dir=settings.log_dir or None)

    specs: list[tuple[str, CollectionSchema]] = [
        (
            settings.knowledge_path,
            chunk_collection_schema(
                settings.chunk_collection,
                dim=settings.dim,
                metric=settings.metric,
                hnsw_m=settings.hnsw_m,
                hnsw_ef_construction=settings.hnsw_ef_construction,
            ),
        ),
    ]

    results: list[CollectionInfo] = []

    for path, schema in specs:
        info = ensure_collection(path, schema)
        # init 只负责"建好", 不长期持有句柄: 真正的读写由服务层自己打开
        info.collection.close()
        results.append(info)

    return results
