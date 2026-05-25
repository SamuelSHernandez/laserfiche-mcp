"""Tests for ``tools/preview_execute_splits.py`` — the 10 preview/execute wrappers.

The wrappers' contract is narrow: route the call through the existing
multiplex tool with the right ``confirmation_token`` argument, and refuse
the wrong call shape (token on preview, no token on execute). The
underlying multiplex tools already have full mock-HTTP coverage in
``test_writes_move_rename.py`` / ``test_writes_delete_entry.py`` /
``test_writes_delete_edoc_pages.py`` — these tests focus on the split
behavior, not re-covering the multiplex internals.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.tools.preview_execute_splits import (
    delete_edoc_execute,
    delete_edoc_preview,
    delete_entry_execute,
    delete_entry_preview,
    delete_pages_execute,
    delete_pages_preview,
    move_entry_execute,
    move_entry_preview,
    rename_entry_execute,
    rename_entry_preview,
)
from tests.conftest import _BASE

# --- Refusal contracts (no HTTP needed) -------------------------------------


@pytest.mark.asyncio
async def test_preview_tools_refuse_when_token_passed(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """Every ``..._preview`` must refuse a non-None confirmation_token."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)

    cases = [
        rename_entry_preview(entry_id=1, new_name="x", confirmation_token="t"),
        move_entry_preview(entry_id=1, new_parent_id=2, confirmation_token="t"),
        delete_entry_preview(entry_id=1, confirmation_token="t"),
        delete_edoc_preview(entry_id=1, confirmation_token="t"),
        delete_pages_preview(entry_id=1, page_range="1", confirmation_token="t"),
    ]
    for awaitable in cases:
        result = await awaitable
        assert result["mode"] == "error"
        assert result["error"] == "preview_does_not_accept_token"
        assert result["kind"] == "invalid_input"


@pytest.mark.asyncio
async def test_execute_tools_refuse_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """Every ``..._execute`` must refuse when confirmation_token is empty.

    The execute signatures type confirmation_token as ``str`` (no Optional)
    so a missing arg is a Python-level error; passing the empty string is
    the lookalike case that has to be caught at runtime.
    """
    monkeypatch.setattr(server._get_settings(), "read_only", False)

    cases = [
        rename_entry_execute(entry_id=1, new_name="x", confirmation_token=""),
        move_entry_execute(entry_id=1, new_parent_id=2, confirmation_token=""),
        delete_entry_execute(entry_id=1, confirmation_token=""),
        delete_edoc_execute(entry_id=1, confirmation_token=""),
        delete_pages_execute(entry_id=1, page_range="1", confirmation_token=""),
    ]
    for awaitable in cases:
        result = await awaitable
        assert result["mode"] == "error"
        assert result["error"] == "execute_requires_token"
        assert result["kind"] == "invalid_input"


# --- Happy-path round-trips ------------------------------------------------


@pytest.mark.asyncio
async def test_rename_split_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """rename_entry_preview returns a token; rename_entry_execute consumes it."""
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

    preview = await rename_entry_preview(entry_id=42, new_name="New")
    assert preview["mode"] == "preview"
    assert preview["would_be_full_path"] == "\\Folder\\New"
    token = preview["confirmation_token"]

    executed = await rename_entry_execute(
        entry_id=42,
        new_name="New",
        confirmation_token=token,
    )
    assert executed["mode"] == "executed"
    assert executed["new_name"] == "New"


@pytest.mark.asyncio
async def test_move_split_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # Source entry
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "doc.pdf",
            "entryType": "Document",
            "fullPath": "\\Src\\doc.pdf",
        },
        is_reusable=True,
    )
    # Destination parent
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/99",
        json={"id": 99, "name": "Dst", "entryType": "Folder", "fullPath": "\\Dst"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "parentId": 99, "name": "doc.pdf"},
    )

    preview = await move_entry_preview(entry_id=42, new_parent_id=99)
    assert preview["mode"] == "preview"
    token = preview["confirmation_token"]

    executed = await move_entry_execute(
        entry_id=42,
        new_parent_id=99,
        confirmation_token=token,
    )
    assert executed["mode"] == "executed"
    assert executed["new_parent_id"] == 99


@pytest.mark.asyncio
async def test_delete_entry_split_round_trip(
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
            "name": "doc.pdf",
            "entryType": "Document",
            "fullPath": "\\Src\\doc.pdf",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        json={"taskId": "abc-123"},
        status_code=201,
    )

    preview = await delete_entry_preview(entry_id=42)
    assert preview["mode"] == "preview"
    assert preview["operation"] == "delete_entry"
    token = preview["confirmation_token"]

    executed = await delete_entry_execute(entry_id=42, confirmation_token=token)
    assert executed["mode"] == "executed"


@pytest.mark.asyncio
async def test_split_tools_are_registered_under_v2_names(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """The 10 split tools must appear in the v2 rename map and registry.

    The rename map is what the README documents; the registry is what the
    decorator iteration in server.py walks at startup. Both should list
    the splits so the LLM catalog reflects them.
    """
    expected_legacy = {
        "rename_entry_preview",
        "rename_entry_execute",
        "move_entry_preview",
        "move_entry_execute",
        "delete_entry_preview",
        "delete_entry_execute",
        "delete_edoc_preview",
        "delete_edoc_execute",
        "delete_pages_preview",
        "delete_pages_execute",
    }
    assert expected_legacy.issubset(server._V2_RENAME_MAP.keys())

    expected_v2 = {
        "laserfiche_entry_rename_preview",
        "laserfiche_entry_rename_execute",
        "laserfiche_entry_move_preview",
        "laserfiche_entry_move_execute",
        "laserfiche_entry_delete_preview",
        "laserfiche_entry_delete_execute",
        "laserfiche_document_edoc_delete_preview",
        "laserfiche_document_edoc_delete_execute",
        "laserfiche_document_pages_delete_preview",
        "laserfiche_document_pages_delete_execute",
    }
    assert expected_v2.issubset(set(server._V2_RENAME_MAP.values()))
