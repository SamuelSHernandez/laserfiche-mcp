"""Tests for ``client/_core.py`` — ``build_repo_path`` + transport (retry, error wrapping)."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError, build_repo_path
from laserfiche_mcp.config import ApiVersion, Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE

# --- build_repo_path ---------------------------------------------------------


@pytest.mark.parametrize(
    "base, repo, suffix, version, expected",
    [
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI/",
            "demo",
            "Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "/Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "Entries/42",
            ApiVersion.V2,
            "https://lf.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        ),
    ],
)
def test_build_repo_path(
    base: str,
    repo: str,
    suffix: str,
    version: ApiVersion,
    expected: str,
) -> None:
    assert build_repo_path(base, repo, suffix, version) == expected


def test_build_repo_path_defaults_to_v1() -> None:
    """Production default is v1 — confirm callers that don't pass a version get v1."""
    assert (
        build_repo_path("https://lf.test/LFRepositoryAPI", "demo", "Entries/42")
        == "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42"
    )


# --- error handling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_error_response_raises(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        status_code=404,
        json={"error": "Entry not found"},
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.get_entry(999)

    assert exc_info.value.status_code == 404


# --- retry behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_transient_5xx(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "2")
    settings = Settings()  # type: ignore[call-arg]

    url = f"{_BASE}/Entries/42"
    httpx_mock.add_response(method="GET", url=url, status_code=503)
    httpx_mock.add_response(method="GET", url=url, status_code=503)
    httpx_mock.add_response(
        method="GET",
        url=url,
        json={"id": 42, "name": "x", "entryType": "Folder"},
    )

    # Patch the asyncio.sleep used by _CoreClient._send so we don't
    # actually wait for the retry backoff.
    from laserfiche_mcp.client import _core as client_core

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_core.asyncio, "sleep", _no_sleep)

    async with _build_client(settings) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_does_not_retry_4xx(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        status_code=403,
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.get_entry(42)

    assert exc_info.value.status_code == 403
    assert len(httpx_mock.get_requests()) == 1
