"""Tests for ``tools/definitions.py`` — repository schema listings + audit reasons.

Covers ``list_field_definitions``, ``list_tag_definitions``,
``list_template_definitions``, ``list_link_definitions``,
``list_repositories``, ``get_template_fields``, ``get_audit_reasons``,
plus the ``summary_only`` shortcut on each ``list_*_definitions`` tool.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- get_audit_reasons -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_audit_reasons(httpx_mock: HTTPXMock, patched_client: LaserficheClient) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/AuditReasons",
        json={"deleteEntry": [{"id": 1, "name": "Records purge"}]},
    )
    result = await server.get_audit_reasons()
    assert result["deleteEntry"][0]["name"] == "Records purge"


# --- list_*_definitions ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_field_definitions_tool(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "Last Name"}]},
    )
    result = await server.list_field_definitions()
    assert len(result["value"]) == 1


@pytest.mark.asyncio
async def test_list_tag_definitions_tool(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_tag_definitions()


@pytest.mark.asyncio
async def test_list_template_definitions_tool(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_template_definitions()


@pytest.mark.asyncio
async def test_list_link_definitions_tool(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_link_definitions()


# --- list_repositories: happy path + endpoint-disabled fallback -------------


@pytest.mark.asyncio
async def test_list_repositories_tool(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        json={"value": [{"repoId": "demo"}]},
    )
    await server.list_repositories()


@pytest.mark.asyncio
async def test_list_repositories_falls_back_on_server_error(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """On endpoint failure, surface the configured repo as a single-item fallback."""
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        status_code=400,
        json={"errorCode": 216, "title": "Endpoint disabled on this build"},
    )

    result = await server.list_repositories()

    assert result["mode"] == "fallback"
    assert result["operation"] == "list_repositories"
    assert result["value"][0]["repoId"] == "demo"  # from test settings
    assert result["value"][0]["is_configured"] is True
    assert result["server_error"]["status_code"] == 400


# --- get_template_fields -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_template_fields_returns_template_metadata(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {
                    "id": 2,
                    "name": "Missionary Document",
                    "templateFieldNames": ["Last Name", "Status"],
                },
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {
                    "id": 16,
                    "name": "Last Name",
                    "fieldType": "String",
                    "isRequired": False,
                    "isMultiValue": False,
                    "listValues": [],
                },
                {
                    "id": 50,
                    "name": "Status",
                    "fieldType": "List",
                    "isRequired": True,
                    "isMultiValue": False,
                    "listValues": ["Pending", "Approved"],
                },
            ]
        },
    )
    result = await server.get_template_fields(template_name="Missionary Document")
    assert result["template_name"] == "Missionary Document"
    assert result["template_id"] == 2
    assert result["field_count"] == 2
    field_names = {f["name"] for f in result["fields"]}
    assert field_names == {"Last Name", "Status"}


@pytest.mark.asyncio
async def test_get_template_fields_required_only_filters(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {"id": 2, "name": "T", "templateFieldNames": ["A", "B"]},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {"id": 1, "name": "A", "fieldType": "String", "isRequired": True},
                {"id": 2, "name": "B", "fieldType": "String", "isRequired": False},
            ]
        },
    )
    result = await server.get_template_fields(template_name="T", required_only=True)
    assert result["field_count"] == 1
    assert result["fields"][0]["name"] == "A"
    assert result["fields"][0]["is_required"] is True


@pytest.mark.asyncio
async def test_get_template_fields_returns_error_on_unknown_template(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Personnel"}]},
    )
    result = await server.get_template_fields(template_name="DoesNotExist")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_template_name"
    assert "Personnel" in result["valid_template_names"]


# --- summary_only on every list_*_definitions tool --------------------------


@pytest.mark.asyncio
async def test_list_field_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
    )
    result = await server.list_field_definitions(summary_only=True)
    assert result == {"count": 2, "names": ["A", "B"]}


@pytest.mark.asyncio
async def test_list_tag_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "Confidential"}]},
    )
    result = await server.list_tag_definitions(summary_only=True)
    assert result == {"count": 1, "names": ["Confidential"]}


@pytest.mark.asyncio
async def test_list_template_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "T1"}, {"id": 2, "name": "T2"}]},
    )
    result = await server.list_template_definitions(summary_only=True)
    assert result["count"] == 2
    assert set(result["names"]) == {"T1", "T2"}


@pytest.mark.asyncio
async def test_list_link_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=25&%24skip=0",
        json={
            "value": [
                {"linkTypeId": 1, "sourceLabel": "S1"},
                {"linkTypeId": 2, "sourceLabel": "S2"},
            ]
        },
    )
    # link defs don't have a 'name'; the summary helper falls back to displayName,
    # which is also absent. Names list will be empty.
    result = await server.list_link_definitions(summary_only=True)
    assert result["count"] == 2
    # The link definition has sourceLabel, not name — empty names list is acceptable.
