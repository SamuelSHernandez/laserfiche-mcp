"""Tests for LaserficheClient using pytest-httpx mocks.

Endpoint paths verified against the official ``Laserfiche/lf-repository-api-client-java``
client (``EntriesClientImpl.java`` and ``SimpleSearchesClientImpl.java``).
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient, LaserficheError, build_repo_path
from laserfiche_mcp.config import Settings

_BASE = "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo"


class _StubAuth(AuthStrategy):
    """Bypasses the /Token roundtrip so client tests stay focused."""

    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer test-token"


def _build_client(settings: Settings) -> LaserficheClient:
    return LaserficheClient(settings, _StubAuth())


# --- build_repo_path ---------------------------------------------------------


@pytest.mark.parametrize(
    "base, repo, suffix, expected",
    [
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "Entries/42",
            "https://lf.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI/",
            "demo",
            "Entries/42",
            "https://lf.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "/Entries/42",
            "https://lf.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        ),
    ],
)
def test_build_repo_path(base: str, repo: str, suffix: str, expected: str) -> None:
    assert build_repo_path(base, repo, suffix) == expected


# --- get_entry --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_uses_canonical_path(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Smith,John", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer test-token"


# --- get_entry_by_path ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_by_path_passes_full_path(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CImports%5C2024",
        json={"id": 99, "name": "2024", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        result = await client.get_entry_by_path("\\Imports\\2024")

    assert result["id"] == 99


# --- list_folder ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folder_uses_folder_children_path(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Regression: path is /Entries/{id}/Folder/Children, NOT /Entries/{id}/Children."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Folder/Children?%24top=10&%24skip=20",
        json={"value": [], "@odata.count": 100},
    )

    async with _build_client(settings) as client:
        result = await client.list_folder(1, max_results=10, skip=20)

    assert result["@odata.count"] == 100


# --- search_entries ---------------------------------------------------------


@pytest.mark.asyncio
async def test_search_entries_posts_to_simple_searches(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Regression: search is POST /SimpleSearches with JSON body, not GET with query."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=10",
        json={"value": []},
    )

    async with _build_client(settings) as client:
        await client.search_entries('{LF:Name="Smith"}', max_results=10)

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    body = request.read().decode()
    assert '"searchCommand"' in body
    assert "Smith" in body


# --- get_field_values -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_field_values(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Fields",
        json={"value": [{"fieldName": "Status", "values": ["Approved"]}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_field_values(42)

    assert result["value"][0]["fieldName"] == "Status"


# --- export_entry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_export_entry_posts_with_part(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Regression: download uses POST /Export with JSON body {part: 'Edoc'|'Text'}."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/42/Export",
        content=b"hello world",
    )

    async with _build_client(settings) as client:
        result = await client.export_entry(42, part="Text")

    assert result == b"hello world"
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    body = request.read().decode()
    assert '"part"' in body and '"Text"' in body


@pytest.mark.asyncio
async def test_export_entry_raises_on_404(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/999/Export",
        status_code=404,
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry(999)

    assert exc_info.value.status_code == 404


# --- error handling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_error_response_raises(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
        method="GET", url=url,
        json={"id": 42, "name": "x", "entryType": "Folder"},
    )

    import laserfiche_mcp.client as client_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)

    async with _build_client(settings) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_does_not_retry_4xx(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
