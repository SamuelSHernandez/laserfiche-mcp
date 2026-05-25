"""Tests for ``tools/write_collapses.py`` — the 5 collapsed write tools.

Focus: the collapse logic (mode routing, mutually-exclusive arg checks,
``template_name=None`` clear path, ``timeout_seconds=0`` poll path).
Underlying tool behavior is already covered in their own test files;
these tests pin down "does the collapse route to the right tool with
the right args."
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.tools.write_collapses import (
    field_update,
    link_update,
    tag_update,
    task_wait_or_poll,
    template_assign_or_remove,
)
from tests.conftest import _BASE


@pytest.fixture
def _writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)


# --- field_update -----------------------------------------------------------


@pytest.mark.asyncio
async def test_field_update_merge_calls_merge_fields(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": [{"fieldName": "Status", "values": ["Old"]}]},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )

    result = await field_update(entry_id=42, updates={"Status": ["New"]})
    assert result["mode"] == "executed"
    assert result["operation"] == "merge_fields"


@pytest.mark.asyncio
async def test_field_update_replace_calls_set_fields(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )

    # set_fields returns the raw server response — no `operation` key,
    # which is what distinguishes the replace path from merge.
    result = await field_update(
        entry_id=42, updates={"Status": ["New"]}, mode="replace"
    )
    assert result == {"value": []}


@pytest.mark.asyncio
async def test_field_update_invalid_mode(
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    result = await field_update(entry_id=42, updates={}, mode="union")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_mode"
    assert result["kind"] == "invalid_input"


# --- tag_update -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_update_replace_routes_to_set_tags(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": [{"name": "Confidential"}]},
    )

    result = await tag_update(entry_id=42, replace=["Confidential"])
    assert result == {"value": [{"name": "Confidential"}]}


@pytest.mark.asyncio
async def test_tag_update_add_routes_to_merge_tags(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": [{"name": "Old"}]},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": [{"name": "Old"}, {"name": "New"}]},
    )

    result = await tag_update(entry_id=42, add=["New"])
    assert result["mode"] == "executed"
    assert result["operation"] == "merge_tags"
    assert "New" in result["added"]


@pytest.mark.asyncio
async def test_tag_update_conflicting_modes(
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    result = await tag_update(entry_id=42, add=["x"], replace=["y"])
    assert result["mode"] == "error"
    assert result["error"] == "conflicting_modes"


@pytest.mark.asyncio
async def test_tag_update_no_op(
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    result = await tag_update(entry_id=42)
    assert result["mode"] == "error"
    assert result["error"] == "no_op"


@pytest.mark.asyncio
async def test_tag_update_empty_replace_is_explicit_clear(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    """``replace=[]`` is a deliberate "clear all tags" — NOT a no_op."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": []},
    )

    result = await tag_update(entry_id=42, replace=[])
    assert result == {"value": []}


# --- link_update ------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_update_replace_routes_to_set_links(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/links",
        json={"value": []},
    )

    result = await link_update(
        entry_id=42,
        links=[{"targetId": 99, "linkTypeId": 1}],
    )
    assert result == {"value": []}


@pytest.mark.asyncio
async def test_link_update_merge_deduplicates_and_preserves(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/links",
        json={"value": [{"targetId": 100, "linkTypeId": 1}]},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/links",
        json={"value": []},
    )

    result = await link_update(
        entry_id=42,
        links=[
            {"targetId": 100, "linkTypeId": 1},  # duplicate, dropped
            {"targetId": 200, "linkTypeId": 2},  # new
        ],
        mode="merge",
    )
    assert result["mode"] == "executed"
    assert result["operation"] == "link_update"
    assert result["total_links"] == 2
    assert result["added_count"] == 1


@pytest.mark.asyncio
async def test_link_update_invalid_mode(
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    result = await link_update(entry_id=42, links=[], mode="union")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_mode"


# --- template_assign_or_remove ----------------------------------------------


@pytest.mark.asyncio
async def test_template_update_assigns_when_name_set(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    # validate_required_fields fires from assign_template; patch the
    # cached settings directly so we don't have to mock the
    # FieldDefinitions probe.
    monkeypatch.setattr(server._get_settings(), "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )

    result = await template_assign_or_remove(entry_id=42, template_name="T")
    assert result == {"id": 42, "templateName": "T"}


@pytest.mark.asyncio
async def test_template_update_removes_when_name_none(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "fullPath": "\\d\\x"},
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": ""},
    )

    result = await template_assign_or_remove(entry_id=42)
    assert result == {"id": 42, "templateName": ""}


@pytest.mark.asyncio
async def test_template_update_rejects_fields_on_remove(
    patched_client: LaserficheClient,
    _writes_on: None,
) -> None:
    result = await template_assign_or_remove(
        entry_id=42,
        template_name=None,
        fields={"X": ["1"]},
    )
    assert result["mode"] == "error"
    assert result["error"] == "fields_ignored_on_remove"


# --- task_wait_or_poll ------------------------------------------------------


@pytest.mark.asyncio
async def test_task_update_zero_timeout_calls_get_status_once(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/tok-1",
        json={"status": "InProgress", "percentComplete": 30},
    )

    result = await task_wait_or_poll(operation_token="tok-1", timeout_seconds=0)
    assert result == {"status": "InProgress", "percentComplete": 30}


@pytest.mark.asyncio
async def test_task_update_positive_timeout_polls_until_terminal(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/tok-2",
        json={"status": "Completed", "percentComplete": 100},
        is_reusable=True,
    )

    result = await task_wait_or_poll(
        operation_token="tok-2",
        timeout_seconds=5,
        poll_interval_seconds=0.1,
    )
    assert result["status"] == "Completed"
    assert result["timed_out"] is False
