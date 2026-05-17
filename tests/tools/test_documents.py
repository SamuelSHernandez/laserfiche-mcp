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
from tests.conftest import (
    _BASE,
    SAMPLE_ENCRYPTED_PDF_BYTES,
    SAMPLE_PDF_BYTES,
    SAMPLE_PDF_TEXT,
    _StubAuth,
)

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

    assert result["text"] == "hello world"
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

    assert result["text"].startswith("x" * 50)
    assert len(result["text"]) == 50
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


@pytest.mark.asyncio
async def test_edoc_text_mode_rejects_non_pdf_non_text_content(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A docx/image edoc → structured error pointing at mode='bytes'."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"PK\x03\x04 fake docx",
        headers={
            "content-type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        },
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "unsupported_content_type"
    assert "wordprocessingml" in (result["content_type"] or "")
    assert "mode='bytes'" in result["message"]


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
    assert result["text"] == "hello world"


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
