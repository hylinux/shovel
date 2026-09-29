"""切块器的测试。

切块是整条链路上最难"看出错了"的一环: 切得不好不会报错, 只会让检索
结果慢慢变差, 而那种退化没有任何报警。所以这里把每一条设计约束都写成
断言 —— 一旦有人为了别的目的动了预算或边界逻辑, 测试会立刻说出是哪一条
约束被破坏了。
"""

from __future__ import annotations

import pytest

from shovel.db.base import chunk_vector_id
from shovel.domain.enums import BlockKind, DocClass, Granularity
from shovel.pipeline.chunker import (
    CHUNKER_VERSION,
    ChunkerConfig,
    chunk_document,
    estimate_tokens,
)
from shovel.pipeline.parsers.base import Block, ParsedDocument

_URI = "file:///t/doc.md"
_HASH = "hash-abc"


def _doc(*blocks: Block, title: str = "测试文档") -> ParsedDocument:
    return ParsedDocument(
        blocks=tuple(blocks),
        title=title,
        doc_class=DocClass.MARKDOWN,
        parser="markdown_doc",
        parser_version="1",
    )


def _para(text: str, *, path: tuple[str, ...] = (), locator: str | None = None) -> Block:
    return Block(text=text, kind=BlockKind.PARAGRAPH, path=path, locator=locator)


def _cut(parsed: ParsedDocument, config: ChunkerConfig | None = None):
    return chunk_document(parsed, doc_uri=_URI, content_hash=_HASH, config=config)


def _of(drafts, granularity: Granularity):
    return [d for d in drafts if d.granularity is granularity]


# --------------------------------------------------------------------------- #
# 两层结构
# --------------------------------------------------------------------------- #
def test_produces_both_layers() -> None:
    """small-to-big: 小块负责召回, 大块负责阅读。

    只有小块的话, LLM 拿到的是一句没有上下文的话; 只有大块的话,
    向量被太多无关内容稀释, 召回率会掉。
    """

    drafts = _cut(_doc(_para("正文内容一。" * 30), _para("正文内容二。" * 30)))

    assert _of(drafts, Granularity.PARENT)
    assert _of(drafts, Granularity.CHUNK)


def test_every_chunk_points_at_an_existing_parent() -> None:
    """parent_id 悬空 = expand_chunk 展不开 = 用户看不到上下文。"""

    drafts = _cut(_doc(*[_para(f"第{i}段正文内容。" * 20) for i in range(6)]))

    parent_ids = {p.id for p in _of(drafts, Granularity.PARENT)}

    assert all(c.parent_id in parent_ids for c in _of(drafts, Granularity.CHUNK))


def test_only_retrieval_chunks_are_embedded() -> None:
    """parent 块不进向量库 —— 给同一份内容做两次 embedding

    既翻倍成本, 又会让同一段文字在检索结果里出现两遍。
    """

    assert Granularity.CHUNK.should_embedded
    assert not Granularity.PARENT.should_embedded


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #
def test_headings_break_parent_groups() -> None:
    """换了章节就该换父块, 哪怕长度还没到预算。

    "违约责任"和"付款方式"被塞进同一个父块, 展开时会读到无关条款。
    """

    drafts = _cut(_doc(
        Block(text="第一章", kind=BlockKind.HEADING, level=1),
        _para("第一章的正文。", path=("第一章",)),
        Block(text="第二章", kind=BlockKind.HEADING, level=1),
        _para("第二章的正文。", path=("第二章",)),
    ))

    parents = _of(drafts, Granularity.PARENT)

    assert len(parents) == 2
    assert "第二章的正文" not in parents[0].text


def test_atomic_blocks_are_never_split() -> None:
    """代码块从中间切开就不再是可运行的代码。"""

    code = "\n".join(f"line_{i} = compute(value_{i})" for i in range(40))
    drafts = _cut(_doc(
        _para("代码之前的说明文字。"),
        Block(text=code, kind=BlockKind.CODE),
        _para("代码之后的说明文字。"),
    ))

    chunks = _of(drafts, Granularity.CHUNK)
    holder = [c for c in chunks if "line_0 " in c.text]

    assert len(holder) == 1
    assert "line_39" in holder[0].text
    # 原子块也不与相邻正文混合, 否则代码和散文会共用一个向量
    assert "说明文字" not in holder[0].text


def test_oversized_atomic_block_still_respects_the_ceiling() -> None:
    """超出硬上限的代码块只能切 —— 但那是"不得不", 不是默认行为。

    不切的话这个块会被 embedding 端点静默截断, 后半部分直接从索引消失,
    那比切开更糟: 至少切开之后每一部分都还能被搜到。
    """

    config = ChunkerConfig(max_chunk_chars=600)
    code = "\n".join(f"line_{i} = compute(value_{i})" for i in range(120))

    chunks = _of(_cut(_doc(Block(text=code, kind=BlockKind.CODE)), config), Granularity.CHUNK)

    assert len(chunks) > 1
    assert all(len(c.text) <= config.max_chunk_chars for c in chunks)
    assert "line_119" in chunks[-1].text


def test_long_text_is_split_with_overlap() -> None:
    """重叠是为了跨边界的句子不被两边都切断。

    没有重叠的话, 正好落在缝上的那句话在两个块里都只剩半截, 两个块
    的向量都表达不了它。
    """

    config = ChunkerConfig(chunk_chars=300, overlap_chars=80, min_chunk_chars=50)
    body = "".join(f"这是第{i}句话, 用于验证重叠切分。" for i in range(60))

    chunks = _of(_cut(_doc(_para(body)), config), Granularity.CHUNK)

    assert len(chunks) > 2

    tail = chunks[0].text[-40:]
    assert any(piece in chunks[1].text for piece in (tail[-20:], tail[:20]))


def test_tiny_trailing_blocks_are_merged() -> None:
    """一个只有五个字的块, 它的向量几乎不携带信息, 却会占一个召回位。"""

    config = ChunkerConfig(chunk_chars=400, min_chunk_chars=120)
    drafts = _cut(_doc(_para("正文内容。" * 60), _para("完。")), config)

    chunks = _of(drafts, Granularity.CHUNK)

    assert all(len(c.text) >= config.min_chunk_chars for c in chunks)
    assert "完。" in "".join(c.text for c in chunks)


# --------------------------------------------------------------------------- #
# 上下文前缀
# --------------------------------------------------------------------------- #
def test_context_prefix_carries_the_heading_path() -> None:
    """"逾期超过三十日的" 这种条款脱离标题就完全不可检索。"""

    drafts = _cut(_doc(
        Block(text="违约责任", kind=BlockKind.HEADING, level=1),
        _para("逾期超过三十日的, 守约方可以解除合同。", path=("违约责任",)),
    ))

    chunk = _of(drafts, Granularity.CHUNK)[0]

    assert chunk.context_prefix
    assert "违约责任" in chunk.context_prefix


def test_context_prefix_does_not_pollute_the_text() -> None:
    """前缀只喂给 embedding。返回给用户的必须是原文 ——

    在引用里看到自动拼上去的标题会让用户怀疑引用是编的。
    """

    drafts = _cut(_doc(
        Block(text="第三章", kind=BlockKind.HEADING, level=1),
        _para("这是正文本身。", path=("第三章",)),
    ))

    chunk = _of(drafts, Granularity.CHUNK)[-1]

    assert not chunk.text.startswith(chunk.context_prefix or "@")


def test_locator_is_kept_in_chunk_meta() -> None:
    """页码/单元格范围是引用回原文的唯一凭据。"""

    drafts = _cut(_doc(_para("某一页的正文内容。" * 10, locator="p.7")))
    chunk = _of(drafts, Granularity.CHUNK)[0]

    assert "p.7" in str(chunk.meta)


# --------------------------------------------------------------------------- #
# id 稳定性
# --------------------------------------------------------------------------- #
def test_same_input_yields_identical_ids() -> None:
    """这是"重复扫描不产生重复向量"的全部基础。"""

    parsed = _doc(_para("稳定性测试正文。" * 40))

    assert [d.id for d in _cut(parsed)] == [d.id for d in _cut(parsed)]


def test_ids_are_unique_within_a_document() -> None:
    """id 撞了就是后写的覆盖先写的, 静默丢内容。"""

    drafts = _cut(_doc(*[_para(f"第{i}段。" * 30) for i in range(8)]))

    assert len({d.id for d in drafts}) == len(drafts)


def test_id_matches_the_shared_formula() -> None:
    """SQLite 与 Zvec 必须用同一个公式算 id。

    各算各的话, "删掉向量库能从 SQLite 重建"这个前提就不成立了。
    """

    drafts = _cut(_doc(_para("公式一致性验证正文。" * 20)))
    first = _of(drafts, Granularity.CHUNK)[0]

    assert first.id == chunk_vector_id(
        _URI, _HASH, CHUNKER_VERSION, Granularity.CHUNK.value, first.chunk_index,
    )


def test_different_content_hash_yields_different_ids() -> None:
    """文档改了内容却沿用旧 id, 会让旧向量永远留在库里。"""

    parsed = _doc(_para("内容哈希参与 id 计算。" * 20))

    a = chunk_document(parsed, doc_uri=_URI, content_hash="h1")
    b = chunk_document(parsed, doc_uri=_URI, content_hash="h2")

    assert {d.id for d in a}.isdisjoint({d.id for d in b})


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
def test_token_estimate_is_higher_for_cjk() -> None:
    """同样的字符数, 中文的 token 数远高于英文。

    用一个比例去估两种语言, 中文长文会稳定超出模型的输入上限。
    """

    cjk = "中文内容测试" * 40
    latin = "english words here " * 13

    assert len(cjk) == pytest.approx(len(latin), abs=20)
    assert estimate_tokens(cjk) > estimate_tokens(latin) * 2


def test_empty_document_produces_no_drafts() -> None:
    assert _cut(_doc()) == ()


@pytest.mark.parametrize("size", [1, 50, 500, 5000])
def test_chunks_never_exceed_the_hard_ceiling(size: int) -> None:
    """超过上限的块会被 embedding 端点直接截断 ——

    截断是静默的, 块的后半部分就这样从索引里消失了。
    """

    config = ChunkerConfig(chunk_chars=400, max_chunk_chars=900, min_chunk_chars=50)
    drafts = _cut(_doc(_para("测试正文。" * size)), config)

    non_atomic = [d for d in _of(drafts, Granularity.CHUNK)]

    assert all(len(d.text) <= config.max_chunk_chars for d in non_atomic)
