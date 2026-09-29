"""扫描流水线的端到端测试: 真实 local_fs + 内存 SQLite + 假 embedder。

这里刻意**不** mock 连接器和解析器 —— 这条链路的价值恰恰在于"接起来
能不能跑", 把中间几段换成 mock 就只剩下在测 mock 自己。被替换掉的只有
embedding 端点和向量库: 前者要网络, 后者要落盘, 都与被验证的编排逻辑
无关。

关注点是那些"接错了才会出问题"的地方: 增量跳过、单篇失败不中断整轮、
discover 未完成时不敢对账、以及全量扫描后消失的文档会不会立碑。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shovel.db.base import Base
from shovel.db.model.content import Chunk, Document
from shovel.db.model.execution import Job
from shovel.domain.enums import (
    DocumentLifecycleState,
    DocumentPipeState,
    Granularity,
    JobState,
    ScanMode,
    VectorState,
)
from shovel.exceptions.pipeline import ResourceNotScannableError
from shovel.pipeline.scan_service import ScanService
from shovel.pipeline.sink import VectorRecord
from shovel.services.credentials import InMemoryCredentialStore
from shovel.services.resource_service import ResourceDraft, ResourceService

_DIM = 8


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class FakeEmbedder:
    """按文本长度造一个确定性向量, 并记录被调用了多少次。

    确定性很重要: 断言"没变的文档不会重新 embedding"时, 靠的是调用
    计数而不是向量内容, 但确定性能让失败时的诊断信息有意义。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.texts: list[str] = []

    @property
    def dimension(self) -> int:
        return _DIM

    @property
    def model_version(self) -> str:
        return "fake-embed@1"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)

        return [[float(len(t) % 7) / 7.0] * _DIM for t in texts]


class FakeSink:
    """把向量攒在内存里, 顺便记录删除表达式。"""

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.deletes: list[str] = []

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        for record in records:
            self.records[record.id] = record

        return len(records)

    async def delete_document(self, resource_id: str, doc_uri: str) -> int:
        self.deletes.append(doc_uri)
        stale = [k for k, v in self.records.items() if v.doc_uri == doc_uri]

        for key in stale:
            del self.records[key]

        return len(stale)

    async def delete_resource(self, resource_id: str) -> int:
        self.deletes.append(resource_id)

        return 0

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import shovel.db.model  # noqa: F401  导入即注册全部表

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest.fixture
def resources(sessions: async_sessionmaker[AsyncSession]) -> ResourceService:
    return ResourceService(sessions, InMemoryCredentialStore())


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def service(
    sessions: async_sessionmaker[AsyncSession],
    resources: ResourceService,
    embedder: FakeEmbedder,
    sink: FakeSink,
) -> ScanService:
    return ScanService(sessions, resources, embedder=embedder, sink=sink)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """一个小语料库: 两种文本格式 + 一个不该被索引的二进制文件。"""

    (tmp_path / "notes.md").write_text(
        "# 项目笔记\n\n第一段内容, 用来验证解析与切块。\n\n## 小节\n\n第二段内容。\n",
        encoding="utf-8",
    )
    (tmp_path / "readme.txt").write_text(
        "纯文本文件的正文。\n\n第二段。\n", encoding="utf-8"
    )
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary garbage\x00")

    return tmp_path


@pytest_asyncio.fixture
async def resource_id(resources: ResourceService, corpus: Path) -> str:
    resource = await resources.create(
        ResourceDraft(
            name="notes",
            connector_kind="local_fs",
            config={"root_path": str(corpus)},
        )
    )
    # verify 成功即进入 ACTIVE —— 能不能扫的唯一凭据是"刚刚真的读到了"
    await resources.verify(resource.id)

    return resource.id


# --------------------------------------------------------------------------- #
# 基本链路
# --------------------------------------------------------------------------- #
async def test_scan_indexes_documents_end_to_end(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    sink: FakeSink,
) -> None:
    """一轮扫描要同时留下三样东西: document 行、chunk 行、向量。

    少任何一样都是"看起来成功了但搜不到", 而那种故障只有用户去搜的时候
    才会暴露, 离扫描已经很远。
    """

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.ok, outcome.error_detail
    assert outcome.discovered >= 3
    assert outcome.parsed == 2  # blob.bin 解析不了
    assert outcome.chunks_written > 0
    assert outcome.vectors_written > 0

    async with sessions() as session:
        docs = (await session.execute(select(Document))).scalars().all()
        chunks = (await session.execute(select(Chunk))).scalars().all()

    indexed = [d for d in docs if d.pipe_state is DocumentPipeState.INDEXED]
    assert len(indexed) == 2
    assert all(d.indexed_hash == d.content_hash for d in indexed)

    # 只有检索块进向量库, parent 块留在 SQLite 供展开阅读
    retrievable = [c for c in chunks if c.granularity is Granularity.CHUNK]
    assert len(sink.records) == len(retrievable)
    assert all(c.vector_state is VectorState.INDEXED for c in retrievable)


async def test_unsupported_file_is_skipped_not_failed(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
) -> None:
    """用户目录里本来就有二进制文件, 算成失败会让扫描永远"带错完成"。"""

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.failed_docs == 0
    assert outcome.state is JobState.SUCCEEDED

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%blob.bin"))
        blob = (await session.execute(stmt)).scalar_one()

    assert blob.pipe_state is DocumentPipeState.SKIPPED


# --------------------------------------------------------------------------- #
# 增量
# --------------------------------------------------------------------------- #
async def test_second_scan_skips_unchanged(
    service: ScanService,
    resource_id: str,
    embedder: FakeEmbedder,
) -> None:
    """没变的文档不该重新 embedding —— 那是整条链路上最贵的一步。"""

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)
    calls_after_first = embedder.calls
    assert calls_after_first > 0

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.skipped_unchanged == 2
    assert outcome.parsed == 0
    assert embedder.calls == calls_after_first


async def test_changed_content_is_reindexed(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """判据是内容哈希而不是 mtime: 内容变了就必须重切重 embed。"""

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    (corpus / "readme.txt").write_text(
        "改过的正文。\n\n新增的第二段内容。\n\n第三段。\n", encoding="utf-8"
    )

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.parsed == 1
    assert outcome.skipped_unchanged == 1

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%readme.txt"))
        doc = (await session.execute(stmt)).scalar_one()

    assert doc.indexed_hash == doc.content_hash


async def test_shrinking_document_drops_stale_chunks(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """文档改短之后, 旧的尾部 chunk 必须消失。

    只 upsert 不删的话, 那些 chunk 不会被任何新 id 覆盖, 会变成搜得到
    却已不存在于原文的幽灵内容 —— 用户点进去看到的是早就删掉的段落。
    """

    long_text = "\n\n".join(f"第{i}段正文内容, 足够长以便切出独立的块。" * 8 for i in range(1, 8))
    (corpus / "readme.txt").write_text(long_text, encoding="utf-8")

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    async with sessions() as session:
        stmt = select(Chunk).where(Chunk.doc_uri.like("%readme.txt"))
        before = len((await session.execute(stmt)).scalars().all())

    (corpus / "readme.txt").write_text("只剩一句话了。\n", encoding="utf-8")
    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    async with sessions() as session:
        stmt = select(Chunk).where(Chunk.doc_uri.like("%readme.txt"))
        after = (await session.execute(stmt)).scalars().all()

    assert len(after) < before
    assert all("第7段" not in c.text_ for c in after)


# --------------------------------------------------------------------------- #
# 删除对账
# --------------------------------------------------------------------------- #
async def test_full_scan_tombstones_vanished_documents(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """全量扫描是唯一有完整观测集的时刻, 也是唯一敢判定"消失"的时刻。"""

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    (corpus / "readme.txt").unlink()

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.tombstoned == 1

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%readme.txt"))
        doc = (await session.execute(stmt)).scalar_one()

    assert doc.lifecycle_state is DocumentLifecycleState.TOMBSTONED


async def test_tombstoned_document_revives(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """立碑而不是删除, 是为了文件被移回来时能原地复活。"""

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    text = (corpus / "readme.txt").read_text(encoding="utf-8")
    (corpus / "readme.txt").unlink()
    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    (corpus / "readme.txt").write_text(text, encoding="utf-8")
    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%readme.txt"))
        doc = (await session.execute(stmt)).scalar_one()

    assert doc.lifecycle_state is DocumentLifecycleState.ACTIVE


async def test_incremental_scan_never_tombstones(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """增量扫描看到的是一个子集, 据此判定删除会把整库误伤成墓碑。"""

    await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    (corpus / "readme.txt").unlink()

    outcome = await service.scan(resource_id, mode=ScanMode.INCREMENTAL)

    assert outcome.tombstoned == 0

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%readme.txt"))
        doc = (await session.execute(stmt)).scalar_one()

    assert doc.lifecycle_state is DocumentLifecycleState.ACTIVE


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #
async def test_one_bad_document_does_not_abort_the_run(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
    corpus: Path,
) -> None:
    """单篇失败要被隔离。二十万篇里有一篇坏文件就整轮作废是不可接受的。"""

    # 扩展名声称是 PDF, 内容却不是 —— 解析必然失败
    (corpus / "broken.pdf").write_bytes(b"this is definitely not a pdf")

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    assert outcome.failed_docs == 1
    assert outcome.parsed == 2  # 另外两篇照常完成
    assert outcome.state is JobState.COMPLETED_WITH_ERRORS

    async with sessions() as session:
        stmt = select(Document).where(Document.uri.like("%broken.pdf"))
        doc = (await session.execute(stmt)).scalar_one()

    assert doc.pipe_state is DocumentPipeState.FAILED
    assert doc.pipe_error_kind is not None


async def test_draft_resource_is_rejected_without_creating_a_job(
    sessions: async_sessionmaker[AsyncSession],
    resources: ResourceService,
    service: ScanService,
    corpus: Path,
) -> None:
    """立刻失败的 Job 对用户没有价值, 只会把作业历史填满噪声。"""

    resource = await resources.create(
        ResourceDraft(
            name="draft-only",
            connector_kind="local_fs",
            config={"root_path": str(corpus)},
        )
    )

    with pytest.raises(ResourceNotScannableError):
        await service.scan(resource.id)

    async with sessions() as session:
        jobs = (await session.execute(select(Job))).scalars().all()

    assert jobs == []


# --------------------------------------------------------------------------- #
# 作业记录
# --------------------------------------------------------------------------- #
async def test_job_records_counters_and_discovery_flag(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
) -> None:
    """``is_discovery_complete`` 是删除对账的硬门闸, 必须真的被置位。"""

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP)

    async with sessions() as session:
        job = await session.get(Job, outcome.job_id)

    assert job is not None
    assert job.is_discovery_complete is True
    assert job.finished_at is not None
    assert job.current_stage is None
    assert job.discovered == outcome.discovered
    assert job.chunks_written == outcome.chunks_written


async def test_limit_truncates_processing_but_not_discovery(
    service: ScanService,
    sessions: async_sessionmaker[AsyncSession],
    resource_id: str,
) -> None:
    """limit 只截断处理量。discover 仍须完整, 否则对账会误判。"""

    outcome = await service.scan(resource_id, mode=ScanMode.FULL_SWEEP, limit=1)

    assert outcome.discovered >= 3
    assert outcome.parsed <= 1

    async with sessions() as session:
        docs = (await session.execute(select(Document))).scalars().all()

    assert len(docs) >= 3
    assert any(d.pipe_state is DocumentPipeState.PENDING for d in docs)
