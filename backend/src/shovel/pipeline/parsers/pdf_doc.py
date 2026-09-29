#---------------------------------------------------------
# PDF 解析器 (pypdf)
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""按页抽取文本。

三件事在 PDF 上和别的格式不一样, 都在这里处理掉:

1. **页码是刚需。** PDF 是唯一一种用户脑子里有"第几页"这个概念的格式,
   所以每个块都带 ``p.N``。引用能落到页, 核对成本就从"翻完整份"降到
   "翻一页"。

2. **扫描件没有文本层。** 一份 200 页的扫描合同, pypdf 会顺利返回 200
   个空字符串。这不能当成"解析成功但内容为空"—— 必须报 EmptyDocumentError,
   否则用户会得到一篇存在于索引里、却永远搜不到的文档。

3. **加密。** pypdf 对加密文档不会抛异常, 而是返回空文本, 表现和扫描件
   一模一样。所以必须显式查 ``is_encrypted`` 并区分开: 一个要去解密,
   一个要去 OCR, 建议完全不同。
"""

from __future__ import annotations

import io
import logging
from typing import ClassVar

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import (
    EmptyDocumentError,
    EncryptedDocumentError,
    ParseError,
)
from shovel.pipeline.parsers.base import (
    Block,
    ParsedDocument,
    ParseHint,
    ParserSpec,
    clean_text,
    collapse_blank_lines,
    take,
)

#: 一页之内再按空行分段的下限。低于这个长度就整页作为一个块 ——
#: 把一页目录切成 30 个三字碎片, 对检索只有负作用。
_SPLIT_PAGE_ABOVE_CHARS = 1200

#: 空密码试解。绝大多数"加密"的 PDF 只设了权限密码 (禁止打印/复制),
#: 打开密码是空的 —— 这类文档 pypdf 用空密码就能解开, 用户根本不觉得
#: 它加过密。不试一下就报错, 会误伤一大批本可正常索引的文档。
_EMPTY_PASSWORD = ""


class PdfParser:

    spec: ClassVar[ParserSpec] = ParserSpec(
        name="pdf",
        version="1",
        doc_classes=frozenset({DocClass.PDF_DOC}),
        suffixes=frozenset({".pdf"}),
    )

    _quieted: ClassVar[bool] = False

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        _quiet_pypdf()

        try:
            reader = PdfReader(io.BytesIO(payload), strict=False)
        except PdfReadError as exc:
            raise ParseError(hint.uri, f"PDF 结构损坏: {exc}") from exc

        if reader.is_encrypted and not _try_decrypt(reader):
            raise EncryptedDocumentError(hint.uri)

        blocks: list[Block] = []
        empty_pages = 0

        for number, page in enumerate(reader.pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception as exc:
                # 单页抽取失败不该让整份文档失败: 一份 300 页的文档里
                # 有一页嵌了畸形字体, 其余 299 页仍然是有价值的。
                blocks.append(_note_block(number, f"本页文本抽取失败: {exc}"))
                continue

            body = collapse_blank_lines(clean_text(raw))

            if not body:
                empty_pages += 1
                continue

            blocks.extend(_page_blocks(number, body))

        if not blocks:
            raise EmptyDocumentError(hint.uri)

        kept, truncated = take(tuple(blocks), hint.max_chars)
        page_count = len(reader.pages)

        return ParsedDocument(
            blocks=kept,
            title=_title_of(reader, hint),
            doc_class=DocClass.PDF_DOC,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
            meta={
                "page_count": page_count,
                # 空白页占比是判断"这份是不是扫描件"的唯一线索。
                # 部分为空 (例如只有封面是图) 不该失败, 但值得留证。
                "empty_pages": empty_pages,
            },
        )


def _try_decrypt(reader: object) -> bool:
    try:
        # pypdf 返回的是枚举, 0 == 失败
        return bool(reader.decrypt(_EMPTY_PASSWORD))  # type: ignore[attr-defined]
    except Exception:
        return False


def _note_block(page: int, message: str) -> Block:
    return Block(
        text=message,
        kind=BlockKind.NOTE,
        locator=f"p.{page}",
    )


def _page_blocks(page: int, body: str) -> list[Block]:
    """一页 -> 一个或多个块。

    短页整页一块; 长页按空行分段, 让切块阶段拿到真实的段落边界而不是
    "每 800 字切一刀"。
    """

    locator = f"p.{page}"

    if len(body) <= _SPLIT_PAGE_ABOVE_CHARS:
        return [Block(text=body, kind=BlockKind.PARAGRAPH, locator=locator)]

    return [
        Block(text=segment, kind=BlockKind.PARAGRAPH, locator=locator)
        for segment in (s.strip() for s in body.split("\n\n"))
        if segment
    ]


def _title_of(reader: object, hint: ParseHint) -> str:
    """优先用 PDF 元数据里的标题。

    但要防一手: 大量 PDF 的 ``/Title`` 是生成工具留下的垃圾
    (``Microsoft Word - 未命名.doc``、临时文件名)。只有当它看起来
    像个标题时才采用, 否则回落文件名 —— 文件名至少是用户自己起的。
    """

    try:
        meta = reader.metadata  # type: ignore[attr-defined]
        title = (meta.title or "").strip() if meta else ""
    except Exception:
        title = ""

    if not title or len(title) > 200 or title.lower().endswith((".doc", ".tmp")):
        return hint.filename

    return title


def _quiet_pypdf() -> None:
    """压掉 pypdf 自己往 root logger 上喷的警告。

    它对每一个不规范的 PDF 都会打 "invalid pdf header" 之类的日志, 而
    用户目录里的 PDF 有相当比例是不规范的。这些警告会盖住 CLI 的进度
    输出, 而且它们是重复的 —— 真正需要用户知道的失败, 我们已经通过
    ``ParseError`` 变成了一条 job_event。
    """

    if PdfParser._quieted:
        return

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    PdfParser._quieted = True


PARSER = PdfParser()

__all__ = ["PARSER", "PdfParser"]
