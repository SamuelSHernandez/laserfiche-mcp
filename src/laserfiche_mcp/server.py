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

import argparse
import asyncio
import base64
import io
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from . import __version__, confirmation, permissions
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
) -> dict[str, Any]:
    """Run a raw Laserfiche search query and return matching entries.

    Use when you already know how to express the search in Laserfiche query
    syntax. If the user describes what they want in natural language and you
    are unsure how to translate, prefer ``search_natural`` (which asks the
    server for the available templates and field names first). For a simple
    name-pattern lookup, ``search_by_name`` is the cheaper option.

    Query syntax cheat sheet:

    - ``{LF:Name="Onboarding*"}`` — name pattern (``*`` and ``?`` wildcards)
    - ``{[Missionary Application]:[Last Name]="Smith"}`` — field on template
    - ``{LF:LookIn="\\Imports\\2024"}`` — restrict to a folder subtree
    - Combine with ``&`` (AND) / ``|`` (OR), e.g.
      ``{LF:Name="*.pdf"} & {[Application]:[Status]="Approved"}``

    Args:
        query: A Laserfiche search expression. Quote string values with
            double quotes; escape inner quotes by doubling them.
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``,
            typically 200).

    Returns: ``SearchResults`` with ``entries`` (id, name, entry_type,
    full_path), ``total_count``, and ``next_link``. Drill in with
    ``get_entry`` or ``get_field_values``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}`` instead
    of raising. Slugs you might see here: ``server_error`` (most common, the
    SimpleSearches endpoint is fragile on some self-hosted builds — see
    ``search_natural`` for a more resilient path), ``auth_failed``,
    ``rate_limited``. Full taxonomy in docs/error-contract.md.
    """
    try:
        raw = await _client().search_entries(
            query, max_results=_clamp_max_results(max_results),
        )
    except LaserficheError as exc:
        return _classify_lf_error("search", exc)

    return SearchResults.from_api(raw).model_dump()


@mcp.tool()
async def search_by_name(
    name_pattern: str,
    in_folder_path: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    """Find entries by file/folder name pattern, optionally scoped to a folder path.

    Use when the user is searching by name and the full Laserfiche query
    syntax is overkill. This wraps ``search_entries`` with a
    ``{LF:Name="..."}`` (plus optional ``{LF:LookIn="..."}``) clause built
    for you.

    Args:
        name_pattern: A name with optional wildcards — ``*`` matches any
            sequence, ``?`` matches one character. Examples:
            ``"Onboarding*"`` (starts-with), ``"*.pdf"`` (ends-with),
            ``"Smith,?"`` (exactly one char after the comma).
        in_folder_path: Backslash-delimited Laserfiche path to scope the
            search to. Example: ``"\\Imports\\2024"``.
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).

    Returns: same ``SearchResults`` shape as ``search_entries``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``. See
    docs/error-contract.md. Note that SimpleSearches is the same fragile
    endpoint behind ``search_entries`` — fall back to ``search_natural``
    if you get repeated ``server_error`` results.
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
        return _classify_lf_error("search", exc)

    return SearchResults.from_api(raw).model_dump()


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


def _dump_search_natural(**kwargs: Any) -> dict[str, Any]:
    """Build a SearchNaturalResponse and dump it to a dict.

    The tool wrapper returns a plain dict so FastMCP's runtime validation
    accepts both success and error shapes uniformly. The model is kept for
    its construction-time validation and self-documentation.
    """
    return SearchNaturalResponse(**kwargs).model_dump()


@mcp.tool()
async def search_natural(
    question: str,
    lf_query: str | None = None,
    folder_path: str | None = None,
    max_results: int = 50,
    fuzzy: bool = True,
) -> dict[str, Any]:
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

    **On failure**
        Mode B returns ``{mode: "error", attempts: [...]}`` with the full
        repair history visible — each attempt records the query, the repair
        tag applied, the HTTP status, and the server's error body, enough
        context for the LLM to write a different query. Other failures
        (auth, rate limit, network) come back via the generic error
        contract; see docs/error-contract.md.
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
        return _dump_search_natural(
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
                return _dump_search_natural(
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
            return _dump_search_natural(
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
        return _dump_search_natural(
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
    return _dump_search_natural(
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
) -> dict[str, Any]:
    """List the immediate children (documents and subfolders) of a folder by ID.

    Use this for browse-style navigation when the user references a known
    folder. The root folder is typically ID 1 — start there if you have
    nothing else. To navigate from a path string, resolve it first with
    ``get_entry_by_path``. To search across the whole repo, use
    ``search_natural`` or ``search_entries``.

    Args:
        folder_id: Integer entry ID of the parent folder.
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination. Combine with ``max_results``
            to walk a large folder in chunks; check ``next_link`` to know
            when to stop.

    Returns: ``SearchResults`` with ``entries``, ``total_count`` (server
    fills it only when the build supports ``$count``), and ``next_link``.
    Each entry has id, name, entry_type, full_path, creation_time, and
    last_modified_time. Drill into a single entry with ``get_entry`` or
    ``get_field_values``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "folder_id": <int>, ...}``. Common slugs: ``not_found`` (folder ID
    doesn't exist), ``auth_failed`` (no read permission).
    """
    try:
        raw = await _client().list_folder(
            folder_id,
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return _classify_lf_error("list_folder", exc, extra={"folder_id": folder_id})

    return SearchResults.from_api(raw).model_dump()


@mcp.tool()
async def get_entry(entry_id: int) -> dict[str, Any]:
    """Fetch metadata for a single entry by ID.

    Use this once you have an entry ID (from search, ``list_folder``, or
    ``get_entry_by_path``) and need the entry's full metadata: name, type
    (Folder vs Document), full path, parent ID, template name, page count
    (for paginated documents), and timestamps.

    This does NOT return field values — for those, call ``get_field_values``.
    This does NOT return document content — for that, call
    ``get_document_edoc`` (``mode="text"`` for extracted text, ``mode="bytes"``
    for the raw file).

    Args:
        entry_id: Integer entry ID.

    Returns: ``EntryDetail`` (id, name, entry_type, parent_id, full_path,
    template_name, page_count, is_electronic_document, extension, creation
    time, last modified time).

    On failure: returns ``{"mode": "error", "error": <slug>, "entry_id": <int>, ...}``.
    Common slugs: ``not_found``, ``auth_failed``.
    """
    try:
        raw = await _client().get_entry(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("get_entry", exc, entry_id=entry_id)
    return EntryDetail.from_api(raw).model_dump()


@mcp.tool()
async def get_entry_by_path(full_path: str) -> dict[str, Any]:
    """Resolve a backslash-delimited Laserfiche path to its entry.

    Use this when the user refers to a location by its name path rather
    than an ID — typical when they paste a path from the Laserfiche web
    client, or when you've authored a path from a known folder structure.
    Once resolved, the returned ``id`` feeds into ``list_folder``,
    ``get_entry``, ``get_field_values``, etc.

    Args:
        full_path: Path from the repository root, backslash-separated.
            Example: ``"\\Imports\\2024\\Onboarding\\Smith,John"``. Forward
            slashes are also accepted.

    Returns: ``EntryDetail`` — same shape as ``get_entry``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "full_path": <str>, ...}``. Common slugs: ``not_found`` (no entry at
    that path), ``auth_failed``.

    **Caveat**: some self-hosted v1 builds return an empty sentinel entry
    (id=0, entry_type="Unknown") for paths they can't resolve, rather than
    a clean 404. Treat ``id=0`` from this tool as "not found" even when
    the response wasn't classified as an error.
    """
    try:
        raw = await _client().get_entry_by_path(full_path)
    except LaserficheError as exc:
        return _classify_lf_error("get_entry_by_path", exc, extra={"full_path": full_path})
    return EntryDetail.from_api(raw).model_dump()


@mcp.tool()
async def get_field_values(entry_id: int) -> dict[str, Any]:
    """Read the template field values currently on an entry.

    Use after you have an entry ID and need the metadata fields the user
    is asking about — e.g. "what's the status of this form?", "who's the
    assigned reviewer?", "when was this signed?". For the entry's own
    properties (name, type, path), use ``get_entry`` instead.

    Args:
        entry_id: Integer entry ID.

    Returns: ``{"values": [...]}`` — a list of field-value descriptors
    under the ``values`` key. Each item has ``field_name``, ``values``
    (always a list, even for single-value fields), ``field_type``,
    ``is_multi_value``, and ``is_required``. Empty / unset fields are
    typically omitted by the Repository API rather than returned with
    empty values, so an empty list usually means the entry has no
    template assigned.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``, ``auth_failed``.
    """
    try:
        raw = await _client().get_field_values(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("get_field_values", exc, entry_id=entry_id)
    return {
        "entry_id": entry_id,
        "values": [fv.model_dump() for fv in FieldValue.list_from_api(raw)],
    }


@mcp.tool()
async def get_document_text(entry_id: int, max_chars: int = 50_000) -> dict[str, Any]:
    """Download a document's server-extracted text (v2-only).

    Use for "summarize this document", "what does this say", or any other
    task that needs the readable contents of a document rather than the
    raw binary. The text comes from Laserfiche's own extraction pipeline
    (OCR for image documents, upstream extraction for office files), so
    you get clean text without having to parse a PDF yourself.

    **v1 servers do not expose this endpoint.** If your deployment is on
    v1 (the default), this tool returns a structured error at the client
    layer. Use ``get_document_edoc(entry_id, mode="text")`` instead — it
    fetches the raw edoc and extracts text client-side (pypdf for PDFs,
    direct decode for ``text/*`` MIME types).

    Args:
        entry_id: Integer entry ID of an electronic document (not a folder).
        max_chars: Truncate the returned text after this many characters
            (default 50,000). The response's ``truncated`` field signals
            whether truncation occurred.

    Returns: ``{"entry_id": <int>, "text": <str>, "char_count": <int>,
    "truncated": <bool>}`` on success.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no extracted text), ``method_not_allowed`` /
    ``server_error`` (v1 server — fall back to ``get_document_edoc``).
    """
    try:
        content = await _client().export_entry(entry_id, part="Text")
    except LaserficheError as exc:
        return _classify_lf_error("get_document_text", exc, entry_id=entry_id)

    text = content.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {
        "entry_id": entry_id,
        "text": text,
        "char_count": len(text),
        "truncated": truncated,
    }


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

    The recommended path for reading document content on v1 servers
    (``get_document_text`` has no endpoint to call there). Three modes
    trade off cost vs. depth:

    Args:
        entry_id: Integer entry ID. Must point to an electronic document,
            not a folder.
        mode:
            ``"info"`` *(default)* — fetches the edoc but returns only its
            size and content-type, plus a hint. No bytes enter the model's
            context. Cheapest; safe to call on anything as a first probe.

            ``"bytes"`` — returns the edoc as base64-encoded bytes plus
            content-type and size. Refused if the edoc exceeds
            ``LF_EDOC_MAX_BYTES`` (default 25 MB) — see ``max_bytes``.

            ``"text"`` — extracts readable text server-side:

            - ``application/pdf`` → pypdf, page by page, truncated to
              ``text_char_limit``. Response includes ``pages_total``,
              ``pages_extracted``, ``truncated``.
            - ``text/*`` → decoded directly as UTF-8 (replacement chars
              on bad bytes).
            - Anything else (.docx, .xlsx, images, etc.) → structured
              error naming the content-type and suggesting ``mode="bytes"``
              for client-side handling. OCR is not attempted.
            - Encrypted or malformed PDFs → structured error with the
              underlying exception class.
        max_bytes: Per-call override for ``LF_EDOC_MAX_BYTES``. Use to
            raise the cap for a specific large document without changing
            the server-wide default.
        text_char_limit: Truncate extracted text after this many
            characters (default 50,000). Truncation is signalled by the
            ``truncated`` field, NOT a marker in the text itself.

    Returns: Always a dict. Shape depends on ``mode`` — see above.
    On size-cap refusal, response contains ``error="size_exceeds_cap"``
    plus ``byte_size`` and ``max_bytes`` so the LLM can decide whether
    to raise the cap and retry.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no edoc), ``auth_failed``.
    """
    settings = _get_settings()
    effective_cap = max_bytes if max_bytes is not None else settings.edoc_max_bytes

    try:
        content, content_type = await _client().export_entry_with_meta(
            entry_id, part="Edoc",
        )
    except LaserficheError as exc:
        return _classify_lf_error("get_document_edoc", exc, entry_id=entry_id)

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


# --- Read tools: definitions, audit reasons, async tasks ---------------------


@mcp.tool()
async def list_repositories() -> dict[str, Any]:
    """List the repositories this account can reach on the server.

    Useful for confirming which repository the server is pointed at and
    for discovering alternate repositories the same account can access.
    On a healthy build, returns the server's raw OData listing with
    ``value``: ``[{repoId, displayName, ...}, ...]``.

    **Endpoint variability**: some self-hosted Laserfiche builds disable
    the ``/Repositories`` endpoint entirely. When the call fails, this
    tool does NOT raise — it returns:

    ```json
    {
      "mode": "fallback",
      "operation": "list_repositories",
      "warning": "Server's /Repositories endpoint returned an error ...",
      "server_error": { /* classified error per docs/error-contract.md */ },
      "value": [{"repoId": "<LF_REPOSITORY_ID>", "displayName": null,
                 "is_configured": true}]
    }
    ```

    Branch on ``mode == "fallback"`` if you need to distinguish a partial
    answer from a full enumeration. The fallback's single item is the
    repo this server is configured against (``LF_REPOSITORY_ID``), so any
    follow-up tool call will work against it.
    """
    try:
        raw = await _client().list_repositories()
    except LaserficheError as exc:
        settings = _get_settings()
        return {
            "mode": "fallback",
            "operation": "list_repositories",
            "warning": (
                f"Server's /Repositories endpoint returned an error "
                f"(status={exc.status_code}). Returning the configured "
                f"repository from LF_REPOSITORY_ID; other repos on this "
                f"server are not enumerable from this build."
            ),
            "server_error": _classify_lf_error("list_repositories", exc),
            "value": [
                {
                    "repoId": settings.repository_id,
                    "displayName": None,
                    "is_configured": True,
                }
            ],
        }
    return raw


@mcp.tool()
async def list_field_definitions(
    max_results: int | None = None,
    skip: int = 0,
) -> dict[str, Any]:
    """List every field definition in the repository.

    Use before authoring a field-based search query or preparing a field
    update — the response tells you which fields exist, their types
    (``String``, ``ShortInteger``, ``List``, ``Date``, ...), whether they
    accept multi-value, whether they're required at the repository level,
    and (for ``List`` fields) the allowed values.

    Independent fields and template-scoped fields are both returned.
    Combine with ``list_template_definitions`` to see which fields belong
    to which template.

    Args:
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination through large repositories.

    Returns: Server's raw OData listing with ``value`` (list of field
    definitions). Each item includes ``id``, ``name``, ``fieldType``,
    ``isRequired``, ``isMultiValue``, ``listValues``, ``defaultValue``,
    ``length``, ``constraint``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _client().list_field_definitions(
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return _classify_lf_error("list_field_definitions", exc)
    return raw


@mcp.tool()
async def list_tag_definitions(
    max_results: int | None = None,
    skip: int = 0,
) -> dict[str, Any]:
    """List every tag definition in the repository.

    Use before calling ``set_tags`` / ``merge_tags`` to confirm a tag
    exists — the server rejects tags that aren't defined here. Tags are
    a flat namespace in Laserfiche, distinct from template fields.

    Args:
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination.

    Returns: Server's raw OData listing with ``value`` (list of tag
    definitions). Each item has ``id``, ``name``, and ``isSecurityTag``.
    Many repositories ship with no tags defined; an empty ``value`` is
    normal.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _client().list_tag_definitions(
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return _classify_lf_error("list_tag_definitions", exc)
    return raw


@mcp.tool()
async def list_template_definitions(
    template_name: str | None = None,
    max_results: int | None = None,
    skip: int = 0,
) -> dict[str, Any]:
    """List template definitions in the repository.

    Use to discover which templates exist before calling ``assign_template``.
    Pass ``template_name`` to fetch a single template by name (the same
    listing, filtered server-side).

    Args:
        template_name: If set, return only the template with this exact
            name. Case-sensitive on most builds.
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination.

    Returns: Server's raw OData listing with ``value``. Each item has
    ``id``, ``name``, ``displayName``, ``description``, ``fieldCount``,
    and ``color``. This response does NOT enumerate the fields ON the
    template — use ``list_field_definitions`` to inspect those (they're
    the ones with ``isRequired=true`` when scoped to the template).

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _client().list_template_definitions(
            template_name=template_name,
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return _classify_lf_error("list_template_definitions", exc)
    return raw


@mcp.tool()
async def list_link_definitions(
    max_results: int | None = None,
    skip: int = 0,
) -> dict[str, Any]:
    """List the entry-link type definitions available on this repository.

    Use before calling ``set_links`` — you need a ``linkTypeId`` from
    this listing to construct a valid link. Each link type is directed:
    it has a ``sourceLabel`` (how the relationship reads from the source
    entry) and a ``targetLabel`` (how it reads from the target).

    Args:
        max_results: Page size (default 25).
        skip: 0-indexed offset for pagination.

    Returns: Server's raw OData listing with ``value``. Each item has
    ``linkTypeId``, ``sourceLabel``, ``targetLabel``, and
    ``linkTypeDescription``. Common defaults include ``"Supersedes" /
    "Superseded by"`` and ``"Attachment" / "Message"``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _client().list_link_definitions(
            max_results=_clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return _classify_lf_error("list_link_definitions", exc)
    return raw


@mcp.tool()
async def get_audit_reasons() -> dict[str, Any]:
    """Return the audit-reason codes the authenticated user is allowed to supply.

    Use before ``delete_entry`` or ``get_document_edoc`` (with export
    auditing) when ``LF_REQUIRE_AUDIT_REASON=true`` or when the user is
    asking for an audited delete. The response is grouped by operation
    type — pick an ID from the correct group.

    Returns: Dict shaped roughly as ``{"deleteEntry": [{id, name, ...}],
    "exportDocument": [...], ...}``. Each item has ``id``, ``name``, and
    ``description``. The ``id`` is what you pass to ``delete_entry`` as
    ``audit_reason_id``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    Common slugs: ``auth_failed`` if the account isn't permitted to audit.
    """
    try:
        raw = await _client().get_audit_reasons()
    except LaserficheError as exc:
        return _classify_lf_error("get_audit_reasons", exc)
    return raw


@mcp.tool()
async def get_task_status(operation_token: str) -> dict[str, Any]:
    """Look up the status of an async operation by its token.

    The async tools (``delete_entry``, ``copy_entry``, sometimes
    ``import_document``) return an ``operation_token`` instead of the
    final result — call this to check whether the operation finished.
    For "wait until done" semantics, use ``wait_for_task`` instead so
    you don't have to write a polling loop.

    Args:
        operation_token: The string token returned by the originating
            async tool.

    Returns: Server's task payload — ``operationToken``,
    ``operationType``, ``percentComplete``, ``status`` (one of
    ``NotStarted``, ``InProgress``, ``Completed``, ``Failed``,
    ``Canceled``), ``redirectUri`` (set when the op produced a new
    entry, e.g. after a copy), ``entryId`` (the resulting entry's ID
    when applicable), ``errors`` (list — empty on success), and
    timestamps.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "operation_token": <str>, ...}``. Common slugs: ``not_found``
    (token unknown — usually expired or from a different server
    instance), ``auth_failed``.
    """
    try:
        raw = await _client().get_task_status(operation_token)
    except LaserficheError as exc:
        return _classify_lf_error(
            "get_task_status", exc,
            extra={"operation_token": operation_token},
        )
    return raw


@mcp.tool()
async def wait_for_task(
    operation_token: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Block until an async operation reaches a terminal state.

    Preferred over manual polling with ``get_task_status``. Returns
    quickly when the op is fast; otherwise polls at ``poll_interval_seconds``
    until ``Completed``, ``Failed``, or ``Canceled`` — or until
    ``timeout_seconds`` is reached, in which case the last observed
    status is returned with ``timed_out=true`` so the caller can decide
    whether to keep waiting.

    Args:
        operation_token: Token from the originating async tool.
        timeout_seconds: Maximum time to wait (default 60). Set higher
            for large folder deletes or large copies.
        poll_interval_seconds: Delay between status checks (default 1.0).
            Bounded below at 0.1s.

    Returns: Same payload as ``get_task_status``, with an added
    ``timed_out`` boolean indicating whether the wait ended on timeout.

    On failure: if a poll call fails mid-wait, returns
    ``{"mode": "error", "error": <slug>, "operation_token": <str>, ...}``.
    Common slugs: ``not_found`` (token invalidated by server restart),
    ``auth_failed``.
    """
    import time as _time
    deadline = _time.monotonic() + max(1, timeout_seconds)
    last: dict[str, Any] = {}
    while True:
        try:
            last = await _client().get_task_status(operation_token)
        except LaserficheError as exc:
            return _classify_lf_error(
                "wait_for_task", exc,
                extra={"operation_token": operation_token},
            )
        status = (last.get("status") or last.get("Status") or "").lower()
        if status in {"completed", "failed", "canceled"}:
            return {**last, "timed_out": False}
        if _time.monotonic() >= deadline:
            return {**last, "timed_out": True}
        await asyncio.sleep(max(0.1, poll_interval_seconds))


# --- Write-tool helpers ------------------------------------------------------


def _require_writes_enabled() -> None:
    """Defense-in-depth: write tools shouldn't be registered when read-only,
    but if anything slipped through, refuse to act."""
    if _get_settings().read_only:
        raise RuntimeError(
            "Write operations are disabled (LF_READ_ONLY=true). Restart with "
            "LF_READ_ONLY=false to enable write tools."
        )


async def _fetch_entry_for_op(
    operation: str, entry_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an entry; return ``(entry, None)`` or ``(None, error_dict)``.

    Used by write tools that need the entry's metadata before acting
    (path-fence checks, preview-build, etc.). Replaces the older raise
    pattern so failures surface as structured tool errors instead of
    opaque "Error executing tool" strings.
    """
    try:
        return await _client().get_entry(entry_id), None
    except LaserficheError as exc:
        return None, _classify_lf_error(operation, exc, entry_id=entry_id)


def _entry_name(entry: dict[str, Any]) -> str:
    return entry.get("name") or entry.get("Name") or ""


def _entry_type(entry: dict[str, Any]) -> str:
    return entry.get("entryType") or entry.get("EntryType") or ""


# --- Error classification ---------------------------------------------------
# Maps Laserfiche server errors into structured tool-error responses with
# stable machine-readable slugs the LLM can branch on. Replaces the old
# "raise RuntimeError" pattern, which surfaced opaque "Error executing tool"
# strings to the LLM.

# Known Laserfiche-specific error codes (the `errorCode` field on
# ProblemDetails responses, distinct from HTTP status).
_LF_ERROR_CODE_AUTH_INVALID = 9010
_LF_ERROR_CODE_LFDS_UNREACHABLE = 9528  # Misleading message; usually = bad creds.
_LF_ERROR_CODE_REQUIRED_FIELD_A = 9039
_LF_ERROR_CODE_REQUIRED_FIELD_B = 9066


def _lf_error_detail(exc: LaserficheError) -> dict[str, Any]:
    """Pull errorCode/title/instance out of a LaserficheError, if present."""
    d = exc.detail
    if isinstance(d, dict):
        # ProblemDetails fields sometimes live at top level, sometimes
        # nested under 'error' (the older Edoc routes use the nested form).
        inner = d.get("error") if isinstance(d.get("error"), dict) else None
        return {**(inner or {}), **{k: v for k, v in d.items() if k != "error"}}
    return {}


def _classify_lf_error(
    operation: str,
    exc: LaserficheError,
    *,
    entry_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a LaserficheError into a structured `mode: error` response.

    Outputs a stable shape:
        {mode, operation, error: <slug>, status_code,
         server_error_code, server_message, reason, [entry_id]}

    The slug is short and machine-readable so LLM callers can branch on
    it (`auth_failed`, `required_field_missing`, `not_found`, ...). The
    reason is a human-readable hint that includes the LF code/message
    when present.
    """
    detail = _lf_error_detail(exc)
    error_code = detail.get("errorCode")
    title = detail.get("title") or detail.get("message")
    status = exc.status_code

    if error_code in (_LF_ERROR_CODE_AUTH_INVALID, _LF_ERROR_CODE_LFDS_UNREACHABLE):
        slug = "auth_failed"
        reason = (
            "Laserfiche rejected the credentials. Verify LF_USERNAME and "
            "LF_PASSWORD; on self-hosted, 9528 ('LFDS unreachable') is "
            "misleadingly worded and most often also means bad creds."
        )
    elif error_code in (_LF_ERROR_CODE_REQUIRED_FIELD_A, _LF_ERROR_CODE_REQUIRED_FIELD_B):
        slug = "required_field_missing"
        reason = (
            "The repository has one or more required fields that aren't "
            "set on this entry. Call list_field_definitions and supply "
            "isRequired=true fields via the tool's `fields` parameter."
        )
    elif status in (401, 403):
        slug = "auth_failed"
        reason = (
            "HTTP 401/403 — credentials or permissions reject this "
            "operation. Confirm the service account has rights on the "
            "target path."
        )
    elif status == 404:
        slug = "not_found"
        reason = "Server returned 404 — the entry, path, or endpoint does not exist."
    elif status == 405:
        slug = "method_not_allowed"
        reason = (
            "HTTP 405 — the URL didn't match a route with this HTTP "
            "method. Usually an MCP routing bug or a build that doesn't "
            "expose the endpoint."
        )
    elif status == 415:
        slug = "unsupported_media_type"
        reason = (
            "HTTP 415 — the server expected a Content-Type the request "
            "didn't supply. Usually a wire-format bug in the MCP."
        )
    elif status == 429:
        slug = "rate_limited"
        reason = "HTTP 429 — slow down and retry after a delay."
    elif status is not None and status >= 500:
        slug = "server_error"
        reason = f"HTTP {status} from the Laserfiche server."
    else:
        slug = "server_error"
        reason = title or str(exc)[:300]

    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "error": slug,
        "status_code": status,
        "server_error_code": error_code,
        "server_message": title,
        "reason": reason,
    }
    if entry_id is not None:
        out["entry_id"] = entry_id
    if extra:
        out.update(extra)
    return out


def _invalid_token_response(
    operation: str, entry_id: int, reason: str | None,
) -> dict[str, Any]:
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_confirmation_token",
        "reason": reason,
        "next_step": (
            "Re-run the same tool without confirmation_token to get a fresh "
            "preview and a new token."
        ),
    }


def _entry_path(entry: dict[str, Any]) -> str | None:
    return entry.get("fullPath") or entry.get("FullPath")


def _check_write_permission(
    operation: str,
    *,
    path: str | None = None,
) -> dict[str, Any] | None:
    """Run pre-write guards. Returns None on success, or an error dict to return.

    Two checks, in order:
        1. Tool allowlist (LF_WRITE_TOOLS_ALLOWED) — refuses operations not
           in the operator-configured set. This is defense-in-depth on top
           of the registration-time filter, in case a tool is invoked
           directly (e.g., by the test suite).
        2. Path scope (LF_WRITE_PATHS_ALLOW / LF_WRITE_PATHS_DENY) — refuses
           mutations on entries outside the configured prefixes.

    The caller is responsible for fetching the entry (or its parent, for
    create ops) and passing its fullPath in. We don't fetch here because
    many tools already need the entry for other reasons (preview, token
    binding) and we want to avoid duplicate round-trips.
    """
    settings = _get_settings()

    ok, reason = permissions.tool_allowed(operation, settings.write_tools_allowed)
    if not ok:
        return {
            "mode": "error",
            "operation": operation,
            "error": "tool_not_allowed",
            "reason": reason,
        }

    ok, reason = permissions.path_allowed(
        path, settings.write_paths_allow, settings.write_paths_deny,
    )
    if not ok:
        return {
            "mode": "error",
            "operation": operation,
            "error": "path_not_allowed",
            "reason": reason,
            "path": path,
        }

    return None


async def _check_write_for_entry(
    operation: str, entry_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch entry and run write-permission checks.

    Returns ``(entry, error)`` — exactly one is None. Tools that don't need
    the entry for any other reason can use this to consolidate the fetch
    and the path check.
    """
    entry, err = await _fetch_entry_for_op(operation, entry_id)
    if err is not None:
        return None, err
    err = _check_write_permission(operation, path=_entry_path(entry))
    if err is not None:
        return None, err
    return entry, None


async def _check_write_for_parent(
    operation: str, parent_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Same as _check_write_for_entry but checks the PARENT folder's path.

    Used by create operations (create_folder, copy_entry, import_document)
    where the target entry doesn't exist yet — we fence on where it would
    land.
    """
    parent, err = await _fetch_entry_for_op(operation, parent_id)
    if err is not None:
        return None, err
    err = _check_write_permission(operation, path=_entry_path(parent))
    if err is not None:
        return None, err
    return parent, None


def _fields_to_put_body(field_values: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a get_field_values listing into the put_fields body shape.

    API returns: ``[{ fieldName, values: [{value, position}], ... }, ...]``
    PUT expects:  ``{ FieldA: { values: [{value, position}] }, ... }``
    """
    out: dict[str, Any] = {}
    for fv in field_values:
        name = fv.get("fieldName") or fv.get("FieldName")
        if not name:
            continue
        values = fv.get("values") or fv.get("Values") or []
        out[name] = {"values": values}
    return out


def _user_fields_to_values(updates: dict[str, list[Any]]) -> dict[str, Any]:
    """Convert caller field updates into the API's ``FieldToUpdate`` shape.

    Example: ``{"Name": ["Smith"]}`` becomes
    ``{"Name": {"values": [{"value": "Smith", "position": 1}]}}``.

    Per the Repository API swagger, ``ValueToUpdate.position`` is
    1-indexed for multi-value fields and ignored for single-value
    fields, so we start at 1.
    """
    out: dict[str, Any] = {}
    for name, vals in updates.items():
        out[name] = {
            "values": [
                {"value": v, "position": i + 1} for i, v in enumerate(vals)
            ],
        }
    return out


# --- Write tools (registered conditionally in main()) ------------------------


async def set_fields(
    entry_id: int,
    fields: dict[str, list[Any]],
) -> dict[str, Any]:
    """OVERWRITE all field values on an entry. Destructive — read carefully.

    **Prefer ``merge_fields``** for any "set field X to Y" intent. This
    tool follows the raw ``PUT /fields`` semantics: any field on the
    entry that is NOT in ``fields`` is deleted (independent fields) or
    reset to empty (templated fields). Use this only when you want that
    delete-everything-else behavior explicitly — e.g. clearing a stale
    snapshot before assigning a new one.

    Args:
        entry_id: Integer entry ID to write to.
        fields: Mapping of field name → list of values. Single-value
            fields take a one-item list; multi-value fields take many.
            Example: ``{"Last Name": ["Smith"], "Hire Date": ["2024-01-15"]}``.
            To clear a field, pass an empty list (``"Note": []``).

    Returns: The server's updated field listing on success.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path falls outside
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``required_field_missing``
    (you're trying to clear a required field), ``auth_failed``,
    ``not_found``.
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("set_fields", entry_id)
    if err:
        return err
    name_err = await _validate_field_names("set_fields", entry_id, list(fields.keys()))
    if name_err is not None:
        return name_err
    body = _user_fields_to_values(fields)
    try:
        raw = await _client().put_fields(entry_id, body)
    except LaserficheError as exc:
        return _classify_lf_error("set_fields", exc, entry_id=entry_id)
    return raw


async def merge_fields(
    entry_id: int,
    updates: dict[str, list[Any]],
) -> dict[str, Any]:
    """Update specific fields on an entry, preserving the rest.

    **The right default for "set field X to Y" intents.** This GET-then-PUT
    helper reads the entry's current field values, layers ``updates`` on
    top, and PUTs the union — fields not mentioned in ``updates`` keep
    their existing values. Use ``set_fields`` only when you specifically
    want overwrite-everything-else semantics.

    Args:
        entry_id: Integer entry ID to update.
        updates: Mapping of field name → list of values. Single-value
            fields take a one-item list; multi-value fields take many.
            Example: ``{"Last Name": ["Smith"], "Hire Year": ["2025"]}``.
            Pass an empty list (``"Note": []``) to clear a specific
            field while leaving others alone.

    Returns: ``{"mode": "executed", "operation": "merge_fields",
    "entry_id": <int>, "fields_updated": [...], "fields_preserved": [...],
    "result": <server response>}``. The ``fields_updated`` and
    ``fields_preserved`` arrays make it easy to confirm exactly what
    changed.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path falls outside the
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``required_field_missing`` (clearing a required field),
    ``auth_failed``.
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("merge_fields", entry_id)
    if err:
        return err
    name_err = await _validate_field_names("merge_fields", entry_id, list(updates.keys()))
    if name_err is not None:
        return name_err
    client = _client()
    try:
        current = await client.get_field_values(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("merge_fields", exc, entry_id=entry_id)

    items = current.get("value") or current.get("Value") or []
    body = _fields_to_put_body(items)
    body.update(_user_fields_to_values(updates))
    try:
        raw = await client.put_fields(entry_id, body)
    except LaserficheError as exc:
        return _classify_lf_error("merge_fields", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "merge_fields",
        "entry_id": entry_id,
        "fields_updated": list(updates.keys()),
        "fields_preserved": [
            k for k in body if k not in updates
        ],
        "result": raw,
    }


async def set_tags(
    entry_id: int,
    tags: list[str],
) -> dict[str, Any]:
    """OVERWRITE the tag list on an entry. Destructive — read carefully.

    Any tag currently on the entry that is NOT in ``tags`` will be
    removed. **Prefer ``merge_tags``** for additive intents — it adds
    and removes specific tags without disturbing the rest.

    Tags must already exist as repository-level tag definitions (see
    ``list_tag_definitions``); the server rejects unknown tag names.

    Args:
        entry_id: Integer entry ID.
        tags: Full list of tag names that should be on the entry after
            this call. Pass ``[]`` to clear all tags.

    Returns: The server's updated tag listing.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``, ``server_error`` (typically caused by a tag name
    that isn't a defined tag — run ``list_tag_definitions`` first).
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("set_tags", entry_id)
    if err:
        return err
    tag_err = await _validate_tag_names("set_tags", entry_id, tags)
    if tag_err is not None:
        return tag_err
    try:
        raw = await _client().put_tags(entry_id, tags)
    except LaserficheError as exc:
        return _classify_lf_error("set_tags", exc, entry_id=entry_id)
    return raw


async def merge_tags(
    entry_id: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add and/or remove specific tags on an entry, preserving the rest.

    GET-then-PUT helper: reads the entry's current tags, applies
    ``add`` and ``remove`` deltas, and PUTs the union. Tags in
    ``remove`` that aren't currently on the entry are silently ignored.
    Tags in ``add`` that are already on the entry are no-ops.

    Tags must already exist as repository-level tag definitions (see
    ``list_tag_definitions``).

    Args:
        entry_id: Integer entry ID.
        add: Tag names to add. Default empty.
        remove: Tag names to remove. Default empty.

    Returns: ``{"mode": "executed", "operation": "merge_tags",
    "entry_id": <int>, "added": [...], "removed": [...],
    "final_tags": [...], "result": <server response>}``. ``added``
    and ``removed`` reflect what actually changed (excluding no-ops),
    so the LLM can confirm the delta.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``.
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("merge_tags", entry_id)
    if err:
        return err
    tag_err = await _validate_tag_names(
        "merge_tags", entry_id, list((add or []) + (remove or [])),
    )
    if tag_err is not None:
        return tag_err
    client = _client()
    try:
        current = await client.get_tags(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("merge_tags", exc, entry_id=entry_id)
    items = current.get("value") or current.get("Value") or []
    existing = {
        (t.get("name") or t.get("Name")) for t in items if (t.get("name") or t.get("Name"))
    }
    add_set = set(add or [])
    remove_set = set(remove or [])
    final = (existing | add_set) - remove_set
    try:
        raw = await client.put_tags(entry_id, sorted(final))
    except LaserficheError as exc:
        return _classify_lf_error("merge_tags", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "merge_tags",
        "entry_id": entry_id,
        "added": sorted(add_set - existing),
        "removed": sorted(remove_set & existing),
        "final_tags": sorted(final),
        "result": raw,
    }


async def set_links(
    entry_id: int,
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    """OVERWRITE the entry-link list on an entry. Destructive — read carefully.

    Any link currently on the entry that is NOT in ``links`` will be
    removed. There is no ``merge_links`` helper — if you only want to
    add a link, first call ``get_entry`` (or inspect via the web client)
    to read the existing links, then call this with the full set.

    Args:
        entry_id: Integer entry ID — the source of each link.
        links: List of link descriptors. Each item is
            ``{"targetId": <int>, "linkTypeId": <int>}`` where
            ``linkTypeId`` comes from ``list_link_definitions`` (the
            ``Supersedes`` type, ``Attachment``, etc.) and ``targetId``
            is the other entry's ID. Pass ``[]`` to clear all links.

    Returns: The server's updated link listing.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — source entry outside the allow list.
          (Targets aren't fenced — the link metadata lives on the
          source, not the target.)

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (source or
    target doesn't exist), ``auth_failed``, ``server_error`` (typically
    an invalid ``linkTypeId``).
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("set_links", entry_id)
    if err:
        return err
    link_type_ids = [
        link.get("linkTypeId") for link in links
        if isinstance(link.get("linkTypeId"), int)
    ]
    link_err = await _validate_link_types("set_links", entry_id, link_type_ids)
    if link_err is not None:
        return link_err
    try:
        raw = await _client().put_links(entry_id, links)
    except LaserficheError as exc:
        return _classify_lf_error("set_links", exc, entry_id=entry_id)
    return raw


def _validate_name(
    operation: str, name: str, *, extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a mode:error response if ``name`` fails entry-name validation.

    Pure-function check via ``permissions.name_allowed``. Used by every
    write tool that creates or renames an entry (``create_folder``,
    ``import_document``, ``copy_entry``, ``rename_entry``,
    ``move_entry`` when ``new_name`` is set).
    """
    ok, reason = permissions.name_allowed(name)
    if ok:
        return None
    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "error": "invalid_name",
        "reason": reason,
        "name": name,
    }
    if extra:
        out.update(extra)
    return out


def _validate_page_range_input(
    operation: str, entry_id: int, range_str: str,
) -> dict[str, Any] | None:
    """Return a mode:error response if ``range_str`` fails the syntax check.

    Distinct from the existing empty-range guard in ``delete_pages``:
    this catches malformed-but-non-empty inputs like ``"1, 2"``,
    ``"abc"``, ``"5-3"``, ``"0,1"``.
    """
    ok, reason = permissions.validate_page_range(range_str)
    if ok:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_page_range",
        "reason": reason,
        "page_range": range_str,
    }


async def _validate_field_names(
    operation: str,
    entry_id: int,
    field_names: list[str],
) -> dict[str, Any] | None:
    """Return a mode:error response if any ``field_names`` is not a defined field.

    Uses the cached field-definition lookup on the client. Returns None
    when every name resolves; returns a structured error listing the
    unknown names plus a sample of valid names when at least one fails.
    Falls back to None (let the real PUT run and surface the upstream
    error) on lookup failure so we don't block writes on a transient
    cache miss. Skipped when ``LF_VALIDATE_NAMES=false``.
    """
    if not _get_settings().validate_names:
        return None
    client = _client()
    try:
        defs = await client.cached_field_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [name for name in field_names if name not in defs]
    if not unknown:
        return None
    valid_sample = sorted(defs.keys())[:20]
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_field_name",
        "reason": (
            f"Field name(s) {unknown!r} are not defined in this repository. "
            "Call list_field_definitions to see available fields."
        ),
        "invalid_field_names": unknown,
        "valid_field_names_sample": valid_sample,
    }


async def _validate_template_name(
    operation: str,
    template_name: str,
    *,
    entry_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a mode:error response if ``template_name`` is not a defined template.

    Case-sensitive (matches the server's matching behavior on most v1
    builds). Falls back to None on lookup failure.
    """
    if not template_name:
        return None  # empty/None template_name is a clear-template signal
    if not _get_settings().validate_names:
        return None
    client = _client()
    try:
        defs = await client.cached_template_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    if template_name in defs:
        return None
    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "error": "invalid_template_name",
        "reason": (
            f"Template {template_name!r} is not defined in this repository. "
            "Call list_template_definitions to see available templates. "
            "Match is case-sensitive."
        ),
        "template_name": template_name,
        "valid_template_names": sorted(defs.keys()),
    }
    if entry_id is not None:
        out["entry_id"] = entry_id
    if extra:
        out.update(extra)
    return out


async def _validate_tag_names(
    operation: str, entry_id: int, tag_names: list[str],
) -> dict[str, Any] | None:
    """Return a mode:error response if any tag in ``tag_names`` is not defined.

    Skips when the list is empty. Falls back to None on lookup failure.
    Skipped entirely when ``LF_VALIDATE_NAMES=false``.
    """
    if not tag_names:
        return None
    if not _get_settings().validate_names:
        return None
    client = _client()
    try:
        defs = await client.cached_tag_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [name for name in tag_names if name not in defs]
    if not unknown:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_tag_name",
        "reason": (
            f"Tag name(s) {unknown!r} are not defined in this repository. "
            "Call list_tag_definitions to see available tags."
        ),
        "invalid_tag_names": unknown,
        "valid_tag_names": sorted(defs.keys()),
    }


async def _validate_link_types(
    operation: str, entry_id: int, link_type_ids: list[int],
) -> dict[str, Any] | None:
    """Return a mode:error response if any linkTypeId is not a defined link type.

    Falls back to None on lookup failure. Skipped when
    ``LF_VALIDATE_NAMES=false``.
    """
    if not link_type_ids:
        return None
    if not _get_settings().validate_names:
        return None
    client = _client()
    try:
        defs = await client.cached_link_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [lid for lid in link_type_ids if lid not in defs]
    if not unknown:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_link_type",
        "reason": (
            f"linkTypeId(s) {unknown!r} are not defined in this repository. "
            "Call list_link_definitions to see available link types."
        ),
        "invalid_link_type_ids": unknown,
        "valid_link_type_ids": sorted(defs.keys()),
    }


async def _validate_required_fields(
    operation: str,
    entry_id: int,
    caller_fields: dict[str, list[Any]] | None,
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if the entry is missing repo-required fields.

    Returns None when everything required is either already on the entry
    or being supplied in ``caller_fields``. Skipped when
    LF_VALIDATE_REQUIRED_FIELDS is false. Falls back to None on any
    validation-read failure so the real PUT still runs and the server's
    own error path is what surfaces.
    """
    settings = _get_settings()
    if not settings.validate_required_fields:
        return None
    client = _client()
    try:
        defs = await client.list_field_definitions(max_results=500, skip=0)
        current = await client.get_field_values(entry_id)
    except LaserficheError:
        return None  # let the actual call surface the error

    required_names: list[dict[str, Any]] = []
    for fd in defs.get("value") or []:
        if fd.get("isRequired"):
            required_names.append(fd)
    if not required_names:
        return None

    already_set = {
        (fv.get("fieldName") or "") for fv in (current.get("value") or [])
        if fv.get("values")
    }
    being_supplied = set(caller_fields.keys()) if caller_fields else set()

    missing = [
        fd for fd in required_names
        if fd["name"] not in already_set and fd["name"] not in being_supplied
    ]
    if not missing:
        return None

    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "missing_required_fields",
        "missing": [fd["name"] for fd in missing],
        "field_details": [
            {
                "name": fd["name"],
                "field_type": fd.get("fieldType"),
                "list_values": fd.get("listValues") or [],
                "default_value": fd.get("defaultValue"),
            }
            for fd in missing
        ],
        "next_step": (
            f"Call {operation} again with `fields=` including each of "
            f"these names. List fields offer constraint values via "
            f"list_values; date/text fields accept literal values. "
            f"Disable this check with LF_VALIDATE_REQUIRED_FIELDS=false."
        ),
    }


async def assign_template(
    entry_id: int,
    template_name: str,
    fields: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Assign a template to an entry, optionally with initial field values.

    Use to attach a template (e.g. "Personnel Document", "Service Record")
    so the entry exposes that template's fields. Existing independent
    fields on the entry are unchanged. Fields common to the previously
    and newly assigned templates retain their values.

    Args:
        entry_id: Integer entry ID to template.
        template_name: Exact name of the template. Case-sensitive on most
            builds. Use ``list_template_definitions`` to discover what's
            available.
        fields: Optional initial field values to set in the same call.
            Mapping of field name → list of values (one item for
            single-value fields, many for multi-value). Often required
            because templates declare required fields — the validator
            below will tell you which.

    Returns: The server's updated entry on success, showing the new
    ``templateName`` and ``templateFieldNames``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``missing_required_fields`` — repository-wide required fields
          (``isRequired=true``, regardless of template membership) aren't
          set on the entry and weren't supplied in ``fields``. The
          response includes ``missing`` (names) and ``field_details``
          (with ``field_type``, ``list_values``, ``default_value``) so
          you can ask the user for valid values, or pick a default,
          then retry. Disable this check with
          ``LF_VALIDATE_REQUIRED_FIELDS=false``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "template_name": <str>, ...}``. Common slugs:
    ``required_field_missing`` (server-side equivalent if the validator
    is disabled), ``not_found`` (entry or template doesn't exist),
    ``auth_failed``.
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("assign_template", entry_id)
    if err:
        return err
    template_err = await _validate_template_name(
        "assign_template", template_name, entry_id=entry_id,
    )
    if template_err is not None:
        return template_err
    if fields:
        field_err = await _validate_field_names(
            "assign_template", entry_id, list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    validation_error = await _validate_required_fields(
        "assign_template", entry_id, fields,
    )
    if validation_error is not None:
        return validation_error
    body_fields = _user_fields_to_values(fields) if fields else None
    try:
        raw = await _client().assign_template(
            entry_id, template_name, fields=body_fields,
        )
    except LaserficheError as exc:
        return _classify_lf_error(
            "assign_template", exc, entry_id=entry_id,
            extra={"template_name": template_name},
        )
    return raw


async def remove_template(entry_id: int) -> dict[str, Any]:
    """Clear the template assigned to an entry.

    Removes the template association; templated field values are
    cleared, independent fields are untouched. Use when the entry was
    misclassified or when changing categorization without picking a
    replacement template (otherwise just call ``assign_template`` —
    the server handles the swap atomically).

    Args:
        entry_id: Integer entry ID.

    Returns: The server's updated entry on success, with
    ``templateName=""`` and empty ``templateFieldNames``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``.
    """
    _require_writes_enabled()
    _, err = await _check_write_for_entry("remove_template", entry_id)
    if err:
        return err
    try:
        raw = await _client().remove_template(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("remove_template", exc, entry_id=entry_id)
    return raw


async def create_folder(
    parent_id: int,
    name: str,
    template_name: str | None = None,
    fields: dict[str, list[Any]] | None = None,
    auto_rename: bool = False,
) -> dict[str, Any]:
    """Create a new folder as a child of ``parent_id``.

    Args:
        parent_id: Integer entry ID of the destination folder. Root is
            typically ID 1. Resolve a path to an ID with
            ``get_entry_by_path`` first if you only have a path string.
        name: New folder name. Backslashes are not allowed in entry
            names; pick something path-safe.
        template_name: Optional template to assign on creation. Use
            ``list_template_definitions`` to discover names.
        fields: Optional initial template-field values. Same shape as
            other field-writing tools: ``{"Status": ["Active"]}``. If
            the template (or repository-wide required fields) demand
            values you don't supply, the server will reject.
        auto_rename: When true, the server appends a numeric suffix if
            ``name`` already exists in the parent. When false (default),
            a collision returns an error.

    Returns: The server's entry payload for the new folder on success —
    ``id``, ``name``, ``parentId``, ``fullPath``, ``templateName``,
    ``creationTime``, ``creator``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — parent's path falls outside
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "parent_id": <int>, "name": <str>, ...}``. Common slugs:
    ``not_found`` (parent doesn't exist), ``required_field_missing``
    (template you're assigning has required fields you didn't supply),
    ``auth_failed``.
    """
    _require_writes_enabled()
    name_err = _validate_name(
        "create_folder", name, extra={"parent_id": parent_id},
    )
    if name_err is not None:
        return name_err
    _, err = await _check_write_for_parent("create_folder", parent_id)
    if err:
        return err
    if template_name:
        template_err = await _validate_template_name(
            "create_folder", template_name, extra={"parent_id": parent_id, "name": name},
        )
        if template_err is not None:
            return template_err
    if fields:
        field_err = await _validate_field_names(
            "create_folder", parent_id, list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    body_fields = _user_fields_to_values(fields) if fields else None
    try:
        raw = await _client().create_child_entry(
            parent_id,
            entry_type="Folder",
            name=name,
            template_name=template_name,
            fields=body_fields,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return _classify_lf_error(
            "create_folder", exc,
            extra={"parent_id": parent_id, "name": name},
        )
    return raw


async def copy_entry(
    source_id: int,
    parent_id: int,
    name: str,
    auto_rename: bool = False,
) -> dict[str, Any]:
    """Copy an existing entry into a new location with a new name.

    Works for documents and folders (folders copy with their entire
    subtree — large copies can take minutes). The copy is server-side,
    so no bytes flow through the MCP. Original entry is unchanged.

    **Async**: returns immediately with an ``operation_token``. Poll
    completion with ``get_task_status`` or block with ``wait_for_task``
    — the final ``entryId`` for the new copy is in the task payload's
    ``entryId`` field once status is ``Completed``.

    Args:
        source_id: Integer entry ID of the entry to copy.
        parent_id: Integer entry ID of the destination folder.
        name: New name for the copy. Must be path-safe (no backslashes).
        auto_rename: When true, server appends a numeric suffix if
            ``name`` collides in the destination. Default false.

    Returns: ``{"token": <operation_token>}`` on success. Pass that
    token to ``wait_for_task(token)`` to get the actual copy result
    once it finishes.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — destination parent's path falls
          outside the allow list. (The source's path isn't fenced —
          this is a copy, not a move; the source is unchanged.)

    On failure: returns ``{"mode": "error", "error": <slug>,
    "source_id": <int>, "parent_id": <int>, "name": <str>, ...}``.
    Common slugs: ``not_found`` (source or parent doesn't exist),
    ``auth_failed``.
    """
    _require_writes_enabled()
    name_err = _validate_name(
        "copy_entry", name,
        extra={"source_id": source_id, "parent_id": parent_id},
    )
    if name_err is not None:
        return name_err
    _, err = await _check_write_for_parent("copy_entry", parent_id)
    if err:
        return err
    try:
        raw = await _client().copy_entry_async(
            parent_id,
            source_id=source_id,
            name=name,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return _classify_lf_error(
            "copy_entry", exc,
            extra={"source_id": source_id, "parent_id": parent_id, "name": name},
        )
    return raw


async def import_document(
    parent_id: int,
    name: str,
    file_path: str,
    template_name: str | None = None,
    fields: dict[str, list[Any]] | None = None,
    tags: list[str] | None = None,
    content_type: str | None = None,
    auto_rename: bool = False,
) -> dict[str, Any]:
    """Upload a local file as a new document into a Laserfiche folder.

    The file is read from the local filesystem **as seen by the MCP
    server process** — typically the same machine that runs Claude
    Desktop/Code. The server reads, then POSTs the bytes as a multipart
    upload to the Laserfiche Repository API.

    Args:
        parent_id: Integer entry ID of the destination folder.
        name: Filename to use inside Laserfiche (extension matters for
            content-type sniffing). Backslashes are not allowed.
        file_path: Absolute or working-directory-relative path to the
            local file. Must exist and be readable by the MCP process.
        template_name: Optional template to assign on import.
        fields: Optional template-field values to set on import. Same
            shape as other field-writing tools.
        tags: Optional list of tag names to attach. Tags must already
            exist as definitions (see ``list_tag_definitions``).
        content_type: Override the auto-detected MIME type if needed
            (e.g. for unusual file extensions). The client sniffs from
            ``name`` if omitted.
        auto_rename: When true, server appends a numeric suffix if
            ``name`` collides in the destination.

    Returns: Server's import payload on success — ``operations``
    (with ``entryCreate.entryId`` for the new document) and
    ``documentLink`` (the API URL of the new entry).

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — parent's path outside the allow list.
        - ``file_not_found`` — ``file_path`` doesn't resolve to a real
          file on the MCP server's filesystem.
        - ``size_exceeds_cap`` — file is larger than ``LF_IMPORT_MAX_BYTES``
          (default 25 MB). The API caps at 100 MB; raise the env var to
          import files between those sizes.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "parent_id": <int>, "name": <str>, "file_path": <str>, ...}``.
    Common slugs: ``not_found`` (parent doesn't exist),
    ``required_field_missing`` (template demanded fields you didn't
    supply), ``auth_failed``.
    """
    _require_writes_enabled()
    name_err = _validate_name(
        "import_document", name, extra={"parent_id": parent_id},
    )
    if name_err is not None:
        return name_err
    _, err = await _check_write_for_parent("import_document", parent_id)
    if err:
        return err
    if template_name:
        template_err = await _validate_template_name(
            "import_document", template_name,
            extra={"parent_id": parent_id, "name": name},
        )
        if template_err is not None:
            return template_err
    if fields:
        field_err = await _validate_field_names(
            "import_document", parent_id, list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    if tags:
        tag_err = await _validate_tag_names("import_document", parent_id, tags)
        if tag_err is not None:
            return tag_err
    import os
    settings = _get_settings()

    if not os.path.isfile(file_path):
        return {
            "mode": "error",
            "operation": "import_document",
            "error": "file_not_found",
            "file_path": file_path,
            "message": f"No file at {file_path!r}.",
        }

    size = os.path.getsize(file_path)
    if size > settings.import_max_bytes:
        return {
            "mode": "error",
            "operation": "import_document",
            "error": "size_exceeds_cap",
            "file_path": file_path,
            "byte_size": size,
            "max_bytes": settings.import_max_bytes,
            "message": (
                f"File is {size} bytes, which exceeds the "
                f"{settings.import_max_bytes}-byte cap. Raise "
                "LF_IMPORT_MAX_BYTES if you really need this file."
            ),
        }

    with open(file_path, "rb") as fh:
        file_bytes = fh.read()

    if content_type is None:
        import mimetypes
        guessed, _ = mimetypes.guess_type(name)
        content_type = guessed or "application/octet-stream"

    metadata: dict[str, Any] | None = None
    if template_name or fields or tags:
        meta_inner: dict[str, Any] = {}
        if template_name:
            meta_inner["templateName"] = template_name
        if fields:
            meta_inner["fields"] = _user_fields_to_values(fields)
        if tags:
            meta_inner["tags"] = tags
        metadata = {"metadata": meta_inner}

    try:
        raw = await _client().import_document(
            parent_id,
            name,
            file_bytes,
            content_type=content_type,
            metadata=metadata,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return _classify_lf_error(
            "import_document", exc,
            extra={"parent_id": parent_id, "name": name, "file_path": file_path},
        )
    return raw


# --- Move / Rename: two-step (preview + confirmation token) ------------------


async def rename_entry(
    entry_id: int,
    new_name: str,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Rename an entry. **Two-step: preview first, then execute with token.**

    Renaming changes the entry's name AND its ``fullPath``. External
    references (shortcuts, links from other entries, deep links into the
    web client, hardcoded paths in workflows) break — read the preview's
    ``would_be_full_path`` carefully and confirm with the user before
    passing the token back.

    **Step 1 — preview**
        Call with ``confirmation_token=None`` (omitted). Response:

        ```json
        {
          "mode": "preview",
          "operation": "rename_entry",
          "entry_id": 84493,
          "current_name": "probe.txt",
          "new_name": "probe-final.txt",
          "current_full_path": "\\Sandbox\\probe.txt",
          "would_be_full_path": "\\Sandbox\\probe-final.txt",
          "entry_type": "Document",
          "warning": "Renaming changes the entry's full path ...",
          "confirmation_token": "<opaque base64>",
          "ttl_seconds": 300,
          "next_step": "Surface this preview ..."
        }
        ```

        Surface the preview to the user. Do NOT silently round-trip both
        calls without showing the user the would-be path.

    **Step 2 — execute**
        Re-call with the same ``entry_id``, the same ``new_name``, and
        the ``confirmation_token`` from step 1. Returns
        ``{"mode": "executed", ..., "result": <updated entry>}``.

    Args:
        entry_id: Integer entry ID.
        new_name: New name. Path-safe (no backslashes).
        confirmation_token: From the preview response. HMAC-signed and
            bound to ``(operation, entry_id, current entry name)`` — a
            token can't be replayed for a different entry. Expires after
            5 minutes; server restart invalidates all pending tokens.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path outside the allow list.
        - ``invalid_confirmation_token`` — token expired, tampered, or
          for a different entry. Re-run step 1 to get a fresh one.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "new_name": <str>, ...}``. Common slugs:
    ``not_found``, ``auth_failed``.
    """
    _require_writes_enabled()
    name_err = _validate_name(
        "rename_entry", new_name, extra={"entry_id": entry_id},
    )
    if name_err is not None:
        return name_err
    entry, fetch_err = await _fetch_entry_for_op("rename_entry", entry_id)
    if fetch_err is not None:
        return fetch_err
    err = _check_write_permission("rename_entry", path=_entry_path(entry))
    if err:
        return err
    current_name = _entry_name(entry)

    if confirmation_token is None:
        token = confirmation.create_token("rename_entry", entry_id, current_name)
        current_path = entry.get("fullPath") or entry.get("FullPath") or ""
        folder_path = entry.get("folderPath") or entry.get("FolderPath")
        if folder_path:
            would_be_path = f"{folder_path}\\{new_name}".rstrip("\\")
        elif current_path:
            sep = current_path.rfind("\\")
            would_be_path = current_path[: sep + 1] + new_name if sep >= 0 else new_name
        else:
            would_be_path = new_name
        return {
            "mode": "preview",
            "operation": "rename_entry",
            "entry_id": entry_id,
            "current_name": current_name,
            "new_name": new_name,
            "current_full_path": current_path,
            "would_be_full_path": would_be_path,
            "entry_type": _entry_type(entry),
            "warning": (
                "Renaming changes the entry's full path, which can break "
                "external references (links, shortcuts, bookmarks)."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "rename_entry again with the same entry_id and new_name, "
                "passing the confirmation_token argument."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token, "rename_entry", entry_id, current_name,
    )
    if not ok:
        return _invalid_token_response("rename_entry", entry_id, reason)

    try:
        raw = await _client().patch_entry(entry_id, name=new_name)
    except LaserficheError as exc:
        return _classify_lf_error(
            "rename_entry", exc,
            entry_id=entry_id, extra={"new_name": new_name},
        )
    return {
        "mode": "executed",
        "operation": "rename_entry",
        "entry_id": entry_id,
        "old_name": current_name,
        "new_name": new_name,
        "result": raw,
    }


async def move_entry(
    entry_id: int,
    new_parent_id: int,
    confirmation_token: str | None = None,
    new_name: str | None = None,
) -> dict[str, Any]:
    """Move an entry to a different parent folder. **Two-step: preview, then execute.**

    Same path-change risks as ``rename_entry`` — external references
    break when the entry's ``fullPath`` changes. Folder moves carry the
    whole subtree along, so the blast radius is the whole subtree's
    descendant paths.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes
        ``current_full_path`` and ``would_be_full_path`` (computed from
        the destination parent), plus an HMAC-signed
        ``confirmation_token`` valid for 5 minutes. Surface to the user.

    **Step 2 — execute**
        Re-call with the same arguments plus the token.

    **Path-fence note**: the path check runs on BOTH the source AND the
    destination. A token issued for an allowed source path cannot be
    replayed to land in a denied destination — the destination's path is
    refetched on the execute leg and checked again.

    Args:
        entry_id: Integer entry ID of the entry to move.
        new_parent_id: Integer entry ID of the destination folder.
        new_name: Optional rename to apply in the same operation. If
            omitted, the entry keeps its current name in the new
            location. Path-safe (no backslashes).
        confirmation_token: From the preview response. HMAC-signed,
            5-minute TTL, server-restart-invalidating.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — source OR destination falls outside
          the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "new_parent_id": <int>, ...}``. Common slugs:
    ``not_found`` (entry or destination doesn't exist), ``auth_failed``.
    """
    _require_writes_enabled()
    if new_name is not None:
        name_err = _validate_name(
            "move_entry", new_name,
            extra={"entry_id": entry_id, "new_parent_id": new_parent_id},
        )
        if name_err is not None:
            return name_err
    entry, fetch_err = await _fetch_entry_for_op("move_entry", entry_id)
    if fetch_err is not None:
        return fetch_err
    err = _check_write_permission("move_entry", path=_entry_path(entry))
    if err:
        return err
    current_name = _entry_name(entry)

    # Also fence on the destination — moving an allowed-path entry into
    # a denied folder is still a write-into-deny-zone. We fetch the target
    # here so both the preview and execute paths share the check (a token
    # obtained from a preview must not let the LLM swap in a different,
    # denied new_parent_id on the execute call).
    try:
        target = await _client().get_entry(new_parent_id)
        target_path = target.get("fullPath") or target.get("FullPath") or ""
    except LaserficheError:
        target = None
        target_path = ""
    if target_path:
        dest_err = _check_write_permission("move_entry", path=target_path)
        if dest_err:
            return dest_err

    if confirmation_token is None:
        token = confirmation.create_token("move_entry", entry_id, current_name)
        final_name = new_name or current_name
        would_be_path = (
            f"{target_path}\\{final_name}".rstrip("\\") if target_path else final_name
        )
        return {
            "mode": "preview",
            "operation": "move_entry",
            "entry_id": entry_id,
            "current_name": current_name,
            "new_name": new_name,
            "new_parent_id": new_parent_id,
            "current_full_path": entry.get("fullPath") or entry.get("FullPath"),
            "would_be_full_path": would_be_path,
            "entry_type": _entry_type(entry),
            "warning": (
                "Moving an entry changes its full path. Folder moves take "
                "the entire subtree along. External references (links, "
                "shortcuts, bookmarks) by path will break."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "move_entry again with the same arguments, passing the "
                "confirmation_token."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token, "move_entry", entry_id, current_name,
    )
    if not ok:
        return _invalid_token_response("move_entry", entry_id, reason)

    try:
        raw = await _client().patch_entry(
            entry_id, parent_id=new_parent_id, name=new_name,
        )
    except LaserficheError as exc:
        return _classify_lf_error(
            "move_entry", exc,
            entry_id=entry_id, extra={"new_parent_id": new_parent_id},
        )
    return {
        "mode": "executed",
        "operation": "move_entry",
        "entry_id": entry_id,
        "old_name": current_name,
        "new_parent_id": new_parent_id,
        "new_name": new_name,
        "result": raw,
    }


# --- Destructive ops (delete*) — confirmation token required -----------------


async def delete_entry(
    entry_id: int,
    confirmation_token: str | None = None,
    audit_reason_id: int | None = None,
    comment: str | None = None,
    force_large_delete: bool = False,
) -> dict[str, Any]:
    """Delete an entry. **Two-step: preview, then execute with token.**

    Irreversible without a Laserfiche backup or recycle bin restore.
    Folders cascade — the entire subtree (every document, every
    subfolder) goes with them. The delete itself is async on the server,
    so the execute call returns an ``operation_token`` you can pass to
    ``wait_for_task`` for confirmation.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response:

        ```json
        {
          "mode": "preview",
          "operation": "delete_entry",
          "entry_id": 84490,
          "entry_name": "probe-2",
          "entry_type": "Folder",
          "full_path": "\\Sandbox\\probe-2",
          "immediate_child_count": 2,
          "exceeds_batch_cap": false,
          "batch_cap": 10,
          "audit_reason_required": false,
          "warning": "...recursively delete its descendants (2 immediate children observed).",
          "confirmation_token": "<opaque>",
          "ttl_seconds": 300,
          "next_step": "..."
        }
        ```

        Always surface to the user before passing the token back. The
        ``immediate_child_count`` is an exact count when ≤ batch_cap; if
        ``exceeds_batch_cap=true``, the exact count is unknown but
        ``immediate_child_count`` will be null and the folder has at
        least ``batch_cap + 1`` children.

    **Step 2 — execute**
        Re-call with the ``confirmation_token``. If the preview showed
        ``exceeds_batch_cap=true``, also pass ``force_large_delete=true``
        on the execute leg. If ``audit_reason_required=true``, supply
        ``audit_reason_id`` from ``get_audit_reasons``.

    Args:
        entry_id: Integer entry ID to delete.
        confirmation_token: From the preview response. HMAC-signed,
            5-minute TTL.
        audit_reason_id: Required when ``LF_REQUIRE_AUDIT_REASON=true``.
            Use ``get_audit_reasons`` to enumerate valid IDs.
        comment: Optional free-text comment recorded alongside the
            audit reason.
        force_large_delete: Required when the folder's child count
            exceeds ``LF_DELETE_FOLDER_MAX_DESCENDANTS`` (default 50);
            independent of the confirmation token, so the LLM has to
            explicitly opt in to a large delete.

    Returns on execute: ``{"mode": "executed", "operation_token":
    "<uuid>", ...}``. Pass ``operation_token`` to ``wait_for_task`` to
    confirm completion.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.
        - ``exceeds_batch_cap`` — folder exceeds the cap and
          ``force_large_delete`` wasn't passed. Response includes
          ``immediate_child_count`` (when known) and ``batch_cap``.
        - ``audit_reason_required`` — env demands an audit reason but
          ``audit_reason_id`` wasn't supplied.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``.
    """
    _require_writes_enabled()
    entry, fetch_err = await _fetch_entry_for_op("delete_entry", entry_id)
    if fetch_err is not None:
        return fetch_err
    err = _check_write_permission("delete_entry", path=_entry_path(entry))
    if err:
        return err
    current_name = _entry_name(entry)
    entry_type = _entry_type(entry)
    settings = _get_settings()

    # Probe child count for folders so both preview and execute can reason
    # about the batch cap.
    # We need to know (a) whether the cap is exceeded and (b) the exact
    # child count up to that cap. On LFRepositoryAPI v1 the OData `$count`
    # is page-bound when combined with `$top` (returns page size, not
    # total), so we can't rely on it. Instead, fetch up to `cap + 1`
    # children and use the page length itself: if we got `cap + 1` back,
    # the folder exceeds the cap (exact count unknown); otherwise the
    # returned length IS the exact count.
    child_count: int | None = None
    exceeds_cap = False
    if entry_type == "Folder":
        cap = settings.delete_folder_max_descendants
        try:
            listing = await _client().list_folder(entry_id, max_results=cap + 1)
            items = listing.get("value") or []
            if len(items) > cap:
                exceeds_cap = True
                child_count = None  # exact count unknown beyond the cap
            else:
                child_count = len(items)
        except LaserficheError:
            child_count = None

    if confirmation_token is None:
        token = confirmation.create_token("delete_entry", entry_id, current_name)
        return {
            "mode": "preview",
            "operation": "delete_entry",
            "entry_id": entry_id,
            "entry_name": current_name,
            "entry_type": entry_type,
            "full_path": entry.get("fullPath") or entry.get("FullPath"),
            "immediate_child_count": child_count,
            "exceeds_batch_cap": exceeds_cap,
            "batch_cap": settings.delete_folder_max_descendants,
            "audit_reason_required": settings.require_audit_reason,
            "warning": (
                "This will queue an irreversible delete of this entry"
                + (
                    f" and recursively delete its descendants "
                    f"({child_count} immediate children observed)."
                    if entry_type == "Folder"
                    else "."
                )
                + (
                    f" Child count exceeds the configured cap "
                    f"({settings.delete_folder_max_descendants}); execute "
                    "will require force_large_delete=true."
                    if exceeds_cap
                    else ""
                )
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "delete_entry again with the same entry_id and the "
                "returned confirmation_token."
                + (
                    " You will also need force_large_delete=true."
                    if exceeds_cap else ""
                )
                + (
                    " audit_reason_id is required (LF_REQUIRE_AUDIT_REASON=true) "
                    "— see get_audit_reasons."
                    if settings.require_audit_reason else ""
                )
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token, "delete_entry", entry_id, current_name,
    )
    if not ok:
        return _invalid_token_response("delete_entry", entry_id, reason)

    if exceeds_cap and not force_large_delete:
        return {
            "mode": "error",
            "operation": "delete_entry",
            "entry_id": entry_id,
            "error": "exceeds_batch_cap",
            "immediate_child_count": child_count,
            "batch_cap": settings.delete_folder_max_descendants,
            "reason": (
                f"Folder has {child_count} immediate children, which "
                f"exceeds the configured cap of "
                f"{settings.delete_folder_max_descendants} "
                "(LF_DELETE_FOLDER_MAX_DESCENDANTS). Pass "
                "force_large_delete=true on this call to proceed."
            ),
        }

    if settings.require_audit_reason and audit_reason_id is None:
        return {
            "mode": "error",
            "operation": "delete_entry",
            "entry_id": entry_id,
            "error": "audit_reason_required",
            "reason": (
                "LF_REQUIRE_AUDIT_REASON=true; pass audit_reason_id (and "
                "optionally a comment). Use get_audit_reasons to enumerate "
                "valid IDs for the authenticated user."
            ),
        }

    try:
        raw = await _client().delete_entry(
            entry_id,
            audit_reason_id=audit_reason_id,
            comment=comment,
        )
    except LaserficheError as exc:
        return _classify_lf_error("delete_entry", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "delete_entry",
        "entry_id": entry_id,
        "entry_name": current_name,
        "operation_token": raw.get("token") or raw.get("Token"),
        "task_id": raw.get("taskId") or raw.get("TaskId"),
        "next_step": (
            "Async op queued. Call get_task_status(operation_token) or "
            "wait_for_task(operation_token) to confirm completion."
        ),
    }


async def delete_edoc(
    entry_id: int,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Wipe a document's binary content while keeping the entry metadata.

    **Two-step: preview, then execute with token.** The entry, its
    template, its field values, its links and tags — all survive. Only
    the underlying file (the "edoc" — electronic document) is removed.
    Useful for retention scenarios where you must purge content but
    preserve the metadata audit trail.

    Irreversible without a Laserfiche backup. Use ``delete_entry`` if
    you actually want the entry gone.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes the
        entry's full path, ``page_count``, ``extension``, and an
        HMAC-signed ``confirmation_token`` (5-minute TTL). Surface to
        the user.

    **Step 2 — execute**
        Re-call with the token. Returns ``{"mode": "executed", "result":
        ...}``.

    Args:
        entry_id: Integer entry ID of an electronic document.
        confirmation_token: From the preview. HMAC-signed, 5-minute TTL.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no edoc), ``method_not_allowed`` (server build doesn't
    expose this endpoint), ``auth_failed``.
    """
    _require_writes_enabled()
    entry, fetch_err = await _fetch_entry_for_op("delete_edoc", entry_id)
    if fetch_err is not None:
        return fetch_err
    err = _check_write_permission("delete_edoc", path=_entry_path(entry))
    if err:
        return err
    current_name = _entry_name(entry)

    if confirmation_token is None:
        token = confirmation.create_token("delete_edoc", entry_id, current_name)
        return {
            "mode": "preview",
            "operation": "delete_edoc",
            "entry_id": entry_id,
            "entry_name": current_name,
            "full_path": entry.get("fullPath") or entry.get("FullPath"),
            "page_count": entry.get("pageCount") or entry.get("PageCount"),
            "extension": entry.get("extension") or entry.get("Extension"),
            "warning": (
                "This will permanently delete the document's binary content "
                "(edoc). The entry's metadata, fields, and template remain "
                "but the file itself is gone."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "delete_edoc again with the same entry_id and the token."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token, "delete_edoc", entry_id, current_name,
    )
    if not ok:
        return _invalid_token_response("delete_edoc", entry_id, reason)

    try:
        raw = await _client().delete_edoc(entry_id)
    except LaserficheError as exc:
        return _classify_lf_error("delete_edoc", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "delete_edoc",
        "entry_id": entry_id,
        "entry_name": current_name,
        "result": raw,
    }


async def delete_pages(
    entry_id: int,
    page_range: str,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Delete specific pages from a paginated document. **Two-step: preview, then execute.**

    Only valid on documents that the Laserfiche server treats as
    paginated (PDFs, TIFFs, scanned images). Plain-text files and Office
    documents have ``page_count: 0`` and will fail server-side with
    "Entry does not contain any pages" — use ``delete_edoc`` to wipe the
    whole file instead.

    Irreversible without a backup. Pages are renumbered after deletion,
    so a subsequent delete_pages("1-3") on the same document targets
    different physical pages than before.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes
        ``page_count`` (server-reported, may be null on v1), the
        ``page_range`` you'll be deleting, and an HMAC-signed token.

    **Step 2 — execute**
        Re-call with the same ``page_range`` plus the token.

    Args:
        entry_id: Integer entry ID of a paginated document.
        page_range: Page-range expression. REQUIRED and non-empty.
            Examples: ``"1,2,3"`` (individual pages), ``"1-3,5"``
            (range + single), ``"2-7,10-12"`` (two ranges). The
            Laserfiche API treats empty as "delete all pages", so this
            tool refuses empty values to remove that footgun. If you
            genuinely want to delete every page, pass an explicit wide
            range like ``"1-9999"``.
        confirmation_token: From the preview. HMAC-signed, 5-minute TTL.

    Pre-server errors (returned before the API call):
        - ``page_range_required`` — ``page_range`` was empty/whitespace.
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "page_range": <str>, ...}``. Common slugs:
    ``not_found`` (entry doesn't exist or has no pages — common for
    non-paginated documents), ``method_not_allowed`` (server build
    doesn't expose this endpoint), ``auth_failed``.
    """
    _require_writes_enabled()
    if not page_range or not page_range.strip():
        return {
            "mode": "error",
            "operation": "delete_pages",
            "error": "page_range_required",
            "message": (
                "page_range must be non-empty. The API would treat empty as "
                "'delete all pages' — too easy to fat-finger. Pass an "
                "explicit range like '1-9999' if you intended to delete all."
            ),
        }
    range_err = _validate_page_range_input("delete_pages", entry_id, page_range)
    if range_err is not None:
        return range_err

    entry, fetch_err = await _fetch_entry_for_op("delete_pages", entry_id)
    if fetch_err is not None:
        return fetch_err
    err = _check_write_permission("delete_pages", path=_entry_path(entry))
    if err:
        return err
    current_name = _entry_name(entry)

    if confirmation_token is None:
        token = confirmation.create_token("delete_pages", entry_id, current_name)
        return {
            "mode": "preview",
            "operation": "delete_pages",
            "entry_id": entry_id,
            "entry_name": current_name,
            "full_path": entry.get("fullPath") or entry.get("FullPath"),
            "page_count": entry.get("pageCount") or entry.get("PageCount"),
            "page_range": page_range,
            "warning": (
                f"This will permanently delete pages matching {page_range!r} "
                "from the document. Page deletes are irreversible."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "delete_pages again with the same entry_id, page_range, "
                "and the token."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token, "delete_pages", entry_id, current_name,
    )
    if not ok:
        return _invalid_token_response("delete_pages", entry_id, reason)

    try:
        raw = await _client().delete_pages(entry_id, page_range)
    except LaserficheError as exc:
        return _classify_lf_error(
            "delete_pages", exc,
            entry_id=entry_id, extra={"page_range": page_range},
        )
    return {
        "mode": "executed",
        "operation": "delete_pages",
        "entry_id": entry_id,
        "entry_name": current_name,
        "page_range": page_range,
        "result": raw,
    }


# --- Conditional registration of write tools ---------------------------------


_WRITE_TOOLS = (
    set_fields, merge_fields,
    set_tags, merge_tags, set_links,
    assign_template, remove_template,
    create_folder, copy_entry, import_document,
    rename_entry, move_entry,
    delete_entry, delete_edoc, delete_pages,
)


def _register_write_tools() -> None:
    """Register write tools with FastMCP if and only if LF_READ_ONLY=false.

    Called from main() after settings have been validated, so the tool
    catalog the LLM sees reflects the configured permission level.

    Also honors LF_WRITE_TOOLS_ALLOWED — if set, only tools named in that
    comma-separated env var are registered. Lets operators ship a
    metadata-only or create-only deployment.
    """
    settings = _get_settings()
    if settings.read_only:
        return
    allowed = permissions.parse_tool_allowlist(settings.write_tools_allowed)
    for fn in _WRITE_TOOLS:
        if allowed is not None and fn.__name__ not in allowed:
            continue
        mcp.tool()(fn)


# --- Entrypoint --------------------------------------------------------------


_HELP_TEXT = """\
laserfiche-mcp — Model Context Protocol server for Laserfiche.

Usage:
  laserfiche-mcp                Start the stdio MCP server.
  laserfiche-mcp --diagnose     Probe the configured server and print a
                                deployment-fitness report (no MCP started).
  laserfiche-mcp --help         Show this message.
  laserfiche-mcp --version      Print version and exit.

Options:
  -v, --verbose                 Increase log verbosity (DEBUG). Repeats are
                                accepted but have no further effect.
  -q, --quiet                   Decrease log verbosity (WARNING). Mutually
                                exclusive with --verbose.
  --config PATH                 Load environment from a specific .env file
                                instead of the default $CWD/.env discovery.

Configuration is read from LF_* environment variables (or a .env file in
the working directory). Required at a minimum:

  LF_REPO_API_URL    Base URL of your Repository API Server
  LF_REPOSITORY_ID   Repository name or ID
  LF_USERNAME        Service account username
  LF_PASSWORD        Service account password

See https://github.com/SamuelSHernandez/laserfiche-mcp#configure for the
full list including OAuth, SSL, retry, write-mode safety guards, and
logging knobs.

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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI args. Separated from main() so it's testable in isolation."""
    parser = argparse.ArgumentParser(
        prog="laserfiche-mcp",
        description="Model Context Protocol server for Laserfiche.",
        add_help=False,  # We render our own --help so the layout matches docs.
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-V", "--version", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--config", metavar="PATH", default=None)
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="count", default=0)
    verbosity.add_argument("-q", "--quiet", action="store_true")
    return parser.parse_args(argv)


def _resolve_log_level(settings: Settings, args: argparse.Namespace) -> str:
    """Settings.log_level is the default; --verbose/--quiet override."""
    if args.verbose:
        return "DEBUG"
    if args.quiet:
        return "WARNING"
    return settings.log_level.upper()


async def _run_diagnose(settings: Settings) -> int:
    """Probe the configured server for endpoint availability.

    Prints a deployment-fitness report to stdout and exits with status 0 if
    auth works (regardless of endpoint variability) or 1 if auth itself
    fails. Designed for new adopters figuring out what their LF build
    actually supports.
    """
    auth = build_auth_strategy(settings)

    def line(label: str, status: str, detail: str = "") -> None:
        print(f"  {label:<32} {status}" + (f"  {detail}" if detail else ""))

    print(f"\nlaserfiche-mcp {__version__} — server diagnostic")
    print(f"  Target: {settings.repo_api_url}{settings.repository_id} "
          f"(API {settings.api_version.value})")
    print(f"  Auth:   mode={settings.auth_mode.value}, "
          f"user={settings.username or '(none)'}")
    print()
    print("Endpoint probes:")

    async with LaserficheClient(settings, auth) as client:
        # Auth probe — implicit via any call. Use list_field_definitions
        # since it's small and universally present on healthy builds.
        try:
            await client.list_field_definitions(max_results=1)
            line("Authentication", "OK")
        except LaserficheError as exc:
            line("Authentication", "FAIL", f"HTTP {exc.status_code}: {exc}")
            print("\nAuthentication failed. Check LF_USERNAME / LF_PASSWORD "
                  "and the service account's permissions.")
            return 1

        # Optional endpoints — failures are informative, not fatal.
        async def probe(label: str, coro: object) -> None:
            try:
                await coro  # type: ignore[misc]
                line(label, "OK")
            except LaserficheError as exc:
                code = exc.status_code or "?"
                line(label, f"unavailable (HTTP {code})")

        await probe("List repositories", client.list_repositories())
        await probe("Field definitions", client.list_field_definitions(max_results=1))
        await probe("Template definitions",
                    client.list_template_definitions(max_results=1))
        await probe("Tag definitions", client.list_tag_definitions(max_results=1))
        await probe("Link definitions", client.list_link_definitions(max_results=1))
        await probe("Audit reasons", client.get_audit_reasons())
        await probe("Root folder children",
                    client.list_folder(1, max_results=1))
        await probe('SimpleSearches ({LF:Name="*"})',
                    client.search_entries('{LF:Name="*"}', max_results=1))

    print()
    print("Write mode:")
    line("LF_READ_ONLY", str(settings.read_only).lower())
    if not settings.read_only:
        line("Write paths allow", settings.write_paths_allow or "(none — writes unfenced)")
        line("Write paths deny", settings.write_paths_deny or "(none)")
        line("Write tools allowed",
             settings.write_tools_allowed or "(all 15 write tools)")
        line("Delete batch cap", str(settings.delete_folder_max_descendants))
        line("Audit reason required", str(settings.require_audit_reason).lower())
        line("Validate required fields",
             str(settings.validate_required_fields).lower())

    print()
    print("Done. If anything above failed, see "
          "https://github.com/SamuelSHernandez/laserfiche-mcp#errors "
          "for the relevant error slug.")
    return 0


def main() -> None:
    """Console-script entrypoint registered in pyproject.toml."""
    args = _parse_args(sys.argv[1:])

    if args.help:
        print(_HELP_TEXT)
        return
    if args.version:
        print(f"laserfiche-mcp {__version__}")
        return

    if args.config is not None:
        # Populate the process environment from the named .env file so
        # pydantic-settings picks it up when Settings() is instantiated.
        # `override=False` lets existing env vars win over .env values —
        # matching the standard pydantic-settings precedence.
        if not os.path.isfile(args.config):
            print(
                f"laserfiche-mcp: --config file not found: {args.config}",
                file=sys.stderr,
            )
            sys.exit(2)
        from dotenv import load_dotenv  # type: ignore[import-not-found]
        load_dotenv(args.config, override=False)

    try:
        settings = _get_settings()
    except (ValidationError, ValueError) as exc:
        print(_format_config_error(exc), file=sys.stderr)
        sys.exit(2)
    except NotImplementedError as exc:
        # Cloud mode and api_key auth raise this from the validator.
        print(f"laserfiche-mcp: {exc}", file=sys.stderr)
        sys.exit(2)

    log_level = _resolve_log_level(settings, args)
    logging.basicConfig(level=log_level)

    if args.diagnose:
        exit_code = asyncio.run(_run_diagnose(settings))
        sys.exit(exit_code)

    _register_write_tools()
    logger.info(
        "Starting laserfiche-mcp (read_only=%s, %d write tools registered).",
        settings.read_only,
        0 if settings.read_only else len(_WRITE_TOOLS),
    )

    try:
        mcp.run()  # stdio transport by default
    except KeyboardInterrupt:
        # Don't dump a traceback for ordinary Ctrl-C exits.
        logger.info("laserfiche-mcp stopped.")


if __name__ == "__main__":
    main()
