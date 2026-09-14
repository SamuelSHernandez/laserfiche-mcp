"""Tests for ``tools/content_search.py`` — the async /Searches flow.

The through-line of these tests is that the search token is a scarce,
session-scoped resource: every path — success, failure, timeout — must end
with a DELETE. Several tests assert on the full request sequence for exactly
that reason.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.tools.content_search import build_search_command
from tests.conftest import _BASE

_TOKEN = "srch-1234"


def _mock_search_lifecycle(
    httpx_mock: HTTPXMock,
    *,
    results: list[dict],
    status: str = "Completed",
) -> None:
    """Wire up create → status → results for the happy path."""
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=202, json={"token": _TOKEN}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}",
        json={"operationToken": _TOKEN, "status": status, "percentComplete": 100, "errors": []},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Searches/{_TOKEN}/Results", json={"value": results}
    )


def _requests(httpx_mock: HTTPXMock) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in httpx_mock.get_requests()]


# --- build_search_command ---------------------------------------------------


def test_bare_phrase_is_wrapped_as_content_clause() -> None:
    assert build_search_command("unpaid balance", None) == '{LF:Basic~="unpaid balance"}'


def test_raw_syntax_is_passed_through_untouched() -> None:
    raw = '{LF:Basic~="asbestos",option="D"}'
    assert build_search_command(raw, None) == raw


def test_quotes_in_a_phrase_are_escaped() -> None:
    assert build_search_command('say "hello"', None) == '{LF:Basic~="say \\"hello\\""}'


def test_folder_path_is_appended_as_lookin_clause() -> None:
    assert build_search_command("x", "\\HR") == '{LF:Basic~="x"} & {LF:LookIn="\\HR"}'


def test_folder_path_composes_with_raw_syntax() -> None:
    out = build_search_command('{LF:Name="*.pdf"}', "\\HR")
    assert out == '{LF:Name="*.pdf"} & {LF:LookIn="\\HR"}'


# --- happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_context_hits_for_matching_entries(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_search_lifecycle(
        httpx_mock,
        results=[{"id": 7, "name": "lease.pdf", "entryType": "Document", "rowNumber": 1}],
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}/Results/1/ContextHits",
        json={
            "value": [
                {
                    "hitNumber": 1,
                    "hitType": "PageContent",
                    "pageNumber": 4,
                    "context": "tenant owes an unpaid balance of $2,400 as of March",
                    "highlight1Offset": 15,
                    "highlight1Length": 14,
                }
            ]
        },
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="unpaid balance")

    assert result["mode"] == "content_search"
    assert result["returned"] == 1
    entry = result["results"][0]
    assert entry["entry_id"] == 7
    assert entry["hit_count"] == 1
    hit = entry["hits"][0]
    assert hit["page"] == 4
    assert hit["match"] == "unpaid balance"
    assert "unpaid balance" in hit["text"]


@pytest.mark.asyncio
async def test_token_is_released_after_a_successful_search(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_search_lifecycle(httpx_mock, results=[])
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    await server.search_content(query="x")

    assert ("DELETE", f"/LFRepositoryAPI/v1/Repositories/demo/Searches/{_TOKEN}") in _requests(
        httpx_mock
    )


@pytest.mark.asyncio
async def test_hits_for_top_zero_skips_the_context_hit_requests(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """hits_for_top=0 should cost no extra round-trips at all."""
    _mock_search_lifecycle(
        httpx_mock,
        results=[{"id": 7, "name": "a.pdf", "entryType": "Document", "rowNumber": 1}],
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x", hits_for_top=0)

    assert result["results"][0]["hits"] == []
    assert not any("ContextHits" in path for _, path in _requests(httpx_mock))


@pytest.mark.asyncio
async def test_hits_are_truncated_per_entry_with_true_count_reported(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    _mock_search_lifecycle(
        httpx_mock,
        results=[{"id": 7, "name": "a.pdf", "entryType": "Document", "rowNumber": 1}],
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}/Results/1/ContextHits",
        json={
            "value": [
                {"pageNumber": i, "context": f"hit {i}", "hitType": "PageContent"}
                for i in range(1, 8)
            ]
        },
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x", hits_per_entry=2)

    entry = result["results"][0]
    assert len(entry["hits"]) == 2
    assert entry["hit_count"] == 7
    assert entry["hits_truncated"] is True


@pytest.mark.asyncio
async def test_a_failing_context_hit_row_still_returns_the_match(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A partial answer beats dropping a genuine match."""
    _mock_search_lifecycle(
        httpx_mock,
        results=[{"id": 7, "name": "a.pdf", "entryType": "Document", "rowNumber": 1}],
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}/Results/1/ContextHits",
        status_code=500,
        json={"error": "boom"},
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x")

    entry = result["results"][0]
    assert entry["entry_id"] == 7
    assert entry["hits_error"] == "500"


# --- failure paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_searches_endpoint_points_at_search_entries(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Old builds have no /Searches; that must not read as 'nothing matched'."""
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=404, json={"title": "Not Found"}
    )

    result = await server.search_content(query="x")

    assert result["mode"] == "error"
    assert result["error"] == "async_search_unavailable"
    assert "search_entries" in result["hint"]


@pytest.mark.asyncio
async def test_server_side_search_failure_is_surfaced_with_its_errors(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=202, json={"token": _TOKEN}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}",
        json={"status": "Failed", "errors": [{"message": "bad syntax"}], "percentComplete": 0},
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x")

    assert result["error"] == "search_failed"
    assert result["kind"] == "upstream_unavailable"
    assert result["server_errors"] == [{"message": "bad syntax"}]
    # Still released, even though the search itself failed.
    assert any(method == "DELETE" for method, _ in _requests(httpx_mock))


@pytest.mark.asyncio
async def test_timeout_abandons_the_search_and_releases_the_token(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=202, json={"token": _TOKEN}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}",
        json={"status": "InProgress", "percentComplete": 30, "errors": []},
        is_reusable=True,
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x", timeout_seconds=0.01)

    assert result["error"] == "search_timeout"
    assert result["kind"] == "upstream_unavailable"
    assert result["percent_complete"] == 30
    assert any(method == "DELETE" for method, _ in _requests(httpx_mock))


@pytest.mark.asyncio
async def test_absent_status_endpoint_falls_through_to_results(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A build without a status endpoint shouldn't lose an otherwise-good search."""
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=202, json={"token": _TOKEN}
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Searches/{_TOKEN}", status_code=405, json={"title": "nope"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}/Results",
        json={"value": [{"id": 9, "name": "b.pdf", "entryType": "Document", "rowNumber": 1}]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}/Results/1/ContextHits",
        json={"value": []},
    )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")

    result = await server.search_content(query="x")

    assert result["returned"] == 1
    assert result["results"][0]["entry_id"] == 9
