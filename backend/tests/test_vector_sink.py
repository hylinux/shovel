"""向量写入端的测试, 跑在**真实的** Zvec collection 上。

这一个文件刻意不用替身。理由是它要验证的全部内容都是"我们对 Zvec API
的理解对不对", 而 mock 只会复述我们的理解, 不会纠正它 —— 这里最初的
两个 bug (把 ``delete_by_filter`` 写成 ``delete``、把比较符写成 ``==``)
在任何 mock 下都会"通过"。

代价是每个用例要建一个落盘的 collection, 比其它测试慢。值得。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shovel.db.base import chunk_vector_id
from shovel.pipeline.sink import VectorRecord, ZvecChunkSink

_DIM = 4

#: 一个不老实的 doc_uri: 带空格、带双引号。
#: 这两样都会出现在真实的 Windows 文件名里, 也都会把过滤表达式拆坏。
_TRICKY = 'file:///C:/a b/合同"终稿".md'
_PLAIN = "file:///C:/notes/other.md"


@pytest.fixture
def collection() -> Iterator[Any]:
    import zvec

    from shovel.vector.schema import chunk_collection_schema
    from shovel.vector.store import init_zvec_runtime

    init_zvec_runtime()

    root = Path(tempfile.mkdtemp(prefix="shovel-sink-"))
    col = zvec.create_and_open(str(root / "chunk"), chunk_collection_schema(dim=_DIM))

    yield col

    col.close()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sink(collection: Any) -> ZvecChunkSink:
    return ZvecChunkSink(collection)


def _record(uri: str, resource_id: str, index: int) -> VectorRecord:
    return VectorRecord(
        id=chunk_vector_id(uri, resource_id, "1", "chunk", index),
        vector=[float(index)] * _DIM,
        text=f"第 {index} 段正文内容。",
        resource_id=resource_id,
        doc_uri=uri,
        granularity="chunk",
        chunk_index=index,
        doc_class="markdown",
        modality="text",
        lang="zh",
        rev=1,
        model_version="fake@1",
        authority=0.8,
    )


def _count(collection: Any) -> int:
    return int(collection.stats.doc_count)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
async def test_upsert_persists_records(sink: ZvecChunkSink, collection: Any) -> None:
    written = await sink.upsert([_record(_PLAIN, "res_1", i) for i in range(5)])

    assert written == 5
    assert _count(collection) == 5


async def test_upsert_is_idempotent(sink: ZvecChunkSink, collection: Any) -> None:
    """同样的内容算出同样的 id, 所以重扫一篇没变的文档不该让库变大。

    这正是"扫描中断后直接重跑"能安全成立的原因。
    """

    records = [_record(_PLAIN, "res_1", i) for i in range(4)]

    await sink.upsert(records)
    await sink.upsert(records)

    assert _count(collection) == 4


async def test_upsert_of_nothing_touches_nothing(sink: ZvecChunkSink, collection: Any) -> None:
    assert await sink.upsert([]) == 0
    assert _count(collection) == 0


async def test_fields_are_queryable_not_just_stored(
    sink: ZvecChunkSink, collection: Any
) -> None:
    """标量必须走 ``Doc.fields``。混进 ``vectors`` 不会报错,

    但字段不会进索引, 于是所有过滤条件都匹配不到 —— 删除传播会静默失效。
    """

    record = _record(_PLAIN, "res_1", 0)
    await sink.upsert([record])

    # fetch 返回的是 id -> Doc 的字典, 缺失的 id 直接不出现
    fields = collection.fetch([record.id])[record.id].fields

    assert fields["resource_id"] == "res_1"
    assert fields["doc_uri"] == _PLAIN
    assert fields["granularity"] == "chunk"


# --------------------------------------------------------------------------- #
# 删除传播
# --------------------------------------------------------------------------- #
async def test_delete_document_removes_only_that_document(
    sink: ZvecChunkSink, collection: Any
) -> None:
    await sink.upsert(
        [_record(_TRICKY, "res_1", i) for i in range(5)]
        + [_record(_PLAIN, "res_1", i) for i in range(3)]
    )
    assert _count(collection) == 8

    await sink.delete_document("res_1", _TRICKY)

    assert _count(collection) == 3


async def test_delete_document_survives_quotes_and_spaces(
    sink: ZvecChunkSink, collection: Any
) -> None:
    """文件名里的引号不转义会把过滤表达式拆坏。

    最好的情况是报错, 最坏的情况是表达式仍然合法但语义变了 —— 那会
    删到别的资源上去, 而且没有任何提示。
    """

    await sink.upsert([_record(_TRICKY, "res_1", i) for i in range(3)])

    await sink.delete_document("res_1", _TRICKY)

    assert _count(collection) == 0


async def test_delete_resource_spares_other_resources(
    sink: ZvecChunkSink, collection: Any
) -> None:
    await sink.upsert(
        [_record(_PLAIN, "res_1", i) for i in range(3)]
        + [_record(_PLAIN, "res_2", i) for i in range(2)]
    )

    await sink.delete_resource("res_1")

    assert _count(collection) == 2


async def test_delete_of_a_missing_document_is_not_an_error(sink: ZvecChunkSink) -> None:
    """重跑一次删除不该炸。对账逻辑天然会重复发起同一个删除。"""

    await sink.delete_document("res_1", "file:///C:/never/indexed.md")


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
async def test_aclose_is_idempotent(collection: Any) -> None:
    """CLI 在 finally 里关, 异常路径上可能会关第二次。"""

    sink = ZvecChunkSink(collection)

    await sink.aclose()
    await sink.aclose()
