"""Tests for the streaming download path in ``client/_core.py``.

This is the primitive that makes large documents usable at all: it never
buffers the body, and it must never leave a truncated file that looks like a
complete one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE_V1, _BASE_V2

_EDOC_V1 = f"{_BASE_V1}/Entries/42/Laserfiche.Repository.Document/edoc"


@pytest.mark.asyncio
async def test_streams_to_disk_and_returns_size_type_and_hash(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path
) -> None:
    body = b"a document body" * 100
    httpx_mock.add_response(
        method="GET",
        url=_EDOC_V1,
        content=body,
        headers={"content-type": "application/pdf"},
    )
    settings = Settings()  # type: ignore[call-arg]
    dest = tmp_path / "out.pdf"

    async with _build_client(settings) as client:
        written, content_type, digest = await client.export_entry_to_file(42, dest)

    assert dest.read_bytes() == body
    assert written == len(body)
    assert content_type == "application/pdf"
    assert digest == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_creates_missing_parent_directories(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=_EDOC_V1, content=b"x")
    settings = Settings()  # type: ignore[call-arg]
    dest = tmp_path / "nested" / "deeper" / "out.bin"

    async with _build_client(settings) as client:
        await client.export_entry_to_file(42, dest)

    assert dest.exists()


@pytest.mark.asyncio
async def test_declared_size_over_the_cap_is_refused_before_writing(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path
) -> None:
    body = b"y" * 5000
    httpx_mock.add_response(
        method="GET", url=_EDOC_V1, content=body, headers={"content-length": "5000"}
    )
    settings = Settings()  # type: ignore[call-arg]
    dest = tmp_path / "too-big.bin"

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="size_exceeds_cap"):
            await client.export_entry_to_file(42, dest, max_bytes=100)

    assert not dest.exists()


@pytest.mark.asyncio
async def test_no_partial_file_survives_a_refused_transfer(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path
) -> None:
    """A truncated file that looks complete is worse than no file."""
    httpx_mock.add_response(
        method="GET", url=_EDOC_V1, content=b"z" * 5000, headers={"content-length": "5000"}
    )
    settings = Settings()  # type: ignore[call-arg]
    dest = tmp_path / "aborted.bin"

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError):
            await client.export_entry_to_file(42, dest, max_bytes=10)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_http_error_raises_without_creating_a_file(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=_EDOC_V1, status_code=404, json={"title": "gone"})
    settings = Settings()  # type: ignore[call-arg]
    dest = tmp_path / "missing.bin"

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as caught:
            await client.export_entry_to_file(42, dest)

    assert caught.value.status_code == 404
    assert not dest.exists()


@pytest.mark.asyncio
async def test_v2_posts_to_the_export_endpoint(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    httpx_mock.add_response(method="POST", url=f"{_BASE_V2}/Entries/42/Export", content=b"body")
    settings = Settings()  # type: ignore[call-arg]

    async with _build_client(settings) as client:
        await client.export_entry_to_file(42, tmp_path / "out.bin")

    assert httpx_mock.get_requests()[0].method == "POST"


@pytest.mark.asyncio
async def test_v1_refuses_non_edoc_parts(lf_env: dict[str, str], tmp_path: Path) -> None:
    settings = Settings()  # type: ignore[call-arg]

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="v1 has no endpoint"):
            await client.export_entry_to_file(42, tmp_path / "out.txt", part="Text")
