"""FastMCP server exposing Laserfiche Repository operations as tools.

Design notes:
- v1 is read-only by default. Writes are gated behind LF_READ_ONLY=false (v1.1).
- Tool descriptions are prompts: they tell the LLM when to use the tool,
  what the parameters mean, and what the response shape will look like.
- Responses are normalized to the pydantic models in models.py — the raw
  Repository API shape leaks too many fields and burns context.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import build_auth_strategy
from .client import LaserficheClient, LaserficheError
from .config import Settings
from .models import EntryDetail, EntrySummary, EntryType, FieldValue, SearchResults

logger = logging.getLogger("laserfiche_mcp")


# --- Lazy bootstrap ----------------------------------------------------------

# Settings used to be loaded at import time, but that broke tests that need
# to set env vars in fixtures. We now load on first access via ``_get_settings``
# and instantiate the FastMCP server eagerly with the module-level decorators.

_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


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
        "use get_entry or get_field_values once you have an entry ID."
    ),
    lifespan=_lifespan,
)


# --- Helpers -----------------------------------------------------------------

def _client() -> LaserficheClient:
    """Pull the shared client out of the lifespan context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["client"]


def _clamp_max_results(requested: int | None) -> int:
    settings = _get_settings()
    requested = requested or settings.max_results_default
    return min(max(1, requested), settings.max_results_ceiling)


def _coerce_entry_type(raw: str | None) -> EntryType:
    if not raw:
        return EntryType.UNKNOWN
    try:
        return EntryType(raw)
    except ValueError:
        return EntryType.UNKNOWN


def _to_summary(raw: dict[str, Any]) -> EntrySummary:
    """Map a Repository API entry payload to our trimmed summary."""
    return EntrySummary(
        id=raw.get("id") or raw.get("Id") or 0,
        name=raw.get("name") or raw.get("Name") or "",
        entry_type=_coerce_entry_type(raw.get("entryType") or raw.get("EntryType")),
        parent_id=raw.get("parentId") or raw.get("ParentId"),
        full_path=raw.get("fullPath") or raw.get("FullPath"),
        creation_time=raw.get("creationTime") or raw.get("CreationTime"),
        last_modified_time=raw.get("lastModifiedTime") or raw.get("LastModifiedTime"),
    )


def _to_detail(raw: dict[str, Any], fields: list[FieldValue] | None = None) -> EntryDetail:
    summary = _to_summary(raw)
    return EntryDetail(
        **summary.model_dump(),
        template_name=raw.get("templateName") or raw.get("TemplateName"),
        fields=fields or [],
        page_count=raw.get("pageCount") or raw.get("PageCount"),
        is_electronic_document=raw.get("isElectronicDocument") or raw.get("IsElectronicDocument"),
        extension=raw.get("extension") or raw.get("Extension"),
    )


def _to_fields(raw: dict[str, Any]) -> list[FieldValue]:
    """Repository API returns fields under 'value' as a list of {fieldName, values, ...}."""
    items = raw.get("value") or raw.get("Value") or []
    out: list[FieldValue] = []
    for item in items:
        out.append(FieldValue(
            field_name=item.get("fieldName") or item.get("FieldName") or "",
            field_type=item.get("fieldType") or item.get("FieldType"),
            values=item.get("values") or item.get("Values") or [],
            is_multi_value=bool(item.get("isMultiValue") or item.get("IsMultiValue")),
        ))
    return out


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

    If the user gives a natural-language description rather than search syntax,
    translate it into Laserfiche syntax before calling this tool. If you're
    uncertain how to translate, ask the user to clarify rather than guessing.

    Returns a list of entry summaries (id, name, type, path). Use get_entry or
    get_field_values to drill into a specific result.
    """
    try:
        raw = await _client().search_entries(query, max_results=_clamp_max_results(max_results))
    except LaserficheError as exc:
        raise RuntimeError(f"Search failed: {exc}") from exc

    items = raw.get("value") or raw.get("Value") or []
    return SearchResults(
        entries=[_to_summary(item) for item in items],
        total_count=raw.get("@odata.count"),
        next_link=raw.get("@odata.nextLink"),
    )


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
    """
    try:
        raw = await _client().list_folder(
            folder_id,
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to list folder {folder_id}: {exc}") from exc

    items = raw.get("value") or raw.get("Value") or []
    return SearchResults(
        entries=[_to_summary(item) for item in items],
        total_count=raw.get("@odata.count"),
        next_link=raw.get("@odata.nextLink"),
    )


@mcp.tool()
async def get_entry(entry_id: int) -> EntryDetail:
    """Fetch full details for a single entry (folder or document) by ID.

    Returns name, type, path, template, page count, and other metadata, but
    does NOT include field values — use get_field_values for those, or the
    document's content — use get_document_text for that.
    """
    try:
        raw = await _client().get_entry(entry_id)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to fetch entry {entry_id}: {exc}") from exc
    return _to_detail(raw)


@mcp.tool()
async def get_entry_by_path(full_path: str) -> EntryDetail:
    """Resolve a Laserfiche full path to an entry.

    Path uses backslashes, e.g. \\Imports\\2024\\Onboarding\\Smith,John.
    Useful when the user references a location by name rather than ID.
    """
    try:
        raw = await _client().get_entry_by_path(full_path)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to resolve path '{full_path}': {exc}") from exc
    return _to_detail(raw)


@mcp.tool()
async def get_field_values(entry_id: int) -> list[FieldValue]:
    """Read all template field values assigned to an entry.

    Returns one FieldValue per field on the entry's template, including
    multi-value fields (where `is_multi_value=true` and `values` has multiple
    entries). Empty fields are typically omitted by the Repository API.
    """
    try:
        raw = await _client().get_field_values(entry_id)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to fetch fields for entry {entry_id}: {exc}") from exc
    return _to_fields(raw)


@mcp.tool()
async def get_document_text(entry_id: int, max_chars: int = 50_000) -> str:
    """Download an electronic document's content as text.

    Only works for entries that are electronic documents (PDFs, Office files,
    text files, etc.) — check `is_electronic_document` on the entry detail
    first if unsure. Output is truncated to `max_chars` characters to keep
    context usage bounded; default 50,000 chars.

    Note: this returns raw bytes decoded as UTF-8 with replacement. For PDFs
    and Office docs you'll get binary noise — extracting clean text from those
    formats is out of scope for v1; use the Laserfiche text extraction
    workflow upstream and read the extracted text instead.
    """
    try:
        content = await _client().get_entry_content(entry_id)
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to download entry {entry_id}: {exc}") from exc

    text = content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated, {len(text) - max_chars} chars omitted]"
    return text


# --- Entrypoint --------------------------------------------------------------


def main() -> None:
    """Console-script entrypoint registered in pyproject.toml."""
    logging.basicConfig(level=logging.INFO)
    settings = _get_settings()
    if settings.read_only:
        logger.info("Starting laserfiche-mcp in READ-ONLY mode.")
    else:
        logger.warning(
            "Starting laserfiche-mcp with WRITE tools enabled. "
            "Write tools will be added in v1.1 — for now this flag has no effect."
        )
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
