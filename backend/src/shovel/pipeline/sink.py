#---------------------------------------------------------
# 向量写入端
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""把 chunk 的向量写进 Zvec, 以及按文档/资源删除它们。

## 为什么是一个协议而不是直接调 zvec

三个具体理由, 按重要性排序:

1. **测试。** 流水线编排的正确性 (计数对不对、失败了会不会重试、删除
   有没有传播) 与"向量真的写进了磁盘"是两件事。前者需要跑几十个用例,
   后者每次都要建一个真实 collection。用协议隔开, 前者可以在毫秒级跑完。
2. **``--no-embed``。** 不做 embedding 时没有向量可写, 需要一个什么都
   不做的实现, 而不是在编排层到处写 ``if embedder is not None``。
3. **Zvec 是可丢弃的。** SQLite 才是真相之源。把向量写入收敛到一个窄接口,
   "删库重建"就只需要重放这个接口, 而不必理解整条流水线。

## 幂等

``upsert`` 而不是 ``insert``: 同样的内容必然算出同样的 chunk id
(见 ``db.base.chunk_vector_id``), 所以重复扫描同一篇未变化的文档时,
写入的是同一批 id。用 insert 会得到主键冲突或重复向量, 用 upsert 则
天然幂等 —— 这正是"扫描中断后直接重跑"能安全成立的原因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from shovel.db.base import now_ts
from shovel.exceptions.pipeline import VectorSinkError
from shovel.vector.schema import DENSE_VECTOR, TEXT_FIELD


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量及其标量字段。

    字段集合与 ``vector/schema.py`` 一一对应。放在这里重复一遍不是冗余,
    而是让"写入端需要什么"成为一份可读的清单 —— schema 声明的是存储结构,
    这里声明的是调用方的义务。
    """

    id: str
    vector: list[float]
    text: str

    resource_id: str
    doc_uri: str
    granularity: str
    chunk_index: int

    doc_class: str | None = None
    modality: str | None = None
    lang: str | None = None
    rev: int = 1
    model_version: str | None = None
    authority: float = 0.6
    is_canonical: bool = True


class ChunkVectorSink(Protocol):
    """向量写入端。"""

    async def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    async def delete_document(self, resource_id: str, doc_uri: str) -> int: ...

    async def delete_resource(self, resource_id: str) -> int: ...

    async def aclose(self) -> None: ...


class NullVectorSink:
    """丢弃一切写入。用于 ``--no-embed`` 与测试。"""

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        return 0

    async def delete_document(self, resource_id: str, doc_uri: str) -> int:
        return 0

    async def delete_resource(self, resource_id: str) -> int:
        return 0

    async def aclose(self) -> None:
        return None


class ZvecChunkSink:
    """真正写 Zvec 的实现。

    Zvec 的 Python 接口是同步的, 而编排层是 async。这里用
    ``asyncio.to_thread`` 把它挪出事件循环 —— 和解析器同样的理由:
    一次几千条的批量写入会阻塞事件循环好几百毫秒, 期间进度输出
    和取消信号都会卡住。
    """

    def __init__(self, collection: Any) -> None:
        self._collection = collection

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0

        import asyncio

        docs = [self._to_doc(record) for record in records]

        try:
            result = await asyncio.to_thread(self._collection.upsert, docs)
            # flush 之后才算真的落盘。不 flush 的话, 进程正常退出时这批
            # 向量可能还在内存里, 而 SQLite 那边已经标成 indexed —— 得到
            # 一个"标着已索引却搜不到"的状态, 且没有任何自动发现途径。
            await asyncio.to_thread(self._collection.flush)
        except Exception as exc:
            raise VectorSinkError(f"写入 {len(docs)} 条向量失败: {exc}") from exc

        _ensure_ok(result, f"写入 {len(docs)} 条向量")

        return len(docs)

    async def delete_document(self, resource_id: str, doc_uri: str) -> int:
        # 比较符是单个 ``=`` (SQL 风格), 不是 ``==``。写成 ``==`` 会被
        # 过滤表达式的语法分析直接拒掉, 于是每一次删除传播都会失败。
        return await self._delete(
            f'resource_id = "{_escape(resource_id)}" '
            f'and doc_uri = "{_escape(doc_uri)}"'
        )

    async def delete_resource(self, resource_id: str) -> int:
        return await self._delete(f'resource_id = "{_escape(resource_id)}"')

    async def aclose(self) -> None:
        import asyncio

        if self._collection is None:
            return

        collection, self._collection = self._collection, None
        await asyncio.to_thread(collection.close)

    # ----------------------------------------------------------------- #
    # 内部
    # ----------------------------------------------------------------- #
    async def _delete(self, expression: str) -> int:
        import asyncio

        try:
            # 注意是 delete_by_filter 而不是 delete —— 后者按 id 删。
            # 传错的话表达式会被当成一个 id, 于是"什么都没删掉"且不报错。
            await asyncio.to_thread(self._collection.delete_by_filter, expression)
            await asyncio.to_thread(self._collection.flush)
        except Exception as exc:
            raise VectorSinkError(f"按条件删除向量失败 ({expression}): {exc}") from exc

        # Zvec 的 delete 不返回行数。返回 0 而不是瞎猜一个数字 ——
        # 调用方把它写进统计时, 一个假的数字比没有数字更有害。
        return 0

    @staticmethod
    def _to_doc(record: VectorRecord) -> Any:
        """转成 Zvec 的 Doc。

        向量走 ``vectors``, 标量走 ``fields`` —— 两者是分开的参数。把标量
        混进 vectors (或反过来) 不会立刻报错, 但字段会静默地不进索引,
        于是过滤条件永远匹配不到任何东西。
        """

        from zvec import Doc

        return Doc(
            id=record.id,
            vectors={DENSE_VECTOR: record.vector},
            fields={
                TEXT_FIELD: record.text,
                "resource_id": record.resource_id,
                "doc_uri": record.doc_uri,
                "granularity": record.granularity,
                "chunk_index": record.chunk_index,
                "doc_class": record.doc_class,
                "modality": record.modality,
                "lang": record.lang,
                "rev": record.rev,
                "model_version": record.model_version,
                "authority": record.authority,
                "is_canonical": record.is_canonical,
                "created_at": now_ts(),
            },
        )


def _ensure_ok(result: Any, what: str) -> None:
    """Zvec 的写入把失败放在返回的 Status 里, 而不是抛异常。

    不检查的话, 一次被拒绝的写入看起来和成功完全一样, 而 SQLite 那边
    已经把 chunk 标成 indexed 了 —— 这类不一致没有任何自动发现途径,
    只有用户搜不到东西的时候才会暴露。
    """

    statuses = result if isinstance(result, list) else [result]

    for status in statuses:
        ok = getattr(status, "ok", None)

        if ok is None:
            ok = getattr(status, "is_ok", None)
            ok = ok() if callable(ok) else ok

        if ok is False:
            raise VectorSinkError(f"{what} 被向量库拒绝: {status}")


def _escape(value: str) -> str:
    """转义过滤表达式里的引号与反斜杠。

    doc_uri 来自文件路径, 而 Windows 路径里有反斜杠, 文件名里可以有
    引号。不转义的话这两样都会把过滤表达式拆坏, 轻则删除失败, 重则
    删到别的资源上去。
    """

    return value.replace("\\", "\\\\").replace('"', '\\"')


def open_chunk_sink(knowledge_path: str) -> ChunkVectorSink:
    """打开知识库 collection 并包成一个 sink。

    collection 不存在时**不**自动创建: 建 collection 需要知道维度和
    距离度量, 那是 ``shovel init`` 的职责。在这里顺手建一个, 用的会是
    默认维度, 而它极可能与用户配的 embedding 模型不符。
    """

    from shovel.vector.store import init_zvec_runtime, open_collection

    init_zvec_runtime()

    return ZvecChunkSink(open_collection(knowledge_path))


__all__ = [
    "ChunkVectorSink",
    "NullVectorSink",
    "VectorRecord",
    "ZvecChunkSink",
    "open_chunk_sink",
]
