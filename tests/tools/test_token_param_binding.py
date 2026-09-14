"""Confirmation tokens must bind the operation's execute-relevant parameters.

Reproduces the three drift attacks from the v2.2.0 adversarial review:
previewing one operation and executing a different one with the same token
(delete pages "1-2" -> execute "1-9999"; rename to X -> execute rename to Y;
move to folder A -> execute move to folder B). Each must now fail token
verification with a reason naming the drifted parameter — the user confirmed
the preview, not whatever the execute call happens to carry.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import confirmation, server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- unit level: params binding in the token itself ---------------------------


def test_token_roundtrips_with_identical_params() -> None:
    token = confirmation.create_token("delete_pages", 42, "Doc", params={"page_range": "1-2"})
    ok, reason = confirmation.verify_token(
        token, "delete_pages", 42, "Doc", params={"page_range": "1-2"}
    )
    assert ok is True
    assert reason is None


def test_token_rejects_drifted_param_naming_it() -> None:
    token = confirmation.create_token("delete_pages", 42, "Doc", params={"page_range": "1-2"})
    ok, reason = confirmation.verify_token(
        token, "delete_pages", 42, "Doc", params={"page_range": "1-9999"}
    )
    assert ok is False
    assert reason is not None
    assert "page_range" in reason
    assert "without confirmation_token" in reason


def test_token_rejects_param_set_mismatch() -> None:
    token = confirmation.create_token("move_entry", 42, "Doc", params={"new_parent_id": 100})
    ok, reason = confirmation.verify_token(token, "move_entry", 42, "Doc", params=None)
    assert ok is False
    assert reason is not None
    assert "new_parent_id" in reason


def test_paramless_token_still_roundtrips() -> None:
    token = confirmation.create_token("delete_entry", 42, "Doc")
    ok, reason = confirmation.verify_token(token, "delete_entry", 42, "Doc")
    assert ok is True
    assert reason is None


# --- end-to-end: the three reproduced drift attacks ---------------------------


@pytest.mark.asyncio
async def test_delete_pages_token_cannot_widen_the_page_range(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Preview pages 1-2, execute 1-9999 — must fail, and nothing may reach
    the DELETE endpoint."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "pageCount": 10},
        is_reusable=True,
    )

    preview = await server.delete_pages(42, "1-2")
    assert preview["mode"] == "preview"

    result = await server.delete_pages(
        42,
        "1-9999",
        confirmation_token=preview["confirmation_token"],
    )

    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert "page_range" in result["reason"]
    assert not [r for r in httpx_mock.get_requests() if r.method == "DELETE"]


@pytest.mark.asyncio
async def test_rename_token_cannot_swap_the_new_name(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Preview rename to 'approved-final.pdf', execute 'totally-different.pdf'."""
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

    preview = await server.rename_entry(42, "approved-final.pdf")
    assert preview["mode"] == "preview"

    result = await server.rename_entry(
        42,
        "totally-different.pdf",
        confirmation_token=preview["confirmation_token"],
    )

    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert "new_name" in result["reason"]
    assert not [r for r in httpx_mock.get_requests() if r.method == "PATCH"]


@pytest.mark.asyncio
async def test_move_token_cannot_switch_the_destination(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Preview move to parent 100, execute to parent 999."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\A\\Doc"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Allowed", "entryType": "Folder", "fullPath": "\\B"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        json={"id": 999, "name": "Other", "entryType": "Folder", "fullPath": "\\C"},
        is_reusable=True,
    )

    preview = await server.move_entry(42, 100)
    assert preview["mode"] == "preview"

    result = await server.move_entry(
        42,
        999,
        confirmation_token=preview["confirmation_token"],
    )

    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert "new_parent_id" in result["reason"]
    assert not [r for r in httpx_mock.get_requests() if r.method == "PATCH"]


@pytest.mark.asyncio
async def test_move_token_cannot_add_a_rename_after_preview(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Previewing a plain move then executing with new_name= is also drift."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\A\\Doc"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Dest", "entryType": "Folder", "fullPath": "\\B"},
        is_reusable=True,
    )

    preview = await server.move_entry(42, 100)
    result = await server.move_entry(
        42,
        100,
        confirmation_token=preview["confirmation_token"],
        new_name="sneaky.pdf",
    )

    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert "new_name" in result["reason"]


@pytest.mark.asyncio
async def test_split_execute_tools_inherit_the_binding(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """The *_preview / *_execute splits delegate to the multiplex tools, so
    the parameter binding must hold across the split path too."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "pageCount": 10},
        is_reusable=True,
    )

    preview = await server.delete_pages_preview(42, "1-2")
    assert preview["mode"] == "preview"

    result = await server.delete_pages_execute(
        42,
        "1-9999",
        confirmation_token=preview["confirmation_token"],
    )

    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert "page_range" in result["reason"]
