#---------------------------------------------------------
# 解析器协议与中间表示
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""解析的产物不是一根字符串, 而是一串**带结构的块**。

这是整个解析层唯一重要的设计决定, 值得说清楚为什么:

如果 ``parse()`` 返回 ``str``, 那么下游切块就只剩"按字数硬切"一条路,
于是一句话会被从中间劈开、一张表会被拦腰截断、章节标题会和它所属的
正文失去关联。而这些恰恰是检索质量最直接的来源 —— 一个带着
"第 3 章 > 3.2 退款政策"前缀的 chunk, 比一个裸的文本片段好用得多。

所以中间表示必须保留三件事, 且只保留这三件:

1. **块边界** —— 哪里可以安全地切开 (段落、单元格行、幻灯片)
2. **结构路径** —— 这段话在文档的哪个位置 (章节层级)
3. **定位信息** —— 它在原件的哪一页/哪个 sheet/第几张幻灯片

第三件是给引用用的。Agent 说"依据是这份合同的第 7 页", 用户能翻到
第 7 页去核对; 说"依据是这份合同", 用户得自己找。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from shovel.domain.enums import BlockKind, DocClass

#: 单篇文档提取文本的上限 (字符)。
#:
#: 这不是"文件大小上限"—— 那个由 ScanProfile.max_file_bytes 管, 且发生在
#: 读盘之前。这道闸门防的是另一件事: 一个 80 MB 的 xlsx 解压后可能是
#: 几亿个字符, 在用户的笔记本上足以把内存吃光。超过就截断并标记,
#: 让一篇畸形文档降级成"索引了前一部分", 而不是让整个扫描进程被 OOM。
DEFAULT_MAX_CHARS = 4_000_000


@dataclass(frozen=True, slots=True)
class Block:
    """一段语义上完整的文本。

    ``path`` 是从根到当前位置的结构路径, 例如
    ``("采购合同", "第三章 违约责任", "3.2 逾期交付")``。它会被切块阶段
    拼成 ``context_prefix`` 送进 embedding —— 同一句"甲方应在 30 日内支付",
    出现在"付款条款"下和出现在"违约责任"下, 语义并不相同, 而只有路径
    能把这个差别告诉模型。
    """

    text: str
    kind: BlockKind = BlockKind.PARAGRAPH

    #: 标题层级; 非标题块为 0。
    level: int = 0

    path: tuple[str, ...] = ()

    #: 人类可读的原件定位, 例如 ``p.7`` / ``Sheet1!A1:D20`` / ``slide 5``。
    #: 格式**刻意不统一**: 它是给人看的, 而"第 7 页"和"Sheet1 的 A1:D20"
    #: 本来就是两种不同的定位方式, 强行抽象成 (start, end) 只会让两边都变得难懂。
    locator: str | None = None

    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_heading(self) -> bool:
        return self.kind is BlockKind.HEADING

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """一篇文档解析后的全部内容。"""

    blocks: tuple[Block, ...]

    title: str | None = None
    lang: str | None = None

    #: 解析器实际认定的类型。可能与 discover 阶段按扩展名猜的不同 ——
    #: 以这里为准, 因为解析器是真的把文件打开看过了。
    doc_class: DocClass = DocClass.UNKNOWN

    #: 产出本文档的解析器名字与版本。存进 Document.source_meta,
    #: 这样"换了新版解析器要重跑哪些文档"是一句 SQL 就能回答的问题。
    parser: str = "unknown"
    parser_version: str = "0"

    #: 是否因为 ``max_chars`` 被截断。会写进 source_meta 并记一条 warn ——
    #: 一篇被悄悄截掉一半的文档, 比一篇解析失败的文档更危险: 后者有
    #: 错误可查, 前者看起来一切正常。
    truncated: bool = False

    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        return sum(b.char_count for b in self.blocks)

    @property
    def is_empty(self) -> bool:
        return not any(b.text.strip() for b in self.blocks)


@dataclass(frozen=True, slots=True)
class ParseHint:
    """解析前已知的信息。

    解析器可以用它做路由和取名, 但**不能依赖它的正确性** ——
    ``doc_class`` 是 discover 阶段按扩展名猜的, 一个被重命名成 ``.txt``
    的 PDF 完全可能走到这里。所以每个解析器都仍要对自己拿到的字节
    做基本校验。
    """

    uri: str
    filename: str
    doc_class: DocClass = DocClass.UNKNOWN
    mime: str | None = None
    size_bytes: int | None = None
    max_chars: int = DEFAULT_MAX_CHARS

    @property
    def suffix(self) -> str:
        """小写扩展名, 含点。没有扩展名时返回空串。"""

        name = self.filename
        dot = name.rfind(".")

        if dot <= 0 or dot == len(name) - 1:
            return ""

        return name[dot:].lower()


@dataclass(frozen=True, slots=True)
class ParserSpec:
    """一个解析器的静态描述。

    ``version`` 参与 ``Document.source_meta``, 并且**不**参与 chunk id 的
    计算 —— chunk id 只认 ``chunker_version``。两者分开是有意的: 解析器
    改版会让文本内容变化(从而 indexed_hash 对不上, 触发重跑), 而切块
    策略改版会让同样的文本切出不同的边界。混在一起就没法只重跑其中一类。
    """

    name: str
    version: str
    doc_classes: frozenset[DocClass]

    #: 认领的扩展名。**扩展名优先于 doc_class** —— 因为 ``DocClass.OFFICE_DOC``
    #: 同时覆盖 docx / xlsx / pptx, 而这三者需要三个完全不同的解析器。
    suffixes: frozenset[str]

    #: 兜底解析器。只有一个 (纯文本), 在所有其他解析器都不认领时被调用。
    is_fallback: bool = False


class Parser(Protocol):
    """所有解析器必须长成的样子。

    ``parse`` 是**同步**的, 这和 Connector 的全 async 约定不同, 是刻意的:
    解析是 CPU 密集型工作 (pypdf 解压、lxml 建树), 写成 async 不会让它
    变快, 只会让它把事件循环堵死 —— 扫描期间的进度输出、取消信号都
    会跟着卡住。编排层用 ``asyncio.to_thread`` 把它挪出事件循环, 这样
    "politeness budget"(max_concurrency) 才真正作用在瓶颈上。

    与 Connector 一样: **只实现, 不继承**。
    """

    spec: ClassVar[ParserSpec]

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument: ...


# --------------------------------------------------------------------- #
# 解析器的公共工具
# --------------------------------------------------------------------- #
def clean_text(value: str) -> str:
    """规整一段提取出来的文本。

    做三件事: 统一换行、去掉零宽字符、折叠行尾空白。

    零宽字符必须去掉而不是保留: PDF 和 Word 里大量存在 ``\\u200b`` 与
    软连字符, 它们肉眼不可见, 却会让 "退款政策" 和 "退\\u200b款政策"
    在 embedding 空间里落到不同的位置, 也会让全文检索匹配不上。
    """

    if not value:
        return ""

    text = value.replace("\r\n", "\n").replace("\r", "\n")

    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad"):
        text = text.replace(ch, "")

    # 不可断空格在 PDF 里极常见, 留着会让按空白切词的一侧行为异常
    text = text.replace("\u00a0", " ")

    lines = [line.rstrip() for line in text.split("\n")]

    return "\n".join(lines).strip()


def collapse_blank_lines(value: str) -> str:
    """把连续空行压成一个。

    PDF 抽出来的文本经常每隔一行就有一个空行, 不压缩的话按空行分段
    会切出一堆单行碎片。
    """

    out: list[str] = []
    blank = False

    for line in value.split("\n"):
        if line.strip():
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True

    return "\n".join(out).strip()


class HeadingStack:
    """维护"当前位于哪一级标题之下"。

    解析器逐块往前走, 遇到标题就 ``push``, 遇到正文就 ``current()``。
    把它抽出来是因为 markdown / docx / pptx 三个解析器都要做同一件事,
    而"遇到更高层级的标题要弹出多少层"这种逻辑写三遍就会错三种写法。
    """

    __slots__ = ("_stack",)

    def __init__(self, root: str | None = None) -> None:
        # (level, 文本)。root 占 level 0, 让"文档标题"天然成为路径的第一段。
        self._stack: list[tuple[int, str]] = []

        if root:
            self._stack.append((0, root))

    def push(self, level: int, text: str) -> None:
        # 同级或更高级的标题出现 = 上一节结束, 把它们全弹掉。
        # 用 >= 而不是 >: 两个平级的 "## 第二章" 之间, 后者不该挂在前者下面。
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()

        self._stack.append((level, text))

    def current(self) -> tuple[str, ...]:
        return tuple(text for _, text in self._stack)


def take(
    blocks: Sequence[Block],
    max_chars: int,
) -> tuple[tuple[Block, ...], bool]:
    """按字符上限截断块序列, 返回 (保留的块, 是否被截断)。

    在**块边界**上截断, 不切开某一块。半块文本在检索里几乎没有价值,
    而它带来的"看起来有内容"的假象会掩盖截断本身。
    """

    kept: list[Block] = []
    total = 0

    for block in blocks:
        if total + block.char_count > max_chars:
            return tuple(kept), True

        kept.append(block)
        total += block.char_count

    return tuple(kept), False


__all__ = [
    "DEFAULT_MAX_CHARS",
    "Block",
    "BlockKind",
    "HeadingStack",
    "ParseHint",
    "ParsedDocument",
    "Parser",
    "ParserSpec",
    "clean_text",
    "collapse_blank_lines",
    "take",
]
