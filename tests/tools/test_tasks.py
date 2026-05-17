"""Tests for ``tools/tasks.py`` — async-operation polling helpers."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE


@pytest.mark.asyncio
async def test_get_task_status(httpx_mock: HTTPXMock, patched_client: LaserficheClient) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "Completed", "percentComplete": 100},
    )
    result = await server.get_task_status("op-1")
    assert result["status"] == "Completed"


@pytest.mark.asyncio
async def test_wait_for_task_returns_on_terminal_status(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "Completed"},
    )
    result = await server.wait_for_task("op-1", timeout_seconds=5)
    assert result["timed_out"] is False
    assert result["status"] == "Completed"


@pytest.mark.asyncio
async def test_wait_for_task_times_out(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "InProgress", "percentComplete": 50},
        is_reusable=True,
    )
    result = await server.wait_for_task(
        "op-1",
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert result["timed_out"] is True
