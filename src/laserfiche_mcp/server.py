"""FastMCP server exposing Laserfiche Repository operations as tools.

Design notes:
- v1 is read-only by default. Writes are gated behind LF_READ_ONLY=false (v1.1).
- Tool descriptions are prompts: they tell the LLM when to use the tool,
  what the parameters mean, and what the response shape will look like.
- Mapping from raw API responses to our trimmed pydantic models lives in
  ``models.py`` (each model has a ``from_api`` classmethod), so this file
  stays focused on tool registration and orchestration.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from . import __version__
from .auth import build_auth_strategy
from .client import LaserficheClient, LaserficheError
from .config import Settings
from .models import EntryDetail, FieldValue, SearchResults

logger = logging.getLogger("laserfiche_mcp")


# --- Lazy bootstrap ----------------------------------------------------------

_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def _reset_settings_for_tests() -> None:
    """Reset the cached settings — for use only by tests via monkeypatch."""
    global _settings
    _settings = None


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Open one shared client for the server's lifetime."""
    settings = _get_settings()
    auth = build_auth_strategy(settings)
    async with LaserficheClient(settings, auth) as client:
        yield {"client": client}


mcp = FastMCP(
    "laserfiche-mcp",
    instructions=(
        "Tools for searching and reading documents in a Laserfiche repository. "
        "Use search_entries when the user describes what they're looking for in "
        "natural language; use list_folder when they reference a known location; "
        "use get_entry or get_field_values once you have an entry ID. "
        "Most workflows are: (1) locate an entry via search/path/folder, "
        "(2) call get_entry for metadata or get_field_values for template data, "
        "(3) optionally call get_document_text for the document body."
    ),
    lifespan=_lifespan,
)


# --- Helpers -----------------------------------------------------------------

def _client() -> LaserficheClient:
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["client"]


def _clamp_max_results(requested: int | None) -> int:
    settings = _get_settings()
    value = settings.max_results_default if requested is None else requested
    return min(max(1, value), settings.max_results_ceiling)


# --- Read tools --------------------------------------------------------------


@mcp.tool()
async def search_entries(
    query: str,
    max_results: int | None = None,
) -> SearchResults:
    """Search the Laserfiche repository for entries matching a query.

    Use this when the user describes documents they're looking for and you don't
    yet have an entry ID or known folder. Examples of valid queries:

        Name search:    {LF:Name="Onboarding*"}
        Field search:   {[Missionary Application]:[Last Name]="Smith"}
        Path scope:     {LF:LookIn="\\Imports\\2024"}

    Combine clauses with & (AND) or | (OR), e.g.:
        {LF:Name="*.pdf"} & {[Application]:[Status]="Approved"}

    If the user gives a natural-language description, translate it into
    Laserfiche search syntax before calling — or use the higher-level
    search_by_name tool if you only need a name match. If you're uncertain
    how to translate, ask the user to clarify rather than guessing.

    Returns a SearchResults with up to `max_results` entries (default 25,
    hard cap from LF_MAX_RESULTS_CEILING). Each entry has id, name, type,
    and full_path — use get_entry or get_field_values to drill in.
    """
    try:
        raw = await _client().search_entries(
            query, max_results=_clamp_max_results(max_results),
        )
    except LaserficheError as exc:
        raise RuntimeError(f"Search failed: {exc}") from exc

    return SearchResults.from_api(raw)


@mcp.tool()
async def search_by_name(
    name_pattern: str,
    in_folder_path: str | None = None,
    max_results: int | None = None,
) -> SearchResults:
    """Search for entries by name pattern — convenience wrapper over search_entries.

    Use this when the user just wants to find entries by name and you don't
    need full Laserfiche query syntax. Wildcards are supported (`*` matches
    any sequence, `?` matches one character).

    Examples:
        name_pattern="Onboarding*"                 → all entries starting with "Onboarding"
        name_pattern="*.pdf"                       → all entries ending in .pdf
        name_pattern="Smith*", in_folder_path="\\Imports\\2024"
                                                   → name match scoped to one folder
    """
    safe_pattern = name_pattern.replace('"', '\\"')
    query = f'{{LF:Name="{safe_pattern}"}}'
    if in_folder_path:
        safe_path = in_folder_path.replace('"', '\\"')
        query = f'{query} & {{LF:LookIn="{safe_path}"}}'

    try:
        raw = await _client().search_entries(
            query, max_results=_clamp_max_results(max_results),
        )
    except LaserficheError as exc:
        raise RuntimeError(f"Search failed: {exc}") from exc

    return SearchResults.from_api(raw)


@mcp.tool()
async def list_folder(
    folder_id: int,
    max_results: int | None = None,
    skip: int = 0,
) -> SearchResults:
    """List immediate children (documents and subfolders) of a folder.

    Use this when the user references a known folder by ID, or after resolving
    a path with get_entry_by_path. The root folder typically has ID 1.

    Pagination: pass `skip` (0-indexed offset) to page through large folders.
    Combine with `max_results` to fetch in chunks. The response includes
    `total_count` and `next_link` when the server provides them.
    """
    try:
        raw = await _client().list_folder(
            folder_id,
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to list folder {folder_id}: {exc}") from exc

    return SearchResults.from_api(raw)


@mcp.tool()
async def get_entry(entry_id: int) -> EntryDetail:
    """Fetch full details for a single entry (folder or document) by ID.

    Returns name, type, path, template, page count, and other metadata. Does
    NOT include field values — call get_field_values for those. Does NOT
    include document content — call get_document_text for that.
    """
    try:
        raw = await _client().get_entry(entry_id)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to fetch entry {entry_id}: {exc}") from exc
    return EntryDetail.from_api(raw)


@mcp.tool()
async def get_entry_by_path(full_path: str) -> EntryDetail:
    """Resolve a Laserfiche full path to an entry.

    Path uses backslashes, e.g. `\\Imports\\2024\\Onboarding\\Smith,John`.
    Useful when the user references a location by name rather than ID.
    """
    try:
        raw = await _client().get_entry_by_path(full_path)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to resolve path '{full_path}': {exc}") from exc
    return EntryDetail.from_api(raw)


@mcp.tool()
async def get_field_values(entry_id: int) -> list[FieldValue]:
    """Read all template field values assigned to an entry.

    Returns one FieldValue per field on the entry's template, including
    multi-value fields (where `is_multi_value=true`). Empty/unset fields
    are typically omitted by the Repository API.
    """
    try:
        raw = await _client().get_field_values(entry_id)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to fetch fields for entry {entry_id}: {exc}") from exc
    return FieldValue.list_from_api(raw)


@mcp.tool()
async def get_document_text(entry_id: int, max_chars: int = 50_000) -> str:
    """Download the Laserfiche-extracted text of an electronic document.

    Uses the official Export endpoint with part="Text", which returns the
    text Laserfiche has already extracted (via OCR or upstream extraction)
    rather than raw bytes. This is the right call for "summarize this
    document" or "what's in this entry" — you get clean text, not a binary
    payload.

    Output is truncated to `max_chars` (default 50,000) to keep context
    bounded. If the entry is a folder or has no extracted text, the server
    will return an error.
    """
    try:
        content = await _client().export_entry(entry_id, part="Text")
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to download text for entry {entry_id}: {exc}") from exc

    text = content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated, {len(text) - max_chars} chars omitted]"
    return text


@mcp.tool()
async def get_document_edoc(entry_id: int) -> dict[str, Any]:
    """Download the raw electronic document (Edoc) for an entry.

    Returns metadata only — never the raw bytes — because PDFs, Office docs,
    and images are not useful to dump into the model's context window. The
    response includes byte size and a hint to use get_document_text for the
    extracted text instead.
    """
    try:
        content = await _client().export_entry(entry_id, part="Edoc")
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to download edoc for entry {entry_id}: {exc}") from exc

    return {
        "entry_id": entry_id,
        "byte_size": len(content),
        "hint": (
            "Raw bytes were fetched but not returned to the model. "
            "Use get_document_text(entry_id) to retrieve the extracted text."
        ),
    }


# --- Entrypoint --------------------------------------------------------------


_HELP_TEXT = """\
laserfiche-mcp — Model Context Protocol server for Laserfiche.

Usage:
  laserfiche-mcp            Start the stdio MCP server (requires env config).
  laserfiche-mcp --help     Show this message.
  laserfiche-mcp --version  Print version and exit.

Configuration is read from LF_* environment variables (or a .env file in
the working directory). Required at a minimum:

  LF_REPO_API_URL    Base URL of your Repository API Server
  LF_REPOSITORY_ID   Repository name or ID
  LF_USERNAME        Service account username
  LF_PASSWORD        Service account password

See https://github.com/SamuelSHernandez/laserfiche-mcp#configure for the
full list including OAuth, SSL, retry, and logging knobs.

This binary is meant to be launched by an MCP client (Claude Desktop,
Claude Code, MCP Inspector). Running it directly without env config is
expected to exit with a configuration error.
"""


def _format_config_error(exc: Exception) -> str:
    """Convert a Pydantic ValidationError into a user-facing message."""
    lines = [
        "laserfiche-mcp: configuration is missing or invalid.",
        "",
    ]
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            msg = err.get("msg", "")
            # Pydantic prefixes value_error.* messages with "Value error, "
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            lines.append(f"  - {msg}")
    else:
        lines.append(f"  - {exc}")
    lines.extend([
        "",
        "Quick start:",
        "  1. Copy .env.example to .env and fill in your repository details, OR",
        "  2. Set LF_REPO_API_URL, LF_REPOSITORY_ID, LF_USERNAME, LF_PASSWORD",
        "     as environment variables (e.g. via your MCP client's `env` block).",
        "",
        "Docs: https://github.com/SamuelSHernandez/laserfiche-mcp#configure",
    ])
    return "\n".join(lines)


def main() -> None:
    """Console-script entrypoint registered in pyproject.toml."""
    argv = sys.argv[1:]
    if any(a in ("-h", "--help") for a in argv):
        print(_HELP_TEXT)
        return
    if any(a in ("-V", "--version") for a in argv):
        print(f"laserfiche-mcp {__version__}")
        return

    try:
        settings = _get_settings()
    except (ValidationError, ValueError) as exc:
        print(_format_config_error(exc), file=sys.stderr)
        sys.exit(2)
    except NotImplementedError as exc:
        # Cloud mode and api_key auth raise this from the validator.
        print(f"laserfiche-mcp: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=settings.log_level.upper())
    if settings.read_only:
        logger.info("Starting laserfiche-mcp in READ-ONLY mode.")
    else:
        logger.warning(
            "Starting laserfiche-mcp with WRITE tools enabled. "
            "Write tools will be added in v1.1 — for now this flag has no effect."
        )

    try:
        mcp.run()  # stdio transport by default
    except KeyboardInterrupt:
        # Don't dump a traceback for ordinary Ctrl-C exits.
        logger.info("laserfiche-mcp stopped.")


if __name__ == "__main__":
    main()
