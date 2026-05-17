"""Tests for ``laserfiche_mcp.server`` — the thin entrypoint shell.

server.py itself does almost nothing beyond:
  1. Re-exporting helpers from ``_app`` (``_clamp_max_results``, ``_client``).
  2. Conditionally registering write tools via ``_register_write_tools``.
  3. Owning the v2 alias map (``_V2_RENAME_MAP``).

Per-tool behavior tests live under ``tests/tools/``. What's left here is
the registration logic, the cross-cutting path-fence / tool-allowlist
checks (which apply uniformly across every write tool), and the
``_clamp_max_results`` helper that's re-exported from ``_app``.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- _clamp_max_results (re-exported from _app) -----------------------------


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


# --- Write tools: read_only gating -------------------------------------------


@pytest.mark.asyncio
async def test_write_tool_refuses_when_read_only(
    patched_client: LaserficheClient,
) -> None:
    """LF_READ_ONLY=true (the test default) makes write helpers refuse to run
    even if invoked directly. Belt-and-suspenders to the registration gate."""
    with pytest.raises(RuntimeError) as exc_info:
        await server.set_fields(42, {"Note": ["x"]})
    assert "read_only" in str(exc_info.value).lower()


# --- Write tools: registration -----------------------------------------------


@pytest.mark.asyncio
async def test_write_tools_registered_when_writes_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LF_READ_ONLY=false, _register_write_tools() adds the writes."""
    monkeypatch.setenv("LF_READ_ONLY", "false")
    server._reset_settings_for_tests()

    # Snapshot the tool registry before mutating it so we can roll back
    # cleanly after the test — the FastMCP instance is a module-level
    # singleton shared with downstream tests.
    before = set(server.mcp._tool_manager._tools.keys())

    try:
        server._register_write_tools()
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert "delete_entry" in names
        assert "rename_entry" in names
        assert "set_fields" in names
        assert "merge_fields" in names
    finally:
        after = set(server.mcp._tool_manager._tools.keys())
        for added in after - before:
            server.mcp._tool_manager.remove_tool(added)
        monkeypatch.setenv("LF_READ_ONLY", "true")
        server._reset_settings_for_tests()


def test_register_write_tools_respects_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_write_tools only registers tools in LF_WRITE_TOOLS_ALLOWED."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings,
        "write_tools_allowed",
        "merge_fields,create_folder",
    )
    registered: list[str] = []
    monkeypatch.setattr(
        server.mcp,
        "tool",
        lambda **kwargs: lambda fn: registered.append(fn.__name__) or fn,
    )
    server._register_write_tools()
    assert set(registered) == {"merge_fields", "create_folder"}


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    """Read tools always register. Write tools only register when
    LF_READ_ONLY=false at startup (see test_write_tools_registered_when_writes_enabled)."""
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    # v1.x names — kept as deprecation shims through v2.0.
    legacy = {
        "search_entries",
        "search_by_name",
        "search_natural",
        "list_folder",
        "get_entry",
        "get_entry_by_path",
        "get_field_values",
        "get_document_text",
        "get_document_edoc",
        "list_repositories",
        "list_field_definitions",
        "list_tag_definitions",
        "list_template_definitions",
        "list_link_definitions",
        "get_audit_reasons",
        "get_task_status",
        "wait_for_task",
        "get_template_fields",
    }
    # v2.0 names — laserfiche_{resource}_{verb}. From _V2_RENAME_MAP.
    v2 = set(server._V2_RENAME_MAP.values())
    # Reads-only registration in this test (writes off in test config).
    expected = legacy | {name for old, name in server._V2_RENAME_MAP.items() if old in legacy}
    assert names == expected
    # Sanity: every old name has a v2 alias.
    for old in legacy:
        assert old in server._V2_RENAME_MAP, f"missing v2 alias for {old}"
    # Sanity: v2 names all start with the laserfiche_ prefix.
    assert all(n.startswith("laserfiche_") for n in v2)


# --- Cross-cutting security: path fences + tool allowlist --------------------
# These exercise the security model end-to-end (entry fetch → permission
# check → tool execution). They span multiple modules by design, so they
# live here rather than in any one ``tests/tools/test_*`` file.


@pytest.mark.asyncio
async def test_write_refused_by_path_deny(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Protected")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Protected\\Doc",
        },
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_write_allowed_within_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_allow", "\\Imports")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Imports\\2024\\Doc",
        },
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    # No "mode": "error" — write proceeded
    assert "error" not in result


@pytest.mark.asyncio
async def test_write_refused_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_allow", "\\Imports")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Production\\Doc",
        },
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_tool_allowlist_blocks_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Defense-in-depth: even if a tool is invoked directly, the allowlist
    refuses operations outside the configured set."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings,
        "write_tools_allowed",
        "merge_fields,merge_tags",
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_entry(42)
    assert result["mode"] == "error"
    assert result["error"] == "tool_not_allowed"
