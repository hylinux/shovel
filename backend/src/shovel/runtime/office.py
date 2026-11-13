"""Bounded OOXML excerpts, never formula evaluation or external relationship I/O."""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterator
from urllib.parse import unquote
from xml.etree import ElementTree as XML

from shovel.runtime.errors import RuntimeFailure

OFFICE_EXCERPT_VERSION = "structured-v1"


def _invalid(message: str = "Invalid Office document.") -> RuntimeFailure:
    return RuntimeFailure(message, status=400)


def _tag(element: XML.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: XML.Element, name: str) -> list[XML.Element]:
    return [child for child in element if _tag(child) == name]


def _text(element: XML.Element) -> str:
    if _tag(element) in {"del", "rPh", "instrText"}:
        return ""
    if _tag(element) == "t":
        return element.text or ""
    if _tag(element) == "tab":
        return "\t"
    if _tag(element) in {"br", "cr"}:
        return "\n"
    return "".join(_text(child) for child in element)


class _Package:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        entries = archive.infolist()
        if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 20 * 1024 * 1024:
            raise RuntimeFailure("Office document expands beyond the safety limit.", status=413)
        self.names = {entry.filename for entry in entries}
        if len(self.names) != len(entries) or "[Content_Types].xml" not in self.names:
            raise _invalid()
        for entry in entries:
            if (
                entry.flag_bits & 1
                or "\\" in entry.filename
                or ":" in entry.filename
                or entry.filename.startswith("/")
                or ".." in entry.filename.split("/")
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
                or entry.file_size > 4 * 1024 * 1024
                or entry.file_size > max(1, entry.compress_size) * 100
                or entry.filename.lower().endswith(("vbaproject.bin", ".exe", ".bin"))
            ):
                raise _invalid("Unsafe Office document.")

    def xml(self, name: str) -> XML.Element:
        source = self.archive.read(name).decode("utf-8-sig")
        if "\x00" in source or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
            raise _invalid("Office entities are not permitted.")
        return XML.fromstring(source)  # noqa: S314 -- bounded, DTD/entity-free XML

    def relationships(self, part: str, kind: str) -> dict[str, str]:
        directory, filename = posixpath.split(part)
        root = self.xml(f"{directory}/_rels/{filename}.rels")
        links: dict[str, str] = {}
        for item in root:
            if not item.get("Type", "").endswith("/" + kind):
                continue
            target = unquote(item.get("Target", ""), errors="strict")
            identifier = item.get("Id", "")
            if (
                not identifier
                or identifier in links
                or not target
                or item.get("TargetMode", "Internal") != "Internal"
                or any(character in target for character in "\\:#?")
                or any(ord(character) < 32 for character in target)
            ):
                raise _invalid("Unsafe Office relationship.")
            path = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target)
            )
            prefix = "xl/worksheets/" if kind == "worksheet" else "ppt/slides/"
            if not path.startswith(prefix) or path not in self.names or not path.endswith(".xml"):
                raise _invalid("Unsafe Office relationship.")
            links[identifier] = path
        return links


def _relationship_id(element: XML.Element) -> str:
    return next((value for key, value in element.attrib.items() if key.endswith("}id")), "")


def _word_blocks(element: XML.Element) -> Iterator[str]:
    kind = _tag(element)
    if kind == "p":
        yield _text(element)
    elif kind == "tbl":
        yield "[Table]"
        for row in _children(element, "tr"):
            yield " | ".join(
                json.dumps("\n".join(_word_blocks(cell)), ensure_ascii=False) for cell in _children(row, "tc")
            )
    elif kind != "del":
        for child in element:
            yield from _word_blocks(child)


def _word(package: _Package) -> Iterator[str]:
    root = package.xml("word/document.xml")
    if any(_tag(element) in {"p", "tbl"} for element in root.iter()):
        yield from _word_blocks(root)
    else:
        # Retain support for old minimal XML fixtures and already accepted documents.
        yield from (element.text or "" for element in root.iter() if _tag(element) == "t")


def _slides(package: _Package) -> Iterator[str]:
    root = package.xml("ppt/presentation.xml")
    links = package.relationships("ppt/presentation.xml", "slide")
    for index, slide in enumerate((item for item in root.iter() if _tag(item) == "sldId"), 1):
        path = links[_relationship_id(slide)]
        yield f"[Slide {index}]"
        for paragraph in package.xml(path).iter():
            if _tag(paragraph) == "p":
                yield _text(paragraph)


def _cell_value(cell: XML.Element, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    value = next((item.text or "" for item in cell if _tag(item) == "v"), "")
    if kind == "s":
        if not re.fullmatch(r"[0-9]{1,7}", value) or int(value) >= len(shared):
            raise _invalid("Invalid spreadsheet shared-string reference.")
        value = shared[int(value)]
    elif kind == "inlineStr":
        value = "".join(_text(item) for item in cell if _tag(item) == "is")
    elif kind == "b":
        if value not in {"0", "1"}:
            raise _invalid("Invalid spreadsheet boolean.")
        return "TRUE" if value == "1" else "FALSE"
    if kind == "e":
        return f"error {json.dumps(value, ensure_ascii=False)}"
    return json.dumps(value, ensure_ascii=False)


def _sheets(package: _Package) -> Iterator[str]:
    root = package.xml("xl/workbook.xml")
    links = package.relationships("xl/workbook.xml", "worksheet")
    shared = (
        [_text(item) for item in package.xml("xl/sharedStrings.xml") if _tag(item) == "si"]
        if "xl/sharedStrings.xml" in package.names
        else []
    )
    yield (
        "[Spreadsheet: stored values only; number/date styling is not applied. "
        "Formulas are not recalculated.]"
    )
    for sheet in (item for item in root.iter() if _tag(item) == "sheet"):
        yield f"[Worksheet {json.dumps(sheet.get('name', ''), ensure_ascii=False)}]"
        worksheet = package.xml(links[_relationship_id(sheet)])
        for row in (item for item in worksheet.iter() if _tag(item) == "row"):
            for index, cell in enumerate(_children(row, "c"), 1):
                coordinate = cell.get("r")
                if coordinate is not None and not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate):
                    raise _invalid("Invalid spreadsheet cell coordinate.")
                position = coordinate or f"cell {index} (position unspecified)"
                formula = next((item for item in cell if _tag(item) == "f"), None)
                if formula is not None:
                    expression = (
                        json.dumps(formula.text, ensure_ascii=False)
                        if formula.text
                        else "[stored expression unavailable]"
                    )
                    cached = (
                        _cell_value(cell, shared)
                        if any(_tag(item) == "v" for item in cell)
                        else "[cached result unavailable]"
                    )
                    yield f"{position}: formula={expression}; cached={cached}"
                else:
                    yield f"{position}: {_cell_value(cell, shared)}"


def _legacy(package: _Package, suffix: str) -> Iterator[str]:
    prefix = {".docx": "word/", ".pptx": "ppt/slides/", ".xlsx": "xl/"}[suffix]
    for entry in package.archive.infolist():
        if entry.filename.startswith(prefix) and entry.filename.endswith(".xml"):
            for element in package.xml(entry.filename).iter():
                if _tag(element) in {"t", "v"} and element.text:
                    yield element.text


def office_text(data: bytes, suffix: str, limit: int, *, legacy: bool = False) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            package = _Package(archive)
            lines = (
                _legacy(package, suffix)
                if legacy
                else {".docx": _word, ".pptx": _slides, ".xlsx": _sheets}[suffix](package)
            )
            output: list[str] = []
            used = 0
            for line in lines:
                used += int(bool(output))
                output.append(line[: max(0, limit + 1 - used)])
                used += len(output[-1])
                if used > limit:
                    break
            return "\n".join(output)
    except (
        zipfile.BadZipFile,
        XML.ParseError,
        OSError,
        RuntimeError,
        UnicodeError,
        LookupError,
        ValueError,
        zlib.error,
        RecursionError,
    ) as exc:
        raise _invalid() from exc
