"""Smoke tests for the LaserficheClient using pytest-httpx mocks."""

from __future__ import annotations

import os

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import BasicAuthStrategy
from laserfiche_mcp.client import LaserficheClient, LaserficheError
from laserfiche_mcp.config import Settings


def _settings() -> Settings:
    """Build settings without polluting real env vars."""
    os.environ.update({
        "LF_DEPLOYMENT_MODE": "self_hosted",
        "LF_REPO_API_URL": "https://lf.example.test/LFRepositoryAPI",
        "LF_REPOSITORY_ID": "demo",
        "LF_AUTH_MODE": "basic",
        "LF_USERNAME": "svc",
        "LF_PASSWORD": "secret",
    })
    return Settings()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_get_entry_shapes_request(httpx_mock: HTTPXMock) -> None:
    settings = _settings()
    auth = BasicAuthStrategy(settings.username or "", settings.password or "")

    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        json={"id": 42, "name": "Smith,John", "entryType": "Folder"},
    )

    async with LaserficheClient(settings, auth) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    assert result["name"] == "Smith,John"

    # Confirm Authorization header was applied
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_search_uses_query_param(httpx_mock: HTTPXMock) -> None:
    settings = _settings()
    auth = BasicAuthStrategy(settings.username or "", settings.password or "")

    httpx_mock.add_response(
        method="GET",
        url=(
            "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo"
            "/Entries/SearchEntries?searchCommand=%7BLF%3AName%3D%22Smith%22%7D&%24top=10"
        ),
        json={"value": []},
    )

    async with LaserficheClient(settings, auth) as client:
        await client.search_entries('{LF:Name="Smith"}', max_results=10)


@pytest.mark.asyncio
async def test_error_response_raises(httpx_mock: HTTPXMock) -> None:
    settings = _settings()
    auth = BasicAuthStrategy(settings.username or "", settings.password or "")

    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/999",
        status_code=404,
        json={"error": "Entry not found"},
    )

    async with LaserficheClient(settings, auth) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.get_entry(999)

    assert exc_info.value.status_code == 404
