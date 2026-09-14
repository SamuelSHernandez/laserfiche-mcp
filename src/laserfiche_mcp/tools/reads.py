"""Read tools: raw search, name search, folder listing, entry/path fetch, fields."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import clamp_max_results
from ..errors import LaserficheError, classify_lf_error
from ..models import EntryDetail, FieldValue, SearchResults
from ._registry import register


@register(v2_name="laserfiche_entry_search")
async def search_entries(
    query: Annotated[
        str,
        Field(
            description=(
                "Laserfiche search expression. Each clause wrapped in braces "
                "and combined with `&` (AND) or `|` (OR). Quote string "
                'values with double quotes; escape inner quotes with `\\"`.'
            ),
            examples=[
                '{LF:Name="*.pdf"}',
                '{LF:Basic~="unpaid balance",option="D"}',
                '{LF:Name="Onboarding*"} & {LF:LookIn="\\\\Imports\\\\2024"}',
                '{[Loan Application]:[Last Name]="Smith"}',
            ],
        ),
    ],
    max_results: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Page size. Defaults to LF_MAX_RESULTS_DEFAULT (25). "
                "Capped at LF_MAX_RESULTS_CEILING (typically 200)."
            ),
            ge=1,
            le=1000,
        ),
    ] = None,
) -> dict[str, Any]:
    """Run a raw Laserfiche search query and return matching entries.

    Use when you can already express the search in Laserfiche syntax. Prefer
    ``search_content`` when the question is about what documents *say* (it
    returns the matched passages); ``search_natural`` when you need the
    grammar and available template/field names first; ``search_by_name`` for
    a simple name pattern.

    Syntax: ``{LF:Name="Onboarding*"}`` name pattern; ``{LF:Basic~="phrase"}``
    content search over document text/OCR, fields, annotations and names
    (``,option="D"`` = document text only); ``{[Template]:[Field]="value"}``
    field match; ``{LF:LookIn="\\Path"}`` folder scope; combine clauses with
    ``&`` / ``|``.

    Returns ``entries`` (id, name, entry_type, full_path), ``total_count``,
    ``next_link``. On failure returns ``{"mode": "error", "error": <slug>}``
    (``server_error`` is common — this endpoint is fragile on some builds;
    see docs/error-contract.md).
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
    name_pattern: Annotated[
        str,
        Field(
            description=(
                "Name with optional wildcards. `*` matches any sequence "
                "(including empty); `?` matches exactly one character. "
                "Case-insensitive. No wildcards = exact match."
            ),
            examples=["Onboarding*", "*.pdf", "Smith,?", "Q?-2024-*"],
        ),
    ],
    in_folder_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional backslash-delimited folder path to scope the "
                "search. Forward slashes are also accepted."
            ),
            examples=["\\Imports\\2024", "\\HR\\Onboarding"],
        ),
    ] = None,
    max_results: Annotated[
        int | None,
        Field(
            default=None,
            description="Page size (default 25, capped by LF_MAX_RESULTS_CEILING).",
            ge=1,
            le=1000,
        ),
    ] = None,
) -> dict[str, Any]:
    """Find entries by name pattern, optionally scoped to a folder path.

    Convenience wrapper over ``search_entries`` that builds the
    ``{LF:Name="..."}`` (plus optional ``{LF:LookIn="..."}``) clause for you.
    Matches names only — for document contents use ``search_content``.

    Returns the same shape as ``search_entries``; same error contract.
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
    folder_id: Annotated[
        int,
        Field(
            description="Integer entry ID of the parent folder. The root folder is typically ID 1.",
            ge=1,
        ),
    ],
    max_results: Annotated[
        int | None,
        Field(
            default=None,
            description="Page size (default 25, capped by LF_MAX_RESULTS_CEILING).",
            ge=1,
            le=1000,
        ),
    ] = None,
    skip: Annotated[
        int,
        Field(
            default=0,
            description=(
                "0-indexed offset for pagination. Combine with max_results "
                "to walk a large folder in chunks; check next_link to know "
                "when to stop."
            ),
            ge=0,
        ),
    ] = 0,
) -> dict[str, Any]:
    """List the immediate children (documents and subfolders) of a folder.

    For browse-style navigation from a known folder; the root is typically
    ID 1. Resolve a path string first with ``get_entry_by_path``; to search
    the whole repository use a search tool instead.

    Returns ``entries``, ``total_count`` (when the build supports ``$count``)
    and ``next_link``. On failure returns ``{"mode": "error", "error":
    <slug>, "folder_id": <int>}`` (``not_found``, ``auth_failed``).
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
    """Fetch one entry's metadata: name, type, path, template, page count.

    Does NOT return field values (``get_field_values``) or document content
    (``get_document_edoc``).

    Returns ``EntryDetail``. On failure returns ``{"mode": "error", "error":
    <slug>, "entry_id": <int>}`` (``not_found``, ``auth_failed``).
    """
    try:
        raw = await _app.get_client().get_entry(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("get_entry", exc, entry_id=entry_id)
    return EntryDetail.from_api(raw).model_dump()


@register(v2_name="laserfiche_entry_get_by_path")
async def get_entry_by_path(
    full_path: Annotated[
        str,
        Field(
            description=(
                "Path from the repository root, backslash-separated. "
                "Forward slashes are also accepted. Case-insensitive."
            ),
            examples=[
                "\\Imports\\2024\\Onboarding\\Smith,John",
                "\\HR\\Personnel\\Doe,Jane.pdf",
            ],
        ),
    ],
) -> dict[str, Any]:
    """Resolve a backslash-delimited Laserfiche path to its entry.

    Use when the user refers to a location by path. The returned ``id``
    feeds ``list_folder``, ``get_entry``, ``get_field_values``, etc.

    Returns ``EntryDetail``. On failure returns ``{"mode": "error", "error":
    <slug>, "full_path": <str>}`` (``not_found``, ``auth_failed``).
    """
    try:
        raw = await _app.get_client().get_entry_by_path(full_path)
    except LaserficheError as exc:
        return classify_lf_error("get_entry_by_path", exc, extra={"full_path": full_path})
    return EntryDetail.from_api(raw).model_dump()


@register(v2_name="laserfiche_field_values_get")
async def get_field_values(entry_id: int) -> dict[str, Any]:
    """Read the template field values currently on an entry.

    For metadata questions ("what's the status?", "who's the reviewer?").
    For the entry's own properties use ``get_entry``.

    Returns ``{"values": [...]}`` — each item has ``field_name``, ``values``
    (always a list), ``field_type``, ``is_multi_value``. An empty list
    usually means no template is assigned. On failure returns
    ``{"mode": "error", "error": <slug>, "entry_id": <int>}``.
    """
    try:
        raw = await _app.get_client().get_field_values(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("get_field_values", exc, entry_id=entry_id)
    return {
        "entry_id": entry_id,
        "values": [fv.model_dump() for fv in FieldValue.list_from_api(raw)],
    }
