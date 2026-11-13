from __future__ import annotations

import io
import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from shovel.runtime import pdf
from shovel.runtime.errors import RuntimeFailure


def pdf_file(
    text: str = "Safe PDF text",
    *,
    pages: int = 1,
    encrypted: bool = False,
    action: bool = False,
    embedded: bool = False,
    compressed: bool = False,
) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        if text:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(
                stream.flate_encode() if compressed else stream
            )
    if encrypted:
        writer.encrypt("not-a-real-password")
    if action:
        writer.add_js("app.alert('untrusted fixture')")
    if embedded:
        writer.add_attachment("untrusted.txt", b"do not extract")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_isolated_pdf_text_has_page_boundaries_and_bounded_truncation() -> None:
    excerpt = pdf.pdf_excerpt(pdf_file(pages=2))
    assert excerpt.pages == excerpt.read_pages == 2
    assert excerpt.text == "[Page 1]\nSafe PDF text\n[Page 2]\nSafe PDF text"
    truncated = pdf.pdf_excerpt(pdf_file("a" * 9000, pages=2))
    assert len(truncated.text) == 8001
    assert truncated.pages == 2 and truncated.read_pages == 1


@pytest.mark.parametrize(
    "options,fragment",
    [
        ({"encrypted": True}, "Encrypted"),
        ({"text": ""}, "OCR"),
        ({"pages": 51}, "50 pages"),
        ({"action": True}, "actions"),
        ({"embedded": True}, "embedded"),
        ({"text": "a" * (5 * 1024 * 1024), "compressed": True}, "resource limit"),
    ],
)
def test_unsupported_or_resource_exhausting_pdfs_fail_explicitly(
    options: dict[str, object],
    fragment: str,
) -> None:
    with pytest.raises(RuntimeFailure, match=fragment):
        pdf.pdf_excerpt(pdf_file(**options))


def test_malformed_pdf_and_timeout_do_not_return_a_successful_empty_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeFailure, match="Invalid"):
        pdf.pdf_excerpt(b"not a PDF")
    with pytest.raises(RuntimeFailure):
        pdf.pdf_excerpt(b"%PDF-1.7\nmalformed\n%%EOF")
    monkeypatch.setattr(pdf, "PDF_TIMEOUT", 0.01)
    with pytest.raises(RuntimeFailure, match="15-second"):
        pdf.pdf_excerpt(pdf_file())


def test_unavailable_os_resource_controls_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable() -> None:
        raise OSError("fixture: resource control unavailable")

    monkeypatch.setattr(pdf, "_limit_worker", unavailable)
    pdf._main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == 503
    assert "isolation is unavailable" in result["error"]
