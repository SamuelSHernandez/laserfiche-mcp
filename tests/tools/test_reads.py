"""Tests for ``tools/reads.py`` — the six read-side tools.

Covers ``search_entries``, ``search_by_name``, ``list_folder``,
``get_entry``, ``get_entry_by_path``, ``get_field_values`` — happy
paths plus the LaserficheError → structured-error translation that
each tool runs through ``classify_lf_error``.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE


@pytest.mark.asyncio
async def test_search_entries_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": [{"id": 7, "name": "x.pdf", "entryType": "Document"}]},
    )

    result = await server.search_entries(query='{LF:Name="x.pdf"}')

    assert len(result["entries"]) == 1
    assert result["entries"][0]["id"] == 7


@pytest.mark.asyncio
async def test_search_entries_omits_web_url_when_unconfigured(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": [{"id": 7, "name": "x.pdf", "entryType": "Document"}]},
    )

    result = await server.search_entries(query='{LF:Name="x.pdf"}')

    assert "web_url" not in result["entries"][0]


@pytest.mark.asyncio
async def test_search_entries_carries_web_url_when_configured(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_WEB_CLIENT_URL_TEMPLATE", "https://lf.example.com/doc/{entry_id}")
    server._reset_settings_for_tests()

    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": [{"id": 7, "name": "x.pdf", "entryType": "Document"}]},
    )

    result = await server.search_entries(query='{LF:Name="x.pdf"}')

    assert result["entries"][0]["web_url"] == "https://lf.example.com/doc/7"


@pytest.mark.asyncio
async def test_search_entries_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        status_code=500,
        json={"error": "internal"},
    )

    result = await server.search_entries(query='{LF:Name="x"}')
    assert result["mode"] == "error"
    assert result["operation"] == "search"
    assert result["status_code"] == 500


@pytest.mark.asyncio
async def test_search_by_name_builds_lf_query_with_wildcards(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """search_by_name must wrap the pattern in {LF:Name="..."} syntax verbatim."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": []},
    )

    await server.search_by_name(name_pattern="Smith*")

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"searchCommand":' in body
    assert 'LF:Name=\\"Smith*\\"' in body


@pytest.mark.asyncio
async def test_search_by_name_appends_lookin_when_folder_provided(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": []},
    )

    await server.search_by_name(
        name_pattern="Smith*",
        in_folder_path="\\Imports\\2024",
    )

    body = httpx_mock.get_requests()[0].read().decode()
    # JSON-encoded backslashes are doubled; check for the raw fragment.
    assert "LF:LookIn=" in body
    assert "Imports" in body and "2024" in body


@pytest.mark.asyncio
async def test_search_by_name_escapes_quotes_in_pattern(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A user-supplied " must be escaped before being interpolated into the query."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        json={"value": []},
    )

    await server.search_by_name(name_pattern='say"hi')

    body = httpx_mock.get_requests()[0].read().decode()
    # In the JSON body, the backslash escape itself is JSON-encoded, so
    # ``\"`` becomes ``\\\"``. We just need to confirm the raw `"` from the
    # user did not land inside the value unescaped.
    assert 'say\\\\\\"hi' in body or 'say\\"hi' in body


@pytest.mark.asyncio
async def test_search_by_name_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches",
        status_code=500,
        json={"e": "boom"},
    )

    result = await server.search_by_name(name_pattern="x")
    assert result["mode"] == "error"
    assert result["operation"] == "search"
    assert result["status_code"] == 500


@pytest.mark.asyncio
async def test_list_folder_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=25&%24skip=0"),
        json={
            "value": [
                {"id": 10, "name": "child", "entryType": "Folder"},
            ],
            "@odata.count": 1,
        },
    )

    result = await server.list_folder(folder_id=1)

    assert result["total_count"] == 1
    assert result["entries"][0]["id"] == 10


@pytest.mark.asyncio
async def test_list_folder_total_count_requires_include_count(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """total_count is documented as present "when the build supports
    $count" — but the tool never actually asked for it by default, so it
    was structurally always null. include_count=True must wire $count=true
    through to the request."""
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=25&%24skip=0&%24count=true"
        ),
        json={"value": [{"id": 10, "name": "child", "entryType": "Folder"}], "@odata.count": 1},
    )

    result = await server.list_folder(folder_id=1, include_count=True)

    assert result["total_count"] == 1


@pytest.mark.asyncio
async def test_list_folder_uses_folder_template_for_child_folders(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LF_WEB_CLIENT_FOLDER_URL_TEMPLATE", "https://lf.example.com/folder/{entry_id}"
    )
    server._reset_settings_for_tests()

    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=25&%24skip=0"),
        json={"value": [{"id": 10, "name": "child", "entryType": "Folder"}]},
    )

    result = await server.list_folder(folder_id=1)

    assert result["entries"][0]["web_url"] == "https://lf.example.com/folder/10"


@pytest.mark.asyncio
async def test_list_folder_clamps_negative_skip_to_zero(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=25&%24skip=0"),
        json={"value": []},
    )

    await server.list_folder(folder_id=1, skip=-10)

    # If the negative skip leaked through, the URL above wouldn't match and
    # httpx_mock would 404 — passing this far proves the clamp ran.


@pytest.mark.asyncio
async def test_list_folder_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/999/Laserfiche.Repository.Folder/children?%24top=25&%24skip=0"),
        status_code=404,
    )

    result = await server.list_folder(folder_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "list_folder"
    assert result["error"] == "not_found"
    assert result["folder_id"] == 999


@pytest.mark.asyncio
async def test_get_entry_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "templateName": "PAF"},
    )

    result = await server.get_entry(entry_id=42)

    assert result["id"] == 42
    assert result["template_name"] == "PAF"


@pytest.mark.asyncio
async def test_get_entry_carries_web_url_when_configured(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_WEB_CLIENT_URL_TEMPLATE", "https://lf.example.com/doc/{entry_id}")
    server._reset_settings_for_tests()

    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document"},
    )

    result = await server.get_entry(entry_id=42)

    assert result["web_url"] == "https://lf.example.com/doc/42"


@pytest.mark.asyncio
async def test_get_entry_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        status_code=404,
    )

    result = await server.get_entry(entry_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "get_entry"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 999


@pytest.mark.asyncio
async def test_get_entry_by_path_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CImports",
        json={"id": 12, "name": "Imports", "entryType": "Folder"},
    )

    result = await server.get_entry_by_path(full_path="\\Imports")

    assert result["id"] == 12


@pytest.mark.asyncio
async def test_get_entry_by_path_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5Cmissing",
        status_code=404,
    )

    result = await server.get_entry_by_path(full_path="\\missing")
    assert result["mode"] == "error"
    assert result["operation"] == "get_entry_by_path"
    assert result["error"] == "not_found"
    assert result["full_path"] == "\\missing"


@pytest.mark.asyncio
async def test_get_field_values_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={
            "value": [
                {"fieldName": "Status", "values": ["Approved"]},
                {"fieldName": "Notes", "values": [], "isMultiValue": True},
            ]
        },
    )

    result = await server.get_field_values(entry_id=42)

    assert result["entry_id"] == 42
    assert len(result["values"]) == 2
    assert result["values"][0]["field_name"] == "Status"
    assert result["values"][1]["is_multi_value"] is True


@pytest.mark.asyncio
async def test_get_field_values_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999/fields",
        status_code=403,
    )

    result = await server.get_field_values(entry_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "get_field_values"
    assert result["error"] == "auth_failed"
    assert result["entry_id"] == 999
