"""Tests for ``tools/documents.py`` — text/edoc retrieval and PDF extraction.

Covers ``get_document_text`` (v2-only Export endpoint) and the three
modes of ``get_document_edoc`` (``info``, ``bytes``, ``text``) including
content-type branching (PDF, text/*, unsupported), encryption errors,
malformed input, oversized-download refusal, and the pypdf-unavailable
fallback.
"""

from __future__ import annotations

import base64

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.tools._helpers import (
    UNTRUSTED_DOCUMENT_TEXT_NOTICE,
    wrap_untrusted_document_text,
)
from laserfiche_mcp.tools.documents import parse_page_spec
from tests.conftest import (
    _BASE,
    SAMPLE_ENCRYPTED_PDF_BYTES,
    SAMPLE_PDF_BYTES,
    SAMPLE_PDF_TEXT,
    _StubAuth,
)


def _unwrap(wrapped: str) -> str:
    """Strip the <laserfiche_document_text> untrusted-content wrapper,
    returning the raw extracted text, for tests that need to assert on
    length/prefix/concatenation of the actual extracted content."""
    prefix = "<laserfiche_document_text>\n" + UNTRUSTED_DOCUMENT_TEXT_NOTICE + "\n\n"
    suffix = "\n</laserfiche_document_text>"
    assert wrapped.startswith(prefix), wrapped[:200]
    assert wrapped.endswith(suffix), wrapped[-200:]
    return wrapped[len(prefix) : -len(suffix)]


# --- get_document_text (v2-only) --------------------------------------------


@pytest.mark.asyncio
async def test_get_document_text_on_v2_returns_decoded_text(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_document_text only works on v2; verify the decode + truncation path.

    Builds its own client because v1 (the default test config) raises
    LaserficheError at the client level before the tool body runs.
    """
    monkeypatch.setenv("LF_API_VERSION", "v2")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]

    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42/Export",
        content=b"hello world",
    )

    async with LaserficheClient(settings, _StubAuth()) as client:
        from laserfiche_mcp import _app

        monkeypatch.setattr(_app, "get_client", lambda: client)
        result = await server.get_document_text(entry_id=42)

    assert result["text"] == wrap_untrusted_document_text("hello world")
    assert result["truncated"] is False
    assert result["entry_id"] == 42


@pytest.mark.asyncio
async def test_get_document_text_truncates_long_output(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]

    long_text = ("x" * 200).encode("utf-8")
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42/Export",
        content=long_text,
    )

    async with LaserficheClient(settings, _StubAuth()) as client:
        from laserfiche_mcp import _app

        monkeypatch.setattr(_app, "get_client", lambda: client)
        result = await server.get_document_text(entry_id=42, max_chars=50)

    raw = _unwrap(result["text"])
    assert raw.startswith("x" * 50)
    assert len(raw) == 50
    assert result["truncated"] is True
    assert result["char_count"] == 50


@pytest.mark.asyncio
async def test_get_document_text_wraps_laserfiche_error_as_runtime(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """On v1 the client raises LaserficheError synthetically — the tool must wrap it."""
    result = await server.get_document_text(entry_id=42)
    assert result["mode"] == "error"
    assert result["operation"] == "get_document_text"


# --- get_document_edoc: error wrap + mode='info' -----------------------------


@pytest.mark.asyncio
async def test_get_document_edoc_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999/Laserfiche.Repository.Document/edoc",
        status_code=403,
    )

    result = await server.get_document_edoc(entry_id=999, mode="info")
    assert result["mode"] == "error"
    assert result["operation"] == "get_document_edoc"
    assert result["error"] == "auth_failed"
    assert result["entry_id"] == 999


@pytest.mark.asyncio
async def test_edoc_info_mode_returns_size_and_content_type(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='info' — current shape preserved: byte_size + content_type + hint, no bytes."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"%PDF-1.4 hello",
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["entry_id"] == 42
    assert result["mode"] == "info"
    assert result["byte_size"] == len(b"%PDF-1.4 hello")
    assert result["content_type"] == "application/pdf"
    assert "data_base64" not in result


# --- get_document_edoc: mode='bytes' ----------------------------------------


@pytest.mark.asyncio
async def test_edoc_bytes_mode_returns_base64_starting_with_pdf_magic(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='bytes' — base64 round-trip yields the original PDF magic bytes."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="bytes")

    assert result["mode"] == "bytes"
    assert result["content_type"] == "application/pdf"
    assert result["byte_size"] == len(SAMPLE_PDF_BYTES)
    decoded = base64.b64decode(result["data_base64"])
    assert decoded.startswith(b"%PDF-")
    assert decoded == SAMPLE_PDF_BYTES


@pytest.mark.asyncio
async def test_edoc_bytes_mode_refuses_oversized_download(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If byte_size > max_bytes, return a structured error — no base64 payload."""
    content = b"a" * 5_000
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=content,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(
        entry_id=42,
        mode="bytes",
        max_bytes=1_000,
    )

    assert result["error"] == "size_exceeds_cap"
    assert result["byte_size"] == 5_000
    assert result["max_bytes"] == 1_000
    assert "data_base64" not in result


# --- get_document_edoc: mode='text' -----------------------------------------


@pytest.mark.asyncio
async def test_edoc_text_mode_extracts_known_text_from_pdf_fixture(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='text' on a real PDF must return the fixture's known-good text.

    Earlier versions used a blank PDF and only asserted that keys existed —
    pypdf could silently regress to extracting nothing and the test would
    still pass. The fixture written by ``tests/fixtures/_generate.py``
    carries deterministic ASCII text so a regression breaks the assertion.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result, result
    assert result["pages_total"] == 1
    assert result["pages_extracted"] == 1
    assert SAMPLE_PDF_TEXT in result["text"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_edoc_text_mode_truncates_when_over_char_limit(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """text_char_limit applies — sets ``truncated=True`` when text overflows."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(
        entry_id=42,
        mode="text",
        text_char_limit=5,
    )

    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_encrypted_pdf(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """An encrypted PDF surfaces the ``pdf_encrypted`` structured error."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_ENCRYPTED_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pdf_encrypted"
    assert "mode='bytes'" in result["message"]


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_malformed_pdf(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A byte string that claims to be PDF but isn't returns pdf_open_failed."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"%PDF-1.4\nnot really a pdf",
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pdf_open_failed"
    assert result.get("exception_class")  # whatever pypdf raised


@pytest.mark.asyncio
async def test_edoc_text_mode_normalizes_content_type_casing(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Content-type matching must be case-insensitive and ignore parameters.

    Servers commonly send ``Application/PDF`` or
    ``application/pdf; charset=binary``. The branch picker must accept both.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "Application/PDF; charset=binary"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result, result
    assert SAMPLE_PDF_TEXT in result["text"]


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """Build a minimal real .docx in memory — a docx IS a zip of XML."""
    import io as _io
    import zipfile

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
async def test_edoc_text_mode_extracts_docx_via_ops(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A .docx edoc now extracts through ops/extract instead of dead-ending."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=_make_docx_bytes(["Offer letter", "Start date: March 1"]),
        headers={"content-type": _DOCX_CT},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "offer.docx", "entryType": "Document"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result.get("error") is None
    assert "Start date: March 1" in result["text"]
    assert result["backend"] == "docx"


@pytest.mark.asyncio
async def test_edoc_text_mode_survives_windows_hostile_entry_name(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Laserfiche allows ':' and '?' in entry names; the extraction scratch
    file on Windows does not. Previously this raised OSError out of the tool."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=_make_docx_bytes(["Body text"]),
        headers={"content-type": _DOCX_CT},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Offer: final?.docx", "entryType": "Document"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result.get("error") is None
    assert "Body text" in result["text"]
    assert result["backend"] == "docx"


@pytest.mark.asyncio
async def test_edoc_text_mode_corrupt_docx_returns_extraction_slug(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"PK\x03\x04 not really a zip",
        headers={"content-type": _DOCX_CT},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "broken.docx", "entryType": "Document"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "not_a_zip"


@pytest.mark.asyncio
async def test_edoc_text_mode_image_points_at_search_content(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Scans have no text layer; the error must route to the OCR index."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"II*\x00",
        headers={"content-type": "image/tiff"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "scan.tiff", "entryType": "Document"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "unsupported_format"
    assert "search_content" in result["hint"]


@pytest.mark.asyncio
async def test_edoc_text_mode_octet_stream_pdf_detected_by_name(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Self-hosted servers label most edocs octet-stream; the entry name
    must carry the format decision. Previously this dead-ended."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/octet-stream"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "contract.pdf", "entryType": "Document"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result.get("error") is None
    assert SAMPLE_PDF_TEXT in result["text"]
    assert result["backend"] == "pypdf"


@pytest.mark.asyncio
async def test_edoc_text_mode_decodes_plain_text(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """text/* content-types should be decoded directly, not run through pypdf."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"hello world",
        headers={"content-type": "text/plain; charset=utf-8"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result
    assert result["text"] == wrap_untrusted_document_text("hello world")


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_when_pypdf_is_unavailable(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pypdf isn't installed, mode='text' on a PDF returns a structured error.

    The branch exists because pypdf is a hard dep today but was optional in
    earlier drafts; the safety net stays so downstream forks can omit it.
    """
    import builtins

    real_import = builtins.__import__

    def _refuse_pypdf(name: str, *args: object, **kwargs: object) -> object:
        if name == "pypdf":
            raise ImportError("pypdf intentionally unavailable for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse_pypdf)

    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pypdf_unavailable"
    assert "pip install pypdf" in result["message"]


# --- page selection + char_offset (token-reduction paths) --------------------


def _multipage_pdf(page_count: int) -> bytes:
    """Build an N-page PDF by repeating the committed single-page fixture.

    Built at test time rather than committed as a second binary fixture —
    pypdf is already a hard runtime dependency, so this costs nothing.
    """
    import io

    import pypdf

    writer = pypdf.PdfWriter()
    source = pypdf.PdfReader(io.BytesIO(SAMPLE_PDF_BYTES))
    for _ in range(page_count):
        writer.add_page(source.pages[0])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _mock_pdf_edoc(httpx_mock: HTTPXMock, content: bytes, entry_id: int = 42) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/{entry_id}/Laserfiche.Repository.Document/edoc",
        content=content,
        headers={"content-type": "application/pdf"},
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("3", [2]),
        ("4-9", [3, 4, 5, 6, 7, 8]),
        ("1,3,5-7", [0, 2, 4, 5, 6]),
        ("5-5", [4]),
        # Duplicates collapse and order is normalized.
        ("3,1,3", [0, 2]),
        ("2-4,3-5", [1, 2, 3, 4]),
    ],
)
def test_parse_page_spec_accepts_valid_forms(spec: str | None, expected: list[int] | None) -> None:
    pages, error = parse_page_spec(spec)
    assert error is None
    assert pages == expected


@pytest.mark.parametrize(
    "spec",
    ["0", "abc", "4-", "-9", "9-4", "1,,x", "1.5", "0-3"],
)
def test_parse_page_spec_rejects_malformed_specs(spec: str) -> None:
    """A bad spec must be an error, never a silent full-document read."""
    pages, error = parse_page_spec(spec)
    assert pages is None
    assert error is not None


@pytest.mark.asyncio
async def test_edoc_text_pages_extracts_only_selected_pages(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(6))

    result = await server.get_document_edoc(entry_id=42, mode="text", pages="2-3")

    assert result["pages_total"] == 6
    assert result["pages_extracted"] == 2
    assert result["pages_selected"] == [2, 3]
    # Two pages of the fixture text, not all six.
    assert result["text"].count(SAMPLE_PDF_TEXT) == 2


@pytest.mark.asyncio
async def test_edoc_text_without_pages_reads_every_page(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(4))

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["pages_extracted"] == 4
    assert "pages_selected" not in result


@pytest.mark.asyncio
async def test_edoc_text_reports_pages_past_end_of_document(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(3))

    result = await server.get_document_edoc(entry_id=42, mode="text", pages="2,99")

    assert result["pages_selected"] == [2]
    assert result["pages_out_of_range"] == [99]


@pytest.mark.asyncio
async def test_edoc_text_errors_when_no_requested_page_exists(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(2))

    result = await server.get_document_edoc(entry_id=42, mode="text", pages="50-60")

    assert result["error"] == "pages_out_of_range"
    assert "2 page(s)" in result["message"]


@pytest.mark.asyncio
async def test_edoc_text_rejects_malformed_page_spec_before_downloading(
    patched_client: LaserficheClient,
) -> None:
    """No httpx_mock response is registered — the guard must fire first."""
    result = await server.get_document_edoc(entry_id=42, mode="text", pages="9-4")

    assert result["error"] == "invalid_page_spec"


@pytest.mark.asyncio
async def test_edoc_text_char_offset_pages_through_a_long_document(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """next_char_offset must hand back a cursor that resumes without gaps."""
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(4))
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(4))

    first = await server.get_document_edoc(entry_id=42, mode="text", text_char_limit=20)

    assert first["truncated"] is True
    assert first["char_offset"] == 0
    assert first["next_char_offset"] == 20
    first_raw = _unwrap(first["text"])
    assert len(first_raw) == 20

    second = await server.get_document_edoc(
        entry_id=42,
        mode="text",
        text_char_limit=20,
        char_offset=first["next_char_offset"],
    )

    assert second["char_offset"] == 20
    # The two windows are contiguous, so concatenating them reproduces a
    # prefix of the whole extraction.
    assert first["chars_available"] == second["chars_available"]
    second_raw = _unwrap(second["text"])
    assert (first_raw + second_raw)[:20] == first_raw


@pytest.mark.asyncio
async def test_edoc_text_final_window_reports_no_next_offset(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_pdf_edoc(httpx_mock, _multipage_pdf(1))

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["truncated"] is False
    assert result["next_char_offset"] is None


@pytest.mark.asyncio
async def test_edoc_text_pages_is_rejected_for_non_paginated_entries(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"plain text body",
        headers={"content-type": "text/plain"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text", pages="1-2")

    assert result["error"] == "pages_not_applicable"


@pytest.mark.asyncio
async def test_edoc_text_char_offset_applies_to_plain_text_entries(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"abcdefghij",
        headers={"content-type": "text/plain"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text", char_offset=4)

    assert result["text"] == wrap_untrusted_document_text("efghij")
    assert result["chars_available"] == 10
    assert result["next_char_offset"] is None


# --- mode='info' is a header probe, not a download ---------------------------


@pytest.mark.asyncio
async def test_edoc_info_mode_reports_null_size_without_content_length(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A chunked response has no Content-Length; say so instead of guessing.

    The old implementation buffered the whole body and reported len(bytes),
    which meant probing a 400 MB edoc cost a 400 MB transfer.
    """
    from pytest_httpx import IteratorStream

    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        stream=IteratorStream([b"%PDF-1.4 ", b"chunk two"]),
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["mode"] == "info"
    assert result["byte_size"] is None
    assert result["content_type"] == "application/pdf"
    assert "Content-Length" in result["hint"]


@pytest.mark.asyncio
async def test_edoc_info_mode_surfaces_http_errors_structurally(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """The streaming probe still has to produce the structured error contract."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/7/Laserfiche.Repository.Document/edoc",
        status_code=404,
        json={"title": "Entry not found"},
    )

    result = await server.get_document_edoc(entry_id=7, mode="info")

    assert result["mode"] == "error"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 7
