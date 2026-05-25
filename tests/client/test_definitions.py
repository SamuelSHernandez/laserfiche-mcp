"""Tests for ``client/_definitions.py`` — list_*_definitions, audit reasons, tasks, caches."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE, _BASE_V1

# --- list_*_definitions -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_field_definitions(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
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
        url=(f"{_BASE}/TemplateDefinitions?%24top=100&%24skip=0&templateName=Personnel"),
        json={"value": [{"id": 5, "name": "Personnel"}]},
    )

    async with _build_client(settings) as client:
        await client.list_template_definitions(template_name="Personnel")


# --- get_audit_reasons / get_task_status ------------------------------------


@pytest.mark.asyncio
async def test_get_audit_reasons(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
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
async def test_get_task_status(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-abc",
        json={"operationToken": "op-abc", "status": "Completed", "percentComplete": 100},
    )

    async with _build_client(settings) as client:
        result = await client.get_task_status("op-abc")
    assert result["status"] == "Completed"


# --- list_repositories (3 envelope-normalization variants) ------------------


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
            {"repoId": "MAIN", "repoName": "Insurance"},
        ],
    )

    async with _build_client(settings) as client:
        result = await client.list_repositories()
    assert isinstance(result, dict)
    assert [r["repoId"] for r in result["value"]] == ["ASTR", "MAIN"]


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


# --- Cached schema-definition lookups ---------------------------------------


@pytest.mark.asyncio
async def test_cached_field_definitions_keyed_by_name(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {"id": 14, "name": "Employee ID", "fieldType": "String", "isRequired": False},
                {"id": 16, "name": "Last Name", "fieldType": "String", "isRequired": False},
            ]
        },
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
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
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
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
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
        url=f"{_BASE_V1}/TagDefinitions?%24top=200&%24skip=0",
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
        url=f"{_BASE_V1}/TemplateDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {"id": 2, "name": "Personnel Document", "fieldCount": 14},
            ]
        },
    )
    async with _build_client(settings) as client:
        result = await client.cached_template_definitions()
    assert "Personnel Document" in result
    assert result["Personnel Document"]["fieldCount"] == 14


@pytest.mark.asyncio
async def test_cached_link_definitions_keyed_by_id(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/LinkDefinitions?%24top=200&%24skip=0",
        json={
            "value": [
                {"linkTypeId": 1, "sourceLabel": "Supersedes"},
                {"linkTypeId": 2, "sourceLabel": "Attachment"},
            ]
        },
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
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=200&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    async with _build_client(settings) as client:
        await client.cached_field_definitions()
        await client.cached_field_definitions()
    # If both mocks were consumed, pytest-httpx didn't raise an unmatched-request error.


@pytest.mark.asyncio
async def test_cached_field_definitions_halves_page_size_on_400(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the server rejects a large $top with 400, the cache halves and retries.

    Reproduces a v1 server quirk: $top=200 → 400 errorCode 216,
    $top=100 → 400, $top=50 → 400, $top=25 → 200 OK.
    """
    monkeypatch.setenv("LF_MAX_RESULTS_DEFAULT", "25")
    monkeypatch.setenv("LF_MAX_RESULTS_CEILING", "200")
    settings = Settings()  # type: ignore[call-arg]
    for top in (200, 100, 50):
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE_V1}/FieldDefinitions?%24top={top}&%24skip=0",
            status_code=400,
            json={"error": {"code": 216, "message": "query parameter not valid"}},
        )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}]},
    )
    async with _build_client(settings) as client:
        result = await client.cached_field_definitions()
    assert "F1" in result


@pytest.mark.asyncio
async def test_cached_field_definitions_pages_until_short_batch(
    httpx_mock: HTTPXMock,
    lf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ceiling-sized pages are walked until the server returns < page_size items.

    Defense against the v1 errorCode-216 case: never assume the whole
    definitions list fits in one round trip.
    """
    monkeypatch.setenv("LF_MAX_RESULTS_DEFAULT", "2")
    monkeypatch.setenv("LF_MAX_RESULTS_CEILING", "2")
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=2&%24skip=0",
        json={"value": [{"id": 1, "name": "F1"}, {"id": 2, "name": "F2"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=2&%24skip=2",
        json={"value": [{"id": 3, "name": "F3"}, {"id": 4, "name": "F4"}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/FieldDefinitions?%24top=2&%24skip=4",
        json={"value": [{"id": 5, "name": "F5"}]},  # short batch → stop
    )
    async with _build_client(settings) as client:
        result = await client.cached_field_definitions()
    assert set(result.keys()) == {"F1", "F2", "F3", "F4", "F5"}
