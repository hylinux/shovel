#---------------------------------------------------------------------
# 记忆子系统(mem0 + 本地 Qdrant)配置
#
# 为什么记忆不走 Zvec:
#   记忆层交给 mem0 托管 —— 抽取事实、判断冲突、supersede 旧记忆
#   这套逻辑不值得自己再写一遍。而 mem0 只认自己注册过的 vector store,
#   Zvec 不在其中。硬接需要给 mem0 写一个 provider 插件, 那等于把
#   mem0 的内部接口变成我们的长期负担。
#
#   所以分工是:
#     知识(chunk) -> Zvec      : 量大, 检索逻辑我们自己控
#     记忆(memory) -> Qdrant   : 量小(10^2~10^4), 由 mem0 全权读写
#
#   Qdrant 用嵌入式(local, path=...)模式: 不需要用户装 Docker,
#   也不需要起服务进程, 与 SQLite 一样是"一个目录就是一个库"。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class QdrantSettings(BaseModel):
    """mem0 使用的 Qdrant 后端配置。

    三种模式互斥, 由 ``mode`` 决定:

    * ``local``  —— 嵌入式, 数据落在 ``path`` 指向的目录(默认)
    * ``server`` —— 连接 ``host``/``port`` 上的 Qdrant 服务
    * ``cloud``  —— 连接 ``url`` + ``api_key`` 的托管实例

    默认值是 local: 个人 Agent 的记忆量根本用不上一个独立服务,
    而多一个必须先启动的进程, 就多一种 'shovel run' 起不来的原因。
    """

    mode: Literal["local", "server", "cloud"] = "local"

    #: 嵌入式模式的落盘目录; 留空时由 validator 回落到 ~/.shovel/memory/qdrant
    path: str = ""

    #: server 模式
    host: str | None = None
    port: int | None = None

    #: cloud 模式
    url: str | None = None
    api_key: str | None = None

    #: True = 向量常驻磁盘(省内存), False = 载入内存(更快)。
    #: 记忆量小, 但它与知识库共用一台机器, 默认让内存留给知识库。
    on_disk: bool = True

    @model_validator(mode="after")
    def _check_mode(self) -> QdrantSettings:
        if self.mode == "local":
            if not self.path:
                object.__setattr__(
                    self, "path",
                    (Path.home() / ".shovel" / "memory" / "qdrant").as_posix(),
                )
            return self

        if self.mode == "server" and not (self.host and self.port):
            raise ValueError(
                "memory.qdrant.mode = 'server' 时必须同时提供 host 与 port。"
            )

        if self.mode == "cloud" and not (self.url and self.api_key):
            raise ValueError(
                "memory.qdrant.mode = 'cloud' 时必须同时提供 url 与 api_key。"
            )

        return self

    def client_kwargs(self, *, collection: str, dim: int) -> dict[str, Any]:
        """转成 mem0 的 ``vector_store.config`` 片段。

        mem0 的 QdrantConfig 用 ``validate_extra_fields`` 拒绝任何多余键,
        所以这里只能吐出它认识的字段, 且互斥的几组必须留空(不是 None 就行,
        是连键都不能带上无关值)。
        """

        config: dict[str, Any] = {
            "collection_name": collection,
            "embedding_model_dims": dim,
            "on_disk": self.on_disk,
        }

        if self.mode == "local":
            config["path"] = self.path
        elif self.mode == "server":
            config["host"] = self.host
            config["port"] = self.port
            # path 有默认值 /tmp/qdrant, 不显式置空会被误当成本地库
            config["path"] = None
        else:
            config["url"] = self.url
            config["api_key"] = self.api_key
            config["path"] = None

        return config


class MemoryLlmSettings(BaseModel):
    """mem0 抽取/归并事实时使用的 LLM。

    留空 provider 表示"跟随 [model] 里的默认大模型", 由
    :func:`shovel.memory.config.build_memory_config` 完成映射。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.1
    max_tokens: int = 2000


class MemoryEmbedderSettings(BaseModel):
    """记忆向量化使用的 embedding 模型。

    ⚠️ ``dims`` 必须与 [memory.qdrant] 建 collection 时用的维度一致。
    两者由 :class:`MemorySettings.dim` 统一给出, 不在这里重复声明。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class MemorySettings(BaseModel):
    """记忆子系统总配置(mem0)。

    与 DatabaseSettings / ZvecSettings 一样继承 BaseModel 而非 BaseSettings:
    环境变量由 AppSettings 通过 env_nested_delimiter 统一处理
    (SHOVEL_MEMORY__DIM -> memory.dim)。
    """

    #: collection 名字与 db/model/memory.py 的注释保持一致
    collection: str = "shovel_memory"

    #: 记忆向量维度。换 embedding 模型 = 换维度 = 必须重建 collection。
    #: 与 [zvec] 的 dim 互相独立: 记忆和知识完全可以用不同的模型。
    dim: int = 1024

    #: mem0 自己的历史库(记录每条记忆的 add/update/delete 轨迹)。
    #: 它是 mem0 内部的 SQLite, 与 Shovel 的 data/shovel.db 是两个文件 ——
    #: 不要指到同一个路径, mem0 会按自己的 schema 建表。
    history_db_path: str = ""

    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    llm: MemoryLlmSettings = Field(default_factory=MemoryLlmSettings)
    embedder: MemoryEmbedderSettings = Field(default_factory=MemoryEmbedderSettings)

    @model_validator(mode="after")
    def _resolve_default_paths(self) -> MemorySettings:
        if not self.history_db_path:
            object.__setattr__(
                self, "history_db_path",
                (Path.home() / ".shovel" / "memory" / "history.db").as_posix(),
            )

        return self
