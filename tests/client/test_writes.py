"""Tests for ``client/_writes.py`` — state mutations (patch, delete, create, copy, import, put*)."""

from __future__ import annotations

import json as _json

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE

# --- patch_entry ------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_entry_sends_only_provided_fields(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "name": "New", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        await client.patch_entry(42, name="New")

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"name"' in body
    assert '"parentId"' not in body
    assert '"templateName"' not in body


@pytest.mark.asyncio
async def test_patch_entry_supports_move_and_rename(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=true",
        json={"id": 42, "name": "X", "parentId": 100},
    )

    async with _build_client(settings) as client:
        await client.patch_entry(42, parent_id=100, name="X", auto_rename=True)

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"parentId":100' in body or '"parentId": 100' in body


# --- delete_entry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_returns_accepted_operation(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-abc", "taskId": "task-1"},
    )

    async with _build_client(settings) as client:
        result = await client.delete_entry(
            42,
            audit_reason_id=5,
            comment="cleanup",
        )

    assert result["token"] == "op-abc"
    body = httpx_mock.get_requests()[0].read().decode()
    assert '"auditReasonId":5' in body or '"auditReasonId": 5' in body
    assert "cleanup" in body


@pytest.mark.asyncio
async def test_delete_entry_omits_body_when_no_audit_reason(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-abc"},
    )

    async with _build_client(settings) as client:
        await client.delete_entry(42)

    # Either no body, or an empty/`null` body — neither should contain audit fields.
    body = httpx_mock.get_requests()[0].read().decode()
    assert "auditReasonId" not in body
    assert "comment" not in body


# --- create_child_entry / import_document ----------------------------------


@pytest.mark.asyncio
async def test_create_child_entry_posts_folder_route(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?autoRename=false",
        json={"id": 999, "name": "New", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        await client.create_child_entry(
            100,
            entry_type="Folder",
            name="New",
        )

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"entryType":"Folder"' in body or '"entryType": "Folder"' in body


@pytest.mark.asyncio
async def test_import_document_posts_multipart(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Foo.pdf?autoRename=false",
        status_code=201,
        json={"entryCreate": {"entryId": 42}},
    )

    async with _build_client(settings) as client:
        await client.import_document(
            100,
            "Foo.pdf",
            b"%PDF-1.4 fake",
            content_type="application/pdf",
        )

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    content_type = request.headers["Content-Type"]
    assert content_type.startswith("multipart/form-data")
    raw = request.read()
    # The file part appears with its filename.
    assert b"Foo.pdf" in raw
    assert b"%PDF-1.4 fake" in raw


@pytest.mark.asyncio
async def test_import_document_url_encodes_special_chars(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        # Space becomes %20 in path
        url=f"{_BASE}/Entries/100/My%20File.pdf?autoRename=false",
        status_code=201,
        json={},
    )

    async with _build_client(settings) as client:
        await client.import_document(100, "My File.pdf", b"x")


# --- put_fields / put_tags / put_links --------------------------------------


@pytest.mark.asyncio
async def test_put_fields_v1_sends_flat_dict(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )

    async with _build_client(settings) as client:
        await client.put_fields(42, {"Last Name": {"values": [{"value": "Smith"}]}})

    # v1 schema (PostAssignFieldValues) is FieldsToUpdate, a flat
    # {FieldName: FieldToUpdate} object with no wrapping "fields" key.
    body = _json.loads(httpx_mock.get_requests()[0].read())
    assert "fields" not in body
    assert body == {"Last Name": {"values": [{"value": "Smith"}]}}


@pytest.mark.asyncio
async def test_put_tags_wraps_in_tags_key(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": []},
    )

    async with _build_client(settings) as client:
        await client.put_tags(42, ["urgent", "review"])

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"tags"' in body
    assert "urgent" in body


@pytest.mark.asyncio
async def test_put_links_sends_bare_array(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    """PutLinksRequest is documented as a bare array, not wrapped."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/links",
        json={"value": []},
    )

    async with _build_client(settings) as client:
        await client.put_links(42, [{"targetId": 7, "linkTypeId": 1}])

    body = httpx_mock.get_requests()[0].read().decode().strip()
    assert body.startswith("[")
    assert "targetId" in body


# --- assign_template / remove_template --------------------------------------


@pytest.mark.asyncio
async def test_assign_template(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "Personnel"},
    )

    async with _build_client(settings) as client:
        await client.assign_template(42, "Personnel")

    body = httpx_mock.get_requests()[0].read().decode()
    assert "Personnel" in body


@pytest.mark.asyncio
async def test_remove_template(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42},
    )

    async with _build_client(settings) as client:
        await client.remove_template(42)


# --- delete_edoc / delete_pages ---------------------------------------------


@pytest.mark.asyncio
async def test_delete_edoc(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        json={"value": True},
    )

    async with _build_client(settings) as client:
        result = await client.delete_edoc(42)

    assert result["value"] is True


@pytest.mark.asyncio
async def test_delete_pages_requires_page_range(
    lf_env: dict[str, str],
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.delete_pages(42, "")
    assert "page_range" in str(exc_info.value)


@pytest.mark.asyncio
async def test_delete_pages_with_range(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/pages?pageRange=1-3",
        json={"value": True},
    )

    async with _build_client(settings) as client:
        await client.delete_pages(42, "1-3")
