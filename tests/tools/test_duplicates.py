"""Tests for ``tools/duplicates.py`` — the ``find_duplicate_documents`` MCP tool.

The ops-layer size-then-hash logic itself is covered exhaustively in
``tests/ops/test_duplicates.py`` against a fake client. These tests instead
cover the MCP wrapper's own responsibilities: resolving/validating the
folder, driving the real walk-then-hash flow against the (mocked) HTTP
client, and shaping the response and error paths.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

_CHILDREN_URL = f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0"


def _edoc_url(entry_id: int) -> str:
    return f"{_BASE}/Entries/{entry_id}/Laserfiche.Repository.Document/edoc"


@pytest.mark.asyncio
async def test_happy_path_groups_duplicates_and_leaves_unique_size_alone(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1",
        json={"id": 1, "name": "Docs", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="GET",
        url=_CHILDREN_URL,
        json={
            "value": [
                {"id": 10, "name": "a.pdf", "entryType": "Document"},
                {"id": 11, "name": "b.pdf", "entryType": "Document"},
                {"id": 12, "name": "c.pdf", "entryType": "Document"},
            ]
        },
    )
    # 10 and 11 are byte-identical (same size -> probed, then both hashed).
    for entry_id in (10, 11):
        httpx_mock.add_response(method="GET", url=_edoc_url(entry_id), content=b"same bytes")
        httpx_mock.add_response(method="GET", url=_edoc_url(entry_id), content=b"same bytes")
    # 12 has a unique size, so it's only ever size-probed, never downloaded.
    httpx_mock.add_response(method="GET", url=_edoc_url(12), content=b"a totally different length")

    result = await server.find_duplicate_documents(folder_id=1)

    assert result["mode"] == "duplicate_report"
    assert result["folder_id"] == 1
    assert result["walk_truncated"] is False
    assert result["documents_examined"] == 3
    assert result["documents_hashed"] == 2
    assert result["bytes_downloaded"] == len(b"same bytes") * 2
    assert result["total_wasted_bytes"] == len(b"same bytes")
    assert result["skipped"] == []
    assert result["folders_unreadable"] == []

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["byte_size"] == len(b"same bytes")
    assert group["wasted_bytes"] == len(b"same bytes")
    assert {e["entry_id"] for e in group["entries"]} == {10, 11}


@pytest.mark.asyncio
async def test_no_duplicates_returns_empty_groups(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1",
        json={"id": 1, "name": "Docs", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="GET",
        url=_CHILDREN_URL,
        json={"value": [{"id": 10, "name": "a.pdf", "entryType": "Document"}]},
    )
    # Every document is size-probed (pass 1), but a lone document has no size
    # collision, so pass 2 never downloads it.
    httpx_mock.add_response(method="GET", url=_edoc_url(10), content=b"solo")

    result = await server.find_duplicate_documents(folder_id=1)

    assert result["groups"] == []
    assert result["documents_hashed"] == 0


@pytest.mark.asyncio
async def test_default_max_bytes_falls_back_to_edoc_max_bytes_setting(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1",
        json={"id": 1, "name": "Docs", "entryType": "Folder"},
    )
    httpx_mock.add_response(method="GET", url=_CHILDREN_URL, json={"value": []})

    result = await server.find_duplicate_documents(folder_id=1)

    from laserfiche_mcp._app import get_settings

    assert result["max_bytes"] == get_settings().edoc_max_bytes


@pytest.mark.asyncio
async def test_max_bytes_override_is_forwarded_and_skips_oversized_docs(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1",
        json={"id": 1, "name": "Docs", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="GET",
        url=_CHILDREN_URL,
        json={"value": [{"id": 10, "name": "big.pdf", "entryType": "Document"}]},
    )
    httpx_mock.add_response(
        method="GET", url=_edoc_url(10), content=b"x" * 5000, headers={"Content-Length": "5000"}
    )

    result = await server.find_duplicate_documents(folder_id=1, max_bytes=100)

    assert result["max_bytes"] == 100
    assert result["skipped"]
    assert result["skipped"][0]["entry_id"] == 10
    assert "max_bytes" in result["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_non_folder_entry_is_rejected_without_walking(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/5",
        json={"id": 5, "name": "file.pdf", "entryType": "Document"},
    )

    result = await server.find_duplicate_documents(folder_id=5)

    assert result["mode"] == "error"
    assert result["error"] == "not_a_folder"
    assert result["entry_id"] == 5
    # No children/edoc requests should have been made.
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_bad_folder_id_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/999", status_code=404)

    result = await server.find_duplicate_documents(folder_id=999)

    assert result["mode"] == "error"
    assert result["operation"] == "find_duplicate_documents"
    assert result["entry_id"] == 999
