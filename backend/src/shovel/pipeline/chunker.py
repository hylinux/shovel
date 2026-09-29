#---------------------------------------------------------
# 切块器: 文本块 -> 两层 chunk
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""把 ``ParsedDocument`` 切成可检索的 chunk。

## 为什么是两层

检索和阅读需要的粒度是**相反**的:

* 检索要**小**。一个 300 字的块语义集中, 向量能准确表达"它在讲什么";
  一个 3000 字的块会把五个主题平均成一个模糊的向量, 什么都搜不准。
* 阅读要**大**。命中一个 300 字的块之后, 只把这 300 字给 LLM, 它经常
  缺上下文 —— 前一段刚定义的"该方案"指什么, 断在块外了。

所以写入时就产出两层: ``CHUNK`` 小块进向量库负责召回, ``PARENT`` 大块
留在 SQLite 不做 embedding, 命中后由 ``expand_chunk`` 顺着
``parent_chunk_id`` 取出来给 LLM 阅读。这就是 small-to-big。

代价是 SQLite 里文本存了两遍。这笔开销是值得的: 文本是整个系统里最
便宜的数据 (向量才贵), 而换来的是召回精度和阅读完整性可以各自最优,
不必互相妥协。

## 为什么用字符数而不是 token 数

token 化需要一个 tokenizer, 而 tokenizer 与 embedding 模型绑定 ——
换模型就要换 tokenizer, 换 tokenizer 就会让所有 chunk 边界改变, 于是
全库重新 embedding。用字符数则边界只取决于文本本身, 换模型不影响切块。

精度损失是有的 (中英文的字符/token 比差三倍), 由 :data:`CJK_RATIO`
粗略补偿。对"控制块大小"这个目的来说, 这个精度完全够用 —— 我们要的
是"别太大也别太小", 不是精确到 token。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from shovel.db.base import chunk_vector_id
from shovel.domain.enums import DocClass, Granularity
from shovel.pipeline.parsers.base import Block, ParsedDocument

#: 切块策略的版本号。
#:
#: 它进 ``chunk_vector_id`` 的计算, 因此**改这个字符串 = 全库 chunk 换 id
#: = 下一次扫描会重新切、重新 embedding**。改动切块逻辑时必须同步改它,
#: 否则新旧策略切出来的块会共用同一个 id, 库里会留下一批永远不被更新的
#: 陈旧向量。
CHUNKER_VERSION = "1"

#: 一个 CJK 字符大约折算多少个 token。用于把字符预算换算成 token 预算 ——
#: 只是量级估计, 不追求准确。
CJK_RATIO = 0.7

#: 一个拉丁字符大约折算多少个 token。
LATIN_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    """切块参数。

    默认值来自一个具体取舍: 检索块 ~800 字符, 在中文里约 550 token,
    落在所有主流 embedding 模型的舒适区内, 同时长到足以容纳一个完整
    论点。父块 ~3200 字符则是"给 LLM 读"的合适长度。
    """

    #: 检索块的目标长度 (字符)。
    chunk_chars: int = 800

    #: 相邻检索块的重叠 (字符)。
    #:
    #: 重叠存在的唯一理由是: 无论边界切在哪里, 总有一句话会被切开。
    #: 重叠让被切开的那句话在两个块里各自完整出现一次, 于是不管命中
    #: 哪一块, 它都是完整的。120 字符约等于一到两句话。
    overlap_chars: int = 120

    #: 父块的目标长度 (字符)。
    parent_chars: int = 3200

    #: 低于这个长度的块不单独成块, 而是并入相邻块。
    #:
    #: 碎块是召回质量的隐形杀手: 一个只有 "3.2 退款" 五个字的块, 向量
    #: 几乎与任何提到退款的查询都相似, 于是它会挤占 top_k 的名额, 而
    #: 它本身没有任何信息量。
    min_chunk_chars: int = 120

    #: 单个块的硬上限。原子块 (表格/代码) 超过它才会被迫切开。
    max_chunk_chars: int = 2400

    def __post_init__(self) -> None:
        if self.overlap_chars >= self.chunk_chars:
            raise ValueError(
                f"overlap_chars ({self.overlap_chars}) 必须小于 "
                f"chunk_chars ({self.chunk_chars}), 否则切块永远无法前进。"
            )

        if self.parent_chars < self.chunk_chars:
            raise ValueError(
                f"parent_chars ({self.parent_chars}) 不能小于 "
                f"chunk_chars ({self.chunk_chars}) —— 父块的意义就是更大。"
            )

    @classmethod
    def from_pipeline(cls, pipeline: dict[str, Any] | None) -> ChunkerConfig:
        """从 ``ScanProfile.pipeline`` 的 ``chunker`` 段构造。

        未知键直接忽略而不是报错: pipeline 是一个自由 JSON 字段, 未来
        会长出别的段 (embedder、enable_stages), 让切块器为了一个它不认识
        的键去中止扫描是不合理的。
        """

        section = (pipeline or {}).get("chunker") or {}
        known = {f: section[f] for f in _CONFIG_FIELDS if f in section}

        return cls(**known)


_CONFIG_FIELDS = (
    "chunk_chars",
    "overlap_chars",
    "parent_chars",
    "min_chunk_chars",
    "max_chunk_chars",
)


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """一个待写入的 chunk。

    "Draft"是因为它还不是 ORM 对象 —— 切块器不认识数据库, 这让它能被
    单独测试, 也让"切块"和"写库"可以在不同的事务边界里发生。
    """

    id: str
    granularity: Granularity
    chunk_index: int
    text: str

    parent_id: str | None = None

    #: 送进 embedding 前拼在文本之前的来源前缀。
    #: 只影响 embedding 的输入, **不**影响 ``text`` 本身 ——
    #: 返回给用户看的必须是原文, 不能带上我们加的料。
    context_prefix: str | None = None

    #: 字符数折算的 token 估计。写进 ``chunk.token_count``, 供预算控制用。
    token_estimate: int = 0

    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def embed_text(self) -> str:
        return f"{self.context_prefix}\n\n{self.text}" if self.context_prefix else self.text


def estimate_tokens(text: str) -> int:
    """按字符构成粗估 token 数。"""

    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk

    return int(cjk * CJK_RATIO + other * LATIN_RATIO) + 1


def chunk_document(
    parsed: ParsedDocument,
    *,
    doc_uri: str,
    content_hash: str,
    config: ChunkerConfig | None = None,
) -> tuple[ChunkDraft, ...]:
    """切一篇文档, 返回 parent 块与 chunk 块的混合序列。

    ``doc_uri`` 与 ``content_hash`` 只用于算 id —— 同样的内容在同样的
    位置必然算出同样的 id, 这是"重复扫描不产生重复向量"的基础, 也是
    "删掉 Zvec 能从 SQLite 重建"的前提 (见 ``db.base.chunk_vector_id``)。
    """

    cfg = config or ChunkerConfig()
    groups = _group(parsed.blocks, cfg)

    drafts: list[ChunkDraft] = []
    chunk_index = 0

    for parent_index, group in enumerate(groups):
        parent_text = "\n\n".join(b.text for b in group)
        prefix = _context_prefix(parsed, group)

        parent_id = chunk_vector_id(
            doc_uri, content_hash, CHUNKER_VERSION,
            Granularity.PARENT.value, parent_index,
        )

        drafts.append(ChunkDraft(
            id=parent_id,
            granularity=Granularity.PARENT,
            chunk_index=parent_index,
            text=parent_text,
            context_prefix=prefix,
            token_estimate=estimate_tokens(parent_text),
            meta=_locator_meta(group),
        ))

        for piece in _split_group(group, cfg):
            drafts.append(ChunkDraft(
                id=chunk_vector_id(
                    doc_uri, content_hash, CHUNKER_VERSION,
                    Granularity.CHUNK.value, chunk_index,
                ),
                granularity=Granularity.CHUNK,
                chunk_index=chunk_index,
                text=piece.text,
                parent_id=parent_id,
                context_prefix=_context_prefix(parsed, piece.blocks),
                token_estimate=estimate_tokens(piece.text),
                meta=_locator_meta(piece.blocks),
            ))
            chunk_index += 1

    return tuple(drafts)


# --------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    blocks: tuple[Block, ...]


def _group(blocks: Sequence[Block], cfg: ChunkerConfig) -> list[tuple[Block, ...]]:
    """把块聚成父块。

    聚合规则只有两条, 优先级从高到低:

    1. **遇到标题就断开** —— 标题是作者亲手划下的语义边界, 它比任何
       长度启发式都可靠。唯一的例外是标题紧接着上一个标题 (章标题下面
       马上是节标题), 那时不该产出一个只有一行标题的空父块。
    2. **超过 parent_chars 就断开** —— 兜底。没有标题的长文档
       (纯文本、日志) 全靠这条。
    """

    groups: list[tuple[Block, ...]] = []
    current: list[Block] = []
    size = 0

    def flush() -> None:
        nonlocal current, size

        if current:
            groups.append(tuple(current))
            current = []
            size = 0

    for block in blocks:
        if not block.text.strip():
            continue

        if block.is_heading:
            # 只有已经攒了正文才断开。连续的标题应当粘在一起,
            # 让"第三章 / 3.1 概述"成为同一个父块的开头。
            if current and not all(b.is_heading for b in current):
                flush()

        elif size + block.char_count > cfg.parent_chars and current:
            flush()

        current.append(block)
        size += block.char_count

    flush()

    return groups


def _split_group(group: Sequence[Block], cfg: ChunkerConfig) -> list[_Piece]:
    """把一个父块切成若干检索块。"""

    pieces: list[_Piece] = []
    buffer: list[Block] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size

        if not buffer:
            return

        text = "\n\n".join(b.text for b in buffer).strip()

        if text:
            pieces.append(_Piece(text=text, blocks=tuple(buffer)))

        buffer = []
        size = 0

    for block in group:
        # 原子块 (表格/代码) 单独成块, 绝不与相邻内容混合, 也不从中间切,
        # 除非它自己就超过了硬上限。
        if block.kind.atomic:
            flush()

            if block.char_count > cfg.max_chunk_chars:
                for part in _hard_split(block.text, cfg.max_chunk_chars):
                    pieces.append(_Piece(text=part, blocks=(block,)))
            else:
                pieces.append(_Piece(text=block.text, blocks=(block,)))

            continue

        if block.char_count > cfg.chunk_chars:
            flush()

            for part in _split_text(block.text, cfg):
                pieces.append(_Piece(text=part, blocks=(block,)))

            continue

        if size + block.char_count > cfg.chunk_chars and buffer:
            flush()

        buffer.append(block)
        size += block.char_count

    flush()

    return _merge_tiny(pieces, cfg)


def _split_text(text: str, cfg: ChunkerConfig) -> list[str]:
    """把一段超长文本按句子边界切开, 带重叠。

    先找句号类标点, 找不到才按字符硬切。这个顺序很重要: 按字符硬切
    会把 "年利率不超过 24" 和 "%" 分到两块, 后者毫无意义, 前者则
    改变了原意。
    """

    parts: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + cfg.chunk_chars, length)

        if end < length:
            end = _sentence_boundary(text, start, end, cfg)

        piece = text[start:end].strip()

        if piece:
            parts.append(piece)

        if end >= length:
            break

        start = max(end - cfg.overlap_chars, start + 1)

    return parts


#: 句子结束标记。中英文都要有 —— 只认 "." 的话中文长文会一刀不切。
_SENTENCE_ENDS = "。！？；\n.!?;"  # noqa: RUF001 - 全角标点是中文断句的必要条件


def _sentence_boundary(text: str, start: int, end: int, cfg: ChunkerConfig) -> int:
    """在 ``end`` 附近往回找一个句子边界。

    往回找而不是往前找: 往前找会让块超出预算, 而预算是为了保护
    embedding 模型的输入上限, 不能突破。
    """

    # 最多往回找 30%: 再远就会切出一个远小于目标的块, 得不偿失
    floor = max(start + cfg.min_chunk_chars, end - int(cfg.chunk_chars * 0.3))

    for index in range(end - 1, floor - 1, -1):
        if text[index] in _SENTENCE_ENDS:
            return index + 1

    return end


def _hard_split(text: str, limit: int) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _merge_tiny(pieces: list[_Piece], cfg: ChunkerConfig) -> list[_Piece]:
    """把过短的块并进相邻块。

    并到**后一块**而不是前一块: 过短的块绝大多数是小标题、图注、
    列表首项 —— 它们在语义上是后面内容的引子。合并方向选错, 得到的
    是"上一节的结尾 + 下一节的标题"这种跨节混合块。

    原子块 (代码/表格) 是合并的禁区, 两个方向都是。把一句散文并进
    代码块, 得到的向量既不像代码也不像散文, 两种查询都召不回它 ——
    这比留一个偏短的块糟得多。
    """

    if not pieces:
        return pieces

    merged: list[_Piece] = []
    pending: _Piece | None = None

    def park(piece: _Piece) -> None:
        """没有"后一块"可并了, 退而求其次并回前一块。"""

        if merged and not _is_atomic(merged[-1]):
            last = merged[-1]
            merged[-1] = _Piece(
                text=f"{last.text}\n\n{piece.text}",
                blocks=last.blocks + piece.blocks,
            )
        else:
            # 实在无处可并就保留 —— 一篇只有一句话的文档也该能被搜到。
            merged.append(piece)

    for piece in pieces:
        atomic = _is_atomic(piece)

        if pending is not None:
            if atomic:
                park(pending)
            else:
                piece = _Piece(
                    text=f"{pending.text}\n\n{piece.text}",
                    blocks=pending.blocks + piece.blocks,
                )

            pending = None

        if atomic or len(piece.text) >= cfg.min_chunk_chars:
            merged.append(piece)
            continue

        pending = piece

    if pending is not None:
        park(pending)

    return merged


def _is_atomic(piece: _Piece) -> bool:
    return any(block.kind.atomic for block in piece.blocks)


def _context_prefix(parsed: ParsedDocument, blocks: Sequence[Block]) -> str | None:
    """拼出送进 embedding 的来源前缀。

    形如 ``采购合同.docx > 第三章 违约责任 > 3.2 逾期交付``。

    这一行字对召回的提升, 经验上大于换一个更强的 embedding 模型:
    孤立的一段"逾期超过 30 日的, 按日万分之五计算"没有任何线索表明
    它属于哪份合同的哪一条, 加上路径之后, "采购合同的违约金怎么算"
    这样的查询才有机会命中它。
    """

    path = next((b.path for b in blocks if b.path), ())
    parts: list[str] = []

    if parsed.title:
        parts.append(parsed.title)

    # 路径首段常常就是文档标题 (markdown 的一级标题), 去重免得
    # 前缀里出现两遍同样的字
    parts.extend(p for p in path if p and p not in parts)

    if not parts:
        return None

    return " > ".join(parts)


def _locator_meta(blocks: Iterable[Block]) -> dict[str, Any]:
    """收集这一块覆盖了原件的哪些位置。

    去重但保序: 一个跨了 p.3 和 p.4 的块, 引用时要能说出"第 3-4 页"。
    """

    locators: list[str] = []

    for block in blocks:
        if block.locator and block.locator not in locators:
            locators.append(block.locator)

    meta: dict[str, Any] = {}

    if locators:
        meta["locators"] = locators

    kinds = {b.kind.value for b in blocks}

    if kinds:
        meta["block_kinds"] = sorted(kinds)

    return meta


def default_config_for(doc_class: DocClass) -> ChunkerConfig:
    """按文档类型给出更合适的默认参数。

    表格类用更大的块: 一个行组块本身就是完整语义单元, 用 800 字符的
    预算去切它, 只会把表头和数据切散。
    """

    if doc_class is DocClass.TABLE:
        return ChunkerConfig(chunk_chars=1600, overlap_chars=0, parent_chars=6400)

    if doc_class is DocClass.CODE:
        # 代码靠缩进和函数边界理解, 重叠帮不上忙, 反而会重复整段函数
        return ChunkerConfig(chunk_chars=1200, overlap_chars=0)

    return ChunkerConfig()


__all__ = [
    "CHUNKER_VERSION",
    "ChunkDraft",
    "ChunkerConfig",
    "chunk_document",
    "default_config_for",
    "estimate_tokens",
]
