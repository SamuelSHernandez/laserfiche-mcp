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
from laserfiche_mcp.errors import LaserficheError, is_no_electronic_document
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


# --- scanned entries on a real v2 server -------------------------------------
# Observed on a real v2 server (password auth): Export(part=Edoc) on a scanned
# entry is rejected with HTTP 400 / errorCode 216 rather than an empty 200.

_V2_NO_EDOC = {
    "type": "https://httpstatuses.io/400",
    "title": "This entry does not contain an electronic document",
    "status": 400,
    "errorCode": 216,
}


def _v2_no_edoc(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, status_code=400, json=_V2_NO_EDOC, is_reusable=True
    )


@pytest.mark.asyncio
async def test_v2_scanned_entry_info_flags_missing_document(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["mode"] == "info"
    assert result["byte_size"] == 0
    assert result["has_electronic_document"] is False
    assert "warning" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "bytes"])
async def test_v2_scanned_entry_is_a_structured_no_electronic_document_error(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient, mode: str
) -> None:
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_edoc(entry_id=42, mode=mode)  # type: ignore[arg-type]

    assert result["mode"] == "error"
    assert result["error"] == "no_electronic_document"
    assert result["kind"] == "not_found"
    assert result["requested_mode"] == mode
    assert "search_content" in result["hint"]
    assert "server_error_code" not in result


@pytest.mark.asyncio
async def test_v2_other_400_216_is_not_mistaken_for_a_missing_document(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        status_code=400,
        json={"title": "Bad parameter", "status": 400, "errorCode": 216},
        is_reusable=True,
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["mode"] == "error"
    assert result["error"] != "no_electronic_document"
    assert result["server_error_code"] == 216


@pytest.mark.asyncio
async def test_v2_scanned_entry_image_read_reports_no_electronic_file(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_image(entry_id=42)

    assert result["mode"] == "error"
    assert result["error"] == "no_electronic_document"
    assert result["operation"] == "get_document_image"


@pytest.mark.asyncio
async def test_v2_scanned_entry_recognised_whatever_shape_the_error_body_has(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """The phrase is matched in the whole error text, not one assumed JSON field."""
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        status_code=400,
        content=b"Error 216: This entry does not contain an electronic document.",
        is_reusable=True,
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "no_electronic_document"


# --- hardening from adversarial review ---------------------------------------


def _lf_error(status: int, detail: object) -> "LaserficheError":  # noqa: UP037
    return LaserficheError(
        f"Laserfiche API error {status}: {detail}", status_code=status, detail=detail
    )


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (400, _V2_NO_EDOC, True),
        (
            400,
            {**_V2_NO_EDOC, "title": "THIS ENTRY  does not\ncontain an electronic document"},
            True,
        ),
        (400, {"error": {"message": "This entry does not contain an electronic document"}}, True),
        (400, "This entry does not contain an electronic document.", True),
        (400, {**_V2_NO_EDOC, "errorCode": 9999}, False),  # a different server code
        (400, {"title": "Bad parameter", "errorCode": 216}, False),
        # the phrase only inside a non-message field (an echoed path) is not a match
        (
            400,
            {
                "title": "Invalid part",
                "errorCode": 216,
                "instance": "/x/does not contain an electronic document.pdf",
            },
            False,
        ),
        # a folder 400 with similar wording is a different failure
        (
            400,
            {
                "title": "Entry 42 is a folder and does not contain an electronic document",
                "errorCode": 216,
            },
            False,
        ),
        (404, _V2_NO_EDOC, False),
        (500, _V2_NO_EDOC, False),
        (None, _V2_NO_EDOC, False),
    ],
)
def test_is_no_electronic_document_predicate(
    status: int | None, detail: object, expected: bool
) -> None:
    assert is_no_electronic_document(_lf_error(status, detail)) is expected  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_v2_no_edoc_info_hint_does_not_point_at_a_failing_call(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert "mode='text'" not in result["hint"]
    assert "search_content" in result["hint"]
    assert "empty file" not in result["warning"]


@pytest.mark.asyncio
async def test_v2_image_error_does_not_tell_a_v2_user_to_switch_to_v2(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_image(entry_id=42)

    assert "LF_API_VERSION=v2" not in result["hint"]
    assert "only return page images on v2" not in result["message"]
    assert "get_document_text" in result["hint"]


@pytest.mark.asyncio
async def test_v2_no_edoc_rejected_at_download_time_is_still_classified(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """The size probe succeeds, then the real export is rejected."""
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=b"x" * 10, headers={"content-type": "text/plain"}
    )
    httpx_mock.add_response(method="POST", url=_EXPORT, status_code=400, json=_V2_NO_EDOC)

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "no_electronic_document"


@pytest.mark.asyncio
async def test_v2_image_no_edoc_rejected_at_download_time_is_still_classified(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=b"x" * 10, headers={"content-type": "image/png"}
    )
    httpx_mock.add_response(method="POST", url=_EXPORT, status_code=400, json=_V2_NO_EDOC)

    result = await server.get_document_image(entry_id=42)

    assert result["error"] == "no_electronic_document"


@pytest.mark.asyncio
async def test_v2_no_edoc_rejection_on_the_followed_download_is_classified(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    _pointer_response(httpx_mock)
    httpx_mock.add_response(method="GET", url=_DOWNLOAD, status_code=400, json=_V2_NO_EDOC)

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["has_electronic_document"] is False


@pytest.mark.asyncio
async def test_v2_get_document_text_no_edoc_rejection_is_no_extracted_text(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    """The same 400 / 216 on a Text export is the contract's no_extracted_text,
    not the raw server error."""
    _v2_no_edoc(httpx_mock)

    result = await server.get_document_text(entry_id=42)

    assert result["mode"] == "error"
    assert result["operation"] == "get_document_text"
    assert result["error"] == "no_extracted_text"
    assert result["kind"] == "not_found"
    assert result["entry_id"] == 42
    assert "search_content" in result["hint"]
    assert "216" not in json.dumps(result)


@pytest.mark.asyncio
async def test_v2_get_document_text_unrelated_400_is_not_reworded(
    httpx_mock: HTTPXMock, v2_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        status_code=400,
        json={"title": "Invalid parameter", "status": 400, "errorCode": 216},
    )

    result = await server.get_document_text(entry_id=42)

    assert result["mode"] == "error"
    assert result["error"] != "no_extracted_text"
