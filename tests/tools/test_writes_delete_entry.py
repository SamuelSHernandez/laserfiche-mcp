"""Tests for ``tools/writes_delete_entry.py`` — the two-step entry delete."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- preview/execute happy paths --------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_preview_returns_token(
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
            "name": "Doomed",
            "entryType": "Document",
            "fullPath": "\\Trash\\Doomed",
        },
    )
    preview = await server.delete_entry(42)
    assert preview["mode"] == "preview"
    assert preview["entry_id"] == 42
    assert "confirmation_token" in preview
    assert "warning" in preview


@pytest.mark.asyncio
async def test_delete_entry_executes_with_valid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doomed", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-xyz", "taskId": "task-1"},
    )

    preview = await server.delete_entry(42)
    token = preview["confirmation_token"]
    result = await server.delete_entry(42, confirmation_token=token)

    assert result["mode"] == "executed"
    assert result["operation_token"] == "op-xyz"


@pytest.mark.asyncio
async def test_delete_entry_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doomed", "entryType": "Document"},
    )
    result = await server.delete_entry(42, confirmation_token="garbage")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"


@pytest.mark.asyncio
async def test_delete_entry_token_bound_to_entry_id(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A token issued for entry A must not work to delete entry B."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "First", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/99",
        json={"id": 99, "name": "Second", "entryType": "Document"},
    )

    preview_42 = await server.delete_entry(42)
    token = preview_42["confirmation_token"]
    bad_call = await server.delete_entry(99, confirmation_token=token)
    assert bad_call["mode"] == "error"
    assert bad_call["error"] == "invalid_confirmation_token"


# --- folder child-count probe -----------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_preview_reports_folder_child_count(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    cap = server._get_settings().delete_folder_max_descendants
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Big", "entryType": "Folder"},
    )
    # Probe fetches cap+1 children; with 47 returned (< cap+1), count is exact.
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?%24top={cap + 1}&%24skip=0"
        ),
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(47)]},
    )
    preview = await server.delete_entry(100)
    assert preview["mode"] == "preview"
    assert preview["immediate_child_count"] == 47


# --- batch cap + force_large_delete -----------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_refuses_exceeding_batch_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "delete_folder_max_descendants", 10)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={
            "id": 100,
            "name": "Big",
            "entryType": "Folder",
            "fullPath": "\\Big",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?%24top=11&%24skip=0"),
        # cap=10 + 1 = 11 items returned ⇒ exceeds_cap=True.
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(11)]},
        is_reusable=True,
    )
    preview = await server.delete_entry(100)
    assert preview["mode"] == "preview"
    assert preview["exceeds_batch_cap"] is True

    blocked = await server.delete_entry(
        100,
        confirmation_token=preview["confirmation_token"],
    )
    assert blocked["mode"] == "error"
    assert blocked["error"] == "exceeds_batch_cap"


@pytest.mark.asyncio
async def test_delete_entry_force_large_delete_overrides_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "delete_folder_max_descendants", 10)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={
            "id": 100,
            "name": "Big",
            "entryType": "Folder",
            "fullPath": "\\Big",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?%24top=11&%24skip=0"),
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(11)]},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/100",
        status_code=202,
        json={"token": "op-xyz"},
    )
    preview = await server.delete_entry(100)
    result = await server.delete_entry(
        100,
        confirmation_token=preview["confirmation_token"],
        force_large_delete=True,
    )
    assert result["mode"] == "executed"


# --- audit-reason requirement -----------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_requires_audit_reason_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "require_audit_reason", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
        is_reusable=True,
    )
    preview = await server.delete_entry(42)
    refused = await server.delete_entry(
        42,
        confirmation_token=preview["confirmation_token"],
    )
    assert refused["mode"] == "error"
    assert refused["error"] == "audit_reason_required"


@pytest.mark.asyncio
async def test_delete_entry_proceeds_with_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "require_audit_reason", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-xyz"},
    )
    preview = await server.delete_entry(42)
    result = await server.delete_entry(
        42,
        confirmation_token=preview["confirmation_token"],
        audit_reason_id=5,
    )
    assert result["mode"] == "executed"


# --- fetch-failure path -----------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_returns_structured_error_when_entry_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Fetch-failure path: entry doesn't exist, structured error surfaces (not RuntimeError)."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        status_code=404,
    )
    result = await server.delete_entry(999)
    assert result["mode"] == "error"
    assert result["operation"] == "delete_entry"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 999
