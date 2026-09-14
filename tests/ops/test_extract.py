"""Tests for ``ops/extract.py``.

OOXML fixtures are synthesized in-test with ``zipfile`` rather than checked
in as binaries: a .docx *is* a zip of XML, so building one here documents
exactly which parts the extractor depends on, and a reviewer can see the
input without opening Word.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from laserfiche_mcp.ops import extract
from tests.conftest import SAMPLE_ENCRYPTED_PDF_BYTES, SAMPLE_PDF_BYTES, SAMPLE_PDF_TEXT

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _make_docx(path: Path, paragraphs: list[str]) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    return path


def _make_pptx(path: Path, slides: list[list[str]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index, runs in enumerate(slides, start=1):
            text = "".join(f"<a:t>{run}</a:t>" for run in runs)
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                f'<?xml version="1.0"?><p:sld xmlns:p="{_P}" xmlns:a="{_A}">{text}</p:sld>',
            )
    return path


# --- detect_kind ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("application/pdf", None, "pdf"),
        ("text/plain; charset=utf-8", None, "text"),
        (None, "report.docx", "docx"),
        (None, "deck.PPTX", "pptx"),
        (None, "book.xlsx", "xlsx"),
        (None, "note.eml", "eml"),
        (None, "legacy.doc", "legacy_office"),
        (None, "scan.tiff", "unknown"),
        # The common self-hosted case: a useless content-type, so the
        # extension has to carry the decision.
        ("application/octet-stream", "contract.pdf", "pdf"),
    ],
)
def test_detect_kind(content_type: str | None, filename: str | None, expected: str) -> None:
    assert extract.detect_kind(content_type, filename) == expected


def test_content_type_wins_over_a_misleading_extension() -> None:
    assert extract.detect_kind("application/pdf", "thing.txt") == "pdf"


# --- pdf --------------------------------------------------------------------


def test_extract_pdf_returns_per_page_text(tmp_path: Path) -> None:
    target = tmp_path / "sample.pdf"
    target.write_bytes(SAMPLE_PDF_BYTES)

    result = extract.extract(target)

    assert SAMPLE_PDF_TEXT in result.text
    assert result.backend == "pypdf"
    assert result.pages is not None
    assert result.page_count == len(result.pages)


def test_extract_encrypted_pdf_raises_with_a_slug(tmp_path: Path) -> None:
    target = tmp_path / "locked.pdf"
    target.write_bytes(SAMPLE_ENCRYPTED_PDF_BYTES)

    with pytest.raises(extract.ExtractionError) as caught:
        extract.extract(target)

    assert caught.value.slug == "pdf_encrypted"


def test_pdf_with_no_text_layer_warns_about_ocr(tmp_path: Path, monkeypatch) -> None:
    """A scan with no text layer is the single most confusing empty result."""

    class _BlankPage:
        def extract_text(self) -> str:
            return "   "

    class _BlankReader:
        is_encrypted = False
        pages = [_BlankPage()]

        def __init__(self, *_: object, **__: object) -> None:
            pass

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", _BlankReader)
    target = tmp_path / "scan.pdf"
    target.write_bytes(SAMPLE_PDF_BYTES)

    result = extract.extract(target)

    assert any("OCR" in w for w in result.warnings)


# --- ooxml ------------------------------------------------------------------


def test_extract_docx_joins_paragraphs_as_lines(tmp_path: Path) -> None:
    target = _make_docx(tmp_path / "memo.docx", ["First line", "Second line"])

    result = extract.extract(target)

    assert result.text == "First line\nSecond line"
    assert result.backend == "docx"
    assert result.pages is None  # docx has no page concept before rendering


def test_extract_docx_rejects_a_non_zip(tmp_path: Path) -> None:
    target = tmp_path / "fake.docx"
    target.write_bytes(b"this is not a zip archive")

    with pytest.raises(extract.ExtractionError) as caught:
        extract.extract(target)

    assert caught.value.slug == "not_a_zip"


def test_extract_docx_without_document_part_is_malformed(tmp_path: Path) -> None:
    target = tmp_path / "empty.docx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("docProps/core.xml", "<x/>")

    with pytest.raises(extract.ExtractionError) as caught:
        extract.extract(target)

    assert caught.value.slug == "malformed_docx"


def test_extract_pptx_treats_each_slide_as_a_page(tmp_path: Path) -> None:
    target = _make_pptx(tmp_path / "deck.pptx", [["Title", "Subtitle"], ["Second slide"]])

    result = extract.extract(target)

    assert result.pages == ["Title\nSubtitle", "Second slide"]
    assert result.page_count == 2


def test_pptx_slides_are_ordered_numerically_not_lexically(tmp_path: Path) -> None:
    """slide10 must not sort between slide1 and slide2."""
    target = _make_pptx(tmp_path / "big.pptx", [[f"slide {i}"] for i in range(1, 12)])

    result = extract.extract(target)

    assert result.pages is not None
    assert result.pages[0] == "slide 1"
    assert result.pages[1] == "slide 2"
    assert result.pages[10] == "slide 11"


# --- text-ish formats -------------------------------------------------------


def test_extract_plain_text(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello world", encoding="utf-8")

    assert extract.extract(target).text == "hello world"


def test_strip_html_drops_script_and_style_content() -> None:
    markup = (
        "<html><head><style>p{color:red}</style></head>"
        "<body><script>alert(1)</script><p>Visible text</p></body></html>"
    )

    text = extract.strip_html(markup)

    assert "Visible text" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_extract_html_from_file(tmp_path: Path) -> None:
    target = tmp_path / "page.html"
    target.write_text("<p>One</p><p>Two</p>", encoding="utf-8")

    result = extract.extract(target)

    assert "One" in result.text
    assert "Two" in result.text


def test_extract_eml_includes_headers_and_body(tmp_path: Path) -> None:
    target = tmp_path / "mail.eml"
    target.write_text(
        "From: a@example.test\n"
        "To: b@example.test\n"
        "Subject: Renewal notice\n"
        "\n"
        "The lease renews in March.\n",
        encoding="utf-8",
    )

    result = extract.extract(target)

    assert "Subject: Renewal notice" in result.text
    assert "The lease renews in March." in result.text
    assert result.backend == "email"


def test_extract_rtf_strips_control_words(tmp_path: Path) -> None:
    target = tmp_path / "doc.rtf"
    target.write_text(
        r"{\rtf1\ansi{\fonttbl{\f0 Times;}}\f0\fs24 Hello RTF world\par}",
        encoding="latin-1",
    )

    result = extract.extract(target)

    assert "Hello RTF world" in result.text
    assert "fonttbl" not in result.text
    assert result.warnings  # best-effort is declared, not silent


# --- unsupported ------------------------------------------------------------


def test_legacy_office_names_the_conversion_path(tmp_path: Path) -> None:
    target = tmp_path / "old.doc"
    target.write_bytes(b"\xd0\xcf\x11\xe0")

    with pytest.raises(extract.ExtractionError) as caught:
        extract.extract(target)

    assert caught.value.slug == "legacy_office_format"
    assert "LibreOffice" in caught.value.message


def test_unknown_format_points_at_the_ocr_index(tmp_path: Path) -> None:
    target = tmp_path / "scan.tiff"
    target.write_bytes(b"II*\x00")

    with pytest.raises(extract.ExtractionError) as caught:
        extract.extract(target)

    assert caught.value.slug == "unsupported_format"
    assert "OCR" in caught.value.message


# --- sanitize_filename -------------------------------------------------------


def test_sanitize_replaces_windows_invalid_characters() -> None:
    assert extract.sanitize_filename('Report: Q1?"final".pdf') == "Report_ Q1__final_.pdf"


def test_sanitize_keeps_ordinary_names_untouched() -> None:
    assert extract.sanitize_filename("lease-4821 (signed).pdf") == "lease-4821 (signed).pdf"


def test_sanitize_strips_path_components() -> None:
    cleaned = extract.sanitize_filename("..\\..\\etc\\passwd")
    assert "\\" not in cleaned
    assert "/" not in extract.sanitize_filename("../../etc/passwd")


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    # Windows rejects names ending in '.' or ' '.
    assert extract.sanitize_filename("draft. ") == "draft"


def test_sanitize_prefixes_reserved_device_names() -> None:
    assert extract.sanitize_filename("CON.pdf") == "_CON.pdf"
    assert extract.sanitize_filename("com1.txt") == "_com1.txt"


def test_sanitize_falls_back_when_nothing_survives() -> None:
    assert extract.sanitize_filename("???", fallback="42.bin") == "42.bin"
    assert extract.sanitize_filename("", fallback="42.bin") == "42.bin"
