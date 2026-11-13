from __future__ import annotations

import io
import zipfile

import pytest

from shovel.runtime.attachments import MAX_TEXT_CHARS, extracted_text, reading_scope
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.office import OFFICE_EXCERPT_VERSION, office_text


def package(parts: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for name, source in parts.items():
            archive.writestr(name, source)
    return output.getvalue()


def workbook(cells: str = '<c r="A1" t="s"><v>0</v></c>', *, target: str = "worksheets/second.xml") -> bytes:
    return package(
        {
            "xl/worksheets/first.xml": (
                '<worksheet><sheetData><row><c r="D8"><v>42</v></c></row></sheetData></worksheet>'
            ),
            "xl/worksheets/second.xml": f"<worksheet><sheetData><row>{cells}</row></sheetData></worksheet>",
            "xl/sharedStrings.xml": (
                "<sst><si><r><t>Alice </t></r><r><t>&amp; Bob</t></r>"
                "<rPh><t>ignored phonetic</t></rPh></si></sst>"
            ),
            "xl/workbook.xml": (
                '<workbook xmlns:r="urn:relationships"><sheets><sheet name="Names" r:id="two"/>'
                '<sheet name="Totals" r:id="one"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships><Relationship Id="one" Type="urn:/worksheet" Target="worksheets/first.xml"/>'
                f'<Relationship Id="two" Type="urn:/worksheet" Target="{target}"/></Relationships>'
            ),
        }
    )


def test_word_preserves_runs_paragraphs_tabs_and_table_cells_without_deleted_text() -> None:
    data = package(
        {
            "word/document.xml": """<w:document xmlns:w="urn:word"><w:body>
        <w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t><w:tab/><w:t>!</w:t></w:r>
        <w:del><w:r><w:t>deleted</w:t></w:r></w:del></w:p>
        <w:p><w:r><w:t>Next paragraph</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:p><w:r><w:t>Alice</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>7</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>""",
            "word/comments.xml": "<comments><t>not body content</t></comments>",
        }
    )
    assert extracted_text("notes.docx", data) == (
        'Hello world\t!\nNext paragraph\n[Table]\n"Name" | "Value"\n"Alice" | "7"'
    )
    scope = reading_scope("notes.docx", data, data, "text")
    assert scope["office_extraction"] == OFFICE_EXCERPT_VERSION
    assert "deleted" in extracted_text("notes.docx", data, legacy_office=True)
    assert "not body content" in extracted_text("notes.docx", data, legacy_office=True)


def test_spreadsheet_resolves_strings_and_sheet_order_and_labels_cached_formulas_without_evaluation() -> None:
    data = workbook(
        '<c r="A1" t="s"><v>0</v></c><c r="B2" t="inlineStr"><is><t>Inline</t></is></c>'
        '<c r="C3" t="b"><v>1</v></c><c r="D4" t="e"><v>#N/A</v></c>'
        '<c r="E5"><f>SUM(D8:D9)</f><v>42</v></c><c r="F6"><f>NOW()</f></c>'
        '<c r="G7"><f t="shared" si="0"/><v>9</v></c>'
    )
    text = extracted_text("values.xlsx", data)
    assert text.index('[Worksheet "Names"]') < text.index('[Worksheet "Totals"]')
    for fragment in (
        'A1: "Alice & Bob"',
        'B2: "Inline"',
        "C3: TRUE",
        'D4: error "#N/A"',
        'E5: formula="SUM(D8:D9)"; cached="42"',
        'F6: formula="NOW()"; cached=[cached result unavailable]',
        'G7: formula=[stored expression unavailable]; cached="9"',
        'D8: "42"',
        "number/date styling is not applied",
    ):
        assert fragment in text
    assert "ignored phonetic" not in text
    legacy = extracted_text("values.xlsx", data, legacy_office=True)
    assert "Alice " in legacy and "A1:" not in legacy


def test_slides_follow_presentation_relationships_not_archive_filename_order() -> None:
    data = package(
        {
            "ppt/slides/slide4.xml": "<slide><p><r><t>Second </t></r><r><t>slide</t></r></p></slide>",
            "ppt/slides/slide30.xml": (
                "<slide><p><r><t>First slide</t></r></p><p><r><t>Next paragraph</t></r></p></slide>"
            ),
            "ppt/presentation.xml": (
                '<presentation xmlns:r="urn:relationships"><sldIdLst><sldId r:id="first"/>'
                '<sldId r:id="second"/></sldIdLst></presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": (
                '<Relationships><Relationship Id="second" Type="urn:/slide" Target="slides/slide4.xml"/>'
                '<Relationship Id="first" Type="urn:/slide" Target="slides/slide30.xml"/></Relationships>'
            ),
        }
    )
    assert (
        extracted_text("slides.pptx", data)
        == "[Slide 1]\nFirst slide\nNext paragraph\n[Slide 2]\nSecond slide"
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://example.invalid/sheet.xml",
        "../../outside.xml",
        "%2e%2e/%2e%2e/outside.xml",
        "worksheets\\second.xml",
        "worksheets/second.xml?url=external",
        "worksheets/missing.xml",
    ],
)
def test_unsafe_or_missing_relationships_fail_explicitly(target: str) -> None:
    with pytest.raises(RuntimeFailure):
        extracted_text("values.xlsx", workbook(target=target))


@pytest.mark.parametrize(
    "cell",
    [
        '<c r="A1" t="s"><v>99</v></c>',
        '<c r="A1" t="s"><v>-1</v></c>',
        '<c r="A1" t="b"><v>unknown</v></c>',
        '<c r="../A1"><v>42</v></c>',
    ],
)
def test_invalid_spreadsheet_values_do_not_become_success_shaped_excerpts(cell: str) -> None:
    with pytest.raises(RuntimeFailure):
        extracted_text("values.xlsx", workbook(cell))


def test_structured_excerpt_limit_keeps_truncation_sentinel_and_entity_fence() -> None:
    data = package(
        {"word/document.xml": f"<document><p><t>{'a' * 8000}</t></p><p><t>more</t></p></document>"}
    )
    assert len(office_text(data, ".docx", MAX_TEXT_CHARS)) == MAX_TEXT_CHARS + 1
    assert reading_scope("notes.docx", data, data, "text")["truncated"] is True
    entity = package(
        {"word/document.xml": '<!DOCTYPE x [<!ENTITY x "secret">]><document><p><t>&x;</t></p></document>'}
    )
    with pytest.raises(RuntimeFailure, match="entities"):
        extracted_text("notes.docx", entity)
