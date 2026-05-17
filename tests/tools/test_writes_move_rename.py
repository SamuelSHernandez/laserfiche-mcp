"""Tests for ``tools/writes_move_rename.py`` — two-step rename and move."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- rename_entry: preview/confirm ------------------------------------------


@pytest.mark.asyncio
async def test_rename_entry_preview_token_then_execute(
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
            "name": "Old",
            "entryType": "Document",
            "fullPath": "\\Folder\\Old",
            "folderPath": "\\Folder",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "name": "New"},
    )

    preview = await server.rename_entry(42, "New")
    assert preview["mode"] == "preview"
    assert preview["would_be_full_path"] == "\\Folder\\New"

    result = await server.rename_entry(
        42,
        "New",
        confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"
    assert result["new_name"] == "New"


@pytest.mark.asyncio
async def test_rename_entry_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Old", "entryType": "Document"},
    )
    result = await server.rename_entry(42, "New", confirmation_token="bad")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"


@pytest.mark.asyncio
async def test_rename_entry_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.rename_entry(entry_id=42, new_name="bad\name")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"
    assert result["entry_id"] == 42


@pytest.mark.asyncio
async def test_rename_preview_falls_back_when_no_paths(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Preview computes would_be_path from the bare new_name when entry has no path metadata."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/77",
        json={"id": 77, "name": "Old", "entryType": "Document"},  # no fullPath, no folderPath
    )
    preview = await server.rename_entry(77, "New")
    assert preview["mode"] == "preview"
    assert preview["current_full_path"] == ""
    assert preview["would_be_full_path"] == "New"


@pytest.mark.asyncio
async def test_rename_entry_classifies_upstream_error_on_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Upstream 500 on the PATCH leg flows through classify_lf_error."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Old", "entryType": "Document", "fullPath": "\\X\\Old"},
        is_reusable=True,
    )
    preview = await server.rename_entry(42, "New")
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        status_code=500,
        json={"title": "boom"},
    )
    result = await server.rename_entry(42, "New", confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "error"
    assert result["error"] == "server_error"
    assert result["entry_id"] == 42
    assert (
        result["extra"]["new_name"] == "New" if "extra" in result else result["new_name"] == "New"
    )


# --- move_entry: preview/confirm + destination fence ------------------------


@pytest.mark.asyncio
async def test_move_entry_preview_then_execute(
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
            "fullPath": "\\Old\\Doc",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/200",
        json={"id": 200, "name": "New", "entryType": "Folder", "fullPath": "\\New"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "name": "Doc", "parentId": 200},
    )

    preview = await server.move_entry(42, 200)
    assert preview["mode"] == "preview"
    assert preview["would_be_full_path"] == "\\New\\Doc"

    result = await server.move_entry(
        42,
        200,
        confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_move_entry_rejects_invalid_token(
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
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/200",
        json={"id": 200, "name": "Other", "entryType": "Folder"},
    )
    result = await server.move_entry(42, 200, confirmation_token="bad")
    assert result["mode"] == "error"


@pytest.mark.asyncio
async def test_move_entry_destination_is_fenced(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A move from an allowed source into a denied destination is refused."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Protected")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Sandbox\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/300",
        json={
            "id": 300,
            "name": "Protected",
            "entryType": "Folder",
            "fullPath": "\\Protected",
        },
    )
    result = await server.move_entry(42, 300)
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_move_entry_destination_lookup_failure_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If the destination GET errors, the dest-fence is skipped and preview still returns."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Old\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        status_code=404,
        json={"title": "not found"},
    )
    preview = await server.move_entry(42, 999)
    assert preview["mode"] == "preview"
    # Empty target_path → would_be_full_path falls back to just the name.
    assert preview["would_be_full_path"] == "Doc"


@pytest.mark.asyncio
async def test_move_entry_classifies_upstream_error_on_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\A\\Doc"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/200",
        json={"id": 200, "name": "B", "entryType": "Folder", "fullPath": "\\B"},
        is_reusable=True,
    )
    preview = await server.move_entry(42, 200)
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        status_code=429,
        json={},
    )
    result = await server.move_entry(42, 200, confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "error"
    assert result["error"] == "rate_limited"
