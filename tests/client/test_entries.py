"""Tests for ``client/_entries.py`` — entry-level reads and document export."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE, _BASE_V1, _BASE_V2

# --- get_entry --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_uses_canonical_path(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
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


@pytest.mark.asyncio
async def test_get_entry_by_path_unwraps_v1_entry_envelope(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1 Repository API wraps the entry in ``{"entry": {...}}``; client must
    unwrap so EntryDetail.from_api (top-level keys) sees the real fields."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CSandbox",
        json={"entry": {"id": 84486, "name": "Sandbox", "entryType": "Folder"}},
    )

    async with _build_client(settings) as client:
        result = await client.get_entry_by_path("\\Sandbox")

    assert result["id"] == 84486
    assert result["name"] == "Sandbox"
    assert result["entryType"] == "Folder"


# --- list_folder ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folder_v1_uses_odata_entity_type_segment(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1: path is /Entries/{id}/Laserfiche.Repository.Folder/children (lowercase)."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE_V1}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=20"),
        json={"value": [], "@odata.count": 100},
    )

    async with _build_client(settings) as client:
        result = await client.list_folder(1, max_results=10, skip=20)

    assert result["@odata.count"] == 100


@pytest.mark.asyncio
async def test_list_folder_v2_uses_folder_children_path(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: path is /Entries/{id}/Folder/Children (PascalCase), NOT /Entries/{id}/Children."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Entries/1/Folder/Children?%24top=10&%24skip=20",
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
async def test_get_field_values_v1_uses_lowercase_fields(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1: segment is lowercase `fields`."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Entries/42/fields",
        json={"value": [{"fieldName": "Status", "values": ["Approved"]}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_field_values(42)

    assert result["value"][0]["fieldName"] == "Status"


@pytest.mark.asyncio
async def test_get_field_values_v2_uses_pascalcase_fields(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: segment is PascalCase `Fields`."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Entries/42/Fields",
        json={"value": [{"fieldName": "Status", "values": ["Approved"]}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_field_values(42)

    assert result["value"][0]["fieldName"] == "Status"


# --- export_entry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_export_entry_v1_edoc_uses_get_on_document_segment(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1 has no /Export — Edoc is fetched via GET on the Document entity-type segment."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"hello world",
    )

    async with _build_client(settings) as client:
        result = await client.export_entry(42, part="Edoc")

    assert result == b"hello world"
    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"


@pytest.mark.asyncio
async def test_export_entry_v1_rejects_text_part(
    lf_env: dict[str, str],
) -> None:
    """v1 has no text-extraction endpoint; asking for it raises a clear error."""
    settings = Settings()  # type: ignore[call-arg]

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="v1 has no endpoint"):
            await client.export_entry(42, part="Text")


@pytest.mark.asyncio
async def test_export_entry_v2_posts_with_part(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: download uses POST /Export with JSON body {part: 'Edoc'|'Text'}."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_V2}/Entries/42/Export",
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
async def test_export_entry_v2_raises_on_404(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_V2}/Entries/999/Export",
        status_code=404,
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry(999)

    assert exc_info.value.status_code == 404
