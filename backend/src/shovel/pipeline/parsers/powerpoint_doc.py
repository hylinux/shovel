#---------------------------------------------------------
# PowerPoint 解析器 (python-pptx)
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""一张幻灯片 = 一个块。

这是本解析器唯一的结构决定, 但它是对的: 幻灯片本身就是作者划好的
语义单元 —— 一页讲一件事。再往下切 (按文本框) 会得到 "Q3 营收" 和
"同比 +18%" 两个互相看不懂的碎片; 往上合 (整份 deck 一块) 则会把
二十个不相干的主题糊在一起。

两个容易被漏掉的点:

* **演讲者备注**才是 deck 里信息密度最高的地方。正文往往只有关键词,
  完整的论述在备注里。所以备注必须抽, 但标成 ``NOTE`` —— 它是解释性
  内容, 检索时应当能与正文区分开。
* **形状顺序不等于阅读顺序**。python-pptx 给的是 z-order, 与人眼的
  "从上到下、从左到右"未必一致。这里按 (top, left) 重排, 让抽出来的
  文本顺序接近观众实际看到的顺序。
"""

from __future__ import annotations

import io
from typing import Any, ClassVar

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import EmptyDocumentError, ParseError
from shovel.pipeline.parsers.base import (
    Block,
    ParsedDocument,
    ParseHint,
    ParserSpec,
    clean_text,
    take,
)


class PowerPointParser:

    spec: ClassVar[ParserSpec] = ParserSpec(
        name="powerpoint",
        version="1",
        doc_classes=frozenset({DocClass.OFFICE_DOC}),
        suffixes=frozenset({".pptx"}),
    )

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        from pptx import Presentation

        try:
            deck = Presentation(io.BytesIO(payload))
        except Exception as exc:
            raise ParseError(
                hint.uri,
                f"无法打开 PowerPoint 文档: {exc}",
                hint="确认它是 .pptx (而非旧版 .ppt), 且文件未损坏。",
            ) from exc

        blocks: list[Block] = []
        deck_title: str | None = None

        for number, slide in enumerate(deck.slides, start=1):
            title = _slide_title(slide)

            if number == 1 and title:
                deck_title = title

            path = (title,) if title else ()
            locator = f"slide {number}"

            if title:
                blocks.append(Block(
                    text=title,
                    kind=BlockKind.HEADING,
                    level=1,
                    path=path,
                    locator=locator,
                ))

            body = _slide_body(slide, skip=title)

            if body:
                blocks.append(Block(
                    text=body,
                    kind=BlockKind.PARAGRAPH,
                    path=path,
                    locator=locator,
                ))

            notes = _slide_notes(slide)

            if notes:
                blocks.append(Block(
                    text=notes,
                    kind=BlockKind.NOTE,
                    path=path,
                    locator=f"{locator} (备注)",
                ))

        if not blocks:
            raise EmptyDocumentError(hint.uri)

        kept, truncated = take(tuple(blocks), hint.max_chars)

        return ParsedDocument(
            blocks=kept,
            title=deck_title or hint.filename,
            doc_class=DocClass.OFFICE_DOC,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
            meta={"slides": len(deck.slides)},
        )


def _slide_title(slide: Any) -> str | None:
    """取幻灯片标题占位符的文字。

    只认占位符, 不去猜"最上面那个大字号的框就是标题"。猜错的后果是
    把正文第一句当成标题, 从而污染整页所有块的结构路径。
    """

    try:
        placeholder = slide.shapes.title
    except Exception:
        return None

    if placeholder is None:
        return None

    text = clean_text(placeholder.text or "")

    return text or None


def _slide_body(slide: Any, *, skip: str | None) -> str:
    """把一页里除标题外的所有文字按阅读顺序拼起来。"""

    pieces: list[tuple[int, int, str]] = []

    for shape in slide.shapes:
        if shape.has_table:
            text = _table_text(shape.table)
        elif getattr(shape, "has_text_frame", False):
            text = clean_text(shape.text_frame.text or "")
        else:
            continue

        if not text or text == skip:
            continue

        # top/left 在某些形状上是 None (继承自版式), 用 0 兜底,
        # 让它们排在最前而不是让排序整个崩掉
        pieces.append((int(shape.top or 0), int(shape.left or 0), text))

    pieces.sort(key=lambda item: (item[0], item[1]))

    return "\n".join(text for _, _, text in pieces).strip()


def _table_text(table: Any) -> str:
    rows: list[str] = []

    for row in table.rows:
        cells = [
            clean_text(cell.text or "").replace("\n", " ").replace("|", "\\|")
            for cell in row.cells
        ]

        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")

    return "\n".join(rows)


def _slide_notes(slide: Any) -> str:
    try:
        if not slide.has_notes_slide:
            return ""

        frame = slide.notes_slide.notes_text_frame
    except Exception:
        return ""

    if frame is None:
        return ""

    return clean_text(frame.text or "")


PARSER = PowerPointParser()

__all__ = ["PARSER", "PowerPointParser"]
