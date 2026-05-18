"""Tests for ``tools/writes_templates.py`` — assign/remove template + required-field validation."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE


@pytest.mark.asyncio
async def test_assign_and_remove_template(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # Skip the required-field validation in this test — it's exercising the
    # assign/remove pair, not the validator (which has its own dedicated tests).
    monkeypatch.setattr(server._get_settings(), "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "Personnel"},
    )
    await server.assign_template(42, "Personnel", fields={"Last Name": ["Smith"]})

    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42},
    )
    await server.remove_template(42)


# --- validate_required_fields pre-flight checks -----------------------------


@pytest.mark.asyncio
async def test_assign_template_blocks_when_required_field_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Validator returns mode:error when a repo-wide required field is unset."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {
                    "name": "Type of Document",
                    "fieldType": "List",
                    "isRequired": True,
                    "listValues": ["Digital", "Original"],
                    "defaultValue": "Digital",
                },
                {
                    "name": "Last Name",
                    "fieldType": "String",
                    "isRequired": False,
                    "listValues": [],
                    "defaultValue": None,
                },
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},  # no fields currently set on the entry
    )

    result = await server.assign_template(42, "Personnel")

    assert result["mode"] == "error"
    assert result["error"] == "missing_required_fields"
    assert result["missing"] == ["Type of Document"]
    assert result["field_details"][0]["list_values"] == ["Digital", "Original"]


@pytest.mark.asyncio
async def test_assign_template_validation_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """With LF_VALIDATE_REQUIRED_FIELDS=false the validator is skipped."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    # No FieldDefinitions or fields mocks — they must not be called.
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "Personnel"},
    )
    result = await server.assign_template(42, "Personnel")
    assert "mode" not in result or result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_passes_when_required_field_already_set(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If every required field is already on the entry, validator returns None
    and the real PUT runs."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {
                    "name": "Type of Document",
                    "fieldType": "List",
                    "isRequired": True,
                    "listValues": ["Digital"],
                },
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={
            "value": [
                {"fieldName": "Type of Document", "values": [{"value": "Digital"}]},
            ]
        },
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(42, "T")
    assert result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_accepts_required_field_via_caller_fields(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If the caller supplies the required field in ``fields=``, validator
    accepts it even though it's not yet on the entry."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {
                    "name": "Type of Document",
                    "fieldType": "List",
                    "isRequired": True,
                    "listValues": ["Digital"],
                },
                {
                    "name": "Doc Classification",
                    "fieldType": "List",
                    "isRequired": True,
                    "listValues": [" "],
                },
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(
        42,
        "T",
        fields={"Type of Document": ["Digital"], "Doc Classification": [" "]},
    )
    assert result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_flags_only_unsupplied_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """When the caller supplies one of two missing required fields, only the
    unsupplied one is reported."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {"name": "A", "fieldType": "String", "isRequired": True, "listValues": []},
                {"name": "B", "fieldType": "String", "isRequired": True, "listValues": []},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    result = await server.assign_template(42, "T", fields={"A": ["x"]})
    assert result["mode"] == "error"
    assert result["missing"] == ["B"]


@pytest.mark.asyncio
async def test_assign_template_validation_falls_through_on_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If list_field_definitions fails, validator returns None and the real
    PUT runs (server-side error path is what surfaces)."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=200&%24skip=0",
        status_code=500,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(42, "T")
    assert result.get("mode") != "error"


# --- classify_lf_error wrap on upstream failures ----------------------------


@pytest.mark.asyncio
async def test_assign_template_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "fullPath": "\\X"},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        status_code=500,
    )
    result = await server.assign_template(42, "T")
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_remove_template_classifies_upstream_error(
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
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        status_code=500,
    )
    result = await server.remove_template(42)
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


# --- LF_VALIDATE_NAMES=true rejection ---------------------------------------


@pytest.mark.asyncio
async def test_assign_template_rejects_unknown_template(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    monkeypatch.setattr(settings, "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "Personnel"}]},
        is_reusable=True,
    )
    result = await server.assign_template(
        entry_id=42,
        template_name="DoesNotExist",
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_template_name"
    assert result["template_name"] == "DoesNotExist"
