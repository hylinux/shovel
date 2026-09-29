#---------------------------------------------------------
# Markdown 解析器
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""Markdown 的价值全在它的结构里, 所以这里**不**把它渲染成 HTML 再抽文本。

渲染再抽取会丢掉两样东西: 标题层级 (变成一串平铺的段落) 和围栏代码块
的边界 (变成普通文字)。而这两样正是切块阶段最需要的。

刻意不引入 markdown 库: 需要识别的只有标题、围栏代码、表格、列表四件事,
它们都是**行首模式**, 手写不到一百行。引入一个依赖去换这一百行, 换来的
是一个渲染器的全部行为 (HTML 转义、扩展语法、插件), 而我们一样都不需要。
"""

from __future__ import annotations

import re
from typing import ClassVar

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import EmptyDocumentError, ParseError
from shovel.pipeline.parsers.base import (
    Block,
    HeadingStack,
    ParsedDocument,
    ParseHint,
    ParserSpec,
    clean_text,
    take,
)
from shovel.pipeline.parsers.plain_text import decode

#: ATX 标题: ``## 标题``。
#: 要求 ``#`` 之后必须有空白, 这样 ``#hashtag`` 和 Python 注释 ``#!/usr/bin``
#: 不会被误判成标题。
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

#: 围栏代码块的起止行: ``` 或 ~~~, 允许前置缩进。
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)")

#: 表格分隔行: ``|---|:---:|``。用它判定"上一行是表头"。
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")

#: YAML front matter 的界定行。
_FRONT_MATTER = "---"


class MarkdownParser:

    spec: ClassVar[ParserSpec] = ParserSpec(
        name="markdown",
        version="1",
        doc_classes=frozenset({DocClass.MARKDOWN}),
        suffixes=frozenset({".md", ".markdown", ".mdx"}),
    )

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        try:
            text, encoding = decode(payload)
        except UnicodeDecodeError as exc:
            raise ParseError(hint.uri, "无法确定文本编码。") from exc

        lines = clean_text(text).split("\n")
        front_matter, start = _read_front_matter(lines)

        title = front_matter.get("title")
        stack = HeadingStack()

        blocks = _walk(lines[start:], stack, offset=start)

        if not blocks:
            raise EmptyDocumentError(hint.uri)

        # 文档标题: front matter 优先, 其次第一个一级标题, 最后回落文件名。
        # 顺序就是可信度顺序 —— front matter 是作者明确写下的。
        if not title:
            title = next(
                (b.text for b in blocks if b.is_heading and b.level == 1),
                None,
            )

        kept, truncated = take(blocks, hint.max_chars)

        return ParsedDocument(
            blocks=kept,
            title=title or hint.filename,
            doc_class=DocClass.MARKDOWN,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
            meta={"encoding": encoding, "front_matter": front_matter},
        )


def _read_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """读 YAML front matter, 返回 (键值对, 正文起始行下标)。

    只解析 ``key: value`` 这一种形态, 不引 YAML 解析器。嵌套结构在
    front matter 里存在, 但它们的值 (标签列表、日期) 对检索的帮助
    远小于引入一个解析器的代价; 真正有用的 ``title`` 恰好总是标量。
    """

    if not lines or lines[0].strip() != _FRONT_MATTER:
        return {}, 0

    meta: dict[str, str] = {}

    for index in range(1, len(lines)):
        line = lines[index].strip()

        if line == _FRONT_MATTER:
            return meta, index + 1

        key, sep, value = line.partition(":")

        if sep and key.strip():
            meta[key.strip()] = value.strip().strip("\"'")

    # 没有闭合的 ``---``: 那第一行就不是 front matter, 而是一条水平分割线
    return {}, 0


def _walk(lines: list[str], stack: HeadingStack, *, offset: int) -> tuple[Block, ...]:
    """逐行扫描, 产出块。"""

    blocks: list[Block] = []
    buffer: list[str] = []
    buffer_kind = BlockKind.PARAGRAPH
    buffer_start = 1

    fence: str | None = None

    def flush() -> None:
        nonlocal buffer, buffer_kind

        body = "\n".join(buffer).strip()
        buffer = []

        if not body:
            buffer_kind = BlockKind.PARAGRAPH
            return

        blocks.append(Block(
            text=body,
            kind=buffer_kind,
            path=stack.current(),
            locator=f"line {buffer_start + offset}",
        ))
        buffer_kind = BlockKind.PARAGRAPH

    for index, raw in enumerate(lines, start=1):
        # --- 围栏代码块: 内部一切模式都不解释 ---
        fence_match = _FENCE.match(raw)

        if fence is not None:
            buffer.append(raw)

            if fence_match and fence_match.group(1).startswith(fence):
                flush()
                fence = None

            continue

        if fence_match and fence_match.group(1):
            flush()
            fence = fence_match.group(1)[:3]
            buffer_kind = BlockKind.CODE
            buffer_start = index
            buffer.append(raw)
            continue

        heading = _HEADING.match(raw)

        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            stack.push(level, title)

            blocks.append(Block(
                text=title,
                kind=BlockKind.HEADING,
                level=level,
                # 标题自己的 path 含它本身, 这样 chunk 的前缀里
                # "章节名"不会因为正文块恰好为空而丢失
                path=stack.current(),
                locator=f"line {index + offset}",
            ))
            continue

        if not raw.strip():
            flush()
            continue

        # --- 表格: 以分隔行为准 ---
        if _TABLE_RULE.match(raw) and buffer:
            buffer_kind = BlockKind.TABLE
            buffer.append(raw)
            continue

        if buffer_kind is BlockKind.TABLE and raw.lstrip().startswith("|"):
            buffer.append(raw)
            continue

        if not buffer:
            buffer_start = index
            buffer_kind = (
                BlockKind.LIST_ITEM if _LIST_ITEM.match(raw) else BlockKind.PARAGRAPH
            )

        buffer.append(raw)

    flush()

    return tuple(blocks)


PARSER = MarkdownParser()

__all__ = ["PARSER", "MarkdownParser"]
