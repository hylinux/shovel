#---------------------------------------------------------------------
# 向量库(Zvec)配置
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator


class ZvecSettings(BaseModel):
    """Zvec 本地向量库配置。

    与 DatabaseSettings 同样继承 BaseModel 而非 BaseSettings:
    环境变量由 AppSettings 通过 env_nested_delimiter 统一处理
    (SHOVEL_ZVEC__DIM -> zvec.dim)。

    两个 collection 各自独占一个目录:
    知识(chunk) 与记忆(memory) 的写入频率、重建代价、删除策略都不同,
    分开存放意味着"删掉知识库重建"不会碰到记忆。
    """

    # collection 落盘目录; 留空时由 validator 回落到 ~/.shovel 下
    knowledge_path: str = ""
    memory_path: str = ""

    # collection 名字与模型层注释中的 `shovel_chunk` / `shovel_memory` 保持一致
    chunk_collection: str = "shovel_chunk"
    memory_collection: str = "shovel_memory"

    #: dense 向量维度。换 embedding 模型 = 换维度 = 必须重建 collection,
    #: 所以它是配置项而不是常量。
    dim: int = 1024

    metric: Literal["cosine", "ip", "l2"] = "cosine"

    # HNSW 构建参数
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200

    #: Zvec 自己的日志目录; 留空回落到 ~/.shovel/logs
    log_dir: str = ""

    @model_validator(mode="after")
    def _resolve_default_paths(self) -> ZvecSettings:
        profile = Path.home() / ".shovel"

        if not self.knowledge_path:
            object.__setattr__(
                self, "knowledge_path",
                (profile / "knowledge" / self.chunk_collection).as_posix(),
            )

        if not self.memory_path:
            object.__setattr__(
                self, "memory_path",
                (profile / "memory" / self.memory_collection).as_posix(),
            )

        if not self.log_dir:
            object.__setattr__(self, "log_dir", (profile / "logs").as_posix())

        return self
