"""Extract inert HTML text, headings, links and crawler directives."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import ClassVar

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import EmptyDocumentError, ParseError
from shovel.pipeline.parsers.base import Block, HeadingStack, ParsedDocument, ParseHint, ParserSpec, take
from shovel.pipeline.parsers.plain_text import decode

_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "li", "pre", "tr", "blockquote", "hr"})
_IGNORE_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object"})
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def robots_directives(values: list[str], *, agent: str = "shovelbot") -> set[str]:
    directives: set[str] = set()
    for value in values:
        applicable = True
        for part in value.lower().split(","):
            if ":" in part:
                name, part = part.split(":", 1)
                if name.strip() in {
                    "max-snippet",
                    "max-image-preview",
                    "max-video-preview",
                    "unavailable_after",
                }:
                    continue
                applicable = name.strip() in {"*", agent}
            if applicable:
                directives.update(part.split())
    if "none" in directives:
        directives.update({"noindex", "nofollow"})
    return directives


def decode_html(payload: bytes, encoding: str | None = None) -> str:
    if encoding is None:
        match = re.search(rb"""charset\s*=\s*["']?\s*([a-zA-Z0-9_-]+)""", payload[:8192], re.I)
        encoding = match.group(1).decode("ascii") if match else None
    if encoding:
        return payload.decode(encoding)
    return decode(payload)[0]


class HTMLContent(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.links: list[str] = []
        self.base: str | None = None
        self.title_parts: list[str] = []
        self.lang: str | None = None
        self.directives: set[str] = set()
        self._stack: list[tuple[str, bool]] = []
        self._head = False
        self._title = False
        self._buffer: list[str] = []
        self._kind = BlockKind.PARAGRAPH
        self._heading = 0
        self._headings = HeadingStack()
        self._pre = False

    @property
    def title(self) -> str | None:
        return " ".join("".join(self.title_parts).split()) or None

    def flush(self) -> None:
        text = "".join(self._buffer)
        self._buffer.clear()
        text = text.strip() if self._pre else " ".join(text.split())
        if not text:
            return
        if self._heading:
            self._headings.push(self._heading, text)
        self.blocks.append(Block(text, kind=self._kind, level=self._heading, path=self._headings.current()))

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = dict(attributes)
        if tag == "html":
            self.lang = attrs.get("lang")
        if tag == "head":
            self._head = True
        if tag == "title":
            self._title = True
        if tag == "meta" and (attrs.get("name") or "").lower() in {"robots", "shovelbot"}:
            self.directives.update(robots_directives([attrs.get("content") or ""]))
        if tag == "base" and self.base is None:
            self.base = attrs.get("href")
        inherited = bool(self._stack and self._stack[-1][1])
        ignored = inherited or tag in _IGNORE_TAGS or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        if tag not in _VOID_TAGS:
            self._stack.append((tag, ignored))
        if ignored or self._head:
            return
        if tag == "a" and attrs.get("href") and "nofollow" not in (attrs.get("rel") or "").lower().split():
            self.links.append(attrs["href"] or "")
        if tag in _BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self._heading = int(tag[1]) if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit() else 0
            self._kind = (
                BlockKind.HEADING
                if self._heading
                else (
                    BlockKind.CODE
                    if tag == "pre"
                    else BlockKind.LIST_ITEM
                    if tag == "li"
                    else BlockKind.PARAGRAPH
                )
            )
            self._pre = tag == "pre"
        if tag in {"br", "td", "th"}:
            self._buffer.append("\n" if tag == "br" else " ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        ignored = bool(self._stack and self._stack[-1][1])
        if not ignored and (tag in _BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}):
            self.flush()
            self._heading = 0
            self._kind = BlockKind.PARAGRAPH
            self._pre = False
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break
        if tag == "head":
            self._head = False
        if tag == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title_parts.append(data)
        elif not self._head and not (self._stack and self._stack[-1][1]):
            self._buffer.append(data)

    @classmethod
    def read(cls, payload: bytes, encoding: str | None = None) -> HTMLContent:
        document = cls()
        document.feed(decode_html(payload, encoding))
        document.close()
        document.flush()
        return document


class HTMLDocumentParser:
    spec: ClassVar[ParserSpec] = ParserSpec(
        name="html",
        version="1",
        doc_classes=frozenset({DocClass.HTML}),
        suffixes=frozenset({".html", ".htm"}),
    )

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        try:
            content = HTMLContent.read(payload, hint.encoding)
        except (UnicodeError, LookupError) as exc:
            raise ParseError(hint.uri, "Cannot decode HTML content.") from exc
        if not content.blocks:
            raise EmptyDocumentError(hint.uri)
        blocks, truncated = take(content.blocks, hint.max_chars)
        return ParsedDocument(
            blocks=blocks,
            title=content.title or hint.filename,
            lang=content.lang,
            doc_class=DocClass.HTML,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
        )


PARSER = HTMLDocumentParser()
