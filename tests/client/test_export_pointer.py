"""Regression tests for issue #27: v2 ``POST /Entries/{id}/Export`` answering
with a JSON *pointer* (``{"@odata.context": ..., "value": "<Download url>"}``)
instead of the file body.

Every export path — buffered, streamed-to-file and the headers-only probe —
must follow the pointer with one authenticated GET and surface the real body.
Equally important: a genuine JSON edoc must never be mistaken for a pointer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE_V1, _BASE_V2

_HOST = "https://lf.example.test"
_EXPORT = f"{_BASE_V2}/Entries/42/Export"
_DOWNLOAD_PATH = "/LFRepositoryAPI/v2/Repositories/demo/Download/tok-123"
_DOWNLOAD = f"{_HOST}{_DOWNLOAD_PATH}"
_JSON_CT = "application/json;odata.metadata=minimal"

_BODY = b"Extracted OCR text from a scanned page.\n" * 20


def _pointer(url: str = _DOWNLOAD) -> dict[str, str]:
    return {"@odata.context": f"{_HOST}/LFRepositoryAPI/v2/$metadata#String", "value": url}


def _add_pointer(
    httpx_mock: HTTPXMock, pointer: object | None = None, *, content_type: str = _JSON_CT
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        json=_pointer() if pointer is None else pointer,
        headers={"content-type": content_type},
    )


def _add_download(
    httpx_mock: HTTPXMock,
    url: str = _DOWNLOAD,
    *,
    content: bytes = _BODY,
    content_type: str = "text/plain",
    status_code: int = 200,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=url,
        content=content,
        status_code=status_code,
        headers={"content-type": content_type},
    )


@pytest.fixture
def v2(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    return Settings()  # type: ignore[call-arg]


class _Chunked(httpx.AsyncByteStream):
    """A response stream with no Content-Length, like a chunked server reply."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        mid = max(1, len(self._data) // 2)
        yield self._data[:mid]
        yield self._data[mid:]


# --- buffered path: export_entry / export_entry_with_meta -------------------


@pytest.mark.asyncio
async def test_export_entry_follows_pointer_and_returns_real_body(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    """The exact scenario from the issue: get_document_text on a scanned entry."""
    _add_pointer(httpx_mock)
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        content = await client.export_entry(42, part="Text")

    assert content == _BODY
    assert b"odata" not in content  # the raw JSON wrapper must never leak out


@pytest.mark.asyncio
async def test_with_meta_returns_content_type_of_the_download_not_the_pointer(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content_type="application/pdf", content=b"%PDF-1.7 real")

    async with _build_client(v2) as client:
        content, content_type = await client.export_entry_with_meta(42, part="Edoc")

    assert content == b"%PDF-1.7 real"
    assert content_type == "application/pdf"


@pytest.mark.asyncio
async def test_exactly_two_requests_with_auth_and_wildcard_accept_on_follow(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        await client.export_entry(42, part="Text")

    first, second = httpx_mock.get_requests()
    assert (first.method, str(first.url)) == ("POST", _EXPORT)
    assert json.loads(first.read()) == {"part": "Text"}
    assert (second.method, str(second.url)) == ("GET", _DOWNLOAD)
    # Same authenticated client: the download request carries the bearer token.
    assert first.headers["authorization"] == "Bearer test-token"
    assert second.headers["authorization"] == "Bearer test-token"
    # The client default is Accept: application/json; the follow must not use it.
    assert second.headers["accept"] == "*/*"


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["Text", "Edoc", "Image"])
async def test_every_part_is_followed(httpx_mock: HTTPXMock, v2: Settings, part: str) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part=part) == _BODY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", _JSON_CT, "Application/JSON"],
)
async def test_pointer_detected_for_any_json_content_type_spelling(
    httpx_mock: HTTPXMock, v2: Settings, content_type: str
) -> None:
    _add_pointer(httpx_mock, content_type=content_type)
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == _BODY


@pytest.mark.asyncio
async def test_pointer_without_odata_context_is_still_followed(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock, {"value": _DOWNLOAD})
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == _BODY


@pytest.mark.asyncio
async def test_query_string_on_download_url_is_preserved(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    url = f"{_DOWNLOAD}?sig=abc%2Bdef&exp=99"
    _add_pointer(httpx_mock, _pointer(url))
    _add_download(httpx_mock, url)

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == _BODY

    assert str(httpx_mock.get_requests()[1].url) == url


# --- security / URL handling ------------------------------------------------


@pytest.mark.asyncio
async def test_pointer_to_internal_hostname_is_rewritten_to_configured_host(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    """A server behind a proxy may advertise its internal name. We must fetch
    from the configured host — and never send credentials to the advertised one."""
    _add_pointer(httpx_mock, _pointer(f"http://lf-internal.corp.local:8080{_DOWNLOAD_PATH}"))
    _add_download(httpx_mock)  # registered ONLY for the configured host

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == _BODY

    hosts = {r.url.host for r in httpx_mock.get_requests()}
    assert hosts == {"lf.example.test"}


@pytest.mark.asyncio
async def test_pointer_to_foreign_host_never_receives_credentials(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock, _pointer(f"https://evil.example.org{_DOWNLOAD_PATH}"))
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        await client.export_entry(42, part="Text")

    assert all(r.url.host != "evil.example.org" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_relative_pointer_is_resolved_against_the_export_url(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock, _pointer(_DOWNLOAD_PATH))
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == _BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://lf.example.test/x"])
async def test_non_http_pointer_scheme_is_rejected(
    httpx_mock: HTTPXMock, v2: Settings, bad: str
) -> None:
    _add_pointer(httpx_mock, _pointer(bad))

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError, match="unsupported scheme"):
            await client.export_entry(42, part="Text")

    assert len(httpx_mock.get_requests()) == 1  # nothing was fetched


@pytest.mark.asyncio
async def test_pointer_to_a_second_pointer_is_followed_only_once(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    """No redirect-style loops: one hop, then whatever comes back is the body."""
    second = json.dumps(_pointer(f"{_HOST}/loop")).encode()
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=second, content_type="application/json")

    async with _build_client(v2) as client:
        content = await client.export_entry(42, part="Text")

    assert content == second
    assert len(httpx_mock.get_requests()) == 2


# --- things that must NOT be treated as pointers ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"value": "https://x.example/y", "name": "report.json"},  # extra key => real data
        {"value": 42},  # not a URL string
        {"value": ""},
        {"value": "   "},
        {"data": [1, 2, 3]},  # no value key
        [{"value": "https://x.example/y"}],  # a list, not an object
        {"a": 1},
        "just a json string",
    ],
)
async def test_genuine_json_edoc_is_returned_untouched(
    httpx_mock: HTTPXMock, v2: Settings, payload: object
) -> None:
    raw = json.dumps(payload).encode()
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=raw, headers={"content-type": "application/json"}
    )

    async with _build_client(v2) as client:
        content, content_type = await client.export_entry_with_meta(42, part="Edoc")

    assert content == raw
    assert content_type == "application/json"
    assert len(httpx_mock.get_requests()) == 1  # no follow-up request


@pytest.mark.asyncio
async def test_pointer_shaped_body_with_non_json_content_type_is_not_followed(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    raw = json.dumps(_pointer()).encode()
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=raw, headers={"content-type": "text/plain"}
    )

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == raw

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_large_json_edoc_is_not_treated_as_a_pointer(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    big = json.dumps({"value": "x" * 20000}).encode()
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=big, headers={"content-type": "application/json"}
    )

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Edoc") == big


@pytest.mark.asyncio
async def test_plain_text_and_pdf_bodies_unchanged(httpx_mock: HTTPXMock, v2: Settings) -> None:
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=b"hello", headers={"content-type": "text/plain"}
    )

    async with _build_client(v2) as client:
        assert await client.export_entry(42, part="Text") == b"hello"

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_v1_edoc_endpoint_never_follows_pointer_shaped_json(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    raw = json.dumps(_pointer()).encode()
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=raw,
        headers={"content-type": "application/json"},
    )
    settings = Settings()  # type: ignore[call-arg]

    async with _build_client(settings) as client:
        assert await client.export_entry(42) == raw

    assert len(httpx_mock.get_requests()) == 1


# --- error handling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_download_404_surfaces_as_laserfiche_error_with_status(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock)
    _add_download(
        httpx_mock,
        content=b'{"title":"Not found"}',
        content_type="application/json",
        status_code=404,
    )

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry(42, part="Text")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_download_401_surfaces_as_laserfiche_error(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=b"denied", status_code=401)

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry(42, part="Text")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_download_connect_error_becomes_laserfiche_error(
    httpx_mock: HTTPXMock, v2: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "0")
    settings = Settings()  # type: ignore[call-arg]
    _add_pointer(httpx_mock)
    httpx_mock.add_exception(httpx.ConnectError("boom"), method="GET", url=_DOWNLOAD)

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="Network error"):
            await client.export_entry(42, part="Text")


# --- streamed path: export_entry_to_file (used by the CLI `cat`/download) ---


@pytest.mark.asyncio
async def test_to_file_follows_pointer_and_writes_real_bytes(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content_type="text/plain")
    dest = tmp_path / "out.txt"

    async with _build_client(v2) as client:
        written, content_type, digest = await client.export_entry_to_file(42, dest, part="Text")

    assert dest.read_bytes() == _BODY
    assert written == len(_BODY)
    assert content_type == "text/plain"
    assert digest == hashlib.sha256(_BODY).hexdigest()
    assert not dest.with_name("out.txt.part").exists()


@pytest.mark.asyncio
async def test_to_file_chunked_pointer_without_content_length_is_followed(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_EXPORT,
        stream=_Chunked(json.dumps(_pointer()).encode()),
        headers={"content-type": _JSON_CT},
    )
    _add_download(httpx_mock)
    dest = tmp_path / "out.txt"

    async with _build_client(v2) as client:
        await client.export_entry_to_file(42, dest, part="Text")

    assert dest.read_bytes() == _BODY


@pytest.mark.asyncio
async def test_to_file_small_genuine_json_edoc_is_written_intact(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    """The peek consumes the body; the replay must give it back byte-for-byte."""
    raw = json.dumps({"hello": "world", "n": [1, 2, 3]}).encode()
    for _ in range(2):  # peek + replay
        httpx_mock.add_response(
            method="POST",
            url=_EXPORT,
            stream=_Chunked(raw),
            headers={"content-type": "application/json"},
        )
    dest = tmp_path / "doc.json"

    async with _build_client(v2) as client:
        written, content_type, digest = await client.export_entry_to_file(42, dest)

    assert dest.read_bytes() == raw
    assert written == len(raw)
    assert content_type == "application/json"
    assert digest == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_to_file_declared_small_genuine_json_is_written_intact(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    raw = json.dumps({"hello": "world"}).encode()
    for _ in range(2):
        httpx_mock.add_response(
            method="POST", url=_EXPORT, content=raw, headers={"content-type": "application/json"}
        )
    dest = tmp_path / "doc.json"

    async with _build_client(v2) as client:
        await client.export_entry_to_file(42, dest)

    assert dest.read_bytes() == raw


@pytest.mark.asyncio
async def test_to_file_large_json_edoc_streams_without_replay(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    big = json.dumps({"rows": ["y" * 50] * 1000}).encode()
    httpx_mock.add_response(
        method="POST", url=_EXPORT, content=big, headers={"content-type": "application/json"}
    )
    dest = tmp_path / "big.json"

    async with _build_client(v2) as client:
        await client.export_entry_to_file(42, dest)

    assert dest.read_bytes() == big
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_to_file_max_bytes_applies_to_the_followed_download(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    """The cap must bound the real file, not the 150-byte pointer."""
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=b"z" * 5000)
    dest = tmp_path / "out.bin"

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError, match="size_exceeds_cap"):
            await client.export_entry_to_file(42, dest, max_bytes=1000)

    assert not dest.exists()
    assert not dest.with_name("out.bin.part").exists()


@pytest.mark.asyncio
async def test_to_file_download_404_leaves_no_file(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=b"nope", status_code=404)
    dest = tmp_path / "out.txt"

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry_to_file(42, dest, part="Text")

    assert exc_info.value.status_code == 404
    assert not dest.exists()
    assert not dest.with_name("out.txt.part").exists()


@pytest.mark.asyncio
async def test_to_file_follow_sends_auth_and_wildcard_accept(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock)

    async with _build_client(v2) as client:
        await client.export_entry_to_file(42, tmp_path / "o.txt", part="Text")

    _, second = httpx_mock.get_requests()
    assert second.headers["authorization"] == "Bearer test-token"
    assert second.headers["accept"] == "*/*"


# --- probe path: export_entry_meta_only (size cap / mode='info') ------------


@pytest.mark.asyncio
async def test_meta_only_reports_real_size_and_type_not_the_pointers(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=b"q" * 4321, content_type="application/pdf")

    async with _build_client(v2) as client:
        size, content_type = await client.export_entry_meta_only(42, part="Edoc")

    assert size == 4321
    assert content_type == "application/pdf"


@pytest.mark.asyncio
async def test_meta_only_small_genuine_json_keeps_its_own_size_and_type(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    raw = json.dumps({"hello": "world"}).encode()
    for _ in range(2):
        httpx_mock.add_response(
            method="POST", url=_EXPORT, content=raw, headers={"content-type": "application/json"}
        )

    async with _build_client(v2) as client:
        size, content_type = await client.export_entry_meta_only(42)

    assert size == len(raw)
    assert content_type == "application/json"


@pytest.mark.asyncio
async def test_meta_only_download_404_raises(httpx_mock: HTTPXMock, v2: Settings) -> None:
    _add_pointer(httpx_mock)
    _add_download(httpx_mock, content=b"nope", status_code=404)

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry_meta_only(42)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_meta_only_error_on_export_itself_still_raises(
    httpx_mock: HTTPXMock, v2: Settings
) -> None:
    httpx_mock.add_response(method="POST", url=_EXPORT, status_code=404, json={"title": "gone"})

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry_meta_only(42)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_to_file_large_chunked_json_edoc_is_replayed_intact(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    """No Content-Length and bigger than any pointer: the peek bails out and the
    request is replayed, so the genuine JSON file arrives complete."""
    big = json.dumps({"rows": ["y" * 50] * 1000}).encode()
    for _ in range(2):  # peek + replay
        httpx_mock.add_response(
            method="POST",
            url=_EXPORT,
            stream=_Chunked(big),
            headers={"content-type": "application/json"},
        )
    dest = tmp_path / "big.json"

    async with _build_client(v2) as client:
        written, _, digest = await client.export_entry_to_file(42, dest)

    assert dest.read_bytes() == big
    assert written == len(big)
    assert digest == hashlib.sha256(big).hexdigest()


@pytest.mark.asyncio
async def test_to_file_network_error_becomes_laserfiche_error(
    httpx_mock: HTTPXMock, v2: Settings, tmp_path: Path
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"), method="POST", url=_EXPORT)

    async with _build_client(v2) as client:
        with pytest.raises(LaserficheError, match="Network error streaming"):
            await client.export_entry_to_file(42, tmp_path / "o.bin")

    assert not (tmp_path / "o.bin").exists()
