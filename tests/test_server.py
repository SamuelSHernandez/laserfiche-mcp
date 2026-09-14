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


def test_mcp_instructions_document_the_confirm_token_contract() -> None:
    """The server's `instructions` field must tell the calling model that
    destructive tools use a preview -> confirm token pattern, so it
    doesn't have to discover the contract by trial and error."""
    text = server.mcp.instructions
    assert text is not None
    lower = text.lower()
    assert "confirmation_token" in lower
    assert "preview" in lower


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
    # This test checks write-tool registration, not the legacy-name default
    # (see test_legacy_names_enabled_parsing for that) — opt into legacy
    # names explicitly so its assertions don't depend on that default.
    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "true")
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


def test_register_write_tools_allowlist_accepts_v2_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LF_WRITE_TOOLS_ALLOWED configured with the v2 (README-recommended)
    names must register the same tools as the legacy-name equivalent —
    not silently register nothing."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings,
        "write_tools_allowed",
        "laserfiche_field_merge,laserfiche_folder_create",
    )
    registered: list[str] = []
    monkeypatch.setattr(
        server.mcp,
        "tool",
        lambda **kwargs: lambda fn: registered.append(fn.__name__) or fn,
    )
    server._register_write_tools()
    assert set(registered) == {"merge_fields", "create_folder"}


def test_register_write_tools_warns_on_unknown_allowlist_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo'd LF_WRITE_TOOLS_ALLOWED entry must be logged, not silently
    dropped — it would otherwise register nothing for that name with no
    diagnostic trail."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings,
        "write_tools_allowed",
        "merge_fields,merg_feilds_typo",
    )
    monkeypatch.setattr(
        server.mcp,
        "tool",
        lambda **kwargs: lambda fn: fn,
    )
    with caplog.at_level("WARNING", logger="laserfiche_mcp"):
        server._register_write_tools()
    assert any("merg_feilds_typo" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    """Read tools always register. Write tools only register when
    LF_READ_ONLY=false at startup (see test_write_tools_registered_when_writes_enabled).

    Reads register once, at module-import time, using whatever
    LF_LEGACY_TOOL_NAMES was in the process environment at that moment —
    unset in this test run, so the v2.3.0+ default (false) applies and
    only the v2 ``laserfiche_*`` names are present (see
    ``test_legacy_gate_halves_registration`` for the opt-in-true case).
    """
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    # v1.x names that exist as deprecation shims (registered only when
    # LF_LEGACY_TOOL_NAMES=true). The ``task_wait_or_poll`` entry is the
    # v2.x collapse of ``get_task_status`` + ``wait_for_task``
    # (PLAN.md step 3); it's the only collapse that's a read (the
    # field/tag/link/template collapses are all writes).
    legacy = {
        "search_entries",
        "search_by_name",
        "search_natural",
        "search_content",
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
        "task_wait_or_poll",
        "find_duplicate_documents",
        "compare_entries",
    }
    # v2.0 names — laserfiche_{resource}_{verb}. From _V2_RENAME_MAP.
    v2 = set(server._V2_RENAME_MAP.values())
    # Reads-only registration in this test (writes off in test config).
    # Default LF_LEGACY_TOOL_NAMES=false means only v2 names register.
    expected = {name for old, name in server._V2_RENAME_MAP.items() if old in legacy}
    assert names == expected
    assert not (names & legacy)
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


@pytest.mark.asyncio
async def test_tool_allowlist_accepts_v2_name_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """The runtime defense-in-depth check (check_write_permission) must
    also accept the v2 name — not just the legacy name — so a
    LF_WRITE_TOOLS_ALLOWED configured with v2 names doesn't refuse a
    tool that's actually registered and allowed."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings,
        "write_tools_allowed",
        "laserfiche_entry_delete",  # v2 name for delete_entry
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_entry(42)
    assert result.get("mode") != "error"


# --- LF_LEGACY_TOOL_NAMES gate ------------------------------------------------


def test_legacy_names_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LF_LEGACY_TOOL_NAMES", raising=False)
    assert server._legacy_names_enabled() is False  # v2.3.0+ default: aliases opt-in
    for off in ("false", "0", "no", "FALSE"):
        monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", off)
        assert server._legacy_names_enabled() is False
    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "true")
    assert server._legacy_names_enabled() is True


@pytest.mark.asyncio
async def test_legacy_gate_halves_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the gate off, only the laserfiche_* name is registered."""
    from mcp.server.fastmcp import FastMCP

    from laserfiche_mcp.tools._registry import all_tools

    spec = all_tools()[0]

    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "false")
    monkeypatch.setattr(server, "mcp", FastMCP("gate-test"))
    server._register_one(spec)
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {spec.v2_name}

    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "true")
    monkeypatch.setattr(server, "mcp", FastMCP("gate-test-2"))
    server._register_one(spec)
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {spec.v2_name, spec.legacy_name}
