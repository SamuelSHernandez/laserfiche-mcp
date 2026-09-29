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

import logging
import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.tools._registry import all_tools
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


def test_mcp_instructions_steer_multi_document_reads_to_search_content() -> None:
    """The instructions field must tell the calling model to prefer
    search_content over opening documents one by one when checking many
    of them for the same fact — reading N full documents into a
    conversation that never reclaims context is what exhausted a client's
    context after ~5 prompts on a pre-2.3.0 build (see CHANGELOG.md and
    the catalog-budget tests below). Guidance alone doesn't force the
    calling model's hand, but it's the cheapest lever the server has."""
    text = server.mcp.instructions
    assert text is not None
    lower = text.lower()
    assert "search_content" in lower
    assert "one by one" in lower or "individually" in lower


def test_mcp_instructions_only_names_registered_v2_tools() -> None:
    """Every ``laserfiche_*``-shaped identifier in the onboarding text must
    be a tool that's actually registered by default.

    Regression: the instructions text used to reference legacy verb-first
    names (``search_content``, ``get_entry``, ...), which register only
    when an operator opts in with ``LF_LEGACY_TOOL_NAMES=true`` (default
    false since v2.3.0) — so a fresh default install's own onboarding
    text told the model to call tools that returned unknown-tool errors.
    v2 names are always registered (see ``server._register_one``), so
    this pins the text to referencing only those.
    """
    text = server.mcp.instructions
    assert text is not None
    v2_names = {spec.v2_name for spec in all_tools()}
    mentioned = set(re.findall(r"laserfiche_[a-z_]+", text))
    assert mentioned, "expected at least one laserfiche_* tool name in the instructions"
    unknown = mentioned - v2_names
    assert not unknown, f"instructions reference unregistered tool name(s): {sorted(unknown)}"


def test_readme_tools_section_mentions_every_registered_v2_name() -> None:
    """The README's Tools section is the operator-facing catalog reference —
    every registered tool's v2 name must appear there (in a table row, or
    the preview/execute explanatory prose for the 10 split tools). It used
    to document only 34 of 51 registered tools; an operator scoping
    LF_WRITE_TOOLS_ALLOWED from that table would unknowingly block a third
    of the write surface."""
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    tools_section = text.split("## Tools", 1)[1].split("\n## ", 1)[0]
    v2_names = {spec.v2_name for spec in all_tools()}
    missing = sorted(name for name in v2_names if name not in tools_section)
    assert not missing, f"README's ## Tools section is missing: {missing}"


@pytest.mark.asyncio
async def test_lifespan_warns_when_destructive_scope_configured_without_oauth(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE without LF_HTTP_OAUTH_ISSUER means the
    destructive-scope gate silently has no effect — must warn at startup,
    consistent with http_transport.py's loopback warning for the same
    "configured X but it has no effect" class of misconfiguration."""
    from laserfiche_mcp import _app

    settings = server._get_settings()
    monkeypatch.setattr(settings, "http_oauth_destructive_scope", "laserfiche.destructive")
    monkeypatch.setattr(settings, "http_oauth_issuer", None)

    with caplog.at_level(logging.WARNING, logger="laserfiche_mcp"):
        async with _app._lifespan(_app.mcp) as ctx:
            assert "client" in ctx

    assert any("LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE" in r.message for r in caplog.records)


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


# --- Catalog size budget -----------------------------------------------------
#
# A client evaluating a pre-2.3.0 build reported the tool exhausting its
# context after ~5 prompts. The catalog (doubled naming schemes stacked on
# untrimmed docstrings — see the "Breaking" and "Changed" sections of the
# 2.3.0 CHANGELOG entry) was a real, measurable contributor, though an
# audit of this fix found it was NOT the dominant cause — a single
# get_document_edoc(mode="text") read can return up to 50,000 chars, and
# reading several documents that way in one conversation costs far more
# than the catalog ever did (see documents.py's _LARGE_READ_HINT_CHARS and
# _with_multi_doc_hint for the fix aimed at that actual mechanism). These
# two tests guard only the catalog-size piece: docstring/schema bloat
# creeping back in, and the legacy-name gate (server._legacy_names_enabled
# / server._register_one) drifting from its documented ~2x cost. Neither
# test is a general "can't reopen silently" claim about the ~5-prompt
# failure as a whole — see docs/internal/TODO.md for the broader
# reliability backlog this sits inside of.


def _catalog_char_total(tools: list) -> int:
    """Sum of name + description + JSON schema length across a tool list.

    A character count, not a token count — cheap, dependency-free (no
    tiktoken), and monotonic with the actual token cost, which is all a
    regression guard needs. Don't read the absolute number as "the token
    cost"; read the trend.
    """
    import json

    return sum(
        len(t.name) + len(t.description or "") + len(json.dumps(t.inputSchema)) for t in tools
    )


@pytest.mark.asyncio
async def test_read_catalog_char_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (LF_LEGACY_TOOL_NAMES unset) read-only catalog must stay
    well under its post-2.3.0-trim size.

    Measured at 30,783 chars the day this test was added (22 tools).
    Ceiling below gives ~25% headroom for legitimate new tools/params
    before it fails — if you're raising this number, that's fine, just do
    it with your eyes open about what it costs every session."""
    from mcp.server.fastmcp import FastMCP

    from laserfiche_mcp.tools._registry import all_tools

    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "false")
    monkeypatch.setattr(server, "mcp", FastMCP("budget-test"))
    for spec in (s for s in all_tools() if not s.is_write):
        server._register_one(spec)
    total = _catalog_char_total(await server.mcp.list_tools())
    assert total < 38_000, (
        f"Read-only catalog grew to {total} chars (budget: 38,000) with "
        "LF_LEGACY_TOOL_NAMES=false — this test forces that value, so it "
        "guards docstring/schema bloat only, not the default itself (see "
        "test_all_tools_registered / test_legacy_names_enabled_parsing "
        "for that). If the growth is deliberate, bump the ceiling with "
        "eyes open about the per-session cost."
    )


@pytest.mark.asyncio
async def test_legacy_names_still_roughly_double_catalog_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canary for the exact mechanism the 2.3.0 Breaking change fixed:
    opting into LF_LEGACY_TOOL_NAMES=true should cost roughly what it
    always cost (~2x the read-only catalog), not silently more or less.

    Registers through the real gate (``server._register_one`` /
    ``server._legacy_names_enabled``) rather than hand-building both name
    sets, so a bug in the gate itself — not just a docstring change —
    would also show up here."""
    from mcp.server.fastmcp import FastMCP

    from laserfiche_mcp.tools._registry import all_tools

    read_specs = [s for s in all_tools() if not s.is_write]

    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "false")
    monkeypatch.setattr(server, "mcp", FastMCP("ratio-test-off"))
    for spec in read_specs:
        server._register_one(spec)
    off_total = _catalog_char_total(await server.mcp.list_tools())

    monkeypatch.setenv("LF_LEGACY_TOOL_NAMES", "true")
    monkeypatch.setattr(server, "mcp", FastMCP("ratio-test-on"))
    for spec in read_specs:
        server._register_one(spec)
    on_total = _catalog_char_total(await server.mcp.list_tools())

    ratio = on_total / off_total
    assert 1.8 < ratio < 2.2, (
        f"LF_LEGACY_TOOL_NAMES=true catalog is {ratio:.2f}x the default "
        "catalog (expected ~2x, since every tool gains a same-size alias)."
    )
