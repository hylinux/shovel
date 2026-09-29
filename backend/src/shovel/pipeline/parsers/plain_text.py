#---------------------------------------------------------
# 纯文本解析器 (txt / log / 代码)
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""最简单的一个解析器, 但有一件事不简单: 猜编码。

Windows 上的中文 txt 大量是 GBK/GB18030 而不是 UTF-8。用 utf-8 强解会
抛 UnicodeDecodeError, 用 ``errors="replace"`` 则会得到一整篇 ``????`` ——
后者更糟, 因为它会被当成"解析成功"写进索引, 之后再也搜不到。

所以这里按**有把握到没把握**的顺序依次尝试, 并把最终采用的编码记进
``meta``, 让"为什么这篇文档是乱码"成为一个可查的事实。
"""

from __future__ import annotations

from typing import ClassVar

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import EmptyDocumentError, ParseError
from shovel.pipeline.parsers.base import (
    Block,
    ParsedDocument,
    ParseHint,
    ParserSpec,
    clean_text,
    collapse_blank_lines,
    take,
)

#: 依次尝试的编码。顺序即优先级, 每一个的位置都有理由:
#:
#: 1. ``utf-8-sig``  带 BOM 的 UTF-8。必须排在 utf-8 前面, 否则 BOM 会
#:    变成正文的第一个字符, 让首个标题永远匹配不上。
#: 2. ``utf-8``      当代默认。
#: 3. ``gb18030``    国标全集, 向下兼容 GBK/GB2312。中文 Windows 的实际默认。
#: 4. ``big5``       繁体。
#: 5. ``utf-16``     Windows 记事本"Unicode"另存的结果, 带 BOM 可判定。
#:
#: latin-1 **不在列表里**, 尽管它能解码任何字节序列 —— 正因为它永远
#: 不会失败, 放进来就等于取消了整个探测机制。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16")

#: 判定"这是二进制文件"的依据: 前若干字节里出现 NUL。
#: 文本文件里不会有 NUL, 而几乎所有二进制格式的头部都有。
_BINARY_PROBE_BYTES = 8192

#: 一个"段"最多包含多少行。超长的日志文件没有空行, 不设上限的话
#: 整个文件会变成一个块, 切块阶段就失去了所有边界信息。
_MAX_LINES_PER_BLOCK = 60


def decode(payload: bytes) -> tuple[str, str]:
    """把字节解成文本, 返回 (文本, 采用的编码)。"""

    if payload.startswith(b"\xff\xfe") or payload.startswith(b"\xfe\xff"):
        return payload.decode("utf-16"), "utf-16"

    for encoding in _ENCODINGS:
        try:
            return payload.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue

    raise UnicodeDecodeError("utf-8", payload[:16], 0, 1, "无法确定文本编码")


def _looks_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:_BINARY_PROBE_BYTES]


class PlainTextParser:
    """按空行分段。"""

    spec: ClassVar[ParserSpec] = ParserSpec(
        name="plain_text",
        version="1",
        doc_classes=frozenset({
            DocClass.PLAIN_TEXT,
            DocClass.CODE,
            DocClass.UNKNOWN,
        }),
        suffixes=frozenset({".txt", ".log", ".text"}),
        is_fallback=True,
    )

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        if _looks_binary(payload):
            raise ParseError(
                hint.uri,
                "文件内容看起来是二进制, 不是文本。",
                hint="若它确实是文本, 请检查是否被压缩或加密过。",
            )

        try:
            text, encoding = decode(payload)
        except UnicodeDecodeError as exc:
            raise ParseError(
                hint.uri,
                f"无法确定文本编码 (尝试过 {', '.join(_ENCODINGS)})。",
            ) from exc

        body = collapse_blank_lines(clean_text(text))

        if not body:
            raise EmptyDocumentError(hint.uri)

        is_code = hint.doc_class is DocClass.CODE
        kind = BlockKind.CODE if is_code else BlockKind.PARAGRAPH

        blocks = tuple(
            Block(text=segment, kind=kind, locator=f"line {start}")
            for segment, start in _segments(body)
        )

        kept, truncated = take(blocks, hint.max_chars)

        return ParsedDocument(
            blocks=kept,
            title=hint.filename,
            doc_class=DocClass.CODE if is_code else DocClass.PLAIN_TEXT,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
            meta={"encoding": encoding},
        )


def _segments(body: str) -> list[tuple[str, int]]:
    """按空行切段, 并对超长段按行数强制断开。

    返回 (段落文本, 起始行号)。行号从 1 开始, 和编辑器一致 ——
    引用里说"第 0 行"没有人会觉得自然。
    """

    out: list[tuple[str, int]] = []
    buffer: list[str] = []
    start = 1

    def flush() -> None:
        nonlocal buffer, start

        if buffer:
            chunk = "\n".join(buffer).strip()
            if chunk:
                out.append((chunk, start))
            buffer = []

    for lineno, line in enumerate(body.split("\n"), start=1):
        if not line.strip():
            flush()
            start = lineno + 1
            continue

        if not buffer:
            start = lineno

        buffer.append(line)

        if len(buffer) >= _MAX_LINES_PER_BLOCK:
            flush()
            start = lineno + 1

    flush()

    return out


PARSER = PlainTextParser()

__all__ = ["PARSER", "PlainTextParser", "decode"]
