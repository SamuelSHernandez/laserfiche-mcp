"""Issue #27 end-to-end at the MCP tool layer.

``get_document_text`` and ``get_document_edoc`` on a v2 server whose Export
endpoint answers with a JSON download pointer (scanned entry, no edoc). The
tools must hand the model the *extracted text*, never the pointer JSON.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import _app, server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.tools._helpers import wrap_untrusted_document_text
from tests.conftest import SAMPLE_PDF_BYTES, SAMPLE_PDF_TEXT, _StubAuth

_API = "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo"
_EXPORT = f"{_API}/Entries/42/Export"
_DOWNLOAD = f"{_API}/Download/8f0c1d"
_POINTER = {
    "@odata.context": "https://lf.example.test/LFRepositoryAPI/v2/$metadata#String",
    "value": _DOWNLOAD,
}
_SCAN_TEXT = "SCANNED PAGE ONE\nInvoice 1042 total 99.00"


@pytest.fixture
async def v2_client(monkeypatch: pytest.MonkeyPatch, lf_env: dict[str, str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LF_API_VERSION", "v2")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]
    async with LaserficheClient(settings, _StubAuth()) as client:
        monkeypatch.setattr(_app, "get_client", lambda: client)
        yield client


def _pointer_response(httpx_mock: HTTPXMock, *, times: int = 1) -> None:
    for _ in range(times):
        httpx_mock.add_response(
            method="POST",
            url=_EXPORT,
            json=_POINTER,
            headers={"content-type": "application/json;odata.metadata=minimal"},
        )


@pytest.mark.asyncio
async def test_get_document_text_returns_extracted_text_not_pointer_json(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """The literal repro from the issue."""
    _pointer_response(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=_DOWNLOAD,
        content=_SCAN_TEXT.encode(),
        headers={"content-type": "text/plain; charset=utf-8"},
    )

    result = await server.get_document_text(entry_id=42)

    assert "error" not in result, result
    assert result["text"] == wrap_untrusted_document_text(_SCAN_TEXT)
    assert result["char_count"] == len(_SCAN_TEXT)
    assert "@odata.context" not in result["text"]
    assert "/Download/" not in result["text"]


@pytest.mark.asyncio
async def test_get_document_text_truncation_applies_to_the_followed_text(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _pointer_response(httpx_mock)
    httpx_mock.add_response(
        method="GET", url=_DOWNLOAD, content=b"x" * 500, headers={"content-type": "text/plain"}
    )

    result = await server.get_document_text(entry_id=42, max_chars=100)

    assert result["truncated"] is True
    assert result["char_count"] == 100


@pytest.mark.asyncio
async def test_get_document_text_download_404_is_a_structured_not_found(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _pointer_response(httpx_mock)
    httpx_mock.add_response(method="GET", url=_DOWNLOAD, status_code=404, content=b"gone")

    result = await server.get_document_text(entry_id=42)

    assert result["mode"] == "error"
    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_get_document_text_unchanged_when_export_returns_text_directly(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=b"hello world", headers={"content-type": "text/plain"}
    )

    result = await server.get_document_text(entry_id=42)

    assert result["text"] == wrap_untrusted_document_text("hello world")
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_document_edoc_text_mode_extracts_pdf_behind_a_pointer(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """mode='text' makes two Export calls (size probe + download); each is followed."""
    _pointer_response(httpx_mock, times=2)
    for _ in range(2):
        httpx_mock.add_response(
            method="GET",
            url=_DOWNLOAD,
            content=SAMPLE_PDF_BYTES,
            headers={"content-type": "application/pdf"},
        )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert "error" not in result, result
    assert result["mode"] == "text"
    assert SAMPLE_PDF_TEXT in result["text"]


@pytest.mark.asyncio
async def test_get_document_edoc_info_reports_the_real_file_not_the_pointer(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _pointer_response(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=_DOWNLOAD,
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["byte_size"] == len(SAMPLE_PDF_BYTES)
    assert result["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_get_document_edoc_size_cap_is_measured_on_the_real_file(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """Without following, the probe sees a ~150-byte pointer and the cap is bypassed."""
    _pointer_response(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=_DOWNLOAD,
        content=b"p" * 5000,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="bytes", max_bytes=1000)

    assert result["mode"] == "error"
    assert result["error"] == "size_exceeds_cap"
    assert result["byte_size"] == 5000


@pytest.mark.asyncio
async def test_json_edoc_that_merely_resembles_a_pointer_is_not_followed(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    raw = json.dumps({"value": "https://elsewhere.example/doc", "title": "real json file"})
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        content=raw.encode(),
        headers={"content-type": "application/json"},
        is_reusable=True,
    )

    result = await server.get_document_edoc(entry_id=42, mode="bytes")

    assert "error" not in result, result
    assert all(r.method == "POST" for r in httpx_mock.get_requests())


# --- scanned entries with no electronic file --------------------------------
# Observed on a real v1 server: the edoc endpoint answers 200 with an empty
# body and no content-type. That used to surface as byte_size=0 "success" /
# a misleading ``unsupported_format``.

_V1_EDOC = f"{_API.replace('/v2/', '/v1/')}/Entries/42/Laserfiche.Repository.Document/edoc"


@pytest.fixture
async def v1_client(monkeypatch: pytest.MonkeyPatch, lf_env: dict[str, str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LF_API_VERSION", "v1")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]
    async with LaserficheClient(settings, _StubAuth()) as client:
        monkeypatch.setattr(_app, "get_client", lambda: client)
        yield client


def _empty_edoc(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=_V1_EDOC, content=b"", is_reusable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "bytes"])
async def test_empty_edoc_is_a_structured_no_electronic_document_error(
    httpx_mock: HTTPXMock, v1_client: LaserficheClient, mode: str
) -> None:
    _empty_edoc(httpx_mock)

    result = await server.get_document_edoc(entry_id=42, mode=mode)  # type: ignore[arg-type]

    assert result["mode"] == "error"
    assert result["error"] == "no_electronic_document"
    assert result["kind"] == "not_found"
    assert result["requested_mode"] == mode
    assert "search_content" in result["hint"]
    assert "data_base64" not in result


@pytest.mark.asyncio
async def test_empty_edoc_info_flags_missing_document(
    httpx_mock: HTTPXMock, v1_client: LaserficheClient
) -> None:
    _empty_edoc(httpx_mock)

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["mode"] == "info"
    assert result["byte_size"] == 0
    assert result["has_electronic_document"] is False
    assert "warning" in result


@pytest.mark.asyncio
async def test_nonempty_edoc_info_has_no_missing_document_flag(
    httpx_mock: HTTPXMock, v1_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=_V1_EDOC,
        content=b"%PDF-1.4 x",
        headers={"content-type": "application/pdf"},
        is_reusable=True,
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert "has_electronic_document" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"   \n\t "])
async def test_get_document_text_empty_text_is_a_structured_error(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient, body: bytes
) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=body, headers={"content-type": "text/plain"}
    )

    result = await server.get_document_text(entry_id=42)

    assert result["mode"] == "error"
    assert result["operation"] == "get_document_text"
    assert result["error"] == "no_extracted_text"
    assert result["kind"] == "not_found"
    assert "search_content" in result["hint"]


@pytest.mark.asyncio
async def test_get_document_text_pointer_to_empty_download_is_the_same_error(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _pointer_response(httpx_mock)
    httpx_mock.add_response(
        method="GET", url=_DOWNLOAD, content=b"", headers={"content-type": "text/plain"}
    )

    result = await server.get_document_text(entry_id=42)

    assert result["error"] == "no_extracted_text"


@pytest.mark.asyncio
async def test_get_document_text_redirect_is_a_structured_error_not_empty_text(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, status_code=302, headers={"location": "https://sso.invalid/"}
    )

    result = await server.get_document_text(entry_id=42)

    assert result["mode"] == "error"
    assert "redirect" in json.dumps(result).lower()
