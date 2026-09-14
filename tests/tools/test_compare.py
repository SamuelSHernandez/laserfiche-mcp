"""Tests for ``tools/compare.py`` — the ``compare_entries`` MCP tool.

The set-comparison logic itself is covered in ``tests/ops/test_compare.py``.
These tests cover the MCP wrapper: fetching both entries (and optionally
both field-value listings) against the real HTTP client, response shaping,
and the LaserficheError -> structured-error translation on either side.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE


def _entry(entry_id: int, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": entry_id,
        "name": "a.pdf",
        "entryType": "Document",
        "templateName": "Invoice",
        "extension": "pdf",
        "pageCount": 2,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_reports_attribute_and_field_differences(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1, pageCount=2))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2, pageCount=3))
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/fields",
        json={"value": [{"fieldName": "Status", "values": ["Open"]}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/2/fields",
        json={"value": [{"fieldName": "Status", "values": ["Closed"]}]},
    )

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["mode"] == "entry_comparison"
    assert result["left_entry_id"] == 1
    assert result["right_entry_id"] == 2
    assert result["identical"] is False

    by_name = {d["name"]: d for d in result["differences"]}
    assert by_name["pageCount"]["kind"] == "attribute"
    assert by_name["pageCount"]["left"] == 2
    assert by_name["pageCount"]["right"] == 3
    assert by_name["Status"]["kind"] == "field"
    assert by_name["Status"]["left"] == ["Open"]
    assert by_name["Status"]["right"] == ["Closed"]


@pytest.mark.asyncio
async def test_identical_entries_report_no_differences(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2))
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/fields",
        json={"value": [{"fieldName": "Status", "values": ["Open"]}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/2/fields",
        json={"value": [{"fieldName": "Status", "values": ["Open"]}]},
    )

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["identical"] is True
    assert result["differences"] == []


@pytest.mark.asyncio
async def test_only_left_and_only_right_fields_are_reported_separately(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2))
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/fields",
        json={"value": [{"fieldName": "OnlyLeft", "values": ["x"]}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/2/fields",
        json={"value": [{"fieldName": "OnlyRight", "values": ["y"]}]},
    )

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["only_left_fields"] == ["OnlyLeft"]
    assert result["only_right_fields"] == ["OnlyRight"]


@pytest.mark.asyncio
async def test_include_fields_false_skips_the_field_value_requests(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2))

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2, include_fields=False)

    assert result["identical"] is True
    assert len(httpx_mock.get_requests()) == 2
    assert not any("fields" in r.url.path for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_left_lookup_failure_is_classified_with_side(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", status_code=404)

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["mode"] == "error"
    assert result["operation"] == "compare_entries"
    assert result["entry_id"] == 1
    assert result["side"] == "left"


@pytest.mark.asyncio
async def test_right_lookup_failure_is_classified_with_side(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", status_code=404)

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["mode"] == "error"
    assert result["entry_id"] == 2
    assert result["side"] == "right"


@pytest.mark.asyncio
async def test_left_field_lookup_failure_is_classified_with_side_and_stage(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1/fields", status_code=500)

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["mode"] == "error"
    assert result["entry_id"] == 1
    assert result["side"] == "left"
    assert result["stage"] == "fields"


@pytest.mark.asyncio
async def test_right_field_lookup_failure_is_classified_with_side_and_stage(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2))
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/fields",
        json={"value": []},
    )
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2/fields", status_code=500)

    result = await server.compare_entries(left_entry_id=1, right_entry_id=2)

    assert result["mode"] == "error"
    assert result["entry_id"] == 2
    assert result["side"] == "right"
    assert result["stage"] == "fields"
