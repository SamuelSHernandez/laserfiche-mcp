"""Tests for ``tools/writes_delete_edoc_pages.py`` — delete_edoc + delete_pages."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- delete_edoc ------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_edoc_preview_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "pageCount": 5,
            "extension": "pdf",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        json={"value": True},
    )
    preview = await server.delete_edoc(42)
    assert preview["mode"] == "preview"
    result = await server.delete_edoc(42, confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_delete_edoc_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_edoc(42, confirmation_token="bad")
    assert result["mode"] == "error"


@pytest.mark.asyncio
async def test_delete_edoc_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
        is_reusable=True,
    )
    preview = await server.delete_edoc(42)
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        status_code=500,
    )
    result = await server.delete_edoc(42, confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_delete_edoc_propagates_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        status_code=404,
    )
    result = await server.delete_edoc(42)
    assert result["mode"] == "error"
    assert result["error"] == "not_found"


# --- delete_pages -----------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_pages_refuses_empty_range(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.delete_pages(42, "")
    assert result["mode"] == "error"
    assert result["error"] == "page_range_required"


@pytest.mark.asyncio
async def test_delete_pages_rejects_malformed_page_range(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.delete_pages(entry_id=42, page_range="1, 2")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_page_range"
    assert result["entry_id"] == 42


@pytest.mark.asyncio
async def test_delete_pages_preview_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "pageCount": 10},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/pages?pageRange=1-3",
        json={"value": True},
    )
    preview = await server.delete_pages(42, "1-3")
    assert preview["mode"] == "preview"
    result = await server.delete_pages(
        42,
        "1-3",
        confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_delete_pages_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_pages(42, "1-3", confirmation_token="bad")
    assert result["mode"] == "error"


@pytest.mark.asyncio
async def test_delete_pages_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X", "pageCount": 10},
        is_reusable=True,
    )
    preview = await server.delete_pages(42, "1-3")
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/pages?pageRange=1-3",
        status_code=500,
    )
    result = await server.delete_pages(42, "1-3", confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "error"
    assert result["error"] == "server_error"
    assert (
        result["extra"]["page_range"] == "1-3"
        if "extra" in result
        else result["page_range"] == "1-3"
    )


@pytest.mark.asyncio
async def test_delete_pages_propagates_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        status_code=404,
    )
    result = await server.delete_pages(42, "1-3")
    assert result["mode"] == "error"
    assert result["error"] == "not_found"
