"""Tests for LaserficheClient using pytest-httpx mocks.

Endpoint paths verified against the official ``Laserfiche/lf-repository-api-client-java``
client (``EntriesClientImpl.java`` and ``SimpleSearchesClientImpl.java``).
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient, LaserficheError, build_repo_path
from laserfiche_mcp.config import ApiVersion, Settings

_BASE_V1 = "https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo"
_BASE_V2 = "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo"
# Tests default to v1 (matches conftest default and the production default).
_BASE = _BASE_V1


class _StubAuth(AuthStrategy):
    """Bypasses the /Token roundtrip so client tests stay focused."""

    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer test-token"


def _build_client(settings: Settings) -> LaserficheClient:
    return LaserficheClient(settings, _StubAuth())


# --- build_repo_path ---------------------------------------------------------


@pytest.mark.parametrize(
    "base, repo, suffix, version, expected",
    [
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI/",
            "demo",
            "Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "/Entries/42",
            ApiVersion.V1,
            "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42",
        ),
        (
            "https://lf.test/LFRepositoryAPI",
            "demo",
            "Entries/42",
            ApiVersion.V2,
            "https://lf.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42",
        ),
    ],
)
def test_build_repo_path(
    base: str, repo: str, suffix: str, version: ApiVersion, expected: str,
) -> None:
    assert build_repo_path(base, repo, suffix, version) == expected


def test_build_repo_path_defaults_to_v1() -> None:
    """Production default is v1 — confirm callers that don't pass a version get v1."""
    assert build_repo_path(
        "https://lf.test/LFRepositoryAPI", "demo", "Entries/42"
    ) == "https://lf.test/LFRepositoryAPI/v1/Repositories/demo/Entries/42"


# --- get_entry --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_uses_canonical_path(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Smith,John", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer test-token"


# --- get_entry_by_path ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_by_path_passes_full_path(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CImports%5C2024",
        json={"id": 99, "name": "2024", "entryType": "Folder"},
    )

    async with _build_client(settings) as client:
        result = await client.get_entry_by_path("\\Imports\\2024")

    assert result["id"] == 99


# --- list_folder ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folder_v1_uses_odata_entity_type_segment(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1: path is /Entries/{id}/Laserfiche.Repository.Folder/children (lowercase)."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE_V1}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=20"
        ),
        json={"value": [], "@odata.count": 100},
    )

    async with _build_client(settings) as client:
        result = await client.list_folder(1, max_results=10, skip=20)

    assert result["@odata.count"] == 100


@pytest.mark.asyncio
async def test_list_folder_v2_uses_folder_children_path(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: path is /Entries/{id}/Folder/Children (PascalCase), NOT /Entries/{id}/Children."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Entries/1/Folder/Children?%24top=10&%24skip=20",
        json={"value": [], "@odata.count": 100},
    )

    async with _build_client(settings) as client:
        result = await client.list_folder(1, max_results=10, skip=20)

    assert result["@odata.count"] == 100


# --- search_entries ---------------------------------------------------------


@pytest.mark.asyncio
async def test_search_entries_posts_to_simple_searches(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Regression: search is POST /SimpleSearches with JSON body, not GET with query."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=10",
        json={"value": []},
    )

    async with _build_client(settings) as client:
        await client.search_entries('{LF:Name="Smith"}', max_results=10)

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    body = request.read().decode()
    assert '"searchCommand"' in body
    assert "Smith" in body


# --- get_field_values -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_field_values_v1_uses_lowercase_fields(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1: segment is lowercase `fields`."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Entries/42/fields",
        json={"value": [{"fieldName": "Status", "values": ["Approved"]}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_field_values(42)

    assert result["value"][0]["fieldName"] == "Status"


@pytest.mark.asyncio
async def test_get_field_values_v2_uses_pascalcase_fields(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: segment is PascalCase `Fields`."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Entries/42/Fields",
        json={"value": [{"fieldName": "Status", "values": ["Approved"]}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_field_values(42)

    assert result["value"][0]["fieldName"] == "Status"


# --- export_entry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_export_entry_v1_edoc_uses_get_on_document_segment(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """v1 has no /Export — Edoc is fetched via GET on the Document entity-type segment."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"hello world",
    )

    async with _build_client(settings) as client:
        result = await client.export_entry(42, part="Edoc")

    assert result == b"hello world"
    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"


@pytest.mark.asyncio
async def test_export_entry_v1_rejects_text_part(
    lf_env: dict[str, str],
) -> None:
    """v1 has no text-extraction endpoint; asking for it raises a clear error."""
    settings = Settings()  # type: ignore[call-arg]

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="v1 has no endpoint"):
            await client.export_entry(42, part="Text")


@pytest.mark.asyncio
async def test_export_entry_v2_posts_with_part(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2: download uses POST /Export with JSON body {part: 'Edoc'|'Text'}."""
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_V2}/Entries/42/Export",
        content=b"hello world",
    )

    async with _build_client(settings) as client:
        result = await client.export_entry(42, part="Text")

    assert result == b"hello world"
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    body = request.read().decode()
    assert '"part"' in body and '"Text"' in body


@pytest.mark.asyncio
async def test_export_entry_v2_raises_on_404(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_V2}/Entries/999/Export",
        status_code=404,
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.export_entry(999)

    assert exc_info.value.status_code == 404


# --- error handling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_error_response_raises(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999",
        status_code=404,
        json={"error": "Entry not found"},
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.get_entry(999)

    assert exc_info.value.status_code == 404


# --- retry behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_transient_5xx(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "2")
    settings = Settings()  # type: ignore[call-arg]

    url = f"{_BASE}/Entries/42"
    httpx_mock.add_response(method="GET", url=url, status_code=503)
    httpx_mock.add_response(method="GET", url=url, status_code=503)
    httpx_mock.add_response(
        method="GET", url=url,
        json={"id": 42, "name": "x", "entryType": "Folder"},
    )

    import laserfiche_mcp.client as client_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)

    async with _build_client(settings) as client:
        result = await client.get_entry(42)

    assert result["id"] == 42
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_does_not_retry_4xx(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        status_code=403,
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError) as exc_info:
            await client.get_entry(42)

    assert exc_info.value.status_code == 403
    assert len(httpx_mock.get_requests()) == 1


# --- v1.2 write methods -----------------------------------------------------


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
            42, audit_reason_id=5, comment="cleanup",
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
            100, entry_type="Folder", name="New",
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
            100, "Foo.pdf", b"%PDF-1.4 fake",
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


@pytest.mark.asyncio
async def test_put_fields_v1_sends_flat_dict(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
    import json as _json
    body = _json.loads(httpx_mock.get_requests()[0].read())
    assert "fields" not in body
    assert body == {"Last Name": {"values": [{"value": "Smith"}]}}


@pytest.mark.asyncio
async def test_put_tags_wraps_in_tags_key(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
async def test_put_links_sends_bare_array(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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


@pytest.mark.asyncio
async def test_assign_template(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
async def test_remove_template(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42},
    )

    async with _build_client(settings) as client:
        await client.remove_template(42)


@pytest.mark.asyncio
async def test_delete_edoc(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
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
async def test_delete_pages_with_range(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/pages?pageRange=1-3",
        json={"value": True},
    )

    async with _build_client(settings) as client:
        await client.delete_pages(42, "1-3")


@pytest.mark.asyncio
async def test_list_field_definitions(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=100&%24skip=0",
        json={"value": [{"id": 1, "name": "Last Name"}]},
    )

    async with _build_client(settings) as client:
        result = await client.list_field_definitions()
    assert len(result["value"]) == 1


@pytest.mark.asyncio
async def test_list_template_definitions_with_name_filter(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/TemplateDefinitions"
            "?%24top=100&%24skip=0&templateName=Personnel"
        ),
        json={"value": [{"id": 5, "name": "Personnel"}]},
    )

    async with _build_client(settings) as client:
        await client.list_template_definitions(template_name="Personnel")


@pytest.mark.asyncio
async def test_get_audit_reasons(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/AuditReasons",
        json={"deleteEntry": [{"id": 1, "name": "Records purge"}]},
    )

    async with _build_client(settings) as client:
        result = await client.get_audit_reasons()
    assert result["deleteEntry"][0]["id"] == 1


@pytest.mark.asyncio
async def test_get_task_status(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-abc",
        json={"operationToken": "op-abc", "status": "Completed", "percentComplete": 100},
    )

    async with _build_client(settings) as client:
        result = await client.get_task_status("op-abc")
    assert result["status"] == "Completed"


@pytest.mark.asyncio
async def test_list_repositories_uses_top_level_route(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """list_repositories sits ABOVE the /Repositories/{id}/ prefix."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        json={"value": [{"repoId": "demo", "repoName": "Demo"}]},
    )

    async with _build_client(settings) as client:
        result = await client.list_repositories()
    assert result["value"][0]["repoId"] == "demo"


@pytest.mark.asyncio
async def test_list_repositories_normalizes_bare_list_response(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Some LF builds return a bare JSON array; client must wrap it."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        json=[
            {"repoId": "ASTR", "repoName": "Astronomy"},
            {"repoId": "IPRS", "repoName": "Insurance"},
        ],
    )

    async with _build_client(settings) as client:
        result = await client.list_repositories()
    assert isinstance(result, dict)
    assert [r["repoId"] for r in result["value"]] == ["ASTR", "IPRS"]


@pytest.mark.asyncio
async def test_list_repositories_handles_empty_response(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """An empty 200 body is normalized to ``{"value": []}``."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        content=b"",
    )

    async with _build_client(settings) as client:
        result = await client.list_repositories()
    assert result == {"value": []}


# --- Cached schema-definition lookups -----------------------------------------


@pytest.mark.asyncio
async def test_cached_field_definitions_keyed_by_name(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 14, "name": "Employee ID", "fieldType": "String", "isRequired": False},
            {"id": 16, "name": "Last Name", "fieldType": "String", "isRequired": False},
        ]},
    )
    async with _build_client(settings) as client:
        result = await client.cached_field_definitions()
    assert "Employee ID" in result
    assert "Last Name" in result
    assert result["Employee ID"]["fieldType"] == "String"


@pytest.mark.asyncio
async def test_cached_field_definitions_hits_cache_on_second_call(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Second call within TTL doesn't re-hit the server."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    async with _build_client(settings) as client:
        first = await client.cached_field_definitions()
        # No second mock registered — second call must hit the cache.
        second = await client.cached_field_definitions()
    assert first is second


@pytest.mark.asyncio
async def test_invalidate_schema_caches_forces_refresh(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}, {"id": 2, "name": "F2"}]},
    )
    async with _build_client(settings) as client:
        first = await client.cached_field_definitions()
        client.invalidate_schema_caches()
        second = await client.cached_field_definitions()
    assert "F2" not in first
    assert "F2" in second


@pytest.mark.asyncio
async def test_cached_tag_definitions_keyed_by_name(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/TagDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Confidential"}]},
    )
    async with _build_client(settings) as client:
        result = await client.cached_tag_definitions()
    assert "Confidential" in result


@pytest.mark.asyncio
async def test_cached_template_definitions_keyed_by_name(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 2, "name": "Missionary Document", "fieldCount": 14},
        ]},
    )
    async with _build_client(settings) as client:
        result = await client.cached_template_definitions()
    assert "Missionary Document" in result
    assert result["Missionary Document"]["fieldCount"] == 14


@pytest.mark.asyncio
async def test_cached_link_definitions_keyed_by_id(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/LinkDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"linkTypeId": 1, "sourceLabel": "Supersedes"},
            {"linkTypeId": 2, "sourceLabel": "Attachment"},
        ]},
    )
    async with _build_client(settings) as client:
        result = await client.cached_link_definitions()
    assert 1 in result
    assert 2 in result
    assert result[1]["sourceLabel"] == "Supersedes"


@pytest.mark.asyncio
async def test_schema_cache_ttl_zero_means_no_caching(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_SCHEMA_CACHE_TTL_SECONDS", "0")
    settings = Settings()  # type: ignore[call-arg]
    # Two responses queued — both should be consumed (no cache hit).
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    async with _build_client(settings) as client:
        await client.cached_field_definitions()
        await client.cached_field_definitions()
    # If both mocks were consumed, pytest-httpx didn't raise an unmatched-request error.
