"""Tests for ``tools/writes_fields_tags_links.py``.

Covers ``set_fields``, ``merge_fields``, ``set_tags``, ``merge_tags``,
``set_links`` — happy paths, the merge_fields preservation invariant,
classifies-upstream-error wrap, and the schema-validator rejections
that fire when ``LF_VALIDATE_NAMES=true``.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- happy paths -------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_fields_happy_path(
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
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    await server.set_fields(42, {"Note": ["new"]})


@pytest.mark.asyncio
async def test_set_tags_happy_path(
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
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": []},
    )
    await server.set_tags(42, ["urgent"])


@pytest.mark.asyncio
async def test_merge_tags_add_and_remove(
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
        url=f"{_BASE}/Entries/42/tags",
        json={"value": [{"name": "old"}, {"name": "keep"}]},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": []},
    )
    result = await server.merge_tags(42, add=["new"], remove=["old"])
    assert result["mode"] == "executed"
    assert "new" in result["added"]
    assert "old" in result["removed"]
    assert sorted(result["final_tags"]) == ["keep", "new"]


@pytest.mark.asyncio
async def test_set_links_happy_path(
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
        method="PUT",
        url=f"{_BASE}/Entries/42/links",
        json={"value": []},
    )
    await server.set_links(42, [{"targetId": 7, "linkTypeId": 1}])


# --- merge_fields preservation invariant ------------------------------------


@pytest.mark.asyncio
async def test_merge_fields_preserves_unmentioned(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(
        server._get_settings(),
        "read_only",
        False,
    )
    # Entry fetch for the path-scope check
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    # Current fields on the entry
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={
            "value": [
                {
                    "fieldName": "Last Name",
                    "values": [{"value": "Smith", "position": 0}],
                },
                {
                    "fieldName": "Note",
                    "values": [{"value": "old note", "position": 0}],
                },
            ]
        },
    )
    # PUT response
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )

    result = await server.merge_fields(42, {"Note": ["new note"]})
    assert result["mode"] == "executed"
    assert result["fields_updated"] == ["Note"]
    assert "Last Name" in result["fields_preserved"]

    # Confirm the PUT body kept "Last Name" intact (request #2 is the PUT;
    # request #0 is the entry GET added by the path-scope check).
    put_body = httpx_mock.get_requests()[2].read().decode()
    assert "Last Name" in put_body
    assert "Smith" in put_body
    assert "new note" in put_body


# --- classify_lf_error wrap on upstream failures ----------------------------


@pytest.mark.asyncio
async def test_set_fields_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        status_code=500,
        json={"title": "boom"},
    )
    result = await server.set_fields(42, {"Name": ["v"]})
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_merge_fields_classifies_upstream_error_on_get(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If the GET of current fields fails, the error surfaces through classify_lf_error."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        status_code=401,
    )
    result = await server.merge_fields(42, {"Name": ["v"]})
    assert result["mode"] == "error"
    assert result["error"] == "auth_failed"


@pytest.mark.asyncio
async def test_set_tags_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        status_code=403,
    )
    result = await server.set_tags(42, [])
    assert result["mode"] == "error"
    assert result["error"] == "auth_failed"


@pytest.mark.asyncio
async def test_merge_tags_classifies_upstream_error_on_get(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/tags",
        status_code=500,
    )
    result = await server.merge_tags(42, add=["t1"])
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_set_links_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/links",
        status_code=500,
    )
    result = await server.set_links(42, [])
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


# --- LF_VALIDATE_NAMES=true rejections --------------------------------------


@pytest.mark.asyncio
async def test_set_fields_rejects_unknown_field_name(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "Status"}]},
        is_reusable=True,
    )
    # path-fence fetch
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    result = await server.set_fields(entry_id=42, fields={"NoSuchField": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "invalid_field_name"
    assert "NoSuchField" in result["invalid_field_names"]


@pytest.mark.asyncio
async def test_set_tags_rejects_unknown_tag(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "Confidential"}]},
        is_reusable=True,
    )
    result = await server.set_tags(entry_id=42, tags=["Unknown"])
    assert result["mode"] == "error"
    assert result["error"] == "invalid_tag_name"


@pytest.mark.asyncio
async def test_set_links_rejects_unknown_link_type(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=200&%24skip=0",
        json={"value": [{"linkTypeId": 1, "sourceLabel": "Supersedes"}]},
        is_reusable=True,
    )
    result = await server.set_links(
        entry_id=42,
        links=[{"targetId": 99, "linkTypeId": 999}],
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_link_type"


@pytest.mark.asyncio
async def test_validators_skip_when_lf_validate_names_false(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """With LF_VALIDATE_NAMES=false the schema endpoints are NOT hit."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # NB: no schema-endpoint mocks registered. If a validator runs, the
    # request will fail.
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    # Will pass because validate_names=false (test conftest default)
    result = await server.set_fields(entry_id=42, fields={"AnyField": ["x"]})
    # The set_fields call succeeds end-to-end; no schema-cache request made.
    assert result.get("mode") != "error" or result.get("error") != "invalid_field_name"
