"""Read tools: raw search, name search, folder listing, entry/path fetch, fields."""

from __future__ import annotations

from typing import Any

from .. import _app
from .._app import clamp_max_results
from ..errors import LaserficheError, classify_lf_error
from ..models import EntryDetail, FieldValue, SearchResults
from ._registry import register


@register(v2_name="laserfiche_entry_search")
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
        raw = await _app.get_client().search_entries(
            query,
            max_results=clamp_max_results(max_results),
        )
    except LaserficheError as exc:
        return classify_lf_error("search", exc)

    return SearchResults.from_api(raw).model_dump()


@register(v2_name="laserfiche_entry_search_by_name")
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
        raw = await _app.get_client().search_entries(
            query,
            max_results=clamp_max_results(max_results),
        )
    except LaserficheError as exc:
        return classify_lf_error("search", exc)

    return SearchResults.from_api(raw).model_dump()


@register(v2_name="laserfiche_folder_list")
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
        raw = await _app.get_client().list_folder(
            folder_id,
            max_results=clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return classify_lf_error("list_folder", exc, extra={"folder_id": folder_id})

    return SearchResults.from_api(raw).model_dump()


@register(v2_name="laserfiche_entry_get")
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
        raw = await _app.get_client().get_entry(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("get_entry", exc, entry_id=entry_id)
    return EntryDetail.from_api(raw).model_dump()


@register(v2_name="laserfiche_entry_get_by_path")
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
    """
    try:
        raw = await _app.get_client().get_entry_by_path(full_path)
    except LaserficheError as exc:
        return classify_lf_error("get_entry_by_path", exc, extra={"full_path": full_path})
    return EntryDetail.from_api(raw).model_dump()


@register(v2_name="laserfiche_field_values_get")
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
        raw = await _app.get_client().get_field_values(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("get_field_values", exc, entry_id=entry_id)
    return {
        "entry_id": entry_id,
        "values": [fv.model_dump() for fv in FieldValue.list_from_api(raw)],
    }
