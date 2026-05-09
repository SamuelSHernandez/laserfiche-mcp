"""Tests for server module helpers and tool registration."""

from __future__ import annotations

import pytest

from laserfiche_mcp import server


@pytest.fixture(autouse=True)
def _reset_settings(lf_env: dict[str, str]) -> None:
    server._reset_settings_for_tests()


def test_clamp_max_results_uses_default_when_none() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(None) == settings.max_results_default


def test_clamp_max_results_floors_at_one() -> None:
    assert server._clamp_max_results(0) == 1
    assert server._clamp_max_results(-5) == 1


def test_clamp_max_results_caps_at_ceiling() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(99_999) == settings.max_results_ceiling


def test_clamp_max_results_passes_through_in_range() -> None:
    assert server._clamp_max_results(10) == 10


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_entries",
        "search_by_name",
        "list_folder",
        "get_entry",
        "get_entry_by_path",
        "get_field_values",
        "get_document_text",
    }
