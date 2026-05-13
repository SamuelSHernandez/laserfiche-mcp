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

import asyncio
import base64
import io
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from . import __version__
from .auth import build_auth_strategy
from .client import LaserficheClient, LaserficheError
from .config import Settings
from .models import (
    EntryDetail,
    FieldValue,
    SearchAttempt,
    SearchNaturalResponse,
    SearchResults,
    TemplateHint,
)
from .search import (
    LF_GRAMMAR_REFERENCE,
    build_candidate_queries,
    repair_escape_quotes,
    repair_wildcard_name,
)

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


def _clamp_search_page_size(requested: int | None) -> int:
    """Hard cap on search_natural pagination, separate from list/folder cap.

    Some self-hosted SimpleSearches implementations 400 on $top values above
    a server-internal limit, so this cap defaults lower (LF_MAX_PAGE_SIZE).
    """
    settings = _get_settings()
    value = settings.max_results_default if requested is None else requested
    return min(max(1, value), settings.max_page_size)


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


async def _sample_folder_templates(
    client: LaserficheClient,
    folder_path: str | None,
) -> tuple[list[TemplateHint], list[str]]:
    """Sample up to ~10 entries from ``folder_path`` and collect template hints.

    Returns ``(templates, notes)``. ``notes`` carries any caveats (folder
    missing, folder empty, individual fetch failures) that the host LLM
    should see in the guidance response.
    """
    notes: list[str] = []

    folder_id: int = 1  # Repository root
    if folder_path:
        try:
            folder = await client.get_entry_by_path(folder_path)
        except LaserficheError as exc:
            notes.append(
                f"Could not resolve folder_path {folder_path!r}: {exc}. "
                "Sampled from the repository root instead."
            )
        else:
            resolved = folder.get("id")
            if resolved and resolved > 0:
                folder_id = resolved
            else:
                notes.append(
                    f"folder_path {folder_path!r} resolved to an empty entry; "
                    "sampled from the repository root instead."
                )

    try:
        children = await client.list_folder(folder_id, max_results=10)
    except LaserficheError as exc:
        notes.append(f"Could not list folder {folder_id}: {exc}")
        return [], notes

    entries = children.get("value") or []
    if not entries:
        notes.append(f"Folder {folder_id} had no children to sample.")
        return [], notes

    entry_ids: list[int] = [e["id"] for e in entries if e.get("id")]
    detail_results = await asyncio.gather(
        *[client.get_entry(eid) for eid in entry_ids],
        return_exceptions=True,
    )

    # Map template_name -> a representative entry_id we can fetch fields from.
    template_sample: dict[str, int] = {}
    for entry_id, detail in zip(entry_ids, detail_results, strict=False):
        if isinstance(detail, BaseException):
            continue
        name = detail.get("templateName") or detail.get("TemplateName")
        if name:
            template_sample.setdefault(name, entry_id)

    if not template_sample:
        notes.append(
            "Sampled entries had no template assigned, so no template "
            "hints could be derived. Field-search queries will need to "
            "be authored without auto-discovery."
        )
        return [], notes

    field_results = await asyncio.gather(
        *[client.get_field_values(eid) for eid in template_sample.values()],
        return_exceptions=True,
    )

    templates: list[TemplateHint] = []
    for template_name, fr in zip(template_sample.keys(), field_results, strict=False):
        if isinstance(fr, BaseException):
            templates.append(TemplateHint(template_name=template_name, field_names=[]))
            continue
        raw_fields = fr.get("value") or fr.get("Value") or []
        field_names: list[str] = []
        for f in raw_fields:
            fname = f.get("fieldName") or f.get("FieldName")
            if fname and fname not in field_names:
                field_names.append(fname)
        templates.append(
            TemplateHint(template_name=template_name, field_names=field_names)
        )

    return templates, notes


def _follow_up_hint(folder_path: str | None, max_results: int) -> str:
    folder_arg = f', folder_path="{folder_path}"' if folder_path else ''
    return (
        'Pick one of the candidate_queries (or refine it) and call '
        f'search_natural(question=..., lf_query="<query>"{folder_arg}, '
        f'max_results={max_results}).'
    )


@mcp.tool()
async def search_natural(
    question: str,
    lf_query: str | None = None,
    folder_path: str | None = None,
    max_results: int = 50,
    fuzzy: bool = True,
) -> SearchNaturalResponse:
    """Two-mode search: guidance first, then execution with automatic repair.

    Most Laserfiche servers reject malformed query syntax with a generic HTTP
    400. This tool gives the host LLM a structured way to author a working
    query without trial-and-error against the user.

    **Mode A — ``lf_query`` omitted**
        Returns ``mode="guidance"`` with:
          * ``grammar`` — the Laserfiche search syntax reference this server
            understands, with examples.
          * ``discovered_templates`` — template names and field names sampled
            from ``folder_path`` (or the repository root). Use these to
            author template-field queries like
            ``{[Personnel]:[Last Name]="Smith"}``.
          * ``candidate_queries`` — up to 3 starter queries built from the
            question's keywords. Pick one or refine it, then call again with
            ``lf_query``.
          * ``follow_up`` — the exact follow-up call shape.

    **Mode B — ``lf_query`` provided**
        Executes the query and returns ``mode="results"`` (or
        ``mode="error"`` with structured detail). On HTTP 400, up to two
        automatic repairs are attempted:

          1. Escape unescaped ``"`` characters inside ``="..."`` value spans.
          2. Wrap ``Name="value"`` values in ``*`` wildcards (only when
             ``fuzzy=True`` and the value has no wildcard).

        Each attempt is recorded in ``attempts`` on the error response.

    **Pagination**
        ``max_results`` is clamped to ``LF_MAX_PAGE_SIZE`` (default 100).
        Some self-hosted SimpleSearches implementations 400 on larger
        ``$top`` values, so the cap is lower than the list-folder ceiling.
        When ``next_link`` is null but the result count hit the effective
        cap, ``pagination_unknown=true`` is surfaced — there may be more
        results, the server just didn't say.

    **What this tool does NOT do**
        It does not silently fall back to folder traversal. If both repairs
        still 400, you get a structured error so the user knows search failed
        and the host LLM can author a fresh query.
    """
    effective_max = _clamp_search_page_size(max_results)
    client = _client()

    # --- Mode A: guidance ---------------------------------------------------
    if lf_query is None:
        templates, notes = await _sample_folder_templates(client, folder_path)
        if effective_max != max_results:
            notes.append(
                f"max_results was clamped from {max_results} to "
                f"{effective_max} by LF_MAX_PAGE_SIZE."
            )
        candidates = build_candidate_queries(question, folder_path, templates)
        return SearchNaturalResponse(
            mode="guidance",
            question=question,
            folder_path=folder_path,
            grammar=LF_GRAMMAR_REFERENCE,
            discovered_templates=templates,
            candidate_queries=candidates,
            follow_up=_follow_up_hint(folder_path, effective_max),
            notes=notes,
            effective_max_results=effective_max,
        )

    # --- Mode B: execute with repair ----------------------------------------
    attempts: list[SearchAttempt] = []
    repairs_applied: list[str] = []
    current_query = lf_query
    current_repair: str | None = None

    # At most three calls: original + escape_quotes + wildcard_wrap.
    for _ in range(3):
        try:
            raw = await client.search_entries(
                current_query, max_results=effective_max,
            )
        except LaserficheError as exc:
            attempts.append(
                SearchAttempt(
                    query=current_query,
                    repair=current_repair,
                    status_code=exc.status_code,
                    error_body=str(exc),
                )
            )
            if exc.status_code != 400:
                # Non-400 errors are not in the repair contract — surface immediately.
                return SearchNaturalResponse(
                    mode="error",
                    question=question,
                    lf_query=lf_query,
                    attempts=attempts,
                    final_error=str(exc),
                    next_action=(
                        "Server returned a non-400 error. Check repository "
                        "permissions, network reachability, and credentials "
                        "before retrying."
                    ),
                )

            # Try repair 1 first if not already tried.
            if "escape_quotes" not in repairs_applied:
                repaired = repair_escape_quotes(current_query)
                if repaired is not None:
                    repairs_applied.append("escape_quotes")
                    current_query = repaired
                    current_repair = "escape_quotes"
                    continue

            # Then repair 2 if fuzzy and not yet tried.
            if fuzzy and "wildcard_wrap" not in repairs_applied:
                repaired = repair_wildcard_name(current_query)
                if repaired is not None:
                    repairs_applied.append("wildcard_wrap")
                    current_query = repaired
                    current_repair = "wildcard_wrap"
                    continue

            # No more repairs available.
            return SearchNaturalResponse(
                mode="error",
                question=question,
                lf_query=lf_query,
                attempts=attempts,
                final_error=str(exc),
                next_action=(
                    "All automatic repairs are exhausted. Read the grammar "
                    "(call search_natural without lf_query) and author a "
                    "new query — common fixes: quote string values, use "
                    "wildcards in Name= clauses, scope with "
                    "{LF:LookIn=\"\\\\path\"}, or switch to a template "
                    "field clause like {[Template]:[Field]=\"value\"}."
                ),
            )

        # Success path.
        results = SearchResults.from_api(raw)
        pagination_unknown = (
            results.next_link is None
            and len(results.entries) >= effective_max
        )
        return SearchNaturalResponse(
            mode="results",
            question=question,
            lf_query=current_query,
            repairs_applied=repairs_applied,
            entries=results.entries,
            total_count=results.total_count,
            next_link=results.next_link,
            pagination_unknown=pagination_unknown,
            effective_max_results=effective_max,
        )

    # Loop exhausted without returning — should not happen, but be safe.
    return SearchNaturalResponse(
        mode="error",
        question=question,
        lf_query=lf_query,
        attempts=attempts,
        final_error="search_natural exhausted repair attempts without a definitive outcome.",
        next_action="Retry with a refined lf_query.",
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

    **On v1 servers**: this endpoint does not exist. Use
    ``get_document_edoc(entry_id, mode="text")`` instead — it fetches the
    raw edoc and extracts text client-side via pypdf for PDFs, or decodes
    directly for text/* MIME types.
    """
    try:
        content = await _client().export_entry(entry_id, part="Text")
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to download text for entry {entry_id}: {exc}") from exc

    text = content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated, {len(text) - max_chars} chars omitted]"
    return text


def _extract_pdf_text(content: bytes, char_limit: int) -> dict[str, Any]:
    """Run pypdf over a PDF byte string.

    Returns a result dict on success or an error dict on extraction failure
    (encrypted PDF, malformed PDF, pypdf-internal exception). The caller
    decides how to wrap this into the tool response.
    """
    try:
        import pypdf  # imported lazily so users on v2 don't need to install it
    except ImportError as exc:
        return {
            "error": "pypdf_unavailable",
            "message": (
                "pypdf is required for mode='text' on PDF documents. "
                "Install with `pip install pypdf` or `uv add pypdf`."
            ),
            "exception": repr(exc),
        }

    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 — pypdf raises various subclasses
        return {
            "error": "pdf_open_failed",
            "exception_class": type(exc).__name__,
            "message": str(exc),
        }

    if reader.is_encrypted:
        return {
            "error": "pdf_encrypted",
            "message": (
                "PDF is password-protected; text extraction is not possible. "
                "Use mode='bytes' if you need the raw file."
            ),
        }

    pages_total = len(reader.pages)
    chunks: list[str] = []
    pages_extracted = 0
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
            pages_extracted += 1
        except Exception as exc:  # noqa: BLE001 — partial extraction is acceptable
            chunks.append(f"[page extraction failed: {type(exc).__name__}]")

    full = "\n".join(chunks)
    truncated = len(full) > char_limit
    if truncated:
        full = full[:char_limit] + f"\n\n[truncated, {len(chunks)} pages total]"

    return {
        "ok": True,
        "text": full,
        "pages_total": pages_total,
        "pages_extracted": pages_extracted,
        "truncated": truncated,
    }


@mcp.tool()
async def get_document_edoc(
    entry_id: int,
    mode: Literal["info", "bytes", "text"] = "info",
    max_bytes: int | None = None,
    text_char_limit: int = 50_000,
) -> dict[str, Any]:
    """Download or inspect a document's raw electronic file (edoc).

    Three modes:

    * ``"info"`` *(default)* — fetches the edoc but returns only its size
      and content-type, plus a hint. No bytes enter the model's context.
      Cheapest mode; safe for any document type.
    * ``"bytes"`` — returns the edoc as base64-encoded bytes alongside
      content-type and size. Refused if the edoc exceeds
      ``LF_EDOC_MAX_BYTES`` (default 25 MB). Pass ``max_bytes`` to override
      the cap inline.
    * ``"text"`` — fetches the edoc and extracts text **server-side**:
        - ``application/pdf`` → extracted via pypdf, page by page, then
          truncated to ``text_char_limit``. Includes ``pages_total``,
          ``pages_extracted``, ``truncated``.
        - ``text/*`` → decoded directly as UTF-8 (lossy on bad bytes).
        - Anything else (docx, xlsx, images) → returns a structured error
          naming the content-type and suggesting ``mode="bytes"`` for
          client-side handling. OCR is not attempted.
        - pypdf extraction failure (encrypted, malformed) → structured
          error with the underlying exception class.

    This is the recommended path for "summarize the PDF" workflows on v1
    servers, where ``get_document_text`` has no endpoint to call.
    """
    settings = _get_settings()
    effective_cap = max_bytes if max_bytes is not None else settings.edoc_max_bytes

    try:
        content, content_type = await _client().export_entry_with_meta(
            entry_id, part="Edoc",
        )
    except LaserficheError as exc:
        raise RuntimeError(f"Failed to download edoc for entry {entry_id}: {exc}") from exc

    byte_size = len(content)

    if mode == "info":
        return {
            "entry_id": entry_id,
            "mode": "info",
            "byte_size": byte_size,
            "content_type": content_type,
            "hint": (
                "Raw bytes were fetched but not returned to the model. "
                "Use mode='bytes' for the base64 payload or mode='text' "
                "for server-side extracted text."
            ),
        }

    if byte_size > effective_cap and mode in ("bytes", "text"):
        return {
            "entry_id": entry_id,
            "mode": mode,
            "error": "size_exceeds_cap",
            "byte_size": byte_size,
            "max_bytes": effective_cap,
            "content_type": content_type,
            "message": (
                f"Edoc is {byte_size} bytes, which exceeds the {effective_cap}-byte cap. "
                "Pass max_bytes=<larger value> or raise LF_EDOC_MAX_BYTES "
                "if you really need this document."
            ),
        }

    if mode == "bytes":
        return {
            "entry_id": entry_id,
            "mode": "bytes",
            "byte_size": byte_size,
            "content_type": content_type,
            "data_base64": base64.b64encode(content).decode("ascii"),
        }

    # mode == "text"
    ct_lower = (content_type or "").lower().split(";")[0].strip()
    if ct_lower == "application/pdf":
        result = _extract_pdf_text(content, text_char_limit)
        if result.get("ok"):
            return {
                "entry_id": entry_id,
                "mode": "text",
                "content_type": content_type,
                "byte_size": byte_size,
                "text": result["text"],
                "pages_total": result["pages_total"],
                "pages_extracted": result["pages_extracted"],
                "truncated": result["truncated"],
            }
        # Extraction failed.
        return {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
            "error": result.get("error", "pdf_extraction_failed"),
            "message": result.get("message"),
            "exception_class": result.get("exception_class"),
            "hint": "Try mode='bytes' to retrieve the raw PDF for client-side handling.",
        }

    if ct_lower.startswith("text/"):
        text = content.decode("utf-8", errors="replace")
        truncated = len(text) > text_char_limit
        if truncated:
            text = text[:text_char_limit] + "\n\n[truncated]"
        return {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
            "text": text,
            "truncated": truncated,
        }

    return {
        "entry_id": entry_id,
        "mode": "text",
        "content_type": content_type,
        "byte_size": byte_size,
        "error": "unsupported_content_type",
        "message": (
            f"Cannot extract text from content-type {content_type!r}. "
            "Server-side text extraction is implemented only for "
            "application/pdf and text/*. Use mode='bytes' to download "
            "the file and handle it client-side."
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
    # LF_READ_ONLY is reserved for future write tools. v1.1 still ships
    # read-only, so the flag has no behavioral effect today; we just log it
    # neutrally so operators see the value they configured.
    logger.info(
        "Starting laserfiche-mcp (read_only=%s).", settings.read_only,
    )

    try:
        mcp.run()  # stdio transport by default
    except KeyboardInterrupt:
        # Don't dump a traceback for ordinary Ctrl-C exits.
        logger.info("laserfiche-mcp stopped.")


if __name__ == "__main__":
    main()
