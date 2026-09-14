"""Tests for ``client/_search.py`` — the asynchronous /Searches flow.

v1 and v2 spell every step of this flow differently (``Searches`` vs
``Searches/SearchAsync``, a dedicated status endpoint vs ``Tasks?taskIds=``),
so each method is tested against both dialects.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE_V1, _BASE_V2

_TOKEN = "srch-1234"


def _v2_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    return Settings()  # type: ignore[call-arg]


# --- create_search ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_search_v1_posts_to_searches(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST", url=f"{_BASE_V1}/Searches", status_code=202, json={"token": _TOKEN}
    )

    async with _build_client(settings) as client:
        token = await client.create_search('{LF:Basic~="x"}')

    assert token == _TOKEN
    body = json.loads(httpx_mock.get_requests()[0].read())
    assert body == {"searchCommand": '{LF:Basic~="x"}'}


@pytest.mark.asyncio
async def test_create_search_v2_posts_to_search_async_and_reads_task_id(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _v2_settings(monkeypatch)
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_V2}/Searches/SearchAsync",
        status_code=202,
        json={"taskId": _TOKEN},
    )

    async with _build_client(settings) as client:
        assert await client.create_search("q") == _TOKEN


@pytest.mark.asyncio
async def test_create_search_without_a_token_is_an_error(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """A 202 with no token is unusable — fail loudly instead of polling nothing."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="POST", url=f"{_BASE_V1}/Searches", status_code=202, json={"unexpected": True}
    )

    async with _build_client(settings) as client:
        with pytest.raises(LaserficheError, match="no search token"):
            await client.create_search("q")


# --- get_search_status ------------------------------------------------------


@pytest.mark.asyncio
async def test_status_v1_normalizes_completed(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Searches/{_TOKEN}",
        json={"status": "Completed", "percentComplete": 100, "errors": []},
    )

    async with _build_client(settings) as client:
        status = await client.get_search_status(_TOKEN)

    assert status["done"] is True
    assert status["failed"] is False
    assert status["percent_complete"] == 100


@pytest.mark.asyncio
async def test_status_v2_unwraps_the_tasks_collection(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _v2_settings(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Tasks?taskIds={_TOKEN}",
        json={"value": [{"id": _TOKEN, "status": "InProgress", "percentComplete": 42}]},
    )

    async with _build_client(settings) as client:
        status = await client.get_search_status(_TOKEN)

    assert status["done"] is False
    assert status["percent_complete"] == 42


@pytest.mark.asyncio
async def test_status_treats_cancelled_spellings_as_failed(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    """Self-hosted builds disagree on Canceled vs Cancelled."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET", url=f"{_BASE_V1}/Searches/{_TOKEN}", json={"status": "Cancelled"}
    )

    async with _build_client(settings) as client:
        assert (await client.get_search_status(_TOKEN))["failed"] is True


# --- get_search_results -----------------------------------------------------


@pytest.mark.asyncio
async def test_results_v1_pages_client_side(httpx_mock: HTTPXMock, lf_env: dict[str, str]) -> None:
    """v1 rejects $top on search endpoints, so paging happens here."""
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Searches/{_TOKEN}/Results",
        json={"value": [{"id": i} for i in range(10)]},
    )

    async with _build_client(settings) as client:
        raw = await client.get_search_results(_TOKEN, max_results=3, skip=2)

    assert [row["id"] for row in raw["value"]] == [2, 3, 4]
    assert "$top" not in str(httpx_mock.get_requests()[0].url)


@pytest.mark.asyncio
async def test_results_v2_pages_server_side(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _v2_settings(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/Searches/{_TOKEN}/Results?%24top=3&%24skip=2&%24count=true",
        json={"value": [], "@odata.count": 10},
    )

    async with _build_client(settings) as client:
        raw = await client.get_search_results(_TOKEN, max_results=3, skip=2)

    assert raw["@odata.count"] == 10


# --- context hits + close ---------------------------------------------------


@pytest.mark.asyncio
async def test_context_hits_uses_the_row_number(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V1}/Searches/{_TOKEN}/Results/7/ContextHits",
        json={"value": [{"pageNumber": 2, "context": "hello"}]},
    )

    async with _build_client(settings) as client:
        raw = await client.get_search_context_hits(_TOKEN, 7)

    assert raw["value"][0]["pageNumber"] == 2


@pytest.mark.asyncio
async def test_close_search_v1_deletes_the_token(
    httpx_mock: HTTPXMock, lf_env: dict[str, str]
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(method="DELETE", url=f"{_BASE_V1}/Searches/{_TOKEN}")

    async with _build_client(settings) as client:
        await client.close_search(_TOKEN)

    assert httpx_mock.get_requests()[0].method == "DELETE"


@pytest.mark.asyncio
async def test_close_search_v2_deletes_via_tasks(
    httpx_mock: HTTPXMock, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _v2_settings(monkeypatch)
    httpx_mock.add_response(method="DELETE", url=f"{_BASE_V2}/Tasks?taskIds={_TOKEN}")

    async with _build_client(settings) as client:
        await client.close_search(_TOKEN)

    assert httpx_mock.get_requests()[0].method == "DELETE"
