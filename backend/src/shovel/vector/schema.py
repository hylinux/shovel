#---------------------------------------------------------------------
# Zvec collection 的 schema 定义
#
# SQLite 是真相之源, Zvec 可丢弃重建。因此这里只声明两类东西:
#   1. dense 向量本身
#   2. "必须写进查询条件里"的标量字段
#
# 判断一个字段要不要进来的标准只有一条: 它会不会出现在检索的过滤条件中。
# 会 -> 必须在 Zvec 里, 否则只能 top_k 之后再过滤, 未授权内容会挤占名额;
# 不会 -> 留在 SQLite, 用 id 回表取即可(两边 id 同值, 见 db/base.py)。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from zvec import (
    CollectionSchema,
    DataType,
    FieldSchema,
    FtsIndexParam,
    HnswIndexParam,
    InvertIndexParam,
    MetricType,
    VectorSchema,
)

#: dense 向量字段名。两个 collection 用同一个名字, 检索层就不必分支。
DENSE_VECTOR = "dense"

#: 全文检索字段名。Zvec 里存一份文本副本用于 FTS,
#: 权威全文仍在 SQLite 的 chunk.text / 记忆表中。
TEXT_FIELD = "text"

_METRICS = {
    "cosine": MetricType.COSINE,
    "ip": MetricType.IP,
    "l2": MetricType.L2,
}


def metric_of(name: str) -> MetricType:
    try:
        return _METRICS[name.lower()]
    except KeyError:
        raise ValueError(
            f"不支持的向量距离度量: {name!r}, 可选: {', '.join(_METRICS)}"
        ) from None


def _dense(dim: int, metric: str, m: int, ef_construction: int) -> VectorSchema:
    if dim <= 0:
        raise ValueError(
            f"向量维度必须大于 0, 当前为 {dim}。请检查 settings.toml 的 zvec.dim。"
        )

    return VectorSchema(
        name=DENSE_VECTOR,
        data_type=DataType.VECTOR_FP32,
        dimension=dim,
        index_param=HnswIndexParam(
            metric_type=metric_of(metric),
            m=m,
            ef_construction=ef_construction,
        ),
    )


def chunk_collection_schema(
        name: str = "shovel_chunk",
        *,
        dim: int = 1024,
        metric: str = "cosine",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 200,
) -> CollectionSchema:
    """知识库 collection。

    doc id == SQLite 的 ``chunk.id`` == :func:`shovel.db.base.chunk_vector_id`
    的返回值, 所以不需要单独声明 id 字段 —— Zvec 的 ``Doc.id`` 就是它。
    """

    return CollectionSchema(
        name=name,
        fields=[
            # 授权与删除传播: 两者都是按 resource_id / doc_uri 成批做的
            FieldSchema("resource_id", DataType.STRING, index_param=InvertIndexParam()),
            FieldSchema("doc_uri", DataType.STRING, index_param=InvertIndexParam()),

            # 分层检索: chunk / parent / section / doc_summary ...
            FieldSchema("granularity", DataType.STRING, index_param=InvertIndexParam()),
            FieldSchema("chunk_index", DataType.INT64, nullable=True),

            # 路由与语言过滤
            FieldSchema("doc_class", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),
            FieldSchema("modality", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),
            FieldSchema("lang", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),

            # 失效判定: rev / model_version 变了, 旧向量就该被清掉
            FieldSchema("rev", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),
            FieldSchema("model_version", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),

            # 排序信号(写入时算好, 从 SQLite 镜像过来)
            FieldSchema("authority", DataType.FLOAT, nullable=True),
            FieldSchema("is_canonical", DataType.BOOL, nullable=True),
            FieldSchema("created_at", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),

            # 混合检索的 BM25 一侧
            FieldSchema(TEXT_FIELD, DataType.STRING, nullable=True,
                        index_param=FtsIndexParam()),
        ],
        vectors=_dense(dim, metric, hnsw_m, hnsw_ef_construction),
    )


def memory_collection_schema(
        name: str = "shovel_memory",
        *,
        dim: int = 1024,
        metric: str = "cosine",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 200,
) -> CollectionSchema:
    """记忆 collection。

    doc id == :func:`shovel.db.base.memory_vector_id`。
    记忆不属于任何文档, 过滤维度是"谁的/哪次会话/还有效吗/多重要",
    与 chunk 完全不同 —— 这正是它独立成一个 collection 的原因。
    """

    return CollectionSchema(
        name=name,
        fields=[
            # episodic / semantic / procedural ...
            FieldSchema("memory_kind", DataType.STRING, index_param=InvertIndexParam()),
            FieldSchema("memory_id", DataType.STRING, index_param=InvertIndexParam()),

            # 记忆的授权边界与 chunk 分开(见 AgentScope.can_access_memory)
            FieldSchema("agent_id", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),
            FieldSchema("session_id", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),

            # supersede 不删行, 检索时必须靠它排除已失效记忆
            FieldSchema("validity_state", DataType.STRING, nullable=True,
                        index_param=InvertIndexParam()),
            FieldSchema("rev", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),

            # 显著性与时间轴: 都是范围查询, 所以开 range optimization
            FieldSchema("importance", DataType.FLOAT, nullable=True),
            FieldSchema("confidence", DataType.FLOAT, nullable=True),
            FieldSchema("is_pinned", DataType.BOOL, nullable=True),
            FieldSchema("occurred_at", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),
            FieldSchema("valid_from", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),
            FieldSchema("valid_until", DataType.INT64, nullable=True,
                        index_param=InvertIndexParam(enable_range_optimization=True)),

            FieldSchema("tags", DataType.ARRAY_STRING, nullable=True,
                        index_param=InvertIndexParam()),

            FieldSchema(TEXT_FIELD, DataType.STRING, nullable=True,
                        index_param=FtsIndexParam()),
        ],
        vectors=_dense(dim, metric, hnsw_m, hnsw_ef_construction),
    )


__all__ = [
    "DENSE_VECTOR",
    "TEXT_FIELD",
    "chunk_collection_schema",
    "memory_collection_schema",
    "metric_of",
]
